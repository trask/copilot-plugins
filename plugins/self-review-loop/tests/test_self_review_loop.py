import argparse
import contextlib
import copy
import importlib.util
import inspect
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "self_review_loop.py"
AGENT = Path(__file__).parents[1] / "agents" / "self-review-loop.agent.md"
PLUGIN = Path(__file__).parents[1] / "plugin.json"
SPEC = importlib.util.spec_from_file_location("self_review_loop", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def pending_task_stdout(run_id, session_id, state="in_progress"):
    return json.dumps({
        "schema": MODULE.CANDIDATE_AGENT_TASK_RESULT_SCHEMA,
        "status": "pending",
        "pipeline": {
            "run_id": run_id,
            "session_id": session_id,
            "request_id": "request-1",
        },
        "task": {"id": "task-1", "state": state},
        "candidate": None,
        "completion": None,
    })


class GithubMutationPolicyTest(unittest.TestCase):
    def setUp(self):
        self.previous = MODULE.ACTIVE_GITHUB_MUTATION_POLICY
        MODULE.ACTIVE_GITHUB_MUTATION_POLICY = "source-only"
        self.addCleanup(
            setattr,
            MODULE,
            "ACTIVE_GITHUB_MUTATION_POLICY",
            self.previous,
        )


    def test_pipeline_defaults_source_only_and_requires_explicit_allow(self):
        self.assertEqual(
            "source-only",
            MODULE.github_mutation_policy(
                argparse.Namespace(
                    pipeline_run="run-1",
                    github_mutation_policy=None,
                )
            ),
        )
        self.assertEqual(
            "allow",
            MODULE.github_mutation_policy(
                argparse.Namespace(
                    pipeline_run="run-1",
                    github_mutation_policy="allow",
                )
            ),
        )
        self.assertIn(
            "--github-mutation-policy source-only",
            AGENT.read_text(encoding="utf-8"),
        )



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

    def test_run_hides_windows_console_processes(self):
        completed = MODULE.subprocess.CompletedProcess(["git"], 0, "", "")
        windows, no_window, subprocess_run = self.windows_patches(completed)
        with windows, no_window, subprocess_run as run:
            MODULE.run(["git"])

        self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)
        self.assertEqual(run.call_args.kwargs["env"]["PYTHONIOENCODING"], "utf-8")

    def test_run_bytes_hides_windows_console_processes(self):
        completed = MODULE.subprocess.CompletedProcess(["git"], 0, b"", b"")
        windows, no_window, subprocess_run = self.windows_patches(completed)
        with windows, no_window, subprocess_run as run:
            MODULE.run_bytes(["git"])

        self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)

    def test_run_leaves_non_windows_process_options_unchanged(self):
        completed = MODULE.subprocess.CompletedProcess(["git"], 0, "", "")
        with (
            mock.patch.object(MODULE, "IS_WINDOWS", False),
            mock.patch.object(
                MODULE.subprocess, "run", return_value=completed
            ) as subprocess_run,
        ):
            MODULE.run(["git"])

        self.assertNotIn("creationflags", subprocess_run.call_args.kwargs)

    def test_bounded_run_does_not_set_a_wall_clock_limit(self):
        completed = MODULE.subprocess.CompletedProcess(["git"], 0, "", "")
        with mock.patch.object(MODULE.subprocess, "run", return_value=completed) as subprocess_run:
            MODULE.run(["git"])
        self.assertNotIn("timeout", subprocess_run.call_args.kwargs)

    def test_bounded_dispatch_requires_sealed_execution_result(self):
        completed = MODULE.subprocess.CompletedProcess(["cloud_task"], 0, "", "")
        execution = SimpleNamespace(run=mock.Mock(return_value=completed))
        with mock.patch.object(MODULE, "_EXECUTION", execution):
            self.assertIs(
                MODULE.run(["cloud_task"], require_execution=True), completed
            )
        self.assertIs(execution.run.call_args.kwargs["require_execution"], True)

    def test_bounded_pipeline_runs_without_a_call_deadline(self):
        args = MODULE.build_parser().parse_args([
            "pipeline", "owner/repo#7", "--state", "state.json",
            "--pipeline-run", "a" * 32, "--pipeline-iteration", "1",
            "--pipeline-max-iterations", "2", "--bounded-step",
        ])
        def check_deadline(_args):
            return {"result": "waiting", "task": None}

        with (
            mock.patch.dict(MODULE.os.environ, {"COPILOT_AGENT_SESSION_ID": "session"}),
            mock.patch.object(MODULE, "command_agent_task", side_effect=check_deadline) as task,
            mock.patch.object(MODULE, "emit"),
        ):
            MODULE.command_pipeline(args)
        task.assert_called_once_with(args)

    def test_pending_checkpoint_cannot_claim_a_final_result(self):
        process = MODULE.subprocess.CompletedProcess(
            ["cloud_task"], 0, pending_task_stdout("a" * 32, "session"), ""
        )
        with mock.patch.object(MODULE.Path, "exists", return_value=True):
            with self.assertRaisesRegex(MODULE.WorkflowError, "final result file"):
                MODULE.bounded_review_pending(
                    process, Path("result.json"),
                    pipeline_run="a" * 32, session_id="session",
                )

    def test_pending_checkpoint_must_match_session_and_run(self):
        process = MODULE.subprocess.CompletedProcess(
            ["cloud_task"], 0, pending_task_stdout("a" * 32, "other"), ""
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "identity or schema"):
            MODULE.bounded_review_pending(
                process, Path("result.json"),
                pipeline_run="a" * 32, session_id="session",
            )

    def test_pending_checkpoint_requires_active_task(self):
        process = MODULE.subprocess.CompletedProcess(
            ["cloud_task"], 0,
            pending_task_stdout("a" * 32, "session", "completed"), "",
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "identity or schema"):
            MODULE.bounded_review_pending(
                process, Path("result.json"),
                pipeline_run="a" * 32, session_id="session",
            )
        queued = MODULE.subprocess.CompletedProcess(
            ["cloud_task"], 0,
            pending_task_stdout("a" * 32, "session", "queued"), "",
        )
        with mock.patch.object(MODULE.Path, "exists", return_value=False):
            self.assertTrue(MODULE.bounded_review_pending(
                queued, Path("result.json"),
                pipeline_run="a" * 32, session_id="session",
            ))

    def test_pending_checkpoint_uses_sealed_child_when_stdout_is_empty(self):
        process = MODULE.subprocess.CompletedProcess(["cloud_task"], 0, "", "")
        observation = json.loads(pending_task_stdout("a" * 32, "session"))
        terminal = {
            "exit_code": 0, "local_status": "finished",
            "workflow_result": observation,
        }
        execution = SimpleNamespace(children=[
            SimpleNamespace(terminal_result={"exit_code": 0}),
            SimpleNamespace(terminal_result=terminal),
        ])
        with (
            mock.patch.object(MODULE, "_EXECUTION", execution),
            mock.patch.object(MODULE.Path, "exists", return_value=False),
        ):
            self.assertTrue(MODULE.bounded_review_pending(
                process, Path("result.json"), pipeline_run="a" * 32,
                session_id="session", children_before=1,
            ))
            execution.children[1].terminal_result = {**terminal, "exit_code": 1}
            with self.assertRaisesRegex(MODULE.WorkflowError, "no sealed execution result"):
                MODULE.bounded_review_pending(
                    process, Path("result.json"), pipeline_run="a" * 32,
                    session_id="session", children_before=1,
                )


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def write(self, kind, path, value):
        if kind == "state":
            MODULE.save_state(path, {"version": MODULE.STATE_VERSION, "value": value})
        else:
            MODULE.atomic_write_text(path, value + "\n")

    def permission_error(self, winerror):
        error = PermissionError(13, "replacement denied")
        error.winerror = winerror
        return error

    def test_retry_preserves_one_closed_temporary_file_and_atomic_contents(self):
        replace = MODULE.os.replace
        for kind in ("state", "text"):
            for code in (5, 32):
                with self.subTest(kind=kind, code=code):
                    path = self.root / f"{kind}-{code}"
                    self.write(kind, path, "old")
                    before = path.read_bytes()
                    attempts = []

                    def fail_twice(source, destination):
                        self.assertEqual(before, path.read_bytes())
                        attempts.append((source, Path(source).read_bytes()))
                        if kind == "text":
                            fsync.assert_called_once()
                        if len(attempts) < 3:
                            raise self.permission_error(code)
                        replace(source, destination)

                    with (
                        mock.patch.object(MODULE, "IS_WINDOWS", True),
                        mock.patch.object(MODULE.os, "replace", side_effect=fail_twice),
                        mock.patch.object(MODULE.os, "fsync", wraps=MODULE.os.fsync) as fsync,
                        mock.patch.object(MODULE.time, "sleep") as sleep,
                    ):
                        self.write(kind, path, "new")
                    self.assertEqual([mock.call(0.01), mock.call(0.02)], sleep.call_args_list)
                    self.assertEqual([attempts[0]] * 3, attempts)
                    self.assertNotEqual(before, path.read_bytes())
                    self.assertEqual([], list(self.root.glob("*.tmp")))

    def test_retry_exhaustion_preserves_destination_and_original_error(self):
        for kind in ("state", "text"):
            for code in (5, 32):
                with self.subTest(kind=kind, code=code):
                    path = self.root / f"{kind}-{code}"
                    self.write(kind, path, "old")
                    before = path.read_bytes()
                    error = self.permission_error(code)
                    with (
                        mock.patch.object(MODULE, "IS_WINDOWS", True),
                        mock.patch.object(MODULE.os, "replace", side_effect=error) as replace,
                        mock.patch.object(MODULE.time, "sleep") as sleep,
                        self.assertRaises(PermissionError) as raised,
                    ):
                        self.write(kind, path, "new")
                    self.assertIs(error, raised.exception)
                    self.assertEqual(6, replace.call_count)
                    self.assertEqual(
                        [mock.call(delay) for delay in (0.01, 0.02, 0.05, 0.1, 0.2)],
                        sleep.call_args_list,
                    )
                    self.assertEqual(before, path.read_bytes())
                    self.assertEqual([], list(self.root.glob("*.tmp")))

    def test_other_errors_and_platforms_fail_without_retry(self):
        for kind in ("state", "text"):
            for windows, error in (
                (False, self.permission_error(5)),
                (False, self.permission_error(32)),
                (True, self.permission_error(33)),
                (True, PermissionError(13, "no Windows code")),
                (True, FileNotFoundError(2, "missing")),
                (True, OSError(28, "disk full")),
            ):
                with self.subTest(kind=kind, windows=windows, error=repr(error)):
                    path = self.root / kind
                    self.write(kind, path, "old")
                    before = path.read_bytes()
                    with (
                        mock.patch.object(MODULE, "IS_WINDOWS", windows),
                        mock.patch.object(MODULE.os, "replace", side_effect=error) as replace,
                        mock.patch.object(MODULE.time, "sleep") as sleep,
                        self.assertRaises(OSError) as raised,
                    ):
                        self.write(kind, path, "new")
                    self.assertIs(error, raised.exception)
                    replace.assert_called_once()
                    sleep.assert_not_called()
                    self.assertEqual(before, path.read_bytes())
                    self.assertEqual([], list(self.root.glob("*.tmp")))

    def test_fsync_failure_is_not_a_replacement_retry(self):
        path = self.root / "text"
        MODULE.atomic_write_text(path, "old")
        error = self.permission_error(5)
        with (
            mock.patch.object(MODULE, "IS_WINDOWS", True),
            mock.patch.object(MODULE.os, "fsync", side_effect=error),
            mock.patch.object(MODULE.os, "replace") as replace,
            mock.patch.object(MODULE.time, "sleep") as sleep,
            self.assertRaises(PermissionError) as raised,
        ):
            MODULE.atomic_write_text(path, "new")
        self.assertIs(error, raised.exception)
        replace.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual("old", path.read_text(encoding="utf-8"))
        self.assertEqual([], list(self.root.glob("*.tmp")))

    @unittest.skipUnless(MODULE.IS_WINDOWS, "requires Windows file sharing")
    def test_real_windows_status_reader_releases_destination_before_retry(self):
        for kind in ("state", "text"):
            with self.subTest(kind=kind):
                path = self.root / kind
                self.write(kind, path, "old")
                before = path.read_bytes()
                with path.open("r", encoding="utf-8") as reader:
                    def release_reader(_delay):
                        self.assertEqual(before, path.read_bytes())
                        reader.close()

                    with mock.patch.object(MODULE.time, "sleep", side_effect=release_reader) as sleep:
                        self.write(kind, path, "new")
                sleep.assert_called_once_with(0.01)
                self.assertNotEqual(before, path.read_bytes())
                self.assertEqual([], list(self.root.glob("*.tmp")))


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


def publish_args(path: Path, **overrides) -> SimpleNamespace:
    arguments = {
        "state": str(path),
        "validated": None,
        "not_validated": None,
        "rewrote": None,
    }
    arguments.update(overrides)
    return SimpleNamespace(**arguments)


def write_state(directory: Path, **overrides) -> Path:
    state = {
        "version": MODULE.STATE_VERSION,
        "created_at": "2026-01-01T00:00:00Z",
        "iterations": 0,
        "next_candidate_id": 1,
        "history": [],
        "repo_root": str(directory),
        "pr": {
            "number": 7,
            "title": "Add a thing",
            "pr_url": "https://github.com/owner/repo/pull/7",
            "repo_name": "owner/repo",
            "upstream_owner": "owner",
            "upstream_repo": "repo",
            "head_owner": "fork",
            "head_repo": "repo",
            "head_branch": "feature",
            "head_sha": "head1",
            "base_branch": "main",
            "base_sha": "base1",
        },
        "review": {
            "id": "pr-7-iteration-1",
            "status": "active",
            "iteration": 1,
            "head_sha": "head1",
            "diff_path": str(directory / "state.json.diff"),
            "anchors": {"app.py": {"LEFT": [2], "RIGHT": [2, 3]}},
            "candidates": [],
            "batches": [],
        },
    }
    state.update(overrides)
    path = directory / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


LOCAL_VALIDATION_HEADING = "### Local Validation Before A Push"
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


