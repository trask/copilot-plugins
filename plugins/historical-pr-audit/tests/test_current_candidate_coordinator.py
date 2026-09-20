import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from test_agent_task_coordinator import METADATA, MODULE


HELPER = (
    Path(__file__).parents[2] / "agent-tasks-runtime" / "skills"
    / "agent-tasks-runtime" / "scripts" / "cloud_task.py"
)


class CurrentCandidateCoordinatorTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state_path = self.root / "state.json"
        self.runtime = MODULE.load_candidate_runtime(HELPER)
        self.identity = {
            "branch": "trask-pr-audit-7", "head": METADATA["head_sha"], "status": "",
        }
        self.commands = []

    def candidate(self, *, with_code=False):
        runtime = self.runtime
        head, artifact_sha = METADATA["head_sha"], "4" * 40
        code_tip = "8" * 40 if with_code else head
        self.code = [{
            "sha": code_tip, "parent_sha": head, "tree_sha": "9" * 40,
            "patch_sha256": "a" * 64, "changed_paths": ["app.py"],
        }] if with_code else []
        self.artifact = {
            "sha": artifact_sha, "parent_sha": code_tip, "tree_sha": "5" * 40,
            "patch_sha256": "6" * 64, "changed_paths": [MODULE.AUDIT_OUTCOME_PATH],
        }
        prompt = MODULE.build_worker_prompt(
            METADATA, audit_branch="trask-pr-audit-7", max_iterations=5,
            pipeline={"run": None, "iteration": None, "max_iterations": None},
        )
        pr = runtime.PullRequestSnapshot(
            **MODULE.expected_result_pull_request(METADATA),
            state="MERGED", cross_repository=False,
        )
        options = runtime.Options(
            report=False, apply_with_report=True, allow_merged_pr=True,
            model="gpt-5.6-sol", policy=MODULE.AGENT_TASK_POLICY, prompt=prompt,
        )
        prompt_hash = MODULE.sha256_text(
            runtime.task_payload(options, runtime.OUTPUT_REPORT_PATH, pr)["prompt"]
        )
        return {
            "schema": MODULE.CANDIDATE_RESULT_SCHEMA,
            "status": "success", "mode": "code_candidate",
            "repository": {"name_with_owner": "owner/repo"},
            "pull_request": MODULE.expected_result_pull_request(METADATA),
            "requested_model": options.model, "policy": runtime.policy_metadata(options),
            "task": {
                "id": "fresh-audit", "url": None, "state": "completed",
                "base_ref": head, "base_sha": head,
            },
            "generated": {
                "branch": "copilot/fresh-audit", "head_sha": artifact_sha,
                "commits": [entry["sha"] for entry in self.code],
            },
            "application": {"status": "not_applied", "final_local_head": head},
            "report": None, "error": None,
            "attestation": {"kind": "dispatcher_candidate", "structural_complete": True},
            "completion": {
                "request": {"requested_model": options.model, "prompt_sha256": prompt_hash},
                "task": {
                    "id": "fresh-audit", "state": "completed",
                    "created_at": "2026-09-20T20:00:00Z",
                    "completed_at": "2026-09-20T20:01:00Z",
                    "raw_response_sha256": "7" * 64,
                },
                "session": {
                    "id": "fresh-session", "state": "completed",
                    "actual_model": options.model,
                    "created_at": "2026-09-20T20:00:01Z",
                    "completed_at": "2026-09-20T20:01:00Z",
                    "prompt_sha256": prompt_hash,
                },
                "repository": {
                    "name_with_owner": "owner/repo", "id": 1,
                    "owner": {"login": "owner", "id": 2},
                },
                "refs": {"base": head, "generated": "copilot/fresh-audit"},
            },
            "candidate": {
                "schema": runtime.CANDIDATE_MANIFEST_SCHEMA,
                "repository": {"name_with_owner": "owner/repo"},
                "task": {"id": "fresh-audit", "session_id": "fresh-session"},
                "base": {"ref": head, "sha": head},
                "generated": {
                    "ref": "copilot/fresh-audit", "head_sha": artifact_sha,
                    "code_tip_sha": code_tip,
                },
                "code_commits": self.code, "artifact_commit": self.artifact,
            },
        }

    def execute(self, result, *, returncode=0):
        code_tip = self.code[-1]["sha"] if self.code else METADATA["head_sha"]
        repository = mock.Mock()
        repository.identity.return_value = SimpleNamespace(branch=self.identity["branch"])
        repository.head.return_value = METADATA["head_sha"]
        repository.cloud_commits.return_value = [
            *[entry["sha"] for entry in self.code], self.artifact["sha"],
        ]
        repository.candidate_history.return_value = SimpleNamespace(
            code_head=code_tip, code_commits=self.code, artifact_commit=self.artifact,
        )

        def hosted(*args, **kwargs):
            self.assertIsNone(kwargs.get("prior_result_path"))
            kwargs["result_path"].write_text(json.dumps(result), encoding="utf-8")
            return MODULE.subprocess.CompletedProcess([], returncode, "", "")

        def run(command, **kwargs):
            self.commands.append(command)
            if command[3:5] == ["merge", "--ff-only"]:
                self.identity["head"] = command[-1]
            else:
                self.assertEqual("push", command[3])
            return MODULE.subprocess.CompletedProcess(command, 0, "", "")

        args = MODULE.build_parser().parse_args([
            "agent-task", METADATA["pr_url"], "--repo-root", str(self.repo),
            "--state", str(self.state_path),
        ])
        with (
            mock.patch.object(MODULE.subprocess, "Popen", side_effect=AssertionError("external execution")),
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=self.repo),
            mock.patch.object(MODULE, "invocation_state_path", return_value=(self.state_path, "fresh")),
            mock.patch.object(MODULE, "discover_cloud_task", return_value=HELPER),
            mock.patch.object(MODULE, "load_candidate_runtime", return_value=self.runtime),
            mock.patch.object(self.runtime, "GitRepository", return_value=repository),
            mock.patch.object(MODULE, "merged_metadata_for", return_value=METADATA),
            mock.patch.object(MODULE, "require_clean_worktree"),
            mock.patch.object(MODULE, "prepared_branch_after_interruption", return_value={"branch_action": "created"}),
            mock.patch.object(MODULE, "local_identity", side_effect=lambda *_: dict(self.identity)),
            mock.patch.object(MODULE, "execute_managed_agent_task", side_effect=hosted),
            mock.patch.object(MODULE, "git", return_value='{"outcome":"clean","iterations_used":2}'),
            mock.patch.object(MODULE, "run", side_effect=run),
            mock.patch.object(MODULE, "find_remote", return_value="origin"),
            mock.patch.object(MODULE, "remote_head", return_value=None),
            mock.patch.object(MODULE, "wait_for_remote_head", return_value=code_tip),
            mock.patch.object(MODULE, "validate_success_result", side_effect=AssertionError("legacy success")),
            mock.patch.object(MODULE, "validate_recovery_result_identity", side_effect=AssertionError("legacy recovery")),
            mock.patch.object(MODULE, "emit") as emit,
        ):
            MODULE.command_agent_task(args)
        return emit.call_args.args[0]

    def test_fresh_no_code_clean_uses_current_candidate_and_creates_no_remote_branch(self):
        output = self.execute(self.candidate())
        self.assertEqual("nothing_to_publish", output["result"])
        self.assertEqual([], self.commands)
        state = MODULE.load_state(self.state_path)
        self.assertEqual(2, state["iterations"])
        self.assertEqual(0, state["agent_task"]["reserved_iterations"])
        self.assertEqual("completed", state["agent_task"]["status"])
        self.assertFalse(state["agent_task"]["reusable_task"])

    def test_fresh_code_candidate_imports_only_code_tip_and_publishes_audit_branch(self):
        output = self.execute(self.candidate(with_code=True))
        self.assertEqual("published", output["result"])
        self.assertEqual("8" * 40, output["head_sha"])
        self.assertEqual(["merge", "--ff-only", "8" * 40], self.commands[0][3:])
        self.assertIn("HEAD:refs/heads/trask-pr-audit-7", self.commands[1])
        self.assertEqual(2, output["iterations"])

    def test_current_creation_and_completed_task_failures_preserve_diagnostics_without_import(self):
        source = self.candidate()
        for created in (False, True):
            with self.subTest(created=created):
                self.state_path = self.root / f"failure-{created}.json"
                result = copy.deepcopy(source)
                result.update(
                    status="error", candidate=None, completion=None,
                    attestation={"kind": "dispatcher_candidate", "structural_complete": False},
                    error={"code": "candidate_invalid", "message": "fresh candidate rejected"},
                )
                if not created:
                    result["task"] = {key: None for key in result["task"]}
                    result["generated"] = {"branch": None, "head_sha": None, "commits": []}
                with self.assertRaisesRegex(MODULE.WorkflowError, "fresh candidate rejected"):
                    self.execute(result, returncode=1)
                state = MODULE.load_state(self.state_path)
                self.assertEqual("failed", state["agent_task"]["status"])
                self.assertEqual("known" if created else "not_created", state["agent_task"]["task_id_status"])
                self.assertEqual(5, state["agent_task"]["reserved_iterations"])
                self.assertFalse(state["agent_task"]["reusable_task"])
                self.assertEqual([], self.commands)

    def test_legacy_result_is_not_admitted_to_current_workflow(self):
        result = self.candidate()
        result["mode"] = "apply_with_report"
        with self.assertRaisesRegex(MODULE.WorkflowError, "wrong identity"):
            self.execute(result)
        self.assertEqual([], self.commands)
