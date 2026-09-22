import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
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
    @classmethod
    def setUpClass(cls):
        cls.template = tempfile.TemporaryDirectory()
        template = Path(cls.template.name)
        cls.template_repo = template / "source"
        cls.template_repo.mkdir()
        runtime_path = template / "runtime" / "cloud_task.py"
        runtime_path.parent.mkdir()
        runtime_path.write_bytes(RUNTIME_PATH.read_bytes())
        cls.runtime = MODULE.load_candidate_runtime(runtime_path)
        MODULE.git(cls.template_repo, "init", "-q", "-b", "feature")
        MODULE.git(cls.template_repo, "config", "user.name", "Fixture")
        MODULE.git(
            cls.template_repo,
            "config",
            "user.email",
            "fixture@example.invalid",
        )
        MODULE.git(cls.template_repo, "config", "commit.gpgsign", "false")
        MODULE.git(cls.template_repo, "config", "core.autocrlf", "false")
        MODULE.git(
            cls.template_repo,
            "remote",
            "add",
            "origin",
            "https://github.com/owner/repo.git",
        )
        (cls.template_repo / "example.txt").write_text(
            "before\n",
            encoding="utf-8",
        )
        MODULE.git(cls.template_repo, "add", "example.txt")
        MODULE.git(cls.template_repo, "commit", "-q", "-m", "Source")
        cls.head = MODULE.git(cls.template_repo, "rev-parse", "HEAD")
        MODULE.git(cls.template_repo, "checkout", "-q", "--detach")

    @classmethod
    def tearDownClass(cls):
        cls.template.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.repo = self.directory / "source"
        shutil.copytree(self.template_repo, self.repo)
        self.runtime = type(self).runtime
        self.head = type(self).head
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

    def repository(self):
        repository = MODULE.candidate_git_repository(self.runtime)
        repository.repository_name = mock.Mock(return_value="owner/repo")
        return repository

    def candidate(
        self, *, fixed=True, decision=None, extra_code=False,
        artifact=True, decision_artifact=True, advisory=None, code_path="example.txt",
    ):
        self.git("checkout", "-q", "-b", "generated")
        code = []
        if fixed:
            path = self.repo / code_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("after\n", encoding="utf-8")
            self.git("add", code_path)
            self.git("commit", "-q", "-m", "Handle fixture input")
            code.append(self.git("rev-parse", "HEAD"))
        if extra_code:
            (self.repo / "example.txt").write_text("corrected\n", encoding="utf-8")
            self.git("add", "example.txt")
            self.git("commit", "-q", "-m", "Correct fixture input")
            code.append(self.git("rev-parse", "HEAD"))
        if decision is None:
            item = {
                "finding_id": MODULE.decision_finding_id("fresh-run", 0),
                "disposition": "fixed" if fixed else "no_change",
            }
            if not fixed:
                item.update(reason="Already handled.", proposed_reply="Already handled.")
            decision = self.decisions(item)
        if artifact:
            path = self.repo / (
                MODULE.HOSTED_DECISION_PATH if decision_artifact
                else ".github/agent-task-output/other.json"
            )
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(decision), encoding="utf-8")
            self.git("add", str(path))
            if advisory is not None:
                report = self.repo / self.runtime.OUTPUT_REPORT_PATH
                report.write_bytes(advisory)
                self.git("add", str(report))
            self.git("commit", "-q", "-m", "Review decisions")
        generated_head = self.git("rev-parse", "HEAD")
        code_tip = code[-1] if code else self.head
        self.git("checkout", "-q", "--detach", self.head)
        snapshot = self.runtime.PullRequestSnapshot(
            state="OPEN", cross_repository=False,
            **MODULE.expected_cloud_pull_request(self.preflight),
        )
        options = self.runtime.Options(
            report=False, model="gpt-5.6-sol", prompt=self.prompt,
            apply_with_report=True, policy=MODULE.HOSTED_DECISION_POLICY,
            pull_request=self.runtime.PrReference(7, "owner/repo", "owner/repo#7"),
            result_file=self.result_path, prompt_file=self.prompt_path,
        )
        submitted = self.runtime.task_payload(
            options,
            report_path=self.runtime.OUTPUT_REPORT_PATH, pull_request=snapshot,
        )["prompt"]
        timestamps = {
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:01:00Z",
            "completed_at": "2026-01-01T00:01:00Z",
        }
        task = {
            "id": "task-fixture", "state": "completed", **timestamps,
            "repository": {"id": 1, "full_name": "owner/repo"},
            "owner": {"login": "owner", "id": 2},
            "artifacts": [{
                "type": "branch", "provider": "github",
                "data": {"head_ref": "generated", "base_ref": "feature"},
            }],
            "sessions": [{
                "id": "session-fixture", "task_id": "task-fixture",
                "state": "completed", **timestamps,
                "model": "sweagent-capi:gpt-5.6-sol", "prompt": submitted,
                "base_ref": "feature", "head_ref": "generated",
                "repository": {"id": 1, "full_name": "owner/repo"},
                "owner": {"login": "owner", "id": 2},
            }],
        }
        repository = mock.Mock(wraps=MODULE.candidate_git_repository(self.runtime))
        repository.snapshot.return_value = self.runtime.WorktreeSnapshot(
            self.repo, "owner/repo", "origin", None, self.head,
        )
        repository.root.return_value = self.repo
        repository.fetch_pr_inputs.return_value = {}
        repository.align_to_pr.return_value = repository.snapshot.return_value
        repository.fetch_generated.return_value = "generated"
        api = mock.Mock()
        api.last_response_sha256 = "a" * 64
        result = self.runtime.ResultEnvelope()
        with (
            mock.patch.object(self.runtime, "GitRepository", return_value=repository),
            mock.patch.object(self.runtime, "ApiClient", return_value=api),
            mock.patch.object(
                self.runtime, "repository_base",
                return_value=SimpleNamespace(branch="main", sha=self.head),
            ),
            mock.patch.object(self.runtime, "resolve_pull_request", return_value=snapshot),
            mock.patch.object(self.runtime, "validate_policy_before_post"),
            mock.patch.object(self.runtime, "validate_policy_before_mutation"),
            mock.patch.object(self.runtime, "start_task", return_value=task),
            mock.patch.object(self.runtime, "monitor_task", return_value=task),
        ):
            self.assertEqual(0, self.runtime.execute(
                options, cwd=self.repo, result=result,
                uuid_factory=lambda: "dispatcher-fixture",
                stdout=io.StringIO(), stderr=io.StringIO(),
            ))
        repository.fast_forward.assert_not_called()
        api.request_json.assert_not_called()
        self.result = result.as_dict()
        self.assertEqual(code, self.result["generated"]["commits"])
        self.assertEqual(generated_head, self.result["generated"]["head_sha"])
        self.assertEqual(code_tip, self.result["candidate"]["generated"]["code_tip_sha"])
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))
        return code_tip

    def decisions(self, *items):
        return {"schema": MODULE.HOSTED_DECISION_REPORT_SCHEMA, "decisions": list(items)}

    def fixed(self, position=0):
        return {
            "finding_id": MODULE.decision_finding_id("fresh-run", position),
            "disposition": "fixed",
        }

    def dispatch(self, command, **kwargs):
        self.commands.append((command, kwargs))
        self.result_path.write_text(json.dumps(self.result), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    def run_worker(self):
        with (
            mock.patch.object(MODULE, "github_decision_fingerprint", return_value=self.github),
            mock.patch.object(MODULE, "run_owned_local_worker", side_effect=self.dispatch),
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
        with mock.patch.object(
            MODULE, "candidate_git_repository", return_value=self.repository()
        ):
            MODULE.apply_verified_import(
                self.repo,
                helper=RUNTIME_PATH,
                requested_model="gpt-5.6-sol",
                prompt=self.prompt,
                result_path=self.result_path,
                result_sha256=MODULE.sha256_file(self.result_path),
                report_content=bundle["report_content"],
                preflight=self.preflight,
                remote=bundle["remote"],
            )
        self.assertEqual(tip, self.git("rev-parse", "HEAD"))
        self.assertEqual("", MODULE.local_identity(self.repo)["branch"])
        self.assertFalse((self.repo / MODULE.HOSTED_DECISION_PATH).exists())
        self.assertEqual("", self.git("status", "--porcelain"))

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

    def test_version_9_prompt_cannot_start_a_fresh_dispatch(self):
        self.prompt_path.write_text(
            "Copilot Review Loop hosted worker prompt version 10.\n\n"
            "Put all warranted fixes in exactly one single-parent code commit.\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "retained requests cannot be replayed"):
            self.run_worker()
        self.assertEqual([], self.commands)
        self.assertFalse(self.result_path.exists())
        self.assertEqual(self.before_source, MODULE.local_source_fingerprint(self.repo))

    def test_mapped_code_history_imports_exact_tip_and_all_commits(self):
        tip = self.candidate(extra_code=True)
        bundle = self.run_worker()
        commits = self.result["generated"]["commits"]
        self.assertEqual(2, len(commits))
        self.assertEqual(commits, bundle["remote"]["commits"])
        report = bundle["report"]
        self.assertEqual(MODULE.INDEXED_COPILOT_REVIEW_REPORT_SCHEMA, report["schema"])
        fixes = report["comments"][0]["fixes"]
        self.assertEqual(commits, [fix["commit"] for fix in fixes])
        for fix in fixes:
            self.assertEqual(bundle["paths_by_commit"][fix["commit"]], fix["changed_paths"])
        fields = MODULE.review_comment_commit_fields(report["comments"][0])
        self.assertEqual({"commits": commits}, fields)
        reply = MODULE.reply_body({**fields, "reply": report["comments"][0]["reply"]})
        for commit in commits:
            self.assertIn(commit, reply)
        tree = self.git("rev-parse", f"{tip}^{{tree}}")
        with mock.patch.object(
            MODULE, "candidate_git_repository", return_value=self.repository()
        ):
            MODULE.apply_verified_import(
                self.repo,
                helper=RUNTIME_PATH,
                requested_model="gpt-5.6-sol",
                prompt=self.prompt,
                result_path=self.result_path,
                result_sha256=MODULE.sha256_file(self.result_path),
                report_content=bundle["report_content"],
                preflight=self.preflight,
                remote=bundle["remote"],
            )
        self.assertEqual(tip, self.git("rev-parse", "HEAD"))
        self.assertEqual(tree, self.git("rev-parse", "HEAD^{tree}"))
        self.assertEqual(commits, self.git("rev-list", "--reverse", f"{self.head}..HEAD").splitlines())
        self.assertFalse((self.repo / MODULE.HOSTED_DECISION_PATH).exists())
        self.assertEqual("", self.git("status", "--porcelain"))

    def test_controller_publishes_and_retains_every_mapped_commit_once(self):
        self.preflight["pr"].update(head_owner="owner", head_repo="repo")
        tip = self.candidate(extra_code=True)
        commits = self.result["generated"]["commits"]
        args = MODULE.build_parser().parse_args(["agent-task", "owner/repo#7"])
        args.repo_root = str(self.repo)
        args.state = str(self.directory / "state.json")
        args.preserve_artifacts = True
        published = self.head
        pushes = []
        original_run = MODULE.run
        emitted = []
        candidate_repository = self.repository()

        def run(command, **kwargs):
            nonlocal published
            if "push" in command:
                pushes.append(command)
                published = self.git("rev-parse", "HEAD")
                return subprocess.CompletedProcess(command, 0, "", "")
            return original_run(command, **kwargs)

        def dispatch(command, **_kwargs):
            self.commands.append(command)
            output = Path(command[command.index("--result-file") + 1])
            output.write_text(json.dumps(self.result), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "wait_for_stable_review_preflight", return_value=self.preflight),
            mock.patch.object(MODULE, "discover_cloud_task", return_value=RUNTIME_PATH),
            mock.patch.object(MODULE, "github_decision_fingerprint", return_value=self.github),
            mock.patch.object(MODULE, "run_owned_local_worker", side_effect=dispatch),
            mock.patch.object(MODULE, "run", side_effect=run),
            mock.patch.object(MODULE.secrets, "token_hex", return_value="fresh-run"),
            mock.patch.object(MODULE, "metadata_for", side_effect=lambda _target: {
                **self.preflight["pr"], "head_sha": published,
            }),
            mock.patch.object(MODULE, "remote_head", side_effect=lambda *_args: published),
            mock.patch.object(MODULE, "wait_for_remote_head", side_effect=lambda *_args: published),
            mock.patch.object(MODULE, "find_push_remote", return_value="origin"),
            mock.patch.object(
                MODULE,
                "candidate_git_repository",
                return_value=candidate_repository,
            ),
            mock.patch.object(MODULE, "require_live_comments", return_value=self.preflight["comments"]),
            mock.patch.object(MODULE, "post_missing_replies", return_value={17: 71}) as replies,
            mock.patch.object(MODULE, "resolve_threads") as resolve,
            mock.patch.object(MODULE, "request_copilot", return_value={"status": "requested"}),
            mock.patch.object(MODULE, "verify_publish", return_value={"head_matches": True}),
            mock.patch.object(MODULE, "continue_after_review_request") as continuation,
            mock.patch.object(MODULE, "emit", side_effect=emitted.append),
        ):
            MODULE.command_agent_task(args)
        self.assertEqual(1, len(self.commands))
        self.assertEqual(1, len(pushes))
        self.assertEqual(f"HEAD:{self.preflight['pr']['head_branch']}", pushes[0][-1])
        self.assertIn(
            f"--force-with-lease=refs/heads/feature:{self.head}", pushes[0],
        )
        self.assertEqual(tip, published)
        self.assertEqual(commits, emitted[-1]["commits"])
        state = MODULE.load_state(args._coordinator_state_path)
        self.assertEqual(1, state["iterations"])
        self.assertEqual("completed", state["agent_task"]["status"])
        self.assertEqual(commits, state["agent_task"]["ordered_commits"])
        self.assertEqual(commits, state["queue"]["comments"][0]["commits"])
        self.assertEqual(commits, state["history"][0]["commits"])
        self.assertEqual(
            commits, [fix["commit"] for fix in state["agent_task"]["comments"][0]["fixes"]],
        )
        handled = replies.call_args.args[1][0]
        for commit in commits:
            self.assertIn(commit, MODULE.reply_body(handled))
        resolve.assert_called_once()
        continuation.assert_called_once()
        self.assertEqual(commits, self.git("rev-list", "--reverse", f"{self.head}..HEAD").splitlines())
        self.assertFalse((self.repo / MODULE.HOSTED_DECISION_PATH).exists())

class HostedReviewDecisionTest(unittest.TestCase):
    def setUp(self):
        self.identity = {
            "source": "thread",
            "comment": 17,
            "thread": "PRRT_fixture",
            "review": 29,
            "url": "https://github.com/owner/repo/pull/7#discussion_r17",
            "author": "copilot-pull-request-reviewer[bot]",
            "author_bot_id": "BOT_1",
            "path": "example.txt",
            "current_line": 1,
            "original_line": 1,
            "side": "RIGHT",
            "body_sha256": MODULE.sha256_text("Handle empty input."),
        }
        self.preflight = {
            "comment_identities": [self.identity],
            "pr": {
                "number": 7,
                "repo_name": "owner/repo",
                "head_sha": "a" * 40,
                "base_sha": "b" * 40,
                "head_branch": "feature",
                "base_branch": "main",
                "title": "Fixture review",
                "body": "Synthetic replay only.",
            },
        }
        self.finding = MODULE.decision_finding_id("fresh-run", 0)
        self.commit = "c" * 40

    def normalize(self, *decisions, commits=None, historical=None):
        return MODULE.normalize_decision_review_report(
            {"decisions": list(decisions)},
            request_id="fresh-run",
            preflight=self.preflight,
            remote={"commits": [self.commit] if commits is None else commits},
            fix_commits=[self.commit],
            verified_paths={self.commit: ["example.txt"]},
            historical_findings={} if historical is None else historical,
        )

    def test_fixed_and_no_change_decisions_use_only_verified_evidence(self):
        fixed = self.normalize(
            {"finding_id": self.finding, "disposition": "fixed"}
        )
        self.assertEqual(self.commit, fixed["comments"][0]["commit"])
        self.assertEqual(["example.txt"], fixed["comments"][0]["changed_paths"])

        no_change = self.normalize(
            {
                "finding_id": self.finding,
                "disposition": "no_change",
                "reason": "Already handled.",
                "proposed_reply": "Already handled.",
            },
            commits=[],
        )
        self.assertEqual("no_changes", no_change["outcome"])
        self.assertIsNone(no_change["comments"][0]["commit"])
        self.assertEqual([], no_change["comments"][0]["changed_paths"])

    def test_decisions_reject_missing_duplicate_unknown_and_malformed_findings(self):
        cases = [
            [],
            [
                {"finding_id": self.finding, "disposition": "fixed"},
                {"finding_id": self.finding, "disposition": "fixed"},
            ],
            [{"finding_id": "unknown", "disposition": "fixed"}],
            [{"finding_id": self.finding, "disposition": "rejected"}],
            [
                {
                    "finding_id": self.finding,
                    "disposition": "no_change",
                    "reason": "",
                    "proposed_reply": "No change.",
                }
            ],
            [
                {
                    "finding_id": self.finding,
                    "disposition": "fixed",
                    "commit": self.commit,
                }
            ],
        ]
        for decisions in cases:
            with self.subTest(decisions=decisions), self.assertRaises(
                MODULE.WorkflowError
            ):
                self.normalize(*decisions)

    def test_fixed_decision_requires_a_verified_current_or_historical_commit(self):
        decision = {"finding_id": self.finding, "disposition": "fixed"}
        with self.assertRaisesRegex(
            MODULE.WorkflowError,
            "no coordinator-owned source transition",
        ):
            self.normalize(decision, commits=[])
        report = self.normalize(
            decision,
            commits=[],
            historical={MODULE.decision_finding_key(self.identity): self.commit},
        )
        self.assertEqual(self.commit, report["comments"][0]["commit"])

    def test_candidate_history_is_limited_to_one_coordinator_fix_commit(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "at most one"):
            self.normalize(
                {"finding_id": self.finding, "disposition": "fixed"},
                commits=[self.commit, "d" * 40],
            )


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