class LegacyAgentInstructions:
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

    def test_names_the_session_from_preflight_metadata_idempotently(self):
        self.assertIn(
            "tools: [read, edit, search, execute, agent, todo, rename_session]",
            self.instructions,
        )
        self.assertIn("## Session Naming", self.instructions)
        self.assertIn(
            "ensure the session name is `Self Review Loop: <PR number> - <PR title>`",
            self.instructions,
        )
        self.assertIn(
            "If the harness has already supplied a name beginning "
            "`Self Review Loop: <PR number> - `",
            self.instructions,
        )
        self.assertIn("do not call `rename_session`", self.instructions)
        self.assertIn(
            "Otherwise call `rename_session` once with the name you want when the "
            "runtime exposes that tool",
            self.instructions,
        )
        self.assertIn(
            "If the tool is unavailable, or it reports that it skipped the rename",
            self.instructions,
        )
        self.assertIn(
            "continue without retrying or reporting it as retrospective friction",
            self.instructions,
        )
        self.assertIn("Never use an interim number-only name", self.instructions)
        self.assertNotIn("call `rename_session` again", self.instructions)
        self.assertNotIn("immediately call `rename_session`", self.instructions)

    def test_bare_pr_reference_runs_the_full_loop(self):
        self.assertIn("name: Self Review Loop", self.instructions)
        self.assertIn(
            'description: "Explicit invocation only: never select automatically;',
            self.instructions,
        )
        self.assertIn(
            "## Activation: Bare PR References Run The Full Loop", self.instructions
        )
        self.assertIn(
            "asks you to run the full Self Review Loop", self.instructions
        )
        self.assertIn(
            "Never defer to the generic `github-pr-diff-review` skill for these "
            "inputs, and never call it or pass the work to it",
            self.instructions,
        )

    def test_never_posts_review_comments_and_allows_required_metadata_corrections(self):
        self.assertIn(
            "This agent never posts an inline comment, a review body, or a PR comment. "
            "Its normal change to GitHub is pushing commits to the PR head branch. The "
            "only exception is the narrow title or description correction that **PR "
            "Metadata Accuracy** requires.",
            self.instructions,
        )
        self.assertNotIn("pending review", self.instructions)
        self.assertNotIn("thread", self.instructions.lower())
        self.assertIn("## PR Metadata Accuracy", self.instructions)
        self.assertIn(
            "takes precedence over the normal push-only limit on changes",
            self.instructions,
        )
        self.assertIn(
            "applies at every terminal outcome", self.instructions
        )
        self.assertIn(
            "whether the inaccuracy predates this loop or a loop commit introduced it",
            self.instructions,
        )
        self.assertIn(
            "After each successful `publish`, and before the next `preflight`, read "
            "the live title and description again against the newly published diff",
            self.instructions,
        )
        self.assertIn(
            "Check once more before the terminal response", self.instructions
        )
        self.assertIn(
            "If you cannot make a required metadata correction safely, stop",
            self.instructions,
        )
        self.assertIn(
            "`**PR metadata:** corrected <title, description, or title and description>.`",
            self.instructions,
        )

    def test_allows_only_honest_changed_line_anchors_for_unchanged_code(self):
        self.assertIn(
            "A changed line may anchor a defect whose complete cause or fix also "
            "involves unchanged code",
            self.instructions,
        )
        self.assertIn(
            "only when that changed line genuinely demonstrates the defect or "
            "incomplete fix",
            self.instructions,
        )
        self.assertIn(
            "Never choose an unrelated changed line merely because the helper accepts it",
            self.instructions,
        )

    def test_restarts_after_resolve_observes_a_moved_head(self):
        self.assertIn(
            "Whenever either clean-exit `resolve` call reports `head_moved`, discard "
            "the old snapshot and start again with a fresh `preflight`",
            self.instructions,
        )
        self.assertIn(
            "The helper supersedes that stale review so its dropped candidates do not "
            "enter history",
            self.instructions,
        )
        self.assertIn(
            "No publication occurred, so this does not consume an iteration",
            self.instructions,
        )

    def test_records_both_clean_exits(self):
        self.assertEqual(
            self.instructions.count("run `resolve --state <path> --outcome clean`"),
            2,
        )

    def test_requires_the_exact_primary_route_and_independent_evaluator(self):
        self.assertIn("## Model Gate", self.instructions)
        self.assertIn("model is exactly `gpt-6-sol`", self.instructions)
        self.assertIn(
            "When the runtime exposes the primary session's reasoning effort, "
            "require it to be exactly `high`",
            self.instructions,
        )
        self.assertIn(
            "an unavailable effort does not fail the gate",
            self.instructions,
        )
        self.assertIn(
            "If you cannot work out the model, the gate has failed",
            self.instructions,
        )
        self.assertIn("The user cannot override this gate", self.instructions)
        self.assertIn(
            "using agent type **general-purpose**, model `claude-sonnet-5`, and "
            "reasoning effort `high`",
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
        self.assertIn(
            "for **each candidate separately**",
            self.instructions,
        )
        self.assertNotIn("claude-opus-5", self.instructions.lower())
        self.assertNotIn("reasoning effort **max**", self.instructions)

    def test_defines_the_bar_each_evaluator_judges_against(self):
        self.assertIn("## Evaluation Standard", self.instructions)
        self.assertIn(
            "Give that evaluator the PR's stated scope, the relevant diff and "
            "context, the **Evaluation Standard**, and exactly one candidate",
            self.instructions,
        )
        self.assertIn(
            "Require two independent decisions, each judged against that "
            "standard and supported by evidence",
            self.instructions,
        )
        self.assertIn(
            "Would a reasonable author apply this fix or knowingly decline it, "
            "as part of what this PR already does?",
            self.instructions,
        )
        self.assertIn(
            "each evaluator judges against a fixed bar instead of its own taste",
            self.instructions,
        )
        self.assertIn(
            "Decision 1 asks whether this PR demonstrates the candidate as fact. "
            "Nothing here relaxes it.",
            self.instructions,
        )
        self.assertIn(
            "needs no user-visible impact, needs no runtime defect behind it, "
            "and needs no large fix",
            self.instructions,
        )
        self.assertIn("- dead code this PR creates.", self.instructions)
        self.assertIn(
            "a departure from the reviewed repository's own instructions, when "
            "the evaluator can name the instruction",
            self.instructions,
        )
        self.assertIn(
            "documentation, naming, or a test that this PR makes wrong or "
            "misleading",
            self.instructions,
        )
        self.assertIn(
            "A preference with no repository instruction behind it does not "
            "clear it",
            self.instructions,
        )

    def test_rejects_unprovable_doubt_and_worth_uncertainty_as_drop_reasons(self):
        self.assertIn("Both decisions need demonstrated doubt", self.instructions)
        self.assertIn(
            "never drops one because a caller, a use, or a reason might exist "
            "somewhere unseen",
            self.instructions,
        )
        self.assertIn(
            '"It cannot be ruled out" states that evidence is missing, so it '
            "decides nothing",
            self.instructions,
        )
        self.assertIn(
            "Each verdict names the decision it failed and the evidence behind "
            "that decision",
            self.instructions,
        )
        self.assertIn(
            "Run `drop` for any candidate where decision 1 fails or stays "
            "uncertain, or where decision 2 fails on evidence the evaluator "
            "named",
            self.instructions,
        )
        self.assertIn(
            "record the decision it failed together with the evaluator's "
            "concrete reason",
            self.instructions,
        )
        self.assertIn(
            "Uncertainty about decision 2 on its own never drops a candidate",
            self.instructions,
        )
        self.assertIn("--rationale-file", self.instructions)

    def test_narrows_the_silence_rule_to_preferences_without_an_instruction(self):
        self.assertIn(
            "a preference with no repository instruction behind it, or an issue "
            "that already existed",
            self.instructions,
        )
        self.assertNotIn("a triviality, a style preference", self.instructions)

    def test_runs_candidate_evaluations_in_parallel(self):
        self.assertIn("## Parallel Evaluation", self.instructions)
        self.assertIn(
            "Run those evaluations concurrently under **Parallel Evaluation**",
            self.instructions,
        )
        self.assertIn(
            "Launch every evaluator as a synchronous task call, and issue the calls "
            "together in one tool-call response",
            self.instructions,
        )
        self.assertIn(
            "Each synchronous call returns its completed verdict",
            self.instructions,
        )
        self.assertIn("Never create a persistent background evaluator handle", self.instructions)
        self.assertNotIn("`mode: background`", self.instructions)
        self.assertNotIn("`read_agent`", self.instructions)
        self.assertIn(
            "Running evaluators at the same time never relaxes the isolation rule", self.instructions
        )
        self.assertIn("Evaluators only read.", self.instructions)
        self.assertIn(
            "write any artifact outside the repository under its own unique "
            "temporary location",
            self.instructions,
        )
        self.assertIn(
            "Consume the collected verdicts in candidate ID order whatever order they "
            "finish in",
            self.instructions,
        )
        self.assertIn(
            "Run an evaluator again, alone and for its own candidate, when it fails, "
            "times out, or returns a verdict you cannot use",
            self.instructions,
        )
        self.assertIn(
            "never let a missing verdict decide by default to keep or drop the "
            "candidate",
            self.instructions,
        )
        self.assertIn(
            "Only this evaluation phase of a single iteration runs in parallel",
            self.instructions,
        )

    def test_allows_focused_runtime_evidence_without_duplicating_ci(self):
        self.assertIn(
            "Skip a blanket run of the test suite, and any other check whose only "
            "purpose is to repeat CI during review",
            self.instructions,
        )
        self.assertIn(
            "CI runs the suite before this loop edits anything, so running it again "
            "here settles nothing",
            self.instructions,
        )
        self.assertIn(
            "Everything else about tests belongs to this review: read the test code "
            "the pull request changes, investigate a test when it bears on a "
            "candidate, and run a targeted test when that is how you answer a "
            "question about the change",
            self.instructions,
        )
        self.assertIn(
            "This does not forbid running something locally as evidence",
            self.instructions,
        )
        self.assertIn(
            "run the smallest throwaway probe that establishes the relevant "
            "repository, shared-helper, dependency, or third-party runtime behavior",
            self.instructions,
        )
        self.assertIn(
            "Reuse the dependencies and caches you already have", self.instructions
        )
        self.assertIn(
            "keep a probe's own generated files outside the repository, delete "
            "them afterward",
            self.instructions,
        )
        self.assertIn(
            "do not widen the probe into general validation", self.instructions
        )

    def test_reviews_for_suppressed_test_coverage(self):
        self.assertIn(
            "Treat suppressed coverage as a defect only a reviewer catches",
            self.instructions,
        )
        self.assertIn(
            "A deleted assertion, an added skip or disable annotation, a loosened "
            "matcher or widened tolerance, and an exception swallowed inside a test "
            "each turn a check green by asking less of the code",
            self.instructions,
        )
        self.assertIn(
            "register a candidate that says exactly what is no longer checked",
            self.instructions,
        )
        self.assertIn(
            "Judge the edit on that, not on its size or on the rationale attached "
            "to it",
            self.instructions,
        )

    def test_commit_body_uses_the_review_finding_label(self):
        self.assertIn("## Commit Content", self.instructions)
        self.assertIn("Address review finding: <short summary>", self.instructions)
        self.assertIn("Review finding:\n", self.instructions)
        self.assertIn("Analysis: <technical analysis and rationale>", self.instructions)
        self.assertIn("Upsides: <concrete benefits>", self.instructions)
        self.assertIn("No material downside identified", self.instructions)
        self.assertNotIn("Copilot comment:", self.instructions)

    def test_documents_file_based_commit_message_authoring(self):
        self.assertIn(
            "Write the whole commit message to a temporary UTF-8 file outside the "
            "repository and commit it with `git commit -F <path>`",
            self.instructions,
        )
        self.assertIn(
            "Never build the message with `git commit -m`, and never use a shell "
            "escape sequence",
            self.instructions,
        )
        self.assertIn(
            "read the message back with `git log -1 --pretty=%B`", self.instructions
        )

    def test_documents_the_capped_autonomous_loop(self):
        self.assertIn(
            "The loop is `preflight -> review -> evaluate -> batch -> commit -> publish`",
            self.instructions,
        )
        self.assertIn("The maximum is 5 iterations,", self.instructions)
        self.assertIn("`max_iterations_reached`", self.instructions)
        self.assertIn("with `--new-invocation`", self.instructions)
        self.assertIn("`--invocation-run <token>`", self.instructions)
        self.assertIn("`nothing_to_publish`", self.instructions)
        self.assertIn(
            "A missing history commit is not enough to raise the finding again",
            self.instructions,
        )
        self.assertIn(
            "Raise it again only when the pinned diff and current code show that the "
            "fix was removed",
            self.instructions,
        )
        self.assertIn(
            "run `preflight --repo-root <workspace>` with no target", self.instructions
        )


    def test_documents_shell_safe_drop_and_evaluator_improved_fixes(self):
        self.assertIn(
            "--rationale-file <file-or->", self.instructions
        )
        self.assertIn(
            "prefer a temporary UTF-8 `--rationale-file` for text a model wrote",
            self.instructions,
        )
        self.assertIn(
            "The registered anchor identifies the defect, not the largest edit you may "
            "make",
            self.instructions,
        )
        self.assertIn(
            "including lines the PR already changed", self.instructions
        )
        self.assertIn(
            "Do not absorb a separate defect just because the evaluator noticed it",
            self.instructions,
        )
        self.assertIn(
            "widen the planned paths before you edit", self.instructions
        )

    def test_routes_plausible_unresolved_candidates_to_the_evaluator(self):
        self.assertIn(
            "\"Prefer silence\" sets the bar for a final finding, not for reaching the "
            "evaluator",
            self.instructions,
        )
        self.assertIn(
            "register a candidate when the PR demonstrates it concretely and "
            "the **Evaluation Standard** admits it",
            self.instructions,
        )
        self.assertIn(
            "you still cannot settle whether it is factual or worth acting on",
            self.instructions,
        )
        self.assertIn(
            "Drop a lead yourself, before you register it, only when direct evidence "
            "already disproves it",
            self.instructions,
        )
        self.assertIn(
            "Do not drop it yourself just because it may turn out to change nothing",
            self.instructions,
        )

    def test_final_response_renders_commit_dropped_candidate_and_pr_links(self):
        self.assertIn(
            "canonical pull request link from the most recent preflight result's "
            "`pr.pr_url`",
            self.instructions,
        )
        self.assertIn(
            "Render ordinary Markdown, never a fenced code block", self.instructions
        )
        self.assertIn(
            "[<short-sha> <short batch summary>](<pr.pr_url>/changes/<full-sha>)",
            self.instructions,
        )
        self.assertNotIn("/commits/<full-sha>", self.instructions)
        self.assertIn(
            "**PR:** [#<pr.number> <pr.title>](<pr.pr_url>)", self.instructions
        )
        self.assertNotIn("PR: <pr.pr_url>", self.instructions)
        self.assertIn(
            "For a clean pass with no commits and no no-code outcomes",
            self.instructions,
        )
        self.assertIn(
            "With no dropped candidates, render exactly the `**Outcome:**` line "
            "followed by the `**PR:**` line",
            self.instructions,
        )
        self.assertIn(
            "after `**Outcome:**` so the main result stays first, and immediately "
            "before `**PR:**`",
            self.instructions,
        )
        self.assertIn("`**Dropped candidates:**`", self.instructions)
        self.assertIn(
            "List every dropped candidate separately with its original problem and "
            "the evaluator's concrete reason; do not collapse them into a count",
            self.instructions,
        )
        self.assertIn(
            "Report every candidate this run evaluated and dropped in any of its "
            "iterations",
            self.instructions,
        )
        self.assertIn(
            "A drop from an earlier iteration still belongs in the block after "
            "`preflight` folds it into `history`",
            self.instructions,
        )
        self.assertIn(
            "Leave out only an entry `history` carried in from a previous run",
            self.instructions,
        )
        self.assertNotIn(
            "not dropped entries carried forward in `history`", self.instructions
        )
        self.assertNotIn(
            "Report dropped candidates only as a count", self.instructions
        )
        self.assertIn(
            "Do not invent a commit, a no-code line, or a narrative line", self.instructions
        )

    def test_reads_the_preflight_result_from_the_helper_file(self):
        self.assertIn("write its complete result to `preflight_path`", self.instructions)
        self.assertIn(
            "print only a compact envelope carrying `result`, `state`, "
            "`preflight_path`",
            self.instructions,
        )
        self.assertIn(
            "Read the full `pr`, `changed_files`, `pr_commits`, `pr_authored_files`, "
            "`history`, `history_commit_presence`, and `repository_context` from the "
            "complete result at `preflight_path`",
            self.instructions,
        )
        self.assertIn(
            "check what you read against the envelope's `counts`",
            self.instructions,
        )
        self.assertIn(
            "The envelope keeps `counts.history_commits_missing` for exact-SHA "
            "reporting",
            self.instructions,
        )
        self.assertIn(
            "Do not compare commit lists or reconstruct patch identity by hand",
            self.instructions,
        )

    def test_defines_the_state_machine_and_repository_context_map(self):
        self.assertIn("## Workflow State Machine", self.instructions)
        self.assertIn(
            "The helper's current result decides the transition", self.instructions
        )
        self.assertIn(
            "Apply precedence in this order: the helper's head and state guards",
            self.instructions,
        )
        self.assertIn("## Repository Context And Validation Map", self.instructions)
        self.assertIn(
            "It is a dependency-free discovery index over tracked files",
            self.instructions,
        )
        for field in (
            "exact command",
            "working directory",
            "prerequisites",
            "files the check reads",
            "expected cost",
        ):
            self.assertIn(field, self.instructions)
        self.assertIn(
            "The discovered files are a starting point, not an outer bound",
            self.instructions,
        )

    def test_uses_patch_identity_without_treating_unknown_as_retained(self):
        self.assertIn(
            "an equivalent whitespace-preserving patch", self.instructions
        )
        self.assertIn(
            "never treats commit-list presence alone as final-tree retention",
            self.instructions,
        )
        self.assertIn(
            "When `retained` is null, inspect the pinned diff and current code",
            self.instructions,
        )
        self.assertIn(
            "Review the finding again only when `retained` is false, or inspection "
            "proves the fix is gone, and no intentional rejection settles it",
            self.instructions,
        )

    def test_reads_the_status_result_from_the_helper_file(self):
        self.assertIn(
            "write the complete state snapshot to `status_path` as JSON",
            self.instructions,
        )
        self.assertIn(
            "open the complete result at `status_path` only when you need",
            self.instructions,
        )

    def test_reads_the_pinned_diff_from_the_helper_snapshot(self):
        self.assertIn(
            "Read the pinned diff only from the returned `diff_path`",
            self.instructions,
        )
        self.assertIn("Never run `gh pr diff` again", self.instructions)
        self.assertIn(
            "Review the whole pinned diff read from `diff_path`", self.instructions
        )
        self.assertIn(
            "Read the whole pinned diff on the first iteration, and whenever the head "
            "holds any change this run did not publish",
            self.instructions,
        )
        self.assertIn(
            "the new preflight head equals the head the preceding `publish` returned",
            self.instructions,
        )
        self.assertIn(
            "the only new commits were this loop's recorded commits",
            self.instructions,
        )
        self.assertIn(
            "carry the earlier full review forward and review only those newly "
            "published commits in their current pinned-diff context",
            self.instructions,
        )
        self.assertIn(
            "you do not need to read unchanged hunks again", self.instructions
        )
        self.assertIn(
            "the earlier review plus the exact proven delta covers every line of the "
            "current pin",
            self.instructions,
        )
        self.assertIn(
            "Before you keep a candidate that claims a semantic or convention violation",
            self.instructions,
        )
        self.assertIn(
            "read the implementation or the authoritative documentation of any shared "
            "helper that defines that contract",
            self.instructions,
        )
        self.assertIn(
            "Do not send an assumption to the evaluator when one direct read of that "
            "helper can disprove it",
            self.instructions,
        )
        self.assertIn(
            "refuse to publish a skipped batch, require the commits sitting on the "
            "pinned head to be exactly the recorded ones",
            self.instructions,
        )
        self.assertIn(
            "GitHub's ordered `pr_commits` with each commit's touched `files`",
            self.instructions,
        )
        self.assertIn(
            "Use `pr_commits`, `pr_authored_files`, and `diff_only_files` to work out "
            "scope when the PR base has drifted",
            self.instructions,
        )
        self.assertIn(
            "treat it as context from base drift rather than as work the PR authored",
            self.instructions,
        )
        self.assertIn(
            "knowing where a change came from narrows who owns it, not what the "
            "authoritative changeset is",
            self.instructions,
        )
        self.assertIn(
            "Do not compare against `origin/main` by hand, do not work out another "
            "merge-base range, and do not replace the helper's provenance with `git "
            "log` or `git show`",
            self.instructions,
        )

    def test_isolates_validation_failures_owned_by_another_pending_batch(self):
        self.assertIn(
            "When the evidence shows that a different candidate, still pending in "
            "another batch, is the only cause",
            self.instructions,
        )
        self.assertIn(
            "focused validation that isolates the current batch", self.instructions
        )
        self.assertIn(
            "if that batch's own relevant checks pass, record it as normal",
            self.instructions,
        )
        self.assertIn(
            "keep the other failure, and handle that candidate in its own batch",
            self.instructions,
        )
        self.assertIn(
            "Never use this exception for a failure you cannot explain, for a shared "
            "root cause, or for a failure the current batch introduced",
            self.instructions,
        )

    def test_closes_every_run_with_a_categorized_retrospective(self):
        self.assertIn(
            "## Self Review Loop Agent Retrospective", self.instructions
        )
        self.assertIn(
            "**Self Review Loop Agent Retrospective**", self.instructions
        )
        self.assertIn(
            "Silence is the normal outcome, and a run that went smoothly reports "
            "nothing",
            self.instructions,
        )
        self.assertIn(
            "Produce the retrospective on every terminal outcome, including a clean "
            "pass, a validation stop you could not fix, `max_iterations_reached`, "
            "`nothing_to_publish`, a helper error, and a failed **Model Gate**",
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
        self.assertIn(
            "The **Self Review Loop Agent Retrospective** is the only content allowed "
            "after the `**PR:**` line",
            self.instructions,
        )
        self.assertIn("The retrospective is advice, and it belongs in chat only", self.instructions)
        self.assertIn(
            "never commit it or push it as part of this loop", self.instructions
        )
        self.assertIn(
            "leave the label out entirely when there is nothing to report", self.instructions
        )
        self.assertIn("Emit exactly one terminal response", self.instructions)
        self.assertIn("must be the very last block", self.instructions)
        self.assertIn("stop immediately after its last list item", self.instructions)
        self.assertIn(
            "never emit a short final response and then a fuller report",
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
        self.assertIn(
            "including the final `resolve` or `publish`, the PR metadata recheck, and "
            "the deletion of any temporary file, before you compose this response",
            self.instructions,
        )
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
            "render the `**Outcome:**`, `**Dropped candidates:**`, and `**PR:**` "
            "lines at most once each",
            self.instructions,
        )

    def test_names_no_build_tool_or_programming_language(self):
        """Each stage runs under the configuration its own repository supplies.

        This list exists to fail on the one wrong fix that is tempting here:
        pasting a concrete build command into the file so the agent does not
        have to work one out. Every name is matched on a word boundary,
        because a bare substring on a short token eventually fires on an
        innocent word and gets deleted by whoever trips over it, and the guard
        is then gone.
        """
        forbidden = [
            "bazel",
            "cargo",
            "dotnet",
            "golang",
            "gradle",
            "gradlew",
            "java",
            "javac",
            "jest",
            "junit",
            "kotlin",
            "maven",
            "mvn",
            "npm",
            "pnpm",
            "pytest",
            "rustc",
            "tsc",
            "typescript",
            "yarn",
        ]
        found = sorted(
            name
            for name in forbidden
            if re.search(rf"\b{name}\b", self.instructions, re.IGNORECASE)
        )
        self.assertEqual([], found)

    def test_the_local_validation_fallback_publishes_instead_of_stopping(self):
        """A repository with no usable narrow command must not become a stop.

        Halting there would create a second class of false escalation on
        exactly the repositories where local validation buys nothing, so every
        paragraph that reaches for the skip flag has to push, and none of them
        may reach for escalation vocabulary.
        """
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        paragraphs = [
            paragraph
            for paragraph in section.split("\n\n")
            if "--not-validated" in paragraph
        ]
        self.assertTrue(paragraphs)
        for paragraph in paragraphs:
            with self.subTest(paragraph=paragraph):
                self.assertIn("publish", paragraph)
                self.assertNotIn("escalat", paragraph.lower())

    def test_every_validation_flag_the_section_names_reaches_publish(self):
        """Prose naming a flag the helper rejects would stop a push outright."""
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        named = sorted(set(re.findall(r"--[a-z][a-z-]+", section)))
        self.assertTrue(named)
        parser = MODULE.build_parser()
        for flag in named:
            with self.subTest(flag=flag):
                args = parser.parse_args(
                    ["publish", "--state", "state.json", flag, "value"]
                )
                self.assertEqual("publish", args.command)

    def test_publish_documents_every_validation_flag_it_accepts(self):
        """A flag the helper grows and the file never mentions goes unused."""
        parser = MODULE.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        accepted = sorted(
            option
            for action in subparsers.choices["publish"]._actions
            for option in action.option_strings
            if "valid" in option or "rewrote" in option
        )
        self.assertTrue(accepted)
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        for flag in accepted:
            with self.subTest(flag=flag):
                self.assertIn(flag, section)

    def test_local_validation_is_wired_into_the_step_that_pushes(self):
        """The requirement is only real where the run reaches the push."""
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        elsewhere = self.instructions.replace(section, "")
        self.assertIn(f"**{LOCAL_VALIDATION_HEADING.lstrip('# ')}**", elsewhere)

    def test_covering_checks_are_not_narrowed_to_compilation(self):
        """The failure this requirement was written for compiled cleanly.

        It was a documentation comment that a separate documentation task
        rejected, so wording that let covering mean "it builds" would sail
        past the very cycle this is meant to save.
        """
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        for word in ["documentation", "lint", "format"]:
            with self.subTest(word=word):
                self.assertIn(word, section)

    def test_requires_committing_what_a_fixing_command_rewrote(self):
        """A rewrite left in the worktree fails silently.

        The push carries the earlier commit, the same check fails on the pull
        request anyway, and the next reset discards the rewritten files.
        """
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        self.assertIn("fixing form", section)
        rewrite_paragraphs = [
            paragraph
            for paragraph in section.split("\n\n")
            if re.search(r"rewr\w+", paragraph, re.IGNORECASE)
            and "commit" in paragraph.lower()
        ]
        self.assertTrue(rewrite_paragraphs)

    def test_local_success_does_not_stand_in_for_the_checks(self):
        self.assertIn(
            "The pull request's checks remain the only thing that says a "
            "change is sound",
            self.instructions,
        )

    def test_routes_a_no_target_request_around_a_detached_worktree(self):
        """The pipeline leaves the worktree detached, so no branch resolves.

        A reader who copies the bare no-target form under a pipeline reaches a
        resolver that refuses on purpose, so both steps have to name what to
        pass instead of leaving the refusal as the answer.
        """
        self.assertIn(
            "`--current` finds that state through the branch that is checked "
            "out, and a detached worktree has no branch to look up, so pass "
            "`--state <path>` there instead.",
            self.instructions,
        )
        self.assertIn(
            "the pipeline leaves each stage's worktree detached at the PR head, "
            "so a request that reaches this loop from a pipeline must name the "
            "PR as a URL or `owner/repo#number`",
            self.instructions,
        )
        self.assertIn(
            "Leaving the target out is the attached case, not the shape to copy.",
            self.instructions,
        )

    def test_the_current_rule_admits_a_detached_worktree_has_no_pull_request(self):
        """The rule is still right about never guessing from a state file.

        It was only wrong to imply a checked-out branch is always there to ask.
        """
        self.assertIn(
            "`current` always means the PR attached to the branch that is "
            "checked out, and a detached worktree has no such PR",
            self.instructions,
        )

    def test_the_argument_hint_stops_selling_the_bare_form_as_the_default(self):
        """The hint is the shape a caller copies before reaching any step list.

        It used to promise the current branch's PR, which a detached worktree
        cannot supply, so the omission read as the ordinary way to call this.
        """
        self.assertIn(
            'argument-hint: "PR URL, PR number, or owner/repo#number; omit only '
            "from a worktree attached to the PR's branch\"",
            self.instructions,
        )
        self.assertNotIn("omit to use the current branch's PR", self.instructions)


class AgentTaskCoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.repo_root = self.directory / "repo"
        self.repo_root.mkdir()
        self.head = "1" * 40
        self.base = "2" * 40
        self.artifact = "3" * 40
        self.validation = [
            {
                "command": "python -m unittest tests.test_feature",
                "outcome": "passed",
            }
        ]
        self.preflight = {
            "repository_root": str(self.repo_root),
            "identity": {
                "branch": "feature",
                "head": self.head,
                "status": "",
            },
            "pr": {
                "owner": "owner",
                "repo": "repo",
                "number": 7,
                "repo_name": "owner/repo",
                "pr_url": "https://github.com/owner/repo/pull/7",
                "title": "Current title",
                "body": "Current body",
                "state": "OPEN",
                "is_draft": False,
                "upstream_owner": "owner",
                "upstream_repo": "repo",
                "head_owner": "owner",
                "head_repo": "repo",
                "head_repository": "owner/repo",
                "head_branch": "feature",
                "head_sha": self.head,
                "base_branch": "main",
                "base_sha": self.base,
                "commits": [],
                "cross_repository": False,
            },
            "viewer": {
                "login": "viewer",
                "repository_role": "write",
                "permissions": {
                    "admin": False,
                    "maintain": False,
                    "push": True,
                    "triage": True,
                    "pull": True,
                },
            },
        }

    def result(self, *, commits=None):
        commits = [] if commits is None else commits
        request_id = "request-1"
        return {
            "schema": MODULE.STRUCTURAL_AGENT_TASK_RESULT_SCHEMA,
            "status": "success",
            "mode": "apply_with_report",
            "repository": {"name_with_owner": "owner/repo"},
            "pull_request": MODULE.expected_cloud_pull_request(self.preflight),
            "requested_model": "gpt-6-sol",
            "policy": {
                "id": "marketplace-agent-apply-report-worker",
                "version": 3,
                "sha256": MODULE.LEGACY_STRUCTURAL_AGENT_TASK_POLICY_V3["sha256"],
            },
            "task": {
                "id": "task-1",
                "url": "https://github.com/owner/repo/agent-tasks/task-1",
                "state": "completed",
                "base_ref": "feature",
                "base_sha": self.head,
            },
            "generated": {
                "branch": "copilot/agent-task",
                "head_sha": self.artifact,
                "commits": commits,
            },
            "application": {
                "status": "not_applied",
                "final_local_head": self.head,
            },
            "report": {
                "path": f".github/agent-task-reports/{request_id}.md",
                "commit": self.artifact,
                "sha256": "4" * 64,
            },
            "attestation": {
                "kind": "dispatcher_structural",
                "structural_complete": True,
            },
            "error": None,
        }

    def semantic_result(self, *, commits=None, payload):
        result = self.result(commits=commits)
        result.update(
            {
                "schema": MODULE.AGENT_TASK_RESULT_SCHEMA,
                "policy": {
                    "id": "marketplace-agent-apply-report-worker",
                    "version": 5,
                    "sha256": MODULE.LEGACY_SEMANTIC_AGENT_TASK_POLICY_V5[
                        "sha256"
                    ],
                },
                "report": None,
                "semantic_output": {
                    "schema": {
                        "id": "github.copilot.agent-task-semantic-output",
                        "version": 2,
                    },
                    "kind": "self-review-loop",
                    "path": ".github/agent-task-semantic/request-1.json",
                    "commit": self.artifact,
                    "sha256": "5" * 64,
                    "payload": payload,
                },
                "attestation": {
                    "kind": "dispatcher_semantic",
                    "structural_complete": True,
                },
            }
        )
        return result

    def candidate_metadata(self, sha, parent, paths):
        return {
            "sha": sha,
            "parent_sha": parent,
            "tree_sha": "a" * 40,
            "patch_sha256": "b" * 64,
            "changed_paths": paths,
        }

    def candidate_result(self, *, commits=None, changed_paths=None):
        commits = [] if commits is None else commits
        paths = changed_paths or ["src/app.py"]
        parent = self.head
        commit_metadata = []
        for commit in commits:
            commit_metadata.append(
                self.candidate_metadata(commit, parent, paths)
            )
            parent = commit
        return {
            "schema": MODULE.CANDIDATE_AGENT_TASK_RESULT_SCHEMA,
            "status": "success",
            "mode": "code_candidate",
            "repository": {"name_with_owner": "owner/repo"},
            "pull_request": MODULE.expected_cloud_pull_request(self.preflight),
            "requested_model": "gpt-6-sol",
            "policy": {
                "id": "marketplace-agent-code-candidate-worker",
                "version": 1,
                "sha256": MODULE.AGENT_TASK_POLICY_SHA256,
            },
            "task": {
                "id": "task-1",
                "url": "https://github.com/owner/repo/agent-tasks/task-1",
                "state": "completed",
                "base_ref": "feature",
                "base_sha": self.head,
            },
            "generated": {
                "branch": "copilot/candidate",
                "head_sha": parent,
                "commits": commits,
            },
            "application": {
                "status": "not_applied",
                "final_local_head": self.head,
            },
            "report": None,
            "candidate": {
                "schema": MODULE.AGENT_TASK_CANDIDATE_MANIFEST_SCHEMA,
                "repository": {"name_with_owner": "owner/repo"},
                "task": {"id": "task-1", "session_id": "session-1"},
                "base": {"ref": "feature", "sha": self.head},
                "generated": {
                    "ref": "copilot/candidate",
                    "head_sha": parent,
                    "code_tip_sha": parent,
                },
                "code_commits": commit_metadata,
                "artifact_commit": None,
            },
            "completion": {
                "request": {
                    "requested_model": "gpt-6-sol",
                    "prompt_sha256": "c" * 64,
                },
                "task": {
                    "id": "task-1",
                    "state": "completed",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:01:00Z",
                    "completed_at": "2026-01-01T00:01:00Z",
                    "raw_response_sha256": "d" * 64,
                },
                "session": {
                    "id": "session-1",
                    "state": "completed",
                    "actual_model": "gpt-6-sol",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:01:00Z",
                    "completed_at": "2026-01-01T00:01:00Z",
                    "prompt_sha256": "c" * 64,
                },
                "repository": {
                    "name_with_owner": "owner/repo",
                    "id": 1,
                    "owner": {"login": "owner", "id": 2},
                },
                "refs": {
                    "base": "feature",
                    "generated": "copilot/candidate",
                },
            },
            "attestation": {
                "kind": "dispatcher_candidate",
                "structural_complete": True,
            },
            "error": None,
        }

    def verified_candidate(self, result, **_kwargs):
        if result["task"]["state"] != "completed":
            raise MODULE.WorkflowError("Self Review candidate rejected")
        candidate = result["candidate"]
        artifact = candidate["artifact_commit"]
        return {
            "contract": "candidate",
            "task_id": result["task"]["id"],
            "task_url": result["task"]["url"],
            "session_id": result["completion"]["session"]["id"],
            "generated_branch": result["generated"]["branch"],
            "generated_head": result["generated"]["head_sha"],
            "code_tip": candidate["generated"]["code_tip_sha"],
            "commits": [item["sha"] for item in candidate["code_commits"]],
            "final_local_head": candidate["generated"]["code_tip_sha"],
            "requires_apply": True,
            "candidate_manifest": candidate,
            "completion": result["completion"],
            "report_evidence": (
                {
                    "path": MODULE.AGENT_TASK_OUTPUT_RESULT,
                    "commit": artifact["sha"],
                    "patch_sha256": artifact["patch_sha256"],
                }
                if artifact is not None
                and MODULE.AGENT_TASK_OUTPUT_RESULT in artifact["changed_paths"]
                else None
            ),
            "structural_attestation": True,
        }

    def candidate_creation_failure(self):
        failure = self.candidate_result()
        failure.update(
            {
                "status": "error",
                "task": {
                    "id": None,
                    "url": None,
                    "state": None,
                    "base_ref": None,
                    "base_sha": None,
                },
                "generated": {"branch": None, "head_sha": None, "commits": []},
                "candidate": None,
                "completion": None,
                "attestation": {
                    "kind": "dispatcher_candidate",
                    "structural_complete": False,
                },
                "error": {
                    "code": "api_failure",
                    "message": "start Agent Task failed",
                },
            }
        )
        return failure

    def legacy_malformed_owner_state(self, state_path):
        request_id = "legacy-request-1"
        generated_head = "3" * 40
        direct_base = self.preflight["pr"]["base_sha"]
        prompt_path = self.directory / "legacy-prompt.txt"
        result_path = self.directory / "legacy-result.json"
        prompt_path.write_text(
            "\n".join(
                [
                    "Self Review Loop Agent Tasks worker prompt version 2.",
                    self.head,
                    self.preflight["pr"]["repo_name"],
                    self.preflight["pr"]["pr_url"],
                    "{{MARKETPLACE_REPORT_PATH}}",
                    "{{MARKETPLACE_VALIDATION_PATH}}",
                ]
            ),
            encoding="utf-8",
        )
        pull_request = MODULE.expected_cloud_pull_request(self.preflight)
        pull_request["base_sha"] = direct_base
        result = {
            "schema": MODULE.LEGACY_AGENT_TASK_RESULT_SCHEMA,
            "status": "error",
            "mode": "apply_with_report",
            "repository": {"name_with_owner": "owner/repo"},
            "pull_request": pull_request,
            "requested_model": "gpt-5.6-sol",
            "policy": MODULE.LEGACY_AGENT_TASK_POLICY_V4,
            "task": {
                "id": "legacy-task-1",
                "url": "https://github.com/owner/repo/tasks/legacy-task-1",
                "state": "completed",
                "base_ref": "feature",
                "base_sha": self.head,
            },
            "generated": {
                "branch": "copilot/legacy-owner",
                "head_sha": generated_head,
                "commits": [],
            },
            "application": {
                "status": "not_applied",
                "final_local_head": self.head,
            },
            "report": {
                "path": f".github/agent-task-reports/{request_id}.md",
                "commit": generated_head,
                "sha256": None,
            },
            "worker_receipt": {
                "path": f".github/agent-task-validations/{request_id}.json",
                "commit": generated_head,
                "sha256": None,
            },
            "validation": {"complete": False, "outcomes": []},
            "error": {
                "code": "validation_incomplete",
                "message": "marketplace worker validation outcome is malformed",
            },
        }
        result_path.write_text(
            json.dumps(result, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        owner = "legacy-owner"
        state = {
            "version": 1,
            "created_at": "2026-09-17T00:00:00Z",
            "iterations": 0,
            "next_candidate_id": 1,
            "history": [],
            "managed_task_history": [],
            "managed_review_history": [],
            "repo_root": str(self.repo_root),
            "pr": self.preflight["pr"],
            "review": {
                "id": f"pr-7-agent-task-{owner}",
                "status": "active",
                "iteration": 1,
                "head_sha": self.head,
                "candidates": [],
                "batches": [],
            },
            "agent_task": {
                "status": "failed",
                "run_id": owner,
                "model": "gpt-5.6-sol",
                "policy": "marketplace-agent-worker@4",
                "allowed_iterations": 5,
                "preflight": self.preflight,
                "prompt_file": str(prompt_path),
                "result_file": str(result_path),
                "error": "Agent Task result has an unsupported schema or fields",
            },
        }
        MODULE.save_state(state_path, state)
        return state, result, prompt_path, result_path

    def test_candidate_taskless_failure_keeps_its_trusted_error(self):
        candidate = self.candidate_creation_failure()
        candidate_error = MODULE.validate_candidate_task_creation_failure_result(
            candidate,
            preflight=self.preflight,
            requested_model="gpt-6-sol",
        )
        self.assertEqual("api_failure", candidate_error["code"])
        malformed_candidate = copy.deepcopy(candidate)
        malformed_candidate["completion"] = {}
        with self.assertRaisesRegex(
            MODULE.WorkflowError,
            "malformed or mismatched identity",
        ):
            MODULE.validate_candidate_task_creation_failure_result(
                malformed_candidate,
                preflight=self.preflight,
                requested_model="gpt-6-sol",
            )


    def test_assignment_failure_retains_unknown_creation_without_retry(self):
        state_path = self.directory / "cca-disabled-state.json"
        helper = self.directory / "cloud_task.py"
        helper.write_text("# helper\n", encoding="utf-8")
        failure = self.candidate_creation_failure()
        failure["requested_model"] = "gpt-5.6-sol"
        failure["error"] = {
            "code": "assignment_unavailable",
            "message": (
                "start Agent Task failed with HTTP 404: assignment not found; "
                "task admission is unknown and the request was not retried"
            ),
        }
        commands = []

        def helper_run(command, **_kwargs):
            commands.append(command)
            output = Path(command[command.index("--result-file") + 1])
            output.write_text(json.dumps(failure), encoding="utf-8")
            return MODULE.subprocess.CompletedProcess(command, 2, "", "failed")

        args = self.arguments(state_path)
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=self.repo_root),
            mock.patch.object(
                MODULE,
                "resolve_target",
                return_value=MODULE.parse_target(args.target),
            ),
            mock.patch.object(
                MODULE,
                "agent_task_preflight",
                return_value=self.preflight,
            ),
            mock.patch.object(MODULE, "discover_cloud_task", return_value=helper),
            mock.patch.object(MODULE, "run", side_effect=helper_run),
            mock.patch.object(
                MODULE,
                "local_identity",
                return_value=self.preflight["identity"],
            ),
            mock.patch.object(MODULE, "publish_shared_state"),
            mock.patch.object(
                MODULE.secrets,
                "token_hex",
                side_effect=[
                    "invocation-1",
                    "run-1",
                    "invocation-2",
                    "run-2",
                ],
            ),
        ):
            with self.assertRaisesRegex(
                MODULE.WorkflowError,
                r"Agent Task failed \[assignment_unavailable\]: start Agent Task failed",
            ):
                MODULE.command_agent_task(args)
            with self.assertRaisesRegex(
                MODULE.WorkflowError,
                "unfinished Agent Task already owns this state",
            ):
                MODULE.command_agent_task(args)

        state = MODULE.load_state(state_path)
        task = state["agent_task"]
        self.assertEqual(0, state["iterations"])
        self.assertEqual([], state["history"])
        self.assertEqual(
            {
                "id": None,
                "url": None,
                "state": None,
                "base_ref": None,
                "base_sha": None,
            },
            task["task"],
        )
        self.assertEqual(
            {"branch": None, "head_sha": None, "commits": []},
            task["generated"],
        )
        self.assertIsNone(task["report"])
        self.assertIsNone(task["semantic_output"])
        self.assertIsNone(task["candidate"])
        self.assertIsNone(task["completion"])
        self.assertEqual("unknown_creation", task["task_id_status"])
        self.assertNotIn("recovery_command", task)
        self.assertNotIn("retry_command", task)
        self.assertNotIn("managed_task_history", state)
        self.assertEqual("run-1", task["run_id"])
        self.assertTrue(
            all("--apply-with-report" in command for command in commands)
        )
        self.assertTrue(
            all(
                command[command.index("--policy") + 1]
                == MODULE.AGENT_TASK_POLICY
                for command in commands
            )
        )
        self.assertTrue(all("--resume" not in command for command in commands))


    def remote(self, *, commits=None):
        return MODULE.validate_success_result(
            self.result(commits=commits),
            preflight=self.preflight,
            requested_model="gpt-6-sol",
        )

    def receipt(self):
        return json.dumps(
            self.validation,
            separators=(",", ":"),
            sort_keys=True,
        )

    def report(
        self,
        *,
        commits=None,
        outcome="cleared",
        findings=None,
        metadata=None,
    ):
        commits = [] if commits is None else commits
        findings = [] if findings is None else findings
        pr = self.preflight["pr"]
        return json.dumps(
            {
                "schema": MODULE.LEGACY_SELF_REVIEW_REPORT_SCHEMA,
                "request_id": "request-1",
                "repository": "owner/repo",
                "pull_request": {
                    "number": 7,
                    "head_sha": self.head,
                    "base_sha": self.base,
                    "title_sha256": MODULE.sha256_text(pr["title"]),
                    "body_sha256": MODULE.sha256_text(pr["body"]),
                },
                "outcome": outcome,
                "iterations_used": 1,
                "findings": findings,
                "pull_request_metadata": metadata
                or {
                    "decision": "keep",
                    "title": pr["title"],
                    "body": pr["body"],
                    "reason": "The current metadata covers the final diff.",
                },
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def arguments(self, state_path, *, resume=False):
        return SimpleNamespace(
            target="owner/repo#7",
            repo_root=str(self.repo_root),
            state=str(state_path),
            resume=resume,
            prepare_only=False,
            apply_prepared=False,
            preserve_artifacts=False,
            model="sol",
            max_iterations=5,
            pipeline_run=None,
            pipeline_iteration=None,
            pipeline_max_iterations=None,
        )


    def split_identity_preflight(self):
        return {
            **self.preflight,
            "identity": {
                "branch": "trask-fix-dashboard-publisher-contention",
                "head": "8c15ae92f010174cc4b0877582dc3e889396550d",
                "status": "",
            },
            "pr": {
                **self.preflight["pr"],
                "number": 347,
                "repo_name": "open-telemetry/shared-workflows",
                "pr_url": (
                    "https://github.com/open-telemetry/shared-workflows/pull/347"
                ),
                "title": "Prevent dashboard publisher starvation",
                "body": (
                    "Prevents concurrent dashboard state updates from starving a "
                    "publisher. State writers respect the repository publisher lease, "
                    "while `--force-with-lease` handles races that begin before the "
                    "lease commit.\n\n"
                    "Direct workflows wait for the publisher. Queue workers return "
                    "every claim for a busy repository and retry after five minutes "
                    "without using the processing-failure budget. Targeted updates and "
                    "head-SHA claim resolution check the lease before GitHub API or "
                    "Copilot work.\n\n"
                    "Fixes #341"
                ),
                "head_owner": "open-telemetry",
                "head_repo": "shared-workflows",
                "head_repository": "open-telemetry/shared-workflows",
                "head_branch": "trask-fix-dashboard-publisher-contention",
                "head_sha": "8c15ae92f010174cc4b0877582dc3e889396550d",
                "base_branch": "main",
                "base_sha": "ad5b9918d6eca8cc999d7034757aee727b2631ea",
                "upstream_owner": "open-telemetry",
                "upstream_repo": "shared-workflows",
                "is_draft": True,
            },
        }

    def nested_identity_preflight(self):
        preflight = self.split_identity_preflight()
        preflight["identity"]["head"] = (
            "f1e7ea3dabd0fab27c6fadc2d257c97ce574e106"
        )
        preflight["pr"]["head_sha"] = (
            "f1e7ea3dabd0fab27c6fadc2d257c97ce574e106"
        )
        preflight["pr"]["base_sha"] = (
            "55fb421179d32aef3b36c7f6503f57193561d14c"
        )
        return preflight

    def test_agent_definition_is_a_thin_managed_coordinator(self):
        instructions = AGENT.read_text(encoding="utf-8")
        self.assertIn("agent-task <target>", instructions)
        self.assertIn("model: gpt-6-sol", instructions)
        self.assertIn("execution-status` takes no arguments", instructions)
        self.assertIn("Use the verified `session_title`", instructions)
        self.assertNotIn("--execution-handle", instructions)
        self.assertNotIn("--pipeline-run", instructions)
        self.assertNotIn("tools: [read", instructions)
        self.assertNotIn("tools: [edit", instructions)
        plugin = json.loads(PLUGIN.read_text(encoding="utf-8"))
        self.assertEqual(plugin["version"], "1.3.72")
        self.assertNotIn("custom_agent", plugin)

    def test_standalone_parser_rejects_internal_execution_arguments(self):
        parser = MODULE.build_parser()
        for flag, value in (
            ("--repo-root", "repo"),
            ("--state", "state.json"),
            ("--pipeline-run", "run"),
            ("--pipeline-iteration", "1"),
            ("--pipeline-max-iterations", "2"),
            ("--execution-handle", "handle.json"),
        ):
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                parser.parse_args(["agent-task", "7", flag, value])

    def test_execution_runtime_uses_the_current_agent_session(self):
        runtime = mock.Mock()
        runtime.entrypoint.return_value = 17
        with (
            mock.patch.object(MODULE.sys, "argv", ["helper", "agent-task", "7"]),
            mock.patch.dict(
                MODULE.os.environ,
                {"COPILOT_AGENT_SESSION_ID": "session-1"},
                clear=False,
            ),
            mock.patch.object(MODULE, "_load_execution", return_value=runtime),
        ):
            self.assertEqual(17, MODULE.execution_main())

        runtime.entrypoint.assert_called_once_with(
            MODULE.main, MODULE.__dict__, commands=("agent-task", "pipeline")
        )
        with (
            mock.patch.object(
                MODULE.sys,
                "argv",
                ["helper", "agent-task", "7", "--state", "state.json"],
            ),
            mock.patch.object(MODULE, "main", return_value=23),
            mock.patch.object(MODULE, "_load_execution") as load_execution,
        ):
            self.assertEqual(23, MODULE.execution_main())
        load_execution.assert_not_called()

        runtime.reset_mock()
        with (
            mock.patch.object(
                MODULE.sys,
                "argv",
                ["helper", "pipeline", "7", "--state", "state.json"],
            ),
            mock.patch.dict(
                MODULE.os.environ,
                {"TRASK_EXECUTION_PARENT": "request.json"},
                clear=True,
            ),
            mock.patch.object(MODULE, "_load_execution", return_value=runtime),
        ):
            self.assertEqual(17, MODULE.execution_main())
        runtime.entrypoint.assert_called_once_with(
            MODULE.main, MODULE.__dict__, commands=("agent-task", "pipeline")
        )

    def test_report_parser_accepts_markdown_with_one_json_payload(self):
        content = "# Result\n\nReadable summary.\n\n```json\n{\"ok\":true}\n```"
        self.assertEqual(
            {"ok": True},
            MODULE.parse_markdown_report(content, description="test report"),
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "exactly one"):
            MODULE.parse_markdown_report("# Result", description="test report")

    def test_prompt_is_versioned_self_contained_and_fail_closed(self):
        prompt = MODULE.build_worker_prompt(
            self.preflight,
            max_iterations=5,
            prior_history=[],
        )
        self.assertIn("worker prompt version 13", prompt)
        self.assertIn("zero or more linear, single-parent code commits", prompt)
        self.assertIn("The dispatcher derives the exact candidate history", prompt)
        self.assertIn(MODULE.AGENT_TASK_OUTPUT_REPORT, prompt)
        self.assertIn("report is advisory and may be absent", prompt)
        self.assertNotIn("commit_index", prompt)
        pinned_data = prompt.split("Pinned preflight data follows.", 1)[1]
        self.assertNotIn("changed_paths", pinned_data)
        self.assertNotIn("{{MARKETPLACE_SEMANTIC_PATH}}", prompt)
        self.assertNotIn('"request_id":', prompt)
        self.assertIn("Perform one review pass only", prompt)
        self.assertIn("untrusted data", prompt)
        self.assertNotIn("`Finding: <identifier>`", prompt)
        self.assertIn("Zero code commits does not establish a clean review", prompt)
        self.assertIn(MODULE.AGENT_TASK_OUTPUT_RESULT, prompt)
        self.assertNotIn("MARKETPLACE_VALIDATION_PATH", prompt)
        MODULE.require_no_credentials(prompt, source="prompt")



















    def test_preserve_artifacts_keeps_self_review_prompt_and_result(self):
        task_state = {
            "prompt_file": "prompt.txt",
            "result_file": "result.json",
            "recovery_command": "resume",
        }
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt.txt"
            result = Path(directory) / "result.json"
            prompt.write_text("prompt", encoding="utf-8")
            result.write_text("result", encoding="utf-8")

            MODULE.finalize_agent_task_artifacts(
                task_state,
                {prompt, result},
                preserve=True,
            )

            self.assertTrue(prompt.is_file())
            self.assertTrue(result.is_file())
        self.assertFalse(task_state["artifacts_removed"])
        self.assertTrue(task_state["artifacts_preserved"])
        self.assertNotIn("recovery_command", task_state)





    def assert_report_recovery_survives_prepare_apply_process_boundary(
        self,
        raw_report,
        *,
        expected_metadata,
        name,
    ):
        state_path = self.directory / f"{name}-prepared.json"
        prompt_path = self.directory / f"{name}-prompt.txt"
        result_path = self.directory / f"{name}-result.json"
        prompt_path.write_text("retained prompt\n", encoding="utf-8")
        stale_base_ref_oid = "9" * 40
        report = json.dumps(raw_report)
        result = self.result()
        result["report"]["sha256"] = MODULE.sha256_text(report)
        result_path.write_text(json.dumps(result), encoding="utf-8")
        MODULE.save_state(
            state_path,
            {
                "version": 1,
                "created_at": "2026-09-17T08:00:00Z",
                "updated_at": "2026-09-17T08:00:00Z",
                "iterations": 0,
                "next_candidate_id": 1,
                "history": [],
                "repo_root": str(self.repo_root),
                "pr": self.preflight["pr"],
                "review": {
                    "id": "review-1",
                    "status": "active",
                    "iteration": 1,
                    "head_sha": self.head,
                    "candidates": [],
                    "batches": [],
                },
                "agent_task": {
                    "status": "failed",
                    "run_id": "run-1",
                    "model": "gpt-6-sol",
                    "policy": "marketplace-agent-apply-report-worker@5",
                    "allowed_iterations": 5,
                    "preflight": self.preflight,
                    "prompt_file": str(prompt_path),
                    "result_file": str(result_path),
                    "clear_shared_state_on_apply": False,
                    "prepared_at": "2026-09-17T08:10:00Z",
                    "preparation": {"legacy": "missing report recovery identity"},
                    "resume_attempts": 2,
                    "error": "runtime-base report identity mismatch",
                },
            },
        )
        prepare_args = self.arguments(state_path, resume=True)
        prepare_args.prepare_only = True
        prepare_args.preserve_artifacts = True
        prepare_args.recovery_state_sha256 = MODULE.sha256_file(state_path)
        prepare_args.recovery_prompt_sha256 = MODULE.sha256_file(prompt_path)
        prepare_args.recovery_result_sha256 = MODULE.sha256_file(result_path)
        prepare_args.recovery_task_id = "task-1"
        prepare_args.recovery_request_id = "request-1"
        prepare_args.recovery_report_base_sha = stale_base_ref_oid
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(
                MODULE, "resolve_repo_root", return_value=self.repo_root
            ),
            mock.patch.object(
                MODULE,
                "resolve_target",
                return_value=MODULE.parse_target("owner/repo#7"),
            ),
            mock.patch.object(MODULE, "discover_cloud_task") as discover,
            mock.patch.object(MODULE, "run") as run,
            mock.patch.object(
                MODULE,
                "local_identity",
                return_value=self.preflight["identity"],
            ),
            mock.patch.object(
                MODULE,
                "verify_runtime_candidate",
                return_value=self.verified_candidate(result),
            ),
            mock.patch.object(MODULE, "fetch_committed_text", return_value=report),
            mock.patch.object(
                MODULE, "metadata_for", return_value=self.preflight["pr"]
            ),
            mock.patch.object(MODULE, "apply_verified_import", return_value=False),
            mock.patch.object(
                MODULE,
                "wait_for_live_pr_snapshot",
                return_value=self.preflight["pr"],
            ),
            mock.patch.object(MODULE, "update_pr_metadata") as update_metadata,
            mock.patch.object(MODULE, "publish_shared_state") as publish_shared,
            mock.patch.object(MODULE, "emit"),
        ):
            MODULE.command_agent_task(prepare_args)

        discover.assert_not_called()
        run.assert_not_called()
        update_metadata.assert_not_called()
        publish_shared.assert_not_called()
        prepared = MODULE.load_state(state_path)
        self.assertEqual(3, prepared["agent_task"]["resume_attempts"])
        self.assertEqual(
            expected_metadata,
            prepared["agent_task"]["preparation"]["pull_request_metadata"],
        )
        recovery = prepared["agent_task"]["preparation"][
            "report_identity_recovery"
        ]
        self.assertEqual(
            {
                "base_sha": stale_base_ref_oid,
                "task_id": "task-1",
                "request_id": "request-1",
                "generated_head": self.artifact,
                "report_sha256": result["report"]["sha256"],
            },
            recovery,
        )

        apply_args = self.arguments(state_path)
        apply_args.apply_prepared = True
        apply_args.preserve_artifacts = True
        update_metadata = mock.Mock(
            return_value={
                **self.preflight["pr"],
                "title": expected_metadata["title"],
                "body": expected_metadata["body"],
            }
        )
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(
                MODULE, "resolve_repo_root", return_value=self.repo_root
            ),
            mock.patch.object(
                MODULE,
                "resolve_target",
                return_value=MODULE.parse_target("owner/repo#7"),
            ),
            mock.patch.object(MODULE, "discover_cloud_task") as discover,
            mock.patch.object(MODULE, "run") as run,
            mock.patch.object(
                MODULE,
                "local_identity",
                return_value=self.preflight["identity"],
            ),
            mock.patch.object(
                MODULE,
                "verify_runtime_candidate",
                return_value=self.verified_candidate(result),
            ),
            mock.patch.object(MODULE, "fetch_committed_text", return_value=report),
            mock.patch.object(
                MODULE, "metadata_for", return_value=self.preflight["pr"]
            ),
            mock.patch.object(
                MODULE, "apply_verified_import", return_value=False
            ) as apply_import,
            mock.patch.object(
                MODULE,
                "wait_for_live_pr_snapshot",
                return_value=self.preflight["pr"],
            ),
            mock.patch.object(MODULE, "update_pr_metadata", new=update_metadata),
            mock.patch.object(MODULE, "publish_shared_state") as publish_shared,
            mock.patch.object(MODULE, "emit"),
        ):
            MODULE.command_agent_task(apply_args)

        discover.assert_not_called()
        run.assert_not_called()
        apply_import.assert_called_once()
        if expected_metadata["decision"] == "replace":
            update_metadata.assert_called_once()
        else:
            update_metadata.assert_not_called()
        self.assertEqual(
            [self.head],
            [call.kwargs["value"] for call in publish_shared.call_args_list],
        )
        completed = MODULE.load_state(state_path)
        self.assertEqual("completed", completed["agent_task"]["status"])
        self.assertEqual(self.head, completed["review"]["clean_at_head_sha"])
        self.assertEqual(expected_metadata["body"], completed["pr"]["body"])


    def test_waits_for_its_own_published_head_but_rejects_other_drift(self):
        fix = "5" * 40
        final = {**self.preflight["pr"], "head_sha": fix}
        with (
            mock.patch.object(
                MODULE,
                "metadata_for",
                side_effect=[self.preflight["pr"], final],
            ) as metadata,
            mock.patch.object(MODULE.time, "sleep") as sleep,
        ):
            actual = MODULE.wait_for_live_pr_snapshot(
                MODULE.parse_target("owner/repo#7"),
                self.preflight["pr"],
                expected_head=fix,
            )
        self.assertEqual(actual["head_sha"], fix)
        self.assertEqual(metadata.call_count, 2)
        sleep.assert_called_once()

        drifted = {**self.preflight["pr"], "body": "Changed elsewhere"}
        with (
            mock.patch.object(MODULE, "metadata_for", return_value=drifted),
            mock.patch.object(MODULE.time, "sleep") as sleep,
            self.assertRaisesRegex(MODULE.WorkflowError, "drifted"),
        ):
            MODULE.wait_for_live_pr_snapshot(
                MODULE.parse_target("owner/repo#7"),
                self.preflight["pr"],
                expected_head=fix,
            )
        sleep.assert_not_called()




    def test_success_dispatches_apply_with_report_and_cleans_only_after_consumption(self):
        state_path = self.directory / "state.json"
        helper = self.directory / "cloud_task.py"
        helper.write_text("# helper\n", encoding="utf-8")
        result = self.candidate_result()
        result["candidate"]["artifact_commit"] = self.candidate_metadata(
            self.artifact, self.head, [MODULE.AGENT_TASK_OUTPUT_RESULT],
        )
        result["candidate"]["generated"]["head_sha"] = self.artifact
        result["generated"]["head_sha"] = self.artifact
        commands = []
        emitted = []

        def helper_run(command, **kwargs):
            commands.append(command)
            result_path = Path(command[command.index("--result-file") + 1])
            result_path.write_text(json.dumps(result), encoding="utf-8")
            return MODULE.subprocess.CompletedProcess(command, 0, "ignored", "")

        arguments = SimpleNamespace(
            target="owner/repo#7",
            repo_root=str(self.repo_root),
            state=str(state_path),
            resume=False,
            model="sol",
            max_iterations=5,
            pipeline_run=None,
            pipeline_iteration=None,
            pipeline_max_iterations=None,
        )
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=self.repo_root),
            mock.patch.object(
                MODULE,
                "resolve_target",
                return_value=MODULE.parse_target("owner/repo#7"),
            ),
            mock.patch.object(
                MODULE, "agent_task_preflight", return_value=self.preflight
            ),
            mock.patch.object(MODULE, "discover_cloud_task", return_value=helper),
            mock.patch.object(MODULE, "run", side_effect=helper_run),
            mock.patch.object(MODULE, "git", return_value='{"outcome":"clean","iterations_used":1}'),
            mock.patch.object(
                MODULE,
                "local_identity",
                return_value=self.preflight["identity"],
            ),
            mock.patch.object(
                MODULE,
                "verify_runtime_candidate",
                return_value=self.verified_candidate(result),
            ),
            mock.patch.object(
                MODULE, "apply_verified_candidate_import", return_value=False
            ),
            mock.patch.object(
                MODULE,
                "metadata_for",
                return_value=self.preflight["pr"],
            ),
            mock.patch.object(MODULE, "publish_shared_state") as publish_shared_state,
            mock.patch.object(MODULE, "emit", emitted.append),
            mock.patch.object(
                MODULE.secrets,
                "token_hex",
                side_effect=["invocation-1", "run-1"],
            ),
        ):
            MODULE.command_agent_task(arguments)

        self.assertIn("--apply-with-report", commands[0])
        self.assertEqual(
            MODULE.AGENT_TASK_POLICY,
            commands[0][commands[0].index("--policy") + 1],
        )
        self.assertEqual(emitted[0]["result"], "nothing_to_publish")
        self.assertEqual(
            emitted[0]["session_title"],
            f"Self Review Loop: 7 - {self.preflight['pr']['title']}",
        )
        self.assertEqual(emitted[0]["stage_outcome"], "cleared")
        state = MODULE.load_state(state_path)
        self.assertEqual(state["agent_task"]["status"], "completed")
        self.assertTrue(state["agent_task"]["artifacts_removed"])
        self.assertNotIn("recovery_command", state["agent_task"])
        self.assertFalse(
            list(state_path.parent.glob("*--agent-task-prompt.txt"))
        )
        self.assertFalse(
            list(state_path.parent.glob("*--agent-task-result.json"))
        )
        self.assertEqual(
            [call.kwargs["value"] for call in publish_shared_state.call_args_list],
            [None, self.head],
        )

    @contextlib.contextmanager
    def pipeline_run(
        self, *, fixes=1, exit_code=0, task_state="completed", max_iterations=3,
        task_error=None,
    ):
        args = MODULE.build_parser().parse_args(
            [
                "pipeline", "owner/repo#7",
                "--state", str(self.directory / "pipeline.json"),
                "--pipeline-run", "pipeline-1",
                "--pipeline-iteration", "1",
                "--pipeline-max-iterations", "2",
                "--max-iterations", str(max_iterations),
                "--github-mutation-policy", "source-only",
                "--model", "sol",
            ]
        )
        live = copy.deepcopy(self.preflight["pr"])
        identity = {**self.preflight["identity"], "branch": ""}
        self.pipeline_live = live
        self.pipeline_identity = identity
        commands, emitted = [], []

        def preflight(*args, **kwargs):
            self.assertTrue(kwargs["allow_detached"])
            self.head = live["head_sha"]
            self.preflight = {
                **self.preflight,
                "pr": copy.deepcopy(live),
                "identity": dict(identity),
            }
            return copy.deepcopy(self.preflight)

        def run(command, **kwargs):
            if "--result-file" in command:
                self.assertEqual(live["head_sha"], identity["head"])
                if not commands:
                    self.assertFalse(emitted)
                commands.append(command)
                state = MODULE.load_state(Path(args.state))
                self.assertEqual(
                    min(len(commands) - 1, max_iterations),
                    state["iterations"],
                )
                self.assertEqual(
                    max_iterations - state["iterations"],
                    state["agent_task"]["allowed_iterations"],
                )
                self.assertEqual("sol", command[command.index("--model") + 1])
                prompt = Path(command[command.index("--prompt-file") + 1])
                self.assertIn(
                    '"maximum_review_iterations": 1',
                    prompt.read_text(encoding="utf-8"),
                )
                commits = (
                    [f"{len(commands) + 5:040x}"]
                    if len(commands) <= fixes else []
                )
                result = self.candidate_result(commits=commits)
                if not commits:
                    result["candidate"]["artifact_commit"] = self.candidate_metadata(
                        self.artifact, self.head, [MODULE.AGENT_TASK_OUTPUT_RESULT],
                    )
                    result["candidate"]["generated"]["head_sha"] = self.artifact
                    result["generated"]["head_sha"] = self.artifact
                result["task"]["state"] = task_state
                result["completion"]["task"]["state"] = task_state
                if task_error is not None:
                    result.update(
                        status="error", error=task_error, candidate=None, completion=None
                    )
                    result["attestation"]["structural_complete"] = False
                    result["generated"].update(head_sha=None, commits=[])
                result_path = Path(command[command.index("--result-file") + 1])
                result_path.write_text(json.dumps(result), encoding="utf-8")
                return MODULE.subprocess.CompletedProcess(command, exit_code, "", "")
            if "merge" in command:
                identity["head"] = command[-1]
            elif "push" in command:
                live["head_sha"] = identity["head"]
            else:
                self.fail(f"unexpected command: {command}")
            return MODULE.subprocess.CompletedProcess(command, 0, "", "")

        def outcome_git(_root, *arguments):
            self.assertEqual(("show", f"{self.artifact}:{MODULE.AGENT_TASK_OUTPUT_RESULT}"), arguments)
            return '{"outcome":"clean","iterations_used":1}'

        def verify_candidate(result, **_kwargs):
            if result["task"]["state"] != "completed":
                raise MODULE.WorkflowError("Self Review candidate rejected")
            candidate = result["candidate"]
            artifact = candidate["artifact_commit"]
            return {
                "contract": "candidate",
                "task_id": result["task"]["id"],
                "task_url": result["task"]["url"],
                "session_id": result["completion"]["session"]["id"],
                "generated_branch": result["generated"]["branch"],
                "generated_head": result["generated"]["head_sha"],
                "code_tip": candidate["generated"]["code_tip_sha"],
                "commits": [item["sha"] for item in candidate["code_commits"]],
                "final_local_head": candidate["generated"]["code_tip_sha"],
                "requires_apply": True,
                "candidate_manifest": candidate,
                "completion": result["completion"],
                "report_evidence": (
                    {
                        "path": MODULE.AGENT_TASK_OUTPUT_RESULT,
                        "commit": artifact["sha"],
                        "patch_sha256": artifact["patch_sha256"],
                    }
                    if artifact is not None
                    else None
                ),
                "structural_attestation": True,
            }

        with (
            mock.patch.object(MODULE, "ACTIVE_GITHUB_MUTATION_POLICY", "allow"),
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=self.repo_root),
            mock.patch.object(MODULE, "agent_task_preflight", side_effect=preflight),
            mock.patch.object(
                MODULE, "discover_cloud_task", return_value=self.directory / "runtime.py"
            ),
            mock.patch.object(MODULE, "run", side_effect=run),
            mock.patch.object(MODULE, "git", side_effect=outcome_git),
            mock.patch.object(MODULE, "local_identity", side_effect=lambda _: dict(identity)),
            mock.patch.object(
                MODULE, "verify_runtime_candidate", side_effect=verify_candidate
            ),
            mock.patch.object(
                MODULE,
                "apply_verified_candidate_import",
                side_effect=lambda *a, **kw: identity.update(
                    head=kw["remote"]["final_local_head"]
                )
                or bool(kw["remote"]["commits"]),
            ),
            mock.patch.object(MODULE, "metadata_for", side_effect=lambda _: dict(live)),
            mock.patch.object(MODULE, "remote_head", side_effect=lambda *a: live["head_sha"]),
            mock.patch.object(
                MODULE, "wait_for_remote_head", side_effect=lambda *a: live["head_sha"]
            ),
            mock.patch.object(MODULE, "find_push_remote", return_value="origin"),
            mock.patch.object(MODULE, "publish_shared_state") as shared,
            mock.patch.object(MODULE, "emit", emitted.append),
        ):
            yield args, commands, emitted
            shared.assert_not_called()

    def test_pipeline_waits_for_fixes_then_a_terminal_clean_pass(self):
        with self.pipeline_run() as (args, commands, emitted):
            MODULE.command_pipeline(args)
            self.assertEqual(2, len(commands))
            self.assertEqual(1, len(emitted))
            self.assertEqual("published", emitted[0]["result"])
            self.assertEqual("cleared", emitted[0]["stage_outcome"])
            self.assertEqual(2, emitted[0]["iterations"])
            self.assertEqual(1, len(emitted[0]["commits"]))
            self.assertEqual(2, len(emitted[0]["tasks"]))
            state = MODULE.load_state(Path(args.state))
            self.assertEqual("", state["agent_task"]["preflight"]["identity"]["branch"])
            self.assertEqual(state["pr"]["head_sha"], MODULE.recorded_clean_at_head_sha(state))
            self.assertEqual(1, len(state["managed_task_history"]))

    def test_clean_pass_survives_target_branch_advance(self):
        with self.pipeline_run(fixes=0) as (args, commands, emitted):
            original_run = MODULE.run.side_effect

            def advance_base(command, **kwargs):
                result = original_run(command, **kwargs)
                if "--result-file" in command:
                    self.pipeline_live["base_sha"] = "8" * 40
                return result

            with (
                mock.patch.object(MODULE, "run", side_effect=advance_base),
                mock.patch.object(MODULE, "gh_json", return_value={"status": "ahead"}),
            ):
                MODULE.command_pipeline(args)
            self.assertEqual(1, len(commands))
            self.assertEqual("cleared", emitted[-1]["stage_outcome"])
            state = MODULE.load_state(Path(args.state))
            self.assertEqual("clean", state["review"]["outcome"])
            self.assertEqual("8" * 40, state["review"]["clean_at_base_sha"])

    def test_later_task_failure_preserves_published_pass_without_clearance(self):
        with self.pipeline_run() as (args, commands, emitted):
            original_run = MODULE.run.side_effect

            def fail_second_pass(command, **kwargs):
                if "--result-file" in command and commands:
                    raise MODULE.WorkflowError("next pass unavailable")
                return original_run(command, **kwargs)

            with mock.patch.object(MODULE, "run", side_effect=fail_second_pass):
                with self.assertRaisesRegex(MODULE.WorkflowError, "next pass unavailable"):
                    MODULE.command_pipeline(args)
            state = MODULE.load_state(Path(args.state))
            self.assertEqual(1, state["iterations"])
            self.assertEqual(1, len(commands))
            self.assertEqual("failed", state["agent_task"]["status"])
            self.assertEqual(1, len(state["managed_task_history"]))
            self.assertEqual(
                ["0" * 39 + "6"],
                state["managed_task_history"][0]["ordered_commits"],
            )
            self.assertIsNone(MODULE.recorded_clean_at_head_sha(state))
            self.assertEqual([], emitted)

    def test_bounded_pipeline_observes_task_and_waits_for_remote_visibility(self):
        with self.pipeline_run(fixes=1) as (args, commands, emitted):
            args.pipeline_run = "b" * 32
            args.bounded_step = True
            original_run = MODULE.run.side_effect
            dispatches = []
            clock = [0]
            remote_checks = [0]
            visibility_checks = [0]

            def run(command, **kwargs):
                if "--pipeline-dispatch" in command or "--pipeline-observe" in command:
                    dispatches.append(command)
                    if len(dispatches) < 4:
                        if len(dispatches) == 1:
                            result_path = Path(command[command.index("--result-file") + 1])
                            result_path.with_name(
                                result_path.name + ".pipeline.json"
                            ).write_text("{}", encoding="utf-8")
                        return MODULE.subprocess.CompletedProcess(
                            command, 0,
                            pending_task_stdout(args.pipeline_run, "review-session"), "",
                        )
                    old = emitted[:]
                    emitted.clear()
                    try:
                        return original_run(command, **kwargs)
                    finally:
                        emitted.extend(old)
                return original_run(command, **kwargs)

            def remote_head(*_):
                remote_checks[0] += 1
                if remote_checks[0] <= 4:
                    return self.preflight["pr"]["head_sha"]
                return self.pipeline_identity["head"]

            def metadata(_):
                if self.pipeline_live["head_sha"] != self.preflight["pr"]["head_sha"]:
                    visibility_checks[0] += 1
                    if visibility_checks[0] <= 3:
                        return {
                            **self.pipeline_live,
                            "head_sha": self.preflight["pr"]["head_sha"],
                        }
                return dict(self.pipeline_live)

            with (
                mock.patch.dict(
                    MODULE.os.environ, {"COPILOT_AGENT_SESSION_ID": "review-session"}
                ),
                mock.patch.object(MODULE, "run", side_effect=run),
                mock.patch.object(MODULE, "remote_head", side_effect=remote_head),
                mock.patch.object(MODULE, "metadata_for", side_effect=metadata),
                mock.patch.object(MODULE.time, "monotonic", side_effect=lambda: clock[0]),
                mock.patch.object(MODULE.time, "sleep", side_effect=AssertionError("blocking wait")),
            ):
                for _ in range(3):
                    MODULE.command_pipeline(args)
                    self.assertEqual("waiting", emitted[-1]["result"])
                    self.assertNotIn("stage_outcome", emitted[-1])
                    clock[0] += 40
                with mock.patch.dict(
                    MODULE.os.environ, {"COPILOT_AGENT_SESSION_ID": "other-session"}
                ):
                    with self.assertRaisesRegex(MODULE.WorkflowError, "different inputs or session"):
                        MODULE.command_pipeline(args)
                with mock.patch.object(args, "max_iterations", 4):
                    with self.assertRaisesRegex(MODULE.WorkflowError, "different inputs or session"):
                        MODULE.command_pipeline(args)
                with mock.patch.object(args, "target", "owner/repo#8"):
                    with self.assertRaisesRegex(MODULE.WorkflowError, "different inputs or session"):
                        MODULE.command_pipeline(args)
                self.assertEqual([], commands)
                pending = MODULE.load_state(Path(args.state))["agent_task"]
                self.assertFalse(Path(pending["result_file"]).exists())
                for _ in range(10):
                    MODULE.command_pipeline(args)
                    clock[0] += 40
                    if emitted[-1]["result"] != "waiting":
                        break
                else:
                    self.fail("bounded publication did not finish")
                self.assertEqual("cleared", emitted[-1]["stage_outcome"])
                self.assertGreater(clock[0], 120)
                self.assertEqual(2, len(commands))
                self.assertEqual("completed", MODULE.load_state(Path(args.state))["agent_task"]["status"])
                result_path = Path(dispatches[0][dispatches[0].index("--result-file") + 1])
                self.assertFalse(
                    result_path.with_name(result_path.name + ".pipeline.json").exists()
                )
            self.assertIn("--pipeline-dispatch", dispatches[0])
            self.assertEqual(
                2, sum("--pipeline-dispatch" in cmd for cmd in dispatches)
            )
            self.assertTrue(all(
                "--pipeline-observe" in cmd or "--pipeline-dispatch" in cmd
                for cmd in dispatches
            ))
            self.assertTrue(all("--apply-with-report" in cmd for cmd in dispatches))

    def test_bounded_dispatch_accepts_sealed_pending_without_stdout(self):
        with self.pipeline_run(fixes=0) as (args, _commands, emitted):
            args.pipeline_run = "b" * 32
            args.bounded_step = True
            execution = SimpleNamespace(children=[], record_state=lambda *_: None)
            original_run = MODULE.run.side_effect

            def run(command, **kwargs):
                if "--pipeline-dispatch" not in command:
                    return original_run(command, **kwargs)
                self.assertIs(kwargs["require_execution"], True)
                result_path = Path(command[command.index("--result-file") + 1])
                result_path.with_name(
                    result_path.name + ".pipeline.json"
                ).write_text("{}", encoding="utf-8")
                execution.children.append(SimpleNamespace(terminal_result={
                    "exit_code": 0, "local_status": "finished",
                    "workflow_result": json.loads(
                        pending_task_stdout(args.pipeline_run, "review-session")
                    ),
                }))
                return MODULE.subprocess.CompletedProcess(command, 0, "", "")

            with (
                mock.patch.dict(
                    MODULE.os.environ, {"COPILOT_AGENT_SESSION_ID": "review-session"}
                ),
                mock.patch.object(MODULE, "_EXECUTION", execution),
                mock.patch.object(MODULE, "run", side_effect=run),
            ):
                MODULE.command_pipeline(args)
            self.assertEqual("waiting", emitted[-1]["result"])
            self.assertEqual(
                "running", MODULE.load_state(Path(args.state))["agent_task"]["status"]
            )

    def test_bounded_pipeline_requires_session_and_hex_run(self):
        args = MODULE.build_parser().parse_args([
            "pipeline", "owner/repo#7", "--state", str(self.directory / "bounded.json"),
            "--pipeline-run", "not-hex", "--pipeline-iteration", "1",
            "--pipeline-max-iterations", "2", "--bounded-step",
        ])
        with mock.patch.dict(MODULE.os.environ, {"COPILOT_AGENT_SESSION_ID": "session"}):
            with self.assertRaisesRegex(MODULE.WorkflowError, "32 lowercase hex"):
                MODULE.command_pipeline(args)
        args.pipeline_run = "a" * 32
        with mock.patch.dict(MODULE.os.environ, {"COPILOT_AGENT_SESSION_ID": ""}):
            with self.assertRaisesRegex(MODULE.WorkflowError, "COPILOT_AGENT_SESSION_ID"):
                MODULE.command_pipeline(args)

    def test_bounded_pipeline_detects_source_drift_after_observation(self):
        with self.pipeline_run(fixes=0, max_iterations=5) as (args, commands, emitted):
            args.pipeline_run = "d" * 32
            args.bounded_step = True
            original_run = MODULE.run.side_effect
            calls = []
            imported = MODULE.apply_verified_candidate_import

            def run(command, **kwargs):
                if "--pipeline-dispatch" in command:
                    calls.append(command)
                    result_path = Path(command[command.index("--result-file") + 1])
                    result_path.with_name(
                        result_path.name + ".pipeline.json"
                    ).write_text("{}", encoding="utf-8")
                    return MODULE.subprocess.CompletedProcess(
                        command, 0,
                        pending_task_stdout(args.pipeline_run, "review-session"), "",
                    )
                if "--pipeline-observe" in command:
                    calls.append(command)
                    waiting = emitted[:]
                    emitted.clear()
                    try:
                        finished = original_run(command, **kwargs)
                    finally:
                        emitted.extend(waiting)
                    self.pipeline_live["head_sha"] = "9" * 40
                    return finished
                return original_run(command, **kwargs)

            with (
                mock.patch.dict(
                    MODULE.os.environ, {"COPILOT_AGENT_SESSION_ID": "review-session"}
                ),
                mock.patch.object(MODULE, "run", side_effect=run),
                mock.patch.object(MODULE, "gh_json", return_value={"status": "ahead"}),
            ):
                MODULE.command_pipeline(args)
                self.assertEqual("waiting", emitted[-1]["result"])
                MODULE.command_pipeline(args)
                self.assertEqual("waiting", emitted[-1]["result"])
                self.assertEqual(
                    "result_ready",
                    MODULE.load_state(Path(args.state))["agent_task"]["status"],
                )
                MODULE.command_pipeline(args)
                self.assertEqual("source_changed", emitted[-1]["result"])
                self.assertEqual("source_changed", emitted[-1]["stage_outcome"])
                retained = MODULE.load_state(Path(args.state))
                self.assertEqual("superseded", retained["agent_task"]["status"])
                self.assertEqual(1, retained["agent_task"]["consumed_iterations"])
                self.assertEqual(1, retained["iterations"])
                self.assertEqual(
                    4, args.max_iterations - (
                        retained["iterations"] - retained["pipeline_budget"]["baseline"]
                    )
                )
                imported.assert_not_called()
            self.assertEqual(2, len(calls))
            self.assertEqual(1, len(commands))

    def test_pipeline_spends_its_budget_once_across_all_sweeps(self):
        with self.pipeline_run(fixes=5) as (args, commands, emitted):
            MODULE.command_pipeline(args)
            self.assertEqual(3, len(commands))
            self.assertEqual(3, len(emitted[-1]["commits"]))
            self.assertEqual("max_iterations_reached", emitted[-1]["stage_outcome"])
            self.assertIsNone(MODULE.recorded_clean_at_head_sha(MODULE.load_state(Path(args.state))))
            args.pipeline_iteration = 2
            MODULE.command_pipeline(args)
            self.assertEqual(3, len(commands))
            self.assertEqual(3, emitted[-1]["iterations"])
            self.assertEqual("max_iterations_reached", emitted[-1]["stage_outcome"])

    def test_one_pass_budget_publishes_code_without_claiming_clean(self):
        with self.pipeline_run(fixes=2, max_iterations=1) as (args, commands, emitted):
            MODULE.command_pipeline(args)
            self.assertEqual(1, len(commands))
            self.assertEqual("published", emitted[-1]["result"])
            self.assertEqual("max_iterations_reached", emitted[-1]["stage_outcome"])
            self.assertEqual(1, emitted[-1]["iterations"])
            self.assertEqual(1, len(emitted[-1]["commits"]))
            self.assertIsNone(
                MODULE.recorded_clean_at_head_sha(MODULE.load_state(Path(args.state)))
            )

    def pipeline_cli(self, args):
        argv = [str(SCRIPT), "pipeline", args.target]
        for name in (
            "state", "pipeline_run", "pipeline_iteration", "pipeline_max_iterations",
            "max_iterations", "github_mutation_policy", "model",
        ):
            argv.extend(["--" + name.replace("_", "-"), str(getattr(args, name))])
        with mock.patch.object(MODULE.sys, "argv", argv):
            return MODULE.main()

    def test_later_sweep_inspects_new_head_with_original_remaining_budget(self):
        with self.pipeline_run() as (args, commands, emitted):
            self.assertEqual(0, self.pipeline_cli(args))
            first = MODULE.load_state(Path(args.state))
            self.assertEqual(2, first["iterations"])
            self.pipeline_live["head_sha"] = "9" * 40
            self.pipeline_identity["head"] = "9" * 40
            args.pipeline_iteration = 2
            emitted.clear()
            self.assertEqual(0, self.pipeline_cli(args))
            second = MODULE.load_state(Path(args.state))
            self.assertEqual(3, len(commands))
            self.assertEqual(3, second["iterations"])
            self.assertEqual(1, second["agent_task"]["allowed_iterations"])
            self.assertEqual("9" * 40, second["agent_task"]["preflight"]["pr"]["head_sha"])
            self.assertEqual("9" * 40, MODULE.recorded_clean_at_head_sha(second))
            self.assertNotEqual(first["agent_task"]["run_id"], second["agent_task"]["run_id"])
            self.assertNotIn("--resume", commands[-1])

    def test_pipeline_retries_validated_state_write_without_repeating_task(self):
        for failures, exit_code in ((1, 0), (6, 1)):
            with self.subTest(failures=failures), self.pipeline_run(fixes=0) as (
                args, commands, emitted
            ):
                args.state = str(self.directory / f"pipeline-{failures}.json")
                replace = MODULE.os.replace
                attempts = []

                def replace_blocked(source, destination):
                    payload = Path(source).read_bytes()
                    if Path(destination) == Path(args.state):
                        state = json.loads(payload)
                        if state["agent_task"]["status"] == "validated":
                            attempts.append(payload)
                            if len(attempts) <= failures:
                                error = PermissionError(13, "status reader is open")
                                error.winerror = 5
                                raise error
                    replace(source, destination)

                with (
                    mock.patch.object(MODULE, "IS_WINDOWS", True),
                    mock.patch.object(MODULE.os, "replace", side_effect=replace_blocked),
                    mock.patch.object(MODULE.time, "sleep") as sleep,
                    mock.patch.object(
                        MODULE, "apply_verified_candidate_import",
                        wraps=MODULE.apply_verified_candidate_import,
                    ) as importer,
                ):
                    self.assertEqual(exit_code, self.pipeline_cli(args))
                importer.assert_called_once()
                self.assertEqual(1, len(commands))
                self.assertFalse(any("push" in call.args[0] for call in MODULE.run.call_args_list))
                self.assertEqual([attempts[0]] * (2 if exit_code == 0 else 6), attempts)
                state = MODULE.load_state(Path(args.state))
                self.assertEqual("completed" if exit_code == 0 else "failed", state["agent_task"]["status"])
                self.assertEqual(1 if exit_code == 0 else 0, state["iterations"])
                if exit_code == 0:
                    self.assertEqual("cleared", emitted[-1]["stage_outcome"])
                    sleep.assert_called_once_with(0.01)
                else:
                    self.assertEqual("error", emitted[-1]["result"])
                    self.assertIsNone(MODULE.recorded_clean_at_head_sha(state))
                    self.assertEqual(5, sleep.call_count)

    def test_later_sweep_new_head_does_not_replenish_exhausted_budget(self):
        with self.pipeline_run(max_iterations=2) as (args, commands, emitted):
            self.assertEqual(0, self.pipeline_cli(args))
            self.pipeline_live["head_sha"] = "9" * 40
            self.pipeline_identity["head"] = "9" * 40
            args.pipeline_iteration = 2
            emitted.clear()
            self.assertEqual(0, self.pipeline_cli(args))
            state = MODULE.load_state(Path(args.state))
            self.assertEqual(2, len(commands))
            self.assertEqual(2, state["iterations"])
            self.assertEqual("max_iterations_reached", emitted[-1]["stage_outcome"])
            self.assertIsNone(MODULE.recorded_clean_at_head_sha(state))

    def test_later_sweep_rejects_same_or_older_completed_sweep(self):
        with self.pipeline_run(fixes=0) as (args, commands, emitted):
            self.assertEqual(0, self.pipeline_cli(args))
            completed = MODULE.load_state(Path(args.state))
            for previous in (1, 2):
                with self.subTest(previous=previous):
                    del commands[1:]
                    state = copy.deepcopy(completed)
                    state["pipeline_budget"]["iteration"] = previous
                    MODULE.save_state(Path(args.state), state)
                    emitted.clear()
                    self.assertEqual(1, self.pipeline_cli(args))
                    self.assertEqual(1, len(commands))

    def test_later_sweep_rejects_unowned_or_unfinished_terminal_state(self):
        mutations = {
            "foreign run": lambda state: state["pipeline_budget"].update(run="other"),
            "changed cap": lambda state: state["pipeline_budget"].update(max_iterations=9),
            "wrong checkout": lambda state: state.update(repo_root="other"),
            "wrong PR": lambda state: state["pr"].update(pr_url="https://github.com/owner/repo/pull/8"),
            "changed head ref": lambda state: state["pr"].update(head_branch="other"),
            "changed head repo": lambda state: state["pr"].update(head_repository="fork/repo"),
            "changed base ref": lambda state: state["pr"].update(base_branch="other"),
            "policy changed": lambda state: state["agent_task"].update(github_mutation_policy="allow"),
            "model changed": lambda state: state["agent_task"].update(model="gpt-6-astra"),
            "active": lambda state: state["agent_task"].update(status="running"),
            "interrupted": lambda state: state["agent_task"].update(status="preparing"),
            "failed": lambda state: state["agent_task"].update(status="failed"),
            "active child": lambda state: state["agent_task"]["task"].update(state="running"),
        }
        with self.pipeline_run(fixes=0) as (args, commands, emitted):
            self.assertEqual(0, self.pipeline_cli(args))
            completed = MODULE.load_state(Path(args.state))
            args.pipeline_iteration = 2
            self.pipeline_live["head_sha"] = "9" * 40
            self.pipeline_identity["head"] = "9" * 40
            for label, mutate in mutations.items():
                with self.subTest(label=label):
                    state = copy.deepcopy(completed)
                    mutate(state)
                    MODULE.save_state(Path(args.state), state)
                    before = Path(args.state).read_bytes()
                    emitted.clear()
                    self.assertEqual(1, self.pipeline_cli(args))
                    self.assertEqual(before, Path(args.state).read_bytes())
                    self.assertEqual(1, len(commands))

    def test_later_sweep_rejects_state_changed_during_preflight(self):
        with self.pipeline_run(fixes=0) as (args, commands, emitted):
            self.assertEqual(0, self.pipeline_cli(args))
            args.pipeline_iteration = 2
            emitted.clear()
            fresh_preflight = MODULE.agent_task_preflight.side_effect

            def preflight(*values, **kwargs):
                result = fresh_preflight(*values, **kwargs)
                state = MODULE.load_state(Path(args.state))
                state["pipeline_budget"]["iteration"] = 3
                MODULE.save_state(Path(args.state), state)
                return result

            with mock.patch.object(MODULE, "agent_task_preflight", side_effect=preflight):
                self.assertEqual(1, self.pipeline_cli(args))
            self.assertEqual(1, len(commands))
            self.assertIn("state changed", emitted[-1]["error"])
            self.assertEqual(3, MODULE.load_state(Path(args.state))["pipeline_budget"]["iteration"])

    def test_pipeline_charges_five_separate_hosted_passes(self):
        with self.pipeline_run(fixes=4, max_iterations=5) as (args, commands, emitted):
            MODULE.command_pipeline(args)
            self.assertEqual(5, len(commands))
            self.assertEqual(1, len(emitted))
            self.assertEqual(5, emitted[0]["iterations"])
            self.assertEqual(4, len(emitted[0]["commits"]))
            self.assertEqual(5, len(emitted[0]["tasks"]))
            self.assertEqual("cleared", emitted[0]["stage_outcome"])

    def test_bounded_pipeline_publishes_code_without_outcome_then_reviews_again(self):
        with self.pipeline_run(fixes=1) as (args, commands, emitted):
            args.pipeline_run = "c" * 32
            args.bounded_step = True
            with mock.patch.dict(
                MODULE.os.environ, {"COPILOT_AGENT_SESSION_ID": "review-session"}
            ):
                MODULE.command_pipeline(args)
                first = MODULE.load_state(Path(args.state))
                self.assertEqual("waiting", emitted[-1]["result"])
                self.assertNotIn("stage_outcome", emitted[-1])
                self.assertEqual("continue", first["agent_task"]["outcome"])
                self.assertEqual("continue", first["review"]["outcome"])
                self.assertEqual(1, first["iterations"])
                self.assertEqual(1, len(commands))
                self.assertEqual(f"{6:040x}", first["pr"]["head_sha"])
                MODULE.command_pipeline(args)
                self.assertEqual("cleared", emitted[-1]["stage_outcome"])
                self.assertEqual(2, len(commands))
                self.assertEqual(first["pr"]["head_sha"], first["agent_task"]["published_head_sha"])
                self.assertEqual(1, len(MODULE.load_state(Path(args.state))["managed_task_history"]))
                self.assertEqual(2, MODULE.load_state(Path(args.state))["iterations"])

    def test_pipeline_rejects_nonzero_exit_even_with_success_result(self):
        with self.pipeline_run(exit_code=9) as (args, commands, emitted):
            with self.assertRaisesRegex(MODULE.WorkflowError, "exited 9"):
                MODULE.command_pipeline(args)
            self.assertEqual(1, len(commands))
            self.assertFalse(emitted)
            state = MODULE.load_state(Path(args.state))
            self.assertEqual("failed", state["agent_task"]["status"])
            self.assertEqual(0, state["iterations"])
            self.assertIsNone(MODULE.recorded_clean_at_head_sha(state))

    def test_pipeline_preserves_completed_task_rejected_by_runtime_head_guard(self):
        message = (
            "pull request #7 no longer matches the frozen source at "
            "post-completion validation; refusing to accept generated work"
        )
        with self.pipeline_run(
            fixes=0,
            exit_code=2,
            task_error={"code": "stale_pr_head", "message": message},
        ) as (args, commands, emitted):
            with (
                mock.patch.object(MODULE, "apply_verified_candidate_import") as apply,
                self.assertRaisesRegex(
                    MODULE.WorkflowError, "stale_pr_head.*post-completion validation"
                ),
            ):
                MODULE.command_pipeline(args)
            apply.assert_not_called()
            MODULE.verify_runtime_candidate.assert_not_called()
            MODULE.metadata_for.assert_not_called()
            self.assertEqual(1, len(commands))
            self.assertFalse(emitted)
            state_path = Path(args.state)
            state = MODULE.load_state(state_path)
            task = state["agent_task"]
            self.assertEqual("failed", task["status"])
            self.assertEqual("completed", task["task"]["state"])
            self.assertIsNotNone(task["task"]["id"])
            self.assertIsNotNone(task["generated"]["branch"])
            self.assertIsNone(task["candidate"])
            self.assertIsNone(task["completion"])
            self.assertNotIn("task_id_status", task)
            self.assertNotIn("published_head_sha", task)
            self.assertNotIn("confirmed_remote_head_sha", task)
            self.assertEqual(0, state["iterations"])
            self.assertTrue(all(value == 0 for value in state["budget_charges"].values()))
            self.assertIsNone(MODULE.recorded_clean_at_head_sha(state))
            self.assertIn(message, task["error"])
            self.assertTrue(Path(task["result_file"]).is_file())
            self.assertTrue(Path(task["prompt_file"]).is_file())
            before = state_path.read_bytes()
            with self.assertRaisesRegex(MODULE.WorkflowError, "unfinished audit"):
                MODULE.command_pipeline(args)
            self.assertEqual(before, state_path.read_bytes())
            self.assertEqual(1, len(commands))

    def test_pipeline_cli_returns_nonzero_for_execution_errors(self):
        argv = [
            str(SCRIPT), "pipeline", "owner/repo#7",
            "--state", str(self.directory / "pipeline.json"),
            "--pipeline-run", "pipeline-1",
            "--pipeline-iteration", "1",
            "--pipeline-max-iterations", "2",
            "--model", "sol",
        ]
        with (
            mock.patch.object(MODULE.sys, "argv", argv),
            mock.patch.object(
                MODULE, "command_agent_task",
                side_effect=MODULE.WorkflowError("managed helper exited 9"),
            ) as child,
            mock.patch.object(MODULE, "emit") as emit,
        ):
            self.assertEqual(1, MODULE.main())
        self.assertTrue(child.call_args.args[0]._pipeline)
        self.assertEqual("error", emit.call_args.args[0]["result"])

    def test_pipeline_rejects_running_result_without_import_or_next_pass(self):
        with self.pipeline_run(task_state="running") as (args, commands, emitted):
            with self.assertRaises(MODULE.WorkflowError):
                MODULE.command_pipeline(args)
            self.assertEqual(1, len(commands))
            self.assertFalse(emitted)
            state = MODULE.load_state(Path(args.state))
            self.assertEqual("failed", state["agent_task"]["status"])
            self.assertEqual(0, state["iterations"])

    def test_pipeline_cannot_adopt_a_failed_invocation(self):
        with self.pipeline_run(exit_code=9) as (args, commands, emitted):
            with self.assertRaises(MODULE.WorkflowError):
                MODULE.command_pipeline(args)
            before = Path(args.state).read_bytes()
            with self.assertRaisesRegex(MODULE.WorkflowError, "unfinished audit"):
                MODULE.command_pipeline(args)
            self.assertEqual(before, Path(args.state).read_bytes())
            self.assertEqual(1, len(commands))

    def test_detached_preflight_keeps_exact_head_and_branch_guards(self):
        for branch, head, status, allowed in (
            ("", self.head, "", True),
            ("", "9" * 40, "", False),
            ("wrong-branch", self.head, "", False),
            ("", self.head, " M file.py", False),
        ):
            with (
                self.subTest(branch=branch, head=head, status=status),
                mock.patch.object(MODULE, "metadata_for", return_value=self.preflight["pr"]),
                mock.patch.object(
                    MODULE, "local_identity",
                    return_value={"branch": branch, "head": head, "status": status},
                ),
                mock.patch.object(
                    MODULE, "gh_json",
                    side_effect=[
                        {"permissions": self.preflight["viewer"]["permissions"]},
                        {"login": "viewer"},
                    ],
                ),
                mock.patch.object(MODULE, "require_fork_head"),
                mock.patch.object(MODULE, "find_push_remote"),
                mock.patch.object(MODULE, "remote_head", return_value=self.head),
            ):
                if allowed:
                    context = MODULE.agent_task_preflight(
                        self.repo_root, {}, allow_detached=True
                    )
                    self.assertEqual("", context["identity"]["branch"])
                    with self.assertRaisesRegex(MODULE.WorkflowError, "branch mismatch"):
                        MODULE.agent_task_preflight(self.repo_root, {})
                else:
                    with self.assertRaises(MODULE.WorkflowError):
                        MODULE.agent_task_preflight(self.repo_root, {}, allow_detached=True)

    def test_preflight_rejects_actual_branch_movement_after_metadata(self):
        with (
            mock.patch.object(MODULE, "metadata_for", return_value=self.preflight["pr"]),
            mock.patch.object(
                MODULE, "local_identity",
                return_value={"branch": "", "head": self.head, "status": ""},
            ),
            mock.patch.object(
                MODULE, "gh_json",
                side_effect=[
                    {"permissions": self.preflight["viewer"]["permissions"]},
                    {"login": "viewer"},
                ],
            ),
            mock.patch.object(MODULE, "require_fork_head"),
            mock.patch.object(MODULE, "find_push_remote"),
            mock.patch.object(MODULE, "remote_head", return_value="9" * 40),
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "moved during preflight"):
                MODULE.agent_task_preflight(self.repo_root, {}, allow_detached=True)

    def test_candidate_import_fast_forwards_a_real_detached_checkout(self):
        def git(*arguments):
            return MODULE.git(self.repo_root, *arguments)

        git("init", "-q")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.com")
        git("remote", "add", "origin", "https://github.com/owner/repo.git")
        source = self.repo_root / "source.txt"
        source.write_text("source\n", encoding="utf-8")
        git("add", "source.txt")
        git("-c", "commit.gpgsign=false", "commit", "-qm", "source")
        source_head = git("rev-parse", "HEAD")
        source.write_text("fixed\n", encoding="utf-8")
        git("add", "source.txt")
        git("-c", "commit.gpgsign=false", "commit", "-qm", "fix")
        code_tip = git("rev-parse", "HEAD")
        git("checkout", "--detach", source_head)
        result_path = self.directory / "candidate.json"
        result_path.write_text("{}\n", encoding="utf-8")
        identity = MODULE.local_identity(self.repo_root)
        self.assertEqual("", identity["branch"])
        runtime_path = (
            Path(__file__).parents[2]
            / "agent-tasks-runtime"
            / "skills"
            / "agent-tasks-runtime"
            / "scripts"
            / "cloud_task.py"
        )
        runtime = MODULE.load_cloud_task_runtime(runtime_path)
        repository = MODULE.candidate_git_repository(runtime)
        repository.repository_name = mock.Mock(return_value="owner/repo")

        def guarded(_result, *, root, git, **_kwargs):
            snapshot = git.snapshot(root)
            self.assertIsNone(snapshot.branch)
            git.fast_forward(snapshot, code_tip)
            return {
                "application": "fast_forwarded",
                "final_local_head": code_tip,
            }

        runtime.guarded_fast_forward_candidate = mock.Mock(side_effect=guarded)
        preflight = copy.deepcopy(self.preflight)
        preflight["identity"] = identity
        preflight["pr"]["head_sha"] = source_head
        with (
            mock.patch.object(
                MODULE, "load_candidate_runtime", return_value=runtime
            ),
            mock.patch.object(
                MODULE, "candidate_git_repository", return_value=repository
            ),
            mock.patch.object(MODULE, "load_agent_task_result", return_value={}),
        ):
            self.assertTrue(
                MODULE.apply_verified_candidate_import(
                    self.repo_root,
                    helper=runtime_path,
                    requested_model="gpt-6-sol",
                    prompt="frozen prompt",
                    result_path=result_path,
                    result_sha256=MODULE.sha256_file(result_path),
                    preflight=preflight,
                    remote={"final_local_head": code_tip, "commits": [code_tip]},
                )
            )
        self.assertEqual(
            {"branch": "", "head": code_tip, "status": ""},
            MODULE.local_identity(self.repo_root),
        )
        self.assertEqual("fixed\n", source.read_text(encoding="utf-8"))







    def test_current_candidate_is_superseded_when_pr_already_advanced(self):
        state_path = self.directory / "push-recovery-state.json"
        helper = self.directory / "cloud_task.py"
        helper.write_text("# helper\n", encoding="utf-8")
        fix = "5" * 40
        result = self.candidate_result(
            commits=[fix],
            changed_paths=["src/app.py"],
        )
        live = {**self.preflight["pr"], "head_sha": fix}
        commands = []

        def helper_run(command, **kwargs):
            commands.append(command)
            result_path = Path(command[command.index("--result-file") + 1])
            result_path.write_text(json.dumps(result), encoding="utf-8")
            return MODULE.subprocess.CompletedProcess(command, 0, "ignored", "")

        arguments = SimpleNamespace(
            target="owner/repo#7",
            repo_root=str(self.repo_root),
            state=str(state_path),
            resume=False,
            model="sol",
            max_iterations=5,
            pipeline_run=None,
            pipeline_iteration=None,
            pipeline_max_iterations=None,
        )
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=self.repo_root),
            mock.patch.object(
                MODULE,
                "resolve_target",
                return_value=MODULE.parse_target("owner/repo#7"),
            ),
            mock.patch.object(
                MODULE, "agent_task_preflight", return_value=self.preflight
            ),
            mock.patch.object(MODULE, "discover_cloud_task", return_value=helper),
            mock.patch.object(MODULE, "run", side_effect=helper_run),
            mock.patch.object(MODULE, "git"),
            mock.patch.object(
                MODULE,
                "local_identity",
                return_value={
                    **self.preflight["identity"],
                    "head": fix,
                },
            ),
            mock.patch.object(
                MODULE,
                "verify_runtime_candidate",
                return_value=self.verified_candidate(result),
            ),
            mock.patch.object(
                MODULE,
                "apply_verified_candidate_import",
                return_value=False,
            ),
            mock.patch.object(MODULE, "metadata_for", return_value=live),
            mock.patch.object(
                MODULE, "same_ref_forward_head_drift", return_value=True
            ),
            mock.patch.object(MODULE, "remote_head", return_value=fix),
            mock.patch.object(MODULE, "wait_for_remote_head", return_value=fix),
            mock.patch.object(MODULE, "publish_shared_state"),
            mock.patch.object(MODULE, "emit") as emit,
            mock.patch.object(
                MODULE.secrets,
                "token_hex",
                side_effect=["invocation-1", "run-1"],
            ),
        ):
            MODULE.command_agent_task(arguments)

        self.assertEqual(len(commands), 1)
        self.assertNotIn("push", commands[0])
        state = MODULE.load_state(state_path)
        self.assertEqual("source_changed", emit.call_args.args[0]["result"])
        self.assertEqual("superseded", state["agent_task"]["status"])
        self.assertEqual(fix, state["agent_task"]["superseded_by_head_sha"])

    def test_candidate_snapshot_accepts_only_linear_base_advancement(self):
        advanced = {**self.preflight["pr"], "base_sha": "8" * 40}
        with mock.patch.object(
            MODULE, "live_base_contains", return_value=True
        ) as contains:
            self.assertTrue(
                MODULE.require_live_pr_snapshot(
                    self.preflight["pr"],
                    advanced,
                    expected_head=self.head,
                    allow_linear_base_advance=True,
                )
            )
        contains.assert_called_once_with(
            self.preflight["pr"]["repo_name"],
            self.preflight["pr"]["base_sha"],
            advanced["base_sha"],
        )

        with (
            mock.patch.object(MODULE, "live_base_contains", return_value=False),
            self.assertRaisesRegex(MODULE.WorkflowError, "drifted"),
        ):
            MODULE.require_live_pr_snapshot(
                self.preflight["pr"],
                advanced,
                expected_head=self.head,
                allow_linear_base_advance=True,
            )




