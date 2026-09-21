import contextlib
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
            GitRepository=mock.Mock, verify_candidate_result=mock.Mock(return_value=verified),
            CloudError=RuntimeError,
        )
        with (
            mock.patch.object(MODULE, "load_candidate_runtime", return_value=runtime),
            mock.patch.object(MODULE, "git", return_value=json.dumps(outcome)),
        ):
            return MODULE.validate_audit_candidate(
                {"generated": {"branch": "copilot/fresh", "head_sha": "3" * 40}},
                helper=Path("helper.py"), repo_root=Path("repo"), metadata=METADATA,
                requested_model="gpt-5.6-sol", prompt="audit", max_iterations=5,
            )

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


def result(commits=None):
    commits = [] if commits is None else commits
    generated_head = "4" * 40
    return {
        "schema": MODULE.AGENT_TASK_RESULT_SCHEMA,
        "status": "success",
        "mode": "apply_with_report",
        "repository": {"name_with_owner": "owner/repo"},
        "pull_request": MODULE.expected_result_pull_request(METADATA),
        "requested_model": "gpt-5.6-sol",
        "policy": {
            "id": "marketplace-agent-apply-report-worker",
            "version": 3,
            "sha256": MODULE.AGENT_TASK_POLICY_SHA256,
        },
        "task": {
            "id": "task-1",
            "url": "https://github.com/owner/repo/agent-tasks/1",
            "state": "completed",
            "base_ref": METADATA["head_sha"],
            "base_sha": METADATA["head_sha"],
        },
        "generated": {
            "branch": "copilot/task-1",
            "head_sha": generated_head,
            "commits": commits,
        },
        "application": {
            "status": "not_applied",
            "final_local_head": METADATA["head_sha"],
        },
        "report": {
            "path": ".github/agent-task-reports/request-1.md",
            "commit": generated_head,
            "sha256": "5" * 64,
        },
        "attestation": {
            "kind": "dispatcher_structural",
            "structural_complete": True,
        },
        "error": None,
    }


def interrupted_result():
    value = result()
    value["status"] = "interrupted"
    value["task"]["state"] = "in_progress"
    value["generated"] = {"branch": None, "head_sha": None, "commits": []}
    value["application"] = {
        "status": "not_applied",
        "final_local_head": METADATA["head_sha"],
    }
    value["report"]["commit"] = None
    value["report"]["sha256"] = None
    value["attestation"]["structural_complete"] = False
    value["error"] = {"code": "interrupted", "message": "monitoring interrupted"}
    return value


def receipt():
    return [dict(item) for item in VALIDATION]


def report(commits=None, outcome=None):
    commits = [] if commits is None else commits
    outcome = outcome or ("clean" if commits else "no_change")
    iterations = [
        {
            "number": 1,
            "head_before": METADATA["head_sha"],
            "outcome": "fixed" if commits else "clean",
            "finding_count": len(commits),
            "commit_shas": commits,
        }
    ]
    if commits:
        iterations.append(
            {
                "number": 2,
                "head_before": commits[-1],
                "outcome": "clean",
                "finding_count": 0,
                "commit_shas": [],
            }
        )
    return {
        "schema": MODULE.AUDIT_REPORT_SCHEMA,
        "request_id": "request-1",
        "repository": "owner/repo",
        "source_pull_request": {
            "number": 7,
            "url": METADATA["pr_url"],
            "base_sha": METADATA["base_sha"],
            "head_sha": METADATA["head_sha"],
            "title_sha256": MODULE.sha256_text(METADATA["title"]),
            "body_sha256": MODULE.sha256_text(METADATA["body"]),
        },
        "audit_branch": "trask-pr-audit-7",
        "outcome": outcome,
        "iterations": iterations,
        "max_iterations": 5,
        "commits": [
            {"sha": sha, "summary": "Fix finding", "paths": ["app.py"]}
            for sha in commits
        ],
        "pipeline": {
            **PIPELINE,
            "stage_outcome": (
                None if outcome == "max_iterations_reached" else "cleared"
            ),
        },
    }


