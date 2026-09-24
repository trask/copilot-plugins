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


def ci_warning_payload(head=HEAD, base=BASE) -> dict:
    return {
        "stage_outcome": "warning",
        "clean_at_head_sha": None,
        "warning_at_head_sha": head,
        "warning_at_base_sha": base,
        "warning_verification": {
            "result": "current",
            "expected_snapshot_sha256": "e" * 64,
            "observed_snapshot_sha256": "e" * 64,
            "reason": "ci_warning_snapshot_current",
        },
        "ci_warnings": [{
            "check_key": "check:77",
            "name": "Integration tests",
            "diagnosis": "pre_existing",
            "reason": "The same failure occurs on the base revision.",
            "evidence": ["Base run 123 fails with the same error at the same line."],
        }],
    }


def ci_green_payload(head=HEAD, base=BASE) -> dict:
    return {
        "stage_outcome": "cleared", "outcome": "green",
        "clean_at_head_sha": head, "clean_at_base_sha": base,
        "clearance_verification": {
            "result": "current", "reason": "ci_snapshot_current",
            "expected_snapshot_sha256": "e" * 64, "observed_snapshot_sha256": "e" * 64,
        },
    }


def stale_ci_green_payload() -> dict:
    return {
        "stage_outcome": "pending", "outcome": None,
        "clean_at_head_sha": None, "clean_at_base_sha": None,
        "clearance_verification": {
            "result": "stale", "reason": "ci_snapshot_changed",
            "expected_snapshot_sha256": "e" * 64, "observed_snapshot_sha256": "f" * 64,
        },
    }


def description_payload(head=HEAD, base=BASE) -> dict:
    return {
        "validated_head_sha": head,
        "pr": {"base": {"sha": base}},
        "agent_task": {"status": "completed", "task": {"state": "completed"}},
        "clearance_verification": {
            "result": "current",
            "reason": "description_snapshot_current",
            "expected_snapshot_sha256": "a" * 64,
            "observed_snapshot_sha256": "a" * 64,
        },
    }


def source_drift_payload(
    *, expected=NEXT_HEAD, observed=HEAD, iteration=1, maximum=2
) -> dict:
    return {
        "stage_outcome": None,
        "validated_head_sha": None,
        "pipeline_run": PIPELINE_RUN,
        "pipeline_iteration": iteration,
        "pipeline_max_iterations": maximum,
        "agent_task": {
            "status": "head_changed",
            "task": {"id": "task-1", "state": "completed"},
            "source_drift": {
                "expected_head_sha": expected,
                "observed_head_sha": observed,
                "pipeline_iteration": iteration,
                "pipeline_max_iterations": maximum,
                "consumed_allowance": 1,
                "remaining_allowance": maximum - iteration,
                "mutation_performed": False,
                "recommendation_adopted": False,
                "publication_performed": False,
            },
        },
    }