class TargetParsingTest(unittest.TestCase):
    def test_accepts_urls_and_short_targets(self):
        self.assertEqual(
            MODULE.parse_target("https://github.com/owner/repo/pull/7"),
            {
                "owner": "owner",
                "repo": "repo",
                "number": 7,
                "repo_name": "owner/repo",
                "pr_url": "https://github.com/owner/repo/pull/7",
            },
        )
        self.assertEqual(
            MODULE.parse_target("owner/repo#7")["pr_url"],
            "https://github.com/owner/repo/pull/7",
        )
        self.assertEqual(
            MODULE.parse_target(
                "https://github.com/owner/repo/pull/7#discussion_r1"
            )["number"],
            7,
        )

    def test_rejects_unsupported_targets(self):
        for value in ("owner/repo", "https://github.com/owner/repo/issues/7", "7"):
            with self.assertRaises(MODULE.WorkflowError):
                MODULE.parse_target(value)

    def test_normalizes_git_bash_style_paths_on_windows(self):
        self.assertEqual(
            MODULE.normalize_cli_path("/c/Users/me/state.json", windows=True),
            "C:/Users/me/state.json",
        )
        self.assertEqual(
            MODULE.normalize_cli_path("/c/Users/me/state.json", windows=False),
            "/c/Users/me/state.json",
        )

    def test_recognizes_github_remotes(self):
        self.assertEqual(
            MODULE.github_repo_from_remote("git@github.com:fork/repo.git"), "fork/repo"
        )
        self.assertEqual(
            MODULE.github_repo_from_remote("https://github.com/fork/repo"), "fork/repo"
        )
        self.assertIsNone(MODULE.github_repo_from_remote("https://example.com/fork/repo"))


class SharedStateBackendTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name).resolve()
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

    def test_shared_state_is_off_by_default_without_config(self):
        with (
            mock.patch.dict(MODULE.os.environ, {}, clear=True),
            mock.patch.object(MODULE.Path, "home", return_value=self.home),
            mock.patch.object(MODULE, "run") as run,
        ):
            MODULE.publish_shared_state(
                {"repo_name": "owner/repo", "number": 7},
                section="self_review",
                field="clean_at_head_sha",
                value="head1",
                updated_at="2026-01-01T00:00:00Z",
            )

        run.assert_not_called()

    def test_environment_override_and_empty_force_off(self):
        config_path = self.home / MODULE.SHARED_STATE_CONFIG
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps({"repository": "config/state"}), encoding="utf-8"
        )
        with (
            mock.patch.object(MODULE.Path, "home", return_value=self.home),
            mock.patch.dict(
                MODULE.os.environ,
                {MODULE.SHARED_STATE_ENV: " env/state "},
                clear=True,
            ),
        ):
            self.assertEqual(MODULE.resolve_shared_state_repo(), "env/state")
        with (
            mock.patch.object(MODULE.Path, "home", return_value=self.home),
            mock.patch.dict(
                MODULE.os.environ, {MODULE.SHARED_STATE_ENV: "  "}, clear=True
            ),
        ):
            self.assertIsNone(MODULE.resolve_shared_state_repo())

    def test_resolves_config_and_warns_for_malformed_config(self):
        config_path = self.home / MODULE.SHARED_STATE_CONFIG
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps({"repository": "config/state"}), encoding="utf-8"
        )
        with (
            mock.patch.dict(MODULE.os.environ, {}, clear=True),
            mock.patch.object(MODULE.Path, "home", return_value=self.home),
        ):
            self.assertEqual(MODULE.resolve_shared_state_repo(), "config/state")

        config_path.write_text("{", encoding="utf-8")
        stderr = io.StringIO()
        with (
            mock.patch.dict(MODULE.os.environ, {}, clear=True),
            mock.patch.object(MODULE.Path, "home", return_value=self.home),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertIsNone(MODULE.resolve_shared_state_repo())
        self.assertIn("invalid config file", stderr.getvalue())

        config_path.write_text(
            json.dumps({"repository": "not-a-repository"}), encoding="utf-8"
        )
        stderr = io.StringIO()
        with (
            mock.patch.dict(MODULE.os.environ, {}, clear=True),
            mock.patch.object(MODULE.Path, "home", return_value=self.home),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertIsNone(MODULE.resolve_shared_state_repo())
        self.assertIn("expected owner/repo", stderr.getvalue())

    def test_publish_preserves_other_entries_and_owned_sections(self):
        document = {
            "version": 1,
            "repository": "owner/repo",
            "pull_requests": {
                "7": {
                    "description": {
                        "validated_head_sha": "other",
                        "updated_at": "2026-01-01T00:00:00Z",
                    }
                },
                "8": {"custom": {"kept": True}},
            },
        }
        with (
            mock.patch.object(
                MODULE, "resolve_shared_state_repo", return_value="state/repo"
            ),
            mock.patch.object(
                MODULE, "run", side_effect=[self.response_for(document), self.process()]
            ) as run,
        ):
            MODULE.publish_shared_state(
                {"repo_name": "owner/repo", "number": 7},
                section="self_review",
                field="clean_at_head_sha",
                value="head1",
                updated_at="2026-01-02T00:00:00Z",
            )

        payload = json.loads(run.call_args_list[1].kwargs["input_text"])
        self.assertEqual(payload["message"], "Update PR Flight state")
        published = json.loads(
            MODULE.base64.b64decode(payload["content"]).decode("utf-8")
        )
        self.assertEqual(published["pull_requests"]["8"], {"custom": {"kept": True}})
        self.assertEqual(
            published["pull_requests"]["7"]["description"],
            document["pull_requests"]["7"]["description"],
        )
        self.assertEqual(
            published["pull_requests"]["7"]["self_review"],
            {
                "clean_at_head_sha": "head1",
                "updated_at": "2026-01-02T00:00:00Z",
            },
        )

    def test_skips_write_when_merged_document_is_unchanged(self):
        document = {
            "version": 1,
            "repository": "owner/repo",
            "pull_requests": {
                "7": {
                    "self_review": {
                        "clean_at_head_sha": "head1",
                        "updated_at": "2026-01-02T00:00:00Z",
                    }
                }
            },
        }
        with (
            mock.patch.object(
                MODULE, "resolve_shared_state_repo", return_value="state/repo"
            ),
            mock.patch.object(
                MODULE, "run", return_value=self.response_for(document)
            ) as run,
        ):
            MODULE.publish_shared_state(
                {"repo_name": "owner/repo", "number": 7},
                section="self_review",
                field="clean_at_head_sha",
                value="head1",
                updated_at="2026-01-02T00:00:00Z",
            )

        run.assert_called_once()

    def test_does_not_replace_a_newer_shared_fact(self):
        document = {
            "version": 1,
            "repository": "owner/repo",
            "pull_requests": {
                "7": {
                    "self_review": {
                        "clean_at_head_sha": None,
                        "updated_at": "2026-01-03T00:00:00Z",
                    }
                }
            },
        }
        with (
            mock.patch.object(
                MODULE, "resolve_shared_state_repo", return_value="state/repo"
            ),
            mock.patch.object(
                MODULE, "run", return_value=self.response_for(document)
            ) as run,
        ):
            MODULE.publish_shared_state(
                {"repo_name": "owner/repo", "number": 7},
                section="self_review",
                field="clean_at_head_sha",
                value="older-head",
                updated_at="2026-01-02T00:00:00Z",
            )

        run.assert_called_once()


class PullRequestMetadataTest(unittest.TestCase):
    def test_includes_githubs_ordered_pr_commit_list(self):
        target = MODULE.parse_target("https://github.com/owner/repo/pull/7")
        payload = {
            "number": 7,
            "title": "Add a thing",
            "body": "This changes the thing.",
            "url": "https://github.com/owner/repo/pull/7",
            "state": "OPEN",
            "isDraft": False,
            "headRefName": "feature",
            "headRefOid": "head",
            "headRepositoryOwner": {"login": "fork"},
            "headRepository": {"name": "repo"},
            "baseRefName": "main",
            "baseRefOid": "frozen",
            "commits": [
                {"oid": "one", "messageHeadline": "First change"},
                {"oid": "two", "messageHeadline": "Second change"},
            ],
        }

        with mock.patch.object(
            MODULE, "gh_json", return_value=payload
        ) as gh_json, mock.patch.object(
            MODULE, "base_ref_tip", return_value="live-tip"
        ), mock.patch.object(
            MODULE, "remote_head", return_value="actual-tip"
        ):
            metadata = MODULE.metadata_for(target)

        self.assertEqual("actual-tip", metadata["head_sha"])
        self.assertNotIn("commits", metadata)
        self.assertEqual(metadata["body"], "This changes the thing.")
        self.assertEqual(metadata["state"], "OPEN")
        self.assertFalse(metadata["is_draft"])
        self.assertIn("body", gh_json.call_args.args[0][-1])
        self.assertNotIn("commits", gh_json.call_args.args[0][-1])

    def test_base_sha_is_the_live_base_branch_tip_not_the_frozen_base_ref_oid(self):
        target = MODULE.parse_target("https://github.com/owner/repo/pull/7")
        payload = {
            "number": 7,
            "title": "Add a thing",
            "body": "",
            "url": "https://github.com/owner/repo/pull/7",
            "state": "OPEN",
            "isDraft": False,
            "headRefName": "feature",
            "headRefOid": "head",
            "headRepositoryOwner": {"login": "fork"},
            "headRepository": {"name": "repo"},
            "baseRefName": "main",
            "baseRefOid": "frozen",
            "commits": [{"oid": "one", "messageHeadline": "First change"}],
        }
        with mock.patch.object(
            MODULE, "gh_json", return_value=payload
        ), mock.patch.object(
            MODULE, "base_ref_tip", return_value="live-tip"
        ) as tip, mock.patch.object(
            MODULE, "remote_head", return_value="actual-tip"
        ):
            metadata = MODULE.metadata_for(target)
        self.assertEqual("live-tip", metadata["base_sha"])
        tip.assert_called_once_with("owner/repo", "main")


class RepositoryContextTest(unittest.TestCase):
    def test_groups_authored_paths_by_nearby_guidance_and_manifests(self):
        context = MODULE.repository_context_from_files(
            [
                "AGENTS.md",
                "CONTRIBUTING.md",
                ".github/copilot-instructions.md",
                ".github/instructions/python.instructions.md",
                ".github/agents/knowledge/gradle.md",
                ".github/workflows/ci.yml",
                "package.json",
                "src/AGENTS.md",
                "src/service/CONTEXT.md",
                "src/service/pyproject.toml",
                "src/service/app.py",
                "web/package.json",
                "web/app.js",
                "ignored/build.gradle",
            ],
            ["src/service/app.py", "web/app.js"],
        )

        self.assertEqual(context["scope"], "pr_authored_files")
        self.assertEqual(context["knowledge_files"], [".github/agents/knowledge/gradle.md"])
        self.assertEqual(
            context["validation_sources"]["workflows"], [".github/workflows/ci.yml"]
        )
        groups = {tuple(group["paths"]): group for group in context["path_groups"]}
        service = groups[("src/service/app.py",)]
        self.assertIn("AGENTS.md", service["instruction_files"])
        self.assertIn("src/AGENTS.md", service["instruction_files"])
        self.assertIn("src/service/CONTEXT.md", service["instruction_files"])
        self.assertEqual(
            service["validation_sources"],
            ["package.json", "src/service/pyproject.toml"],
        )
        web = groups[("web/app.js",)]
        self.assertEqual(web["validation_sources"], ["package.json", "web/package.json"])
        self.assertNotIn("ignored/build.gradle", service["validation_sources"])

    def test_groups_paths_with_the_same_context(self):
        context = MODULE.repository_context_from_files(
            ["AGENTS.md", "src/pyproject.toml"],
            ["src/a.py", "src/b.py"],
        )

        self.assertEqual(
            context["path_groups"],
            [
                {
                    "paths": ["src/a.py", "src/b.py"],
                    "instruction_files": ["AGENTS.md"],
                    "validation_sources": ["src/pyproject.toml"],
                }
            ],
        )

    def test_keeps_a_literal_backslash_in_a_git_path(self):
        context = MODULE.repository_context_from_files(
            ["AGENTS.md", "src/AGENTS.md", r"src\app.py"],
            [r"src\app.py"],
        )

        self.assertEqual(context["path_groups"][0]["paths"], [r"src\app.py"])
        self.assertEqual(
            context["path_groups"][0]["instruction_files"], ["AGENTS.md"]
        )

    def test_reads_nul_delimited_paths_without_newline_translation(self):
        response = SimpleNamespace(
            returncode=0,
            stdout=b"line\r\nbreak\0carriage\ronly\0",
            stderr=b"",
        )
        with mock.patch.object(MODULE, "run_bytes", return_value=response):
            paths = MODULE.git_z_paths(Path("repo"), "ls-files")

        self.assertEqual(paths, ["line\r\nbreak", "carriage\ronly"])

    def test_patch_identity_ignores_hunk_offsets_but_preserves_whitespace(self):
        original = (
            b"diff --git a/app.py b/app.py\n"
            b"index 111..222 100644\n"
            b"--- a/app.py\n"
            b"+++ b/app.py\n"
            b"@@ -2,2 +2,3 @@\n"
            b" if ready:\n"
            b"+    run()\n"
        )
        rebased = original.replace(
            b"index 111..222 100644", b"index 333..444 100644"
        ).replace(b"@@ -2,2 +2,3 @@", b"@@ -20,2 +20,3 @@")
        changed_indentation = rebased.replace(b"+    run()", b"+run()")

        self.assertEqual(
            MODULE.patch_identity(original), MODULE.patch_identity(rebased)
        )
        self.assertNotEqual(
            MODULE.patch_identity(original),
            MODULE.patch_identity(changed_indentation),
        )
        carriage_return_one = original.replace(b"+    run()", b"+x\rindex first")
        carriage_return_two = original.replace(b"+    run()", b"+x\rindex second")
        self.assertNotEqual(
            MODULE.patch_identity(carriage_return_one),
            MODULE.patch_identity(carriage_return_two),
        )
        self.assertIsNone(MODULE.patch_identity(b""))


class PatchRetentionTest(unittest.TestCase):
    def response(self, returncode, stdout=b"", stderr=b""):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_proves_present_absent_and_unknown_patch_states(self):
        patch = b"diff --git a/app.py b/app.py\n"
        cases = [
            (
                [
                    self.response(0, patch),
                    self.response(0),
                    self.response(1),
                ],
                True,
            ),
            (
                [
                    self.response(0, patch),
                    self.response(1),
                    self.response(0),
                ],
                False,
            ),
            (
                [
                    self.response(0, patch),
                    self.response(1),
                    self.response(1),
                ],
                None,
            ),
            (
                [
                    self.response(0, patch),
                    self.response(0),
                    self.response(0),
                ],
                None,
            ),
            ([self.response(1, stderr=b"missing")], None),
        ]
        for responses, expected in cases:
            with self.subTest(expected=expected), mock.patch.object(
                MODULE, "run_bytes", side_effect=responses
            ):
                self.assertIs(
                    MODULE.commit_patch_retention(Path("repo"), "commit"), expected
                )


class BaseRefTipTest(unittest.TestCase):
    def test_returns_the_live_tip_from_the_branch_ref(self):
        response = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"object": {"sha": "live-tip", "type": "commit"}}),
            stderr="",
        )
        with mock.patch.object(MODULE, "run", return_value=response) as run:
            self.assertEqual("live-tip", MODULE.base_ref_tip("owner/repo", "main"))
        self.assertEqual(
            ["gh", "api", "repos/owner/repo/git/ref/heads/main"],
            run.call_args.args[0],
        )

    def test_a_deleted_base_branch_raises_rather_than_falling_back(self):
        response = SimpleNamespace(
            returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)"
        )
        with mock.patch.object(MODULE, "run", return_value=response):
            with self.assertRaisesRegex(MODULE.WorkflowError, "may have been deleted"):
                MODULE.base_ref_tip("owner/repo", "gone")


