import base64
import importlib.util
import json
from pathlib import Path
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


class ReceiptFailureTest(unittest.TestCase):
    def test_legacy_receipt_names_missing_contract_fields(self):
        receipt = {
            "schema": {
                "id": MODULE.RECEIPT_SCHEMA_ID,
                "version": MODULE.RECEIPT_SCHEMA_VERSION,
            },
            "request_id": "request-1",
            "policy": {
                "id": MODULE.MARKETPLACE_POLICY_ID,
                "version": MODULE.MARKETPLACE_POLICY_VERSION,
                "sha256": MODULE.MARKETPLACE_POLICY_HASH,
            },
            "mode": "apply_with_report",
            "repository": "owner/repo",
            "pull_request_head_sha": "1" * 40,
            "validation": [{"command": "git diff --check"}],
        }
        encoded = base64.b64encode(
            (json.dumps(receipt) + "\n").encode("utf-8")
        ).decode("ascii")
        api = mock.Mock()
        api.request_json.return_value = {
            "type": "file",
            "encoding": "base64",
            "content": encoded,
        }
        pull_request = SimpleNamespace(head_sha="1" * 40)

        with self.assertRaisesRegex(
            MODULE.CloudError,
            "missing fields: validation_complete",
        ) as raised:
            MODULE.fetch_worker_receipt(
                api,
                "owner/repo",
                ".github/agent-task-receipts/request-1.json",
                "copilot/task-1",
                request_id="request-1",
                expected_mode="apply_with_report",
                pull_request=pull_request,
            )

        self.assertEqual(raised.exception.code, "malformed_report")

    def test_receipt_failure_retains_verified_generated_history(self):
        root = Path("C:/repo")
        base_sha = "1" * 40
        code_commit = "2" * 40
        receipt_commit = "3" * 40
        generated_head = receipt_commit
        snapshot = MODULE.WorktreeSnapshot(
            root,
            "owner/repo",
            "origin",
            "feature",
            base_sha,
        )
        pull_request = MODULE.PullRequestSnapshot(
            7,
            "https://github.com/owner/repo/pull/7",
            "OPEN",
            "owner/repo",
            "main",
            "4" * 40,
            "owner/repo",
            "feature",
            base_sha,
            False,
        )
        task = {
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
        options = MODULE.Options(
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
        repository = mock.Mock()
        repository.snapshot.return_value = snapshot
        repository.head.return_value = base_sha
        repository.fetch_pr_inputs.return_value = {}
        repository.align_to_pr.return_value = snapshot
        repository.identity.return_value = MODULE.LocalIdentity(
            "feature",
            base_sha,
            "",
            None,
        )
        repository.fetch_generated.return_value = (
            "refs/cloud-agent-tasks/request-1/generated"
        )
        repository.ref_sha.return_value = generated_head
        repository.cloud_commits.return_value = [code_commit, receipt_commit]
        repository.worker_history.return_value = MODULE.WorkerHistory(
            code_commit,
            (code_commit,),
            receipt_commit,
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
                return_value=pull_request,
            ),
            mock.patch.object(MODULE, "validate_policy_before_post"),
            mock.patch.object(MODULE, "validate_policy_before_mutation"),
            mock.patch.object(MODULE, "start_task", return_value=task),
            mock.patch.object(MODULE, "monitor_task", return_value=task),
            mock.patch.object(
                MODULE,
                "fetch_worker_receipt",
                side_effect=MODULE.CloudError(
                    "marketplace worker receipt has unexpected or missing fields: "
                    "missing fields: validation_complete",
                    "malformed_report",
                ),
            ),
        ):
            with self.assertRaisesRegex(
                MODULE.CloudError,
                "missing fields: validation_complete",
            ):
                MODULE.execute(
                    options,
                    cwd=root,
                    uuid_factory=lambda: "request-1",
                    result=result,
                )

        self.assertEqual(result.generated_branch, "copilot/task-1")
        self.assertEqual(result.generated_head, generated_head)
        self.assertEqual(result.cloud_commits, [code_commit])
        self.assertEqual(result.receipt_commit, receipt_commit)
        self.assertEqual(
            result.report_path,
            ".github/agent-task-reports/request-1.md",
        )
        self.assertEqual(result.report_commit, receipt_commit)
        self.assertFalse(result.validation_complete)


if __name__ == "__main__":
    unittest.main()
