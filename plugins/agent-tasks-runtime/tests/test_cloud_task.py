import base64
from dataclasses import replace
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
                    "marketplace-agent-apply-report-worker@2, "
                    "marketplace-agent-apply-report-worker@3, "
                    "marketplace-agent-apply-report-worker@4, "
                    "marketplace-agent-apply-report-worker@5, "
                    "marketplace-agent-code-candidate-worker@1, "
                    "marketplace-agent-report-recommendation-worker@1, "
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

    def test_historical_structural_policies_cannot_start_a_new_task(self):
        result_path = str((Path.cwd().parent / "result.json").resolve())

        for policy in (
            MODULE.MARKETPLACE_APPLY_REPORT_POLICY_V1_SELECTOR,
            MODULE.MARKETPLACE_APPLY_REPORT_POLICY_V2_SELECTOR,
        ):
            with self.subTest(policy=policy):
                with self.assertRaisesRegex(
                    MODULE.CloudError,
                    "available only for task recovery",
                ):
                    MODULE.parse_args(
                        [
                            "--apply-with-report",
                            "--pr",
                            "owner/repo#1",
                            "--prompt-file",
                            str(SCRIPT),
                            "--result-file",
                            result_path,
                            "--policy",
                            policy,
                        ]
                    )

        self.assertNotIn(
            MODULE.MARKETPLACE_APPLY_REPORT_POLICY_V3_SELECTOR,
            MODULE.RECOVERY_ONLY_APPLY_REPORT_POLICY_SELECTORS,
        )

    def test_structural_policy_v3_remains_dispatchable_for_existing_consumers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt_path = root / "prompt.txt"
            prompt_path.write_text("Review and fix.", encoding="utf-8")
            options = MODULE.parse_args(
                [
                    "--apply-with-report",
                    "--pr",
                    "owner/repo#1",
                    "--prompt-file",
                    str(prompt_path),
                    "--result-file",
                    str(root / "result.json"),
                    "--policy",
                    MODULE.MARKETPLACE_APPLY_REPORT_POLICY_V3_SELECTOR,
                ]
            )

        self.assertEqual(
            options.policy,
            MODULE.MARKETPLACE_APPLY_REPORT_POLICY_V3_SELECTOR,
        )

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
                    "requires|disabled",
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
            policy=MODULE.MARKETPLACE_APPLY_REPORT_POLICY_V3_SELECTOR,
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
            "Policy: marketplace-agent-apply-report-worker@3",
            prompt,
        )
        self.assertIn("zero or more linear commits", prompt)
        self.assertIn("Record finding-to-commit and changed-path correlation only", prompt)
        self.assertNotIn("`Finding: <identifier>`", prompt)
        self.assertIn("exactly one final single-parent report commit", prompt)
        self.assertIn(f"only changed path must be `{report_path}`", prompt)
        self.assertIn("untrusted inert evidence", prompt)
        self.assertNotIn("agent-task-validations", prompt)
        self.assertNotIn(MODULE.VALIDATION_PATH_PLACEHOLDER, prompt)

    def test_semantic_policy_removes_worker_owned_identity_and_commit_shas(self):
        semantic_path = ".github/agent-task-semantic/request-1.json"
        options = MODULE.Options(
            report=False,
            model="gpt-5.6-sol",
            prompt=f"Write `{MODULE.SEMANTIC_PATH_PLACEHOLDER}`.",
            pull_request=MODULE.PrReference(1, "owner/repo", "owner/repo#1"),
            apply_with_report=True,
            result_file=(Path.cwd().parent / "result.json").resolve(),
            policy=MODULE.MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR,
            prompt_file=(Path.cwd().parent / "prompt.txt").resolve(),
            semantic_kind="self-review-loop",
        )

        payload = MODULE.task_payload(
            options,
            report_path=semantic_path,
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
            repository="owner/repo",
        )
        prompt = payload["prompt"]

        self.assertIn("Policy: marketplace-agent-apply-report-worker@5", prompt)
        self.assertIn(f"only changed path is `{semantic_path}`", prompt)
        self.assertIn("Do not wrap it in schema, kind, version, or payload fields", prompt)
        self.assertNotIn('"kind": "self-review-loop"', prompt)
        self.assertIn("one-based integer `commit_index`", prompt)
        self.assertIn("identities belong only to the dispatcher", prompt.lower())
        self.assertNotIn(MODULE.SEMANTIC_PATH_PLACEHOLDER, prompt)

    def test_semantic_binding_rejects_identity_and_requires_every_commit(self):
        commits = ["1" * 40, "2" * 40]
        payload, references = MODULE.bind_semantic_payload(
            {
                "findings": [
                    {"commit_index": 1},
                    {"commit_index": 2},
                    {"commit_index": 2},
                ]
            },
            commits,
        )
        self.assertEqual(references, {1, 2})
        self.assertEqual(
            [finding["commit"] for finding in payload["findings"]],
            [commits[0], commits[1], commits[1]],
        )
        with self.assertRaisesRegex(MODULE.CloudError, "dispatcher-owned"):
            MODULE.bind_semantic_payload(
                {"repository": "wrong/repo", "findings": []},
                [],
            )
        for field in ("commit", "commits", "sha"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(MODULE.CloudError, "dispatcher-owned"):
                    MODULE.bind_semantic_payload(
                        {"findings": [{field: "1" * 40}]},
                        [],
                    )

    def test_artifact_placeholders_do_not_cross_policy_boundaries(self):
        with self.assertRaisesRegex(MODULE.CloudError, "unresolved artifact"):
            MODULE.render_artifact_paths(
                f"Write {MODULE.SEMANTIC_PATH_PLACEHOLDER}.",
                report_path=".github/agent-task-reports/request-1.md",
                worker_receipt=None,
                semantic=False,
            )
        with self.assertRaisesRegex(MODULE.CloudError, "unresolved artifact"):
            MODULE.render_artifact_paths(
                f"Write {MODULE.REPORT_PATH_PLACEHOLDER}.",
                report_path=".github/agent-task-semantic/request-1.json",
                worker_receipt=None,
                semantic=True,
            )

    def test_clean_semantic_output_cannot_hide_generated_fix_history(self):
        content = json.dumps(
            {
                "outcome": "cleared",
                "iterations_used": 1,
                "findings": [],
                "pull_request_metadata": {"decision": "keep"},
            }
        )
        api = mock.Mock()
        api.request_json.return_value = {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }

        with self.assertRaisesRegex(
            MODULE.CloudError,
            "does not account for every generated fix commit",
        ):
            MODULE.fetch_semantic_output(
                api,
                "owner/repo",
                ".github/agent-task-semantic/request-1.json",
                "copilot/task-1",
                kind="self-review-loop",
                commits=["1" * 40],
            )

    def test_current_policy_preserves_exact_20075_payload_without_normalizing_it(self):
        value = {
            "version": 1,
            "payload": {
                "findings": [],
                "result": "clean",
                "title": "Report configured Redis targets for Rediscala",
                "body": "Reports configured Redis deployments.",
            },
        }
        content = json.dumps(value)
        api = mock.Mock()
        api.request_json.return_value = {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }

        payload, digest = MODULE.fetch_semantic_output(
            api,
            "owner/repo",
            ".github/agent-task-semantic/request-1.json",
            "copilot/task-1",
            kind="self-review-loop",
            commits=[],
        )

        self.assertEqual(value, payload)
        self.assertEqual(hashlib.sha256(content.encode("utf-8")).hexdigest(), digest)

    def test_policy_v4_keeps_the_legacy_model_authored_wrapper(self):
        prompt = MODULE.build_apply_report_policy_prompt(
            "Review.",
            report_path=".github/agent-task-semantic/request-1.json",
            policy=MODULE.MARKETPLACE_APPLY_REPORT_POLICY_V4_SELECTOR,
            semantic_kind="self-review-loop",
        )

        self.assertIn("Policy: marketplace-agent-apply-report-worker@4", prompt)
        self.assertIn('"kind": "self-review-loop"', prompt)
        self.assertIn('"schema":', prompt)

    def test_structural_recovery_is_disabled(self):
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
            MODULE.MARKETPLACE_APPLY_REPORT_POLICY_V3_SELECTOR,
        ]
        with self.assertRaisesRegex(MODULE.CloudError, "disabled"):
            MODULE.parse_args(base)
        with self.assertRaisesRegex(MODULE.CloudError, "disabled"):
            MODULE.parse_args(
                [
                    *base,
                    "--request-id",
                    "request-1",
                    "--worker-receipt",
                    ".github/agent-task-validations/request-1.json",
                ]
            )
        with self.assertRaisesRegex(MODULE.CloudError, "disabled"):
            MODULE.parse_args([*base, "--request-id", "request-1"])


class PullRequestResolutionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd()
        self.stale_base = "1" * 40
        self.live_base = "2" * 40
        self.head = "3" * 40
        self.metadata = {
            "number": 7,
            "url": "https://github.com/owner/repo/pull/7",
            "state": "OPEN",
            "baseRefName": "release/next",
            "baseRefOid": self.stale_base,
            "headRepository": {"nameWithOwner": "owner/repo"},
            "headRepositoryOwner": {"login": "owner"},
            "headRefName": "feature",
            "headRefOid": self.head,
            "isCrossRepository": False,
        }

    def test_uses_live_base_ref_tip_instead_of_stale_base_ref_oid(self):
        def runner(command, **_kwargs):
            if command[:3] == ["gh", "pr", "view"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(self.metadata),
                    stderr="",
                )
            if command[:2] == ["git", "check-ref-format"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="",
                    stderr="",
                )
            if command[:2] == ["gh", "api"]:
                self.assertEqual(
                    command[2],
                    "repos/owner/repo/git/ref/heads/release%2Fnext",
                )
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "ref": "refs/heads/release/next",
                            "object": {"sha": self.live_base},
                        }
                    ),
                    stderr="",
                )
            self.fail(f"unexpected command: {command}")

        pull_request = MODULE.resolve_pull_request(
            runner,
            self.root,
            "owner/repo",
            MODULE.parse_pr_reference("owner/repo#7"),
        )

        self.assertEqual(self.live_base, pull_request.base_sha)
        self.assertNotEqual(self.stale_base, pull_request.base_sha)

    def test_rejects_mismatched_live_base_ref_identity(self):
        runner = mock.Mock(
            side_effect=[
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps(self.metadata),
                    stderr="",
                ),
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps(
                        {
                            "ref": "refs/heads/main",
                            "object": {"sha": self.live_base},
                        }
                    ),
                    stderr="",
                ),
            ]
        )

        with self.assertRaisesRegex(
            MODULE.CloudError,
            "invalid base branch identity",
        ):
            MODULE.resolve_pull_request(
                runner,
                self.root,
                "owner/repo",
                MODULE.parse_pr_reference("owner/repo#7"),
            )


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
            policy=MODULE.MARKETPLACE_APPLY_REPORT_POLICY_V3_SELECTOR,
        )
        task = self.mutated_task(prompt=prompt)

        self.validate(
            task,
            worker_receipt=None,
            policy=MODULE.MARKETPLACE_APPLY_REPORT_POLICY_V3_SELECTOR,
        )

    def test_structural_v1_recovery_cannot_cross_policy_versions(self):
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
            policy=MODULE.MARKETPLACE_APPLY_REPORT_POLICY_V1_SELECTOR,
        )
        task = self.mutated_task(prompt=prompt)

        self.validate(
            task,
            worker_receipt=None,
            policy=MODULE.MARKETPLACE_APPLY_REPORT_POLICY_V1_SELECTOR,
        )
        with self.assertRaisesRegex(
            MODULE.CloudError,
            "does not prove the original apply-with-report policy",
        ):
            self.validate(
                task,
                worker_receipt=None,
                policy=MODULE.MARKETPLACE_APPLY_REPORT_POLICY_V3_SELECTOR,
            )

    def test_parse_rejects_complete_recovery_identity(self):
        result_path = str((Path.cwd().parent / "result.json").resolve())
        with self.assertRaisesRegex(MODULE.CloudError, "disabled"):
            MODULE.parse_args(
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

    def test_parse_rejects_monitor_mode(self):
        result_path = str((Path.cwd().parent / "result.json").resolve())
        with self.assertRaisesRegex(MODULE.CloudError, "monitor-only.*disabled"):
            MODULE.parse_args(
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
        semantic_fetch = mock.Mock(
            return_value=(
                {
                    "outcome": "cleared",
                    "findings": [{"commit": self.code_commit}],
                },
                "6" * 64,
            )
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
            mock.patch.object(MODULE, "start_task", return_value=task) as start,
            mock.patch.object(MODULE, "get_task", return_value=task),
            mock.patch.object(MODULE, "monitor_task", return_value=task),
            mock.patch.object(
                MODULE,
                "fetch_worker_receipt",
                return_value=(outcomes, "5" * 64),
            ),
            mock.patch.object(MODULE, "fetch_report", report_fetch),
            mock.patch.object(
                MODULE,
                "fetch_semantic_output",
                semantic_fetch,
            ),
        ):
            code = MODULE.execute(
                options,
                cwd=self.root,
                uuid_factory=lambda: "request-1",
                result=result,
            )
        self.last_report_fetch = report_fetch
        self.last_semantic_fetch = semantic_fetch
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
            policy=MODULE.MARKETPLACE_APPLY_REPORT_POLICY_V3_SELECTOR,
            prompt_file=Path("C:/state/prompt.txt"),
        )

        code, result, _, _ = self.execute(repository, options=options)

        self.assertEqual(code, 0)
        self.assertEqual(result.schema_version, 2)
        self.assertTrue(result.structural_complete)
        self.assertFalse(result.validation_complete)
        self.assertIsNone(result.receipt_path)
        self.assertEqual(result.application_status, "not_applied")
        self.assertEqual(result.final_local_head, self.base_sha)
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
        repository.require_correlated_fix_commits.assert_not_called()
        repository.fast_forward.assert_not_called()
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

    def test_semantic_apply_binds_payload_without_worker_report(self):
        repository = self.repository()
        options = MODULE.Options(
            report=False,
            model="gpt-5.6-sol",
            prompt="Write {{MARKETPLACE_SEMANTIC_PATH}}.",
            pull_request=MODULE.PrReference(7, "owner/repo", "owner/repo#7"),
            apply_with_report=True,
            result_file=Path("C:/state/result.json"),
            policy=MODULE.MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR,
            prompt_file=Path("C:/state/prompt.txt"),
            semantic_kind="self-review-loop",
        )

        code, result, _, _ = self.execute(repository, options=options)

        self.assertEqual(code, 0)
        self.assertEqual(result.schema_version, 4)
        self.assertTrue(result.structural_complete)
        self.assertIsNone(result.report_path)
        self.assertEqual(
            result.semantic_path,
            ".github/agent-task-semantic/request-1.json",
        )
        self.assertEqual(result.semantic_commit, self.artifact_commit)
        self.assertEqual(result.semantic_sha256, "6" * 64)
        self.assertEqual(
            result.semantic_payload,
            {
                "outcome": "cleared",
                "findings": [{"commit": self.code_commit}],
            },
        )
        self.assertEqual(result.application_status, "not_applied")
        repository.fast_forward.assert_not_called()
        self.last_report_fetch.assert_not_called()
        self.last_semantic_fetch.assert_called_once()
        envelope = result.as_dict()
        self.assertEqual(envelope["attestation"]["kind"], "dispatcher_semantic")
        self.assertEqual(envelope["semantic_output"]["schema"], MODULE.SEMANTIC_OUTPUT_SCHEMA)
        self.assertIsNone(envelope["report"])

    def test_legacy_semantic_apply_never_imports_worker_commits(self):
        repository = self.repository()
        options = MODULE.Options(
            report=False,
            model="gpt-5.6-sol",
            prompt="Write {{MARKETPLACE_SEMANTIC_PATH}}.",
            pull_request=MODULE.PrReference(7, "owner/repo", "owner/repo#7"),
            apply_with_report=True,
            result_file=Path("C:/state/result.json"),
            policy=MODULE.MARKETPLACE_APPLY_REPORT_POLICY_V4_SELECTOR,
            prompt_file=Path("C:/state/prompt.txt"),
            semantic_kind="self-review-loop",
        )

        code, result, _, _ = self.execute(repository, options=options)

        self.assertEqual(code, 0)
        self.assertEqual(result.schema_version, 3)
        self.assertEqual(result.application_status, "not_applied")
        self.assertEqual(result.final_local_head, self.base_sha)
        repository.fast_forward.assert_not_called()

    def creation_failure(self, options):
        repository = self.repository()
        failure = MODULE.CloudError(
            "start Agent Task failed with HTTP 409: "
            "user or repo does not have CCA enabled; "
            "the request cannot be completed",
            "api_failure",
        )
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.json"
            options = MODULE.Options(
                **{
                    **options.__dict__,
                    "result_file": result_path,
                }
            )
            with (
                mock.patch.object(MODULE, "parse_args", return_value=options),
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
                    "start_task",
                    side_effect=failure,
                ) as start,
            ):
                code = MODULE.main(
                    [],
                    cwd=self.root,
                    uuid_factory=lambda: "request-1",
                    stderr=io.StringIO(),
                )
            envelope = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(2, code)
        self.assertEqual(
            {
                "code": "api_failure",
                "message": str(failure),
            },
            envelope["error"],
        )
        self.assertEqual({"name_with_owner": "owner/repo"}, envelope["repository"])
        self.assertEqual(
            "https://github.com/owner/repo/pull/7",
            envelope["pull_request"]["url"],
        )
        self.assertEqual("gpt-5.6-sol", envelope["requested_model"])
        self.assertEqual(MODULE.policy_metadata(options), envelope["policy"])
        self.assertEqual(
            {
                "id": None,
                "url": None,
                "state": None,
                "base_ref": None,
                "base_sha": None,
            },
            envelope["task"],
        )
        self.assertEqual(
            {"branch": None, "head_sha": None, "commits": []},
            envelope["generated"],
        )
        self.assertIn("request-1", start.call_args.args[2]["prompt"])
        return envelope

    def test_structural_v3_creation_failure_has_no_report_identity(self):
        options = MODULE.Options(
            report=False,
            model="gpt-5.6-sol",
            prompt="Review the pull request.",
            pull_request=MODULE.PrReference(7, "owner/repo", "owner/repo#7"),
            apply_with_report=True,
            result_file=Path("C:/state/result.json"),
            policy=MODULE.MARKETPLACE_APPLY_REPORT_POLICY_V3_SELECTOR,
            prompt_file=Path("C:/state/prompt.txt"),
        )

        envelope = self.creation_failure(options)

        self.assertIsNone(envelope["report"])
        self.assertNotIn("semantic_output", envelope)

    def test_semantic_v4_creation_failure_has_no_output_identity(self):
        options = MODULE.Options(
            report=False,
            model="gpt-5.6-sol",
            prompt="Write {{MARKETPLACE_SEMANTIC_PATH}}.",
            pull_request=MODULE.PrReference(7, "owner/repo", "owner/repo#7"),
            apply_with_report=True,
            result_file=Path("C:/state/result.json"),
            policy=MODULE.MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR,
            prompt_file=Path("C:/state/prompt.txt"),
            semantic_kind="self-review-loop",
        )

        envelope = self.creation_failure(options)

        self.assertIsNone(envelope["report"])
        self.assertIsNone(envelope["semantic_output"])

    def test_structural_v1_keeps_commit_trailer_correlation(self):
        repository = self.repository()
        options = MODULE.Options(
            report=False,
            model="gpt-5.6-sol",
            prompt="Review the pull request.",
            pull_request=MODULE.PrReference(7, "owner/repo", "owner/repo#7"),
            apply_with_report=True,
            result_file=Path("C:/state/result.json"),
            policy=MODULE.MARKETPLACE_APPLY_REPORT_POLICY_V1_SELECTOR,
            prompt_file=Path("C:/state/prompt.txt"),
        )

        self.execute(repository, options=options)

        repository.require_correlated_fix_commits.assert_called_once_with(
            self.root,
            (self.code_commit,),
        )

    def test_structural_v2_keeps_prevalidation_application_behavior(self):
        repository = self.repository()
        options = MODULE.Options(
            report=False,
            model="gpt-5.6-sol",
            prompt="Review the pull request.",
            pull_request=MODULE.PrReference(7, "owner/repo", "owner/repo#7"),
            apply_with_report=True,
            result_file=Path("C:/state/result.json"),
            policy=MODULE.MARKETPLACE_APPLY_REPORT_POLICY_V2_SELECTOR,
            prompt_file=Path("C:/state/prompt.txt"),
        )

        _, result, _, _ = self.execute(repository, options=options)

        self.assertEqual(result.application_status, "applied")
        repository.require_correlated_fix_commits.assert_not_called()
        repository.fast_forward.assert_called_once()

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