class DiffAnchorTest(unittest.TestCase):
    def test_forward_base_fetch_preserves_frozen_review_diff(self):
        base, advanced, head = "1" * 40, "3" * 40, "2" * 40
        pr = {
            "repo_name": "owner/repo", "base_branch": "main", "base_sha": base,
            "head_owner": "fork", "head_repo": "repo",
            "head_branch": "feature", "head_sha": head,
        }
        with mock.patch.object(
            MODULE, "git", side_effect=["C:\\repo", "", advanced, "", head, DIFF]
        ) as git, mock.patch.object(
            MODULE, "run", return_value=SimpleNamespace(returncode=0)
        ) as run:
            self.assertEqual(DIFF, MODULE.fetch_authoritative_diff(pr))
        self.assertIn(
            mock.call(
                ["git", "-C", "C:\\repo", "merge-base", "--is-ancestor", base, advanced],
                check=False,
            ),
            run.call_args_list,
        )
        self.assertIn(f"{base}...{head}", git.call_args.args)

    def test_parses_changed_lines_per_side(self):
        anchors = MODULE.parse_unified_diff(DIFF)

        self.assertEqual(sorted(anchors), ["app.py"])
        self.assertEqual(anchors["app.py"]["LEFT"], {2})
        self.assertEqual(anchors["app.py"]["RIGHT"], {2, 3})

    def test_serializes_anchors_as_sorted_lists(self):
        self.assertEqual(
            MODULE.serialize_anchors(MODULE.parse_unified_diff(DIFF)),
            {"app.py": {"LEFT": [2], "RIGHT": [2, 3]}},
        )


