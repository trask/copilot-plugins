from contextlib import redirect_stdout
import copy
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "pr_pipeline.py"
AGENT = Path(__file__).parents[1] / "agents" / "pr-pipeline.agent.md"
SPEC = importlib.util.spec_from_file_location("pr_pipeline", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

HEAD = "a" * 40
NEXT_HEAD = "b" * 40
BASE = "c" * 40
NEXT_BASE = "d" * 40
PIPELINE_RUN = "1" * 32


def target() -> dict:
    return MODULE.build_target("owner", "repo", 7)


def pull_request(head: str = HEAD, state: str = "OPEN") -> dict:
    return {
        **target(),
        "title": "Add a thing",
        "state": state,
        "is_draft": True,
        "head_branch": "feature",
        "base_branch": "main",
        "base_sha": BASE,
        "head_sha": head,
    }


def clear_stage(stage: str, head: str = HEAD) -> dict:
    return {
        "stage": stage,
        "clear": True,
        "clear_at_head_sha": head,
        "outcome": "cleared",
        "reason": None,
        "installed": True,
        "status_state": "state.json",
        "status": {},
    }


def uncleared_stage(stage: str, outcome: str | None = "carried") -> dict:
    return {
        "stage": stage,
        "clear": False,
        "clear_at_head_sha": None,
        "outcome": outcome,
        "reason": outcome or "not_cleared",
        "installed": True,
        "status_state": "state.json",
        "status": {},
    }


class GithubMutationPolicyTest(unittest.TestCase):
    def setUp(self):
        self.previous = MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY
        self.addCleanup(
            setattr,
            MODULE.common,
            "ACTIVE_GITHUB_MUTATION_POLICY",
            self.previous,
        )

    def test_scheduler_defaults_to_normal_policy(self):
        args = MODULE.build_parser().parse_args(["start", "owner/repo#7"])
        command = MODULE.scheduler_command(
            args, target(), "run-1", Path("progress.jsonl")
        )
        self.assertEqual("allow", args.github_mutation_policy)
        self.assertEqual(
            "allow", command[command.index("--github-mutation-policy") + 1]
        )

    def test_scheduler_preserves_source_only_policy(self):
        args = MODULE.build_parser().parse_args(
            [
                "start",
                "owner/repo#7",
                "--github-mutation-policy",
                "source-only",
            ]
        )
        command = MODULE.scheduler_command(
            args,
            {"owner": "owner", "repo": "repo", "number": 7},
            "run-1",
            Path("progress.jsonl"),
        )

        index = command.index("--github-mutation-policy")
        self.assertEqual("source-only", command[index + 1])

    def test_metadata_sensitive_stages_receive_source_only_helper_argument(self):
        MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY = "source-only"
        target = {"repo_name": "owner/repo", "number": 7}
        for stage in (
            MODULE.common.STAGE_COPILOT_REVIEW,
            MODULE.common.STAGE_SELF_REVIEW,
            MODULE.common.STAGE_DESCRIPTION,
        ):
            with self.subTest(stage=stage):
                entry = next(
                    item
                    for item in MODULE.common.STAGES
                    if item["stage"] == stage
                )
                with mock.patch.object(
                    MODULE.common, "validate_stage_route"
                ):
                    command = MODULE.common.stage_command(
                        entry,
                        target,
                        model="gpt-5.6-sol",
                        effort="high",
                        arguments=[
                            "--pipeline-run",
                            "run-1",
                            "--pipeline-iteration",
                            "1",
                        ],
                        resolve_program=lambda _name: "copilot",
                    )

                self.assertEqual(
                    "source-only", command[command.index("--github-mutation-policy") + 1]
                )
                self.assertNotIn("-p", command)

    def test_agent_documents_normal_authorization_and_explicit_restrictions(self):
        instructions = AGENT.read_text(encoding="utf-8")
        self.assertIn("Normal execution uses `--github-mutation-policy allow`", instructions)
        self.assertIn("draft or ready for review", instructions)
        self.assertIn("standard stage-owned actions", instructions)
        self.assertIn("bot-authored review threads", instructions)
        self.assertIn("which require a separate explicit request", instructions)
        self.assertIn("only when the caller explicitly requests", instructions)
        self.assertIn("Do not infer source-only from draft status", instructions)
        self.assertNotIn("open draft pull request", instructions)
        self.assertIn("--github-mutation-policy source-only", instructions)
        self.assertIn("never change it for that run", instructions)


class InstalledRuntimeTest(unittest.TestCase):
    def test_dynamic_common_import_does_not_write_bytecode(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            installed = Path(raw_directory)
            copied_script = installed / SCRIPT.name
            copied_script.write_bytes(SCRIPT.read_bytes())
            (installed / "pipeline_common.py").write_bytes(
                (SCRIPT.parent / "pipeline_common.py").read_bytes()
            )
            environment = os.environ.copy()
            environment.pop("PYTHONDONTWRITEBYTECODE", None)
            completed = subprocess.run(
                [os.fsdecode(os.environ.get("PYTHON", "python")), str(copied_script), "--help"],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                ),
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertFalse((installed / "__pycache__").exists())


class WindowsSubprocessTest(unittest.TestCase):
    @staticmethod
    def access_denied() -> OSError:
        error = OSError("Access is denied")
        error.winerror = 5
        return error

    def test_run_hides_windows_console_processes(self):
        completed = subprocess.CompletedProcess(["tasklist"], 0, "", "")
        with (
            mock.patch.object(MODULE.common, "IS_WINDOWS", True),
            mock.patch.object(
                MODULE.common.subprocess,
                "CREATE_NO_WINDOW",
                0x08000000,
                create=True,
            ),
            mock.patch.object(
                MODULE.common.subprocess, "run", return_value=completed
            ) as run,
        ):
            MODULE.common.run(["tasklist"], check=False)

        self.assertEqual(0x08000000, run.call_args.kwargs["creationflags"])

    def test_detached_scheduler_uses_no_window_without_detached_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.object(MODULE.common, "IS_WINDOWS", True),
                mock.patch.object(
                    MODULE.common.subprocess,
                    "CREATE_NO_WINDOW",
                    0x08000000,
                    create=True,
                ),
                mock.patch.object(
                    MODULE.common.subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    0x00000200,
                    create=True,
                ),
                mock.patch.object(
                    MODULE.common.subprocess,
                    "CREATE_BREAKAWAY_FROM_JOB",
                    0x01000000,
                    create=True,
                ),
                mock.patch.object(
                    MODULE.common.subprocess,
                    "DETACHED_PROCESS",
                    0x00000008,
                    create=True,
                ),
                mock.patch.object(MODULE.common.subprocess, "Popen") as popen,
            ):
                MODULE.common.start_detached(
                    ["python", "scheduler.py"],
                    cwd=root,
                    log_path=root / "scheduler.log",
                )

        flags = popen.call_args.kwargs["creationflags"]
        self.assertEqual(0x08000000, flags & 0x08000000)
        self.assertEqual(0, flags & 0x00000008)

    def test_detached_scheduler_retries_without_breakaway_when_access_is_denied(self):
        process = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.object(MODULE.common, "IS_WINDOWS", True),
                mock.patch.object(
                    MODULE.common.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True
                ),
                mock.patch.object(
                    MODULE.common.subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    0x00000200,
                    create=True,
                ),
                mock.patch.object(
                    MODULE.common.subprocess,
                    "CREATE_BREAKAWAY_FROM_JOB",
                    0x01000000,
                    create=True,
                ),
                mock.patch.object(
                    MODULE.common.subprocess,
                    "Popen",
                    side_effect=[self.access_denied(), process],
                ) as popen,
            ):
                started = MODULE.common.start_detached(
                    [r"C:\Python\python.exe", "scheduler.py"],
                    cwd=root,
                    log_path=root / "scheduler.log",
                )

        self.assertIs(process, started)
        self.assertEqual(2, popen.call_count)
        self.assertEqual(0x09000200, popen.call_args_list[0].kwargs["creationflags"])
        self.assertEqual(0x08000200, popen.call_args_list[1].kwargs["creationflags"])

    def test_detached_scheduler_fallback_reports_operation_and_program(self):
        fallback_error = OSError("fallback failed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.object(MODULE.common, "IS_WINDOWS", True),
                mock.patch.object(
                    MODULE.common.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True
                ),
                mock.patch.object(
                    MODULE.common.subprocess,
                    "CREATE_BREAKAWAY_FROM_JOB",
                    0x01000000,
                    create=True,
                ),
                mock.patch.object(
                    MODULE.common.subprocess,
                    "Popen",
                    side_effect=[self.access_denied(), fallback_error],
                ),
            ):
                with self.assertRaisesRegex(
                    MODULE.common.WorkflowError,
                    r"detached scheduler launch failed for C:\\Python\\python.exe",
                ):
                    MODULE.common.start_detached(
                        [r"C:\Python\python.exe", "scheduler.py"],
                        cwd=root,
                        log_path=root / "scheduler.log",
                    )

    def test_windows_liveness_check_uses_the_windows_api(self):
        with (
            mock.patch.object(MODULE.common, "IS_WINDOWS", True),
            mock.patch.object(
                MODULE.common, "windows_process_is_alive", return_value=True
            ) as windows_query,
            mock.patch.object(
                MODULE.common, "run", side_effect=AssertionError("spawned a command")
            ),
        ):
            self.assertTrue(MODULE.common.process_is_alive(123))

        windows_query.assert_called_once_with(123)

    def test_owned_stage_monitor_polls_and_reaps_the_child_handle(self):
        process = mock.Mock()
        process.poll.side_effect = [None, 0]
        process.wait.return_value = 0
        progress = mock.Mock()
        sleep = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = MODULE.common.run_monitored(
                ["copilot"],
                cwd=root,
                log_path=root / "stage.log",
                progress=progress,
                interval=0,
                start=mock.Mock(return_value=process),
                sleep=sleep,
            )
        self.assertEqual(0, result["returncode"])
        self.assertEqual(2, progress.call_count)
        sleep.assert_called_once_with(0)
        process.wait.assert_called_once()

    def test_owned_stage_monitor_terminates_and_reaps_on_interruption(self):
        process = mock.Mock()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(KeyboardInterrupt):
                MODULE.common.run_monitored(
                    ["copilot"],
                    cwd=root,
                    log_path=root / "stage.log",
                    progress=mock.Mock(side_effect=KeyboardInterrupt),
                    start=mock.Mock(return_value=process),
                )
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=10)

    def test_owned_stage_monitor_kills_a_child_that_ignores_termination(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired(["copilot"], 10),
            1,
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(KeyboardInterrupt):
                MODULE.common.run_monitored(
                    ["copilot"],
                    cwd=root,
                    log_path=root / "stage.log",
                    progress=mock.Mock(side_effect=KeyboardInterrupt),
                    start=mock.Mock(return_value=process),
                )
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(
            [mock.call(timeout=10), mock.call()], process.wait.call_args_list
        )

    def test_windows_owned_process_terminates_the_job_before_reaping(self):
        process = mock.Mock()
        process.pid = 123
        process.poll.return_value = None

        def finish(timeout=None):
            process.poll.return_value = 1
            return 1

        process.wait.side_effect = finish
        owner = mock.Mock()
        owned = MODULE.common.OwnedProcess(process, owner)
        with mock.patch.object(MODULE.common, "IS_WINDOWS", True):
            result = MODULE.common.terminate_process_tree(owned)

        self.assertEqual(1, result)
        owner.terminate.assert_called_once()
        owner.close.assert_called_once()
        process.terminate.assert_not_called()

    def test_windows_background_worker_is_assigned_before_it_resumes(self):
        process = mock.Mock(pid=123)
        owner = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.object(MODULE.common, "IS_WINDOWS", True),
                mock.patch.object(
                    MODULE.common.subprocess,
                    "CREATE_NO_WINDOW",
                    0x08000000,
                    create=True,
                ),
                mock.patch.object(
                    MODULE.common.subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    0x00000200,
                    create=True,
                ),
                mock.patch.object(
                    MODULE.common.subprocess,
                    "CREATE_BREAKAWAY_FROM_JOB",
                    0x01000000,
                    create=True,
                ),
                mock.patch.object(
                    MODULE.common.subprocess, "Popen", return_value=process
                ) as popen,
                mock.patch.object(
                    MODULE.common, "create_windows_kill_job", return_value=owner
                ) as create_job,
                mock.patch.object(MODULE.common, "resume_windows_process") as resume,
            ):
                started = MODULE.common.start_background(
                    ["copilot"],
                    cwd=root,
                    log_path=root / "worker.log",
                )

        self.assertIs(process, started.process)
        create_job.assert_called_once_with(123)
        resume.assert_called_once_with(123)
        self.assertEqual(0x09000204, popen.call_args.kwargs["creationflags"])

    def test_windows_background_worker_falls_back_inside_the_parent_job(self):
        process = mock.Mock(pid=123)
        job_error = self.access_denied()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.object(MODULE.common, "IS_WINDOWS", True),
                mock.patch.object(
                    MODULE.common.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True
                ),
                mock.patch.object(
                    MODULE.common.subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    0x00000200,
                    create=True,
                ),
                mock.patch.object(
                    MODULE.common.subprocess,
                    "CREATE_BREAKAWAY_FROM_JOB",
                    0x01000000,
                    create=True,
                ),
                mock.patch.object(
                    MODULE.common.subprocess,
                    "Popen",
                    side_effect=[self.access_denied(), process],
                ) as popen,
                mock.patch.object(
                    MODULE.common,
                    "create_windows_kill_job",
                    side_effect=job_error,
                ) as create_job,
                mock.patch.object(MODULE.common, "resume_windows_process") as resume,
            ):
                started = MODULE.common.start_background(
                    [r"C:\Program Files\GitHub Copilot\copilot.exe"],
                    cwd=root,
                    log_path=root / "worker.log",
                )

        self.assertIs(process, started.process)
        self.assertIsNone(started.owner)
        self.assertEqual(2, popen.call_count)
        self.assertEqual(0x09000204, popen.call_args_list[0].kwargs["creationflags"])
        self.assertEqual(0x08000204, popen.call_args_list[1].kwargs["creationflags"])
        create_job.assert_called_once_with(123)
        resume.assert_called_once_with(123)


