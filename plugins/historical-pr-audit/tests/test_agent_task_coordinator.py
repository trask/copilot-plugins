import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "historical_pr_audit.py"
AGENT = ROOT / "agents" / "historical-pr-audit.agent.md"
SPEC = importlib.util.spec_from_file_location("historical_pr_audit_agent_task", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
RUNTIME_ROOT = (
    ROOT.parent
    / "agent-tasks-runtime"
    / "skills"
    / "agent-tasks-runtime"
    / "scripts"
)

VALIDATION = [
    {
        "command": "python -m pytest tests/test_app.py",
        "outcome": "passed",
    }
]
PIPELINE = {"run": "run-1", "iteration": "2", "max_iterations": "7"}
METADATA = {
    "number": 7,
    "title": "Add a thing",
    "body": "Historical body",
    "pr_url": "https://github.com/owner/repo/pull/7",
    "repo_name": "owner/repo",
    "state": "MERGED",
    "merged_at": "2024-05-01T00:00:00Z",
    "merge_commit": "3" * 40,
    "upstream_owner": "owner",
    "upstream_repo": "repo",
    "head_owner": "owner",
    "head_repo": "repo",
    "head_branch": "feature",
    "head_sha": "2" * 40,
    "base_branch": "main",
    "base_sha": "1" * 40,
    "commits": [{"sha": "2" * 40, "message": "Change app"}],
}


class RuntimeLoaderTest(unittest.TestCase):
    def test_embedded_loaders_accept_current_runtime_sources(self):
        cloud = MODULE.load_cloud_task_runtime(RUNTIME_ROOT / "cloud_task.py")
        execution = MODULE.load_execution_runtime(RUNTIME_ROOT / "execution.py")

        self.assertTrue(callable(cloud.verify_current_candidate))
        self.assertTrue(callable(cloud.guarded_fast_forward_candidate))
        self.assertTrue(callable(execution.entrypoint))


class CandidateOutcomeTest(unittest.TestCase):
    def validate(self, outcome, *, commits=None):
        verified = {
            "artifact_commit": {"sha": "3" * 40, "changed_paths": [MODULE.AUDIT_OUTCOME_PATH]},
            "commits": [] if commits is None else commits,
            "task": {"id": "fresh-task"}, "code_tip": "2" * 40,
            "candidate": {}, "completion": {},
        }
        runtime = SimpleNamespace(
            PullRequestSnapshot=SimpleNamespace, Options=SimpleNamespace,
            GitRepository=mock.Mock,
            verify_current_candidate=mock.Mock(return_value=verified),
            CloudError=RuntimeError,
        )
        with (
            mock.patch.object(MODULE, "load_cloud_task_runtime", return_value=runtime),
            mock.patch.object(MODULE, "git", return_value=json.dumps(outcome)),
        ):
            value = MODULE.validate_audit_candidate(
                {"generated": {"branch": "copilot/fresh", "head_sha": "3" * 40}},
                helper=Path("helper.py"), repo_root=Path("repo"), metadata=METADATA,
                requested_model="gpt-5.6-sol", prompt="audit", max_iterations=5,
            )
        runtime.verify_current_candidate.assert_called_once()
        return value

    def test_clean_code_and_no_code_account_for_hosted_passes(self):
        for commits, expected in (([], "no_change"), (["2" * 40], "clean")):
            with self.subTest(commits=commits):
                _, _, report = self.validate({"outcome": "clean", "iterations_used": 3}, commits=commits)
                self.assertEqual({"outcome": expected, "iterations_used": 3}, report)

    def test_exhaustion_is_not_clean(self):
        _, _, report = self.validate({"outcome": "exhausted", "iterations_used": 5}, commits=["2" * 40])
        self.assertEqual({"outcome": "max_iterations_reached", "iterations_used": 5}, report)

    def test_incomplete_and_invalid_counts_cannot_authorize_import(self):
        for outcome in (
            {"outcome": "incomplete", "iterations_used": 0},
            {"outcome": "clean", "iterations_used": True},
            {"outcome": "clean", "iterations_used": 0},
            {"outcome": "clean", "iterations_used": 6},
            {"outcome": "exhausted", "iterations_used": 4},
            {"outcome": "clean"},
            {"outcome": "clean", "iterations_used": 1, "sha": "invented"},
            {"outcome": [], "iterations_used": 1},
        ):
            with self.subTest(outcome=outcome), self.assertRaises(MODULE.WorkflowError):
                self.validate(outcome)

    def test_no_code_creates_no_remote_branch_and_lost_push_response_is_confirmed(self):
        for commits in ([], ["2" * 40]):
            with self.subTest(commits=commits), tempfile.TemporaryDirectory() as directory:
                state = {
                    "pr": METADATA, "audit_branch": "trask-pr-audit-7",
                    "original": {"head_branch": "feature"}, "audit": {},
                    "agent_task": {"reserved_iterations": 5}, "max_iterations": 5,
                }
                remote = {
                    "commits": commits, "final_local_head": "2" * 40,
                    "report_data": {"outcome": "clean" if commits else "no_change", "iterations_used": 3},
                    "task": {"id": "fresh-task"},
                }
                with (
                    mock.patch.object(MODULE, "local_identity", return_value={
                        "branch": "trask-pr-audit-7", "head": "2" * 40, "status": "",
                    }),
                    mock.patch.object(MODULE, "merged_metadata_for", return_value=METADATA),
                    mock.patch.object(MODULE, "find_remote", return_value="origin"),
                    mock.patch.object(MODULE, "remote_head", return_value=None) as remote_head,
                    mock.patch.object(MODULE, "wait_for_remote_head", return_value="2" * 40) as confirm,
                    mock.patch.object(MODULE, "run", return_value=SimpleNamespace(returncode=1)) as push,
                ):
                    result = MODULE.publish_agent_task_result(
                        Path(directory), state_path=Path(directory) / "state.json",
                        state=state, remote=remote,
                    )
                self.assertEqual(bool(commits), result["pushed"])
                self.assertEqual(3, result["iterations"])
                self.assertEqual(0, state["agent_task"]["reserved_iterations"])
                if commits:
                    self.assertIn("--force-with-lease=refs/heads/trask-pr-audit-7:", push.call_args.args[0])
                    self.assertFalse(push.call_args.kwargs["check"])
                    confirm.assert_called_once()
                else:
                    push.assert_not_called()
                    remote_head.assert_not_called()
                    confirm.assert_not_called()


class AgentTaskCoordinatorTest(unittest.TestCase):
    def test_fresh_invocation_cannot_publish_a_retained_validated_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo_root = root / "repo"
            repo_root.mkdir()
            state_path = root / "state.json"
            MODULE.save_state(
                state_path,
                {
                    "version": MODULE.STATE_VERSION,
                    "max_iterations": 5,
                    "pr": METADATA,
                    "agent_task": {
                        "status": "validated",
                        "invocation_id": "original-run",
                        "model": "gpt-5.6-sol",
                        "pipeline": {
                            "run": None,
                            "iteration": None,
                            "max_iterations": None,
                        },
                        "validated": {},
                    },
                },
            )
            args = MODULE.build_parser().parse_args(
                ["agent-task", METADATA["pr_url"]]
            )
            args.repo_root = str(repo_root)
            args.state = str(state_path)
            with (
                mock.patch.object(MODULE, "require_tools"),
                mock.patch.object(
                    MODULE, "resolve_repo_root", return_value=repo_root
                ),
                mock.patch.object(
                    MODULE, "discover_cloud_task", return_value=root / "helper.py"
                ),
                mock.patch.object(MODULE, "finish_agent_task") as finish,
                self.assertRaisesRegex(
                    MODULE.WorkflowError, "retained audit invocations"
                ),
            ):
                MODULE.command_agent_task(args)

            finish.assert_not_called()

    def test_agent_is_thin_and_explicit(self):
        instructions = AGENT.read_text(encoding="utf-8")
        for text in (
            "disable-model-invocation: true",
            "tools: [execute, rename_session, rename_branch]",
            "python <helper> agent-task <target>",
            "execution-status` takes no arguments",
            "Use the verified `session_title`",
        ):
            self.assertIn(text, instructions)
        self.assertNotIn("--execution-handle", instructions)
        self.assertNotIn("--pipeline-run", instructions)
        self.assertNotIn("tools: [read, edit, search", instructions)
        self.assertNotIn("custom_agent:", instructions)

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
            mock.patch.object(
                MODULE.sys, "argv", ["helper", "execution-status"]
            ),
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
                ["helper", "agent-task", "7", "--pipeline-run", "run"],
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
                ["helper", "agent-task", "7", "--pipeline-run", "run"],
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

    def test_pins_shared_helper_and_current_policy(self):
        self.assertEqual(
            MODULE.REQUIRED_CLOUD_TASK_SHA256,
            "1307b4c754ed4e3bf32339020a28ef81bded8ff4be7c77e07240e1793f43cb4b",
        )
        self.assertEqual(
            MODULE.AGENT_TASK_POLICY,
            "marketplace-agent-code-candidate-worker@1",
        )

    def test_prompt_is_versioned_untrusted_and_remote_only(self):
        prompt = MODULE.build_worker_prompt(
            METADATA,
            audit_branch="trask-pr-audit-7",
            max_iterations=5,
            pipeline=PIPELINE,
        )
        self.assertIn(MODULE.AUDIT_OUTCOME_PATH, prompt)
        self.assertIn("iterations_used", prompt)
        for text in (
            "worker prompt version 2",
            "untrusted data",
            "Never request, read, print, persist, or transmit credentials",
            "Do not select a custom_agent",
            "Do not use Cloud Sandboxes",
            "Perform every substantive action in this Agent Task",
            "Run at most 5 audit iterations",
            "single-parent fix commit",
            "exhausted",
            "optional",
        ):
            self.assertIn(text, prompt)
        self.assertFalse(MODULE.contains_credentials(prompt))

    def test_artifacts_are_absolute_and_outside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            with self.assertRaisesRegex(MODULE.WorkflowError, "must be absolute"):
                MODULE.require_outside_repository(Path("result.json"), repo)
            with self.assertRaisesRegex(MODULE.WorkflowError, "outside the repository"):
                MODULE.require_outside_repository(repo / "result.json", repo)
            MODULE.require_outside_repository(repo.parent / "result.json", repo)

    def test_preparation_recovery_accepts_the_already_prepared_clean_branch(self):
        with (
            mock.patch.object(
                MODULE,
                "local_identity",
                return_value={
                    "branch": "trask-pr-audit-7",
                    "head": METADATA["head_sha"],
                    "status": "",
                },
            ),
            mock.patch.object(MODULE, "remote_head", return_value=None),
        ):
            prepared = MODULE.prepared_branch_after_interruption(
                Path("C:/repo"),
                metadata=METADATA,
                audit_branch="trask-pr-audit-7",
            )
        self.assertEqual(prepared["branch_action"], "recovered_preparation")
        self.assertEqual(prepared["local_head"], METADATA["head_sha"])

    def test_execute_uses_subprocess_and_no_window_wrapper(self):
        completed = MODULE.subprocess.CompletedProcess(["python"], 0, "", "")
        root = Path(tempfile.gettempdir()).resolve()
        with mock.patch.object(MODULE, "run", return_value=completed) as run:
            actual = MODULE.execute_managed_agent_task(
                root / "copilot" / "cloud_task.py",
                repo_root=root / "repo",
                metadata=METADATA,
                model_alias="sol",
                prompt_path=root / "state" / "prompt.txt",
                result_path=root / "state" / "result.json",
            )
        self.assertIs(actual, completed)
        self.assertEqual(
            run.call_args.kwargs,
            {"cwd": root / "repo", "check": False},
        )
        self.assertNotIn("ApiClient", MODULE.execute_managed_agent_task.__code__.co_names)
        self.assertNotIn("start_task", MODULE.execute_managed_agent_task.__code__.co_names)

    def test_cleanup_removes_all_attempt_artifacts_only_after_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            prompt = root / "prompt.txt"
            result_one = root / "result-1.json"
            result_two = root / "result-2.json"
            for path in (prompt, result_one, result_two):
                path.write_text("data", encoding="utf-8")
            state = {
                "version": MODULE.STATE_VERSION,
                "pr": METADATA,
                "agent_task": {
                    "status": "completed",
                    "prompt_file": str(prompt),
                    "result_file": str(result_two),
                    "prior_result_file": str(result_one),
                    "result_files": [str(result_one), str(result_two)],
                },
            }
            MODULE.save_state(state_path, state)
            self.assertEqual(
                MODULE.cleanup_agent_task_artifacts(state_path, state), []
            )
            self.assertFalse(prompt.exists())
            self.assertFalse(result_one.exists())
            self.assertFalse(result_two.exists())
            saved = MODULE.load_state(state_path)
            self.assertTrue(saved["agent_task"]["artifacts_removed"])
            self.assertNotIn("result_files", saved["agent_task"])

if __name__ == "__main__":
    unittest.main()