class CandidateValidationTest(unittest.TestCase):
    def setUp(self):
        self.anchors = {"app.py": {"LEFT": [2], "RIGHT": [2, 3]}}

    def test_accepts_candidates_anchored_to_changed_lines(self):
        self.assertEqual(
            MODULE.validate_candidates(
                [{"path": "app.py", "line": 3, "side": "RIGHT", "body": " Fix it. "}],
                self.anchors,
            ),
            [{"path": "app.py", "line": 3, "side": "RIGHT", "body": "Fix it."}],
        )

    def test_rejects_invalid_candidates(self):
        cases = [
            [],
            [{"path": "app.py", "line": 9, "side": "RIGHT", "body": "Fix it."}],
            [{"path": "app.py", "line": 3, "side": "LEFT", "body": "Fix it."}],
            [{"path": "other.py", "line": 3, "side": "RIGHT", "body": "Fix it."}],
            [{"path": "app.py", "line": 3, "side": "MIDDLE", "body": "Fix it."}],
            [{"path": "app.py", "line": 3, "side": "RIGHT", "body": "  "}],
            [{"path": "app.py", "line": 0, "side": "RIGHT", "body": "Fix it."}],
            [{"path": "app.py", "line": True, "side": "RIGHT", "body": "Fix it."}],
            [
                {
                    "path": "app.py",
                    "line": 3,
                    "side": "RIGHT",
                    "body": "Fix it.",
                    "extra": 1,
                }
            ],
            [{"path": "app.py", "line": 3, "side": "RIGHT"}],
            ["not an object"],
        ]
        for candidates in cases:
            with self.subTest(candidates=candidates):
                with self.assertRaises(MODULE.WorkflowError):
                    MODULE.validate_candidates(candidates, self.anchors)

    def test_invalid_anchor_reports_nearest_and_accepted_lines(self):
        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.validate_candidates(
                [{"path": "app.py", "line": 4, "side": "RIGHT", "body": "Fix it."}],
                self.anchors,
            )

        self.assertIn("nearest valid RIGHT line: 3", str(error.exception))
        self.assertIn("accepted RIGHT lines: 2, 3", str(error.exception))

    def test_invalid_side_reports_the_other_sides_accepted_lines(self):
        anchors = {"app.py": {"LEFT": [7, 8], "RIGHT": []}}

        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.validate_candidates(
                [{"path": "app.py", "line": 8, "side": "RIGHT", "body": "Fix it."}],
                anchors,
            )

        self.assertIn("app.py has no changed RIGHT lines", str(error.exception))
        self.assertIn("accepted LEFT lines: 7, 8", str(error.exception))

    def test_invalid_anchor_path_reports_changed_paths(self):
        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.validate_candidates(
                [{"path": "other.py", "line": 3, "side": "RIGHT", "body": "Fix it."}],
                self.anchors,
            )

        self.assertIn("anchor path is not in the pinned diff: other.py", str(error.exception))
        self.assertIn("changed paths: app.py", str(error.exception))

    def test_candidate_key_error_names_unexpected_and_missing_keys(self):
        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.validate_candidates(
                [
                    {
                        "id": 1,
                        "title": "Fix it",
                        "path": "app.py",
                        "line": 3,
                        "side": "RIGHT",
                    }
                ],
                self.anchors,
            )

        self.assertIn("unexpected keys: id, title", str(error.exception))
        self.assertIn("missing keys: body", str(error.exception))
        self.assertIn(
            "expected exactly: path, line, side, body", str(error.exception)
        )