class CandidatePolicyTest(unittest.TestCase):
    def test_policy_and_schema_audit_identities_are_immutable(self):
        expected_hashes = {
            "MARKETPLACE_POLICY_HASH": (
                "a9a1592c15abb39c077c5af0e23b46b7b0e3fc3d747e02f41975813130b0c096"
            ),
            "MARKETPLACE_REPORT_POLICY_HASH": (
                "b6ce6f5940c28fac03dda5be647c693e2a83e0c8f7eaf38f1b644b47bf49f2a2"
            ),
            "MARKETPLACE_APPLY_REPORT_POLICY_V1_HASH": (
                "ea61b3edb7eb56b262d80eccb3b6a7e20a2167d5ca4381db66b7663bca33dd78"
            ),
            "MARKETPLACE_APPLY_REPORT_POLICY_V2_HASH": (
                "411a9ba9a0931d40c685c6233639b15c31e0d6daa4b29706527424016367cad2"
            ),
            "MARKETPLACE_APPLY_REPORT_POLICY_V3_HASH": (
                "7d48868140710139939cabc803a99f2122305e97dedbffa747e5f69903c16af1"
            ),
            "MARKETPLACE_APPLY_REPORT_POLICY_V4_HASH": (
                "708e601f66db19d501f1f92ac5444980f025c84b0266ef9be1a178d37c36274b"
            ),
            "MARKETPLACE_APPLY_REPORT_POLICY_HASH": (
                "8e843c0e41703fc067ae317da15916f839b57970f2fbb240629d9f610f55f82b"
            ),
            "MARKETPLACE_CODE_CANDIDATE_POLICY_HASH": (
                "a110207256318e2df4b23b95c0b6843193cf64319bd1017731afdf0615705270"
            ),
            "MARKETPLACE_REPORT_RECOMMENDATION_POLICY_HASH": (
                "07aeb40461735368b72a570123a1afcb12d21f3a6b70cfa3dfd4e6dc2e6308ab"
            ),
        }
        for name, expected in expected_hashes.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(MODULE, name), expected)
        self.assertEqual(MODULE.RESULT_SCHEMA_VERSION, 1)
        self.assertEqual(MODULE.REPORT_RESULT_SCHEMA_VERSION, 2)
        self.assertEqual(MODULE.LEGACY_SEMANTIC_RESULT_SCHEMA_VERSION, 3)
        self.assertEqual(MODULE.SEMANTIC_RESULT_SCHEMA_VERSION, 4)
        self.assertEqual(MODULE.CANDIDATE_RESULT_SCHEMA_VERSION, 5)
        self.assertEqual(
            MODULE.LEGACY_SEMANTIC_OUTPUT_SCHEMA,
            {
                "id": "github.copilot.agent-task-semantic-output",
                "version": 1,
            },
        )
        self.assertEqual(
            MODULE.SEMANTIC_OUTPUT_SCHEMA,
            {
                "id": "github.copilot.agent-task-semantic-output",
                "version": 2,
            },
        )
        self.assertEqual(
            MODULE.CANDIDATE_MANIFEST_SCHEMA,
            {
                "id": "github.copilot.agent-task-candidate-manifest",
                "version": 1,
            },
        )

    def test_code_candidate_policy_uses_fixed_optional_output(self):
        options = MODULE.Options(
            report=False,
            model="gpt-5.6-sol",
            prompt=f"Write advisory prose to {MODULE.REPORT_PATH_PLACEHOLDER}.",
            pull_request=MODULE.PrReference(1, "owner/repo", "owner/repo#1"),
            apply_with_report=True,
            result_file=Path("C:/state/result.json"),
            policy=MODULE.MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR,
            prompt_file=Path("C:/state/prompt.txt"),
        )
        pull_request = MODULE.PullRequestSnapshot(
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
        )

        prompt = MODULE.task_payload(
            options,
            report_path=MODULE.OUTPUT_REPORT_PATH,
            pull_request=pull_request,
            request_id="ignored-request-id",
            repository="owner/repo",
        )["prompt"]

        self.assertIn(
            "Policy: marketplace-agent-code-candidate-worker@1",
            prompt,
        )
        self.assertIn("zero or more linear single-parent commits", prompt)
        self.assertIn("artifact commit is optional", prompt)
        self.assertIn(MODULE.OUTPUT_REPORT_PATH, prompt)
        self.assertIn("never imports or applies candidate commits", prompt)
        self.assertNotIn(MODULE.REPORT_PATH_PLACEHOLDER, prompt)
        self.assertNotIn(MODULE.APPLY_WITH_REPORT_MARKER, prompt)

    def test_report_recommendation_policy_forbids_code(self):
        prompt = MODULE.build_candidate_policy_prompt(
            "Review only.",
            policy=MODULE.MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR,
        )

        self.assertIn(
            "Policy: marketplace-agent-report-recommendation-worker@1",
            prompt,
        )
        self.assertIn("Do not create code", prompt)
        self.assertIn("exactly one final single-parent artifact commit", prompt)
        self.assertIn("contents are never mechanical validation evidence", prompt)

    def test_candidate_policies_are_fresh_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt_path = root / "prompt.txt"
            prompt_path.write_text("Review.", encoding="utf-8")
            result_path = root / "result.json"
            code = MODULE.parse_args(
                [
                    "--apply-with-report",
                    "--pr",
                    "owner/repo#1",
                    "--prompt-file",
                    str(prompt_path),
                    "--result-file",
                    str(result_path),
                    "--policy",
                    MODULE.MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR,
                ]
            )
            report = MODULE.parse_args(
                [
                    "--report",
                    "--pr",
                    "owner/repo#1",
                    "--prompt-file",
                    str(prompt_path),
                    "--result-file",
                    str(result_path),
                    "--policy",
                    MODULE.MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR,
                ]
            )

            self.assertEqual(
                code.policy,
                MODULE.MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR,
            )
            self.assertEqual(
                report.policy,
                MODULE.MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR,
            )
            with self.assertRaisesRegex(MODULE.CloudError, "disabled|does not support"):
                MODULE.parse_args(
                    [
                        "--apply-with-report",
                        "--allow-merged-pr",
                        "--pr",
                        "owner/repo#1",
                        "--prompt-file",
                        str(prompt_path),
                        "--result-file",
                        str(result_path),
                        "--policy",
                        MODULE.MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR,
                    ]
                )