class TargetTest(unittest.TestCase):
    def test_parses_supported_targets(self):
        expected = target()
        self.assertEqual(expected, MODULE.parse_target(expected["pr_url"]))
        self.assertEqual(expected, MODULE.parse_target("owner/repo#7"))
        self.assertEqual(expected, MODULE.parse_target("#7", "owner/repo"))

    def test_reads_commit_links_for_the_pull_request(self):
        with mock.patch.object(
            MODULE,
            "gh_json",
            return_value={
                "commits": [
                    {
                        "oid": HEAD,
                        "messageHeadline": "Fix the thing",
                    }
                ]
            },
        ):
            self.assertEqual(
                [
                    {
                        "sha": HEAD,
                        "title": "Fix the thing",
                        "url": f"{target()['pr_url']}/commits/{HEAD}",
                    }
                ],
                MODULE.read_pr_commits(target()),
            )

    def test_reads_the_live_base_branch_tip(self):
        payload = {
            "number": 7,
            "title": "Add a thing",
            "url": target()["pr_url"],
            "state": "OPEN",
            "isDraft": True,
            "headRefName": "feature",
            "baseRefName": "main",
            "headRefOid": HEAD,
        }
        with mock.patch.object(
            MODULE,
            "gh_json",
            side_effect=[payload, {"object": {"sha": BASE}}],
        ):
            result = MODULE.read_pull_request(target())

        self.assertEqual(BASE, result["base_sha"])

    def test_rejects_a_base_ref_without_a_commit(self):
        with mock.patch.object(MODULE, "gh_json", return_value={"object": {}}):
            with self.assertRaisesRegex(MODULE.WorkflowError, "has no commit SHA"):
                MODULE.base_ref_tip("owner/repo", "main")

    def test_marks_rewritten_history_and_lists_replacement_commits(self):
        replacement = {
            "sha": NEXT_HEAD,
            "title": "Rebased commit",
            "url": f"{target()['pr_url']}/commits/{NEXT_HEAD}",
        }
        added, errors, rewritten = MODULE.commits_added(
            {"commits": [{"sha": HEAD, "title": "Old commit"}]},
            {"commits": [replacement]},
        )
        self.assertEqual([replacement], added)
        self.assertEqual([], errors)
        self.assertTrue(rewritten)

    def test_bare_number_needs_repository_context(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "repository context"):
            MODULE.parse_target("7")

    def test_normalizes_msys_copilot_home_on_windows(self):
        self.assertEqual(
            "C:/Users/example/.copilot",
            MODULE.normalize_cli_path(
                "/c/Users/example/.copilot",
                windows=True,
            ),
        )


