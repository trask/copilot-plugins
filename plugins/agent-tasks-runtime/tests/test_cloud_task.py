import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "agent-tasks-runtime"
    / "scripts"
    / "cloud_task.py"
)
SPEC = importlib.util.spec_from_file_location("agent_tasks_cloud_task", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PolicyPromptTest(unittest.TestCase):
    def test_requests_only_minimal_remote_validation_artifact(self):
        validation_path = ".github/agent-task-validations/request-1.json"
        prompt = MODULE.build_policy_prompt(
            "Review the pull request.",
            request_id="request-1",
            receipt=validation_path,
            mode="apply_with_report",
            repository="owner/repo",
            pull_request=SimpleNamespace(head_sha="1" * 40),
        )

        self.assertIn("Policy: marketplace-agent-worker@5", prompt)
        self.assertIn(f"write `{validation_path}` as a nonempty JSON array", prompt)
        self.assertIn("exactly `command` and `outcome`", prompt)
        self.assertIn('"outcome":"passed"', prompt)
        self.assertIn("`outcome` must be `passed`", prompt)
        self.assertIn("Do not write request, policy, repository", prompt)
        self.assertIn(
            json.dumps(
                [
                    ".github/agent-task-reports/request-1.md",
                    validation_path,
                ]
            ),
            prompt,
        )
        self.assertNotIn("receipt JSON template", prompt)
        self.assertNotIn("validation_complete", prompt)
        self.assertNotIn("python3 -c", prompt)
        self.assertNotIn("pull_request_head_sha", prompt)
        self.assertIn("`Finding: <identifier>`", prompt)
        self.assertIn("nonempty UTF-8 Markdown for humans", prompt)
        self.assertIn("dispatcher derives and records generated fix commit order", prompt)
        self.assertIn("Do not create, stage, or commit alternate", prompt)
        self.assertNotIn("top-level `fix_commits` array", prompt)

    def test_renders_dynamic_artifact_paths_into_workflow_prompt(self):
        report_path = ".github/agent-task-reports/request-1.md"
        validation_path = ".github/agent-task-validations/request-1.json"
        options = MODULE.Options(
            report=True,
            model="gpt-5.6-sol",
            prompt=(
                "Write the candidate report directly to "
                f"`{MODULE.REPORT_PATH_PLACEHOLDER}` and validation directly to "
                f"`{MODULE.VALIDATION_PATH_PLACEHOLDER}`. Do not use alternate names."
            ),
            policy=MODULE.MARKETPLACE_POLICY_SELECTOR,
        )

        payload = MODULE.task_payload(
            options,
            report_path=report_path,
            request_id="request-1",
            worker_receipt=validation_path,
            repository="owner/repo",
        )
        prompt = payload["prompt"]

        self.assertIn(f"candidate report directly to `{report_path}`", prompt)
        self.assertIn(f"validation directly to `{validation_path}`", prompt)
        self.assertNotIn(MODULE.REPORT_PATH_PLACEHOLDER, prompt)
        self.assertNotIn(MODULE.VALIDATION_PATH_PLACEHOLDER, prompt)
        self.assertNotIn("candidate-report.json", prompt)
        self.assertNotIn("worker-validation.json", prompt)
        self.assertIn(
            json.dumps([report_path, validation_path]),
            prompt,
        )

    def test_older_policies_are_explicitly_rejected(self):
        result_path = str((Path.cwd().parent / "result.json").resolve())
        for version in (1, 2, 3):
            with self.subTest(version=version):
                with self.assertRaisesRegex(
                    MODULE.CloudError,
                    f"unknown policy 'marketplace-agent-worker@{version}'; expected "
                    "one of marketplace-agent-apply-report-worker@1, "
                    "marketplace-agent-report-worker@1, "
                    "marketplace-agent-worker@5",
                ) as raised:
                    MODULE.parse_args(
                        [
                            "--apply-with-report",
                            "--pr",
                            "owner/repo#1",
                            "--result-file",
                            result_path,
                            "--policy",
                            f"marketplace-agent-worker@{version}",
                            "Review.",
                        ]
                    )

                self.assertEqual(raised.exception.code, "policy_unknown")

    def test_report_policy_requests_one_markdown_artifact(self):
        report_path = ".github/agent-task-reports/request-1.md"
        options = MODULE.Options(
            report=True,
            model="gpt-5.6-sol",
            prompt=f"Write `{MODULE.REPORT_PATH_PLACEHOLDER}`.",
            policy=MODULE.MARKETPLACE_REPORT_POLICY_SELECTOR,
        )

        payload = MODULE.task_payload(
            options,
            report_path=report_path,
            request_id="request-1",
            worker_receipt=None,
            repository="owner/repo",
        )
        prompt = payload["prompt"]

        self.assertIn("Policy: marketplace-agent-report-worker@1", prompt)
        self.assertIn(f"only changed path must be `{report_path}`", prompt)
        self.assertIn("records structural completion only", prompt)
        self.assertNotIn("validation artifact", prompt)
        self.assertNotIn(MODULE.REPORT_PATH_PLACEHOLDER, prompt)

    def test_report_policy_rejects_apply_recovery_and_receipt_options(self):
        result_path = str((Path.cwd().parent / "result.json").resolve())
        cases = [
            ["--apply-with-report"],
            ["--report", "--worker-receipt", "receipt.json"],
            ["--report", "--task-id", "task-1"],
        ]
        for mode in cases:
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(
                    MODULE.CloudError,
                    "marketplace-agent-report-worker@1 requires",
                ):
                    MODULE.parse_args(
                        [
                            *mode,
                            "--pr",
                            "owner/repo#1",
                            "--result-file",
                            result_path,
                            "--policy",
                            MODULE.MARKETPLACE_REPORT_POLICY_SELECTOR,
                            "Review.",
                        ]
                    )

    def test_apply_report_policy_uses_one_structural_report_artifact(self):
        report_path = ".github/agent-task-reports/request-1.md"
        options = MODULE.Options(
            report=False,
            model="gpt-5.6-sol",
            prompt=f"Write `{MODULE.REPORT_PATH_PLACEHOLDER}`.",
            pull_request=MODULE.PrReference(1, "owner/repo", "owner/repo#1"),
            apply_with_report=True,
            result_file=(Path.cwd().parent / "result.json").resolve(),
            policy=MODULE.MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR,
            prompt_file=(Path.cwd().parent / "prompt.txt").resolve(),
        )

        payload = MODULE.task_payload(
            options,
            report_path=report_path,
            pull_request=MODULE.PullRequestSnapshot(
                1,
                "https://github.com/owner/repo/pull/1",
                "OPEN",
                "owner/repo",
                "main",
                "2" * 40,
                "owner/repo",
                "feature",
                "1" * 40,
                False,
            ),
            request_id="request-1",
            worker_receipt=None,
            repository="owner/repo",
        )
        prompt = payload["prompt"]

        self.assertIn(
            "Policy: marketplace-agent-apply-report-worker@1",
            prompt,
        )
        self.assertIn("zero or more linear commits", prompt)
        self.assertIn("exactly one final single-parent report commit", prompt)
        self.assertIn(f"only changed path must be `{report_path}`", prompt)
        self.assertIn("untrusted inert evidence", prompt)
        self.assertNotIn("agent-task-validations", prompt)
        self.assertNotIn(MODULE.VALIDATION_PATH_PLACEHOLDER, prompt)

    def test_structural_recovery_requires_request_id_and_rejects_receipt(self):
        result_path = str((Path.cwd().parent / "result.json").resolve())
        base = [
            "--resume-apply-with-report",
            "--pr",
            "owner/repo#1",
            "--task-id",
            "task-1",
            "--result-file",
            result_path,
            "--policy",
            MODULE.MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR,
        ]
        with self.assertRaisesRegex(MODULE.CloudError, "--request-id"):
            MODULE.parse_args(base)
        with self.assertRaisesRegex(MODULE.CloudError, "--worker-receipt"):
            MODULE.parse_args(
                [
                    *base,
                    "--request-id",
                    "request-1",
                    "--worker-receipt",
                    ".github/agent-task-validations/request-1.json",
                ]
            )
        options = MODULE.parse_args([*base, "--request-id", "request-1"])
        self.assertEqual(options.request_id, "request-1")
        self.assertIsNone(options.worker_receipt)


class InterruptedApplyRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.task_id = "task-1"
        self.request_id = "request-1"
        self.repository = "owner/repo"
        self.model = "gpt-5.6-sol"
        self.report_path = (
            f".github/agent-task-reports/{self.request_id}.md"
        )
        self.validation_path = (
            f".github/agent-task-validations/{self.request_id}.json"
        )
        self.pull_request = MODULE.PullRequestSnapshot(
            7,
            "https://github.com/owner/repo/pull/7",
            "OPEN",
            "owner/repo",
            "main",
            "4" * 40,
            "fork/repo",
            "feature",
            "1" * 40,
            True,
        )
        self.prompt = MODULE.build_policy_prompt(
            MODULE.build_pr_prompt(
                MODULE.build_apply_with_report_prompt(
                    "Review the pull request.",
                    self.report_path,
                    self.validation_path,
                ),
                self.pull_request,
            ),
            request_id=self.request_id,
            receipt=self.validation_path,
            mode="apply_with_report",
            repository=self.repository,
            pull_request=self.pull_request,
        )
        self.task = {
            "id": self.task_id,
            "state": "completed",
            "created_at": "2026-09-15T00:00:00Z",
            "repository": {"id": 1},
            "owner": {"id": 2},
            "sessions": [
                {
                    "id": "session-1",
                    "task_id": self.task_id,
                    "state": "completed",
                    "created_at": "2026-09-15T00:00:00Z",
                    "model": f"sweagent-capi:{self.model}",
                    "base_ref": self.pull_request.head_sha,
                    "repository": {"id": 1},
                    "owner": {"id": 2},
                    "prompt": self.prompt,
                }
            ],
        }

    def validate(self, task=None, **overrides):
        arguments = {
            "task_id": self.task_id,
            "repository": self.repository,
            "model": self.model,
            "pull_request": self.pull_request,
            "request_id": self.request_id,
            "report_path": self.report_path,
            "worker_receipt": self.validation_path,
        }
        arguments.update(overrides)
        MODULE.validate_interrupted_apply_task(
            self.task if task is None else task,
            **arguments,
        )

    def mutated_task(self, **session_updates):
        task = json.loads(json.dumps(self.task))
        task["sessions"][0].update(session_updates)
        return task

    def test_accepts_exact_live_apply_with_report_identity(self):
        self.validate()

    def test_accepts_exact_structural_apply_report_identity(self):
        prompt = MODULE.build_apply_report_policy_prompt(
            MODULE.build_pr_prompt(
                MODULE.build_apply_with_report_prompt(
                    "Review the pull request.",
                    self.report_path,
                    None,
                ),
                self.pull_request,
            ),
            report_path=self.report_path,
        )
        task = self.mutated_task(prompt=prompt)

        self.validate(
            task,
            worker_receipt=None,
            policy=MODULE.MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR,
        )

    def test_parse_requires_complete_recovery_identity(self):
        result_path = str((Path.cwd().parent / "result.json").resolve())
        options = MODULE.parse_args(
            [
                "--resume-apply-with-report",
                "--model",
                "sol",
                "--pr",
                "owner/repo#7",
                "--task-id",
                self.task_id,
                "--worker-receipt",
                self.validation_path,
                "--result-file",
                result_path,
                "--policy",
                MODULE.MARKETPLACE_POLICY_SELECTOR,
            ]
        )

        self.assertTrue(options.apply_with_report)
        self.assertTrue(options.resume_apply_with_report)
        self.assertFalse(options.monitor_only)
        self.assertEqual(options.prompt, "")
        self.assertEqual(MODULE.mode_name(options), "apply_with_report")

    def test_parse_preserves_current_monitor_mode(self):
        result_path = str((Path.cwd().parent / "result.json").resolve())
        options = MODULE.parse_args(
            [
                "--monitor-only",
                "--model",
                "sol",
                "--pr",
                "owner/repo#7",
                "--task-id",
                self.task_id,
                "--worker-receipt",
                self.validation_path,
                "--result-file",
                result_path,
                "--policy",
                MODULE.MARKETPLACE_POLICY_SELECTOR,
            ]
        )

        self.assertTrue(options.monitor_only)
        self.assertFalse(options.apply_with_report)
        self.assertFalse(options.resume_apply_with_report)
        self.assertEqual(MODULE.mode_name(options), "monitor_only")

    def test_rejects_mode_path_and_policy_prompt_mismatches(self):
        cases = {
            "mode": self.prompt.replace(
                MODULE.APPLY_WITH_REPORT_MARKER,
                MODULE.REPORT_MARKER,
            ),
            "path": self.prompt.replace(self.request_id, "other-request"),
            "policy": self.prompt.replace(
                MODULE.MARKETPLACE_POLICY_HASH,
                "0" * 64,
            ),
        }
        for name, prompt in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(MODULE.CloudError) as raised:
                    self.validate(self.mutated_task(prompt=prompt))
                self.assertEqual(
                    raised.exception.code,
                    "task_identity_mismatch",
                )

    def test_rejects_source_model_and_task_mismatches(self):
        task_mismatch = self.mutated_task(task_id="other-task")
        cases = {
            "source": (
                self.task,
                {"pull_request": MODULE.PullRequestSnapshot(
                    **{
                        **self.pull_request.__dict__,
                        "head_sha": "9" * 40,
                    }
                )},
            ),
            "model": (
                self.mutated_task(model="sweagent-capi:gpt-6-astra"),
                {},
            ),
            "task": (task_mismatch, {}),
        }
        for name, (task, overrides) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(MODULE.CloudError) as raised:
                    self.validate(task, **overrides)
                self.assertEqual(
                    raised.exception.code,
                    "task_identity_mismatch",
                )

    def test_rejects_missing_malformed_or_ambiguous_prompt(self):
        cases = [
            self.mutated_task(prompt=None),
            self.mutated_task(prompt="not a managed prompt"),
            self.mutated_task(
                prompt=self.prompt + "\n" + MODULE.POLICY_MARKER
            ),
        ]
        for task in cases:
            with self.subTest(prompt=task["sessions"][0]["prompt"]):
                with self.assertRaises(MODULE.CloudError):
                    self.validate(task)


class ValidationArtifactTest(unittest.TestCase):
    def fetch(self, payload):
        content = payload if isinstance(payload, str) else json.dumps(payload) + "\n"
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        api = mock.Mock()
        api.request_json.return_value = {
            "type": "file",
            "encoding": "base64",
            "content": encoded,
        }
        return MODULE.fetch_worker_receipt(
            api,
            "owner/repo",
            ".github/agent-task-validations/request-1.json",
            "copilot/task-1",
        )

    def test_accepts_strict_nonempty_passed_array_without_executing_commands(self):
        sentinel = Path.cwd() / "worker-command-was-executed"
        sentinel.unlink(missing_ok=True)
        command = (
            "python -c \"from pathlib import Path; "
            f"Path(r'{sentinel}').write_text('unsafe')\""
        )

        with mock.patch.object(MODULE.subprocess, "run") as run:
            outcomes, digest = self.fetch(
                [
                    {
                        "command": command,
                        "outcome": "passed",
                    }
                ]
            )

        self.assertEqual(outcomes[0]["command"], command)
        self.assertEqual(
            digest,
            hashlib.sha256(
                (
                    json.dumps(
                        [
                            {
                                "command": command,
                                "outcome": "passed",
                            }
                        ]
                    )
                    + "\n"
                ).encode("utf-8")
            ).hexdigest(),
        )
        run.assert_not_called()
        self.assertFalse(sentinel.exists())

    def test_exact_v4_and_v5_receipt_shapes_remain_distinct(self):
        fixtures = Path(__file__).parent / "fixtures"
        v4_drift = json.loads(
            (fixtures / "shared-workflows-377-v4-validation.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(v4_drift)
        self.assertTrue(all(set(entry) == {"command", "outcome"} for entry in v4_drift))
        self.assertTrue(all("status" not in entry for entry in v4_drift))
        with self.assertRaisesRegex(MODULE.CloudError, "malformed"):
            self.fetch(
                (
                    fixtures / "shared-workflows-377-v5-validation.json"
                ).read_text(encoding="utf-8")
            )

    def test_accepts_production_command_outcome_receipt(self):
        outcomes, _ = self.fetch(
            [
                {
                    "command": "python3 -m unittest discover -p 'test_*.py'",
                    "outcome": "passed",
                },
                {
                    "command": "git worktree orphan initialization probe",
                    "outcome": "passed",
                },
                {
                    "command": "CodeQL (actions, python)",
                    "outcome": "passed",
                },
            ]
        )

        self.assertEqual(
            [entry["command"] for entry in outcomes],
            [
                "python3 -m unittest discover -p 'test_*.py'",
                "git worktree orphan initialization probe",
                "CodeQL (actions, python)",
            ],
        )

    def test_fails_closed_for_missing_malformed_or_empty_artifact(self):
        cases = [
            ("not json", "malformed marketplace worker validation"),
            ({}, "marketplace worker validation is incomplete"),
            ([], "marketplace worker validation is incomplete"),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(MODULE.CloudError, expected):
                    self.fetch(payload)

    def test_fails_closed_for_failed_skipped_extra_and_legacy_outcomes(self):
        cases = [
            {"command": "test", "outcome": "failed"},
            {"command": "test", "outcome": "skipped"},
            {
                "command": "test",
                "outcome": "passed",
                "extra": True,
            },
            {"command": "test", "result": "passed"},
        ]
        for outcome in cases:
            with self.subTest(outcome=outcome):
                with self.assertRaises(MODULE.CloudError) as raised:
                    self.fetch([outcome])
                self.assertEqual(raised.exception.code, "validation_incomplete")


class MarkdownReportTest(unittest.TestCase):
    def test_invalid_utf8_report_fails_closed(self):
        api = mock.Mock()
        api.request_json.return_value = {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(b"\xff").decode("ascii"),
        }

        with self.assertRaisesRegex(MODULE.CloudError, "invalid report file"):
            MODULE.fetch_report(
                api,
                "owner/repo",
                ".github/agent-task-reports/request-1.md",
                "copilot/task-1",
            )


class WorkerHistoryTest(unittest.TestCase):
    def repository(self, responses):
        def runner(command, **kwargs):
            key = tuple(command[1:3])
            output = responses.get(key, "")
            if isinstance(output, dict):
                output = output.get(command[-1], "")
            return subprocess.CompletedProcess(command, 0, output, "")

        return MODULE.GitRepository(runner, Path.exists)

    def test_accepts_linear_history_and_exact_artifact_paths(self):
        base = "1" * 40
        fix = "2" * 40
        artifact = "3" * 40
        repository = self.repository(
            {
                ("rev-list", "--parents"): {
                    artifact: f"{artifact} {fix}\n",
                    fix: f"{fix} {base}\n",
                },
                ("diff-tree", "--no-commit-id"): (
                    ".github/agent-task-reports/request-1.md\0"
                    ".github/agent-task-validations/request-1.json\0"
                ),
            }
        )

        history = repository.worker_history(
            Path("C:/repo"),
            base,
            [fix, artifact],
            [
                ".github/agent-task-validations/request-1.json",
                ".github/agent-task-reports/request-1.md",
            ],
        )

        self.assertEqual(history.code_commits, (fix,))
        self.assertEqual(history.receipt_commit, artifact)

    def test_accepts_report_only_commit_directly_on_source_head(self):
        base = "1" * 40
        artifact = "3" * 40
        report_path = ".github/agent-task-reports/request-1.md"
        repository = self.repository(
            {
                ("rev-list", "--parents"): {
                    artifact: f"{artifact} {base}\n",
                },
                ("diff-tree", "--no-commit-id"): f"{report_path}\0",
            }
        )

        history = repository.worker_history(
            Path("C:/repo"),
            base,
            [artifact],
            [report_path],
        )

        self.assertEqual(history.code_commits, ())
        self.assertEqual(history.receipt_commit, artifact)

    def test_rejects_non_linear_history_and_unexpected_paths(self):
        base = "1" * 40
        fix = "2" * 40
        artifact = "3" * 40
        merge_parent = "4" * 40
        non_linear = self.repository(
            {
                ("rev-list", "--parents"): {
                    artifact: f"{artifact} {fix} {merge_parent}\n",
                },
            }
        )
        with self.assertRaisesRegex(MODULE.CloudError, "single-parent"):
            non_linear.worker_history(
                Path("C:/repo"),
                base,
                [fix, artifact],
                [".github/agent-task-validations/request-1.json"],
            )

        wrong_path = self.repository(
            {
                ("rev-list", "--parents"): {
                    artifact: f"{artifact} {fix}\n",
                },
                ("diff-tree", "--no-commit-id"): (
                    "candidate-report.json\0worker-validation.json\0"
                ),
            }
        )
        with self.assertRaisesRegex(MODULE.CloudError, "unexpected paths"):
            wrong_path.worker_history(
                Path("C:/repo"),
                base,
                [fix, artifact],
                [".github/agent-task-validations/request-1.json"],
            )

    def test_accepts_one_unique_correlation_per_commit_without_parsing_report(self):
        fix = "2" * 40
        repository = self.repository(
            {
                ("show", "-s"): "Fix inconsistent instrumentation\n\nFinding: finding-7\n",
            }
        )

        repository.require_correlated_fix_commits(
            Path("C:/repo"),
            [fix],
        )

    def test_rejects_missing_commit_correlation(self):
        fix = "2" * 40
        repository = self.repository({("show", "-s"): "Fix inconsistency\n"})

        with self.assertRaisesRegex(MODULE.CloudError, "Finding: correlation") as raised:
            repository.require_correlated_fix_commits(
                Path("C:/repo"),
                [fix],
            )

        self.assertEqual(raised.exception.code, "malformed_history")

    def test_rejects_duplicate_or_multiple_commit_correlations(self):
        first = "2" * 40
        second = "3" * 40
        cases = [
            "Fix inconsistency\n\nFinding: finding-7\n",
            (
                "Fix inconsistency\n\nFinding: finding-7\n"
                "Finding: finding-8\n"
            ),
        ]

        for second_message in cases:
            repository = self.repository(
                {
                    ("show", "-s"): {
                        first: "Fix first\n\nFinding: finding-7\n",
                        second: second_message,
                    }
                }
            )
            with self.subTest(second_message=second_message):
                with self.assertRaises(MODULE.CloudError) as raised:
                    repository.require_correlated_fix_commits(
                        Path("C:/repo"),
                        [first, second],
                    )
                self.assertEqual(raised.exception.code, "malformed_history")


class DispatcherFinalizationTest(unittest.TestCase):
    def setUp(self):
        self.root = Path("C:/repo")
        self.base_sha = "1" * 40
        self.code_commit = "2" * 40
        self.artifact_commit = "3" * 40
        self.report = (
            "# Consistency review\n\n"
            "The generated fix resolves finding-7 and all remote validation passed.\n"
        )
        self.snapshot = MODULE.WorktreeSnapshot(
            self.root,
            "owner/repo",
            "origin",
            "feature",
            self.base_sha,
        )
        self.pull_request = MODULE.PullRequestSnapshot(
            7,
            "https://github.com/owner/repo/pull/7",
            "OPEN",
            "owner/repo",
            "main",
            "4" * 40,
            "owner/repo",
            "feature",
            self.base_sha,
            False,
        )
        self.task = {
            "id": "task-1",
            "state": "completed",
            "html_url": "https://github.com/owner/repo/tasks/task-1",
            "artifacts": [
                {
                    "type": "branch",
                    "provider": "github",
                    "data": {
                        "head_ref": "copilot/task-1",
                        "base_ref": "feature",
                    },
                }
            ],
            "sessions": [],
        }
        self.options = MODULE.Options(
            report=False,
            model="gpt-5.6-sol",
            prompt="Review the pull request.",
            pull_request=MODULE.PrReference(7, "owner/repo", "owner/repo#7"),
            dispatch_only=False,
            monitor_only=False,
            apply_with_report=True,
            result_file=Path("C:/state/result.json"),
            policy=MODULE.MARKETPLACE_POLICY_SELECTOR,
        )

    def repository(self):
        repository = mock.Mock()
        repository.root.return_value = self.root
        repository.snapshot.return_value = self.snapshot
        repository.head.return_value = self.base_sha
        repository.fetch_pr_inputs.return_value = {}
        repository.align_to_pr.return_value = self.snapshot
        repository.identity.return_value = MODULE.LocalIdentity(
            "feature",
            self.base_sha,
            "",
            None,
        )
        repository.fetch_generated.return_value = (
            "refs/cloud-agent-tasks/request-1/generated"
        )
        repository.ref_sha.return_value = self.artifact_commit
        repository.cloud_commits.return_value = [
            self.code_commit,
            self.artifact_commit,
        ]
        repository.worker_history.return_value = MODULE.WorkerHistory(
            self.code_commit,
            (self.code_commit,),
            self.artifact_commit,
        )
        return repository

    def execute(
        self,
        repository,
        *,
        options=None,
        task=None,
        worker_validation=None,
        mutation_error=None,
    ):
        result = MODULE.ResultEnvelope()
        options = self.options if options is None else options
        task = self.task if task is None else task
        outcomes = worker_validation or [
            {
                "command": "git diff --check",
                "outcome": "passed",
            }
        ]
        mutation = (
            mock.Mock(side_effect=mutation_error)
            if mutation_error is not None
            else mock.Mock()
        )
        report_fetch = mock.Mock(return_value=self.report)
        with (
            mock.patch.object(MODULE, "GitRepository", return_value=repository),
            mock.patch.object(MODULE, "ApiClient"),
            mock.patch.object(
                MODULE,
                "repository_base",
                return_value=SimpleNamespace(branch="main", sha="4" * 40),
            ),
            mock.patch.object(
                MODULE,
                "resolve_pull_request",
                return_value=self.pull_request,
            ),
            mock.patch.object(MODULE, "validate_policy_before_post"),
            mock.patch.object(
                MODULE,
                "validate_policy_before_mutation",
                mutation,
            ),
            mock.patch.object(MODULE, "start_task", return_value=task) as start,
            mock.patch.object(MODULE, "get_task", return_value=task),
            mock.patch.object(MODULE, "monitor_task", return_value=task),
            mock.patch.object(
                MODULE,
                "fetch_worker_receipt",
                return_value=(outcomes, "5" * 64),
            ),
            mock.patch.object(MODULE, "fetch_report", report_fetch),
        ):
            code = MODULE.execute(
                options,
                cwd=self.root,
                uuid_factory=lambda: "request-1",
                result=result,
            )
        self.last_report_fetch = report_fetch
        return code, result, mutation, start

    def test_successfully_attests_and_applies_only_fix_commits(self):
        repository = self.repository()

        code, result, mutation, _ = self.execute(repository)

        self.assertEqual(code, 0)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.application_status, "applied")
        self.assertEqual(result.cloud_commits, [self.code_commit])
        self.assertEqual(result.generated_head, self.artifact_commit)
        self.assertEqual(
            result.receipt_path,
            ".github/agent-task-validations/request-1.json",
        )
        self.assertEqual(result.receipt_commit, self.artifact_commit)
        self.assertEqual(result.receipt_sha256, "5" * 64)
        self.assertEqual(result.report_commit, self.artifact_commit)
        self.last_report_fetch.assert_called_once_with(
            mock.ANY,
            "owner/repo",
            ".github/agent-task-reports/request-1.md",
            self.artifact_commit,
        )
        self.assertEqual(
            result.report_sha256,
            hashlib.sha256(self.report.encode("utf-8")).hexdigest(),
        )
        self.assertTrue(result.validation_complete)
        repository.require_correlated_fix_commits.assert_called_once_with(
            self.root,
            (self.code_commit,),
        )
        repository.fast_forward.assert_called_once_with(
            self.snapshot,
            self.code_commit,
        )
        self.assertGreaterEqual(mutation.call_count, 2)
        self.assertEqual(result.cloud_commits, [self.code_commit])

    def test_structural_apply_attests_without_worker_validation(self):
        repository = self.repository()
        options = MODULE.Options(
            report=False,
            model="gpt-5.6-sol",
            prompt="Review the pull request.",
            pull_request=MODULE.PrReference(7, "owner/repo", "owner/repo#7"),
            apply_with_report=True,
            result_file=Path("C:/state/result.json"),
            policy=MODULE.MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR,
            prompt_file=Path("C:/state/prompt.txt"),
        )

        code, result, _, _ = self.execute(repository, options=options)

        self.assertEqual(code, 0)
        self.assertEqual(result.schema_version, 2)
        self.assertTrue(result.structural_complete)
        self.assertFalse(result.validation_complete)
        self.assertIsNone(result.receipt_path)
        self.assertEqual(result.report_commit, self.artifact_commit)
        self.last_report_fetch.assert_called_once_with(
            mock.ANY,
            "owner/repo",
            ".github/agent-task-reports/request-1.md",
            self.artifact_commit,
        )
        repository.worker_history.assert_called_once_with(
            self.root,
            self.base_sha,
            [self.code_commit, self.artifact_commit],
            [".github/agent-task-reports/request-1.md"],
        )
        envelope = result.as_dict()
        self.assertNotIn("worker_receipt", envelope)
        self.assertNotIn("validation", envelope)
        self.assertEqual(
            envelope["attestation"],
            {
                "kind": "dispatcher_structural",
                "structural_complete": True,
            },
        )

    def test_empty_markdown_report_fails_closed(self):
        repository = self.repository()
        self.report = " \n"

        with self.assertRaisesRegex(MODULE.CloudError, "report is empty"):
            self.execute(repository)

        repository.fast_forward.assert_not_called()

    def test_recovers_interrupted_apply_with_report_without_starting_task(self):
        validation_path = (
            ".github/agent-task-validations/request-1.json"
        )
        report_path = ".github/agent-task-reports/request-1.md"
        prompt = MODULE.build_policy_prompt(
            MODULE.build_pr_prompt(
                MODULE.build_apply_with_report_prompt(
                    "Review the pull request.",
                    report_path,
                    validation_path,
                ),
                self.pull_request,
            ),
            request_id="request-1",
            receipt=validation_path,
            mode="apply_with_report",
            repository="owner/repo",
            pull_request=self.pull_request,
        )
        task = {
            **self.task,
            "created_at": "2026-09-15T00:00:00Z",
            "repository": {"id": 1},
            "owner": {"id": 2},
            "sessions": [
                {
                    "id": "session-1",
                    "task_id": "task-1",
                    "state": "completed",
                    "created_at": "2026-09-15T00:00:00Z",
                    "model": "sweagent-capi:gpt-5.6-sol",
                    "base_ref": "feature",
                    "repository": {"id": 1},
                    "owner": {"id": 2},
                    "prompt": prompt,
                }
            ],
        }
        options = MODULE.Options(
            report=False,
            model="gpt-5.6-sol",
            prompt="",
            pull_request=MODULE.PrReference(
                7,
                "owner/repo",
                "owner/repo#7",
            ),
            apply_with_report=True,
            resume_apply_with_report=True,
            result_file=Path("C:/state/recovery-result.json"),
            policy=MODULE.MARKETPLACE_POLICY_SELECTOR,
            task_id="task-1",
            worker_receipt=validation_path,
        )
        repository = self.repository()

        code, result, _, start = self.execute(
            repository,
            options=options,
            task=task,
        )

        self.assertEqual(code, 0)
        self.assertEqual(result.mode, "apply_with_report")
        self.assertEqual(result.application_status, "applied")
        start.assert_not_called()
        repository.fast_forward.assert_called_once_with(
            self.snapshot,
            self.code_commit,
        )

    def test_monitor_only_keeps_validation_only_history_semantics(self):
        validation_path = (
            ".github/agent-task-validations/request-1.json"
        )
        options = MODULE.Options(
            report=False,
            model="gpt-5.6-sol",
            prompt="",
            pull_request=MODULE.PrReference(
                7,
                "owner/repo",
                "owner/repo#7",
            ),
            monitor_only=True,
            result_file=Path("C:/state/monitor-result.json"),
            policy=MODULE.MARKETPLACE_POLICY_SELECTOR,
            task_id="task-1",
            worker_receipt=validation_path,
        )
        repository = self.repository()
        repository.root.return_value = self.root
        repository.repository_name.return_value = "owner/repo"
        repository.matching_remote.return_value = "origin"

        code, result, _, start = self.execute(
            repository,
            options=options,
        )

        self.assertEqual(code, 0)
        self.assertEqual(result.mode, "monitor_only")
        self.assertEqual(result.application_status, "not_applicable")
        start.assert_not_called()
        repository.worker_history.assert_called_once_with(
            self.root,
            self.base_sha,
            [self.code_commit, self.artifact_commit],
            [validation_path],
        )
        repository.fast_forward.assert_not_called()

    def test_identity_or_live_head_failure_prevents_application(self):
        repository = self.repository()
        error = MODULE.CloudError("pull request head changed", "stale_pr_head")

        with self.assertRaisesRegex(MODULE.CloudError, "pull request head changed"):
            self.execute(repository, mutation_error=error)

        repository.fast_forward.assert_not_called()

    def test_history_gate_failure_retains_generated_evidence(self):
        repository = self.repository()
        repository.worker_history.side_effect = MODULE.CloudError(
            "worker artifact commit changed unexpected paths",
            "unexpected_paths",
        )
        result = MODULE.ResultEnvelope()

        with (
            mock.patch.object(MODULE, "GitRepository", return_value=repository),
            mock.patch.object(MODULE, "ApiClient"),
            mock.patch.object(
                MODULE,
                "repository_base",
                return_value=SimpleNamespace(branch="main", sha="4" * 40),
            ),
            mock.patch.object(
                MODULE,
                "resolve_pull_request",
                return_value=self.pull_request,
            ),
            mock.patch.object(MODULE, "validate_policy_before_post"),
            mock.patch.object(MODULE, "validate_policy_before_mutation"),
            mock.patch.object(MODULE, "start_task", return_value=self.task),
            mock.patch.object(MODULE, "monitor_task", return_value=self.task),
        ):
            with self.assertRaisesRegex(MODULE.CloudError, "unexpected paths"):
                MODULE.execute(
                    self.options,
                    cwd=self.root,
                    uuid_factory=lambda: "request-1",
                    result=result,
                )

        self.assertEqual(result.generated_branch, "copilot/task-1")
        self.assertEqual(result.generated_head, self.artifact_commit)
        self.assertEqual(
            result.cloud_commits,
            [self.code_commit, self.artifact_commit],
        )
        self.assertFalse(result.validation_complete)
        repository.fast_forward.assert_not_called()


class ReportOnlyDispatcherTest(unittest.TestCase):
    def setUp(self):
        self.root = Path("C:/repo")
        self.base_sha = "1" * 40
        self.artifact_commit = "3" * 40
        self.report = "# PR review report\n\n- **Review complete:** yes\n"
        self.snapshot = MODULE.WorktreeSnapshot(
            self.root,
            "owner/repo",
            "origin",
            "feature",
            self.base_sha,
        )
        self.pull_request = MODULE.PullRequestSnapshot(
            7,
            "https://github.com/owner/repo/pull/7",
            "OPEN",
            "owner/repo",
            "main",
            "4" * 40,
            "owner/repo",
            "feature",
            self.base_sha,
            False,
        )
        self.task = {
            "id": "task-1",
            "state": "completed",
            "html_url": "https://github.com/owner/repo/tasks/task-1",
            "artifacts": [
                {
                    "type": "branch",
                    "provider": "github",
                    "data": {
                        "head_ref": "copilot/task-1",
                        "base_ref": "feature",
                    },
                }
            ],
            "sessions": [],
        }
        self.options = MODULE.Options(
            report=True,
            model="gpt-5.6-sol",
            prompt="Review the pull request.",
            pull_request=MODULE.PrReference(7, "owner/repo", "owner/repo#7"),
            result_file=Path("C:/state/result.json"),
            policy=MODULE.MARKETPLACE_REPORT_POLICY_SELECTOR,
        )

    def repository(self):
        repository = mock.Mock()
        repository.root.return_value = self.root
        repository.snapshot.return_value = self.snapshot
        repository.head.return_value = self.base_sha
        repository.fetch_pr_inputs.return_value = {}
        repository.align_to_pr.return_value = self.snapshot
        repository.identity.return_value = MODULE.LocalIdentity(
            "feature",
            self.base_sha,
            "",
            None,
        )
        repository.fetch_generated.return_value = (
            "refs/cloud-agent-tasks/request-1/generated"
        )
        repository.ref_sha.return_value = self.artifact_commit
        repository.cloud_commits.return_value = [self.artifact_commit]
        repository.worker_history.return_value = MODULE.WorkerHistory(
            None,
            (),
            self.artifact_commit,
        )
        return repository

    def test_attests_one_markdown_report_without_worker_validation(self):
        repository = self.repository()
        result = MODULE.ResultEnvelope()
        fetch_receipt = mock.Mock(
            side_effect=AssertionError("report-only mode fetched validation")
        )
        with (
            mock.patch.object(MODULE, "GitRepository", return_value=repository),
            mock.patch.object(MODULE, "ApiClient"),
            mock.patch.object(
                MODULE,
                "repository_base",
                return_value=SimpleNamespace(branch="main", sha="4" * 40),
            ),
            mock.patch.object(
                MODULE,
                "resolve_pull_request",
                return_value=self.pull_request,
            ),
            mock.patch.object(MODULE, "validate_policy_before_post"),
            mock.patch.object(MODULE, "validate_policy_before_mutation"),
            mock.patch.object(MODULE, "start_task", return_value=self.task),
            mock.patch.object(MODULE, "monitor_task", return_value=self.task),
            mock.patch.object(MODULE, "fetch_worker_receipt", fetch_receipt),
            mock.patch.object(MODULE, "fetch_report", return_value=self.report),
        ):
            code = MODULE.execute(
                self.options,
                cwd=self.root,
                uuid_factory=lambda: "request-1",
                result=result,
            )

        envelope = result.as_dict()
        self.assertEqual(code, 0)
        self.assertEqual(envelope["schema"]["version"], 2)
        self.assertEqual(envelope["status"], "success")
        self.assertEqual(
            envelope["attestation"],
            {
                "kind": "dispatcher_structural",
                "structural_complete": True,
            },
        )
        self.assertNotIn("worker_receipt", envelope)
        self.assertNotIn("validation", envelope)
        self.assertEqual(
            envelope["report"]["path"],
            ".github/agent-task-reports/request-1.md",
        )
        self.assertEqual(envelope["generated"]["commits"], [])
        repository.worker_history.assert_called_once_with(
            self.root,
            self.base_sha,
            [self.artifact_commit],
            [".github/agent-task-reports/request-1.md"],
        )
        repository.require_correlated_fix_commits.assert_not_called()
        repository.fast_forward.assert_not_called()
        fetch_receipt.assert_not_called()

    def test_empty_report_fails_without_structural_completion(self):
        repository = self.repository()
        result = MODULE.ResultEnvelope()
        with (
            mock.patch.object(MODULE, "GitRepository", return_value=repository),
            mock.patch.object(MODULE, "ApiClient"),
            mock.patch.object(
                MODULE,
                "repository_base",
                return_value=SimpleNamespace(branch="main", sha="4" * 40),
            ),
            mock.patch.object(
                MODULE,
                "resolve_pull_request",
                return_value=self.pull_request,
            ),
            mock.patch.object(MODULE, "validate_policy_before_post"),
            mock.patch.object(MODULE, "validate_policy_before_mutation"),
            mock.patch.object(MODULE, "start_task", return_value=self.task),
            mock.patch.object(MODULE, "monitor_task", return_value=self.task),
            mock.patch.object(MODULE, "fetch_report", return_value=" \n"),
        ):
            with self.assertRaisesRegex(MODULE.CloudError, "report is empty"):
                MODULE.execute(
                    self.options,
                    cwd=self.root,
                    uuid_factory=lambda: "request-1",
                    result=result,
                )

        self.assertFalse(result.structural_complete)


if __name__ == "__main__":
    unittest.main()
