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

        self.assertIn("Policy: marketplace-agent-worker@2", prompt)
        self.assertIn(f"write `{validation_path}` as a nonempty JSON array", prompt)
        self.assertIn("exactly `command`, `status`, and `detail`", prompt)
        self.assertIn("`status` must be `passed`", prompt)
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

    def test_v1_policy_is_explicitly_rejected(self):
        result_path = str((Path.cwd().parent / "result.json").resolve())
        with self.assertRaisesRegex(
            MODULE.CloudError,
            "unknown policy 'marketplace-agent-worker@1'; expected "
            "marketplace-agent-worker@2",
        ) as raised:
            MODULE.parse_args(
                [
                    "--apply-with-report",
                    "--pr",
                    "owner/repo#1",
                    "--result-file",
                    result_path,
                    "--policy",
                    "marketplace-agent-worker@1",
                    "Review.",
                ]
            )

        self.assertEqual(raised.exception.code, "policy_unknown")


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
                        "status": "passed",
                        "detail": "Executed remotely.",
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
                                "status": "passed",
                                "detail": "Executed remotely.",
                            }
                        ]
                    )
                    + "\n"
                ).encode("utf-8")
            ).hexdigest(),
        )
        run.assert_not_called()
        self.assertFalse(sentinel.exists())

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
            {"command": "test", "status": "failed", "detail": "failed"},
            {"command": "test", "status": "skipped", "detail": "skipped"},
            {
                "command": "test",
                "status": "passed",
                "detail": "ok",
                "extra": True,
            },
            {"command": "test", "result": "passed"},
        ]
        for outcome in cases:
            with self.subTest(outcome=outcome):
                with self.assertRaises(MODULE.CloudError) as raised:
                    self.fetch([outcome])
                self.assertEqual(raised.exception.code, "validation_incomplete")


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
                ("diff-tree", "--no-commit-id"): "untrusted-script.py\0",
            }
        )
        with self.assertRaisesRegex(MODULE.CloudError, "unexpected paths"):
            wrong_path.worker_history(
                Path("C:/repo"),
                base,
                [fix, artifact],
                [".github/agent-task-validations/request-1.json"],
            )


class DispatcherFinalizationTest(unittest.TestCase):
    def setUp(self):
        self.root = Path("C:/repo")
        self.base_sha = "1" * 40
        self.code_commit = "2" * 40
        self.artifact_commit = "3" * 40
        self.report = "Complete consistency report.\n"
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

    def execute(self, repository, *, worker_validation=None, mutation_error=None):
        result = MODULE.ResultEnvelope()
        outcomes = worker_validation or [
            {
                "command": "git diff --check",
                "status": "passed",
                "detail": "No whitespace errors.",
            }
        ]
        mutation = (
            mock.Mock(side_effect=mutation_error)
            if mutation_error is not None
            else mock.Mock()
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
            mock.patch.object(
                MODULE,
                "validate_policy_before_mutation",
                mutation,
            ),
            mock.patch.object(MODULE, "start_task", return_value=self.task),
            mock.patch.object(MODULE, "monitor_task", return_value=self.task),
            mock.patch.object(
                MODULE,
                "fetch_worker_receipt",
                return_value=(outcomes, "5" * 64),
            ),
            mock.patch.object(
                MODULE,
                "fetch_report",
                return_value=self.report,
            ),
        ):
            code = MODULE.execute(
                self.options,
                cwd=self.root,
                uuid_factory=lambda: "request-1",
                result=result,
            )
        return code, result, mutation

    def test_successfully_attests_and_applies_only_fix_commits(self):
        repository = self.repository()

        code, result, mutation = self.execute(repository)

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
        self.assertEqual(
            result.report_sha256,
            hashlib.sha256(self.report.encode("utf-8")).hexdigest(),
        )
        self.assertTrue(result.validation_complete)
        repository.require_structured_fix_commits.assert_called_once_with(
            self.root,
            (self.code_commit,),
        )
        repository.fast_forward.assert_called_once_with(
            self.snapshot,
            self.code_commit,
        )
        self.assertGreaterEqual(mutation.call_count, 2)

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


if __name__ == "__main__":
    unittest.main()