class CandidateHistoryTest(unittest.TestCase):
    def repository(self, *, parents, paths, trees=None, patches=None):
        trees = trees or {}
        patches = patches or {}

        def runner(command, **kwargs):
            args = command[1:]
            if args[:3] == ["rev-list", "--parents", "-n"]:
                commit = args[-1]
                output = parents[commit]
            elif args[:3] == ["diff-tree", "--no-commit-id", "--name-only"]:
                commit = args[-1]
                output = "\0".join(paths[commit]) + "\0"
            elif args[:3] == ["show", "-s", "--format=%T"]:
                commit = args[-1]
                output = trees.get(commit, "a" * 40) + "\n"
            elif args and args[0] == "diff-tree" and "--patch" in args:
                commit = args[-1]
                output = patches.get(commit, f"patch for {commit}\n")
            else:
                raise AssertionError(f"unexpected git command: {command}")
            return subprocess.CompletedProcess(command, 0, output, "")

        return MODULE.GitRepository(runner, Path.exists)

    def test_derives_ordered_code_and_trailing_artifact_manifest_entries(self):
        base = "1" * 40
        first = "2" * 40
        second = "3" * 40
        artifact = "4" * 40
        repository = self.repository(
            parents={
                first: f"{first} {base}\n",
                second: f"{second} {first}\n",
                artifact: f"{artifact} {second}\n",
            },
            paths={
                first: ["src/a.py"],
                second: ["README.md", "src/b.py"],
                artifact: [
                    ".github/agent-task-output/details.json",
                    MODULE.OUTPUT_REPORT_PATH,
                ],
            },
            trees={
                first: "a" * 40,
                second: "b" * 40,
                artifact: "c" * 40,
            },
        )

        history = repository.candidate_history(
            Path("C:/repo"),
            base,
            [first, second, artifact],
            report_only=False,
        )

        self.assertEqual(history.code_head, second)
        self.assertEqual(
            [commit["sha"] for commit in history.code_commits],
            [first, second],
        )
        self.assertEqual(
            history.code_commits[1]["changed_paths"],
            ["README.md", "src/b.py"],
        )
        self.assertEqual(history.code_commits[0]["parent_sha"], base)
        self.assertEqual(history.code_commits[1]["parent_sha"], first)
        self.assertEqual(history.code_commits[1]["tree_sha"], "b" * 40)
        self.assertRegex(history.code_commits[1]["patch_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(history.artifact_commit["sha"], artifact)
        self.assertEqual(history.artifact_commit["parent_sha"], second)

    def test_code_candidate_accepts_zero_commits_and_no_artifact(self):
        history = self.repository(parents={}, paths={}).candidate_history(
            Path("C:/repo"),
            "1" * 40,
            [],
            report_only=False,
        )

        self.assertEqual(history.code_head, "1" * 40)
        self.assertEqual(history.code_commits, ())
        self.assertIsNone(history.artifact_commit)

    def test_report_recommendation_accepts_one_inert_output_commit(self):
        base = "1" * 40
        artifact = "2" * 40
        history = self.repository(
            parents={artifact: f"{artifact} {base}\n"},
            paths={artifact: [".github/agent-task-output/arbitrary.bin"]},
        ).candidate_history(
            Path("C:/repo"),
            base,
            [artifact],
            report_only=True,
        )

        self.assertEqual(history.code_commits, ())
        self.assertEqual(history.artifact_commit["sha"], artifact)

    def test_rejects_mixed_multiple_nonfinal_and_unsafe_output_history(self):
        base = "1" * 40
        first = "2" * 40
        second = "3" * 40
        cases = {
            "mixes": (
                [first],
                {first: f"{first} {base}\n"},
                {first: ["src/a.py", MODULE.OUTPUT_REPORT_PATH]},
            ),
            "multiple": (
                [first, second],
                {
                    first: f"{first} {base}\n",
                    second: f"{second} {first}\n",
                },
                {
                    first: [MODULE.OUTPUT_REPORT_PATH],
                    second: [".github/agent-task-output/details.json"],
                },
            ),
            "unsafe": (
                [first],
                {first: f"{first} {base}\n"},
                {first: ["src/../secret.txt"]},
            ),
        }
        for name, (commits, parents, paths) in cases.items():
            with self.subTest(name=name):
                repository = self.repository(parents=parents, paths=paths)
                with self.assertRaises(MODULE.CloudError):
                    repository.candidate_history(
                        Path("C:/repo"),
                        base,
                        commits,
                        report_only=False,
                    )

    def test_rejects_non_linear_and_report_only_code_history(self):
        base = "1" * 40
        code = "2" * 40
        merge = "3" * 40
        nonlinear = self.repository(
            parents={code: f"{code} {base} {merge}\n"},
            paths={code: ["src/a.py"]},
        )
        with self.assertRaisesRegex(MODULE.CloudError, "linear single-parent"):
            nonlinear.candidate_history(
                Path("C:/repo"),
                base,
                [code],
                report_only=False,
            )

        report_with_code = self.repository(
            parents={code: f"{code} {base}\n"},
            paths={code: ["src/a.py"]},
        )
        with self.assertRaisesRegex(MODULE.CloudError, "forbids candidate code"):
            report_with_code.candidate_history(
                Path("C:/repo"),
                base,
                [code],
                report_only=True,
            )

        with self.assertRaisesRegex(MODULE.CloudError, "requires one final output"):
            self.repository(parents={}, paths={}).candidate_history(
                Path("C:/repo"),
                base,
                [],
                report_only=True,
            )

    def test_rejects_generated_history_unrelated_to_the_fetched_base(self):
        def runner(command, **kwargs):
            if command[1:4] == ["merge-base", "--is-ancestor", "1" * 40]:
                return subprocess.CompletedProcess(command, 1, "", "")
            raise AssertionError(f"unexpected git command: {command}")

        repository = MODULE.GitRepository(runner, Path.exists)
        with self.assertRaisesRegex(MODULE.CloudError, "not an ancestor"):
            repository.cloud_commits(
                Path("C:/repo"),
                "1" * 40,
                "refs/cloud-agent-tasks/request-1/generated",
            )


class FreshCompletionEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.prompt = "Managed prompt"
        self.task = {
            "id": "task-1",
            "state": "completed",
            "created_at": "2026-09-18T12:00:00Z",
            "updated_at": "2026-09-18T12:03:00Z",
            "completed_at": "2026-09-18T12:03:00Z",
            "repository": {
                "id": 11,
                "full_name": "owner/repo",
            },
            "owner": {
                "id": 12,
                "login": "owner",
            },
            "base_ref": "feature",
            "head_ref": "copilot/task-1",
            "sessions": [
                {
                    "id": "session-1",
                    "task_id": "task-1",
                    "state": "completed",
                    "created_at": "2026-09-18T12:00:01Z",
                    "updated_at": "2026-09-18T12:03:00Z",
                    "completed_at": "2026-09-18T12:03:00Z",
                    "model": "sweagent-capi:gpt-5.6-sol",
                    "base_ref": "feature",
                    "head_ref": "copilot/task-1",
                    "repository": {
                        "id": 11,
                        "full_name": "owner/repo",
                    },
                    "owner": {
                        "id": 12,
                        "login": "owner",
                    },
                    "prompt": self.prompt,
                }
            ],
        }

    def validate(self, task=None, **overrides):
        arguments = {
            "expected_task_id": "task-1",
            "repository": "owner/repo",
            "requested_model": "gpt-5.6-sol",
            "expected_prompt": self.prompt,
            "expected_base_ref": "feature",
            "generated_ref": "copilot/task-1",
            "raw_task_response_sha256": "a" * 64,
        }
        arguments.update(overrides)
        return MODULE.validate_fresh_completion(
            self.task if task is None else task,
            **arguments,
        )

    def mutate(self, *, task_updates=None, session_updates=None):
        task = json.loads(json.dumps(self.task))
        task.update(task_updates or {})
        task["sessions"][0].update(session_updates or {})
        return task

    def test_persists_fresh_task_session_model_prompt_and_ref_evidence(self):
        evidence = self.validate()

        self.assertEqual(evidence["task"]["id"], "task-1")
        self.assertEqual(evidence["session"]["id"], "session-1")
        self.assertEqual(
            evidence["session"]["actual_model"],
            "sweagent-capi:gpt-5.6-sol",
        )
        self.assertEqual(
            evidence["request"]["prompt_sha256"],
            hashlib.sha256(self.prompt.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(evidence["task"]["raw_response_sha256"], "a" * 64)
        self.assertEqual(evidence["repository"]["id"], 11)
        self.assertEqual(evidence["repository"]["owner"]["id"], 12)
        self.assertEqual(evidence["refs"]["base"], "feature")
        self.assertEqual(evidence["refs"]["generated"], "copilot/task-1")

    def test_rejects_ambiguous_stale_or_unbound_completion(self):
        cases = [
            (
                self.mutate(
                    task_updates={
                        "sessions": self.task["sessions"] * 2,
                    }
                ),
                {},
            ),
            (self.mutate(session_updates={"model": "gpt-6-astra"}), {}),
            (self.mutate(session_updates={"prompt": "other"}), {}),
            (self.mutate(session_updates={"base_ref": "other"}), {}),
            (self.mutate(session_updates={"head_ref": "copilot/other"}), {}),
            (
                self.mutate(session_updates={"repository": {"id": 99}}),
                {},
            ),
            (
                self.mutate(session_updates={"owner": {"id": 99}}),
                {},
            ),
            (self.task, {"repository": "other/repo"}),
            (self.task, {"expected_task_id": "other-task"}),
            (
                self.mutate(
                    task_updates={"completed_at": "2026-09-18T11:59:00Z"}
                ),
                {},
            ),
            (self.task, {"raw_task_response_sha256": None}),
        ]
        for task, overrides in cases:
            with self.subTest(task=task, overrides=overrides):
                with self.assertRaises(MODULE.CloudError):
                    self.validate(task, **overrides)

    def test_api_client_hashes_the_exact_success_response_body(self):
        body = '{\r\n "id": "task-1", "state": "completed"\r\n}\r\n'

        def runner(command, **kwargs):
            return subprocess.CompletedProcess(
                command,
                0,
                f"HTTP/2 200 OK\r\nContent-Type: application/json\r\n\r\n{body}",
                "",
            )

        api = MODULE.ApiClient(runner)
        value = api.request_json(
            "GET",
            "agents/repos/owner/repo/tasks/task-1",
            expected_status=200,
            operation="poll Agent Task task-1",
        )

        self.assertEqual(value["id"], "task-1")
        self.assertEqual(
            api.last_response_sha256,
            hashlib.sha256(body.encode("utf-8")).hexdigest(),
        )


class DetachedCandidateCheckoutTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.git("init", "--quiet", "--initial-branch", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("commit", "--quiet", "--allow-empty", "-m", "source")
        self.git("remote", "add", "origin", "https://github.com/owner/repo.git")
        self.head = self.git("rev-parse", "HEAD")
        self.git("checkout", "--quiet", "--detach", self.head)
        self.repository = MODULE.GitRepository(
            runner=lambda command, **kwargs: subprocess.run(
                command, env=dict(os.environ), **kwargs
            )
        )
        self.repository.repository_name = mock.Mock(return_value="owner/repo")

    def git(self, *arguments):
        return subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            env=dict(os.environ),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        ).stdout.strip()

    def test_exact_head_detached_candidate_preserves_checkout(self):
        snapshot = self.repository.snapshot(self.root, allow_detached=True)

        self.assertIsNone(snapshot.branch)
        self.assertEqual(self.head, snapshot.head)
        self.repository.require_unchanged(snapshot)
        self.assertEqual(
            snapshot,
            self.repository.align_to_pr(
                snapshot,
                SimpleNamespace(number=7, head_sha=self.head),
                MODULE.PrTrackingRefs("refs/heads/main", "refs/heads/main"),
            ),
        )
        self.assertIsNone(self.repository.identity(self.root).branch)
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_legacy_code_mode_still_requires_a_branch(self):
        with self.assertRaisesRegex(MODULE.CloudError, "checked-out local branch"):
            self.repository.snapshot(self.root)

    def test_detached_candidate_cannot_align_a_different_head(self):
        self.git("commit", "--quiet", "--allow-empty", "-m", "different head")
        snapshot = self.repository.snapshot(self.root, allow_detached=True)
        with self.assertRaisesRegex(
            MODULE.CloudError, "must already match the pull request head"
        ) as error:
            self.repository.align_to_pr(
                snapshot,
                SimpleNamespace(number=7, head_sha=self.head),
                MODULE.PrTrackingRefs("refs/heads/main", "refs/heads/main"),
            )
        self.assertEqual("local_drift", error.exception.code)
        self.assertEqual(snapshot.head, self.git("rev-parse", "HEAD"))
        self.assertIsNone(self.repository.identity(self.root).branch)

    def test_detached_candidate_rejects_dirty_worktrees(self):
        (self.root / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.CloudError, "clean worktree"):
            self.repository.snapshot(self.root, allow_detached=True)

    def test_detached_candidate_rejects_in_progress_operations(self):
        marker = self.root / self.git("rev-parse", "--git-path", "MERGE_HEAD")
        marker.write_text(self.head + "\n", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.CloudError, "in-progress merge"):
            self.repository.snapshot(self.root, allow_detached=True)

    def test_detached_candidate_rejects_branch_drift(self):
        snapshot = self.repository.snapshot(self.root, allow_detached=True)
        self.git("checkout", "--quiet", "main")
        with self.assertRaisesRegex(MODULE.CloudError, "worktree moved from branch"):
            self.repository.require_unchanged(snapshot)

    def test_detached_candidate_rejects_head_drift(self):
        snapshot = self.repository.snapshot(self.root, allow_detached=True)
        self.git("commit", "--quiet", "--allow-empty", "-m", "head drift")
        with self.assertRaisesRegex(MODULE.CloudError, "worktree HEAD moved"):
            self.repository.require_unchanged(snapshot)


class CandidateDispatcherTest(unittest.TestCase):
    def test_derives_manifest_without_reading_or_applying_worker_output(self):
        self.check_candidate("feature")

    def test_derives_manifest_from_exact_head_detached_checkout(self):
        self.check_candidate(None)

    def test_head_movement_during_preparation_does_not_start_a_task(self):
        self.check_candidate(None, drift_phase="preparation")

    def test_post_completion_drift_retains_completed_task_without_accepting_work(self):
        for branch in ("feature", None):
            for field, value in (
                ("head_sha", "9" * 40),
                ("head_ref", "foreign-branch"),
                ("head_repository", "foreign/repo"),
                ("state", "CLOSED"),
            ):
                with self.subTest(branch=branch, field=field):
                    self.check_candidate(
                        branch,
                        drift_phase="completion",
                        drift_fields={field: value},
                    )

    def check_candidate(self, branch, *, drift_phase=None, drift_fields=None):
        root = Path("C:/repo")
        base_sha = "1" * 40
        code_commit = "2" * 40
        artifact_commit = "3" * 40
        snapshot = MODULE.WorktreeSnapshot(
            root,
            "owner/repo",
            "origin",
            branch,
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
        options = MODULE.Options(
            report=False,
            model="gpt-5.6-sol",
            prompt="Review and prepare candidate fixes.",
            pull_request=MODULE.PrReference(7, "owner/repo", "owner/repo#7"),
            apply_with_report=True,
            result_file=Path("C:/state/result.json"),
            policy=MODULE.MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR,
            prompt_file=Path("C:/state/prompt.txt"),
        )
        submitted_prompt = MODULE.task_payload(
            options,
            MODULE.OUTPUT_REPORT_PATH,
            pull_request,
            request_id="request-1",
            repository="owner/repo",
        )["prompt"]
        task = {
            "id": "task-1",
            "state": "completed",
            "created_at": "2026-09-18T12:00:00Z",
            "completed_at": "2026-09-18T12:03:00Z",
            "repository": {"id": 11, "full_name": "owner/repo"},
            "owner": {"id": 12, "login": "owner"},
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
            "sessions": [
                {
                    "id": "session-1",
                    "task_id": "task-1",
                    "state": "completed",
                    "created_at": "2026-09-18T12:00:01Z",
                    "completed_at": "2026-09-18T12:03:00Z",
                    "model": "sweagent-capi:gpt-5.6-sol",
                    "base_ref": "feature",
                    "head_ref": "copilot/task-1",
                    "repository": {"id": 11, "full_name": "owner/repo"},
                    "owner": {"id": 12, "login": "owner"},
                    "prompt": submitted_prompt,
                }
            ],
        }
        code_metadata = {
            "sha": code_commit,
            "parent_sha": base_sha,
            "tree_sha": "a" * 40,
            "patch_sha256": "b" * 64,
            "changed_paths": ["src/a.py"],
        }
        artifact_metadata = {
            "sha": artifact_commit,
            "parent_sha": code_commit,
            "tree_sha": "c" * 40,
            "patch_sha256": "d" * 64,
            "changed_paths": [MODULE.OUTPUT_REPORT_PATH],
        }
        repository = mock.Mock()
        repository.snapshot.return_value = snapshot
        repository.root.return_value = root
        repository.head.return_value = base_sha
        repository.fetch_pr_inputs.return_value = {}
        repository.align_to_pr.return_value = snapshot
        repository.identity.return_value = MODULE.LocalIdentity(
            branch,
            base_sha,
            "",
            None,
        )
        repository.fetch_generated.return_value = (
            "refs/cloud-agent-tasks/request-1/generated"
        )
        repository.ref_sha.return_value = artifact_commit
        repository.cloud_commits.return_value = [code_commit, artifact_commit]
        repository.candidate_history.return_value = MODULE.CandidateHistory(
            code_commit,
            (code_metadata,),
            artifact_metadata,
        )
        api = mock.Mock()
        api.last_response_sha256 = "e" * 64
        drifted = replace(
            pull_request, **(drift_fields or {"head_sha": "9" * 40})
        )
        observations = [
            pull_request,
            drifted if drift_phase == "preparation" else pull_request,
            drifted if drift_phase == "completion" else pull_request,
        ]
        with (
            mock.patch.object(MODULE, "GitRepository", return_value=repository),
            mock.patch.object(MODULE, "ApiClient", return_value=api),
            mock.patch.object(
                MODULE,
                "repository_base",
                return_value=SimpleNamespace(branch="main", sha="4" * 40),
            ),
            mock.patch.object(
                MODULE,
                "resolve_pull_request",
                side_effect=observations,
            ) as resolve,
            mock.patch.object(MODULE, "validate_policy_before_post"),
            mock.patch.object(
                MODULE, "start_task", return_value={**task, "state": "queued"}
            ) as start,
            mock.patch.object(MODULE, "monitor_task", return_value=task) as monitor,
            mock.patch.object(
                MODULE,
                "fetch_report",
                side_effect=AssertionError("candidate policy parsed worker prose"),
            ),
            mock.patch.object(MODULE, "parse_args", return_value=options),
            mock.patch.object(MODULE, "atomic_write_json") as write_result,
        ):
            code = MODULE.main(
                [
                    "--result-file", str(options.result_file),
                    "--policy", options.policy,
                ],
                cwd=root,
                uuid_factory=lambda: "request-1",
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        write_result.assert_called_once()
        self.assertEqual(write_result.call_args.args[0], options.result_file)
        envelope = write_result.call_args.args[1]
        repository.fast_forward.assert_not_called()
        repository.cherry_pick.assert_not_called()
        repository.snapshot.assert_called_once_with(root, allow_detached=True)
        if drift_phase is not None:
            self.assertEqual(code, 2)
            self.assertEqual(envelope["status"], "error")
            self.assertEqual(envelope["error"]["code"], "stale_pr_head")
            message = envelope["error"]["message"]
            for field in drift_fields or {"head_sha": "9" * 40}:
                self.assertIn(
                    f"{field} expected={getattr(pull_request, field)!r} "
                    f"observed={getattr(drifted, field)!r}",
                    message,
                )
            self.assertIsNone(envelope["candidate"])
            self.assertIsNone(envelope["completion"])
            self.assertFalse(envelope["attestation"]["structural_complete"])
            self.assertEqual(envelope["application"]["status"], "not_applied")
            self.assertEqual(envelope["application"]["final_local_head"], base_sha)
            self.assertIsNone(envelope["generated"]["head_sha"])
            self.assertEqual(envelope["generated"]["commits"], [])
            repository.fetch_generated.assert_not_called()
            repository.candidate_history.assert_not_called()
            if drift_phase == "preparation":
                start.assert_not_called()
                monitor.assert_not_called()
                self.assertEqual(resolve.call_count, 2)
                self.assertIsNone(envelope["task"]["id"])
                self.assertIsNone(envelope["generated"]["branch"])
                self.assertIn("the Agent Task was not started", message)
                self.assertNotIn("post-completion", message)
            else:
                start.assert_called_once()
                monitor.assert_called_once()
                self.assertEqual(resolve.call_count, 3)
                self.assertEqual(envelope["task"]["id"], "task-1")
                self.assertEqual(envelope["task"]["state"], "completed")
                self.assertEqual(envelope["task"]["base_sha"], base_sha)
                self.assertEqual(envelope["generated"]["branch"], "copilot/task-1")
                self.assertIn("post-completion validation", message)
                self.assertIn("refusing to accept generated work", message)
                self.assertNotIn("not started", message)
            return
        self.assertEqual(code, 0)
        self.assertEqual(envelope["schema"]["version"], 5)
        self.assertEqual(
            envelope["policy"],
            {
                "id": "marketplace-agent-code-candidate-worker",
                "version": 1,
                "sha256": MODULE.MARKETPLACE_CODE_CANDIDATE_POLICY_HASH,
            },
        )
        self.assertEqual(
            envelope["candidate"]["schema"],
            MODULE.CANDIDATE_MANIFEST_SCHEMA,
        )
        self.assertEqual(
            envelope["candidate"]["generated"]["code_tip_sha"],
            code_commit,
        )
        self.assertEqual(
            envelope["candidate"]["artifact_commit"]["sha"],
            artifact_commit,
        )
        self.assertEqual(envelope["generated"]["commits"], [code_commit])
        self.assertEqual(envelope["completion"]["session"]["id"], "session-1")
        self.assertEqual(
            envelope["completion"]["task"]["raw_response_sha256"],
            "e" * 64,
        )
        self.assertEqual(envelope["application"]["status"], "not_applied")
        self.assertEqual(
            envelope["attestation"],
            {
                "kind": "dispatcher_candidate",
                "structural_complete": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
