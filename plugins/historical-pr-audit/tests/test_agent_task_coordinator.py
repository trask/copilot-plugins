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
        "status": "passed",
        "detail": "4 passed",
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
            "id": "marketplace-agent-worker",
            "version": 2,
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
            "status": "applied" if commits else "no_changes",
            "final_local_head": commits[-1] if commits else METADATA["head_sha"],
        },
        "report": {
            "path": ".github/agent-task-reports/request-1.md",
            "commit": generated_head,
            "sha256": "5" * 64,
        },
        "worker_receipt": {
            "path": ".github/agent-task-validations/request-1.json",
            "commit": generated_head,
            "sha256": MODULE.sha256_text(json.dumps(receipt())),
        },
        "validation": {
            "complete": True,
            "outcomes": [dict(item) for item in VALIDATION],
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
    value["report"] = None
    value["worker_receipt"]["commit"] = None
    value["validation"] = {"complete": False, "outcomes": []}
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
        "validation": VALIDATION,
        "pipeline": {
            **PIPELINE,
            "stage_outcome": (
                None if outcome == "max_iterations_reached" else "cleared"
            ),
        },
    }


class AgentTaskCoordinatorTest(unittest.TestCase):
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
            "66a76fa96d8eafd8b256ae5477777aab0a190d4b99a05cb90c56e03fc8dc8565",
        )
        self.assertEqual(
            MODULE.AGENT_TASK_POLICY_SHA256,
            "33bb702b099ee1c7dd933f81396c3081279a781c9c8e04e7d4a0dee9317d5714",
        )

    def test_prompt_is_versioned_untrusted_and_remote_only(self):
        prompt = MODULE.build_worker_prompt(
            METADATA,
            audit_branch="trask-pr-audit-7",
            max_iterations=5,
            pipeline=PIPELINE,
        )
        for text in (
            "worker prompt version 1",
            "untrusted data",
            "Never request, read, print, persist, or transmit credentials",
            "Do not select a custom_agent",
            "Do not use Cloud Sandboxes",
            "Perform every substantive action in this Agent Task",
            "Run at most 5 audit iterations",
            "single-parent fix commit",
            "max_iterations_reached",
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
            "marketplace-agent-worker@2",
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
        with mock.patch.object(MODULE, "run", return_value=completed) as run:
            actual = MODULE.execute_managed_agent_task(
                Path("C:/copilot/cloud_task.py"),
                repo_root=Path("C:/repo"),
                metadata=METADATA,
                model_alias="sol",
                prompt_path=Path("C:/state/prompt.txt"),
                result_path=Path("C:/state/result.json"),
            )
        self.assertIs(actual, completed)
        self.assertEqual(run.call_args.kwargs, {"cwd": Path("C:/repo"), "check": False})
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
        failed_validation = result()
        failed_validation["validation"]["outcomes"][0]["status"] = "failed"
        cases.append(failed_validation)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(MODULE.WorkflowError):
                MODULE.validate_success_result(
                    value,
                    metadata=METADATA,
                    requested_model="gpt-5.6-sol",
                )

    def test_report_binds_body_history_paths_and_pipeline(self):
        validated = MODULE.validate_audit_report(
            json.dumps(report()),
            request_id="request-1",
            metadata=METADATA,
            audit_branch="trask-pr-audit-7",
            commits=[],
            validation=VALIDATION,
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
                validation=VALIDATION,
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
            validation=VALIDATION,
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
                    validation=VALIDATION,
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
        no_task["task"]["id"] = None
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

    def test_receipt_rejects_wrong_identity_incomplete_validation_and_credentials(self):
        cases = []
        wrong = receipt()
        wrong[0]["unexpected"] = "identity"
        cases.append(wrong)
        incomplete = receipt()
        incomplete.clear()
        cases.append(incomplete)
        failed = receipt()
        failed[0]["status"] = "failed"
        cases.append(failed)
        credential = receipt()
        credential[0]["detail"] = "token=github_pat_example_value_123456"
        cases.append(credential)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(MODULE.WorkflowError):
                MODULE.validate_worker_receipt(
                    json.dumps(value),
                    request_id="request-1",
                    metadata=METADATA,
                    validation=VALIDATION,
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

            self.assertEqual(len(command_calls), 3)
            first_path, first_prior = command_calls[0]
            second_path, second_prior = command_calls[1]
            third_path, third_prior = command_calls[2]
            self.assertIsNone(first_prior)
            self.assertEqual(second_prior, first_path)
            self.assertNotEqual(second_path, first_path)
            self.assertEqual(third_prior, second_path)
            self.assertNotEqual(third_path, second_path)

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

    def test_import_rejects_merge_unexpected_paths_and_bad_body(self):
        commit = "6" * 40
        valid_body = "\n".join(
            f"{field}: value" for field in MODULE.FIX_COMMIT_FIELDS
        )

        def run_case(parents, paths, body, identity=None):
            def fake_git(_repo, *arguments):
                if arguments[:3] == ("rev-list", "--reverse", "--topo-order"):
                    return commit
                if arguments[:3] == ("rev-list", "--parents", "-n"):
                    return parents
                if arguments[0] == "diff-tree":
                    return paths
                if arguments[:3] == ("show", "-s", "--format=%B"):
                    return body
                raise AssertionError(arguments)

            with (
                mock.patch.object(
                    MODULE,
                    "local_identity",
                    return_value=identity
                    or {
                        "branch": "trask-pr-audit-7",
                        "head": commit,
                        "status": "",
                    },
                ),
                mock.patch.object(MODULE, "git", side_effect=fake_git),
            ):
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

        run_case(f"{commit} {METADATA['head_sha']}", "app.py", valid_body)
        invalid = (
            (
                f"{commit} {METADATA['head_sha']} {'7' * 40}",
                "app.py",
                valid_body,
                None,
            ),
            (f"{commit} {METADATA['head_sha']}", "other.py", valid_body, None),
            (f"{commit} {METADATA['head_sha']}", "app.py", "Fix app", None),
            (
                f"{commit} {METADATA['head_sha']}",
                "app.py",
                valid_body,
                {
                    "branch": "trask-pr-audit-7",
                    "head": commit,
                    "status": " M app.py",
                },
            ),
            (
                f"{commit} {METADATA['head_sha']}",
                "app.py",
                valid_body,
                {
                    "branch": "trask-pr-audit-7",
                    "head": "8" * 40,
                    "status": "",
                },
            ),
        )
        for parents, paths, body, identity in invalid:
            with self.subTest(parents=parents, paths=paths):
                with self.assertRaises(MODULE.WorkflowError):
                    run_case(parents, paths, body, identity)


if __name__ == "__main__":
    unittest.main()