class HistoryTest(unittest.TestCase):
    def test_maps_candidate_status_to_history_outcome(self):
        self.assertEqual(
            MODULE.history_outcome({"status": "handled", "commit": "abc"}), "addressed"
        )
        self.assertEqual(
            MODULE.history_outcome({"status": "handled", "commit": None}), "no_code"
        )
        self.assertEqual(MODULE.history_outcome({"status": "dropped"}), "dropped")
        self.assertEqual(MODULE.history_outcome({"status": "skipped"}), "skipped")
        self.assertEqual(MODULE.history_outcome({"status": "pending"}), "unresolved")

    def test_archives_only_resolved_candidates(self):
        state = {
            "history": [],
            "review": {
                "iteration": 1,
                "candidates": [
                    {
                        "id": 1,
                        "path": "app.py",
                        "line": 3,
                        "side": "RIGHT",
                        "body": "Fix it.",
                        "status": "handled",
                        "commit": "abc",
                        "summary": "fix",
                    },
                    {
                        "id": 2,
                        "path": "app.py",
                        "line": 2,
                        "side": "RIGHT",
                        "body": "Speculative.",
                        "status": "dropped",
                        "rationale": "not demonstrated",
                    },
                    {
                        "id": 3,
                        "path": "app.py",
                        "line": 2,
                        "side": "LEFT",
                        "body": "Never reached.",
                        "status": "pending",
                    },
                    {
                        "id": 4,
                        "path": "app.py",
                        "line": 3,
                        "side": "RIGHT",
                        "body": "Validation failed.",
                        "status": "skipped",
                        "rationale": "tests fail",
                    },
                ],
            },
        }

        MODULE.archive_review(state)
        MODULE.archive_review(state)

        self.assertEqual([entry["id"] for entry in state["history"]], [1, 2])
        self.assertEqual(state["history"][0]["outcome"], "addressed")
        self.assertEqual(state["history"][0]["commit"], "abc")
        self.assertEqual(state["history"][1]["outcome"], "dropped")
        self.assertEqual(state["history"][1]["detail"], "not demonstrated")

    def test_compares_only_recorded_history_commits_with_current_pr_commits(self):
        history = [
            {"id": 1, "commit": "old", "patch_id": "same-patch"},
            {"id": 2, "commit": "current"},
            {"id": 3, "commit": "unknown"},
            {"id": 4, "commit": "gone", "patch_id": "gone-patch"},
            {"id": 5, "commit": None},
            {"id": 6},
        ]

        self.assertEqual(
            MODULE.compare_history_commits(
                history,
                [
                    {"sha": "current", "patch_id": None},
                    {"sha": "other", "patch_id": "same-patch"},
                ],
                {
                    "old": True,
                    "current": True,
                    "unknown": None,
                    "gone": False,
                },
            ),
            [
                {
                    "history_id": 1,
                    "commit": "old",
                    "patch_id": "same-patch",
                    "in_pr_commits": False,
                    "retained": True,
                    "match_kind": "equivalent_patch",
                    "commit_match_kind": "equivalent_patch",
                    "matching_commit": "other",
                },
                {
                    "history_id": 2,
                    "commit": "current",
                    "patch_id": None,
                    "in_pr_commits": True,
                    "retained": True,
                    "match_kind": "exact_commit",
                    "commit_match_kind": "exact_commit",
                    "matching_commit": "current",
                },
                {
                    "history_id": 3,
                    "commit": "unknown",
                    "patch_id": None,
                    "in_pr_commits": False,
                    "retained": None,
                    "match_kind": "unknown",
                    "commit_match_kind": "unknown",
                    "matching_commit": None,
                },
                {
                    "history_id": 4,
                    "commit": "gone",
                    "patch_id": "gone-patch",
                    "in_pr_commits": False,
                    "retained": False,
                    "match_kind": "missing",
                    "commit_match_kind": "missing",
                    "matching_commit": None,
                },
            ],
        )


class HeadVerificationTest(unittest.TestCase):
    def test_requires_the_local_head_to_equal_the_pr_head(self):
        MODULE.require_checkout_head("abc", "abc")

        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.require_checkout_head("abc", "def")

        self.assertIn("HEAD mismatch", str(error.exception))

    def test_refuses_to_push_a_missing_upstream_head_branch(self):
        pr = {
            "upstream_owner": "owner",
            "upstream_repo": "repo",
            "head_owner": "owner",
            "head_repo": "repo",
            "head_branch": "feature",
        }

        with mock.patch.object(MODULE, "remote_head", return_value=None):
            with self.assertRaises(MODULE.WorkflowError):
                MODULE.require_fork_head(pr)

        with mock.patch.object(MODULE, "remote_head", return_value="abc"):
            MODULE.require_fork_head(pr)

    def test_allows_a_fork_head_without_checking_the_remote(self):
        pr = {
            "upstream_owner": "owner",
            "upstream_repo": "repo",
            "head_owner": "fork",
            "head_repo": "repo",
            "head_branch": "feature",
        }

        with mock.patch.object(MODULE, "remote_head") as remote_head:
            MODULE.require_fork_head(pr)

        remote_head.assert_not_called()

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
            mock.patch.object(
                MODULE, "remote_head", return_value="old-head"
            ) as remote_head,
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

class CommitProvenanceTest(unittest.TestCase):
    def test_returns_each_pr_commit_with_its_sorted_unique_file_set(self):
        commits = [
            {"sha": "one", "message": "First"},
            {"sha": "two", "message": "Second"},
        ]

        with mock.patch.object(
            MODULE,
            "git_z_paths",
            side_effect=[
                ["z.py", "a.py", "z.py"],
                ["docs/readme.md"],
            ],
        ) as git_z_paths:
            result = MODULE.commit_provenance(Path("repo"), commits)

        self.assertEqual(
            result,
            [
                {"sha": "one", "message": "First", "files": ["a.py", "z.py"]},
                {
                    "sha": "two",
                    "message": "Second",
                    "files": ["docs/readme.md"],
                },
            ],
        )
        self.assertEqual(
            git_z_paths.call_args_list,
            [
                mock.call(
                    Path("repo"),
                    "diff-tree",
                    "--root",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "-m",
                    "one",
                ),
                mock.call(
                    Path("repo"),
                    "diff-tree",
                    "--root",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "-m",
                    "two",
                ),
            ],
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

    def test_reports_the_state_attached_to_an_explicit_path(self):
        path = write_state(self.directory, iterations=2)

        MODULE.command_status(
            SimpleNamespace(state=str(path), current=False, repo_root=None)
        )

        envelope = self.emitted[-1]
        self.assertEqual(envelope["result"], "ready")
        self.assertEqual(envelope["pr"]["number"], 7)
        self.assertEqual(envelope["iterations"], 2)

    def test_status_reports_when_the_helper_last_wrote_its_state(self):
        """The only signal a reader has for telling working from wedged.

        Every write stamps it, so a stamp minutes old and a stamp an hour old
        are different answers to the question a person actually asks.
        """
        path = write_state(self.directory, updated_at="2026-02-03T04:05:06Z")

        MODULE.command_status(
            SimpleNamespace(state=str(path), current=False, repo_root=None)
        )

        envelope = self.emitted[-1]
        self.assertEqual("2026-02-03T04:05:06Z", envelope["last_helper_activity"])
        snapshot = json.loads(
            Path(envelope["status_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual("2026-02-03T04:05:06Z", snapshot["last_helper_activity"])

    def test_writes_the_complete_state_snapshot_and_emits_a_compact_envelope(self):
        review = {
            "id": "pr-7-iteration-1",
            "status": "active",
            "iteration": 1,
            "head_sha": "head1",
            "diff_path": str(self.directory / "state.json.diff"),
            "anchors": {"app.py": {"LEFT": [2], "RIGHT": [2, 3]}},
            "pr_commits": [{"sha": "commit1", "message": "Change app", "files": []}],
            "diff_only_files": [],
            "candidates": [
                {"id": 1, "status": "pending", "path": "app.py", "body": "x" * 4096},
                {"id": 2, "status": "dropped", "path": "app.py", "body": "y" * 4096},
                {"id": 3, "status": "dropped", "path": "app.py", "body": "z" * 4096},
            ],
            "batches": [{"id": "batch-1", "status": "planned"}],
        }
        path = write_state(
            self.directory,
            iterations=1,
            review=review,
            history=[{"id": 9, "body": "w" * 4096}],
        )

        MODULE.command_status(
            SimpleNamespace(state=str(path), current=False, repo_root=None)
        )

        envelope = self.emitted[-1]
        status_path = MODULE.status_path_for(path)
        self.assertEqual(Path(envelope["status_path"]), status_path)
        self.assertNotIn("history", envelope)
        self.assertEqual(
            set(envelope["pr"]),
            {"number", "title", "pr_url", "repo_name", "head_branch", "base_branch"},
        )
        self.assertEqual(
            envelope["review"]["candidate_statuses"], {"pending": 1, "dropped": 2}
        )
        self.assertEqual(envelope["review"]["batch_statuses"], {"planned": 1})
        self.assertNotIn("anchors", envelope["review"])
        self.assertNotIn("candidates", envelope["review"])
        self.assertEqual(
            envelope["counts"],
            {
                "batches": 1,
                "candidates": 3,
                "changed_files": 1,
                "diff_only_files": 0,
                "history": 1,
                "pr_commits": 1,
            },
        )
        self.assertLess(len(json.dumps(envelope)), 2048)

        result = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(result["result"], "ready")
        self.assertEqual(result["review"], review)
        self.assertEqual(result["history"], [{"id": 9, "body": "w" * 4096}])
        self.assertEqual(result["iterations"], 1)

    def test_reports_no_state_for_the_current_branch_pr(self):
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
        self.assertEqual(result["pr"]["number"], 7)
        self.assertIsNone(result["review"])
        self.assertNotIn("status_path", result)
        self.assertFalse(
            MODULE.status_path_for(self.directory / "missing.json").exists()
        )

    def test_fresh_invocations_never_select_the_legacy_pr_state(self):
        target = MODULE.parse_target("owner/repo#20075")
        args = SimpleNamespace(
            pipeline_run=None,
            invocation_run=None,
            new_invocation=False,
            state=None,
        )
        with mock.patch.object(
            MODULE.uuid,
            "uuid4",
            side_effect=[
                SimpleNamespace(hex="fresh-1"),
                SimpleNamespace(hex="fresh-2"),
            ],
        ):
            first_run = MODULE.invocation_run(args)
            second_run = MODULE.invocation_run(args)
        first = MODULE.invocation_state_path(target, args, first_run)
        second = MODULE.invocation_state_path(target, args, second_run)

        self.assertNotEqual(MODULE.default_state_path(target), first)
        self.assertNotEqual(first, second)

    def test_cleanup_removes_the_state_file(self):
        path = write_state(self.directory)
        diff_path = MODULE.diff_path_for(path)
        diff_path.write_text(DIFF, encoding="utf-8")
        preflight_path = MODULE.preflight_path_for(path)
        preflight_path.write_text("{}", encoding="utf-8")
        status_path = MODULE.status_path_for(path)
        status_path.write_text("{}", encoding="utf-8")

        MODULE.command_cleanup(SimpleNamespace(state=str(path)))

        self.assertFalse(path.exists())
        self.assertFalse(diff_path.exists())
        self.assertFalse(preflight_path.exists())
        self.assertFalse(status_path.exists())
        self.assertEqual(self.emitted[-1]["result"], "cleaned_up")

    def test_cleanup_tolerates_a_missing_diff_snapshot(self):
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

    def review(self, **overrides):
        review = {
            "id": "pr-7-iteration-1",
            "status": "active",
            "iteration": 1,
            "head_sha": "head1",
            "diff_path": str(self.directory / "state.json.diff"),
            "anchors": {"app.py": {"LEFT": [2], "RIGHT": [2, 3]}},
            "candidates": [],
            "batches": [],
        }
        review.update(overrides)
        return review

    def status(self, **overrides):
        path = write_state(self.directory, **overrides)
        MODULE.command_status(
            SimpleNamespace(state=str(path), current=False, repo_root=None)
        )
        envelope = self.emitted[-1]
        result = json.loads(
            MODULE.status_path_for(path).read_text(encoding="utf-8")
        )
        return envelope, result

    def clean_review(self):
        return self.review(outcome="clean", clean_at_head_sha="head1")

    def marker_of(self, state):
        """Read the clean-at-head marker the way an orchestrator reads it.

        This deliberately repeats the rule rather than calling the helper, so a
        change that lets `stage_outcome` claim `cleared` on its own still fails.
        """

        review = state.get("review")
        if not isinstance(review, dict) or review.get("outcome") != "clean":
            return None
        value = review.get("clean_at_head_sha")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def test_a_resolved_clean_review_cleared(self):
        envelope, result = self.status(review=self.clean_review())

        self.assertEqual(envelope["result"], "ready")
        self.assertEqual(envelope["stage_outcome"], "cleared")
        self.assertEqual(result["result"], "ready")
        self.assertEqual(result["stage_outcome"], "cleared")

    def test_a_batch_validation_blocked_reports_no_outcome(self):
        review = self.review(
            candidates=[{"id": 1, "status": "skipped", "path": "app.py"}],
            batches=[{"id": "batch-1", "status": "skipped"}],
        )

        envelope, result = self.status(review=review)

        self.assertNotIn("stage_outcome", envelope)
        self.assertNotIn("stage_outcome", result)

    def test_the_iteration_cap_reports_no_outcome(self):
        envelope, result = self.status(iterations=MODULE.DEFAULT_MAX_ITERATIONS)

        self.assertNotIn("stage_outcome", envelope)
        self.assertNotIn("stage_outcome", result)

    def test_an_unfinished_review_reports_no_outcome(self):
        pending, _ = self.status(
            review=self.review(
                candidates=[{"id": 1, "status": "pending", "path": "app.py"}]
            )
        )
        published, _ = self.status(
            review=self.review(status="published", published_head_sha="head2"),
            iterations=1,
        )

        self.assertNotIn("stage_outcome", pending)
        self.assertNotIn("stage_outcome", published)

    def test_the_outcome_can_say_that_it_has_no_answer(self):
        """A return type with no absence value has to invent an ending."""

        annotation = inspect.signature(MODULE.stage_outcome).return_annotation

        self.assertEqual(str(annotation).replace("'", ""), "str | None")
        self.assertIsNone(MODULE.stage_outcome({}))

    def test_a_state_that_holds_no_run_reports_no_outcome(self):
        target = MODULE.parse_target("owner/repo#7")

        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=self.directory),
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

    def test_cleared_never_outruns_the_recorded_clean_head(self):
        states = [
            {"review": self.review()},
            {"review": self.review(outcome="clean")},
            {"review": self.review(outcome="clean", clean_at_head_sha=None)},
            {"review": self.review(outcome="clean", clean_at_head_sha="   ")},
            {"review": self.review(clean_at_head_sha="head1")},
            {"review": self.review(status="published", clean_at_head_sha="head1")},
            {"review": self.review(outcome="dirty", clean_at_head_sha="head1")},
            {"review": None},
            {
                "review": self.review(
                    candidates=[{"id": 1, "status": "skipped", "path": "app.py"}]
                )
            },
            {"iterations": MODULE.DEFAULT_MAX_ITERATIONS},
            {"review": self.clean_review()},
            {"review": self.clean_review(), "iterations": 4},
        ]

        for overrides in states:
            with self.subTest(overrides=overrides):
                envelope, result = self.status(**overrides)
                state = MODULE.load_state(self.directory / "state.json")
                marker = self.marker_of(state)
                cleared = marker is not None
                self.assertEqual(envelope.get("stage_outcome") == "cleared", cleared)
                self.assertEqual(result.get("stage_outcome") == "cleared", cleared)
                if cleared:
                    self.assertEqual(envelope["review"]["clean_at_head_sha"], marker)
                    self.assertEqual(result["review"]["clean_at_head_sha"], marker)
                else:
                    self.assertNotIn("stage_outcome", envelope)
                    self.assertNotIn("stage_outcome", result)



class PipelineBudgetTest(unittest.TestCase):
    """A stage budget belongs to an outer loop's iteration, not to a launch."""

    RECORDED = {"run": "run-a", "iteration": 2, "baseline": 3, "run_baseline": 1}

    def scope(self, state, **pipeline):
        return MODULE.pipeline_scope(state, SimpleNamespace(**pipeline))

    def test_migration_seals_a_paused_pipeline_budget_before_standalone_work(self):
        state = {
            "iterations": 7,
            "pipeline_budget": {
                "run": "run-a",
                "iteration": 2,
                "baseline": 5,
                "run_baseline": 5,
            },
        }
        state["budget_scope"] = "lifetime"
        MODULE.charge_iteration(state)
        scope = MODULE.scoped_budget(
            state,
            "pipeline",
            self.scope(state, pipeline_run="run-a", pipeline_iteration=2),
        )

        self.assertEqual((2, 2), MODULE.budget_spent(state, scope))

    def test_direct_legacy_pipeline_publish_is_charged_after_migration(self):
        state = {
            "iterations": 7,
            "pipeline_budget": {
                "run": "run-a",
                "iteration": 2,
                "baseline": 5,
                "run_baseline": 5,
            },
        }

        MODULE.charge_iteration(state)
        scope = MODULE.scoped_budget(
            state,
            "pipeline",
            self.scope(state, pipeline_run="run-a", pipeline_iteration=2),
        )

        self.assertEqual((3, 3), MODULE.budget_spent(state, scope))

    def test_a_standalone_invocation_is_left_exactly_as_it_was(self):
        """Absent, empty, and unusable run tokens must never read as a new run."""
        for pipeline in (
            {},
            {"pipeline_run": None, "pipeline_iteration": None},
            {"pipeline_run": "", "pipeline_iteration": 2},
            {"pipeline_run": 7, "pipeline_iteration": 2},
            {"pipeline_iteration": 2},
            {"pipeline_iteration": 2, "pipeline_max_iterations": 3},
        ):
            with self.subTest(pipeline=pipeline):
                self.assertIsNone(self.scope({"iterations": 3}, **pipeline))

    def test_the_run_token_alone_decides_whether_the_budget_is_scoped(self):
        """Enumerate every subset of the three arguments rather than assert it in prose.

        The two halves are not symmetric for a reader. An iteration with no run
        asks which run it belongs to and nothing can answer it. A run with no
        iteration still answers what the token is for, whether this loop has seen
        the run before, so it scopes on equality alone. Only the outer cap is
        optional in the other sense: leaving it out falls back rather than lifting
        the ceiling.
        """
        parts = {
            "run": {"pipeline_run": "run-a"},
            "iteration": {"pipeline_iteration": 2},
            "cap": {"pipeline_max_iterations": 3},
        }
        scoped_by_names = {
            (): False,
            ("run",): True,
            ("iteration",): False,
            ("cap",): False,
            ("run", "cap"): True,
            ("iteration", "cap"): False,
            ("run", "iteration"): True,
            ("run", "iteration", "cap"): True,
        }
        for names, scoped in scoped_by_names.items():
            with self.subTest(names=names):
                pipeline = {}
                for name in names:
                    pipeline.update(parts[name])
                scope = self.scope({"iterations": 9}, **pipeline)
                self.assertEqual(scoped, scope is not None)
                self.assertEqual(
                    scoped,
                    MODULE.absolute_iteration_cap(
                        scope, 5, pipeline.get("pipeline_max_iterations")
                    )
                    is not None,
                )

    def test_a_lone_run_token_resets_once_and_is_inert_on_every_relaunch(self):
        """This is what makes the degraded case coarser rather than launch-scoped.

        The caller mints one token per run and repeats it on every relaunch inside
        that run, so equality alone still tells a first sighting from a repeat. The
        budget therefore refreshes once when the run arrives and never again while
        it lasts, which is the stricter direction, not the unbounded one.
        """
        state = {"iterations": 5}

        first = self.scope(state, pipeline_run="run-a")
        self.assertEqual(5, first["baseline"])
        self.assertEqual(5, first["run_baseline"])

        state["pipeline_budget"] = first
        for spent in (5, 7, 40):
            with self.subTest(spent=spent):
                state["iterations"] = spent
                relaunch = self.scope(state, pipeline_run="run-a")
                self.assertEqual(5, relaunch["baseline"])
                self.assertEqual(5, relaunch["run_baseline"])

        state["iterations"] = 40
        next_run = self.scope(state, pipeline_run="run-b")
        self.assertEqual(40, next_run["baseline"])
        self.assertEqual(40, next_run["run_baseline"])

    def test_an_unusable_iteration_degrades_rather_than_refusing_the_pull_request(self):
        """Ignoring the run outright is the permanent refusal this contract removes.

        The durable count only ever climbs, so a position this loop discarded would
        leave a pull request that already reached the cap refusing every later run
        for the rest of its life. The usable half is used instead.
        """
        for iteration in (None, 0, -1, True, "2", 1.5):
            with self.subTest(iteration=iteration):
                scope = self.scope(
                    {"iterations": 5}, pipeline_run="run-a", pipeline_iteration=iteration
                )
                self.assertIsNotNone(scope)
                self.assertIsNone(
                    MODULE.exhausted_budget({"iterations": 5}, scope, 5, 10)
                )

    def test_an_iteration_with_no_run_cannot_be_completed_by_this_loops_own_state(self):
        """A run token must come from the caller, never from what this loop recorded.

        Reading it back out of an earlier budget, a head it pushed, or an
        escalation it wrote would be this loop naming its own position.
        """
        states = (
            {},
            {"iterations": 4},
            {"iterations": 4, "pipeline_budget": dict(self.RECORDED)},
            {"iterations": 4, "pr": {"head_sha": "aaaa"}, "history": [{"id": "one"}]},
            {"iterations": 4, "escalation": {"kind": "max_iterations"}},
            {"iterations": 4, "clean_at_head_sha": "aaaa"},
        )
        for state in states:
            with self.subTest(state=state):
                self.assertIsNone(
                    self.scope(dict(state), pipeline_iteration=2, pipeline_max_iterations=3)
                )

    def test_only_the_position_the_caller_passes_can_reset_the_budget(self):
        """Enumerate the inputs to a reset instead of claiming the property in prose.

        A repeat of one position stays inert no matter what this loop did in
        between: a new head, a commit it pushed, an escalation it recorded, a
        clearance, or more iterations it spent. Every one of those varies here
        while the caller's values stay the same, and neither baseline moves.
        """
        observable = (
            {},
            {"pr": {"head_sha": "new-head"}},
            {"pr": {"head_sha": "another-head"}, "attempt": {"status": "published"}},
            {"escalation": {"kind": "max_iterations"}},
            {"history": [{"id": "one"}, {"id": "two"}]},
            {"clean_at_head_sha": "new-head"},
            {"last_result": "published"},
        )
        for spent in (0, 3, 5, 40):
            for extra in observable:
                with self.subTest(spent=spent, extra=extra):
                    state = {
                        "iterations": spent,
                        "pipeline_budget": dict(self.RECORDED),
                        **extra,
                    }
                    scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=2)
                    self.assertEqual(self.RECORDED, scope)

    def test_a_stale_or_replayed_iteration_is_inert(self):
        """Strictly greater, so a repeat and a replay both buy nothing."""
        for iteration in (1, 2):
            with self.subTest(iteration=iteration):
                state = {"iterations": 9, "pipeline_budget": dict(self.RECORDED)}
                scope = self.scope(
                    state, pipeline_run="run-a", pipeline_iteration=iteration
                )
                self.assertEqual(self.RECORDED, scope)

    def test_a_genuine_advance_refreshes_only_the_per_iteration_budget(self):
        """The whole-run ceiling must survive an advance, or it bounds nothing."""
        state = {"iterations": 9, "pipeline_budget": dict(self.RECORDED)}

        scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=3)

        self.assertEqual(
            {"run": "run-a", "iteration": 3, "baseline": 9, "run_baseline": 1}, scope
        )

    def test_a_first_iteration_inside_a_run_scoped_budget_is_not_an_advance(self):
        """Nothing was recorded to advance past, so the run's own reset still stands."""
        state = {
            "iterations": 9,
            "pipeline_budget": {
                "run": "run-a",
                "iteration": None,
                "baseline": 5,
                "run_baseline": 5,
            },
        }

        scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=3)

        self.assertEqual(
            {"run": "run-a", "iteration": 3, "baseline": 5, "run_baseline": 5}, scope
        )

    def test_a_new_run_resets_both_budgets_even_when_its_iteration_went_backwards(self):
        """An outer run restarts at 1 while this state is durable per pull request.

        Comparing order alone would see the count go backwards on every later run
        and never reset again, refusing the pull request for the rest of its life.
        """
        state = {
            "iterations": 9,
            "pipeline_budget": {
                "run": "run-a",
                "iteration": 6,
                "baseline": 7,
                "run_baseline": 2,
            },
        }

        scope = self.scope(state, pipeline_run="run-b", pipeline_iteration=1)

        self.assertEqual(
            {"run": "run-b", "iteration": 1, "baseline": 9, "run_baseline": 9}, scope
        )

    def test_the_run_is_opaque_and_only_ever_compared_for_equality(self):
        """Tokens that would sort or parse are still just tokens."""
        state = {
            "iterations": 4,
            "pipeline_budget": {
                "run": "2026-05-01/7",
                "iteration": 3,
                "baseline": 2,
                "run_baseline": 0,
            },
        }

        same = self.scope(state, pipeline_run="2026-05-01/7", pipeline_iteration=3)
        self.assertEqual(2, same["baseline"])
        for other in ("2026-05-01/8", "2026-04-01/7", "7", "run", " 2026-05-01/7"):
            with self.subTest(other=other):
                scope = self.scope(state, pipeline_run=other, pipeline_iteration=3)
                self.assertEqual(4, scope["baseline"])
                self.assertEqual(4, scope["run_baseline"])

    def test_a_reset_never_rewrites_the_durable_count_itself(self):
        """Both budgets are baselines, so the per-PR iteration numbering stays monotone.

        Zeroing the count instead would restart the numbering, and an attempt id
        built from it would collide with one already folded into history, where a
        duplicate is dropped rather than recorded.
        """
        state = {"iterations": 9, "pipeline_budget": dict(self.RECORDED)}

        self.scope(state, pipeline_run="run-b", pipeline_iteration=1)

        self.assertEqual(9, state["iterations"])

    def test_the_ceiling_is_derived_from_the_callers_own_cap(self):
        scope = {"run": "run-a", "iteration": 1, "baseline": 0, "run_baseline": 0}
        self.assertEqual(15, MODULE.absolute_iteration_cap(scope, 5, 3))
        self.assertEqual(20, MODULE.absolute_iteration_cap(scope, 10, 2))

    def test_an_omitted_outer_cap_falls_back_rather_than_disabling_the_ceiling(self):
        """Only the outer cap is optional, and omitting it must not remove the bound."""
        scope = {"run": "run-a", "iteration": 1, "baseline": 0, "run_baseline": 0}
        for value in (None, 0, -1, True, "3"):
            with self.subTest(value=value):
                self.assertEqual(
                    5 * MODULE.DEFAULT_PIPELINE_MAX_ITERATIONS,
                    MODULE.absolute_iteration_cap(scope, 5, value),
                )

    def test_a_standalone_run_keeps_the_flat_per_pull_request_cap(self):
        """No arguments means the behavior this loop has always had."""
        for spent, expected in ((0, None), (4, None), (5, "iteration"), (9, "iteration")):
            with self.subTest(spent=spent):
                self.assertEqual(
                    expected,
                    MODULE.exhausted_budget({"iterations": spent}, None, 5, None),
                )

    def test_a_scoped_run_spends_against_its_baseline_and_not_the_lifetime_count(self):
        """A spent brake must not read as a permanent refusal.

        Ninety iterations over the pull request's life say nothing about the run
        that just started, which has spent none of its own budget.
        """
        scope = {"run": "run-a", "iteration": 1, "baseline": 90, "run_baseline": 90}
        self.assertIsNone(MODULE.exhausted_budget({"iterations": 90}, scope, 5, 10))
        self.assertIsNone(MODULE.exhausted_budget({"iterations": 94}, scope, 5, 10))
        self.assertEqual(
            "iteration", MODULE.exhausted_budget({"iterations": 95}, scope, 5, 10)
        )

    def test_the_whole_run_ceiling_holds_even_when_every_iteration_looks_fresh(self):
        """A caller that keeps advancing must still not spend without end."""
        scope = {"run": "run-a", "iteration": 4, "baseline": 10, "run_baseline": 0}
        self.assertEqual(
            "absolute", MODULE.exhausted_budget({"iterations": 10}, scope, 5, 10)
        )
        self.assertIsNone(MODULE.exhausted_budget({"iterations": 9}, scope, 5, 10))