class StageContractTest(unittest.TestCase):
    def test_stage_order_is_fixed(self):
        self.assertEqual(
            (
                "pr-conflict-resolver",
                "copilot-review-loop",
                "self-review-loop",
                "ci-fix-loop",
                "pr-description",
            ),
            MODULE.STAGE_NAMES,
        )

    def test_every_agent_is_plugin_qualified(self):
        for entry in MODULE.STAGES:
            self.assertEqual(f"{entry['plugin']}:{entry['stage']}", entry["agent"])

    def test_each_stage_has_one_head_marker(self):
        self.assertEqual(
            {
                MODULE.STAGE_CONFLICT: ("mergeable_at_head_sha",),
                MODULE.STAGE_SELF_REVIEW: ("review", "clean_at_head_sha"),
                MODULE.STAGE_COPILOT_REVIEW: ("clean_at_head_sha",),
                MODULE.STAGE_CI: ("clean_at_head_sha",),
                MODULE.STAGE_DESCRIPTION: ("validated_head_sha",),
            },
            {entry["stage"]: entry["marker"] for entry in MODULE.STAGES},
        )
        self.assertEqual(
            ("policy_skip", "head_sha"),
            MODULE.STAGE_BY_NAME[MODULE.STAGE_COPILOT_REVIEW]["skip_marker"],
        )

    def test_self_review_requires_the_exact_model_and_effort(self):
        models = MODULE.stage_models(None)
        self.assertEqual("gpt-5.6-sol", models[MODULE.STAGE_SELF_REVIEW])
        with self.assertRaisesRegex(
            MODULE.WorkflowError,
            "requires exactly model gpt-5.6-sol",
        ):
            MODULE.stage_models(["self-review-loop=claude-sonnet-5"])
        with self.assertRaisesRegex(
            MODULE.WorkflowError,
            "requires exactly reasoning effort high",
        ):
            MODULE.stage_models(None, "max")

    def test_self_review_launch_revalidates_the_exact_route(self):
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_SELF_REVIEW]
        for model, effort, error in (
            ("claude-sonnet-5", "high", "requires exactly model gpt-5.6-sol"),
            ("gpt-5.6-sol", "max", "requires exactly reasoning effort high"),
        ):
            with (
                self.subTest(model=model, effort=effort),
                self.assertRaisesRegex(MODULE.WorkflowError, error),
            ):
                MODULE.common.stage_command(
                    entry,
                    target(),
                    model=model,
                    effort=effort,
                    arguments=[],
                    resolve_program=lambda name: name,
                )

    def test_description_model_overrides_use_coordinator_aliases(self):
        for model, alias in (
            ("gpt-5.6-sol", "sol"),
            ("gpt-5.6-luna", "luna"),
            ("gpt-5.6-terra", "terra"),
            ("gpt-6-astra", "astra"),
        ):
            with self.subTest(model=model):
                models = MODULE.stage_models([f"pr-description={model}"])
                command = MODULE.common.stage_command(
                    MODULE.STAGE_BY_NAME[MODULE.STAGE_DESCRIPTION],
                    target(),
                    model=models[MODULE.STAGE_DESCRIPTION],
                    effort="high",
                    arguments=[],
                )
                self.assertEqual(alias, command[command.index("--model") + 1])

    def test_unsupported_models_fail_before_stage_launch(self):
        for assignment, error in (
            ("pr-description=unknown-model", "does not support model"),
            ("pr-conflict-resolver=gpt-6-astra", "requires exactly model"),
            ("copilot-review-loop=gpt-6-astra", "requires exactly model"),
        ):
            with (
                self.subTest(assignment=assignment),
                self.assertRaisesRegex(MODULE.WorkflowError, error),
            ):
                MODULE.stage_models([assignment])

    def test_pipeline_position_is_one_run_and_two_sweeps(self):
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_CI]
        with mock.patch.object(MODULE, "stage_accepts_pipeline_position", return_value=True):
            arguments = MODULE.pipeline_arguments(entry, "run-1", 2)
        self.assertEqual(
            [
                "--pipeline-run",
                "run-1",
                "--pipeline-iteration",
                "2",
                "--pipeline-max-iterations",
                "2",
            ],
            arguments,
        )

    def test_pipeline_prompt_replaces_standalone_invocation_scope(self):
        prompt = MODULE.stage_prompt(
            target(),
            [
                "--pipeline-run",
                "run-1",
                "--pipeline-iteration",
                "1",
                "--pipeline-max-iterations",
                "2",
            ],
        )

        self.assertIn("exactly as written", prompt)
        self.assertIn("replaces standalone invocation scope", prompt)
        self.assertIn("Do not pass --new-invocation or --invocation-run", prompt)

    def test_the_conflict_stage_receives_the_run_identity(self):
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_CONFLICT]
        self.assertEqual(
            ["--pipeline-run", "run-1", "--pipeline-iteration", "2",
             "--pipeline-max-iterations", "2"],
            MODULE.pipeline_arguments(entry, "run-1", 2),
        )

    def test_conflict_stage_receives_the_invocation_state_path_explicitly(self):
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_CONFLICT]
        expected = MODULE.stage_state_path(entry, target(), "run-1")
        with mock.patch.object(
            MODULE.common,
            "path_image",
            return_value=str(Path("copilot.exe").resolve()),
        ):
            command = MODULE.stage_command(
                entry,
                target(),
                model=MODULE.stage_models(None)[MODULE.STAGE_CONFLICT],
                effort="high",
                run_id="run-1",
                sweep=1,
                conflict_strategy="merge",
            )

        self.assertEqual(str(expected), command[command.index("--state") + 1])
        self.assertEqual("merge", command[command.index("--strategy") + 1])
        self.assertEqual("pipeline", command[2])

    def test_ci_stage_runs_the_coordinator_directly_with_exact_state(self):
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_CI]
        expected = MODULE.stage_state_path(entry, target(), "run-1")
        with (
            mock.patch.object(
                MODULE.common,
                "stage_script_path",
                return_value=Path("installed-ci-fix-loop.py"),
            ),
            mock.patch.object(
                MODULE,
                "stage_accepts_pipeline_position",
                return_value=True,
            ),
        ):
            command = MODULE.stage_command(
                entry,
                target(),
                model=MODULE.stage_models(None)[MODULE.STAGE_CI],
                effort="high",
                run_id="run-1",
                sweep=1,
            )

        self.assertEqual(
            [
                MODULE.sys.executable,
                "installed-ci-fix-loop.py",
                "pipeline",
                "owner/repo#7",
                "--model",
                "sol",
                "--pipeline-run",
                "run-1",
                "--pipeline-iteration",
                "1",
                "--pipeline-max-iterations",
                "2",
                "--state",
                str(expected),
            ],
            command,
        )
        self.assertNotIn("copilot", command)

    def test_ci_live_progress_reads_the_action_and_pending_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "ci.json"
            state.write_text(
                json.dumps(
                    {
                        "run": {
                            "head_sha": "head1",
                            "decision": {
                                "action": "attribute",
                                "reason": "unattributed_failures",
                                "action_checks": ["check:build"],
                                "pending_checks": ["check:test"],
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            progress = MODULE.common.stage_live_progress(
                MODULE.STAGE_BY_NAME[MODULE.STAGE_CI],
                target(),
                state_for=lambda _entry, _target: state,
            )
        self.assertEqual("diagnosing", progress["phase"])
        self.assertEqual(["check:build"], progress["action_checks"])
        self.assertEqual(["check:test"], progress["pending_checks"])

    def test_live_progress_prefers_a_stage_s_structured_substate(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "review.json"
            state.write_text(
                json.dumps(
                    {
                        "stage_progress": {
                            "phase": "validating",
                            "detail": "targeted tests",
                            "observed_at": "2026-08-31T12:00:00Z",
                        }
                    }
                ),
                encoding="utf-8",
            )
            progress = MODULE.common.stage_live_progress(
                MODULE.STAGE_BY_NAME[MODULE.STAGE_COPILOT_REVIEW],
                target(),
                state_for=lambda _entry, _target: state,
            )
        self.assertEqual("validating", progress["phase"])
        self.assertEqual("targeted tests", progress["detail"])


class MarkerTest(unittest.TestCase):
    def status(
        self,
        stage: str,
        payload: dict,
        base_sha: str = BASE,
        run_id: str | None = None,
    ) -> dict:
        entry = MODULE.STAGE_BY_NAME[stage]
        with mock.patch.object(
            MODULE,
            "read_stage_status",
            return_value={
                "ok": True,
                "installed": True,
                "state": "state.json",
                "payload": {
                    "result": "ready",
                    "stage_outcome": "cleared",
                    **payload,
                },
            },
        ):
            return MODULE.inspect_stage(entry, target(), HEAD, base_sha, run_id)

    def policy_skip_payload(self) -> dict:
        observed_at = "2026-09-18T09:01:54Z"
        return {
            "stage_outcome": "skipped",
            "pr": {
                **target(),
                "state": "OPEN",
                "head_owner": "owner",
                "head_repo": "repo",
                "head_repository": "owner/repo",
                "head_branch": "feature",
                "head_sha": HEAD,
                "base_sha": BASE,
                "cross_repository": False,
            },
            "clean_at_head_sha": None,
            "github_mutation_policy": "source-only",
            "last_result": "source_only_review_not_applicable",
            "budget_scope": "pipeline",
            "pipeline_budget": {"run": PIPELINE_RUN},
            "policy_skip": {
                "policy": "source-only",
                "reason": "review_request_forbidden",
                "repo_name": "owner/repo",
                "number": 7,
                "head_repository": "owner/repo",
                "head_branch": "feature",
                "head_sha": HEAD,
                "base_sha": BASE,
                "viewer_login": "viewer",
                "pipeline_run": PIPELINE_RUN,
                "preflight_sha256": "e" * 64,
                "observed_at": observed_at,
            },
            "coordinator": {
                "status": "not_applicable",
                "detail": MODULE.common.SOURCE_ONLY_POLICY_SKIP_DETAIL,
                "head_sha": HEAD,
                "observed_at": observed_at,
            },
            "queue": None,
            "monitoring": None,
            "agent_task": None,
            "escalation": None,
            "terminal_exit": None,
            "thread_mutations": None,
            "history": [],
            "managed_task_history": [],
            "local_validation": [],
        }

    def test_reads_every_stage_marker(self):
        payloads = {
            MODULE.STAGE_CONFLICT: {
                "mergeable_at_head_sha": HEAD,
                "attempt": {"base_sha": BASE},
            },
            MODULE.STAGE_SELF_REVIEW: {
                "review": {"outcome": "clean", "clean_at_head_sha": HEAD}
            },
            MODULE.STAGE_COPILOT_REVIEW: {"clean_at_head_sha": HEAD},
            MODULE.STAGE_CI: {"clean_at_head_sha": HEAD},
            MODULE.STAGE_DESCRIPTION: {"validated_head_sha": HEAD},
        }
        for stage, payload in payloads.items():
            with self.subTest(stage=stage):
                self.assertTrue(self.status(stage, payload)["clear"])

    def test_minimized_stage_results_clear_without_hosted_report_fields(self):
        payloads = {
            MODULE.STAGE_CONFLICT: {
                "mergeable_at_head_sha": HEAD,
                "attempt": {
                    "base_sha": BASE,
                    "result_schema": {
                        "id": "github.copilot.conflict-result",
                        "version": 4,
                    },
                    "receipt_version": 3,
                    "status": "mergeable",
                },
            },
            MODULE.STAGE_SELF_REVIEW: {
                "review": {
                    "outcome": "clean",
                    "clean_at_head_sha": HEAD,
                    "report_version": 3,
                    "candidate_commits": 0,
                },
                "agent_task": {"status": "completed"},
            },
            MODULE.STAGE_CI: {
                "clean_at_head_sha": HEAD,
                "run": {
                    "head_sha": HEAD,
                    "status": "completed",
                    "report_version": 7,
                    "receipt_version": 3,
                },
            },
            MODULE.STAGE_DESCRIPTION: {
                "validated_head_sha": HEAD,
                "proposal": {"version": 3, "decision": "keep"},
                "agent_task": {"status": "completed"},
            },
        }
        forbidden = {
            "report": "arbitrary advisory prose with no machine meaning",
            "findings": [{"body": "model prose"}],
            "validation": [{"command": "./gradlew test", "passed": True}],
            "model_commit_mappings": {"model": "commit"},
            "report_identity": {"author": "model"},
            "changed_file_evidence": ["src/App.java"],
        }
        for stage, payload in payloads.items():
            with self.subTest(stage=stage):
                missing = self.status(stage, payload)
                arbitrary = self.status(stage, {**payload, **forbidden})
                self.assertTrue(missing["clear"])
                self.assertTrue(arbitrary["clear"])
                self.assertEqual(missing["clear_at_head_sha"], arbitrary["clear_at_head_sha"])
                self.assertTrue(
                    set(arbitrary["status"]).isdisjoint(forbidden)
                )

    def test_ci_candidate_publication_waits_for_exact_sha_github_green(self):
        published = self.status(
            MODULE.STAGE_CI,
            {
                "stage_outcome": None,
                "clean_at_head_sha": None,
                "run": {
                    "status": "published",
                    "published_head_sha": HEAD,
                },
            },
        )
        waiting = self.status(
            MODULE.STAGE_CI,
            {
                "stage_outcome": None,
                "clean_at_head_sha": None,
                "run": {
                    "status": "waiting",
                    "head_sha": HEAD,
                    "decision": {"reason": "pending_checks"},
                },
            },
        )
        green = self.status(
            MODULE.STAGE_CI,
            {
                "stage_outcome": "cleared",
                "clean_at_head_sha": HEAD,
                "run": {"status": "completed", "head_sha": HEAD},
            },
        )
        stale_green = self.status(
            MODULE.STAGE_CI,
            {
                "stage_outcome": "cleared",
                "clean_at_head_sha": NEXT_HEAD,
                "run": {"status": "completed", "head_sha": NEXT_HEAD},
            },
        )

        self.assertFalse(published["clear"])
        self.assertFalse(waiting["clear"])
        self.assertTrue(green["clear"])
        self.assertFalse(stale_green["clear"])
        self.assertEqual(
            "clearance_is_for_an_older_head", stale_green["reason"]
        )

    def test_source_only_description_proposal_never_counts_as_applied(self):
        previous_policy = MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY
        MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY = "source-only"
        try:
            result = self.status(
                MODULE.STAGE_DESCRIPTION,
                {
                    "stage_outcome": None,
                    "validated_head_sha": None,
                    "proposal": {
                        "version": 3,
                        "title": "Replacement title",
                        "body": "Replacement body",
                    },
                    "agent_task": {
                        "status": "completed",
                        "github_mutation_policy": "source-only",
                    },
                },
            )
        finally:
            MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY = previous_policy

        self.assertFalse(result["clear"])
        self.assertIsNone(result["clear_at_head_sha"])
        self.assertEqual(
            "source-only",
            result["status"]["agent_task"]["github_mutation_policy"],
        )

    def test_verified_source_only_review_skip_is_clear_without_review_marker(self):
        previous_policy = MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY
        MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY = "source-only"
        try:
            result = self.status(
                MODULE.STAGE_COPILOT_REVIEW,
                self.policy_skip_payload(),
                run_id=PIPELINE_RUN,
            )
        finally:
            MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY = previous_policy

        self.assertTrue(result["clear"])
        self.assertEqual("skipped", result["outcome"])
        self.assertEqual("policy_skip", result["clearance_kind"])
        self.assertEqual(HEAD, result["clear_at_head_sha"])
        self.assertIsNone(result["reason"])

    def test_review_skip_is_not_clear_under_allow_policy(self):
        result = self.status(
            MODULE.STAGE_COPILOT_REVIEW,
            self.policy_skip_payload(),
            run_id=PIPELINE_RUN,
        )

        self.assertFalse(result["clear"])
        self.assertEqual("policy_skip_not_verified", result["reason"])

    def test_review_skip_with_owned_work_is_not_clear(self):
        previous_policy = MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY
        MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY = "source-only"
        try:
            payload = self.policy_skip_payload()
            payload["queue"] = {"status": "active", "comments": []}
            result = self.status(
                MODULE.STAGE_COPILOT_REVIEW,
                payload,
                run_id=PIPELINE_RUN,
            )
        finally:
            MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY = previous_policy

        self.assertFalse(result["clear"])
        self.assertEqual("policy_skip_not_verified", result["reason"])

    def test_review_skip_rejects_cross_identity_and_forged_variants(self):
        previous_policy = MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY
        MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY = "source-only"
        cases = {}

        run = self.policy_skip_payload()
        run["policy_skip"]["pipeline_run"] = "2" * 32
        cases["run"] = run

        base = self.policy_skip_payload()
        base["policy_skip"]["base_sha"] = NEXT_BASE
        cases["base"] = base

        repo = self.policy_skip_payload()
        repo["policy_skip"]["repo_name"] = "other/repo"
        cases["repo"] = repo

        viewer = self.policy_skip_payload()
        viewer["policy_skip"]["viewer_login"] = ""
        cases["viewer"] = viewer

        forged = self.policy_skip_payload()
        forged["policy_skip"]["unexpected"] = True
        cases["forged"] = forged

        replayed = self.policy_skip_payload()
        replayed["pipeline_budget"]["run"] = "2" * 32
        cases["replayed"] = replayed

        clean = self.policy_skip_payload()
        clean["clean_at_head_sha"] = HEAD
        cases["clean marker"] = clean

        try:
            for name, payload in cases.items():
                with self.subTest(name=name):
                    result = self.status(
                        MODULE.STAGE_COPILOT_REVIEW,
                        payload,
                        run_id=PIPELINE_RUN,
                    )
                    self.assertFalse(result["clear"])
                    self.assertEqual("policy_skip_not_verified", result["reason"])
        finally:
            MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY = previous_policy

    def test_review_skip_requires_a_ready_status_envelope(self):
        previous_policy = MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY
        MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY = "source-only"
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_COPILOT_REVIEW]
        try:
            with mock.patch.object(
                MODULE,
                "read_stage_status",
                return_value={
                    "ok": False,
                    "installed": True,
                    "state": "state.json",
                    "reason": "status_not_ready",
                    "payload": self.policy_skip_payload(),
                },
            ):
                result = MODULE.inspect_stage(
                    entry,
                    target(),
                    HEAD,
                    BASE,
                    PIPELINE_RUN,
                )
        finally:
            MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY = previous_policy

        self.assertFalse(result["clear"])
        self.assertEqual("status_not_ready", result["reason"])

    def test_old_marker_is_not_clear(self):
        result = self.status(
            MODULE.STAGE_DESCRIPTION,
            {"validated_head_sha": NEXT_HEAD, "stage_outcome": "cleared"},
        )
        self.assertFalse(result["clear"])
        self.assertEqual("clearance_is_for_an_older_head", result["reason"])

    def test_conflict_clearance_for_an_older_base_is_not_clear(self):
        result = self.status(
            MODULE.STAGE_CONFLICT,
            {
                "mergeable_at_head_sha": HEAD,
                "attempt": {"base_sha": BASE},
            },
            NEXT_BASE,
        )
        self.assertFalse(result["clear"])
        self.assertEqual("clearance_is_for_an_older_base", result["reason"])
        self.assertEqual(BASE, result["clear_at_base_sha"])

    def test_missing_conflict_state_is_not_mislabeled_as_an_older_base(self):
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_CONFLICT]
        with mock.patch.object(
            MODULE,
            "read_stage_status",
            return_value={
                "ok": False,
                "installed": True,
                "state": "state.json",
                "payload": None,
                "reason": "no_state",
            },
        ):
            result = MODULE.inspect_stage(entry, target(), HEAD, BASE)

        self.assertFalse(result["clear"])
        self.assertEqual("no_state", result["reason"])
        self.assertIsNone(result["clear_at_base_sha"])

    def test_cap_is_incomplete_not_blocked(self):
        result = self.status(MODULE.STAGE_CI, {"stage_outcome": "carried"})
        self.assertFalse(result["clear"])
        self.assertEqual("carried", result["reason"])

    def test_preserves_stage_status_details(self):
        escalation = {
            "reason": "unfixable_failure",
            "detail": "library defect",
            "checks": ["check:test"],
        }
        result = self.status(
            MODULE.STAGE_CI,
            {
                "stage_outcome": "escalated",
                "escalation": escalation,
            },
        )
        self.assertEqual(escalation, result["status"]["escalation"])
        self.assertNotIn("validation", result["status"])
        self.assertNotIn("verdicts", result["status"])

    def test_ci_stage_command_never_runs_a_local_build(self):
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_CI]
        command = MODULE.common.stage_command(
            entry,
            target(),
            model="gpt-5.6-sol",
            effort="high",
            arguments=["--pipeline-run", PIPELINE_RUN],
        )

        self.assertEqual("pipeline", command[2])
        lowered = " ".join(command).lower()
        for forbidden in ("gradle", "mvn", "maven", "pytest", " test"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)

    def test_preserves_agent_task_recovery_details(self):
        agent_task = {
            "status": "failed",
            "error": "start Agent Task failed with HTTP 409",
            "recovery_command": "python helper.py agent-task --resume",
            "recovery_files": ["result.json"],
        }
        result = self.status(
            MODULE.STAGE_SELF_REVIEW,
            {
                "stage_outcome": None,
                "agent_task": agent_task,
            },
        )
        self.assertEqual(agent_task, result["status"]["agent_task"])


class InvocationStateIsolationTest(unittest.TestCase):
    RUN_ID = "419b3efaa0754a18a27235f3bcc6b8ed"
    HEAD = "028894b47c864dc5ea068751017788d3e6966740"
    BASE = "737354d8" + ("0" * 32)
    OLD_HEAD = "a48b898a" + ("0" * 32)

    def test_20075_reads_only_fresh_stage_state(self):
        stale_owner = "e9a8f3877a7e86e16973f9ebe01caaa2"
        stale_task = "ccfbeb8b-3fef-4fff-9898-7be4d3dda172"
        stale_report = "043c1f18" + ("0" * 56)
        canonical_payloads = {
            MODULE.STAGE_CONFLICT: {
                "result": "ready",
                "stage_outcome": "cleared",
                "mergeable_at_head_sha": self.OLD_HEAD,
                "attempt": {"base_sha": self.BASE},
            },
            MODULE.STAGE_COPILOT_REVIEW: {
                "result": "ready",
                "stage_outcome": "cleared",
                "clean_at_head_sha": self.HEAD,
            },
            MODULE.STAGE_SELF_REVIEW: {
                "result": "ready",
                "agent_task": {
                    "status": "failed",
                    "run_id": stale_owner,
                    "task_id": stale_task,
                    "report_sha256": stale_report,
                },
            },
        }
        invocation_payloads = {
            MODULE.STAGE_CONFLICT: {
                "result": "ready",
                "stage_outcome": "cleared",
                "mergeable_at_head_sha": self.HEAD,
                "attempt": {"base_sha": self.BASE},
            },
            MODULE.STAGE_COPILOT_REVIEW: {
                "result": "ready",
                "stage_outcome": "review_required",
                "agent_task": {
                    "status": "completed",
                    "session_id": "e1313855-fresh",
                },
            },
        }
        read_paths = []

        def read_status(entry, selected, *, script_for=None, state_for):
            del script_for
            path = state_for(entry, selected)
            read_paths.append(path)
            canonical = MODULE.stage_state_path(entry, selected)
            invocation = MODULE.stage_state_path(entry, selected, self.RUN_ID)
            if path == canonical:
                payload = canonical_payloads[entry["stage"]]
            elif path == invocation:
                payload = invocation_payloads.get(entry["stage"])
            else:
                self.fail(f"unexpected stage state path: {path}")
            if payload is None:
                return {
                    "ok": False,
                    "installed": True,
                    "state": str(path),
                    "payload": None,
                    "reason": "no_state",
                }
            return {
                "ok": True,
                "installed": True,
                "state": str(path),
                "payload": payload,
            }

        with mock.patch.object(
            MODULE.common, "read_stage_status", side_effect=read_status
        ):
            results = {
                entry["stage"]: MODULE.inspect_stage_for_run(
                    entry,
                    target(),
                    self.HEAD,
                    self.BASE,
                    self.RUN_ID,
                )
                for entry in MODULE.STAGES[:3]
            }

        self.assertTrue(results[MODULE.STAGE_CONFLICT]["clear"])
        self.assertEqual(
            self.HEAD,
            results[MODULE.STAGE_CONFLICT]["clear_at_head_sha"],
        )
        self.assertFalse(results[MODULE.STAGE_COPILOT_REVIEW]["clear"])
        self.assertEqual(
            "review_required",
            results[MODULE.STAGE_COPILOT_REVIEW]["outcome"],
        )
        self.assertEqual(
            "e1313855-fresh",
            results[MODULE.STAGE_COPILOT_REVIEW]["status"]["agent_task"][
                "session_id"
            ],
        )
        self.assertEqual("no_state", results[MODULE.STAGE_SELF_REVIEW]["reason"])
        self.assertEqual({}, results[MODULE.STAGE_SELF_REVIEW]["status"])
        self.assertNotIn(stale_owner, json.dumps(results))
        self.assertNotIn(stale_task, json.dumps(results))
        self.assertNotIn(stale_report, json.dumps(results))
        self.assertEqual(
            [
                MODULE.stage_state_path(entry, target(), self.RUN_ID)
                for entry in MODULE.STAGES[:3]
            ],
            read_paths,
        )
        self.assertTrue(
            all("--invocation-eaafa0037567822b.json" in str(path) for path in read_paths)
        )

    def read_status_envelope(self, payload: dict) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            script = directory / "stage.py"
            state = directory / "state.json"
            script.write_text("", encoding="utf-8")
            state.write_text("{}", encoding="utf-8")
            completed = subprocess.CompletedProcess(
                ["python", str(script)],
                0,
                json.dumps(payload),
                "",
            )
            with mock.patch.object(MODULE.common, "run", return_value=completed):
                return MODULE.common.read_stage_status(
                    MODULE.STAGES[0],
                    target(),
                    script_for=lambda _entry: script,
                    state_for=lambda _entry, _target: state,
                )

    def test_status_envelope_cannot_name_another_invocation_state(self):
        result = self.read_status_envelope(
            {
                "result": "ready",
                "state": "shared-state.json",
                "pr": {
                    "number": target()["number"],
                    "repo_name": target()["repo_name"],
                },
            }
        )

        self.assertFalse(result["ok"])
        self.assertEqual("status_state_mismatch", result["reason"])
        self.assertIsNone(result["payload"])
        self.assertIn(
            result["reason"],
            MODULE.UNAVAILABLE_STATUS_REASONS,
        )

    def test_status_envelope_cannot_name_another_pull_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            state.write_text("{}", encoding="utf-8")
            script = Path(temporary) / "stage.py"
            script.write_text("", encoding="utf-8")
            completed = subprocess.CompletedProcess(
                ["python", str(script)],
                0,
                json.dumps(
                    {
                        "result": "ready",
                        "state": str(state),
                        "pr": {
                            "number": 20075,
                            "repo_name": "open-telemetry/"
                            "opentelemetry-java-instrumentation",
                        },
                    }
                ),
                "",
            )
            with mock.patch.object(MODULE.common, "run", return_value=completed):
                result = MODULE.common.read_stage_status(
                    MODULE.STAGES[0],
                    target(),
                    script_for=lambda _entry: script,
                    state_for=lambda _entry, _target: state,
                )

        self.assertFalse(result["ok"])
        self.assertEqual("status_identity_mismatch", result["reason"])
        self.assertIsNone(result["payload"])
        self.assertIn(
            result["reason"],
            MODULE.UNAVAILABLE_STATUS_REASONS,
        )

    def test_status_accepts_the_resolved_form_of_the_same_state_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "nested").mkdir()
            state = directory / "nested" / ".." / "state.json"
            state.write_text("{}", encoding="utf-8")
            script = directory / "stage.py"
            script.write_text("", encoding="utf-8")
            completed = subprocess.CompletedProcess(
                ["python", str(script)],
                0,
                json.dumps(
                    {
                        "result": "ready",
                        "state": str(state.resolve()),
                        "pr": {
                            "number": target()["number"],
                            "repo_name": target()["repo_name"].upper(),
                        },
                    }
                ),
                "",
            )
            with mock.patch.object(MODULE.common, "run", return_value=completed):
                result = MODULE.common.read_stage_status(
                    MODULE.STAGES[0],
                    target(),
                    script_for=lambda _entry: script,
                    state_for=lambda _entry, _target: state,
                )

        self.assertTrue(result["ok"])

    def test_preidentity_ci_coordinator_failure_keeps_its_exact_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state = directory / "state.json"
            state.write_text("{}", encoding="utf-8")
            script = directory / "stage.py"
            script.write_text("", encoding="utf-8")
            detail = "GitHub check lookup failed before PR identity was recorded"
            payload = {
                "result": "ready",
                "state": str(state),
                "pr": None,
                "coordinator": {"status": "blocked", "detail": detail},
                "escalation": {
                    "reason": "coordinator_error",
                    "detail": detail,
                },
            }
            completed = subprocess.CompletedProcess(
                ["python", str(script)],
                0,
                json.dumps(payload),
                "",
            )
            with mock.patch.object(MODULE.common, "run", return_value=completed):
                status = MODULE.common.read_stage_status(
                    MODULE.STAGE_BY_NAME[MODULE.STAGE_CI],
                    target(),
                    script_for=lambda _entry: script,
                    state_for=lambda _entry, _target: state,
                )
            stage = MODULE.common.inspect_stage(
                MODULE.STAGE_BY_NAME[MODULE.STAGE_CI],
                target(),
                self.HEAD,
                self.BASE,
                read_status=lambda _entry, _target: status,
            )

        self.assertTrue(status["ok"])
        self.assertEqual(
            ("stage_coordinator_error", detail),
            MODULE.stage_blocker(stage, after_launch=False),
        )


class RunStageStateIsolationTest(unittest.TestCase):
    def test_every_stage_invokes_the_installed_coordinator_without_a_model_wrapper(self):
        for entry in MODULE.STAGES:
            with self.subTest(stage=entry["stage"]), mock.patch.object(
                MODULE.common, "stage_script_path", return_value=Path("installed.py")
            ):
                command = MODULE.common.stage_command(
                    entry, target(), model=entry["model"], effort="high",
                    arguments=["--state", "state with spaces.json"],
                    repo_root=Path("repo with spaces"),
                    resolve_program=lambda _name: self.fail("model wrapper launched"),
                )
                self.assertEqual(
                    [MODULE.sys.executable, "installed.py", "pipeline",
                     "owner/repo#7", "--model", "sol"],
                    command[:6],
                )
                self.assertEqual(
                    "state with spaces.json", command[command.index("--state") + 1]
                )
                self.assertEqual(
                    "repo with spaces", command[command.index("--repo-root") + 1]
                )

    def test_coordinator_failure_is_not_translated_from_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "coordinator.py"
            script.write_text(
                "print('{\"result\": \"success\"}')\nraise SystemExit(1)\n",
                encoding="utf-8",
            )
            result = MODULE.common.run_foreground(
                [MODULE.sys.executable, str(script)],
                cwd=root, log_path=root / "stage.log",
            )
        self.assertEqual(1, result["returncode"])

    def test_ci_progress_reads_the_current_pipeline_state(self):
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_CI]
        seen = []

        def progress(_entry, selected, *, state_for):
            seen.append(state_for(entry, selected))
            return None

        def monitored(_command, *, cwd, log_path, progress):
            del cwd, log_path
            progress()
            return {
                "returncode": 0,
                "log_path": "ci.log",
                "started_at": "start",
                "ended_at": "end",
            }

        with (
            mock.patch.object(MODULE, "stage_command", return_value=["copilot"]),
            mock.patch.object(MODULE, "stage_log_path", return_value=Path("ci.log")),
            mock.patch.object(
                MODULE.common, "stage_live_progress", side_effect=progress
            ),
            mock.patch.object(MODULE.common, "run_monitored", side_effect=monitored),
        ):
            MODULE.run_stage(
                entry,
                target(),
                Path("C:/repo"),
                model="gpt-5.6-sol",
                effort="high",
                run_id=InvocationStateIsolationTest.RUN_ID,
                sweep=1,
            )

        self.assertEqual(
            [
                MODULE.stage_state_path(
                    entry,
                    target(),
                    InvocationStateIsolationTest.RUN_ID,
                )
            ],
            seen,
        )


class SweepTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path("C:/repo")
        self.sync_heads = [HEAD]
        self.base_sha = BASE
        self.clear_at = {stage: None for stage in MODULE.STAGE_NAMES}
        self.clear_base_at = None
        self.completed: set[str] = set()
        self.attempt_ids = {MODULE.STAGE_CONFLICT: "old-attempt"}
        self.launched: list[tuple[str, int]] = []
        self.events: list[dict] = []

        self.patches = [
            mock.patch.object(MODULE, "read_pull_request", side_effect=self.read_pr),
            mock.patch.object(MODULE, "sync_worktree", side_effect=self.sync),
            mock.patch.object(MODULE, "run_stage", side_effect=self.run_stage),
            mock.patch.object(MODULE, "settle_after_stage", side_effect=self.settle),
            mock.patch.object(MODULE, "inspect_stage", side_effect=self.inspect),
            mock.patch.object(MODULE, "inspect_stages", side_effect=self.inspect_all),
            mock.patch.object(
                MODULE, "snapshot_pr_commits", side_effect=self.snapshot_commits
            ),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def read_pr(self, _target):
        return {**pull_request(self.sync_heads[-1]), "base_sha": self.base_sha}

    def sync(self, _repo, _target, _pr, *, known_safe_head):
        return {
            "result": "ready",
            "head_sha": self.sync_heads[-1],
            "changed": known_safe_head not in (None, self.sync_heads[-1]),
        }

    def run_stage(
        self,
        entry,
        _target,
        _repo,
        *,
        model,
        effort,
        run_id,
        sweep,
        conflict_strategy,
        report=None,
    ):
        self.launched.append((entry["stage"], sweep))
        self.clear_at[entry["stage"]] = self.sync_heads[-1]
        if entry["stage"] == MODULE.STAGE_CONFLICT:
            self.clear_base_at = self.base_sha
        return {
            "returncode": 0,
            "log_path": f"{sweep}-{entry['stage']}.log",
            "started_at": "start",
            "ended_at": "end",
        }

    def snapshot_commits(self, _target):
        head = self.sync_heads[-1]
        return {
            "commits": [
                {
                    "sha": head,
                    "title": f"Commit {head[0]}",
                    "url": f"https://github.com/owner/repo/pull/7/commits/{head}",
                }
            ]
        }

    def settle(self, _repo, _target, *, started_head_sha):
        return {
            "result": "ready",
            "head_sha": self.sync_heads[-1],
            "changed": self.sync_heads[-1] != started_head_sha,
        }

    def inspect(self, entry, _target, head, base_sha, run_id=None):
        del run_id
        if entry["stage"] in self.completed:
            return {
                **uncleared_stage(entry["stage"]),
                "outcome": "completed",
                "reason": "completed",
                "status": {
                    "attempt": {"id": self.attempt_ids[MODULE.STAGE_CONFLICT]}
                },
            }
        if self.clear_at[entry["stage"]] == head and (
            entry["stage"] != MODULE.STAGE_CONFLICT
            or self.clear_base_at == base_sha
        ):
            return clear_stage(entry["stage"], head)
        return uncleared_stage(entry["stage"])

    def inspect_all(self, _target, head, base_sha, run_id=None):
        del run_id
        return [
            self.inspect(entry, _target, head, base_sha) for entry in MODULE.STAGES
        ]

    def execute(self, *, conflict_strategy="auto"):
        return MODULE.run_pipeline(
            target(),
            self.repo,
            models=MODULE.stage_models(None),
            effort="high",
            conflict_strategy=conflict_strategy,
            report=self.events.append,
        )

    def test_one_sweep_runs_all_stages_in_order(self):
        result = self.execute()
        self.assertEqual("complete", result["result"])
        self.assertEqual(1, result["sweeps"])
        self.assertEqual(
            [(stage, 1) for stage in MODULE.STAGE_NAMES],
            self.launched,
        )

    def test_ready_for_review_pull_request_runs_all_stages(self):
        with mock.patch.object(
            MODULE,
            "read_pull_request",
            return_value={**pull_request(), "is_draft": False},
        ):
            result = self.execute()
        self.assertEqual("complete", result["result"])
        self.assertEqual(
            [(stage, 1) for stage in MODULE.STAGE_NAMES], self.launched
        )

    def test_every_scheduler_stage_read_uses_the_current_pipeline_run(self):
        calls = []
        original = MODULE.inspect_stage_for_run

        def inspect(entry, selected, head, base, run_id):
            calls.append((entry["stage"], run_id))
            return original(entry, selected, head, base, run_id)

        with mock.patch.object(
            MODULE, "inspect_stage_for_run", side_effect=inspect
        ):
            result = self.execute()

        self.assertTrue(calls)
        self.assertEqual(
            {result["run_id"]},
            {run_id for _stage, run_id in calls},
        )
        self.assertEqual(
            [(stage, result["run_id"]) for stage in MODULE.STAGE_NAMES for _ in range(2)],
            calls,
        )

    @unittest.skip("failed invocations are abandoned rather than replaced")
    def test_replaces_failed_preflight_when_no_agent_task_was_created(self):
        original = self.inspect
        inspections = 0

        def retained_failure(entry, *arguments):
            nonlocal inspections
            if entry["stage"] == MODULE.STAGE_CONFLICT and inspections == 0:
                inspections += 1
                return {
                    **uncleared_stage(entry["stage"]),
                    "status": {
                        "agent_task": {
                            "status": "failed",
                            "task_id": None,
                            "task_id_status": "not_created",
                            "error": {
                                "code": "stale_target",
                                "message": "a checked-out branch is required",
                            },
                        }
                    },
                }
            return original(entry, *arguments)

        MODULE.inspect_stage.side_effect = retained_failure

        result = self.execute()

        self.assertEqual("complete", result["result"])
        self.assertEqual(
            [(stage, 1) for stage in MODULE.STAGE_NAMES],
            self.launched,
        )

    def test_failed_not_created_invocation_blocks_without_replacement(self):
        stage_result = {
            **uncleared_stage(MODULE.STAGE_CONFLICT),
            "status": {
                "agent_task": {
                    "status": "failed",
                    "task_id": None,
                    "task_id_status": "not_created",
                    "error": {
                        "code": "stale_target",
                        "message": "a checked-out branch is required",
                    },
                }
            },
        }
        self.assertEqual(
            "stage_invocation_abandoned",
            MODULE.stage_blocker(
                stage_result, after_launch=False, conflict_strategy="auto"
            )[0],
        )
        self.assertEqual(
            "stage_invocation_abandoned",
            MODULE.stage_blocker(
                stage_result,
                after_launch=True,
                conflict_strategy="auto",
            )[0],
        )
        normalization = copy.deepcopy(stage_result)
        normalization["status"]["agent_task"]["status"] = "normalization_required"
        normalization["status"]["agent_task"]["error"] = {
            "code": "native_stack_normalization_required",
            "message": "native stack member requires explicit owner normalization",
        }
        self.assertEqual(
            "stage_invocation_abandoned",
            MODULE.stage_blocker(normalization, after_launch=False)[0],
        )
        self.assertEqual(
            "stage_invocation_abandoned",
            MODULE.stage_blocker(normalization, after_launch=True)[0],
        )
        for field, value in (
            ("task_id", "task-1"),
            ("task_id_status", "created"),
            ("status", "interrupted"),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(stage_result)
                changed["status"]["agent_task"][field] = value
                self.assertEqual(
                    "stage_invocation_abandoned",
                    MODULE.stage_blocker(
                        changed,
                        after_launch=False,
                        conflict_strategy="auto",
                    )[0],
                )
        changed = copy.deepcopy(stage_result)
        del changed["status"]["agent_task"]["task_id"]
        self.assertEqual(
            "stage_invocation_abandoned",
            MODULE.stage_blocker(changed, after_launch=False)[0],
        )
        changed = copy.deepcopy(stage_result)
        changed["stage"] = MODULE.STAGE_COPILOT_REVIEW
        self.assertEqual(
            "stage_invocation_abandoned",
            MODULE.stage_blocker(changed, after_launch=False)[0],
        )

    @unittest.skip("legacy owner replacement is intentionally unavailable")
    def test_replaces_exact_legacy_malformed_self_review_owner_prelaunch(self):
        original = self.inspect
        inspections = 0

        def retained_failure(entry, *arguments):
            nonlocal inspections
            if entry["stage"] == MODULE.STAGE_SELF_REVIEW and inspections == 0:
                inspections += 1
                return self.legacy_malformed_self_review_stage()
            return original(entry, *arguments)

        MODULE.inspect_stage.side_effect = retained_failure

        result = self.execute()

        self.assertEqual("complete", result["result"])
        self.assertEqual(
            [(stage, 1) for stage in MODULE.STAGE_NAMES],
            self.launched,
        )

    def legacy_malformed_self_review_stage(self):
        request_id = "legacy-request-1"
        owner = "legacy-owner"
        source_head = "1" * 40
        return {
            **uncleared_stage(MODULE.STAGE_SELF_REVIEW),
            "status": {
                "agent_task": {
                    "status": "failed",
                    "run_id": owner,
                    "model": "gpt-5.6-sol",
                    "policy": "marketplace-agent-worker@4",
                    "error": "Agent Task result has an unsupported schema or fields",
                },
                "review": {
                    "id": f"pr-7-agent-task-{owner}",
                    "status": "active",
                    "head_sha": source_head,
                },
                "malformed_owner_recovery": {
                    "status": "ready",
                    "kind": "legacy_v1_validation_incomplete",
                    "run_id": owner,
                    "task_id": "legacy-task-1",
                    "request_id": request_id,
                    "source_head_sha": source_head,
                    "direct_base_sha": "2" * 40,
                    "generated_head_sha": "3" * 40,
                    "prompt_sha256": "4" * 64,
                    "result_sha256": "5" * 64,
                    "report_path": (
                        f".github/agent-task-reports/{request_id}.md"
                    ),
                    "validation_path": (
                        f".github/agent-task-validations/{request_id}.json"
                    ),
                    "remaining_iterations": 5,
                },
            },
        }

    def test_legacy_malformed_self_review_owner_blocks_without_replacement(self):
        stage_result = self.legacy_malformed_self_review_stage()
        self.assertEqual(
            "stage_invocation_abandoned",
            MODULE.stage_blocker(stage_result, after_launch=False)[0],
        )
        self.assertEqual(
            "stage_invocation_abandoned",
            MODULE.stage_blocker(stage_result, after_launch=True)[0],
        )
        mutations = {
            "wrong_stage": lambda value: value.update(
                stage=MODULE.STAGE_COPILOT_REVIEW
            ),
            "owner": lambda value: value["status"][
                "malformed_owner_recovery"
            ].update(run_id="other-owner"),
            "model": lambda value: value["status"]["agent_task"].update(
                model="gpt-6-astra"
            ),
            "policy": lambda value: value["status"]["agent_task"].update(
                policy="marketplace-agent-apply-report-worker@3"
            ),
            "generated_head": lambda value: value["status"][
                "malformed_owner_recovery"
            ].update(generated_head_sha="not-a-sha"),
            "path": lambda value: value["status"][
                "malformed_owner_recovery"
            ].update(
                validation_path=(
                    ".github/agent-task-validations/other-request.json"
                )
            ),
            "review_head": lambda value: value["status"]["review"].update(
                head_sha="6" * 40
            ),
            "ambiguous": lambda value: value["status"][
                "malformed_owner_recovery"
            ].update(extra=True),
            "exhausted": lambda value: value["status"][
                "malformed_owner_recovery"
            ].update(remaining_iterations=0),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(stage_result)
                mutate(changed)
                self.assertEqual(
                    "stage_invocation_abandoned",
                    MODULE.stage_blocker(changed, after_launch=False)[0],
                )
        active = copy.deepcopy(stage_result)
        active["status"]["agent_task"]["status"] = "running"
        self.assertEqual(
            "stage_still_active",
            MODULE.stage_blocker(active, after_launch=False)[0],
        )

    def test_reports_sweep_and_stage_progress_in_order(self):
        self.execute()
        self.assertEqual(
            [
                "pipeline_started",
                "sweep_started",
                *(["stage_started", "stage_finished"] * len(MODULE.STAGES)),
                "sweep_finished",
            ],
            [event["event"] for event in self.events],
        )
        stage_events = [
            event["stage"]
            for event in self.events
            if event["event"] == "stage_started"
        ]
        self.assertEqual(list(MODULE.STAGE_NAMES), stage_events)

    def test_capped_stage_does_not_block_later_stages(self):
        original = self.run_stage

        def cap_self_review(entry, *args, **kwargs):
            result = original(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_SELF_REVIEW:
                self.clear_at[entry["stage"]] = None
            return result

        MODULE.run_stage.side_effect = cap_self_review
        result = self.execute()
        self.assertEqual("incomplete", result["result"])
        self.assertEqual(1, result["sweeps"])
        self.assertEqual(
            [(stage, 1) for stage in MODULE.STAGE_NAMES],
            self.launched,
        )

    def test_pipeline_stops_when_a_launched_stage_still_owns_failed_task_state(self):
        self.clear_at[MODULE.STAGE_CONFLICT] = HEAD
        self.clear_base_at = BASE
        original = self.inspect
        copilot_inspections = 0

        def failed_after_launch(entry, *args):
            nonlocal copilot_inspections
            if entry["stage"] != MODULE.STAGE_COPILOT_REVIEW:
                return original(entry, *args)
            copilot_inspections += 1
            if copilot_inspections == 1:
                return uncleared_stage(entry["stage"], None)
            return {
                **uncleared_stage(entry["stage"], None),
                "status": {
                    "agent_task": {
                        "status": "failed",
                        "error": {
                            "code": "agent_task_http_409",
                            "message": (
                                "user or repo does not have Copilot coding agent enabled"
                            ),
                        },
                        "recovery_command": "python review.py agent-task --resume",
                        "recovery_files": ["prompt.txt", "result.json"],
                    },
                    "queue": {
                        "id": "pr-347",
                        "status": "active",
                        "comments": [{"id": 4018692884, "status": "pending"}],
                    },
                },
            }

        MODULE.inspect_stage.side_effect = failed_after_launch
        result = self.execute()

        self.assertEqual("blocked", result["result"])
        self.assertEqual("stage_invocation_abandoned", result["reason"])
        self.assertEqual(MODULE.STAGE_COPILOT_REVIEW, result["stage"])
        self.assertIn("agent_task_http_409", result["detail"])
        self.assertEqual(
            [(MODULE.STAGE_COPILOT_REVIEW, 1)],
            self.launched,
        )
        self.assertEqual(
            "python review.py agent-task --resume",
            result["stage_result"]["status"]["agent_task"]["recovery_command"],
        )

    def test_pipeline_stops_for_native_stack_normalization(self):
        original = self.inspect
        conflict_inspections = 0

        def normalization_after_launch(entry, *args):
            nonlocal conflict_inspections
            if entry["stage"] != MODULE.STAGE_CONFLICT:
                return original(entry, *args)
            conflict_inspections += 1
            if conflict_inspections == 1:
                return uncleared_stage(entry["stage"], None)
            return {
                **uncleared_stage(entry["stage"], None),
                "status": {
                    "agent_task": {
                        "status": "normalization_required",
                        "task_id": None,
                        "task_id_status": "not_created",
                        "error": {
                            "code": "native_stack_normalization_required",
                            "message": "native stack member requires normalization",
                        },
                        "normalization_sha256": "a" * 64,
                    },
                },
            }

        MODULE.inspect_stage.side_effect = normalization_after_launch
        result = self.execute()

        self.assertEqual("blocked", result["result"])
        self.assertEqual("stage_invocation_abandoned", result["reason"])
        self.assertEqual(MODULE.STAGE_CONFLICT, result["stage"])
        self.assertIn("native_stack_normalization_required", result["detail"])
        self.assertEqual([(MODULE.STAGE_CONFLICT, 1)], self.launched)

    def test_restart_does_not_duplicate_a_stage_with_an_active_task(self):
        self.clear_at[MODULE.STAGE_CONFLICT] = HEAD
        self.clear_base_at = BASE
        original = self.inspect

        def active_before_launch(entry, *args):
            if entry["stage"] != MODULE.STAGE_COPILOT_REVIEW:
                return original(entry, *args)
            return {
                **uncleared_stage(entry["stage"], None),
                "status": {
                    "agent_task": {"status": "running"},
                    "queue": {"id": "pr-347", "status": "active"},
                },
            }

        MODULE.inspect_stage.side_effect = active_before_launch
        result = self.execute()

        self.assertEqual("blocked", result["result"])
        self.assertEqual("stage_still_active", result["reason"])
        self.assertEqual([], self.launched)

    def test_stage_that_returns_without_state_blocks_before_the_next_stage(self):
        inspections = 0
        original = self.inspect

        def missing_after_launch(entry, *args):
            nonlocal inspections
            if entry["stage"] != MODULE.STAGE_CONFLICT:
                return original(entry, *args)
            inspections += 1
            if inspections == 1:
                return uncleared_stage(entry["stage"], None)
            return {
                **uncleared_stage(entry["stage"], None),
                "reason": "no_state",
            }

        MODULE.inspect_stage.side_effect = missing_after_launch
        result = self.execute()

        self.assertEqual("blocked", result["result"])
        self.assertEqual("stage_did_not_record_state", result["reason"])
        self.assertIn("Expected state path: state.json.", result["detail"])
        self.assertEqual([(MODULE.STAGE_CONFLICT, 1)], self.launched)

    def test_unreadable_stage_status_blocks_before_launch(self):
        MODULE.inspect_stage.side_effect = None
        MODULE.inspect_stage.return_value = {
            **uncleared_stage(MODULE.STAGE_CONFLICT, None),
            "reason": "status_timeout",
            "detail": "status command timed out",
        }
        result = self.execute()

        self.assertEqual("blocked", result["result"])
        self.assertEqual("stage_status_unavailable", result["reason"])
        self.assertEqual(
            "status command timed out Expected state path: state.json.",
            result["detail"],
        )
        self.assertEqual([], self.launched)

    def test_pre_identity_ci_coordinator_error_blocks_with_exact_detail(self):
        for stage in (
            MODULE.STAGE_CONFLICT,
            MODULE.STAGE_COPILOT_REVIEW,
            MODULE.STAGE_SELF_REVIEW,
        ):
            self.clear_at[stage] = HEAD
        self.clear_base_at = BASE
        original = self.inspect
        detail = (
            "gh api repos/open-telemetry/shared-workflows/actions/jobs/"
            "104705281292 failed (1): gh: Not Found (HTTP 404)"
        )

        def blocked_ci(entry, *args):
            if entry["stage"] != MODULE.STAGE_CI:
                return original(entry, *args)
            return {
                **uncleared_stage(entry["stage"], None),
                "status": {
                    "coordinator": {"status": "blocked", "detail": detail},
                    "escalation": {
                        "reason": "coordinator_error",
                        "detail": detail,
                    },
                },
            }

        MODULE.inspect_stage.side_effect = blocked_ci
        result = self.execute()

        self.assertEqual("blocked", result["result"])
        self.assertEqual("stage_coordinator_error", result["reason"])
        self.assertEqual(MODULE.STAGE_CI, result["stage"])
        self.assertEqual(detail, result["detail"])
        self.assertEqual([], self.launched)

    def test_head_change_runs_a_second_sweep_for_stale_stages(self):
        first_sweep_calls = 0

        def move_head(entry, *args, **kwargs):
            nonlocal first_sweep_calls
            first_sweep_calls += 1
            if first_sweep_calls == 3:
                self.sync_heads.append(NEXT_HEAD)
                for stage in (MODULE.STAGE_CONFLICT, MODULE.STAGE_COPILOT_REVIEW):
                    self.clear_at[stage] = None
            result = self.run_stage(entry, *args, **kwargs)
            return result

        MODULE.run_stage.side_effect = move_head
        result = self.execute()
        self.assertEqual("complete", result["result"])
        self.assertEqual(2, result["sweeps"])
        self.assertIn((MODULE.STAGE_CONFLICT, 2), self.launched)
        self.assertIn((MODULE.STAGE_COPILOT_REVIEW, 2), self.launched)
        self.assertNotIn((MODULE.STAGE_SELF_REVIEW, 2), self.launched)
        self_review_run = next(
            run
            for run in result["runs"]
            if run["stage"] == MODULE.STAGE_SELF_REVIEW and run["sweep"] == 1
        )
        self.assertEqual(
            [
                {
                    "sha": NEXT_HEAD,
                    "title": "Commit b",
                    "url": (
                        "https://github.com/owner/repo/pull/7/commits/"
                        f"{NEXT_HEAD}"
                    ),
                }
            ],
            self_review_run["published_commits"],
        )

    def test_base_change_reruns_conflict_stage_without_a_head_change(self):
        original = self.run_stage

        def move_base(entry, *args, **kwargs):
            result = original(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_DESCRIPTION:
                self.base_sha = NEXT_BASE
            return result

        MODULE.run_stage.side_effect = move_base
        result = self.execute()

        self.assertEqual("complete", result["result"])
        self.assertEqual(2, result["sweeps"])
        self.assertEqual(
            [
                (MODULE.STAGE_CONFLICT, 1),
                (MODULE.STAGE_CONFLICT, 2),
            ],
            [
                launch
                for launch in self.launched
                if launch[0] == MODULE.STAGE_CONFLICT
            ],
        )

    def test_second_sweep_retries_an_uncleared_stage(self):
        def move_and_stall(entry, *args, **kwargs):
            if entry["stage"] == MODULE.STAGE_COPILOT_REVIEW:
                self.sync_heads.append(NEXT_HEAD)
                self.clear_at[MODULE.STAGE_CONFLICT] = None
                self.clear_at[MODULE.STAGE_SELF_REVIEW] = None
            result = self.run_stage(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_COPILOT_REVIEW:
                self.clear_at[entry["stage"]] = None
            return result

        MODULE.run_stage.side_effect = move_and_stall
        result = self.execute()
        launches = [
            item for item in self.launched if item[0] == MODULE.STAGE_COPILOT_REVIEW
        ]
        self.assertEqual(
            [
                (MODULE.STAGE_COPILOT_REVIEW, 1),
                (MODULE.STAGE_COPILOT_REVIEW, 2),
            ],
            launches,
        )
        self.assertEqual("incomplete", result["result"])

    def test_second_sweep_does_not_relaunch_a_completed_conflict_resolution(self):
        original = self.run_stage

        def complete_conflict_and_move_head(entry, *args, **kwargs):
            result = original(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_CONFLICT:
                self.clear_at[entry["stage"]] = None
                self.completed.add(entry["stage"])
                self.attempt_ids[entry["stage"]] = "current-attempt"
                if kwargs["sweep"] == 1:
                    self.sync_heads.append(NEXT_HEAD)
            return result

        MODULE.run_stage.side_effect = complete_conflict_and_move_head
        result = self.execute()

        self.assertEqual("incomplete", result["result"])
        self.assertEqual(
            [(MODULE.STAGE_CONFLICT, 1)],
            [
                launch
                for launch in self.launched
                if launch[0] == MODULE.STAGE_CONFLICT
            ],
        )
        skipped = [
            run
            for run in result["runs"]
            if run["stage"] == MODULE.STAGE_CONFLICT and run["sweep"] == 2
        ]
        self.assertEqual("completed_this_run", skipped[0]["action"])

    def test_stale_completed_state_does_not_suppress_a_second_sweep(self):
        self.completed.add(MODULE.STAGE_CONFLICT)
        original = self.run_stage

        def move_head_without_new_resolver_state(entry, *args, **kwargs):
            result = original(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_DESCRIPTION and kwargs["sweep"] == 1:
                self.sync_heads.append(NEXT_HEAD)
            if entry["stage"] == MODULE.STAGE_CONFLICT:
                self.completed.add(entry["stage"])
                self.clear_at[entry["stage"]] = None
            return result

        MODULE.run_stage.side_effect = move_head_without_new_resolver_state
        self.execute()

        self.assertEqual(
            [(MODULE.STAGE_CONFLICT, 1), (MODULE.STAGE_CONFLICT, 2)],
            [
                launch
                for launch in self.launched
                if launch[0] == MODULE.STAGE_CONFLICT
            ],
        )

    def test_two_sweeps_bound_the_run_at_ten_stage_launches(self):
        def never_clear(entry, *args, **kwargs):
            result = self.run_stage(entry, *args, **kwargs)
            self.clear_at[entry["stage"]] = None
            if entry["stage"] == MODULE.STAGE_DESCRIPTION and kwargs["sweep"] == 1:
                self.sync_heads.append(NEXT_HEAD)
            return result

        MODULE.run_stage.side_effect = never_clear
        result = self.execute()
        self.assertEqual("incomplete", result["result"])
        self.assertEqual("two_sweeps_finished", result["reason"])
        self.assertEqual(10, len(self.launched))

    def test_nonzero_stage_exit_blocks_later_stages(self):
        original = self.run_stage

        def fail_conflict(entry, *args, **kwargs):
            result = original(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_CONFLICT:
                result["returncode"] = 1
                self.clear_at[entry["stage"]] = None
            return result

        MODULE.run_stage.side_effect = fail_conflict
        result = self.execute()
        self.assertEqual("blocked", result["result"])
        self.assertEqual("stage_execution_failed", result["reason"])
        self.assertEqual(
            [(MODULE.STAGE_CONFLICT, 1)],
            self.launched,
        )

    def test_failed_coordinator_cannot_clear_a_stage_even_with_a_current_marker(self):
        def fail_after_marker(entry, *args, **kwargs):
            result = self.run_stage(entry, *args, **kwargs)
            result["returncode"] = 1
            return result

        MODULE.run_stage.side_effect = fail_after_marker
        result = self.execute()
        self.assertEqual("blocked", result["result"])
        self.assertEqual("stage_execution_failed", result["reason"])
        self.assertEqual([(MODULE.STAGE_CONFLICT, 1)], self.launched)

    def test_unsafe_worktree_stops_the_sweep(self):
        MODULE.settle_after_stage.side_effect = None
        MODULE.settle_after_stage.return_value = {
            "result": "blocked",
            "reason": "stage_left_dirty_worktree",
            "detail": "dirty",
        }
        result = self.execute()
        self.assertEqual("blocked", result["result"])
        self.assertEqual("stage_left_dirty_worktree", result["reason"])
        self.assertEqual([(MODULE.STAGE_CONFLICT, 1)], self.launched)

    def test_blocked_stage_preserves_escalation_and_retained_commits(self):
        local_head = "c" * 40
        escalation = {
            "reason": "unfixable_failure",
            "detail": "upstream defect",
            "checks": ["check:test"],
            "next_action": "Decide the fix.",
        }
        MODULE.settle_after_stage.side_effect = None
        MODULE.settle_after_stage.return_value = {
            "result": "blocked",
            "reason": "stage_left_unpublished_commits",
            "detail": "local commit was not pushed",
            "local_head_sha": local_head,
            "pr_head_sha": HEAD,
        }
        MODULE.inspect_stages.side_effect = None
        MODULE.inspect_stages.return_value = [
            {
                **uncleared_stage(stage, "escalated"),
                "status": {"escalation": escalation},
            }
            if stage == MODULE.STAGE_CONFLICT
            else uncleared_stage(stage)
            for stage in MODULE.STAGE_NAMES
        ]
        retained = [{"sha": local_head, "title": "Keep partial fix"}]
        with mock.patch.object(
            MODULE, "local_commits_between", return_value=retained
        ):
            result = self.execute()

        self.assertEqual("blocked", result["result"])
        self.assertEqual("stage_left_unpublished_commits", result["reason"])
        self.assertEqual(local_head, result["local_head_sha"])
        self.assertEqual(retained, result["retained_commits"])
        self.assertEqual(escalation, result["stage_result"]["status"]["escalation"])
        self.assertEqual(retained, result["runs"][0]["retained_commits"])
        self.assertEqual(5, len(result["stages"]))

    def test_closed_pull_request_runs_nothing(self):
        MODULE.read_pull_request.side_effect = lambda _target: pull_request(
            HEAD, state="CLOSED"
        )
        result = self.execute()
        self.assertEqual("blocked", result["result"])
        self.assertEqual("pr_not_open", result["reason"])
        self.assertEqual([], self.launched)


class WorktreeSafetyTest(unittest.TestCase):
    def git(self, repo: Path, *args: str) -> str:
        process = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.com",
            },
        )
        return process.stdout.strip()

    def make_remote(self, root: Path) -> tuple[Path, str, str]:
        remote = root / "remote"
        remote.mkdir()
        self.git(remote, "init", "-q", "-b", "main")
        self.git(remote, "commit", "-q", "--allow-empty", "-m", "base")
        base = self.git(remote, "rev-parse", "HEAD")
        self.git(remote, "checkout", "-q", "-b", "feature")
        self.git(remote, "commit", "-q", "--allow-empty", "-m", "pull request")
        head = self.git(remote, "rev-parse", "HEAD")
        self.git(remote, "update-ref", "refs/pull/7/head", head)
        self.git(remote, "checkout", "-q", "main")
        return remote, base, head

    def clone(self, root: Path, remote: Path) -> Path:
        local = root / "local"
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--single-branch",
                "--branch",
                "main",
                str(remote),
                str(local),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.git(local, "config", "user.name", "t")
        self.git(local, "config", "user.email", "t@example.com")
        return local

    def sync(self, local: Path, remote: Path, known_safe_head=None):
        with mock.patch.object(MODULE, "target_remote", return_value=str(remote)):
            return MODULE.sync_worktree(
                local,
                target(),
                pull_request(),
                known_safe_head=known_safe_head,
            )

    def test_unreachable_local_commit_is_not_discarded(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            self.git(repo, "init", "-q", "-b", "main")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "base")
            published = self.git(repo, "rev-parse", "HEAD")
            self.git(repo, "checkout", "-q", "--detach")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "local")
            with mock.patch.object(
                MODULE,
                "fetch_pr_head",
                return_value={"result": "ready", "head_sha": published},
            ):
                result = MODULE.sync_worktree(
                    repo,
                    target(),
                    pull_request(published),
                    known_safe_head=None,
                )
            self.assertEqual("blocked", result["result"])
            self.assertEqual("local_head_not_published", result["reason"])
            self.assertNotEqual(published, self.git(repo, "rev-parse", "HEAD"))

    def test_pr_branch_behind_remote_is_checked_out(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, _base, head = self.make_remote(root)
            local = self.clone(root, remote)
            self.git(local, "checkout", "-q", "-b", "feature")

            result = self.sync(local, remote)

            self.assertEqual("ready", result["result"])
            self.assertEqual(head, self.git(local, "rev-parse", "HEAD"))
            self.assertEqual("", self.git(local, "branch", "--show-current"))

    def test_unpublished_commit_on_pr_branch_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, _base, head = self.make_remote(root)
            local = self.clone(root, remote)
            self.git(local, "fetch", "-q", str(remote), "refs/pull/7/head")
            self.git(local, "checkout", "-q", "-b", "feature", "FETCH_HEAD")
            self.git(local, "commit", "-q", "--allow-empty", "-m", "local")
            local_head = self.git(local, "rev-parse", "HEAD")

            result = self.sync(local, remote)

            self.assertEqual("blocked", result["result"])
            self.assertEqual("local_head_not_published", result["reason"])
            self.assertEqual(local_head, self.git(local, "rev-parse", "HEAD"))
            self.assertNotEqual(head, local_head)

    def test_detached_old_pr_head_moves_to_new_pr_head(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, _base, old_head = self.make_remote(root)
            local = self.clone(root, remote)
            self.git(local, "fetch", "-q", str(remote), "refs/pull/7/head")
            self.git(local, "checkout", "-q", "--detach", "FETCH_HEAD")
            self.git(remote, "checkout", "-q", "feature")
            self.git(remote, "commit", "-q", "--allow-empty", "-m", "next")
            new_head = self.git(remote, "rev-parse", "HEAD")
            self.git(remote, "update-ref", "refs/pull/7/head", new_head)
            self.git(remote, "checkout", "-q", "main")

            result = self.sync(local, remote)

            self.assertEqual("ready", result["result"])
            self.assertNotEqual(old_head, new_head)
            self.assertEqual(new_head, self.git(local, "rev-parse", "HEAD"))

    def test_published_stage_commit_followed_by_another_push_is_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, _base, started = self.make_remote(root)
            local = self.clone(root, remote)
            self.git(local, "fetch", "-q", str(remote), "refs/pull/7/head")
            self.git(local, "checkout", "-q", "--detach", "FETCH_HEAD")
            self.git(local, "commit", "-q", "--allow-empty", "-m", "stage")
            stage_head = self.git(local, "rev-parse", "HEAD")
            self.git(local, "push", "-q", str(remote), "HEAD:refs/pull/7/head")
            self.git(remote, "checkout", "-q", "feature")
            self.git(remote, "reset", "-q", "--hard", stage_head)
            self.git(remote, "commit", "-q", "--allow-empty", "-m", "other")
            final_head = self.git(remote, "rev-parse", "HEAD")
            self.git(remote, "update-ref", "refs/pull/7/head", final_head)
            self.git(remote, "checkout", "-q", "main")

            with mock.patch.object(MODULE, "target_remote", return_value=str(remote)):
                result = MODULE.settle_after_stage(
                    local,
                    target(),
                    started_head_sha=started,
                )

            self.assertEqual("ready", result["result"])
            self.assertEqual(final_head, self.git(local, "rev-parse", "HEAD"))

    def test_lists_retained_first_parent_commits(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            self.git(repo, "init", "-q", "-b", "main")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "base")
            base = self.git(repo, "rev-parse", "HEAD")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "first fix")
            first = self.git(repo, "rev-parse", "HEAD")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "second fix")
            second = self.git(repo, "rev-parse", "HEAD")

            self.assertEqual(
                [
                    {"sha": first, "title": "first fix"},
                    {"sha": second, "title": "second fix"},
                ],
                MODULE.local_commits_between(repo, base, second),
            )


class AgentInstructionTest(unittest.TestCase):
    def test_requires_the_exact_primary_model_and_exposed_effort(self):
        text = AGENT.read_text(encoding="utf-8")
        self.assertIn("model is exactly `gpt-5.6-sol`", text)
        self.assertIn(
            "effort is either exactly `high` or unavailable",
            text,
        )
        self.assertIn("an unavailable effort does not fail the gate", text)
        self.assertIn("If you cannot determine the model", text)
        self.assertIn("The user cannot override this gate", text)

    def test_requires_a_chat_only_retrospective_after_the_terminal_response(self):
        text = AGENT.read_text(encoding="utf-8")
        self.assertIn("## Retrospective", text)
        for category in (
            "**Agent**",
            "**Helper**",
            "**General instructions**",
            "**Repository**",
        ):
            self.assertIn(category, text)
        self.assertIn("After every terminal outcome", text)
        self.assertIn("Keep this advisory and chat-only", text)
        self.assertIn("Omit this section when the run encountered no friction", text)
        self.assertGreater(text.index("## Retrospective"), text.index("Write a concise final response"))

    def test_agent_uses_the_durable_start_and_watch_protocol(self):
        text = AGENT.read_text(encoding="utf-8")
        self.assertIn("pr_pipeline.py\" start", text)
        self.assertIn("pr_pipeline.py\" watch", text)
        self.assertIn("at most two foreground sweeps", text)
        self.assertIn("A nonzero stage exit", text)
        self.assertIn("Sweeps never reset or multiply it", text)
        self.assertIn("an active child after its coordinator returns blocks", text)
        self.assertIn("Run `start` synchronously exactly once", text)
        self.assertIn("`next_watch.arguments`", text)
        self.assertIn("never add or reconstruct a positional target", text)
        self.assertIn("--wait-seconds 300", text)
        self.assertIn("no more than one per five minutes", text)
        self.assertIn("Never end your turn", text)
        self.assertIn("visible assistant line in this session conversation", text)
        self.assertIn("Waiting: <wait_reason>.", text)
        self.assertIn("Next: <next_action>.", text)
        self.assertIn("Do not send these updates to the PR Flight canvas", text)
        self.assertIn(
            "If `updates` is empty, invoke the returned `next_watch.arguments` again",
            text,
        )
        self.assertIn("`final_event`", text)
        watch_lines = [
            line for line in text.splitlines() if "pr_pipeline.py" in line and " watch " in line
        ]
        self.assertTrue(any("copilot_home=" in line for line in watch_lines))
        self.assertTrue(any("$copilotHome =" in line for line in watch_lines))
        self.assertTrue(all("<owner/repo#number>" not in line for line in watch_lines))
        self.assertIn("A clean run that pushed no commits", text)
        self.assertIn("Do not organize the response by sweep", text)
        self.assertNotIn("### Sweep 1", text)
        self.assertNotIn("### Sweep 2", text)
        self.assertNotIn("### Final stage status", text)
        self.assertNotIn("Pushed commits: none", text)
        self.assertIn("published_commits", text)
        self.assertIn("retained_commits", text)
        self.assertIn("stage_result.status", text)
        self.assertLess(
            text.index("2. `copilot-review-loop`"),
            text.index("3. `self-review-loop`"),
        )


class CommandOutputTest(unittest.TestCase):
    def test_run_emits_json_lines_ending_with_pipeline_result(self):
        def fake_pipeline(
            _target, _repo, *, models, effort, conflict_strategy, report
        ):
            self.assertIsNotNone(models)
            self.assertEqual("high", effort)
            self.assertEqual("auto", conflict_strategy)
            report({"event": "pipeline_started", "run_id": "run-1"})
            report(
                {
                    "event": "stage_started",
                    "stage": MODULE.STAGE_CONFLICT,
                    "sweep": 1,
                }
            )
            return {
                "result": "complete",
                "run_id": "run-1",
                "head_sha": HEAD,
            }

        args = MODULE.build_parser().parse_args(["run", "owner/repo#7"])
        output = StringIO()
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=Path("C:/repo")),
            mock.patch.object(MODULE, "resolve_target", return_value=target()),
            mock.patch.object(MODULE, "run_pipeline", side_effect=fake_pipeline),
            redirect_stdout(output),
        ):
            MODULE.command_run(args)

        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            ["pipeline_started", "stage_started", "pipeline_finished"],
            [event["event"] for event in events],
        )
        self.assertEqual("complete", events[-1]["result"])
        self.assertEqual(HEAD, events[-1]["head_sha"])

    def test_error_is_a_terminal_json_event(self):
        output = StringIO()
        with (
            mock.patch.object(
                MODULE, "require_tools", side_effect=MODULE.WorkflowError("broken")
            ),
            mock.patch.object(
                __import__("sys"), "argv", ["pr_pipeline.py", "run", "owner/repo#7"]
            ),
            redirect_stdout(output),
        ):
            result = MODULE.main()

        self.assertEqual(1, result)
        event = json.loads(output.getvalue())
        self.assertEqual("pipeline_finished", event["event"])
        self.assertEqual("error", event["result"])
        self.assertEqual("broken", event["error"])

    def test_watch_errors_are_not_reported_as_pipeline_outcomes(self):
        output = StringIO()
        with (
            mock.patch.object(
                __import__("sys"),
                "argv",
                [
                    "pr_pipeline.py",
                    "watch",
                    "owner/repo#7",
                    "--run-id",
                    "invalid",
                ],
            ),
            redirect_stdout(output),
        ):
            result = MODULE.main()

        self.assertEqual(1, result)
        event = json.loads(output.getvalue())
        self.assertEqual(MODULE.PROGRESS_UPDATE_EVENT, event["event"])
        self.assertTrue(event["finished"])
        self.assertIn("run-id", event["monitor_failure"])
        self.assertEqual("invalid", event["run_id"])
        self.assertEqual(0, event["cursor"])
        self.assertNotIn("next_watch", event)
        self.assertNotEqual("pipeline_finished", event["event"])

    def test_watch_without_a_run_id_returns_one_terminal_json_event(self):
        output = StringIO()
        with (
            mock.patch.object(
                __import__("sys"),
                "argv",
                ["pr_pipeline.py", "watch"],
            ),
            redirect_stdout(output),
        ):
            result = MODULE.main()

        self.assertEqual(1, result)
        event = json.loads(output.getvalue())
        self.assertEqual(MODULE.PROGRESS_UPDATE_EVENT, event["event"])
        self.assertTrue(event["finished"])
        self.assertEqual("watch requires --run-id", event["monitor_failure"])
        self.assertIsNone(event["run_id"])
        self.assertNotIn("next_watch", event)


class ProgressProtocolTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.event_log = self.root / "progress.jsonl"

    def test_stage_transitions_include_sweep_pr_wait_and_next_action(self):
        output = []
        reporter = MODULE.ProgressReporter(
            target=target(),
            event_log=self.event_log,
            output=output.append,
            wall_time=lambda: 1000.0,
        )
        reporter(
            {
                "event": "stage_started",
                "stage": MODULE.STAGE_CI,
                "sweep": 1,
            }
        )
        reporter(
            {
                "event": "stage_finished",
                "stage": MODULE.STAGE_CI,
                "sweep": 1,
                "action": "launched",
                "clear": False,
                "stage_reason": "checks_failed",
            }
        )

        updates = MODULE.common.read_progress_log(self.event_log)
        self.assertEqual(2, len(output))
        self.assertIn("Sweep 1/2", updates[0]["message"])
        self.assertIn("#7", updates[0]["message"])
        self.assertEqual(MODULE.STAGE_CI, updates[0]["stage"])
        self.assertEqual("waiting for CI remediation", updates[0]["wait_reason"])
        self.assertTrue(updates[0]["next_action"])
        self.assertIn("not clear: checks_failed", updates[1]["message"])

    def test_stage_progress_distinguishes_diagnostics_from_check_waiting(self):
        output = []
        reporter = MODULE.ProgressReporter(
            target=target(),
            event_log=self.event_log,
            output=output.append,
            wall_time=lambda: 1000.0,
        )
        reporter(
            {
                "event": "stage_progress",
                "stage": MODULE.STAGE_CI,
                "sweep": 1,
                "number": 7,
                "phase": "diagnosing",
                "action_checks": ["check:build"],
                "pending_checks": ["check:test"],
            }
        )
        update = MODULE.common.read_progress_log(self.event_log)[0]
        self.assertIn("diagnosing 1 known failure", update["message"])
        self.assertIn("diagnosing a known CI failure", update["wait_reason"])

    def test_stale_base_progress_names_recorded_and_live_revisions(self):
        reporter = MODULE.ProgressReporter(
            target=target(),
            event_log=self.event_log,
            output=lambda _payload: None,
            wall_time=lambda: 1000.0,
        )
        reporter(
            {
                "event": "stage_finished",
                "stage": MODULE.STAGE_CONFLICT,
                "sweep": 1,
                "action": "launched",
                "clear": False,
                "stage_reason": "clearance_is_for_an_older_base",
                "clear_at_base_sha": "1" * 40,
                "inspected_base_sha": "2" * 40,
            }
        )

        message = MODULE.common.read_progress_log(self.event_log)[0]["message"]
        self.assertIn("recorded 11111111, live 22222222", message)

    def test_scheduler_command_carries_the_monitor_handle_and_options(self):
        args = MODULE.build_parser().parse_args(
            [
                "start",
                "owner/repo#7",
                "--stage-model",
                "ci-fix-loop=claude-sonnet-5",
                "--effort",
                "high",
                "--conflict-strategy",
                "merge",
            ]
        )
        command = MODULE.scheduler_command(
            args,
            target(),
            "a" * 32,
            self.event_log,
        )

        self.assertIn("run", command)
        self.assertIn("--run-id", command)
        self.assertIn("a" * 32, command)
        self.assertIn("--event-log", command)
        self.assertIn("ci-fix-loop=claude-sonnet-5", command)
        self.assertIn("--conflict-strategy", command)
        self.assertIn("merge", command)

    def test_start_writes_a_durable_launch_record_before_returning(self):
        args = MODULE.build_parser().parse_args(
            [
                "start",
                "owner/repo#7",
                "--conflict-strategy",
                "merge",
            ]
        )
        process = mock.Mock(pid=4321)
        output = StringIO()
        with (
            mock.patch.object(MODULE, "resolve_repo_root", return_value=self.root),
            mock.patch.object(MODULE, "resolve_target", return_value=target()),
            mock.patch.object(MODULE, "copilot_home", return_value=self.root),
            mock.patch.object(MODULE.common, "start_detached", return_value=process),
            mock.patch.object(MODULE.uuid, "uuid4", return_value=mock.Mock(hex="a" * 32)),
            redirect_stdout(output),
        ):
            MODULE.command_start(args)

        launch = MODULE.common.read_json(
            self.root
            / "run"
            / MODULE.RUN_KIND
            / MODULE.run_slug(target())
            / ("a" * 32)
            / "launch.json"
        )
        locator = MODULE.common.read_json(
            self.root
            / "run"
            / MODULE.RUN_KIND
            / "monitors"
            / f"{'a' * 32}.json"
        )
        event = json.loads(output.getvalue())
        self.assertEqual(4321, launch["pid"])
        self.assertEqual("a" * 32, launch["run_id"])
        self.assertEqual("merge", launch["conflict_strategy"])
        self.assertEqual("pipeline_launched", event["event"])
        self.assertEqual("owner/repo#7", event["target"])
        self.assertEqual("merge", event["conflict_strategy"])
        self.assertEqual(MODULE.MONITOR_SCHEMA, locator["schema"])
        self.assertEqual(MODULE.MONITOR_VERSION, locator["version"])
        self.assertEqual(
            {"owner": "owner", "repo": "repo", "number": 7},
            locator["target"],
        )
        self.assertEqual(
            [
                "watch",
                "--run-id",
                "a" * 32,
                "--cursor",
                "0",
                "--wait-seconds",
                "300",
            ],
            event["next_watch"]["arguments"],
        )
        self.assertNotIn("owner/repo#7", event["next_watch"]["arguments"])

    def write_monitor_run(
        self,
        run_id: str,
        *,
        selected: dict | None = None,
        launch_run_id: str | None = None,
    ) -> dict:
        selected = selected or target()
        launch = {
            "kind": MODULE.RUN_KIND,
            "run_id": launch_run_id or run_id,
            "target": selected,
            "pid": 4321,
            "event_log": str(MODULE.progress_log_path(selected, run_id)),
            "started_at": "2026-09-17T00:00:00Z",
            "started_at_epoch": 1.0,
            "conflict_strategy": "auto",
            "github_mutation_policy": "source-only",
        }
        MODULE.common.write_json_atomically(
            MODULE.launch_state_path(selected, run_id), launch
        )
        MODULE.common.write_json_atomically(
            MODULE.monitor_locator_path(run_id),
            MODULE.monitor_locator(selected, run_id),
        )
        return selected

    def test_watch_uses_the_start_bound_target_without_a_positional_target(self):
        run_id = "a" * 32
        args = MODULE.build_parser().parse_args(
            [
                "watch",
                "--run-id",
                run_id,
                "--cursor",
                "4",
                "--wait-seconds",
                "1",
            ]
        )
        output = StringIO()
        with (
            mock.patch.object(MODULE, "copilot_home", return_value=self.root),
            mock.patch.object(
                MODULE.common,
                "watch_progress",
                return_value={
                    "event": MODULE.PROGRESS_UPDATE_EVENT,
                    "cursor": 5,
                    "updates": [],
                    "finished": False,
                },
            ) as watch,
            redirect_stdout(output),
        ):
            self.write_monitor_run(run_id)
            MODULE.command_watch(args)

        run_directory = (
            self.root
            / "run"
            / MODULE.RUN_KIND
            / MODULE.run_slug(target())
            / run_id
        )
        watch.assert_called_once_with(
            event_log=run_directory / "progress.jsonl",
            launch_path=run_directory / "launch.json",
            observer_path=run_directory / "observer.json",
            cursor=4,
            wait_seconds=1.0,
        )
        event = json.loads(output.getvalue())
        self.assertEqual("owner/repo#7", event["target"])
        self.assertEqual(run_id, event["run_id"])
        self.assertEqual(
            [
                "watch",
                "--run-id",
                run_id,
                "--cursor",
                "5",
                "--wait-seconds",
                "300",
            ],
            event["next_watch"]["arguments"],
        )

    def test_watch_does_not_fall_back_to_another_or_latest_run(self):
        requested = "a" * 32
        decoy = "b" * 32
        args = MODULE.build_parser().parse_args(
            ["watch", "--run-id", requested]
        )
        with (
            mock.patch.object(MODULE, "copilot_home", return_value=self.root),
            mock.patch.object(MODULE.common, "watch_progress") as watch,
        ):
            self.write_monitor_run(decoy)
            latest = self.root / "run" / MODULE.RUN_KIND / "latest.json"
            MODULE.common.write_json_atomically(
                latest,
                {"run_id": decoy, "target": MODULE.target_identity(target())},
            )
            with self.assertRaisesRegex(
                MODULE.WorkflowError,
                f"monitor handle does not exist for run {requested}",
            ):
                MODULE.command_watch(args)

        watch.assert_not_called()

    def test_watch_rejects_a_malformed_monitor_locator(self):
        run_id = "a" * 32
        args = MODULE.build_parser().parse_args(
            ["watch", "--run-id", run_id]
        )
        with (
            mock.patch.object(MODULE, "copilot_home", return_value=self.root),
            mock.patch.object(MODULE.common, "watch_progress") as watch,
        ):
            self.write_monitor_run(run_id)
            locator = MODULE.monitor_locator(target(), run_id)
            locator["launch_path"] = str(
                MODULE.launch_state_path(target(), "b" * 32)
            )
            MODULE.common.write_json_atomically(
                MODULE.monitor_locator_path(run_id), locator
            )
            with self.assertRaisesRegex(
                MODULE.WorkflowError,
                f"monitor handle paths are invalid for run {run_id}",
            ):
                MODULE.command_watch(args)

        watch.assert_not_called()

    def test_watch_rejects_a_monitor_target_that_escapes_the_run_root(self):
        run_id = "a" * 32
        args = MODULE.build_parser().parse_args(
            ["watch", "--run-id", run_id]
        )
        with (
            mock.patch.object(MODULE, "copilot_home", return_value=self.root),
            mock.patch.object(MODULE.common, "watch_progress") as watch,
        ):
            locator = MODULE.monitor_locator(target(), run_id)
            locator["target"]["owner"] = "..\\..\\outside"
            MODULE.common.write_json_atomically(
                MODULE.monitor_locator_path(run_id), locator
            )
            with self.assertRaisesRegex(
                MODULE.WorkflowError,
                "invalid owner path identity",
            ):
                MODULE.command_watch(args)

        watch.assert_not_called()

    def test_watch_rejects_a_target_that_disagrees_with_the_handle(self):
        run_id = "a" * 32
        args = MODULE.build_parser().parse_args(
            ["watch", "owner/repo#8", "--run-id", run_id]
        )
        with (
            mock.patch.object(MODULE, "copilot_home", return_value=self.root),
            mock.patch.object(MODULE.common, "watch_progress") as watch,
        ):
            self.write_monitor_run(run_id)
            with self.assertRaisesRegex(
                MODULE.WorkflowError,
                f"watch target does not match monitor handle for run {run_id}",
            ):
                MODULE.command_watch(args)

        watch.assert_not_called()

    def test_watch_rejects_a_launch_record_from_another_run(self):
        run_id = "a" * 32
        args = MODULE.build_parser().parse_args(
            ["watch", "--run-id", run_id]
        )
        with (
            mock.patch.object(MODULE, "copilot_home", return_value=self.root),
            mock.patch.object(MODULE.common, "watch_progress") as watch,
        ):
            self.write_monitor_run(run_id, launch_run_id="b" * 32)
            with self.assertRaisesRegex(
                MODULE.WorkflowError,
                f"launch record identity is invalid for run {run_id}",
            ):
                MODULE.command_watch(args)

        watch.assert_not_called()

    def test_legacy_watch_requires_an_explicit_exact_target(self):
        run_id = "a" * 32
        args = MODULE.build_parser().parse_args(
            ["watch", "owner/repo#7", "--run-id", run_id]
        )
        output = StringIO()
        with (
            mock.patch.object(MODULE, "copilot_home", return_value=self.root),
            mock.patch.object(
                MODULE.common,
                "watch_progress",
                return_value={
                    "event": MODULE.PROGRESS_UPDATE_EVENT,
                    "cursor": 1,
                    "updates": [],
                    "finished": False,
                },
            ) as watch,
            redirect_stdout(output),
        ):
            selected = target()
            MODULE.common.write_json_atomically(
                MODULE.launch_state_path(selected, run_id),
                {
                    "kind": MODULE.RUN_KIND,
                    "run_id": run_id,
                    "target": selected,
                    "pid": 4321,
                    "event_log": str(MODULE.progress_log_path(selected, run_id)),
                },
            )
            MODULE.command_watch(args)

        watch.assert_called_once()
        event = json.loads(output.getvalue())
        self.assertEqual(
            [
                "watch",
                "owner/repo#7",
                "--run-id",
                run_id,
                "--cursor",
                "1",
                "--wait-seconds",
                "300",
            ],
            event["next_watch"]["arguments"],
        )
        replay = MODULE.build_parser().parse_args(
            event["next_watch"]["arguments"]
        )
        self.assertEqual("owner/repo#7", replay.target)
        self.assertEqual(run_id, replay.run_id)

    def test_terminal_watch_response_has_no_next_command(self):
        payload = MODULE.bind_next_watch(
            {
                "event": MODULE.PROGRESS_UPDATE_EVENT,
                "cursor": 3,
                "updates": [],
                "finished": True,
            },
            target=target(),
            run_id="a" * 32,
            legacy_target=False,
        )

        self.assertNotIn("next_watch", payload)

    def test_failed_start_does_not_publish_a_monitor_handle(self):
        args = MODULE.build_parser().parse_args(
            ["start", "owner/repo#7"]
        )
        process_error = OSError("could not start scheduler")
        with (
            mock.patch.object(MODULE, "resolve_repo_root", return_value=self.root),
            mock.patch.object(MODULE, "resolve_target", return_value=target()),
            mock.patch.object(MODULE, "copilot_home", return_value=self.root),
            mock.patch.object(
                MODULE.common,
                "start_detached",
                side_effect=process_error,
            ),
            mock.patch.object(
                MODULE.uuid,
                "uuid4",
                return_value=mock.Mock(hex="a" * 32),
            ),
        ):
            with self.assertRaisesRegex(OSError, "could not start scheduler"):
                MODULE.command_start(args)

        self.assertFalse(
            (
                self.root
                / "run"
                / MODULE.RUN_KIND
                / "monitors"
                / f"{'a' * 32}.json"
            ).exists()
        )


class ParserTest(unittest.TestCase):
    def test_the_progress_protocol_adds_start_and_watch_commands(self):
        parser = MODULE.build_parser()
        action = next(
            action
            for action in parser._actions
            if isinstance(action, __import__("argparse")._SubParsersAction)
        )
        self.assertEqual({"run", "start", "watch"}, set(action.choices))

    def test_run_accepts_model_overrides(self):
        args = MODULE.build_parser().parse_args(
            [
                "run",
                "owner/repo#7",
                "--stage-model",
                "ci-fix-loop=claude-sonnet-5",
            ]
        )
        self.assertEqual("owner/repo#7", args.target)
        self.assertEqual(["ci-fix-loop=claude-sonnet-5"], args.stage_model)

    def test_start_accepts_an_explicit_conflict_strategy(self):
        args = MODULE.build_parser().parse_args(
            ["start", "owner/repo#7", "--conflict-strategy", "merge"]
        )

        self.assertEqual("merge", args.conflict_strategy)

    def test_watch_accepts_the_monitor_handle_without_a_target(self):
        args = MODULE.build_parser().parse_args(
            [
                "watch",
                "--run-id",
                "a" * 32,
                "--cursor",
                "4",
                "--wait-seconds",
                "300",
            ]
        )
        self.assertIsNone(args.target)
        self.assertEqual("a" * 32, args.run_id)
        self.assertEqual(4, args.cursor)
        self.assertEqual(300, args.wait_seconds)


if __name__ == "__main__":
    unittest.main()
