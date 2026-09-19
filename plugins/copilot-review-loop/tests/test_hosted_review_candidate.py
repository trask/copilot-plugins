import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "copilot_review_loop.py"
SPEC = importlib.util.spec_from_file_location("hosted_review_test", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
RUNTIME_PATH = (
    Path(__file__).parents[2] / "agent-tasks-runtime" / "skills"
    / "agent-tasks-runtime" / "scripts" / "cloud_task.py"
)


class HostedReviewCandidateTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.repo = self.directory / "source"
        self.repo.mkdir()
        self.runtime = MODULE.load_candidate_runtime(RUNTIME_PATH)
        self.git("init", "-q", "-b", "feature")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")
        (self.repo / "example.txt").write_text("before\n", encoding="utf-8")
        self.git("add", "example.txt")
        self.git("commit", "-q", "-m", "Source")
        self.head = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "--detach")
        comment = {
            "id": 17, "source": "thread", "thread_id": "PRRT_fixture",
            "review_id": 29, "url": "https://github.com/owner/repo/pull/7#discussion_r17",
            "author": "copilot-pull-request-reviewer[bot]", "author_bot_id": "BOT_1",
            "path": "example.txt", "line": 1, "original_line": 1,
            "side": "RIGHT", "body": "Handle empty input.", "resolved": False,
        }
        self.preflight = {
            "repository_root": str(self.repo),
            "identity": {"branch": "", "head": self.head, "status": ""},
            "pr": {
                "number": 7, "repo_name": "owner/repo",
                "pr_url": "https://github.com/owner/repo/pull/7",
                "state": "OPEN", "is_draft": True,
                "head_repository": "owner/repo", "head_sha": self.head,
                "head_branch": "feature", "base_sha": self.head,
                "base_branch": "main", "cross_repository": False,
                "title": "Fixture review", "body": "Synthetic replay only.",
            },
            "viewer": {"login": "fixture"},
            "comments": [comment],
            "comment_identities": [MODULE.comment_identity(comment)],
        }
        self.prompt_path = self.directory / "prompt.txt"
        self.result_path = self.directory / "result.json"
        self.decision_path = self.directory / "decisions.json"
        self.canonical_path = self.directory / "canonical.json"
        self.prompt = MODULE.build_worker_prompt(
            self.preflight, request_id="fresh-run", iteration_allowance=1,
            prior_history=[],
        )
        self.prompt_path.write_text(self.prompt, encoding="utf-8")
        self.github = {"head_ref_sha": self.head, "reviews_sha256": "a" * 64}
        self.before_source = MODULE.local_source_fingerprint(self.repo)
        self.commands = []

    def git(self, *arguments):
        return MODULE.git(self.repo, *arguments)

    def candidate(self, *, fixed=True, decision=None, extra_code=False):
        self.git("checkout", "-q", "-b", "generated")
        code = []
        if fixed:
            (self.repo / "example.txt").write_text("after\n", encoding="utf-8")
            self.git("add", "example.txt")
            self.git("commit", "-q", "-m", "Handle fixture input")
            code.append(self.git("rev-parse", "HEAD"))
        if extra_code:
            (self.repo / "extra.txt").write_text("extra\n", encoding="utf-8")
            self.git("add", "extra.txt")
            self.git("commit", "-q", "-m", "Extra")
            code.append(self.git("rev-parse", "HEAD"))
        if decision is None:
            item = {
                "finding_id": MODULE.decision_finding_id("fresh-run", 0),
                "disposition": "fixed" if fixed else "no_change",
            }
            if not fixed:
                item.update(reason="Already handled.", proposed_reply="Already handled.")
            decision = {"decisions": [item]}
        path = self.repo / MODULE.HOSTED_DECISION_PATH
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(decision), encoding="utf-8")
        self.git("add", str(path))
        self.git("commit", "-q", "-m", "Review decisions")
        artifact = self.git("rev-parse", "HEAD")
        history = MODULE.candidate_git_repository(self.runtime).candidate_history(
            self.repo, self.head, [*code, artifact], report_only=False
        )
        self.git("checkout", "-q", "--detach", self.head)
        snapshot = self.runtime.PullRequestSnapshot(
            state="OPEN", cross_repository=False,
            **MODULE.expected_cloud_pull_request(self.preflight),
        )
        submitted = self.runtime.task_payload(
            self.runtime.Options(
                report=False, model="gpt-5.6-sol", prompt=self.prompt,
                apply_with_report=True, policy=MODULE.HOSTED_DECISION_POLICY,
            ),
            report_path=self.runtime.OUTPUT_REPORT_PATH, pull_request=snapshot,
        )["prompt"]
        digest = MODULE.sha256_text(submitted)
        timestamps = {
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:01:00Z",
            "completed_at": "2026-01-01T00:01:00Z",
        }
        self.result = {
            "schema": MODULE.CANDIDATE_AGENT_TASK_RESULT_SCHEMA,
            "status": "success", "error": None, "mode": "code_candidate",
            "requested_model": "gpt-5.6-sol",
            "repository": {"name_with_owner": "owner/repo"},
            "pull_request": MODULE.expected_cloud_pull_request(self.preflight),
            "policy": {
                "id": self.runtime.MARKETPLACE_CODE_CANDIDATE_POLICY_ID,
                "version": self.runtime.MARKETPLACE_CODE_CANDIDATE_POLICY_VERSION,
                "sha256": self.runtime.MARKETPLACE_CODE_CANDIDATE_POLICY_HASH,
            },
            "task": {
                "id": "task-fixture", "url": None, "state": "completed",
                "base_ref": "feature", "base_sha": self.head,
            },
            "generated": {"branch": "generated", "head_sha": artifact, "commits": code},
            "application": {"status": "not_applied", "final_local_head": self.head},
            "report": None,
            "attestation": {"kind": "dispatcher_candidate", "structural_complete": True},
            "candidate": {
                "schema": self.runtime.CANDIDATE_MANIFEST_SCHEMA,
                "repository": {"name_with_owner": "owner/repo"},
                "task": {"id": "task-fixture", "session_id": "session-fixture"},
                "base": {"ref": "feature", "sha": self.head},
                "generated": {
                    "ref": "generated", "head_sha": artifact, "code_tip_sha": history.code_head,
                },
                "code_commits": list(history.code_commits),
                "artifact_commit": history.artifact_commit,
            },
            "completion": {
                "request": {"requested_model": "gpt-5.6-sol", "prompt_sha256": digest},
                "task": {
                    "id": "task-fixture", "state": "completed",
                    "raw_response_sha256": "a" * 64, **timestamps,
                },
                "session": {
                    "id": "session-fixture", "state": "completed",
                    "actual_model": "gpt-5.6-sol", "prompt_sha256": digest, **timestamps,
                },
                "repository": {
                    "name_with_owner": "owner/repo", "id": 1,
                    "owner": {"login": "owner", "id": 2},
                },
                "refs": {"base": "feature", "generated": "generated"},
            },
        }
        return history.code_head

    def dispatch(self, command, **kwargs):
        self.commands.append((command, kwargs))
        self.result_path.write_text(json.dumps(self.result), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    def run_worker(self):
        with (
            mock.patch.object(MODULE, "github_decision_fingerprint", return_value=self.github),
            mock.patch.object(MODULE, "run_owned_local_worker", side_effect=self.dispatch),
            mock.patch.object(MODULE, "run_local_decision_worker", side_effect=AssertionError("local fallback")),
        ):
            return MODULE.run_hosted_decision_worker(
                repo_root=self.repo, target={}, preflight=self.preflight,
                prompt_path=self.prompt_path, result_path=self.result_path,
                decision_path=self.decision_path, canonical_path=self.canonical_path,
                run_id="fresh-run", requested_model="gpt-5.6-sol",
                before_source=self.before_source, before_github=self.github,
                helper=RUNTIME_PATH, timeout=123,
            )

    def test_hosted_fix_imports_only_verified_code_tip_and_keeps_detached(self):
        tip = self.candidate()
        bundle = self.run_worker()
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))
        command, options = self.commands[0]
        self.assertEqual(str(RUNTIME_PATH), command[1])
        self.assertIn(MODULE.HOSTED_DECISION_POLICY, command)
        self.assertNotIn("--allow-all-tools", command)
        self.assertEqual(123, options["timeout"])
        self.assertNotIn("GH_CONFIG_DIR", options["environment"])
        self.assertEqual(tip, bundle["remote"]["final_local_head"])
        self.assertEqual("fixed", bundle["report"]["comments"][0]["disposition"])
        MODULE.apply_verified_import(
            self.repo, result_path=self.result_path,
            result_sha256=MODULE.sha256_file(self.result_path),
            report_content=bundle["report_content"],
            preflight=self.preflight, remote=bundle["remote"],
        )
        self.assertEqual(tip, self.git("rev-parse", "HEAD"))
        self.assertEqual("", MODULE.local_identity(self.repo)["branch"])
        self.assertFalse((self.repo / MODULE.HOSTED_DECISION_PATH).exists())
        self.assertEqual("", self.git("status", "--porcelain"))

    def test_hosted_no_change_still_has_exact_finding_decision(self):
        self.candidate(fixed=False)
        bundle = self.run_worker()
        self.assertEqual([], bundle["remote"]["commits"])
        self.assertEqual(self.head, bundle["remote"]["final_local_head"])
        self.assertEqual("no_change", bundle["report"]["comments"][0]["disposition"])

    def test_identity_manifest_and_completion_drift_fail_before_import(self):
        self.candidate()
        original = copy.deepcopy(self.result)
        cases = [
            ("policy", "version", 99), ("pull_request", "head_sha", "f" * 40),
            ("application", "status", "applied"), ("task", "state", "running"),
            ("candidate", "code_commits", []),
            ("completion.session", "actual_model", "another-model"),
            ("completion.session", "prompt_sha256", "f" * 64),
            ("completion.request", "prompt_sha256", "f" * 64),
            ("completion.refs", "base", "different"),
            ("completion.repository", "id", True),
            ("completion.session", "created_at", "not-a-timestamp"),
        ]
        for path, key, value in cases:
            with self.subTest(path=path, key=key):
                self.result = copy.deepcopy(original)
                target = self.result
                for part in path.split("."):
                    target = target[part]
                target[key] = value
                with self.assertRaises(MODULE.WorkflowError):
                    self.run_worker()
                self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_bad_decisions_do_not_modify_source(self):
        self.candidate(decision={"decisions": []})
        with self.assertRaisesRegex(MODULE.WorkflowError, "stale or incomplete"):
            self.run_worker()
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))
        self.assertTrue(self.result_path.is_file())
        self.assertTrue(self.decision_path.is_file())

    def test_extra_code_commit_is_rejected(self):
        self.candidate(extra_code=True)
        with self.assertRaisesRegex(MODULE.WorkflowError, "manifest or decisions"):
            self.run_worker()

    def test_legacy_result_cannot_supply_a_fresh_hosted_candidate(self):
        self.candidate()
        self.result["schema"] = {**self.result["schema"], "version": 2}
        with self.assertRaisesRegex(MODULE.WorkflowError, "unsupported schema"):
            self.run_worker()
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_source_changes_are_evidence_not_permission_to_import(self):
        self.candidate()
        (self.repo / "example.txt").write_text("unexpected local change\n", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.WorkflowError, "pinned source"):
            self.run_worker()
        self.assertEqual("unexpected local change\n", (self.repo / "example.txt").read_text())
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_github_review_drift_rejects_candidate(self):
        self.candidate()
        before = copy.deepcopy(self.github)
        self.github["reviews_sha256"] = "f" * 64
        with (
            mock.patch.object(MODULE, "github_decision_fingerprint", return_value=before),
            mock.patch.object(MODULE, "run_owned_local_worker", side_effect=self.dispatch),
            self.assertRaisesRegex(MODULE.WorkflowError, "GitHub"),
        ):
            MODULE.run_hosted_decision_worker(
                repo_root=self.repo, target={}, preflight=self.preflight,
                prompt_path=self.prompt_path, result_path=self.result_path,
                decision_path=self.decision_path, canonical_path=self.canonical_path,
                run_id="fresh-run", requested_model="gpt-5.6-sol",
                before_source=self.before_source, before_github=self.github,
                helper=RUNTIME_PATH, timeout=123,
            )
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_suppressed_finding_keeps_synthetic_identity(self):
        comment = self.preflight["comments"][0]
        comment.update(id=-17, source="suppressed", thread_id=None)
        self.preflight["comment_identities"] = [MODULE.comment_identity(comment)]
        self.prompt = MODULE.build_worker_prompt(
            self.preflight, request_id="fresh-run", iteration_allowance=1, prior_history=[]
        )
        self.prompt_path.write_text(self.prompt, encoding="utf-8")
        self.candidate(fixed=False)
        bundle = self.run_worker()
        self.assertEqual(-17, bundle["report"]["comments"][0]["id"])
        self.assertIsNone(bundle["report"]["comments"][0]["thread_id"])

    def test_no_change_cannot_conceal_candidate_code(self):
        self.candidate(decision={
            "decisions": [{
                "finding_id": MODULE.decision_finding_id("fresh-run", 0),
                "disposition": "no_change", "reason": "No fix", "proposed_reply": "No fix",
            }],
        })
        with self.assertRaisesRegex(MODULE.WorkflowError, "account for every fix commit"):
            self.run_worker()
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_missing_result_is_explicit_and_has_no_local_fallback(self):
        def no_result(command, **_kwargs):
            return subprocess.CompletedProcess(command, 1, "", "failure")
        self.dispatch = no_result
        with self.assertRaisesRegex(MODULE.WorkflowError, "without a result"):
            self.run_worker()


class HostedDispatcherOwnershipTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "native Windows job ownership")
    def test_native_windows_timeout_reaps_owned_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "child.pid"
            script = (
                "import pathlib, subprocess, sys, time; "
                "child = subprocess.Popen("
                "[sys.executable, '-c', 'import time; time.sleep(60)'], "
                "creationflags=subprocess.CREATE_NO_WINDOW); "
                f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid)); "
                "time.sleep(60)"
            )
            try:
                with self.assertRaisesRegex(MODULE.WorkflowError, "timed out"):
                    MODULE.run_owned_local_worker(
                        [sys.executable, "-c", script], cwd=Path(directory),
                        input_text="", timeout=2, environment=MODULE.subprocess_environment(),
                        description="synthetic dispatcher",
                    )
                self.assertTrue(pid_path.is_file())
                pid = int(pid_path.read_text())
                deadline = time.monotonic() + 5
                while MODULE.process_is_running(pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(MODULE.process_is_running(pid))
            finally:
                if pid_path.is_file():
                    pid = int(pid_path.read_text())
                    if MODULE.process_is_running(pid):
                        os.kill(pid, MODULE.signal.SIGTERM)

    def test_posix_shutdown_kills_descendants_after_parent_exit(self):
        process = mock.Mock(pid=42, returncode=0)
        with (
            mock.patch.object(MODULE, "IS_WINDOWS", False),
            mock.patch.object(MODULE.os, "killpg", create=True) as kill_group,
            mock.patch.object(MODULE.signal, "SIGKILL", 9, create=True),
        ):
            MODULE.terminate_owned_local_worker(process, None, timeout=1)
        self.assertEqual(
            [mock.call(42, MODULE.signal.SIGTERM), mock.call(42, 9)],
            kill_group.call_args_list,
        )

    def test_windows_dispatcher_uses_no_window_and_authenticated_controller_environment(self):
        process = mock.Mock(pid=42)
        owner = mock.Mock()
        environment = {"GH_TOKEN": "fixture-only"}
        with (
            mock.patch.object(MODULE, "IS_WINDOWS", True),
            mock.patch.object(MODULE.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
            mock.patch.object(MODULE.subprocess, "Popen", return_value=process) as launch,
            mock.patch.object(MODULE, "create_windows_kill_job", return_value=owner),
            mock.patch.object(MODULE, "resume_windows_process") as resume,
        ):
            returned, ownership = MODULE.popen_owned_local_worker(
                ["python", "pinned-runtime.py"], cwd=Path("."), environment=environment
            )
        self.assertIs(returned, process)
        self.assertIs(ownership, owner)
        self.assertEqual(environment, launch.call_args.kwargs["env"])
        self.assertTrue(launch.call_args.kwargs["creationflags"] & 0x08000000)
        self.assertTrue(launch.call_args.kwargs["creationflags"] & 0x00000004)
        resume.assert_called_once_with(42)

    def test_timeout_terminates_job_even_if_dispatcher_parent_already_exited(self):
        process = mock.Mock(pid=42, returncode=1)
        process.poll.return_value = 1
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["python"], 1), ("", ""),
        ]
        owner = mock.Mock()
        with (
            mock.patch.object(MODULE, "popen_owned_local_worker", return_value=(process, owner)),
            self.assertRaisesRegex(MODULE.WorkflowError, "hosted Agent Task dispatcher timed out"),
        ):
            MODULE.run_owned_local_worker(
                ["python", "pinned-runtime.py"], cwd=Path("."), input_text="",
                timeout=1, environment={}, description="hosted Agent Task dispatcher",
            )
        owner.terminate.assert_called_once()
        owner.close.assert_called_once()
        process.wait.assert_called_once()
        self.assertEqual(
            MODULE.LOCAL_DECISION_TERMINATION_TIMEOUT_SECONDS,
            process.communicate.call_args.kwargs["timeout"],
        )

    def test_output_drain_timeout_is_explicit(self):
        process = mock.Mock(pid=42, returncode=1)
        process.poll.return_value = 1
        process.communicate.side_effect = subprocess.TimeoutExpired(["python"], 1)
        owner = mock.Mock()
        with (
            mock.patch.object(MODULE, "popen_owned_local_worker", return_value=(process, owner)),
            self.assertRaisesRegex(MODULE.WorkflowError, "shutdown did not drain output"),
        ):
            MODULE.run_owned_local_worker(
                ["python", "pinned-runtime.py"], cwd=Path("."), input_text="",
                timeout=1, environment={}, description="hosted Agent Task dispatcher",
            )
        owner.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