class InvocationBudgetTest(unittest.TestCase):
    def test_manual_and_pipeline_charges_are_independent(self):
        state = {"iterations": 5}
        pipeline = MODULE.scoped_budget(
            state,
            "pipeline",
            {
                "run": "pipeline-run",
                "iteration": 2,
                "baseline": 0,
                "run_baseline": 0,
            },
        )
        invocation = MODULE.scoped_budget(
            state,
            "invocation",
            {
                "run": "manual-run",
                "iteration": None,
                "baseline": 5,
                "run_baseline": 5,
            },
        )
        state["pipeline_budget"] = {
            key: value for key, value in pipeline.items() if not key.startswith("_")
        }
        state["budget_scope"] = "pipeline"
        MODULE.charge_iteration(state)
        state["invocation_budget"] = {
            key: value for key, value in invocation.items() if not key.startswith("_")
        }
        state["budget_scope"] = "invocation"
        MODULE.charge_iteration(state)

        self.assertEqual((6, 6), MODULE.budget_spent(state, pipeline))
        self.assertEqual((1, 1), MODULE.budget_spent(state, invocation))

    def test_the_helper_advertises_the_flag_an_orchestrator_probes_for(self):
        """An orchestrator reads the installed script to decide whether to send it.

        It omits the position entirely when the flag is missing, so renaming it
        would silently leave this stage unscoped rather than fail.
        """
        self.assertIn("--pipeline-run", SCRIPT.read_text(encoding="utf-8"))


class LegacyLauncherPositionInstructions:
    """The agent file has to take a position however the caller words it."""

    def setUp(self):
        self.instructions = AGENT.read_text(encoding="utf-8")

    def test_the_position_reaches_preflight_as_the_three_arguments(self):
        self.assertIn("### A Launcher's Loop Position", self.instructions)
        self.assertIn(
            "--pipeline-run <token> --pipeline-iteration <number> "
            "--pipeline-max-iterations <number>",
            self.instructions,
        )
        self.assertIn("Copy them exactly. Do not read the token", self.instructions)

    def test_keys_the_position_on_the_values_rather_than_one_spelling(self):
        """A launcher that words it differently still gets its budget scoped.

        Making one phrasing the trigger drops a position supplied any other way,
        and it drops it silently: the run reports cleanly and the budget was
        simply never scoped. What makes the widening safe is where a value came
        from, not how it was written.
        """
        self.assertIn("Read the values, not the spelling.", self.instructions)
        self.assertIn(
            "a spelling you do not recognize is still the caller's instruction",
            self.instructions,
        )
        self.assertIn("the caller may supply one and you may not", self.instructions)

    def test_the_two_halves_go_out_together_as_a_rule_on_the_sender(self):
        """The pairing binds what a launcher emits, never what this loop accepts.

        Reading it as a receiver rule would have this loop discard a position that
        arrived with a half missing, and its durable count would then refuse the
        pull request for good.
        """
        self.assertIn(
            "Omit all three only when the request names no position at all",
            self.instructions,
        )
        self.assertIn(
            "Send `--pipeline-run` and `--pipeline-iteration` together",
            self.instructions,
        )
        self.assertNotIn("the helper ignores a lone one", self.instructions)

    def test_the_loop_may_never_supply_a_value_itself(self):
        self.assertIn(
            "Never supply, guess, carry over, or reconstruct a value yourself",
            self.instructions,
        )
        self.assertIn(
            "never invent one to keep working after `max_iterations_reached`",
            self.instructions,
        )
        self.assertIn(
            "A value you produced would be this loop refreshing its own cap",
            self.instructions,
        )

    def test_the_flat_cap_is_stated_as_a_default_an_outer_loop_may_replace(self):
        self.assertIn("unless an outer loop sets its own", self.instructions)


class LocalValidationRecordTest(unittest.TestCase):
    """The record is what makes the push requirement falsifiable.

    Reading a stage's own state afterwards has to say whether it validated,
    skipped, or claimed nothing at all, because inferring that from the checks
    that fail later is exactly the guessing this replaced.
    """

    def entry(self, head="head1", **overrides):
        args = SimpleNamespace(validated=None, rewrote=None, not_validated=None)
        for key, value in overrides.items():
            setattr(args, key, value)
        return MODULE.local_validation_entry(args, head)

    def test_records_the_commands_that_ran_and_the_head_they_covered(self):
        entry = self.entry(validated=["check one", "check two"])
        self.assertEqual("passed", entry["status"])
        self.assertEqual(["check one", "check two"], entry["commands"])
        self.assertEqual([], entry["rewrote"])
        self.assertEqual("head1", entry["head_sha"])

    def test_separates_the_commands_that_rewrote_files(self):
        """A command that ran clean and one that changed files differ.

        Only the second has anything that must reach the commits being pushed.
        """
        entry = self.entry(validated=["check one"], rewrote=["check one"])
        self.assertEqual(["check one"], entry["rewrote"])
        self.assertEqual(["check one"], entry["commands"])

    def test_a_rewriting_command_counts_as_one_that_ran(self):
        """Naming a command as rewriting implies it ran.

        Folding that in keeps a malformed claim from reaching the state as a
        contradiction, and keeps it from becoming a reason to refuse.
        """
        entry = self.entry(rewrote=["check one"])
        self.assertEqual("passed", entry["status"])
        self.assertEqual(["check one"], entry["commands"])
        self.assertEqual(["check one"], entry["rewrote"])

    def test_records_the_reason_when_nothing_covering_ran(self):
        entry = self.entry(not_validated="no narrow command exists here")
        self.assertEqual("skipped", entry["status"])
        self.assertEqual("no narrow command exists here", entry["reason"])
        self.assertNotIn("commands", entry)

    def test_records_that_the_publication_claimed_nothing(self):
        """This is the value that shows the requirement being ignored.

        A run that says neither thing must be distinguishable from one that
        deliberately skipped, or a live run proves nothing either way.
        """
        self.assertEqual("unreported", self.entry()["status"])

    def test_blank_claims_are_treated_as_no_claim(self):
        entry = self.entry(validated=["  "], not_validated="   ")
        self.assertEqual("unreported", entry["status"])


class DetachedHeadTargetTest(unittest.TestCase):
    """A refusal that names no correction is a dead end for its caller.

    The resolver is right to refuse, because a commit can belong to more than
    one pull request and no tie-break belongs here. What it owes the caller is
    the one thing that gets them past it.
    """

    def test_the_refusal_names_the_correction_and_not_only_the_fault(self):
        with mock.patch.object(MODULE, "git", return_value=""):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.current_pr_target(Path("repo"))
        message = str(error.exception)
        self.assertIn("detached HEAD", message)
        self.assertIn(
            "pass the pull request explicitly as a URL or owner/repo#number",
            message,
        )


class CandidateContractTest(unittest.TestCase):
    def setUp(self):
        self.head = "1" * 40
        self.base = "2" * 40
        self.code = "3" * 40
        self.output = "4" * 40
        self.preflight = {
            "identity": {"branch": "feature", "head": self.head, "status": ""},
            "pr": {
                "repo_name": "owner/repo",
                "number": 7,
                "pr_url": "https://github.com/owner/repo/pull/7",
                "base_branch": "main",
                "base_sha": self.base,
                "head_repository": "owner/repo",
                "head_branch": "feature",
                "head_sha": self.head,
                "cross_repository": False,
                "state": "OPEN",
            },
        }

    def metadata(self, sha, parent, paths):
        return {
            "sha": sha,
            "parent_sha": parent,
            "tree_sha": "a" * 40,
            "patch_sha256": MODULE.sha256_text("patch\n"),
            "changed_paths": paths,
        }

    def result(self, *, code=True, output_paths=None):
        commits = (
            [self.metadata(self.code, self.head, ["src/app.py"])] if code else []
        )
        code_tip = self.code if code else self.head
        artifact = (
            self.metadata(self.output, code_tip, output_paths)
            if output_paths is not None
            else None
        )
        generated_head = self.output if artifact else code_tip
        completion = {
            "request": {
                "requested_model": "gpt-6-sol",
                "prompt_sha256": "b" * 64,
            },
            "task": {
                "id": "task-1",
                "state": "completed",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:01:00Z",
                "completed_at": "2026-01-01T00:01:00Z",
                "raw_response_sha256": "c" * 64,
            },
            "session": {
                "id": "session-1",
                "state": "completed",
                "actual_model": "gpt-6-sol",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:01:00Z",
                "completed_at": "2026-01-01T00:01:00Z",
                "prompt_sha256": "b" * 64,
            },
            "repository": {
                "name_with_owner": "owner/repo",
                "id": 1,
                "owner": {"login": "owner", "id": 2},
            },
            "refs": {"base": "feature", "generated": "copilot/candidate"},
        }
        return {
            "schema": MODULE.CANDIDATE_AGENT_TASK_RESULT_SCHEMA,
            "status": "success",
            "mode": "code_candidate",
            "repository": {"name_with_owner": "owner/repo"},
            "pull_request": MODULE.expected_cloud_pull_request(self.preflight),
            "requested_model": "gpt-6-sol",
            "policy": {
                "id": "marketplace-agent-code-candidate-worker",
                "version": 1,
                "sha256": MODULE.AGENT_TASK_POLICY_SHA256,
            },
            "task": {
                "id": "task-1",
                "url": "https://github.com/owner/repo/agent-tasks/task-1",
                "state": "completed",
                "base_ref": "feature",
                "base_sha": self.head,
            },
            "generated": {
                "branch": "copilot/candidate",
                "head_sha": generated_head,
                "commits": [item["sha"] for item in commits],
            },
            "application": {
                "status": "not_applied",
                "final_local_head": self.head,
            },
            "report": None,
            "attestation": {
                "kind": "dispatcher_candidate",
                "structural_complete": True,
            },
            "candidate": {
                "schema": MODULE.AGENT_TASK_CANDIDATE_MANIFEST_SCHEMA,
                "repository": {"name_with_owner": "owner/repo"},
                "task": {"id": "task-1", "session_id": "session-1"},
                "base": {"ref": "feature", "sha": self.head},
                "generated": {
                    "ref": "copilot/candidate",
                    "head_sha": generated_head,
                    "code_tip_sha": code_tip,
                },
                "code_commits": commits,
                "artifact_commit": artifact,
            },
            "completion": completion,
            "error": None,
        }

    def validate(self, result):
        candidate = result["candidate"]
        artifact = candidate["artifact_commit"]
        verified = {
            "task": result["task"],
            "completion": result["completion"],
            "candidate": candidate,
            "artifact_commit": artifact,
            "code_tip": candidate["generated"]["code_tip_sha"],
            "commits": [item["sha"] for item in candidate["code_commits"]],
        }
        runtime = SimpleNamespace(
            CloudError=RuntimeError,
            PullRequestSnapshot=lambda **values: SimpleNamespace(**values),
            Options=lambda **values: SimpleNamespace(**values),
            GitRepository=mock.Mock(return_value=object()),
            verify_current_candidate=mock.Mock(return_value=verified),
        )
        with mock.patch.object(
            MODULE, "load_candidate_runtime", return_value=runtime
        ):
            remote = MODULE.verify_runtime_candidate(
                result,
                helper=Path("cloud_task.py"),
                repo_root=Path("repo"),
                preflight=self.preflight,
                requested_model="gpt-6-sol",
                prompt="frozen prompt",
            )
        runtime.verify_current_candidate.assert_called_once()
        kwargs = runtime.verify_current_candidate.call_args.kwargs
        self.assertIs(kwargs["base_is_ancestor"], MODULE.live_base_contains)
        options = kwargs["options"]
        self.assertEqual("frozen prompt", options.prompt)
        self.assertEqual(MODULE.AGENT_TASK_POLICY, options.policy)
        return remote

    def test_accepts_missing_malformed_or_arbitrary_advisory_report(self):
        cases = [
            (self.result(code=False), None),
            (
                self.result(
                    output_paths=[MODULE.AGENT_TASK_OUTPUT_REPORT],
                ),
                MODULE.AGENT_TASK_OUTPUT_REPORT,
            ),
            (
                self.result(
                    output_paths=[
                        ".github/agent-task-output/arbitrary.bin",
                        MODULE.AGENT_TASK_OUTPUT_REPORT,
                    ],
                ),
                MODULE.AGENT_TASK_OUTPUT_REPORT,
            ),
        ]
        for result, report_path in cases:
            with self.subTest(report_path=report_path):
                remote = self.validate(result)
                self.assertEqual(
                    report_path,
                    (
                        remote["report_evidence"]["path"]
                        if remote["report_evidence"]
                        else None
                    ),
                )

    def test_zero_and_nonzero_candidates_use_the_manifest_code_tip(self):
        zero = self.validate(self.result(code=False))
        fixed = self.validate(
            self.result(output_paths=[MODULE.AGENT_TASK_OUTPUT_REPORT])
        )

        self.assertEqual([], zero["commits"])
        self.assertEqual(self.head, zero["final_local_head"])
        self.assertEqual([self.code], fixed["commits"])
        self.assertEqual(self.code, fixed["final_local_head"])
        self.assertEqual(self.output, fixed["generated_head"])

    def test_runtime_rejection_is_fail_closed(self):
        result = self.result()
        result["pull_request"]["head_sha"] = "9" * 40
        runtime = SimpleNamespace(
            CloudError=RuntimeError,
            PullRequestSnapshot=lambda **values: SimpleNamespace(**values),
            Options=lambda **values: SimpleNamespace(**values),
            GitRepository=mock.Mock(return_value=object()),
            verify_current_candidate=mock.Mock(
                side_effect=RuntimeError("candidate identity changed")
            ),
        )
        with (
            mock.patch.object(MODULE, "load_candidate_runtime", return_value=runtime),
            self.assertRaisesRegex(MODULE.WorkflowError, "candidate rejected"),
        ):
            MODULE.verify_runtime_candidate(
                result,
                helper=Path("cloud_task.py"),
                repo_root=Path("repo"),
                preflight=self.preflight,
                requested_model="gpt-6-sol",
                prompt="frozen prompt",
            )

    def test_hosted_base_comparison_accepts_only_forward_history(self):
        self.validate(self.result())
        for status, accepted in (
            ("ahead", True), ("identical", True),
            ("behind", False), ("diverged", False),
        ):
            with self.subTest(status=status), mock.patch.object(
                MODULE, "gh_json", return_value={"status": status}
            ) as compare:
                self.assertIs(
                    MODULE.live_base_contains("owner/repo", self.base, "5" * 40),
                    accepted,
                )
            compare.assert_called_once_with(
                ["api", f"repos/owner/repo/compare/{self.base}...{'5' * 40}"]
            )

    def test_runtime_manifest_coverage_excludes_output_commit(self):
        result = self.result(output_paths=[MODULE.AGENT_TASK_OUTPUT_REPORT])
        remote = self.validate(result)
        coverage = {
            item["sha"]: item["changed_paths"]
            for item in remote["candidate_manifest"]["code_commits"]
        }
        self.assertEqual({self.code: ["src/app.py"]}, coverage)

    def test_guarded_import_targets_only_the_code_tip(self):
        result = self.result(output_paths=[MODULE.AGENT_TASK_OUTPUT_REPORT])
        remote = self.validate(result)
        imported = {
            "application": "fast_forwarded",
            "final_local_head": self.code,
        }
        runtime = SimpleNamespace(
            CloudError=RuntimeError,
            PullRequestSnapshot=lambda **values: SimpleNamespace(**values),
            Options=lambda **values: SimpleNamespace(**values),
            GitRepository=mock.Mock(return_value=object()),
            guarded_fast_forward_candidate=mock.Mock(return_value=imported),
        )
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.json"
            result_path.write_text(json.dumps(result), encoding="utf-8")
            digest = MODULE.sha256_file(result_path)
            with (
                mock.patch.object(
                    MODULE, "load_candidate_runtime", return_value=runtime
                ),
                mock.patch.object(
                    MODULE,
                    "local_identity",
                    side_effect=[
                        {"branch": "feature", "head": self.head, "status": ""},
                        {"branch": "feature", "head": self.code, "status": ""},
                    ],
                ),
            ):
                self.assertTrue(
                    MODULE.apply_verified_candidate_import(
                        Path("repo"),
                        helper=Path("cloud_task.py"),
                        requested_model="gpt-6-sol",
                        prompt="frozen prompt",
                        result_path=result_path,
                        result_sha256=digest,
                        preflight=self.preflight,
                        remote=remote,
                    )
                )

        call = runtime.guarded_fast_forward_candidate.call_args
        self.assertEqual(result, call.args[0])
        self.assertEqual("frozen prompt", call.kwargs["options"].prompt)
        self.assertEqual(MODULE.AGENT_TASK_POLICY, call.kwargs["options"].policy)
        self.assertIs(call.kwargs["base_is_ancestor"], MODULE.live_base_contains)


class SourceDriftClassificationTest(unittest.TestCase):
    def test_accepts_only_verified_same_ref_forward_movement(self):
        expected = {
            "number": 7,
            "repo_name": "owner/repo",
            "title": "Title",
            "body": "Body",
            "head_owner": "owner",
            "head_repo": "repo",
            "head_branch": "feature",
            "head_sha": "1" * 40,
            "base_branch": "main",
            "base_sha": "2" * 40,
            "state": "OPEN",
        }
        actual = {**expected, "head_sha": "3" * 40}
        with mock.patch.object(
            MODULE, "gh_json", return_value={"status": "ahead"}
        ) as compare:
            self.assertTrue(MODULE.same_ref_forward_head_drift(expected, actual))
        self.assertIn(
            f"{expected['head_sha']}...{actual['head_sha']}",
            compare.call_args.args[0][1],
        )

        with mock.patch.object(MODULE, "gh_json") as compare:
            self.assertFalse(
                MODULE.same_ref_forward_head_drift(
                    expected, {**actual, "head_branch": "other"}
                )
            )
        compare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