def stale_ci_warning_payload() -> dict:
    return {
        "stage_outcome": "pending",
        "outcome": None,
        "clean_at_head_sha": None,
        "warning_at_head_sha": None,
        "warning_at_base_sha": None,
        "ci_warnings": [],
        "all_ci_passed": False,
        "warning_verification": {
            "result": "stale",
            "expected_snapshot_sha256": "e" * 64,
            "observed_snapshot_sha256": "f" * 64,
            "reason": "ci_warning_snapshot_changed",
        },
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

    def test_run_defaults_to_normal_policy(self):
        args = MODULE.build_parser().parse_args(["run", "owner/repo#7"])
        self.assertEqual("allow", args.github_mutation_policy)

    def test_run_preserves_source_only_policy(self):
        args = MODULE.build_parser().parse_args(
            [
                "run",
                "owner/repo#7",
                "--github-mutation-policy",
                "source-only",
            ]
        )
        self.assertEqual("source-only", args.github_mutation_policy)

    def test_metadata_sensitive_stages_receive_source_only_helper_argument(self):
        MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY = "source-only"
        target = {"repo_name": "owner/repo", "number": 7}
        for stage in (
            MODULE.common.STAGE_COPILOT_REVIEW,
            MODULE.common.STAGE_SELF_REVIEW,
            MODULE.common.STAGE_CI,
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
                        model="gpt-6-sol",
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

    def test_ci_stage_receives_frozen_policy_for_both_modes(self):
        for policy in ("allow", "source-only"):
            with self.subTest(policy=policy):
                MODULE.common.ACTIVE_GITHUB_MUTATION_POLICY = policy
                command = MODULE.stage_command(
                    MODULE.STAGE_BY_NAME[MODULE.STAGE_CI],
                    target(),
                    model="gpt-6-sol",
                    effort="high",
                    run_id=PIPELINE_RUN,
                    sweep=2,
                )
                self.assertEqual(1, command.count("--github-mutation-policy"))
                self.assertEqual(policy, command[command.index("--github-mutation-policy") + 1])
                self.assertEqual(PIPELINE_RUN, command[command.index("--pipeline-run") + 1])
                self.assertNotIn("--new-invocation", command)

    def test_agent_documents_normal_authorization_and_explicit_restrictions(self):
        instructions = AGENT.read_text(encoding="utf-8")
        self.assertIn("default `allow` policy", instructions)
        self.assertIn("bot-thread replies and resolution", instructions)
        self.assertIn("never permits merging, approval", instructions)
        self.assertIn("--github-mutation-policy source-only", instructions)
        self.assertIn("only when the user requests source-only", instructions)


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
        self.assertEqual(1, progress.call_count)
        sleep.assert_called_once_with(0)
        process.wait.assert_called_once()

    def test_stage_exit_at_deadline_preserves_the_child_failure(self):
        process = mock.Mock()
        process.poll.side_effect = [None, 1]
        process.wait.return_value = 1
        process.terminal_result = {
            "exit_code": 1,
            "workflow_result": {
                "result": "error", "error": "state replacement denied",
            },
        }
        progress = mock.Mock(side_effect=[None, AssertionError("deadline masked child")])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = MODULE.common.run_monitored(
                ["copilot"], cwd=root, log_path=root / "stage.log",
                progress=progress, interval=0,
                start=mock.Mock(return_value=process),
                sleep=mock.Mock(),
            )
        self.assertEqual(1, result["returncode"])
        self.assertEqual(
            "state replacement denied",
            result["child_terminal_result"]["workflow_result"]["error"],
        )
        progress.assert_called_once()

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

    def test_stage_monitor_preserves_progress_failure_during_forced_drainage(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.terminate_tree.side_effect = RuntimeError(
            "owned Windows job required forced drainage"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.object(MODULE.common, "_EXECUTION", object()),
                self.assertRaisesRegex(
                    MODULE.WorkflowError,
                    "TypeError: duplicate progress field; local drainage: "
                    "owned Windows job required forced drainage",
                ),
            ):
                MODULE.common.run_monitored(
                    ["copilot"], cwd=root, log_path=root / "stage.log",
                    progress=mock.Mock(side_effect=TypeError("duplicate progress field")),
                    start=mock.Mock(return_value=process),
                )
        process.terminate_tree.assert_called_once_with(timeout=10.0)

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
        identity = {
            "pid": 123, "creation_time": "456", "running": True,
            "in_job": True, "job_query_error": None,
        }
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
                mock.patch.object(
                    MODULE.common, "windows_process_identity", return_value=identity,
                ) as process_identity,
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
        process_identity.assert_called_once_with(123)
        self.assertEqual(identity, started.launch_receipt["process_identity"])
        self.assertTrue(started.launch_receipt["scheduler_owned_job"])

    def test_windows_background_worker_falls_back_inside_the_parent_job(self):
        process = mock.Mock(pid=123)
        job_error = self.access_denied()
        identity = {
            "pid": 123, "creation_time": "456", "running": True,
            "in_job": True, "job_query_error": None,
        }
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
                mock.patch.object(
                    MODULE.common, "windows_process_identity", return_value=identity,
                ) as process_identity,
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
        process_identity.assert_called_once_with(123)
        self.assertEqual(identity, started.launch_receipt["process_identity"])
        self.assertFalse(started.launch_receipt["scheduler_owned_job"])


class TargetTest(unittest.TestCase):
    def test_parses_supported_targets(self):
        expected = target()
        self.assertEqual(expected, MODULE.parse_target(expected["pr_url"]))
        self.assertEqual(expected, MODULE.parse_target("owner/repo#7"))
        self.assertEqual(expected, MODULE.parse_target("#7", "owner/repo"))

    def test_reads_commit_links_for_the_pull_request(self):
        with (
            mock.patch.object(MODULE.common, "git_succeeds", return_value=True),
            mock.patch.object(MODULE.common, "local_commits_between", return_value=[
                {"sha": HEAD, "title": "Fix the thing"}
            ]),
        ):
            self.assertEqual(
                [
                    {
                        "sha": HEAD,
                        "title": "Fix the thing",
                        "url": f"{target()['pr_url']}/commits/{HEAD}",
                    }
                ],
                MODULE.read_pr_commits(
                    target(), repo_root=Path("C:/repo"), base_sha=BASE, head_sha=HEAD
                ),
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
            "headRepository": {"name": "repo"},
            "headRepositoryOwner": {"login": "owner"},
        }
        with mock.patch.object(
            MODULE,
            "gh_json",
            side_effect=[
                payload, {"object": {"sha": BASE}}, {"object": {"sha": NEXT_HEAD}}
            ],
        ):
            result = MODULE.read_pull_request(target())

        self.assertEqual(BASE, result["base_sha"])
        self.assertEqual(NEXT_HEAD, result["head_sha"])
        self.assertEqual("owner/repo", result["head_repository"])

    def test_resolves_fork_branch_even_when_pull_request_oid_is_stale(self):
        payload = {
            "number": 7, "title": "Fork change", "url": target()["pr_url"],
            "state": "OPEN", "headRefName": "feature/topic", "baseRefName": "main",
            "headRefOid": HEAD, "headRepository": {"name": "repo"},
            "headRepositoryOwner": {"login": "fork"},
        }
        def api(arguments):
            if arguments[:2] == ["pr", "view"]:
                return payload
            if arguments[1] == "repos/owner/repo/git/ref/heads/main":
                return {"object": {"sha": BASE}}
            if arguments[1] == "repos/fork/repo/git/ref/heads/feature%2Ftopic":
                return {"object": {"sha": NEXT_HEAD}}
            self.fail(f"unexpected API call: {arguments}")

        result = MODULE.common.read_pull_request(
            target(), api=api, base_tip=lambda _repo, _branch: BASE,
        )
        self.assertEqual("fork/repo", result["head_repository"])
        self.assertEqual("feature/topic", result["head_branch"])
        self.assertEqual(NEXT_HEAD, result["head_sha"])
        self.assertNotEqual(HEAD, result["head_sha"])

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
        self.assertEqual("gpt-6-sol", models[MODULE.STAGE_SELF_REVIEW])
        with self.assertRaisesRegex(
            MODULE.WorkflowError,
            "requires exactly model gpt-6-sol",
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
            ("claude-sonnet-5", "high", "requires exactly model gpt-6-sol"),
            ("gpt-6-sol", "max", "requires exactly reasoning effort high"),
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
            ("gpt-6-sol", "sol"),
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
            ("pr-description=gpt-5.6-sol", "does not support model"),
            ("self-review-loop=gpt-5.6-sol", "requires exactly model"),
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
                "--github-mutation-policy",
                "allow",
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
                "review": {"outcome": "clean", "clean_at_head_sha": HEAD, "clean_at_base_sha": BASE}
            },
            MODULE.STAGE_COPILOT_REVIEW: {"clean_at_head_sha": HEAD, "clean_at_base_sha": BASE},
            MODULE.STAGE_CI: ci_green_payload(),
            MODULE.STAGE_DESCRIPTION: description_payload(),
        }
        for stage, payload in payloads.items():
            with self.subTest(stage=stage):
                self.assertTrue(self.status(stage, payload)["clear"])

    def test_current_head_clearance_survives_base_tip_movement_but_needs_ci_snapshot(self):
        for stage, payload in (
            (MODULE.STAGE_COPILOT_REVIEW, {"clean_at_head_sha": HEAD, "clean_at_base_sha": BASE}),
            (MODULE.STAGE_SELF_REVIEW, {"review": {"clean_at_head_sha": HEAD, "clean_at_base_sha": BASE}}),
            (MODULE.STAGE_CI, ci_green_payload()),
        ):
            with self.subTest(stage=stage):
                current = self.status(stage, payload, base_sha=NEXT_HEAD)
                self.assertTrue(current["clear"])
        cached = ci_green_payload()
        del cached["clearance_verification"]
        self.assertFalse(self.status(MODULE.STAGE_CI, cached)["clear"])

    def test_proven_source_drift_is_retained_without_clearance(self):
        payload = source_drift_payload()

        result = self.status(
            MODULE.STAGE_DESCRIPTION,
            payload,
            run_id=PIPELINE_RUN,
        )

        self.assertFalse(result["clear"])
        self.assertEqual("source_drift", result["reason"])
        self.assertEqual(
            payload["agent_task"]["source_drift"],
            result["status"]["agent_task"]["source_drift"],
        )
        self.assertEqual(
            payload["agent_task"]["source_drift"],
            result["source_drift"],
        )

    def test_source_drift_that_performed_mutation_is_not_trusted(self):
        payload = source_drift_payload()
        payload["agent_task"]["source_drift"]["mutation_performed"] = True

        result = self.status(
            MODULE.STAGE_DESCRIPTION,
            payload,
            run_id=PIPELINE_RUN,
        )

        self.assertFalse(result["clear"])
        self.assertEqual("not_cleared", result["reason"])
        self.assertNotIn("source_drift", result)

    def test_ci_warning_clears_orchestration_without_a_clean_marker(self):
        for diagnosis in ("unrelated", "pre_existing"):
            payload = ci_warning_payload()
            payload["ci_warnings"][0]["diagnosis"] = diagnosis
            with self.subTest(diagnosis=diagnosis):
                result = self.status(MODULE.STAGE_CI, payload)
                self.assertTrue(result["clear"])
                self.assertEqual("ci_warning", result["clearance_kind"])
                self.assertEqual(HEAD, result["clear_at_head_sha"])
                self.assertEqual(BASE, result["clear_at_base_sha"])
                self.assertFalse(result["all_ci_passed"])
                self.assertEqual(payload["ci_warnings"], result["ci_warnings"])
                self.assertEqual(payload["ci_warnings"], result["status"]["ci_warnings"])
                self.assertIsNone(payload["clean_at_head_sha"])

    def test_ci_warning_requires_head_and_base_evidence_not_current_base_tip(self):
        for head, base, inspected_base, reason in (
            (NEXT_HEAD, BASE, BASE, "clearance_is_for_an_older_head"),
            (None, BASE, BASE, "ci_warning_not_verified"),
            (HEAD, None, BASE, "clearance_base_marker_unavailable"),
            (" " + HEAD, BASE, BASE, "ci_warning_not_verified"),
            (HEAD, BASE + " ", BASE, "ci_warning_not_verified"),
        ):
            with self.subTest(head=head, base=base, inspected_base=inspected_base):
                result = self.status(
                    MODULE.STAGE_CI, ci_warning_payload(head, base), inspected_base
                )
                self.assertFalse(result["clear"])
                self.assertEqual(reason, result["reason"])
                self.assertNotIn("all_ci_passed", result)
                self.assertIsNone(result["clearance_kind"])
        self.assertTrue(
            self.status(MODULE.STAGE_CI, ci_warning_payload(), NEXT_BASE)["clear"]
        )

    def test_ci_warning_requires_well_formed_diagnoses_and_evidence(self):
        valid = ci_warning_payload()
        malformed = [None, {}, [], [None], ["advice"]]
        for key, values in {
            "check_key": [None, "", " "],
            "name": [None, "", " "],
            "reason": [None, "", " ", 7],
            "diagnosis": ["unknown", "transient", "pr_caused", None, {}],
            "evidence": [None, [], "", [" "], [1], [{}], ["valid", ""]],
        }.items():
            for value in values:
                malformed.append([{**valid["ci_warnings"][0], key: value}])
            missing = dict(valid["ci_warnings"][0])
            missing.pop(key)
            malformed.append([missing])
        for warnings in malformed:
            with self.subTest(warnings=warnings):
                result = self.status(MODULE.STAGE_CI, {**valid, "ci_warnings": warnings})
                self.assertFalse(result["clear"])
                self.assertEqual("ci_warning_not_verified", result["reason"])

    def test_ci_warning_requires_fresh_matching_snapshot_verification(self):
        payload = ci_warning_payload()
        verified = payload["warning_verification"]
        malformed = [None, {}, "current"]
        for key, values in {
            "result": ["stale", None, True],
            "reason": ["ci_warning_snapshot_changed", None, ""],
            "expected_snapshot_sha256": ["", "e" * 63, "z" * 64, None, 1],
            "observed_snapshot_sha256": ["f" * 64, "", None],
        }.items():
            for value in values:
                malformed.append({**verified, key: value})
            malformed.append({field: value for field, value in verified.items() if field != key})
        for verification in malformed:
            with self.subTest(verification=verification):
                result = self.status(
                    MODULE.STAGE_CI, {**payload, "warning_verification": verification}
                )
                self.assertFalse(result["clear"])
                self.assertEqual("ci_warning_not_verified", result["reason"])
                self.assertNotIn("ci_warnings", result)
        payload.pop("warning_verification")
        self.assertFalse(self.status(MODULE.STAGE_CI, payload)["clear"])

    def test_ci_snapshot_change_is_stale_at_the_same_head_and_base(self):
        payload = stale_ci_warning_payload()
        result = self.status(MODULE.STAGE_CI, payload)
        self.assertFalse(result["clear"])
        self.assertEqual("ci_warning_snapshot_changed", result["reason"])
        self.assertEqual(payload["warning_verification"], result["warning_verification"])
        self.assertEqual(payload["warning_verification"], result["status"]["warning_verification"])
        self.assertNotIn("ci_warnings", result)

    def test_ci_warning_rejects_missing_or_populated_clean_marker(self):
        for payload in (
            {key: value for key, value in ci_warning_payload().items() if key != "clean_at_head_sha"},
            {**ci_warning_payload(), "clean_at_head_sha": HEAD},
        ):
            result = self.status(MODULE.STAGE_CI, payload)
            self.assertFalse(result["clear"])
            self.assertEqual("ci_warning_not_verified", result["reason"])

    def test_non_ci_stages_cannot_clear_with_warnings(self):
        for entry in MODULE.STAGES:
            if entry["stage"] == MODULE.STAGE_CI:
                continue
            with self.subTest(stage=entry["stage"]):
                result = self.status(entry["stage"], {
                    **ci_warning_payload(),
                    "clean_at_head_sha": HEAD,
                    "mergeable_at_head_sha": HEAD,
                    "attempt": {"base_sha": BASE},
                    "review": {"clean_at_head_sha": HEAD},
                    "validated_head_sha": HEAD,
                })
                self.assertFalse(result["clear"])
                self.assertEqual("ci_warning_not_verified", result["reason"])

    def test_ci_warning_cannot_clear_without_the_exact_run_status_envelope(self):
        for reason in ("no_state", "status_identity_mismatch", "status_state_mismatch"):
            with (
                self.subTest(reason=reason),
                mock.patch.object(MODULE, "read_stage_status", return_value={
                    "ok": False, "installed": True, "state": "current-run.json",
                    "reason": reason, "payload": ci_warning_payload(),
                }),
            ):
                result = MODULE.inspect_stage(
                    MODULE.STAGE_BY_NAME[MODULE.STAGE_CI],
                    target(), HEAD, BASE, PIPELINE_RUN,
                )
                self.assertFalse(result["clear"])
                self.assertEqual(reason, result["reason"])
                self.assertNotIn("ci_warnings", result)

    def test_hosted_report_warning_is_not_a_coordinator_clearance(self):
        result = self.status(MODULE.STAGE_CI, {
            "stage_outcome": None,
            "clean_at_head_sha": None,
            "report": ci_warning_payload(),
            "canonical_report": ci_warning_payload(),
        })
        self.assertFalse(result["clear"])
        self.assertNotIn("ci_warnings", result)

    def test_ci_warning_cannot_skip_owned_active_or_failed_work(self):
        for state in ("running", "publishing", "failed", "interrupted"):
            with self.subTest(state=state):
                result = self.status(
                    MODULE.STAGE_CI,
                    {**ci_warning_payload(), "agent_task": {"status": state}},
                )
                self.assertFalse(result["clear"])
                self.assertIsNotNone(MODULE.stage_blocker(result, after_launch=False))

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
                    "clean_at_base_sha": BASE,
                    "report_version": 3,
                    "candidate_commits": 0,
                },
                "agent_task": {"status": "completed"},
            },
            MODULE.STAGE_CI: {
                **ci_green_payload(),
                "run": {
                    "head_sha": HEAD,
                    "status": "completed",
                    "report_version": 7,
                    "receipt_version": 3,
                },
            },
            MODULE.STAGE_DESCRIPTION: {
                **description_payload(),
                "proposal": {"version": 3, "decision": "keep"},
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
                **ci_green_payload(),
                "run": {"status": "completed", "head_sha": HEAD},
            },
        )
        stale_green = self.status(
            MODULE.STAGE_CI,
            {
                **ci_green_payload(head=NEXT_HEAD),
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
                    self.assertEqual(
                        "policy_skip_not_verified",
                        result["reason"],
                    )
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

    def test_conflict_clearance_is_head_bound_across_base_movement(self):
        result = self.status(
            MODULE.STAGE_CONFLICT,
            {
                "mergeable_at_head_sha": HEAD,
                "attempt": {"base_sha": BASE},
            },
            NEXT_BASE,
        )
        self.assertTrue(result["clear"])
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
            model="gpt-6-sol",
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

    def test_only_ci_status_requests_live_warning_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "stage.py"
            state = Path(temporary) / "invocation-state.json"
            script.write_text("", encoding="utf-8")
            state.write_text("{}", encoding="utf-8")
            completed = subprocess.CompletedProcess(
                ["python", str(script)], 0,
                json.dumps({"result": "ready", "state": str(state), "pr": target()}),
                "",
            )
            for entry in MODULE.STAGES:
                with (
                    self.subTest(stage=entry["stage"]),
                    mock.patch.object(MODULE.common, "run", return_value=completed) as run,
                ):
                    result = MODULE.common.read_stage_status(
                        entry, target(),
                        script_for=lambda _: script,
                        state_for=lambda *_: state,
                    )
                    self.assertTrue(result["ok"])
                    command = run.call_args.args[0]
                    self.assertEqual(
                        entry["stage"] in {MODULE.STAGE_CI, MODULE.STAGE_DESCRIPTION},
                        "--verify-clearance-snapshot" in command,
                    )
                    self.assertEqual(str(state), command[command.index("--state") + 1])
                    self.assertNotIn("timeout", run.call_args.kwargs)

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

    def test_every_stage_progress_reads_its_current_pipeline_state(self):
        for entry in MODULE.STAGES:
            with self.subTest(stage=entry["stage"]):
                seen = []

                def progress(_entry, selected, *, state_for):
                    seen.append(state_for(entry, selected))
                    return None

                def monitored(_command, *, cwd, log_path, progress):
                    del cwd, log_path
                    progress()
                    return {
                        "returncode": 0,
                        "log_path": "stage.log",
                        "started_at": "start",
                        "ended_at": "end",
                    }

                with (
                    mock.patch.object(
                        MODULE, "stage_command", return_value=["copilot"]
                    ),
                    mock.patch.object(
                        MODULE,
                        "stage_log_path",
                        return_value=Path("stage.log"),
                    ),
                    mock.patch.object(
                        MODULE.common,
                        "stage_live_progress",
                        side_effect=progress,
                    ),
                    mock.patch.object(
                        MODULE.common,
                        "run_monitored",
                        side_effect=monitored,
                    ),
                ):
                    MODULE.run_stage(
                        entry,
                        target(),
                        Path("C:/repo"),
                        model="gpt-6-sol",
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

    def test_unchanged_stage_progress_emits_one_minute_heartbeats(self):
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_COPILOT_REVIEW]
        events = []

        def monitored(_command, *, cwd, log_path, progress):
            del cwd, log_path
            progress()
            progress()
            progress()
            return {
                "returncode": 0,
                "log_path": "stage.log",
                "started_at": "start",
                "ended_at": "end",
            }

        with (
            mock.patch.object(MODULE, "stage_command", return_value=["copilot"]),
            mock.patch.object(
                MODULE, "stage_log_path", return_value=Path("stage.log")
            ),
            mock.patch.object(
                MODULE.common,
                "stage_live_progress",
                return_value={"phase": "addressing_comments"},
            ),
            mock.patch.object(
                MODULE.common, "run_monitored", side_effect=monitored
            ),
            mock.patch.object(
                MODULE.time,
                "monotonic",
                side_effect=[100.0, 100.0, 110.0, 161.0],
            ),
        ):
            MODULE.run_stage(
                entry,
                target(),
                Path("C:/repo"),
                model="gpt-6-sol",
                effort="high",
                run_id=InvocationStateIsolationTest.RUN_ID,
                sweep=1,
                report=events.append,
            )

        self.assertEqual(
            ["stage_progress", "stage_heartbeat"],
            [event["event"] for event in events],
        )
        self.assertEqual(61, events[-1]["elapsed_seconds"])
        transition = MODULE.progress_transition(events[-1])
        self.assertIn("Copilot review addressing comments", transition["message"])
        self.assertIn("1m 1s elapsed", transition["message"])

    def test_nested_hosted_task_transition_replaces_generic_stage_progress(self):
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_CONFLICT]
        events = []

        def monitored(_command, *, cwd, log_path, progress):
            del cwd, log_path
            progress()
            return {
                "returncode": 0,
                "log_path": "stage.log",
                "started_at": "start",
                "ended_at": "end",
            }

        with (
            mock.patch.object(MODULE, "stage_command", return_value=["copilot"]),
            mock.patch.object(
                MODULE, "stage_log_path", return_value=Path("stage.log")
            ),
            mock.patch.object(
                MODULE.common,
                "stage_live_progress",
                return_value={"phase": "running"},
            ),
            mock.patch.object(
                MODULE.common,
                "hosted_task_progress",
                return_value={
                    "phase": "hosted_task",
                    "hosted_task_state": "in_progress",
                    "hosted_task_id": "task-one",
                    "elapsed_seconds": 999,
                },
            ),
            mock.patch.object(
                MODULE.common, "run_monitored", side_effect=monitored
            ),
        ):
            MODULE.run_stage(
                entry,
                target(),
                Path("C:/repo"),
                model="gpt-6-sol",
                effort="high",
                run_id=InvocationStateIsolationTest.RUN_ID,
                sweep=1,
                report=events.append,
            )

        self.assertEqual("in_progress", events[0]["hosted_task_state"])
        self.assertNotEqual(999, events[0]["elapsed_seconds"])
        transition = MODULE.progress_transition(events[0])
        self.assertIn(
            "conflict resolution hosted task in progress",
            transition["message"],
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
        self.launch_calls: list[dict] = []
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

    def sync(self, _repo, _target, *, run_id):
        return {
            "result": "ready",
            "head_sha": self.sync_heads[-1],
            "changed": False,
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
        self.launch_calls.append({
            "stage": entry["stage"],
            "model": model,
            "effort": effort,
            "run_id": run_id,
            "sweep": sweep,
            "conflict_strategy": conflict_strategy,
        })
        self.clear_at[entry["stage"]] = self.sync_heads[-1]
        if entry["stage"] == MODULE.STAGE_CONFLICT:
            self.clear_base_at = self.base_sha
        return {
            "returncode": 0,
            "log_path": f"{sweep}-{entry['stage']}.log",
            "started_at": "start",
            "ended_at": "end",
        }

    def snapshot_commits(self, _target, *, repo_root, base_sha, head_sha):
        head = head_sha
        return {
            "commits": [
                {
                    "sha": head,
                    "title": f"Commit {head[0]}",
                    "url": f"https://github.com/owner/repo/pull/7/commits/{head}",
                }
            ]
        }

    def settle(self, _repo, _target, *, run_id, started_head_sha):
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

    def test_rewritten_checkout_reports_retained_head(self):
        def rewritten_checkout(_repo, _target, *, run_id):
            result = self.sync(_repo, _target, run_id=run_id)
            if not any(event["event"] == "checkout_head_retained" for event in self.events):
                result.update({
                    "previous_head_sha": "a" * 40,
                    "recovery_ref": f"refs/copilot/pr-pipeline/{run_id}/7/{'a' * 40}",
                })
            return result

        with mock.patch.object(MODULE, "sync_worktree", side_effect=rewritten_checkout):
            self.assertEqual("complete", self.execute()["result"])
        event = next(event for event in self.events if event["event"] == "checkout_head_retained")
        self.assertEqual("a" * 40, event["previous_head_sha"])
        self.assertEqual(self.sync_heads[-1], event["head_sha"])
        self.assertIn(event["run_id"], event["recovery_ref"])

    def test_bounded_calls_wait_without_restarting_the_stage(self):
        original_stage = self.run_stage
        observations = 0
        checkpoints = []

        def bounded_stage(entry, *arguments, **options):
            nonlocal observations
            self.assertTrue(options.pop("bounded"))
            if entry["stage"] == MODULE.STAGE_CONFLICT and observations < 4:
                observations += 1
                return {"returncode": 0, "waiting": True, "wait_seconds": 60}
            return original_stage(entry, *arguments, **options)

        def checkpoint(value):
            checkpoints.append(json.loads(json.dumps(value)))

        with mock.patch.object(MODULE, "run_stage", side_effect=bounded_stage):
            outcome = MODULE.run_pipeline(
                target(), self.repo, models=MODULE.stage_models(None), effort="high",
                run_id=PIPELINE_RUN, report=self.events.append,
                checkpoint=checkpoint,
            )
            for _ in range(20):
                if outcome["result"] not in {"waiting", "continue"}:
                    break
                outcome = MODULE.run_pipeline(
                    target(), self.repo, models=MODULE.stage_models(None), effort="high",
                    run_id=PIPELINE_RUN, report=self.events.append,
                    cursor=checkpoints[-1], checkpoint=checkpoint,
                )

        self.assertEqual("complete", outcome["result"])
        self.assertEqual(4, observations)
        self.assertEqual([(stage, 1) for stage in MODULE.STAGE_NAMES], self.launched)
        self.assertEqual(
            1,
            sum(event["event"] == "stage_started" and
                event["stage"] == MODULE.STAGE_CONFLICT for event in self.events),
        )
        self.assertEqual(1, sum(event["event"] == "pipeline_started" for event in self.events))

    def test_bounded_calls_keep_the_second_sweep_and_one_run_identity(self):
        original_stage = self.run_stage
        checkpoints = []
        next_head = "b" * 40

        def bounded_stage(entry, *arguments, **options):
            self.assertTrue(options.pop("bounded"))
            result = original_stage(entry, *arguments, **options)
            if entry["stage"] == MODULE.STAGE_DESCRIPTION and options["sweep"] == 1:
                self.sync_heads[-1] = next_head
            return result

        def checkpoint(value):
            checkpoints.append(json.loads(json.dumps(value)))

        with mock.patch.object(MODULE, "run_stage", side_effect=bounded_stage):
            outcome = MODULE.run_pipeline(
                target(), self.repo, models=MODULE.stage_models(None), effort="high",
                run_id=PIPELINE_RUN, checkpoint=checkpoint,
            )
            for _ in range(20):
                if outcome["result"] not in {"continue", "waiting"}:
                    break
                outcome = MODULE.run_pipeline(
                    target(), self.repo, models=MODULE.stage_models(None), effort="high",
                    run_id=PIPELINE_RUN, cursor=checkpoints[-1], checkpoint=checkpoint,
                )

        self.assertEqual("complete", outcome["result"])
        self.assertEqual(2, outcome["sweeps"])
        self.assertEqual({PIPELINE_RUN}, {call["run_id"] for call in self.launch_calls})
        self.assertEqual(
            [(stage, sweep) for sweep in (1, 2) for stage in MODULE.STAGE_NAMES],
            self.launched,
        )

    def enable_ci_warnings(self):
        self.warning_payload = None
        launch = self.run_stage

        def run_stage(entry, *args, **kwargs):
            result = launch(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_CI:
                self.warning_payload = ci_warning_payload(self.sync_heads[-1], self.base_sha)
            return result

        def inspect(entry, selected, head, base, run_id=None):
            if entry["stage"] != MODULE.STAGE_CI or self.warning_payload is None:
                return self.inspect(entry, selected, head, base, run_id)
            return MODULE.common.inspect_stage(
                entry, selected, head, base,
                pipeline_run=run_id,
                read_status=lambda *_: {
                    "ok": True, "installed": True, "state": "state.json",
                    "payload": self.warning_payload,
                },
            )

        for patch in (
            mock.patch.object(MODULE, "run_stage", side_effect=run_stage),
            mock.patch.object(MODULE, "inspect_stage", side_effect=inspect),
            mock.patch.object(
                MODULE, "inspect_stages",
                side_effect=lambda selected, head, base, run_id=None: [
                    inspect(entry, selected, head, base, run_id) for entry in MODULE.STAGES
                ],
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def enable_ci_green(self, *, second_payload=None):
        self.green_payload = None
        ci_launches = 0
        launch = self.run_stage

        def run_stage(entry, *args, **kwargs):
            nonlocal ci_launches
            result = launch(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_CI:
                ci_launches += 1
                self.green_payload = copy.deepcopy(
                    second_payload
                    if ci_launches == 2 and second_payload is not None
                    else ci_green_payload(self.sync_heads[-1], self.base_sha)
                )
            return result

        def inspect(entry, selected, head, base, run_id=None):
            if entry["stage"] != MODULE.STAGE_CI or self.green_payload is None:
                return self.inspect(entry, selected, head, base, run_id)
            return MODULE.common.inspect_stage(
                entry, selected, head, base,
                pipeline_run=run_id,
                read_status=lambda *_: {
                    "ok": True, "installed": True, "state": "state.json",
                    "payload": self.green_payload,
                },
            )

        for patch in (
            mock.patch.object(MODULE, "run_stage", side_effect=run_stage),
            mock.patch.object(MODULE, "inspect_stage", side_effect=inspect),
            mock.patch.object(
                MODULE, "inspect_stages",
                side_effect=lambda selected, head, base, run_id=None: [
                    inspect(entry, selected, head, base, run_id)
                    for entry in MODULE.STAGES
                ],
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_ci_warning_finishes_workflow_and_continues_description(self):
        self.enable_ci_warnings()
        result = self.execute()
        self.assertEqual("complete", result["result"])
        self.assertEqual(1, result["sweeps"])
        self.assertFalse(result["all_ci_passed"])
        self.assertEqual(self.warning_payload["ci_warnings"], result["ci_warnings"])
        self.assertEqual([(stage, 1) for stage in MODULE.STAGE_NAMES], self.launched)
        for event in [
            *[
                event for event in self.events
                if event["event"] == "sweep_finished"
                or event["event"] == "stage_finished" and event["stage"] == MODULE.STAGE_CI
            ],
            {"event": "pipeline_finished", **result},
        ]:
            update = MODULE.progress_transition(event)
            self.assertIn("WITH CI WARNINGS", update["message"])
            self.assertNotIn("all stages are clear", update["message"])
            self.assertFalse(update["all_ci_passed"])

    def test_unchanged_ci_warning_does_not_repeat_ci_in_second_sweep(self):
        self.enable_ci_warnings()
        original_run_stage = MODULE.run_stage.side_effect

        def run_stage(entry, *args, **kwargs):
            result = original_run_stage(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_SELF_REVIEW and kwargs["sweep"] == 1:
                self.sync_heads.append(NEXT_HEAD)
            return result

        MODULE.run_stage.side_effect = run_stage
        result = self.execute()
        self.assertEqual("complete", result["result"])
        self.assertEqual(2, result["sweeps"])
        self.assertFalse(result["all_ci_passed"])
        self.assertEqual([(MODULE.STAGE_CI, 1)], [item for item in self.launched if item[0] == MODULE.STAGE_CI])
        skipped = next(
            event for event in self.events
            if event["event"] == "stage_finished" and event["stage"] == MODULE.STAGE_CI
            and event["sweep"] == 2
        )
        self.assertEqual("already_clear", skipped["action"])
        self.assertEqual("ci_warning", skipped["clearance_kind"])
        self.assertIn("WITH CI WARNINGS", MODULE.progress_transition(skipped)["message"])

    def assert_ci_warning_refreshed_after_movement(self, revision):
        self.enable_ci_warnings()
        launch = MODULE.run_stage.side_effect

        def run_stage(entry, *args, **kwargs):
            result = launch(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_DESCRIPTION and kwargs["sweep"] == 1:
                if revision == "head":
                    self.sync_heads.append(NEXT_HEAD)
                else:
                    self.base_sha = NEXT_BASE
            return result

        MODULE.run_stage.side_effect = run_stage
        result = self.execute()
        self.assertEqual("complete", result["result"])
        self.assertEqual(2, result["sweeps"])
        self.assertEqual(
            [(MODULE.STAGE_CI, 1)] + ([(MODULE.STAGE_CI, 2)] if revision == "head" else []),
            [item for item in self.launched if item[0] == MODULE.STAGE_CI],
        )
        ci = next(stage for stage in result["stages"] if stage["stage"] == MODULE.STAGE_CI)
        self.assertEqual(self.sync_heads[-1], ci["clear_at_head_sha"])
        self.assertEqual(BASE if revision == "base" else self.base_sha, ci["clear_at_base_sha"])

    def test_head_movement_requires_fresh_ci_warning_work(self):
        self.assert_ci_warning_refreshed_after_movement("head")

    def test_base_movement_does_not_repeat_ci_warning_work(self):
        self.assert_ci_warning_refreshed_after_movement("base")

    def test_ci_warning_does_not_hide_a_later_stage_failure(self):
        self.enable_ci_warnings()
        launch = MODULE.run_stage.side_effect

        def run_stage(entry, *args, **kwargs):
            result = launch(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_DESCRIPTION:
                result["returncode"] = 1
            return result

        MODULE.run_stage.side_effect = run_stage
        result = self.execute()
        self.assertEqual("blocked", result["result"])
        self.assertEqual("stage_execution_failed", result["reason"])
        self.assertFalse(result["all_ci_passed"])
        self.assertEqual(self.warning_payload["ci_warnings"], result["ci_warnings"])

    def test_green_snapshot_change_during_description_uses_second_sweep(self):
        self.enable_ci_green()
        launch = MODULE.run_stage.side_effect

        def run_stage(entry, *args, **kwargs):
            result = launch(entry, *args, **kwargs)
            if (
                entry["stage"] == MODULE.STAGE_DESCRIPTION
                and kwargs["sweep"] == 1
            ):
                self.green_payload = stale_ci_green_payload()
            return result

        MODULE.run_stage.side_effect = run_stage
        result = self.execute()
        self.assertEqual("complete", result["result"])
        self.assertEqual(2, result["sweeps"])
        self.assertEqual(
            [
                *[(stage, 1) for stage in MODULE.STAGE_NAMES],
                (MODULE.STAGE_CI, 2),
            ],
            self.launched,
        )
        run_ids = {call["run_id"] for call in self.launch_calls}
        self.assertEqual(1, len(run_ids))
        self.assertTrue(next(iter(run_ids)))
        ci = next(stage for stage in result["stages"] if stage["stage"] == MODULE.STAGE_CI)
        self.assertEqual(
            "ci_snapshot_current",
            ci["status"]["clearance_verification"]["reason"],
        )

    def test_warning_snapshot_change_during_description_uses_second_sweep(self):
        self.enable_ci_warnings()
        launch = MODULE.run_stage.side_effect

        def run_stage(entry, *args, **kwargs):
            result = launch(entry, *args, **kwargs)
            if (
                entry["stage"] == MODULE.STAGE_DESCRIPTION
                and kwargs["sweep"] == 1
            ):
                self.warning_payload = stale_ci_warning_payload()
            return result

        MODULE.run_stage.side_effect = run_stage
        result = self.execute()
        self.assertEqual("complete", result["result"])
        self.assertEqual(2, result["sweeps"])
        self.assertFalse(result["all_ci_passed"])
        self.assertEqual(self.warning_payload["ci_warnings"], result["ci_warnings"])
        ci = next(stage for stage in result["stages"] if stage["stage"] == MODULE.STAGE_CI)
        self.assertEqual("ci_warning", ci["clearance_kind"])
        self.assertEqual(
            [
                *[(stage, 1) for stage in MODULE.STAGE_NAMES],
                (MODULE.STAGE_CI, 2),
            ],
            self.launched,
        )
        run_ids = {call["run_id"] for call in self.launch_calls}
        self.assertEqual(1, len(run_ids))
        self.assertTrue(next(iter(run_ids)))

    def test_same_revision_sweep_gate_accepts_only_verified_ci_snapshot_drift(self):
        def final_stages(ci):
            return [
                ci if entry["stage"] == MODULE.STAGE_CI
                else clear_stage(entry["stage"])
                for entry in MODULE.STAGES
            ]

        green = {
            **uncleared_stage(MODULE.STAGE_CI),
            "status": {
                "clearance_verification": {
                    "result": "stale",
                    "reason": "ci_snapshot_changed",
                    "expected_snapshot_sha256": "e" * 64,
                    "observed_snapshot_sha256": "f" * 64,
                },
            },
        }
        warning = {
            **uncleared_stage(MODULE.STAGE_CI),
            "status": {},
            "warning_verification": {
                "result": "stale",
                "reason": "ci_warning_snapshot_changed",
                "expected_snapshot_sha256": "e" * 64,
                "observed_snapshot_sha256": "f" * 64,
            },
        }
        self.assertTrue(MODULE.requires_ci_revalidation_sweep(final_stages(green)))
        self.assertTrue(MODULE.requires_ci_revalidation_sweep(final_stages(warning)))

        rejected = {
            "pending": uncleared_stage(MODULE.STAGE_CI, "pending"),
            "failing": uncleared_stage(MODULE.STAGE_CI, "failed"),
            "unknown": uncleared_stage(MODULE.STAGE_CI, "unknown"),
            "exhausted": uncleared_stage(MODULE.STAGE_CI, "max_iterations_reached"),
            "unknown stale reason": {
                **green,
                "status": {
                    "clearance_verification": {
                        **green["status"]["clearance_verification"],
                        "reason": "unexpected_snapshot_reason",
                    },
                },
            },
            "malformed stale warning": {
                **warning,
                "warning_verification": {
                    **warning["warning_verification"],
                    "observed_snapshot_sha256": "not-a-hash",
                },
            },
            "unchanged snapshot": {
                **green,
                "status": {
                    "clearance_verification": {
                        **green["status"]["clearance_verification"],
                        "observed_snapshot_sha256": "e" * 64,
                    },
                },
            },
        }
        for name, stage in rejected.items():
            with self.subTest(name=name):
                self.assertFalse(
                    MODULE.requires_ci_revalidation_sweep(final_stages(stage))
                )
        self.assertFalse(
            MODULE.requires_ci_revalidation_sweep(
                [
                    *final_stages(green),
                    uncleared_stage(MODULE.STAGE_SELF_REVIEW),
                ]
            )
        )

    def test_second_same_revision_ci_drift_ends_incomplete(self):
        self.enable_ci_green()
        launch = MODULE.run_stage.side_effect
        inspect_all = MODULE.inspect_stages.side_effect
        final_inspections = 0

        def run_stage(entry, *args, **kwargs):
            result = launch(entry, *args, **kwargs)
            if (
                entry["stage"] == MODULE.STAGE_DESCRIPTION
                and kwargs["sweep"] == 1
            ):
                self.green_payload = stale_ci_green_payload()
            return result

        def inspect_with_later_drift(*args, **kwargs):
            nonlocal final_inspections
            final_inspections += 1
            if final_inspections == 2:
                self.green_payload = stale_ci_green_payload()
            return inspect_all(*args, **kwargs)

        MODULE.run_stage.side_effect = run_stage
        MODULE.inspect_stages.side_effect = inspect_with_later_drift
        result = self.execute()
        self.assertEqual("incomplete", result["result"])
        self.assertEqual("two_sweeps_finished", result["reason"])
        self.assertEqual(2, result["sweeps"])
        self.assertEqual(
            [(MODULE.STAGE_CI, 1), (MODULE.STAGE_CI, 2)],
            [item for item in self.launched if item[0] == MODULE.STAGE_CI],
        )

    def assert_second_ci_outcome_remains_incomplete(self, second_payload):
        self.enable_ci_green(second_payload=second_payload)
        launch = MODULE.run_stage.side_effect

        def run_stage(entry, *args, **kwargs):
            result = launch(entry, *args, **kwargs)
            if (
                entry["stage"] == MODULE.STAGE_DESCRIPTION
                and kwargs["sweep"] == 1
            ):
                self.green_payload = stale_ci_green_payload()
            return result

        MODULE.run_stage.side_effect = run_stage
        result = self.execute()
        self.assertEqual("incomplete", result["result"])
        self.assertEqual("two_sweeps_finished", result["reason"])
        self.assertEqual(2, result["sweeps"])
        self.assertNotIn("all_ci_passed", result)
        self.assertEqual(
            [
                *[(stage, 1) for stage in MODULE.STAGE_NAMES],
                (MODULE.STAGE_CI, 2),
            ],
            self.launched,
        )

    def test_second_ci_pending_outcome_remains_incomplete(self):
        self.assert_second_ci_outcome_remains_incomplete({
            "stage_outcome": "pending",
            "outcome": None,
            "clean_at_head_sha": None,
            "clean_at_base_sha": None,
        })

    def test_second_ci_failure_outcome_remains_incomplete(self):
        self.assert_second_ci_outcome_remains_incomplete({
            "stage_outcome": "failed",
            "outcome": "failed",
            "clean_at_head_sha": None,
            "clean_at_base_sha": None,
        })

    def test_second_ci_unknown_outcome_remains_incomplete(self):
        self.assert_second_ci_outcome_remains_incomplete({
            "clean_at_head_sha": None,
            "clean_at_base_sha": None,
        })

    def test_second_ci_exhausted_outcome_remains_incomplete(self):
        self.assert_second_ci_outcome_remains_incomplete({
            "stage_outcome": "carried",
            "outcome": None,
            "clean_at_head_sha": None,
            "clean_at_base_sha": None,
            "escalation": {
                "reason": "max_iterations_reached",
                "detail": "the CI repair allowance is exhausted",
            },
        })

    def test_blocked_result_rechecks_same_head_base_warning_snapshot(self):
        self.enable_ci_warnings()
        launch = MODULE.run_stage.side_effect

        def run_stage(entry, *args, **kwargs):
            result = launch(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_DESCRIPTION:
                self.warning_payload = stale_ci_warning_payload()
                result["returncode"] = 1
            return result

        MODULE.run_stage.side_effect = run_stage
        result = self.execute()
        self.assertEqual("blocked", result["result"])
        self.assertEqual("stage_execution_failed", result["reason"])
        self.assertNotIn("ci_warnings", result)
        self.assertNotIn("all_ci_passed", result)

    def test_blocked_result_reports_unverified_warning_snapshot(self):
        self.enable_ci_warnings()
        launch = MODULE.run_stage.side_effect

        def run_stage(entry, *args, **kwargs):
            result = launch(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_DESCRIPTION:
                self.warning_payload.pop("warning_verification")
                result["returncode"] = 1
            return result

        MODULE.run_stage.side_effect = run_stage
        result = self.execute()
        self.assertEqual("blocked", result["result"])
        self.assertEqual("ci_warning_not_verified", result["ci_warning_revalidation_error"])
        self.assertNotIn("ci_warnings", result)
        update = MODULE.progress_transition({"event": "pipeline_finished", **result})
        self.assertIn("CI warning status could not be revalidated", update["message"])

    def test_ci_warning_does_not_override_a_nonzero_ci_exit(self):
        self.enable_ci_warnings()
        launch = MODULE.run_stage.side_effect

        def run_stage(entry, *args, **kwargs):
            result = launch(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_CI:
                result["returncode"] = 1
            return result

        MODULE.run_stage.side_effect = run_stage
        result = self.execute()
        self.assertEqual("blocked", result["result"])
        self.assertEqual("stage_execution_failed", result["reason"])
        self.assertNotIn((MODULE.STAGE_DESCRIPTION, 1), self.launched)
        event = next(
            event for event in self.events
            if event["event"] == "stage_finished" and event["stage"] == MODULE.STAGE_CI
        )
        self.assertIn("failed with exit code 1", MODULE.progress_transition(event)["message"])
        self.assertNotIn("completed", MODULE.progress_transition(event)["message"])

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

    def test_review_exhaustion_preserves_unresolved_status_and_runs_remaining_stages(self):
        original_run, original_inspect = self.run_stage, self.inspect

        def run(entry, *args, **kwargs):
            result = original_run(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_COPILOT_REVIEW:
                self.clear_at[entry["stage"]] = None
            return result

        def inspect(entry, *args, **kwargs):
            result = original_inspect(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_COPILOT_REVIEW and self.launched:
                result["status"] = {
                    "iterations": 5, "max_iterations": 5,
                    "pending_comments": [{"id": "pending-finding"}],
                }
            return result

        MODULE.run_stage.side_effect = run
        MODULE.inspect_stage.side_effect = inspect
        result = self.execute()
        self.assertEqual("incomplete", result["result"])
        self.assertEqual([(stage, 1) for stage in MODULE.STAGE_NAMES], self.launched)
        self.assertFalse(result["stages"][1]["clear"])
        self.assertTrue(all(stage["clear"] for stage in result["stages"][2:]))

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

    def test_source_drift_uses_the_existing_second_sweep_allowance(self):
        original_run_stage = self.run_stage
        original_inspect = self.inspect
        drift = source_drift_payload(expected=HEAD, observed=NEXT_HEAD)[
            "agent_task"
        ]["source_drift"]

        def run_stage(entry, *args, **kwargs):
            result = original_run_stage(entry, *args, **kwargs)
            if (
                entry["stage"] == MODULE.STAGE_DESCRIPTION
                and kwargs["sweep"] == 1
            ):
                self.clear_at[entry["stage"]] = None
                self.sync_heads.append(NEXT_HEAD)
            return result

        def inspect(entry, selected, head, base, run_id=None):
            result = original_inspect(entry, selected, head, base, run_id)
            if (
                entry["stage"] == MODULE.STAGE_DESCRIPTION
                and head == NEXT_HEAD
                and self.clear_at[entry["stage"]] is None
            ):
                result.update(
                    {
                        "outcome": None,
                        "reason": "source_drift",
                        "source_drift": drift,
                        "status": {
                            "agent_task": {
                                "status": "head_changed",
                                "source_drift": drift,
                            }
                        },
                    }
                )
            return result

        MODULE.run_stage.side_effect = run_stage
        MODULE.inspect_stage.side_effect = inspect
        MODULE.inspect_stages.side_effect = (
            lambda selected, head, base, run_id=None: [
                inspect(entry, selected, head, base, run_id)
                for entry in MODULE.STAGES
            ]
        )

        result = self.execute()

        self.assertEqual("complete", result["result"])
        self.assertEqual(2, result["sweeps"])
        description_runs = [
            record
            for record in result["runs"]
            if record["stage"] == MODULE.STAGE_DESCRIPTION
        ]
        self.assertEqual([1, 2], [record["sweep"] for record in description_runs])
        self.assertEqual(drift, description_runs[0]["source_drift"])
        self.assertTrue(description_runs[1]["clear"])

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

    def test_nonzero_stage_exit_reports_the_recorded_task_error(self):
        original_run = self.run_stage
        original_inspect = self.inspect
        task_error = (
            "source_snapshot_mismatch: remote source changed before publication"
        )

        def fail_conflict(entry, *args, **kwargs):
            result = original_run(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_CONFLICT:
                result["returncode"] = 1
                self.clear_at[entry["stage"]] = None
            return result

        def inspect(entry, *args, **kwargs):
            result = original_inspect(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_CONFLICT and self.launched:
                result["status"] = {
                    "agent_task": {"status": "blocked", "error": task_error}
                }
            return result

        MODULE.run_stage.side_effect = fail_conflict
        MODULE.inspect_stage.side_effect = inspect

        result = self.execute()
        self.assertEqual("stage_execution_failed", result["reason"])
        self.assertEqual(task_error, result["detail"])
        self.assertEqual(task_error, result["detail"])
        self.assertNotIn(".log", result["detail"])

    def test_nonzero_stage_exit_preserves_sealed_error_when_state_is_missing(self):
        original = self.run_stage
        sealed_error = "Copilot review body contains unparsed review details"

        def fail_conflict(entry, *args, **kwargs):
            result = original(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_CONFLICT:
                result.update(
                    returncode=1,
                    child_terminal_result={
                        "run_id": "child-run",
                        "result_sha256": "a" * 64,
                        "exit_code": 1,
                        "local_status": "failed",
                        "workflow_result": {
                            "result": "error",
                            "error": sealed_error,
                        },
                        "finalization_errors": [
                            "stage state was not recorded"
                        ],
                    },
                )
                self.clear_at[entry["stage"]] = None
            return result

        MODULE.run_stage.side_effect = fail_conflict
        original_inspect = self.inspect

        def missing_state(entry, *args, **kwargs):
            result = original_inspect(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_CONFLICT and self.launched:
                result.update(
                    reason="no_state",
                    status={},
                    status_state="C:/run/missing-state.json",
                )
            return result

        MODULE.inspect_stage.side_effect = missing_state

        result = self.execute()

        self.assertEqual("stage_execution_failed", result["reason"])
        self.assertEqual(sealed_error, result["detail"])
        stage = result["stage_result"]
        self.assertEqual("no_state", stage["reason"])
        self.assertEqual("child-run", stage["sealed_terminal"]["run_id"])
        self.assertEqual(
            "no_state", stage["missing_state_diagnostic"]["reason"]
        )

    def test_nonzero_stage_exit_reports_sealed_error_when_prior_state_is_stale(self):
        original = self.run_stage
        sealed_error = (
            "pipeline state requires a completed earlier sweep with unchanged identity"
        )

        def fail_conflict(entry, *args, **kwargs):
            result = original(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_CONFLICT:
                result.update(
                    returncode=1,
                    child_terminal_result={
                        "run_id": "child-run",
                        "result_sha256": "a" * 64,
                        "exit_code": 1,
                        "local_status": "failed",
                        "workflow_result": {
                            "result": "error",
                            "error": sealed_error,
                        },
                    },
                )
            return result

        MODULE.run_stage.side_effect = fail_conflict
        result = self.execute()
        self.assertEqual("stage_execution_failed", result["reason"])
        self.assertEqual(sealed_error, result["detail"])
        self.assertEqual("child-run", result["stage_result"]["sealed_terminal"]["run_id"])

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

    def sync(self, local: Path, remote: Path):
        with (
            mock.patch.object(MODULE, "target_remote", return_value=str(remote)),
            mock.patch.object(
                MODULE.common, "head_ref_tip",
                side_effect=lambda _repo, branch: self.git(remote, "rev-parse", f"refs/heads/{branch}"),
            ),
        ):
            return MODULE.sync_worktree(
                local, self.source_target(), run_id="a" * 32,
            )

    def source_target(self):
        return {**target(), "head_branch": "feature", "head_repository": "owner/repo"}

    def test_divergent_detached_commit_is_retained_before_switching(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            self.git(repo, "init", "-q", "-b", "main")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "base")
            published = self.git(repo, "rev-parse", "HEAD")
            self.git(repo, "checkout", "-q", "--detach")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "local")
            previous = self.git(repo, "rev-parse", "HEAD")
            with mock.patch.object(
                MODULE,
                "fetch_pr_head",
                return_value={"result": "ready", "head_sha": published},
            ):
                result = MODULE.sync_worktree(
                    repo, target(), run_id="a" * 32,
                )
            self.assertEqual("ready", result["result"])
            self.assertEqual(published, self.git(repo, "rev-parse", "HEAD"))
            self.assertEqual(previous, self.git(repo, "rev-parse", result["recovery_ref"]))
            self.assertEqual(previous, result["previous_head_sha"])

    def test_rewritten_pr_head_preserves_local_branch_and_recovery_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, base, old_head = self.make_remote(root)
            local = self.clone(root, remote)
            self.git(local, "fetch", "-q", str(remote), "refs/pull/7/head")
            self.git(local, "checkout", "-q", "-b", "feature", "FETCH_HEAD")
            self.git(local, "commit", "-q", "--allow-empty", "-m", "local only")
            local_tip = self.git(local, "rev-parse", "HEAD")
            self.git(remote, "checkout", "-q", "-B", "feature", base)
            self.git(remote, "commit", "-q", "--allow-empty", "-m", "rebased")
            new_head = self.git(remote, "rev-parse", "HEAD")

            result = self.sync(local, remote)

            self.assertEqual("ready", result["result"])
            self.assertEqual(new_head, self.git(local, "rev-parse", "HEAD"))
            self.assertEqual(local_tip, self.git(local, "rev-parse", "feature"))
            self.assertEqual(local_tip, self.git(local, "rev-parse", result["recovery_ref"]))
            self.assertEqual("", self.git(local, "branch", "--show-current"))
            self.git(local, "checkout", "-q", "--detach", local_tip)
            self.assertEqual(result["recovery_ref"], self.sync(local, remote)["recovery_ref"])

    def test_recovery_ref_failure_does_not_move_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            self.git(repo, "init", "-q", "-b", "main")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "base")
            published = self.git(repo, "rev-parse", "HEAD")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "local")
            local_tip = self.git(repo, "rev-parse", "HEAD")
            with (
                mock.patch.object(MODULE, "fetch_pr_head", return_value={
                    "result": "ready", "head_sha": published,
                }),
                mock.patch.object(MODULE.common, "retain_checkout_head", return_value={
                    "result": "blocked", "reason": "recovery_ref_failed", "detail": "denied",
                }),
                mock.patch.object(MODULE, "checkout_fetched_head") as checkout,
            ):
                result = MODULE.sync_worktree(repo, target(), run_id="a" * 32)
            self.assertEqual("recovery_ref_failed", result["reason"])
            self.assertEqual(local_tip, self.git(repo, "rev-parse", "HEAD"))
            checkout.assert_not_called()

    def test_recovery_ref_collision_keeps_local_head(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            self.git(repo, "init", "-q", "-b", "main")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "base")
            published = self.git(repo, "rev-parse", "HEAD")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "local")
            local_tip = self.git(repo, "rev-parse", "HEAD")
            reference = f"refs/copilot/pr-pipeline/{'a' * 32}/7/{local_tip}"
            self.git(repo, "update-ref", reference, published)
            with mock.patch.object(MODULE, "fetch_pr_head", return_value={
                "result": "ready", "head_sha": published,
            }):
                result = MODULE.sync_worktree(repo, target(), run_id="a" * 32)
            self.assertEqual("recovery_ref_failed", result["reason"])
            self.assertEqual(local_tip, self.git(repo, "rev-parse", "HEAD"))
            self.assertEqual(published, self.git(repo, "rev-parse", reference))

    def test_dirty_checkout_remains_blocked_before_rewrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, _base, _head = self.make_remote(root)
            local = self.clone(root, remote)
            (local / "unfinished.txt").write_text("local work", encoding="utf-8")
            before = self.git(local, "rev-parse", "HEAD")
            result = self.sync(local, remote)
            self.assertEqual("dirty_worktree", result["reason"])
            self.assertEqual(before, self.git(local, "rev-parse", "HEAD"))

    def test_stage_does_not_discard_unpublished_commits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, _base, old_head = self.make_remote(root)
            local = self.clone(root, remote)
            self.git(local, "fetch", "-q", str(remote), "refs/pull/7/head")
            self.git(local, "checkout", "-q", "--detach", "FETCH_HEAD")
            self.git(local, "commit", "-q", "--allow-empty", "-m", "unpublished stage")
            local_tip = self.git(local, "rev-parse", "HEAD")
            with (
                mock.patch.object(MODULE, "target_remote", return_value=str(remote)),
                mock.patch.object(MODULE.common, "head_ref_tip", return_value=old_head),
            ):
                result = MODULE.settle_after_stage(
                    local, self.source_target(), run_id="a" * 32, started_head_sha=old_head,
                )
            self.assertEqual("stage_left_unpublished_commits", result["reason"])
            self.assertEqual(local_tip, self.git(local, "rev-parse", "HEAD"))

    def test_stage_settlement_retains_head_rewritten_during_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, base, old_head = self.make_remote(root)
            local = self.clone(root, remote)
            self.git(local, "fetch", "-q", str(remote), "refs/pull/7/head")
            self.git(local, "checkout", "-q", "--detach", "FETCH_HEAD")
            self.git(remote, "checkout", "-q", "-B", "feature", base)
            self.git(remote, "commit", "-q", "--allow-empty", "-m", "rebased")
            new_head = self.git(remote, "rev-parse", "HEAD")
            with (
                mock.patch.object(MODULE, "target_remote", return_value=str(remote)),
                mock.patch.object(
                    MODULE.common, "head_ref_tip", return_value=new_head,
                ),
            ):
                result = MODULE.settle_after_stage(
                    local, self.source_target(), run_id="a" * 32, started_head_sha=old_head,
                )
            self.assertEqual("ready", result["result"])
            self.assertEqual(new_head, self.git(local, "rev-parse", "HEAD"))
            self.assertEqual(old_head, self.git(local, "rev-parse", result["recovery_ref"]))

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
            self.git(remote, "checkout", "-q", "main")

            result = self.sync(local, remote)

            self.assertEqual("ready", result["result"])
            self.assertNotEqual(old_head, new_head)
            self.assertEqual(new_head, self.git(local, "rev-parse", "HEAD"))

    def test_stale_pull_ref_is_not_used_for_commit_tracking_or_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, base, old_head = self.make_remote(root)
            local = self.clone(root, remote)
            self.git(remote, "checkout", "-q", "feature")
            self.git(remote, "commit", "-q", "--allow-empty", "-m", "branch source")
            new_head = self.git(remote, "rev-parse", "HEAD")

            synced = self.sync(local, remote)
            commits = MODULE.read_pr_commits(
                target(), repo_root=local, base_sha=base, head_sha=synced["head_sha"],
            )

            self.assertEqual(old_head, self.git(remote, "rev-parse", "refs/pull/7/head"))
            self.assertEqual(new_head, synced["head_sha"])
            self.assertEqual(new_head, self.git(local, "rev-parse", "HEAD"))
            self.assertEqual([old_head, new_head], [commit["sha"] for commit in commits])

    def test_branch_drift_during_fetch_blocks_before_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, _base, old_head = self.make_remote(root)
            local = self.clone(root, remote)
            self.git(remote, "checkout", "-q", "feature")
            self.git(remote, "commit", "-q", "--allow-empty", "-m", "new remote head")
            original = self.git(local, "rev-parse", "HEAD")
            with (
                mock.patch.object(MODULE, "target_remote", return_value=str(remote)),
                mock.patch.object(MODULE.common, "head_ref_tip", return_value=old_head),
            ):
                result = MODULE.sync_worktree(
                    local, self.source_target(), run_id="a" * 32,
                )
            self.assertEqual("source_head_moved", result["reason"])
            self.assertEqual(original, self.git(local, "rev-parse", "HEAD"))

    def test_post_publication_settlement_uses_branch_while_pull_ref_lags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, base, old_head = self.make_remote(root)
            local = self.clone(root, remote)
            self.assertEqual("ready", self.sync(local, remote)["result"])
            before = MODULE.read_pr_commits(
                target(), repo_root=local, base_sha=base, head_sha=old_head,
            )
            self.git(remote, "checkout", "-q", "feature")
            self.git(remote, "commit", "-q", "--allow-empty", "-m", "published change")
            new_head = self.git(remote, "rev-parse", "HEAD")
            with (
                mock.patch.object(MODULE, "target_remote", return_value=str(remote)),
                mock.patch.object(MODULE.common, "head_ref_tip", return_value=new_head),
            ):
                settled = MODULE.settle_after_stage(
                    local, self.source_target(), run_id="a" * 32,
                    started_head_sha=old_head,
                )
            after = MODULE.read_pr_commits(
                target(), repo_root=local, base_sha=base, head_sha=settled["head_sha"],
            )
            added, errors, rewritten = MODULE.commits_added(
                {"commits": before}, {"commits": after}
            )
            self.assertEqual("ready", settled["result"])
            self.assertEqual(old_head, self.git(remote, "rev-parse", "refs/pull/7/head"))
            self.assertEqual([new_head], [commit["sha"] for commit in added])
            self.assertEqual([], errors)
            self.assertFalse(rewritten)

    def test_fork_source_fetches_the_fork_branch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, _base, old_head = self.make_remote(root)
            local = self.clone(root, remote)
            fork = root / "fork"
            subprocess.run(
                ["git", "clone", "-q", str(remote), str(fork)],
                check=True, capture_output=True, text=True,
            )
            self.git(fork, "checkout", "-q", "feature")
            self.git(fork, "commit", "-q", "--allow-empty", "-m", "fork change")
            fork_head = self.git(fork, "rev-parse", "HEAD")
            source = {**self.source_target(), "head_repository": "fork/repo"}
            with (
                mock.patch.object(MODULE, "target_remote", side_effect=lambda _repo, value: (
                    str(fork) if value["repo_name"] == "fork/repo" else str(remote)
                )),
                mock.patch.object(MODULE.common, "head_ref_tip", return_value=fork_head),
            ):
                result = MODULE.sync_worktree(local, source, run_id="a" * 32)
            self.assertEqual("ready", result["result"])
            self.assertEqual(fork_head, result["head_sha"])
            self.assertNotEqual(old_head, result["head_sha"])


class AgentInstructionTest(unittest.TestCase):
    def test_requires_the_exact_primary_model_and_exposed_effort(self):
        text = AGENT.read_text(encoding="utf-8")
        self.assertIn("only with model `gpt-6-sol`", text)
        self.assertIn("require `high`", text)
        self.assertIn("An unavailable effort value is allowed", text)
        self.assertIn("cannot be determined", text)

    def test_agent_drives_bounded_steps_without_background_processes(self):
        text = AGENT.read_text(encoding="utf-8")
        self.assertIn("copilot plugin list --json", text)
        self.assertIn(
            "installed-plugins\\trask-plugins\\pr-pipeline\\scripts\\pr_pipeline.py",
            text,
        )
        self.assertIn("at most two sweeps", text)
        self.assertIn("each receive a fresh iteration allowance on a later sweep", text)
        self.assertIn('start "<target>"', text)
        self.assertIn('advance "<target>" --run-id "<run_id>"', text)
        self.assertIn("Invoke each helper call synchronously", text)
        self.assertIn(
            "Do not send a user-visible response while a command is running",
            text,
        )
        self.assertIn("Keep doing this without asking the user to resume", text)
        self.assertIn("Do not use asynchronous mode, background execution", text)
        self.assertNotIn("execution-status", text)
        self.assertNotIn("execution-cancel", text)
        self.assertNotIn("execution tool's asynchronous mode", text)
        self.assertIn("verified terminal `workflow_result`", text)
        self.assertIn("verified Markdown presentation exactly", text)

    def test_agent_forbids_recursive_or_checkout_entrypoint_discovery(self):
        text = AGENT.read_text(encoding="utf-8")
        self.assertIn("Never use recursive filesystem discovery", text)
        self.assertIn("never run a helper from the current repository", text)
        self.assertIn("source checkout", text)
        self.assertNotIn("<installed-pr-pipeline>", text)
        self.assertIn("Do not reconstruct a summary from workflow state", text)
        self.assertNotIn("--execution-handle", text)
        self.assertNotIn("--pipeline-run", text)
        self.assertIn("does not pass stack membership, `--whole-stack`", text)


class CommandOutputTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patch = mock.patch.object(MODULE, "copilot_home", return_value=Path(temporary.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_run_emits_json_lines_ending_with_pipeline_result(self):
        def fake_pipeline(
            _target, _repo, *, models, effort, conflict_strategy, report, run_id
        ):
            self.assertIsNotNone(models)
            self.assertEqual("high", effort)
            self.assertEqual("auto", conflict_strategy)
            report({"event": "pipeline_started", "run_id": run_id})
            report(
                {
                    "event": "stage_started",
                    "stage": MODULE.STAGE_CONFLICT,
                    "sweep": 1,
                }
            )
            return {
                "result": "complete",
                "run_id": run_id,
                "head_sha": HEAD,
                "pr": pull_request(),
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
        self.assertEqual("PR Pipeline: #7 - Add a thing", events[-1]["session_title"])

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

    def test_unexpected_progress_error_is_a_terminal_json_event(self):
        output = StringIO()
        with (
            mock.patch.object(MODULE, "require_tools", side_effect=TypeError("duplicate progress field")),
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
        self.assertEqual("duplicate progress field", event["error"])


class BoundedCommandTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patch = mock.patch.object(MODULE, "copilot_home", return_value=Path(temporary.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.session = mock.patch.dict(
            MODULE.os.environ, {"COPILOT_AGENT_SESSION_ID": "session-one"}
        )
        self.session.start()
        self.addCleanup(self.session.stop)
        self.tools = [
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=Path("C:/repo")),
            mock.patch.object(MODULE, "resolve_target", return_value=target()),
        ]
        for patch in self.tools:
            patch.start()
            self.addCleanup(patch.stop)

    def start(self):
        output = StringIO()
        with redirect_stdout(output):
            MODULE.command_start(
                MODULE.build_parser().parse_args(["start", "owner/repo#7"])
            )
        return json.loads(output.getvalue())

    def test_start_and_advance_are_bound_to_one_session(self):
        started = self.start()
        state_path = Path(started["state"])
        self.assertEqual("continue", started["result"])

        def wait(_target, _repo, *, cursor, checkpoint, **_options):
            self.assertIsNone(cursor)
            checkpoint({"sweep": 1, "stage_index": 0, "pending_stage": {
                "phase": "waiting", "stage_index": 0,
            }})
            return {"result": "waiting", "run_id": started["run_id"],
                    "stage": MODULE.STAGE_CONFLICT, "wait_seconds": 60}

        args = MODULE.build_parser().parse_args([
            "advance", "owner/repo#7", "--run-id", started["run_id"],
        ])
        output = StringIO()
        with mock.patch.object(MODULE, "run_pipeline", side_effect=wait), redirect_stdout(output):
            MODULE.command_advance(args)
        self.assertEqual("waiting", json.loads(output.getvalue())["result"])
        self.assertEqual("waiting", json.loads(state_path.read_text())["status"])
        with mock.patch.dict(
            MODULE.os.environ, {"COPILOT_AGENT_SESSION_ID": "another-session"}
        ), self.assertRaisesRegex(MODULE.WorkflowError, "another session"):
            MODULE.command_advance(args)

    def test_failed_step_cannot_be_advanced_again(self):
        started = self.start()
        args = MODULE.build_parser().parse_args([
            "advance", "owner/repo#7", "--run-id", started["run_id"],
        ])

        def interrupted(_target, _repo, *, checkpoint, **_options):
            checkpoint({"sweep": 1, "stage_index": 0, "pending_stage": {
                "phase": "executing", "stage_index": 0,
            }})
            raise MODULE.WorkflowError("stage interrupted")

        with mock.patch.object(MODULE, "run_pipeline", side_effect=interrupted):
            with self.assertRaisesRegex(MODULE.WorkflowError, "stage interrupted"):
                MODULE.command_advance(args)
        self.assertEqual(
            "blocked", json.loads(Path(started["state"]).read_text())["status"]
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "cannot advance"):
            MODULE.command_advance(args)

    def test_next_step_requires_the_previous_root_to_have_sealed_success(self):
        state = {"previous_execution": {"root": "C:/record/handle.json", "run_id": "root-one"}}
        runtime = mock.Mock()
        runtime.status.return_value = {
            "run_id": "root-one", "terminal": True, "exit_code": 0,
            "local_status": "finished", "local_children_drained": True,
            "workflow_result": {"result": "waiting"},
        }
        with (
            mock.patch.object(MODULE.common, "_EXECUTION", object()),
            mock.patch.dict(
                MODULE.sys.modules, {"_trask_foreground_execution": runtime}
            ),
        ):
            MODULE.require_finished_step(state)
            runtime.status.return_value["local_status"] = "failed"
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "did not finish safely"
            ):
                MODULE.require_finished_step(state)

    def test_bounded_stage_requires_a_sealed_structured_result(self):
        with mock.patch.object(
            MODULE.common, "run_monitored",
            return_value={"returncode": 0, "child_terminal_result": {
                "workflow_result": {"result": "waiting", "wait_seconds": 60},
            }},
        ):
            stage = MODULE.run_stage(
                MODULE.STAGES[0], target(), Path("C:/repo"), model="gpt-6-sol",
                effort="high", run_id="a" * 32, sweep=1, bounded=True,
            )
        self.assertTrue(stage["waiting"])
        self.assertEqual(60, stage["wait_seconds"])

    def test_bounded_stage_can_finish_after_old_step_deadline(self):
        def monitor(_command, *, progress, **_options):
            progress()
            return {
                "returncode": 0,
                "child_terminal_result": {
                    "workflow_result": {"result": "waiting", "wait_seconds": 1},
                },
            }

        with (
            mock.patch.object(MODULE.time, "monotonic", side_effect=[0.0, 3600.0]),
            mock.patch.object(MODULE.common, "run_monitored", side_effect=monitor),
            mock.patch.object(MODULE.common, "stage_live_progress", return_value=None),
            mock.patch.object(MODULE.common, "hosted_task_progress", return_value=None),
        ):
            stage = MODULE.run_stage(
                MODULE.STAGES[0], target(), Path("C:/repo"), model="gpt-6-sol",
                effort="high", run_id="a" * 32, sweep=1, bounded=True,
            )
        self.assertTrue(stage["waiting"])


class ParserTest(unittest.TestCase):
    def test_pipeline_exposes_bounded_session_commands(self):
        parser = MODULE.build_parser()
        action = next(
            action
            for action in parser._actions
            if isinstance(action, __import__("argparse")._SubParsersAction)
        )
        self.assertEqual({"run", "start", "advance"}, set(action.choices))

    def test_removed_commands_fail_before_side_effects(self):
        for command in ("watch", "cancel"):
            side_effect = mock.Mock()
            with (
                self.subTest(command=command),
                mock.patch.object(MODULE.sys, "argv", ["pr_pipeline.py", command]),
                mock.patch.object(MODULE, "require_tools", side_effect=side_effect),
                self.assertRaises(SystemExit),
            ):
                MODULE.main()
            side_effect.assert_not_called()

    def test_run_accepts_model_overrides(self):
        args = MODULE.build_parser().parse_args(
            ["run", "owner/repo#7", "--stage-model", "ci-fix-loop=claude-sonnet-5"]
        )
        self.assertEqual("owner/repo#7", args.target)
        self.assertEqual(["ci-fix-loop=claude-sonnet-5"], args.stage_model)

    def test_execution_controls_use_the_runtime_entrypoint(self):
        runtime = mock.Mock()
        runtime.entrypoint.return_value = 17
        with (
            mock.patch.object(MODULE.sys, "argv", ["pr_pipeline.py", "execution-status"]),
            mock.patch.object(MODULE, "_load_execution", return_value=runtime),
        ):
            self.assertEqual(17, MODULE.execution_main())
        runtime.entrypoint.assert_called_once_with(
            MODULE.main, MODULE.__dict__, commands=("run", "start", "advance")
        )

    def test_session_owned_run_uses_the_runtime_entrypoint(self):
        runtime = mock.Mock()
        runtime.entrypoint.return_value = 17
        with (
            mock.patch.object(MODULE.sys, "argv", ["pr_pipeline.py", "run", "7"]),
            mock.patch.dict(
                MODULE.os.environ,
                {"COPILOT_AGENT_SESSION_ID": "87654321-4321-4321-4321-cba987654321"},
                clear=True,
            ),
            mock.patch.object(MODULE, "_load_execution", return_value=runtime),
        ):
            self.assertEqual(17, MODULE.execution_main())
        runtime.entrypoint.assert_called_once_with(
            MODULE.main, MODULE.__dict__, commands=("run", "start", "advance")
        )


if __name__ == "__main__":
    unittest.main()