class AgentTaskCoordinatorTest(unittest.TestCase):
    def test_recovery_is_rejected_before_tools_or_state_access(self):
        args = MODULE.build_parser().parse_args(
            ["agent-task", "owner/repo#7", "--recover"]
        )
        with mock.patch.object(MODULE, "require_tools") as require_tools:
            with self.assertRaisesRegex(MODULE.WorkflowError, "disabled"):
                MODULE.command_agent_task(args)
        require_tools.assert_not_called()

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
                [
                    "agent-task",
                    METADATA["pr_url"],
                    "--repo-root",
                    str(repo_root),
                    "--state",
                    str(state_path),
                ]
            )
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

    def test_same_invocation_cannot_reenter_retained_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo_root = root / "repo"
            repo_root.mkdir()
            state_path = root / "state.json"
            pipeline = {
                "run": None,
                "iteration": None,
                "max_iterations": None,
            }
            state = {
                "version": MODULE.STATE_VERSION,
                "max_iterations": 5,
                "pr": METADATA,
                "agent_task": {
                    "status": "validated",
                    "invocation_id": "original-run",
                    "model": "gpt-5.6-sol",
                    "pipeline": pipeline,
                    "validated": {"head_sha": "4" * 40},
                },
            }
            MODULE.save_state(state_path, state)
            args = MODULE.build_parser().parse_args(
                [
                    "agent-task",
                    METADATA["pr_url"],
                    "--repo-root",
                    str(repo_root),
                    "--state",
                    str(state_path),
                    "--invocation-run",
                    "original-run",
                ]
            )
            original = state_path.read_bytes()
            with (
                mock.patch.object(MODULE, "require_tools"),
                mock.patch.object(
                    MODULE, "resolve_repo_root", return_value=repo_root
                ),
                mock.patch.object(
                    MODULE, "discover_cloud_task", return_value=root / "helper.py"
                ),
                mock.patch.object(
                    MODULE, "merged_metadata_for", return_value=METADATA
                ),
                mock.patch.object(
                    MODULE, "finish_agent_task"
                ) as finish,
                mock.patch.object(MODULE, "emit") as emit,
            ):
                for status in ("validated", "publication_failed"):
                    with self.subTest(status=status):
                        state["agent_task"]["status"] = status
                        MODULE.save_state(state_path, state)
                        original = state_path.read_bytes()
                        with self.assertRaisesRegex(MODULE.WorkflowError, "retained audit invocations"):
                            MODULE.command_agent_task(args)
                        self.assertEqual(state_path.read_bytes(), original)

            finish.assert_not_called()
            emit.assert_not_called()

    def test_agent_is_thin_and_explicit(self):
        instructions = AGENT.read_text(encoding="utf-8")
        for text in (
            "disable-model-invocation: true",
            "tools: [execute, rename_session, rename_branch]",
            "python \"$helper\" agent-task <target>",
            "Agent Task performs all repository analysis",
            "Never run another local repository command",
            "Never use Cloud Sandboxes",
        ):
            self.assertIn(text, instructions)
        self.assertNotIn("tools: [read, edit, search", instructions)
        self.assertNotIn("custom_agent:", instructions)

    def test_pins_shared_helper_and_policy_integrity(self):
        self.assertEqual(
            MODULE.REQUIRED_CLOUD_TASK_SHA256,
            "a3eb90898b86fcc528f7a3a403aac560b4df1852a56cc98a99b6c2d9fdb8d86c",
        )
        self.assertEqual(
            MODULE.AGENT_TASK_POLICY_SHA256,
            "7d48868140710139939cabc803a99f2122305e97dedbffa747e5f69903c16af1",
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

    def test_report_parser_accepts_markdown_with_one_json_payload(self):
        content = "# Result\n\nReadable summary.\n\n```json\n{\"ok\":true}\n```"
        self.assertEqual(
            {"ok": True},
            MODULE.parse_markdown_report(content, description="test report"),
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "exactly one"):
            MODULE.parse_markdown_report("# Result", description="test report")

    def test_artifacts_are_absolute_and_outside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            with self.assertRaisesRegex(MODULE.WorkflowError, "must be absolute"):
                MODULE.require_outside_repository(Path("result.json"), repo)
            with self.assertRaisesRegex(MODULE.WorkflowError, "outside the repository"):
                MODULE.require_outside_repository(repo / "result.json", repo)
            MODULE.require_outside_repository(repo.parent / "result.json", repo)

    def test_initial_and_resume_commands_use_the_pinned_cli_contract(self):
        helper = Path("C:/copilot/cloud_task.py")
        prompt = Path("C:/state/prompt.txt")
        first_result = Path("C:/state/result-1.json")
        prior_result = Path("C:/state/result-0.json")
        expected = [
            MODULE.sys.executable,
            str(helper),
            "--apply-with-report",
            "--allow-merged-pr",
            "--pr",
            METADATA["pr_url"],
            "--model",
            "sol",
            "--prompt-file",
            str(prompt),
            "--result-file",
            str(first_result),
            "--policy",
            "marketplace-agent-code-candidate-worker@1",
        ]
        self.assertEqual(
            MODULE.agent_task_command(
                helper,
                metadata=METADATA,
                model_alias="sol",
                prompt_path=prompt,
                result_path=first_result,
            ),
            expected,
        )
        resumed = MODULE.agent_task_command(
            helper,
            metadata=METADATA,
            model_alias="sol",
            prompt_path=prompt,
            result_path=first_result,
            prior_result_path=prior_result,
        )
        self.assertEqual(
            resumed,
            [*expected, "--input-result-file", str(prior_result)],
        )
        self.assertNotEqual(first_result, prior_result)

    def test_recovery_command_quotes_every_path_argument(self):
        with (
            mock.patch.object(
                MODULE.sys,
                "executable",
                "C:\\Program Files\\Python\\python.exe",
            ),
            mock.patch.object(
                MODULE.Path,
                "resolve",
                return_value=Path("C:\\Program Files\\Plugin\\audit.py"),
            ),
        ):
            command = MODULE.agent_task_recovery_command(
                target={"pr_url": METADATA["pr_url"]},
                repo_root=Path("C:\\work trees\\repo"),
                state_path=Path("C:\\Users\\A User\\state.json"),
            )
        for value in (
            "C:\\Program Files\\Python\\python.exe",
            "C:\\Program Files\\Plugin\\audit.py",
            "C:\\work trees\\repo",
            "C:\\Users\\A User\\state.json",
        ):
            self.assertIn(json.dumps(value), command)

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

    def test_missing_result_state_has_no_unusable_recovery_command(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(
                path,
                {
                    "version": MODULE.STATE_VERSION,
                    "agent_task": {
                        "status": "running",
                        "recovery_command": "retry",
                    },
                },
            )
            state = MODULE.record_missing_agent_task_result(
                path,
                message="helper returned no result",
            )
        self.assertEqual(state["agent_task"]["status"], "failed_without_result")
        self.assertNotIn("recovery_command", state["agent_task"])

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

    def test_result_binds_identity_artifacts_and_receipt_only_no_change(self):
        remote = MODULE.validate_success_result(
            result(),
            metadata=METADATA,
            requested_model="gpt-5.6-sol",
        )
        self.assertEqual(remote["commits"], [])
        self.assertEqual(remote["final_local_head"], METADATA["head_sha"])
        cases = []
        wrong_identity = result()
        wrong_identity["task"]["base_sha"] = "9" * 40
        cases.append(wrong_identity)
        wrong_artifact = result()
        wrong_artifact["report"]["path"] = "README.md"
        cases.append(wrong_artifact)
        failed_attestation = result()
        failed_attestation["attestation"]["structural_complete"] = False
        cases.append(failed_attestation)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(MODULE.WorkflowError):
                MODULE.validate_success_result(
                    value,
                    metadata=METADATA,
                    requested_model="gpt-5.6-sol",
                )

    def test_accepts_strict_legacy_v2_success_without_a_second_import(self):
        commit = "6" * 40
        legacy = result([commit])
        legacy["policy"] = MODULE.LEGACY_STRUCTURAL_AGENT_TASK_POLICY_V2
        legacy["application"] = {
            "status": "applied",
            "final_local_head": commit,
        }

        remote = MODULE.validate_success_result(
            legacy,
            metadata=METADATA,
            requested_model="gpt-5.6-sol",
        )

        self.assertFalse(remote["requires_apply"])
        self.assertEqual(remote["final_local_head"], commit)

    def test_report_binds_body_history_paths_and_pipeline(self):
        validated = MODULE.validate_audit_report(
            json.dumps(report()),
            request_id="request-1",
            metadata=METADATA,
            audit_branch="trask-pr-audit-7",
            commits=[],
            max_iterations=5,
            pipeline=PIPELINE,
        )
        self.assertEqual(validated["outcome"], "no_change")
        changed = report()
        changed["source_pull_request"]["body_sha256"] = "0" * 64
        with self.assertRaisesRegex(MODULE.WorkflowError, "wrong identity"):
            MODULE.validate_audit_report(
                json.dumps(changed),
                request_id="request-1",
                metadata=METADATA,
                audit_branch="trask-pr-audit-7",
                commits=[],
                max_iterations=5,
                pipeline=PIPELINE,
            )

    def test_report_requires_chained_complete_iteration_history(self):
        commits = ["6" * 40, "7" * 40]
        valid = report(commits)
        MODULE.validate_audit_report(
            json.dumps(valid),
            request_id="request-1",
            metadata=METADATA,
            audit_branch="trask-pr-audit-7",
            commits=commits,
            max_iterations=5,
            pipeline=PIPELINE,
        )
        cases = []
        wrong_chain = report(commits)
        wrong_chain["iterations"][1]["head_before"] = METADATA["head_sha"]
        cases.append(wrong_chain)
        duplicate = report(commits)
        duplicate["iterations"][0]["commit_shas"] = [commits[0], commits[0]]
        cases.append(duplicate)
        missing = report(commits)
        missing["iterations"][0]["commit_shas"] = [commits[0]]
        cases.append(missing)
        no_clean_pass = report(commits)
        no_clean_pass["iterations"] = no_clean_pass["iterations"][:1]
        cases.append(no_clean_pass)
        premature_cap = report(commits)
        premature_cap["outcome"] = "max_iterations_reached"
        premature_cap["pipeline"]["stage_outcome"] = None
        premature_cap["iterations"][-1]["outcome"] = "max_iterations_reached"
        premature_cap["iterations"][-1]["finding_count"] = 1
        cases.append(premature_cap)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(MODULE.WorkflowError):
                MODULE.validate_audit_report(
                    json.dumps(value),
                    request_id="request-1",
                    metadata=METADATA,
                    audit_branch="trask-pr-audit-7",
                    commits=commits,
                    max_iterations=5,
                    pipeline=PIPELINE,
                )

    def test_resume_rejects_replacement_task_and_changed_artifacts(self):
        prior = result()
        interrupted = interrupted_result()
        self.assertTrue(
            MODULE.validate_recovery_result_identity(
                interrupted,
                metadata=METADATA,
                requested_model="gpt-5.6-sol",
            )
        )
        replacement = result()
        replacement["task"]["id"] = "task-2"
        with self.assertRaisesRegex(MODULE.WorkflowError, "replacement"):
            MODULE.require_same_agent_task(prior, replacement)
        changed = result()
        changed["generated"]["branch"] = "copilot/other"
        with self.assertRaisesRegex(MODULE.WorkflowError, "generated branch"):
            MODULE.require_same_agent_task(prior, changed)

    def test_recovery_result_rejects_wrong_identity_and_credentials(self):
        no_task = interrupted_result()
        no_task["status"] = "error"
        no_task["task"] = {
            "id": None,
            "url": None,
            "state": None,
            "base_ref": None,
            "base_sha": None,
        }
        no_task["report"]["commit"] = None
        no_task["report"]["sha256"] = None
        no_task["error"] = {
            "code": "api_failure",
            "message": "user or repo does not have CCA enabled",
        }
        self.assertFalse(
            MODULE.validate_recovery_result_identity(
                no_task,
                metadata=METADATA,
                requested_model="gpt-5.6-sol",
            )
        )
        wrong = result()
        wrong["pull_request"]["head_sha"] = "9" * 40
        with self.assertRaisesRegex(MODULE.WorkflowError, "wrong identity"):
            MODULE.validate_recovery_result_identity(
                wrong,
                metadata=METADATA,
                requested_model="gpt-5.6-sol",
            )
        credential = result()
        credential["task"]["url"] = "https://user:password@example.com/task"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps(credential), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.WorkflowError, "credentials"):
                MODULE.load_agent_task_result(path)

    def test_rejects_incomplete_structural_attestation(self):
        value = result()
        value["attestation"]["structural_complete"] = False
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.validate_success_result(
                value,
                metadata=METADATA,
                requested_model="gpt-5.6-sol",
            )

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

    @unittest.skip("hosted task resume is intentionally unavailable")
    def test_command_persists_before_prepare_and_resumes_same_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo_root = root / "repo"
            repo_root.mkdir()
            state_path = root / "state.json"
            helper = root / "cloud_task.py"
            helper.write_text("helper", encoding="utf-8")
            args = SimpleNamespace(
                target=METADATA["pr_url"],
                repo_root=str(repo_root),
                state=str(state_path),
                max_iterations=5,
                pipeline_run=PIPELINE["run"],
                pipeline_iteration=PIPELINE["iteration"],
                pipeline_max_iterations=PIPELINE["max_iterations"],
                model="sol",
                recover=False,
            )
            command_calls = []
            events = []
            first = interrupted_result()
            success = result()
            report_content = json.dumps(report())
            success["report"]["sha256"] = MODULE.sha256_text(report_content)

            def prepare(*_args, **_kwargs):
                self.assertEqual(events, ["discover"])
                self.assertTrue(state_path.is_file())
                return {
                    "branch": "trask-pr-audit-7",
                    "branch_action": "realigned",
                    "local_head": METADATA["head_sha"],
                    "reference": "origin/main",
                }

            responses = [
                (130, first),
                (0, success),
            ]

            def execute(
                _helper,
                *,
                result_path,
                prior_result_path=None,
                **_kwargs,
            ):
                code, payload = responses.pop(0)
                command_calls.append((result_path, prior_result_path))
                result_path.write_text(json.dumps(payload), encoding="utf-8")
                return MODULE.subprocess.CompletedProcess(["python"], code, "", "")

            def committed_text(_repository, _path, _commit, *, description):
                return (
                    report_content
                    if description == "audit report"
                    else json.dumps(receipt())
                )

            patches = (
                mock.patch.object(MODULE, "require_tools"),
                mock.patch.object(MODULE, "resolve_repo_root", return_value=repo_root),
                mock.patch.object(
                    MODULE,
                    "discover_cloud_task",
                    side_effect=lambda: events.append("discover") or helper,
                ),
                mock.patch.object(
                    MODULE, "merged_metadata_for", return_value=dict(METADATA)
                ),
                mock.patch.object(MODULE, "require_clean_worktree"),
                mock.patch.object(MODULE, "prepare_audit_branch", side_effect=prepare),
                mock.patch.object(
                    MODULE,
                    "local_identity",
                    return_value={
                        "branch": "trask-pr-audit-7",
                        "head": METADATA["head_sha"],
                        "status": "",
                    },
                ),
                mock.patch.object(
                    MODULE, "execute_managed_agent_task", side_effect=execute
                ),
                mock.patch.object(
                    MODULE, "fetch_committed_text", side_effect=committed_text
                ),
                mock.patch.object(
                    MODULE,
                    "validate_imported_commits",
                    side_effect=[
                        MODULE.WorkflowError("import verification failed"),
                        None,
                    ],
                ),
                mock.patch.object(
                    MODULE,
                    "finish_agent_task",
                    return_value={"result": "nothing_to_publish"},
                ),
                mock.patch.object(MODULE, "emit"),
            )
            with contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                with self.assertRaisesRegex(
                    MODULE.WorkflowError, "did not complete"
                ):
                    MODULE.command_agent_task(args)
                failed = MODULE.load_state(state_path)
                self.assertEqual(failed["agent_task"]["status"], "failed")
                self.assertEqual(failed["agent_task"]["task"]["id"], "task-1")
                args.recover = True
                with self.assertRaisesRegex(
                    MODULE.WorkflowError, "import verification failed"
                ):
                    MODULE.command_agent_task(args)
                failed = MODULE.load_state(state_path)
                self.assertEqual(failed["agent_task"]["status"], "failed")
                self.assertEqual(failed["agent_task"]["task"]["id"], "task-1")
                MODULE.command_agent_task(args)

            self.assertEqual(len(command_calls), 2)
            first_path, first_prior = command_calls[0]
            second_path, second_prior = command_calls[1]
            self.assertIsNone(first_prior)
            self.assertEqual(second_prior, first_path)
            self.assertNotEqual(second_path, first_path)

    def test_publication_failure_keeps_artifacts_for_idempotent_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            prompt = root / "prompt.txt"
            result_path = root / "result.json"
            prompt.write_text("prompt", encoding="utf-8")
            result_path.write_text("result", encoding="utf-8")
            state = {
                "version": MODULE.STATE_VERSION,
                "pr": METADATA,
                "agent_task": {
                    "status": "validated",
                    "prompt_file": str(prompt),
                    "result_file": str(result_path),
                    "result_files": [str(result_path)],
                },
            }
            MODULE.save_state(state_path, state)
            with mock.patch.object(
                MODULE,
                "publish_agent_task_result",
                side_effect=MODULE.WorkflowError("push failed"),
            ):
                with self.assertRaisesRegex(MODULE.WorkflowError, "push failed"):
                    MODULE.finish_agent_task(
                        repo_root=root,
                        state_path=state_path,
                        state=state,
                        remote={},
                    )
            failed = MODULE.load_state(state_path)
            self.assertEqual(failed["agent_task"]["status"], "publication_failed")
            self.assertTrue(prompt.exists())
            self.assertTrue(result_path.exists())
            with mock.patch.object(
                MODULE,
                "publish_agent_task_result",
                return_value={"result": "published"},
            ):
                envelope = MODULE.finish_agent_task(
                    repo_root=root,
                    state_path=state_path,
                    state=failed,
                    remote={},
                )
            self.assertEqual(envelope["result"], "published")
            self.assertFalse(prompt.exists())
            self.assertFalse(result_path.exists())

    def test_import_rejects_merge_and_unexpected_paths(self):
        commit = "6" * 40

        def run_case(parents, paths):
            def fake_git(_repo, *arguments):
                if arguments[:3] == ("rev-list", "--reverse", "--topo-order"):
                    return commit
                if arguments[:3] == ("rev-list", "--parents", "-n"):
                    return parents
                if arguments[0] == "diff-tree":
                    return paths
                raise AssertionError(arguments)

            with mock.patch.object(MODULE, "git", side_effect=fake_git):
                MODULE.validate_imported_commits(
                    Path("."),
                    base_sha=METADATA["head_sha"],
                    commits=[commit],
                    report={
                        "commits": [
                            {
                                "sha": commit,
                                "summary": "Fix finding",
                                "paths": ["app.py"],
                            }
                        ]
                    },
                )

        run_case(f"{commit} {METADATA['head_sha']}", "app.py")
        invalid = (
            (f"{commit} {METADATA['head_sha']} {'7' * 40}", "app.py"),
            (f"{commit} {METADATA['head_sha']}", "other.py"),
        )
        for parents, paths in invalid:
            with self.subTest(parents=parents, paths=paths):
                with self.assertRaises(MODULE.WorkflowError):
                    run_case(parents, paths)


if __name__ == "__main__":
    unittest.main()
