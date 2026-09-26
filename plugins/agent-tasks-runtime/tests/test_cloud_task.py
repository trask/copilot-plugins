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
        live_head = "4" * 40

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
                branch = command[2].rsplit("/", 1)[-1]
                self.assertIn(branch, ("release%2Fnext", "feature"))
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "ref": f"refs/heads/{'release/next' if branch == 'release%2Fnext' else 'feature'}",
                            "object": {"sha": self.live_base if branch == "release%2Fnext" else live_head},
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
        self.assertEqual(live_head, pull_request.head_sha)
        self.assertNotEqual(self.head, pull_request.head_sha)

    def test_fork_reads_its_branch_and_merged_snapshot_keeps_frozen_head(self):
        self.metadata.update(
            state="MERGED",
            headRepository={"nameWithOwner": "fork/repo"},
            headRepositoryOwner={"login": "fork"},
            isCrossRepository=True,
        )
        requested = []

        def runner(command, **_kwargs):
            requested.append(command)
            if command[:3] == ["gh", "pr", "view"]:
                return subprocess.CompletedProcess(command, 0, json.dumps(self.metadata), "")
            if command[:2] == ["git", "check-ref-format"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[:2] == ["gh", "api"]:
                self.assertIn("repos/owner/repo/git/ref/heads/", command[2])
                return subprocess.CompletedProcess(
                    command, 0,
                    json.dumps({"ref": "refs/heads/release/next",
                                "object": {"sha": self.live_base}}), "",
                )
            self.fail(f"unexpected command {command}")

        snapshot = MODULE.resolve_pull_request(
            runner, self.root, "owner/repo",
            MODULE.parse_pr_reference("owner/repo#7"), allow_merged=True,
        )
        self.assertEqual(self.head, snapshot.head_sha)
        self.assertFalse(any("fork/repo" in part for command in requested for part in command))

        self.metadata["state"] = "OPEN"
        def open_runner(command, **_kwargs):
            if command[:2] == ["gh", "api"] and "fork/repo" in command[2]:
                self.assertEqual(command[2], "repos/fork/repo/git/ref/heads/feature")
                return subprocess.CompletedProcess(
                    command, 0,
                    json.dumps({"ref": "refs/heads/feature",
                                "object": {"sha": "4" * 40}}), "",
                )
            return runner(command, **_kwargs)

        snapshot = MODULE.resolve_pull_request(
            open_runner, self.root, "owner/repo",
            MODULE.parse_pr_reference("owner/repo#7"),
        )
        self.assertEqual("4" * 40, snapshot.head_sha)

    def test_fetched_fork_branch_must_match_live_head(self):
        commands = []
        def runner(command, **_kwargs):
            commands.append(command)
            if command[1] == "check-ref-format":
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[1] == "fetch":
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[1] == "rev-parse":
                return subprocess.CompletedProcess(command, 0, self.head, "")
            self.fail(f"unexpected command {command}")
        git = MODULE.GitRepository(runner)
        snapshot = MODULE.WorktreeSnapshot(self.root, "owner/repo", "origin", "feature", self.head)
        pr = MODULE.PullRequestSnapshot(
            7, self.metadata["url"], "OPEN", "owner/repo", "main",
            self.live_base, "fork/repo", "feature", "4" * 40, True,
        )
        with self.assertRaisesRegex(MODULE.CloudError, "moved while it was fetched"):
            git.fetch_pr_inputs(snapshot, "main", pr, "request")
        self.assertIn(
            ["git", "fetch", "--no-tags", "https://github.com/fork/repo.git",
             "+refs/heads/feature:refs/cloud-agent-tasks/request/pr-head"],
            commands,
        )

    def test_actual_branch_moving_after_capture_rejects_completion(self):
        tips = iter(("4" * 40, "5" * 40))
        def runner(command, **_kwargs):
            if command[:3] == ["gh", "pr", "view"]:
                return subprocess.CompletedProcess(command, 0, json.dumps(self.metadata), "")
            if command[:2] == ["git", "check-ref-format"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[:2] == ["gh", "api"]:
                branch = command[2].rsplit("/", 1)[-1]
                return subprocess.CompletedProcess(
                    command, 0,
                    json.dumps({
                        "ref": f"refs/heads/{'release/next' if branch == 'release%2Fnext' else 'feature'}",
                        "object": {"sha": self.live_base if branch == "release%2Fnext" else next(tips)},
                    }), "",
                )
            self.fail(f"unexpected command {command}")

        reference = MODULE.parse_pr_reference("owner/repo#7")
        frozen = MODULE.resolve_pull_request(runner, self.root, "owner/repo", reference)
        current = MODULE.resolve_pull_request(runner, self.root, "owner/repo", reference)
        with self.assertRaisesRegex(MODULE.CloudError, "head_sha"):
            MODULE.require_pr_unchanged(frozen, current, task_completed=True)

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

    def test_assignment_unavailable_is_not_retried_without_admission_proof(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                1,
                "HTTP/2 404 Not Found\r\nContent-Type: application/json\r\n\r\n"
                '{"message":"assignment not found",'
                '"documentation_url":"https://docs.github.com/rest"}',
                "gh: assignment not found (HTTP 404)",
            )

        api = MODULE.ApiClient(runner, max_transient_failures=3)
        with self.assertRaisesRegex(
            MODULE.CloudError,
            "task admission is unknown and the request was not retried",
        ) as raised:
            api.request_json(
                "POST",
                "agents/repos/owner/repo/tasks",
                payload={"prompt": "redacted"},
                expected_status=201,
                operation="start Agent Task",
            )

        self.assertEqual("assignment_unavailable", raised.exception.code)
        self.assertEqual(1, len(calls))


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
    def test_required_candidate_output_reports_verified_task_and_session(self):
        for commits in ([], ["2" * 40]):
            with self.subTest(commits=commits):
                verified = {
                    "artifact_commit": None,
                    "commits": commits,
                    "task": {"id": "task-1"},
                    "completion": {"session": {"id": "session-1"}},
                    "candidate": {"repository": {"name_with_owner": "owner/repo"}},
                }
                with self.assertRaises(MODULE.CloudError) as raised:
                    MODULE.require_output_commit(verified, purpose="audit outcome")
                self.assertEqual("missing_output_commit", raised.exception.code)
                message = str(raised.exception)
                self.assertIn("generated zero commits" if not commits else "generated 1 code commit(s)", message)
                self.assertIn("audit outcome", message)
                self.assertIn("https://github.com/owner/repo/tasks/task-1", message)
                self.assertIn("session-1 (gh agent-task view session-1 --log)", message)
        artifact = {"sha": "3" * 40}
        self.assertIs(
            MODULE.require_output_commit(
                {**verified, "artifact_commit": artifact}, purpose="audit outcome"
            ),
            artifact,
        )

    def test_report_worker_completed_without_commits_identifies_task_and_session(self):
        root = Path("C:/repo")
        base_sha = "1" * 40
        snapshot = MODULE.WorktreeSnapshot(root, "owner/repo", "origin", "feature", base_sha)
        pull_request = MODULE.PullRequestSnapshot(
            7, "https://github.com/owner/repo/pull/7", "OPEN",
            "owner/repo", "main", "4" * 40, "owner/repo", "feature", base_sha, False,
        )
        options = MODULE.Options(
            report=True, model="gpt-5.6-sol", prompt="Describe the PR",
            policy=MODULE.MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR,
        )
        git = mock.Mock()
        git.fetch_generated.return_value = "refs/cloud-agent-tasks/request-1/generated"
        git.ref_sha.return_value = base_sha
        git.cloud_commits.return_value = []
        api = mock.Mock(last_response_sha256="e" * 64)
        final = {
            "id": "task-1",
            "state": "completed",
            "artifacts": [{
                "type": "branch", "provider": "github",
                "data": {"head_ref": "copilot/task-1", "base_ref": "feature"},
            }],
            "sessions": [{"id": "session-1"}],
        }

        for session_url in (None, "https://github.com/owner/repo/pull/7/agent-sessions/session-1"):
            with self.subTest(session_url=session_url):
                if session_url is not None:
                    final["sessions"][0]["html_url"] = session_url
                with (
                    mock.patch.object(MODULE, "validate_policy_before_mutation"),
                    mock.patch.object(
                        MODULE, "validate_fresh_completion",
                        return_value={"session": {"id": "session-1"}},
                    ) as validate,
                    self.assertRaises(MODULE.CloudError) as raised,
                ):
                    MODULE.collect_completed_task(
                        options, git=git, api=api, runner=mock.Mock(), root=root,
                        repository="owner/repo", pull_request=pull_request,
                        snapshot=snapshot, policy_identity=MODULE.LocalIdentity(
                            "feature", base_sha, "", None,
                        ),
                        request_id="request-1", task_id="task-1",
                        submitted_prompt="Managed prompt", final=final,
                        result=MODULE.ResultEnvelope(), stderr=io.StringIO(),
                    )
                self.assertEqual(raised.exception.code, "missing_output_commit")
                self.assertIn("completed Agent Task generated zero commits", str(raised.exception))
                self.assertIn("https://github.com/owner/repo/tasks/task-1", str(raised.exception))
                self.assertIn(
                    session_url or "session-1 (gh agent-task view session-1 --log)",
                    str(raised.exception),
                )
                validate.assert_called_once()
                git.candidate_history.assert_not_called()

    def test_derives_manifest_without_reading_or_applying_worker_output(self):
        self.check_candidate("feature")

    def test_derives_manifest_from_exact_head_detached_checkout(self):
        self.check_candidate(None)

    def test_historical_merged_source_dispatches_on_its_immutable_head(self):
        self.check_candidate("trask-pr-audit-7", historical=True)

    def test_candidate_accepts_only_proven_forward_base_advance(self):
        self.check_candidate("feature", check_base_advance=True)

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

    def check_candidate(
        self,
        branch,
        *,
        drift_phase=None,
        drift_fields=None,
        historical=False,
        managed=False,
        check_base_advance=False,
    ):
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
            "MERGED" if historical else "OPEN",
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
            allow_merged_pr=historical,
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
                        "base_ref": base_sha if historical else "feature",
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
                    "base_ref": base_sha if historical else "feature",
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

        def complete_task(_api, _repository, _initial, progress, _sleep, _report):
            progress.task_id = "task-1"
            progress.last_state = "completed"
            return task

        stderr = io.StringIO()
        runtime = mock.Mock()
        with (
            mock.patch.object(MODULE, "_EXECUTION", runtime if managed else None),
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
            mock.patch.object(
                MODULE, "monitor_task", side_effect=complete_task
            ) as monitor,
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
                stderr=stderr,
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
            self.assertEqual(
                envelope["error"]["code"],
                "stale_pr_head",
                stderr.getvalue(),
            )
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
        self.assertEqual(code, 0, stderr.getvalue())
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
        if managed:
            runtime.record_dispatch.assert_has_calls(
                [
                    mock.call(
                        options.result_file,
                        "request-1",
                        "owner/repo",
                    ),
                    mock.call(
                        options.result_file,
                        "request-1",
                        "owner/repo",
                        {"id": "task-1", "url": None, "state": "queued"},
                    ),
                    mock.call(
                        options.result_file,
                        "request-1",
                        "owner/repo",
                        {"id": "task-1", "state": "completed", "url": None},
                    ),
                ]
            )
        self.assertEqual(
            envelope["attestation"],
            {
                "kind": "dispatcher_candidate",
                "structural_complete": True,
            },
        )
        verified = MODULE.verify_candidate_result(
            envelope, options=options, pull_request=pull_request, root=root, git=repository,
        )
        self.assertEqual(verified["code_tip"], code_commit)
        if check_base_advance:
            frozen_pr = replace(pull_request, base_sha="0" * 40)
            with self.assertRaisesRegex(MODULE.CloudError, "identity or policy"):
                MODULE.verify_current_candidate(
                    envelope, options=options, pull_request=frozen_pr,
                    root=root, git=repository,
                )
            contains = mock.Mock(return_value=True)
            verified = MODULE.verify_current_candidate(
                envelope, options=options, pull_request=frozen_pr,
                root=root, git=repository, base_is_ancestor=contains,
            )
            self.assertEqual(verified["code_tip"], code_commit)
            contains.assert_called_once_with("owner/repo", frozen_pr.base_sha, pull_request.base_sha)
            repository.head.side_effect = [code_commit]
            imported = MODULE.guarded_fast_forward_candidate(
                envelope, options=options, pull_request=frozen_pr,
                root=root, git=repository, base_is_ancestor=contains,
            )
            self.assertEqual(imported["final_local_head"], code_commit)
            repository.fast_forward.assert_called_once_with(snapshot, code_commit)
            self.assertEqual(contains.call_count, 2)
            contains.reset_mock()
            contains.return_value = False
            with self.assertRaisesRegex(MODULE.CloudError, "identity or policy"):
                MODULE.verify_current_candidate(
                    envelope, options=options, pull_request=frozen_pr,
                    root=root, git=repository, base_is_ancestor=contains,
                )
            for field, value in (("head_sha", "9" * 40), ("base_ref", "other")):
                changed = json.loads(json.dumps(envelope))
                changed["pull_request"][field] = value
                contains.reset_mock()
                with self.subTest(field=field), self.assertRaisesRegex(
                    MODULE.CloudError, "identity or policy"
                ):
                    MODULE.verify_current_candidate(
                        changed, options=options, pull_request=frozen_pr,
                        root=root, git=repository, base_is_ancestor=contains,
                    )
                contains.assert_not_called()
            malformed = json.loads(json.dumps(envelope))
            malformed["pull_request"]["base_sha"] = "invalid"
            contains.reset_mock()
            with self.assertRaisesRegex(MODULE.CloudError, "identity or policy"):
                MODULE.verify_current_candidate(
                    malformed, options=options, pull_request=frozen_pr,
                    root=root, git=repository, base_is_ancestor=contains,
                )
            contains.assert_not_called()
            repository.head.side_effect = None
        if historical:
            self.assertEqual(envelope["task"]["base_ref"], base_sha)
            return
        for state in ("CLOSED", "MERGED"):
            with self.subTest(ordinary_source_state=state):
                with self.assertRaisesRegex(MODULE.CloudError, "source state"):
                    MODULE.verify_candidate_result(
                        envelope, options=options, pull_request=replace(pull_request, state=state),
                        root=root, git=repository,
                    )
        for field in ("head_sha", "base_sha"):
            with self.subTest(stale=field):
                with self.assertRaisesRegex(MODULE.CloudError, "identity"):
                    MODULE.verify_candidate_result(
                        envelope, options=options, pull_request=replace(pull_request, **{field: "9" * 40}),
                        root=root, git=repository,
                    )
        wrong_model = json.loads(json.dumps(envelope))
        wrong_model["completion"]["session"]["actual_model"] = "gpt-6-astra"
        with self.assertRaisesRegex(MODULE.CloudError, "completion identity"):
            MODULE.verify_candidate_result(
                wrong_model, options=options, pull_request=pull_request, root=root, git=repository,
            )
        historical_pr = replace(pull_request, state="MERGED")
        historical_options = replace(options, allow_merged_pr=True)
        historical_result = json.loads(json.dumps(envelope))
        historical_result["task"]["base_ref"] = base_sha
        historical_result["candidate"]["base"]["ref"] = base_sha
        historical_result["completion"]["refs"]["base"] = base_sha
        historical_prompt = MODULE.task_payload(
            historical_options, MODULE.OUTPUT_REPORT_PATH, historical_pr,
        )["prompt"]
        prompt_sha = hashlib.sha256(historical_prompt.encode("utf-8")).hexdigest()
        historical_result["completion"]["request"]["prompt_sha256"] = prompt_sha
        historical_result["completion"]["session"]["prompt_sha256"] = prompt_sha
        repository.identity.return_value = MODULE.LocalIdentity("trask-pr-audit-7", base_sha, "", None)
        historical = MODULE.verify_candidate_result(
            historical_result, options=historical_options, pull_request=historical_pr,
            root=root, git=repository,
        )
        self.assertEqual(historical["code_tip"], code_commit)
        contains = mock.Mock(return_value=True)
        with self.assertRaisesRegex(MODULE.CloudError, "identity or policy"):
            MODULE.verify_candidate_result(
                historical_result, options=historical_options,
                pull_request=replace(historical_pr, base_sha="0" * 40),
                root=root, git=repository, base_is_ancestor=contains,
            )
        contains.assert_not_called()
        repository.identity.return_value = MODULE.LocalIdentity("feature", base_sha, "", None)
        with self.assertRaisesRegex(MODULE.CloudError, "frozen audit branch"):
            MODULE.verify_candidate_result(
                historical_result, options=historical_options, pull_request=historical_pr,
                root=root, git=repository,
            )

    def test_managed_candidate_records_its_terminal_dispatch_observation(self):
        self.check_candidate("feature", managed=True)


class DefaultBranchCandidateTest(unittest.TestCase):
    def setUp(self):
        self.root = Path("C:/repo")
        self.source = MODULE.BaseSnapshot("main", "1" * 40)
        self.local_head = "0" * 40
        self.code_tip = "2" * 40
        self.artifact = "3" * 40
        self.git = mock.Mock()
        self.git.snapshot.return_value = MODULE.WorktreeSnapshot(
            self.root, "owner/repo", "origin", "owner-branch", self.local_head,
        )
        self.git.root.return_value = self.root
        self.git.repository_name.return_value = "owner/repo"
        self.git.matching_remote.return_value = "origin"
        self.git.branch.return_value = "owner-branch"
        self.git.head.return_value = self.local_head
        self.git.identity.return_value = MODULE.LocalIdentity(
            "owner-branch", self.local_head, "", None,
        )
        self.git.fetch_generated.return_value = "refs/cloud-agent-tasks/request-1/generated"
        self.git.ref_sha.return_value = self.artifact
        self.git.cloud_commits.return_value = [self.code_tip, self.artifact]
        self.code_metadata = {
            "sha": self.code_tip, "parent_sha": self.source.sha,
            "tree_sha": "a" * 40, "patch_sha256": "b" * 64,
            "changed_paths": ["src/fixture.py"],
        }
        self.artifact_metadata = {
            "sha": self.artifact, "parent_sha": self.code_tip,
            "tree_sha": "c" * 40, "patch_sha256": "d" * 64,
            "changed_paths": [MODULE.OUTPUT_REPORT_PATH],
        }
        self.git.candidate_history.return_value = MODULE.CandidateHistory(
            self.code_tip, (self.code_metadata,), self.artifact_metadata,
        )
        self.api = mock.Mock(last_response_sha256="e" * 64)
        self.result_path = Path("C:/state/result.json")

    def options(self, *, report=False):
        return MODULE.Options(
            report=report, model="gpt-5.6-sol", prompt="Investigate.",
            apply_with_report=not report, default_branch=True,
            result_file=self.result_path, prompt_file=Path("C:/state/prompt.txt"),
            policy=(
                MODULE.DEFAULT_REPORT_POLICY_SELECTOR if report
                else MODULE.DEFAULT_CODE_POLICY_SELECTOR
            ),
        )

    def test_cli_requires_explicit_exclusive_source_and_matching_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt.txt"
            prompt.write_text("Investigate.", encoding="utf-8")
            common = ["--prompt-file", str(prompt),
                      "--result-file", str(Path(directory) / "result.json")]
            for selector, mode in (
                (MODULE.DEFAULT_CODE_POLICY_SELECTOR, "--apply-with-report"),
                (MODULE.DEFAULT_REPORT_POLICY_SELECTOR, "--report"),
            ):
                options = MODULE.parse_args([
                    mode, "--default-branch", *common, "--policy", selector,
                ])
                self.assertTrue(options.default_branch)
                self.assertIsNone(options.pull_request)
                self.assertEqual(selector, options.policy)
                self.assertEqual(
                    MODULE.task_payload(
                        options, MODULE.OUTPUT_REPORT_PATH,
                        default_source=self.source, repository="owner/repo",
                    )["base_ref"], self.source.sha,
                )
                for extra in (
                    ["--pr", "owner/repo#1"],
                    ["--allow-merged-pr"],
                    ["--pipeline-dispatch", "--pipeline-run", "a" * 32],
                    ["--default-branch"],
                ):
                    with self.subTest(selector=selector, extra=extra), self.assertRaises(MODULE.CloudError):
                        MODULE.parse_args([mode, "--default-branch", *extra,
                                           *common, "--policy", selector])
                with self.assertRaises(MODULE.CloudError):
                    MODULE.parse_args([mode, *common, "--policy", selector])
            with self.assertRaises(MODULE.CloudError):
                MODULE.parse_args([
                    "--report", "--default-branch", *common,
                    "--policy", MODULE.MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR,
                ])

    def test_repository_default_snapshot_rejects_wrong_repository_or_ref(self):
        def api(repository_name="owner/repo", branch_ref="refs/heads/main"):
            client = mock.Mock()
            client.request_json.side_effect = [
                {"full_name": repository_name, "default_branch": "main"},
                {"ref": branch_ref, "object": {"sha": self.source.sha}},
            ]
            return client
        self.assertEqual(
            MODULE.repository_base(api(), "owner/repo"), self.source,
        )
        with self.assertRaisesRegex(MODULE.CloudError, "different repository"):
            MODULE.repository_base(api(repository_name="wrong/repo"), "owner/repo")
        with self.assertRaisesRegex(MODULE.CloudError, "default-branch metadata"):
            MODULE.repository_base(api(branch_ref="refs/heads/other"), "owner/repo")

    def test_success_freezes_sha_and_never_applies_or_pushes(self):
        self._check_result(report=False)

    def _check_result(self, *, report=False, no_change=False):
        if no_change:
            self.git.ref_sha.return_value = self.source.sha
            self.git.cloud_commits.return_value = []
            self.git.candidate_history.return_value = MODULE.CandidateHistory(
                self.source.sha, (), None,
            )
        if report:
            self.git.cloud_commits.return_value = [self.artifact]
            self.git.candidate_history.return_value = MODULE.CandidateHistory(
                self.source.sha, (), {
                    **self.artifact_metadata, "parent_sha": self.source.sha,
                },
            )
        result = MODULE.ResultEnvelope()
        options = self.options(report=report)
        with (
            mock.patch.object(MODULE, "_EXECUTION", None),
            mock.patch.object(MODULE, "GitRepository", return_value=self.git),
            mock.patch.object(MODULE, "ApiClient", return_value=self.api),
            mock.patch.object(MODULE, "repository_base",
                              side_effect=[self.source, self.source]) as base,
            mock.patch.object(MODULE, "start_task") as start,
            mock.patch.object(MODULE, "monitor_task") as monitor,
            mock.patch.object(MODULE, "validate_policy_before_post"),
        ):
            payload = MODULE.task_payload(
                options, MODULE.OUTPUT_REPORT_PATH,
                default_source=self.source, repository="owner/repo",
            )
            task = {
                "id": "task-1", "state": "completed",
                "created_at": "2026-09-18T12:00:00Z",
                "completed_at": "2026-09-18T12:03:00Z",
                "repository": {"id": 11, "full_name": "owner/repo"},
                "owner": {"id": 12, "login": "owner"},
                "artifacts": [{"type": "branch", "provider": "github",
                               "data": {"head_ref": "copilot/task-1", "base_ref": self.source.sha}}],
                "sessions": [{
                    "id": "session-1", "task_id": "task-1", "state": "completed",
                    "created_at": "2026-09-18T12:00:01Z",
                    "completed_at": "2026-09-18T12:03:00Z",
                    "model": "sweagent-capi:gpt-5.6-sol",
                    "base_ref": self.source.sha, "head_ref": "copilot/task-1",
                    "repository": {"id": 11, "full_name": "owner/repo"},
                    "owner": {"id": 12, "login": "owner"},
                    "prompt": payload["prompt"],
                }],
            }
            start.return_value = {**task, "state": "queued"}
            monitor.side_effect = lambda api, repo, initial, progress, sleep, stopped: task
            code = MODULE.execute(
                options, cwd=self.root, uuid_factory=lambda: "request-1",
                result=result, stderr=io.StringIO(),
            )
            base.assert_has_calls([mock.call(self.api, "owner/repo")] * 2)
            start.assert_called_once_with(self.api, "owner/repo", payload)
        self.assertEqual(code, 0)
        envelope = result.as_dict()
        self.assertEqual(envelope["source"], {
            "kind": "default_branch", "repository": "owner/repo",
            "ref": "refs/heads/main", "sha": self.source.sha,
        })
        self.assertIsNone(envelope["pull_request"])
        self.assertEqual(envelope["task"]["base_ref"], self.source.sha)
        self.assertEqual(envelope["candidate"]["base"], {
            "ref": self.source.sha, "sha": self.source.sha,
        })
        expected_tip = self.source.sha if report or no_change else self.code_tip
        self.assertEqual(envelope["candidate"]["generated"]["code_tip_sha"], expected_tip)
        self.assertEqual(envelope["generated"]["commits"], [] if report or no_change else [self.code_tip])
        self.assertEqual(envelope["application"], {
            "status": "not_applicable" if report else "not_applied",
            "final_local_head": self.local_head,
        })
        self.git.fast_forward.assert_not_called()
        self.git.align_to_pr.assert_not_called()
        self.git.fetch_default_source.assert_called_once_with(
            self.git.snapshot.return_value if not report else
            MODULE.WorktreeSnapshot(self.root, "owner/repo", "origin",
                                    "owner-branch", self.local_head),
            self.source, "request-1",
        )
        self.assertEqual(
            MODULE.verify_default_branch_candidate(
                envelope, options=options, source=self.source,
                repository="owner/repo", local_head_sha=self.local_head,
                root=self.root, git=self.git,
            )["code_tip"], expected_tip,
        )
        return envelope, options

    def test_no_change_candidate_keeps_source_as_code_tip(self):
        self._check_result(no_change=True)

    def test_report_only_requires_artifact_and_has_no_code_tip(self):
        envelope, options = self._check_result(report=True)
        self.assertEqual(envelope["candidate"]["artifact_commit"]["sha"], self.artifact)
        with self.assertRaisesRegex(MODULE.CloudError, "only default-branch code"):
            MODULE.guarded_fast_forward_default_candidate(
                envelope, options=options, source=self.source,
                repository="owner/repo", executor_head_sha=self.local_head,
                root=self.root, git=self.git,
            )

    def test_frozen_default_moving_before_post_blocks_dispatch(self):
        options = self.options()
        with (
            mock.patch.object(MODULE, "GitRepository", return_value=self.git),
            mock.patch.object(MODULE, "ApiClient", return_value=self.api),
            mock.patch.object(MODULE, "repository_base",
                              side_effect=[self.source, MODULE.BaseSnapshot("main", "9" * 40)]),
            mock.patch.object(MODULE, "start_task") as start,
            mock.patch.object(MODULE, "validate_policy_before_post"),
        ):
            with self.assertRaisesRegex(MODULE.CloudError, "moved before dispatch") as error:
                MODULE.execute(options, cwd=self.root, uuid_factory=lambda: "request-1")
        self.assertEqual(error.exception.code, "stale_default_branch")
        start.assert_not_called()

    def test_fetched_source_mismatch_blocks_dispatch(self):
        commands = []
        def runner(command, **kwargs):
            commands.append(command)
            if command[1] == "check-ref-format" or command[1] == "fetch":
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[1] == "rev-parse":
                return subprocess.CompletedProcess(command, 0, "9" * 40, "")
            self.fail(f"unexpected command {command}")
        git = MODULE.GitRepository(runner)
        snapshot = MODULE.WorktreeSnapshot(
            self.root, "owner/repo", "origin", "owner-branch", self.local_head,
        )
        with mock.patch.object(MODULE, "start_task") as start:
            with self.assertRaisesRegex(MODULE.CloudError, "moved while it was fetched"):
                git.fetch_default_source(snapshot, self.source, "request-1")
        start.assert_not_called()
        self.assertIn(
            ["git", "fetch", "--no-tags", "origin",
             "+refs/heads/main:refs/cloud-agent-tasks/request-1/default"],
            commands,
        )

    def test_mismatched_identity_and_manifest_fail_closed(self):
        envelope, options = self._check_result()
        for section, field, value in (
            ("source", "sha", "9" * 40),
            ("source", "repository", "other/repo"),
            ("task", "base_sha", "9" * 40),
            ("task", "id", "foreign-task"),
            ("completion", "session", {"id": "foreign-session"}),
            ("completion", "refs", {"base": "9" * 40, "generated": "copilot/task-1"}),
            ("candidate", "code_commits", []),
            ("candidate", "artifact_commit", None),
            ("candidate", "generated", {"ref": "copilot/task-1",
                                       "head_sha": self.artifact, "code_tip_sha": self.artifact}),
        ):
            changed = json.loads(json.dumps(envelope))
            changed[section][field] = value
            with self.subTest(section=section, field=field), self.assertRaises(MODULE.CloudError):
                MODULE.verify_default_branch_candidate(
                    changed, options=options, source=self.source,
                    repository="owner/repo", local_head_sha=self.local_head,
                    root=self.root, git=self.git,
                )
        with self.assertRaisesRegex(MODULE.CloudError, "PR consumer"):
            MODULE.verify_candidate_result(
                envelope, options=options,
                pull_request=mock.Mock(), root=self.root, git=self.git,
            )

    def test_guarded_import_targets_only_code_tip_on_frozen_branch(self):
        envelope, options = self._check_result()
        self.git.snapshot.return_value = MODULE.WorktreeSnapshot(
            self.root, "owner/repo", "origin", "owner-branch", self.source.sha,
        )
        self.git.head.return_value = self.code_tip
        imported = MODULE.guarded_fast_forward_default_candidate(
            envelope, options=options, source=self.source,
            repository="owner/repo", executor_head_sha=self.local_head,
            root=self.root, git=self.git,
        )
        self.git.fast_forward.assert_called_once_with(
            self.git.snapshot.return_value, self.code_tip,
        )
        self.assertEqual(imported["final_local_head"], self.code_tip)
        self.assertNotEqual(imported["final_local_head"], self.artifact)
        self.git.fast_forward.reset_mock()
        self.git.snapshot.return_value = replace(
            self.git.snapshot.return_value, head="9" * 40,
        )
        with self.assertRaisesRegex(MODULE.CloudError, "frozen default source"):
            MODULE.guarded_fast_forward_default_candidate(
                envelope, options=options, source=self.source,
                repository="owner/repo", executor_head_sha=self.local_head,
                root=self.root, git=self.git,
            )
        self.git.fast_forward.assert_not_called()


class PipelineStagesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(temporary.cleanup)
        self.files = Path(temporary.name)
        self.prompt = self.files / "prompt.txt"
        self.prompt.write_text("Review and prepare fixes.", encoding="utf-8")
        self.result_path = self.files / "result.json"
        self.run_id = "a" * 32
        self.root = Path("C:/repo")
        self.head = "1" * 40
        self.pull = MODULE.PullRequestSnapshot(
            7, "https://github.com/owner/repo/pull/7", "OPEN",
            "owner/repo", "main", "4" * 40, "owner/repo", "feature",
            self.head, False,
        )
        self.snapshot = MODULE.WorktreeSnapshot(
            self.root, "owner/repo", "origin", "feature", self.head,
        )
        self.repository = mock.Mock()
        self.repository.snapshot.return_value = self.snapshot
        self.repository.root.return_value = self.root
        self.repository.repository_name.return_value = "owner/repo"
        self.repository.head.return_value = self.head
        self.repository.fetch_pr_inputs.return_value = {}
        self.repository.align_to_pr.return_value = self.snapshot
        self.repository.identity.return_value = MODULE.LocalIdentity(
            "feature", self.head, "", None,
        )
        self.repository.fetch_generated.return_value = "refs/cloud-agent-tasks/request-1/generated"
        self.repository.ref_sha.return_value = self.head
        self.repository.cloud_commits.return_value = []
        self.repository.candidate_history.return_value = MODULE.CandidateHistory(
            self.head, (), None,
        )
        self.api = mock.Mock()
        self.api.last_response_sha256 = "e" * 64
        self.initial = {
            "id": "task-1", "state": "queued",
            "created_at": "2026-09-18T12:00:00Z",
            "repository": {"id": 11, "full_name": "owner/repo"},
        }
        options = MODULE.Options(
            report=False, model="gpt-5.6-sol",
            prompt="Review and prepare fixes.",
            pull_request=MODULE.PrReference(7, "owner/repo", "owner/repo#7"),
            apply_with_report=True, result_file=self.result_path,
            policy=MODULE.MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR,
            prompt_file=self.prompt,
        )
        submitted = MODULE.task_payload(options, MODULE.OUTPUT_REPORT_PATH, self.pull)["prompt"]
        self.completed = {
            **self.initial,
            "state": "completed",
            "completed_at": "2026-09-18T12:03:00Z",
            "owner": {"id": 12, "login": "owner"},
            "artifacts": [{
                "type": "branch", "provider": "github",
                "data": {"head_ref": "copilot/task-1", "base_ref": "feature"},
            }],
            "sessions": [{
                "id": "session-1", "task_id": "task-1", "state": "completed",
                "created_at": "2026-09-18T12:00:01Z",
                "completed_at": "2026-09-18T12:03:00Z",
                "model": "sweagent-capi:gpt-5.6-sol",
                "base_ref": "feature", "head_ref": "copilot/task-1",
                "repository": {"id": 11, "full_name": "owner/repo"},
                "owner": {"id": 12, "login": "owner"},
                "prompt": submitted,
            }],
        }
        self.patchers = [
            mock.patch.object(MODULE, "GitRepository", return_value=self.repository),
            mock.patch.object(MODULE, "ApiClient", return_value=self.api),
            mock.patch.object(MODULE, "repository_base", return_value=SimpleNamespace(
                branch="main", sha="4" * 40,
            )),
            mock.patch.object(MODULE, "resolve_pull_request", return_value=self.pull),
            mock.patch.object(MODULE, "validate_policy_before_post"),
            mock.patch.object(MODULE, "_EXECUTION", None),
            mock.patch.dict(MODULE.os.environ, {"COPILOT_AGENT_SESSION_ID": "session-root"}),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def invoke(
        self, stage="dispatch", *, run=None, prompt=None, session=None,
        report=False,
    ):
        if prompt is not None:
            self.prompt.write_text(prompt, encoding="utf-8")
        argv = [
            "--pipeline-" + stage, "--pipeline-run", run or self.run_id,
            "--model", "sol", "--report" if report else "--apply-with-report",
            "--pr", "owner/repo#7",
            "--prompt-file", str(self.prompt), "--result-file", str(self.result_path),
            "--policy", (
                MODULE.MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR
                if report else MODULE.MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR
            ),
        ]
        with mock.patch.dict(
            MODULE.os.environ,
            {"COPILOT_AGENT_SESSION_ID": session or "session-root"},
        ):
            output = io.StringIO()
            code = MODULE.main(
                argv, cwd=self.root, uuid_factory=lambda: "request-1",
                stdout=output, stderr=io.StringIO(),
                sleep=lambda seconds: self.fail(f"unexpected sleep: {seconds}"),
            )
            self.stdout_result = output.getvalue()
            return code

    def result(self):
        if self.result_path.exists():
            return json.loads(self.result_path.read_text(encoding="utf-8"))
        return json.loads(self.stdout_result)

    def test_dispatch_reserves_confirmed_task_and_rejects_duplicate_post(self):
        self.api._request_once.return_value = self.initial
        self.assertEqual(0, self.invoke())
        pending = self.result()
        self.assertEqual("pending", pending["status"])
        self.assertEqual("task-1", pending["task"]["id"])
        self.assertIsNone(pending["completion"])
        self.assertEqual(self.run_id, pending["pipeline"]["run_id"])
        self.assertFalse(self.result_path.exists())
        self.assertEqual(1, len(self.stdout_result.splitlines()))
        checkpoint = MODULE.load_pipeline_checkpoint(
            MODULE.pipeline_checkpoint_path(self.result_path)
        )
        self.assertEqual("session-root", checkpoint["identity"]["session_id"])
        self.assertEqual(self.run_id, checkpoint["identity"]["run_id"])
        self.assertEqual("request-1", checkpoint["request_id"])
        self.assertEqual("task-1", checkpoint["task_id"])
        self.assertEqual(2, self.invoke())
        self.assertEqual(
            "pipeline_duplicate_dispatch", self.result()["error"]["code"]
        )
        self.assertFalse(MODULE._PIPELINE_MODE.get())
        self.assertFalse(self.result_path.exists())
        self.assertEqual(1, self.api._request_once.call_count)
        self.assertEqual("POST", self.api._request_once.call_args.args[0])
        self.assertEqual(300, self.api._request_once.call_args.kwargs["timeout"])

    def test_report_policy_dispatch_and_active_observation_remain_bounded(self):
        self.api._request_once.return_value = self.initial
        self.assertEqual(0, self.invoke(report=True))
        self.assertEqual("not_applicable", self.result()["application"]["status"])
        self.api._request_once.reset_mock()
        self.api._request_once.return_value = {**self.initial, "state": "in_progress"}
        self.assertEqual(0, self.invoke("observe", report=True))
        self.assertEqual("pending", self.result()["status"])
        self.assertFalse(self.result_path.exists())
        self.api._request_once.assert_called_once()
        self.assertEqual(300, self.api._request_once.call_args.kwargs["timeout"])

    def test_report_observation_does_not_recheck_moving_fork_before_get(self):
        cross_repo_pull = replace(
            self.pull, head_repository="contributor/repo", cross_repository=True,
        )
        with mock.patch.object(
            MODULE, "resolve_pull_request", return_value=cross_repo_pull
        ):
            self.api._request_once.return_value = self.initial
            self.assertEqual(0, self.invoke(report=True))
        self.repository.verify_fork_head.assert_called_once()
        self.repository.verify_fork_head.reset_mock()
        self.api._request_once.return_value = {
            **self.initial, "state": "in_progress",
        }
        self.assertEqual(0, self.invoke("observe", report=True))
        self.assertEqual("pending", self.result()["status"])
        self.repository.verify_fork_head.assert_not_called()
        self.assertFalse(self.result_path.exists())

    def test_managed_stages_record_remote_identity_and_observed_states(self):
        runtime = mock.Mock()
        self.api._request_once.return_value = self.initial
        with mock.patch.object(MODULE, "_EXECUTION", runtime):
            self.assertEqual(0, self.invoke())
            self.api._request_once.return_value = {
                **self.initial, "state": "in_progress",
            }
            self.assertEqual(0, self.invoke("observe"))
            self.api._request_once.return_value = self.completed
            self.assertEqual(0, self.invoke("observe"))
        observations = [
            call.args[3]
            for call in runtime.record_dispatch.call_args_list
            if len(call.args) == 4
        ]
        self.assertTrue(runtime.record_dispatch.call_args_list)
        for call in runtime.record_dispatch.call_args_list:
            self.assertEqual(self.result_path, call.args[0])
            self.assertEqual("request-1", call.args[1])
            self.assertEqual("owner/repo", call.args[2])
        self.assertIn({"id": "task-1", "url": None, "state": "queued"}, observations)
        self.assertIn({"id": "task-1", "state": "in_progress", "url": None}, observations)
        self.assertIn({"id": "task-1", "state": "completed", "url": None}, observations)
        self.assertEqual(
            ["pending", "pending"],
            [call.args[0]["status"] for call in runtime.emit.call_args_list],
        )
        self.assertEqual(
            ["task-1", "task-1"],
            [call.args[0]["task"]["id"] for call in runtime.emit.call_args_list],
        )
        self.assertEqual(
            ["request-1", "request-1"],
            [
                call.args[0]["pipeline"]["request_id"]
                for call in runtime.emit.call_args_list
            ],
        )
        self.assertEqual("success", self.result()["status"])

    def test_unknown_post_never_retries_or_allows_observation(self):
        self.api._request_once.side_effect = MODULE.TransientApiError("network lost")
        self.assertEqual(2, self.invoke())
        self.assertEqual("pipeline_dispatch_unknown", self.result()["error"]["code"])
        self.assertFalse(self.result_path.exists())
        self.assertEqual(1, self.api._request_once.call_count)
        self.assertEqual(2, self.invoke())
        self.assertEqual(2, self.invoke("observe"))
        self.assertEqual(1, self.api._request_once.call_count)

    def test_timed_out_post_is_unconfirmed_and_not_retried(self):
        self.api._request_once.side_effect = MODULE.CloudError(
            "gh exceeded its subprocess timeout", "api_timeout"
        )
        self.assertEqual(2, self.invoke())
        self.assertEqual("pipeline_dispatch_unknown", self.result()["error"]["code"])
        self.assertIn("gh exceeded its subprocess timeout", self.result()["error"]["message"])
        self.assertFalse(self.result_path.exists())
        self.assertEqual(300, self.api._request_once.call_args.kwargs["timeout"])
        self.assertEqual(2, self.invoke())
        self.assertEqual(1, self.api._request_once.call_count)

    def test_terminal_post_response_never_seals_as_pending(self):
        runtime = mock.Mock()
        self.api._request_once.return_value = {
            **self.initial, "state": "completed",
        }
        with mock.patch.object(MODULE, "_EXECUTION", runtime):
            self.assertEqual(2, self.invoke())
        self.assertEqual(
            "pipeline_dispatch_not_active", self.result()["error"]["code"]
        )
        self.assertFalse(self.result_path.exists())
        runtime.emit.assert_called_once()
        self.assertEqual("error", runtime.emit.call_args.args[0]["status"])
        self.assertFalse(any(
            len(call.args) == 4 and call.args[3].get("state") == "queued"
            for call in runtime.record_dispatch.call_args_list
        ))
        self.assertEqual(2, self.invoke("observe"))
        self.assertEqual(1, self.api._request_once.call_count)

    def test_unknown_get_fences_later_observation_without_a_final_file(self):
        self.api._request_once.return_value = self.initial
        self.assertEqual(0, self.invoke())
        self.api._request_once.side_effect = MODULE.TransientApiError("network lost")
        self.assertEqual(2, self.invoke("observe"))
        self.assertEqual("api_failure", self.result()["error"]["code"])
        self.assertFalse(self.result_path.exists())
        self.assertEqual(2, self.invoke("observe"))
        self.assertEqual(2, self.api._request_once.call_count)

    def test_observe_rejects_changed_prompt_session_run_and_source_before_get(self):
        self.api._request_once.return_value = self.initial
        self.assertEqual(0, self.invoke())
        self.api._request_once.reset_mock()
        self.assertEqual(2, self.invoke("observe", session="another-session"))
        self.assertEqual(2, self.invoke("observe", run="b" * 32))
        self.assertEqual(2, self.invoke("observe", prompt="A different prompt."))
        self.prompt.write_text("Review and prepare fixes.", encoding="utf-8")
        with mock.patch.object(
            MODULE, "resolve_pull_request",
            return_value=replace(self.pull, head_sha="9" * 40),
        ):
            self.api._request_once.return_value = {
                **self.initial, "state": "in_progress",
            }
            self.assertEqual(0, self.invoke("observe"))
            self.assertEqual("pending", self.result()["status"])
        self.assertEqual(1, self.api._request_once.call_count)
        self.api._request_once.reset_mock()
        self.repository.identity.return_value = MODULE.LocalIdentity(
            "feature", "9" * 40, "", None,
        )
        self.assertEqual(2, self.invoke("observe"))
        self.assertEqual(
            "pipeline_identity_mismatch", self.result()["error"]["code"]
        )
        self.api._request_once.assert_not_called()

    def test_completed_task_attests_frozen_source_for_stage_drift_handling(self):
        self.api._request_once.return_value = self.initial
        self.assertEqual(0, self.invoke())
        self.api._request_once.return_value = self.completed
        with mock.patch.object(
            MODULE, "resolve_pull_request",
            return_value=replace(self.pull, head_sha="9" * 40),
        ):
            self.assertEqual(0, self.invoke("observe"))
        outcome = self.result()
        self.assertEqual("success", outcome["status"])
        self.assertEqual("task-1", outcome["task"]["id"])
        self.assertEqual("completed", outcome["task"]["state"])
        self.assertTrue(self.result_path.exists())
        self.assertEqual(self.head, outcome["pull_request"]["head_sha"])
        self.assertEqual("task-1", outcome["candidate"]["task"]["id"])
        self.assertEqual(
            "session-1", outcome["completion"]["session"]["id"]
        )
        self.repository.require_identity_unchanged.assert_called()
        self.api._request_once.assert_called()

    def test_active_observation_is_one_get_per_call_and_completion_attests(self):
        self.api._request_once.return_value = self.initial
        self.assertEqual(0, self.invoke())
        self.api._request_once.reset_mock()
        self.api._request_once.return_value = {**self.initial, "state": "in_progress"}
        self.assertEqual(0, self.invoke("observe"))
        self.assertEqual("pending", self.result()["status"])
        self.assertEqual("in_progress", self.result()["task"]["state"])
        self.assertFalse(self.result_path.exists())
        self.assertEqual(1, self.api._request_once.call_count)
        self.api._request_once.reset_mock()
        self.assertEqual(0, self.invoke("observe"))
        self.assertEqual("pending", self.result()["status"])
        self.assertEqual(1, self.api._request_once.call_count)
        self.api._request_once.reset_mock()
        self.api._request_once.return_value = self.completed
        self.assertEqual(0, self.invoke("observe"))
        final = self.result()
        self.assertEqual("success", final["status"])
        self.assertTrue(self.result_path.exists())
        self.assertEqual("", self.stdout_result)
        self.assertTrue(final["attestation"]["structural_complete"])
        self.assertEqual("session-1", final["completion"]["session"]["id"])
        self.assertEqual("task-1", final["candidate"]["task"]["id"])
        self.assertEqual(1, self.api._request_once.call_count)
        self.assertEqual("GET", self.api._request_once.call_args.args[0])
        self.assertEqual(2, self.invoke("observe"))
        self.assertEqual(final, self.result())

    def test_observation_rejects_mismatched_remote_task_and_terminal_failure(self):
        self.api._request_once.return_value = self.initial
        self.assertEqual(0, self.invoke())
        self.api._request_once.return_value = {**self.initial, "id": "foreign-task"}
        self.assertEqual(2, self.invoke("observe"))
        self.assertEqual("task_identity_mismatch", self.result()["error"]["code"])
        self.assertEqual(2, self.invoke("observe"))
        self.assertEqual(2, self.api._request_once.call_count)

    def test_active_session_model_mismatch_cannot_be_reported_pending(self):
        self.api._request_once.return_value = self.initial
        self.assertEqual(0, self.invoke())
        self.api._request_once.return_value = {
            **self.initial,
            "state": "in_progress",
            "sessions": [{
                "id": "session-1", "state": "in_progress",
                "created_at": "2026-09-18T12:00:01Z",
                "model": "gpt-6-astra",
            }],
        }
        self.assertEqual(2, self.invoke("observe"))
        self.assertEqual("task_identity_mismatch", self.result()["error"]["code"])

    def test_failed_task_does_not_forge_candidate_completion(self):
        self.api._request_once.return_value = self.initial
        self.assertEqual(0, self.invoke())
        self.api._request_once.return_value = {**self.initial, "state": "failed"}
        self.assertEqual(2, self.invoke("observe"))
        failed = self.result()
        self.assertEqual("task_failed", failed["error"]["code"])
        self.assertEqual("task-1", failed["task"]["id"])
        self.assertIsNone(failed["candidate"])
        self.assertIsNone(failed["completion"])

    def test_interrupt_during_dispatch_leaves_non_adoptable_checkpoint(self):
        self.api._request_once.side_effect = KeyboardInterrupt()
        self.assertEqual(130, self.invoke())
        self.assertEqual("interrupted", self.result()["status"])
        self.assertFalse(self.result_path.exists())
        self.assertEqual(2, self.invoke("observe"))
        self.assertEqual(2, self.invoke())
        self.assertEqual(1, self.api._request_once.call_count)

    def test_interrupt_during_observation_is_not_adopted_by_later_call(self):
        self.api._request_once.return_value = self.initial
        self.assertEqual(0, self.invoke())
        self.api._request_once.side_effect = KeyboardInterrupt()
        self.assertEqual(130, self.invoke("observe"))
        self.assertEqual("interrupted", self.result()["status"])
        self.assertFalse(self.result_path.exists())
        self.assertEqual(2, self.invoke("observe"))
        self.assertEqual(2, self.api._request_once.call_count)

    def test_bounded_processes_keep_windows_no_window_and_independent_timeouts(self):
        self.assertEqual(300, MODULE.PIPELINE_PROCESS_TIMEOUT_SECONDS)
        elapsed = [0]
        calls = []

        def runner(command, **kwargs):
            calls.append(kwargs)
            elapsed[0] += 90
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch.object(MODULE.os, "name", "nt"),
            mock.patch.object(MODULE, "time", SimpleNamespace(monotonic=lambda: elapsed[0])),
        ):
            token = MODULE._PIPELINE_MODE.set(True)
            try:
                for command in (["git", "status"], ["gh", "pr", "view"],
                                ["gh", "api", "--method", "POST"],
                                ["gh", "api", "--method", "GET"]):
                    MODULE.run_process(
                        runner, command,
                        timeout=300 if command[-1] in ("POST", "GET") else None,
                    )
                self.assertGreater(elapsed[0], 300)
            finally:
                MODULE._PIPELINE_MODE.reset(token)
        self.assertEqual([300] * 4, [call["timeout"] for call in calls])
        self.assertTrue(all(
            call["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            for call in calls
        ))
        MODULE.run_process(runner, ["git", "status"])
        self.assertNotIn("timeout", calls[-1])

    def test_pipeline_timeout_is_scoped_to_its_invocation(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess(["git"], 0, "", ""))
        token = MODULE._PIPELINE_MODE.set(True)
        try:
            MODULE.run_process(runner, ["git", "status"])
            self.assertEqual(300, runner.call_args.kwargs["timeout"])
        finally:
            MODULE._PIPELINE_MODE.reset(token)
        MODULE.run_process(runner, ["git", "status"])
        self.assertNotIn("timeout", runner.call_args.kwargs)

    def test_git_timeout_reports_a_process_failure(self):
        command = ["git", "fetch"]
        runner = mock.Mock(side_effect=subprocess.TimeoutExpired(command, 300))
        token = MODULE._PIPELINE_MODE.set(True)
        try:
            with self.assertRaisesRegex(MODULE.CloudError, "git exceeded its subprocess timeout") as raised:
                MODULE.run_process(runner, command)
        finally:
            MODULE._PIPELINE_MODE.reset(token)
        self.assertEqual("process_timeout", raised.exception.code)
        self.assertEqual(300, runner.call_args.kwargs["timeout"])


class CurrentRuntimeApiTest(unittest.TestCase):
    def options(self, root, policy=MODULE.MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR):
        prompt = root / "prompt.txt"
        prompt.write_text("Review.", encoding="utf-8")
        return MODULE.Options(
            report=policy == MODULE.MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR,
            model="gpt-5.6-sol",
            prompt="Review.",
            pull_request=MODULE.PrReference(1, "owner/repo", "owner/repo#1"),
            apply_with_report=policy == MODULE.MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR,
            result_file=root / "result.json",
            policy=policy,
            prompt_file=prompt,
        )

    def test_credential_example_does_not_block_dispatch_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            repo.mkdir()
            state = root / "state"
            state.mkdir()
            example = "Review gproto+http://user:password@host:8080"
            options = replace(self.options(state), prompt=example)

            MODULE.validate_policy_before_post(options, repo, options.result_file)
            self.assertIn(
                example,
                MODULE.task_payload(options, MODULE.OUTPUT_REPORT_PATH)["prompt"],
            )

    def test_current_cli_contracts_and_removed_compatibility_fail_during_parse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.txt"
            prompt.write_text("Review.", encoding="utf-8")
            common = [
                "--pr", "owner/repo#1",
                "--prompt-file", str(prompt),
                "--result-file", str(root / "result.json"),
            ]
            code = MODULE.parse_args([
                "--apply-with-report",
                *common,
                "--policy", MODULE.MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR,
            ])
            report = MODULE.parse_args([
                "--report",
                *common,
                "--policy",
                MODULE.MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR,
            ])
            self.assertTrue(code.apply_with_report)
            self.assertTrue(report.report)
            self.assertEqual("gpt-5.6-sol", code.model)
            self.assertEqual(
                "gpt-5.6-sol",
                MODULE.parse_args([
                    "--apply-with-report",
                    *common,
                    "--model", "sol",
                    "--policy", MODULE.MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR,
                ]).model,
            )
            with self.assertRaisesRegex(MODULE.CloudError, "unsupported model"):
                MODULE.parse_args([
                    "--apply-with-report",
                    *common,
                    "--model", "gpt-6-sol",
                    "--policy", MODULE.MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR,
                ])
            for option in (
                "--dispatch-only",
                "--monitor-only",
                "--resume-apply-with-report",
                "--task-id",
                "--input-result-file",
            ):
                with self.subTest(option=option), self.assertRaisesRegex(
                    MODULE.CloudError, "removed"
                ):
                    MODULE.parse_args([option])
            with self.assertRaisesRegex(MODULE.CloudError, "unknown policy"):
                MODULE.parse_args([
                    "--apply-with-report",
                    *common,
                    "--policy", "marketplace-agent-apply-report-worker@5",
                ])

        for symbol in (
            "read_apply_result",
            "read_dispatch_result",
            "validate_interrupted_apply_task",
            "MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR",
        ):
            self.assertFalse(hasattr(MODULE, symbol), symbol)

    def test_pipeline_stage_flags_require_one_scoped_run_and_never_accept_task_id(self):
            with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
                root = Path(directory)
                prompt = root / "prompt.txt"
                prompt.write_text("Review.", encoding="utf-8")
                common = [
                    "--apply-with-report", "--pr", "owner/repo#1",
                    "--prompt-file", str(prompt), "--result-file", str(root / "result.json"),
                    "--policy", MODULE.MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR,
                ]
                run = "a" * 32
                self.assertEqual(
                    MODULE.parse_args(["--pipeline-dispatch", "--pipeline-run", run, *common]).pipeline_mode,
                    "dispatch",
                )
                self.assertEqual(
                    MODULE.parse_args(["--pipeline-observe", "--pipeline-run", run, *common]).pipeline_mode,
                    "observe",
                )
                for prefix in (
                    ["--pipeline-dispatch"],
                    ["--pipeline-run", run],
                    ["--pipeline-dispatch", "--pipeline-observe", "--pipeline-run", run],
                    ["--pipeline-observe", "--pipeline-run", "A" * 32],
                    ["--pipeline-observe", "--pipeline-run", run, "--task-id", "task-1"],
                ):
                    with self.subTest(prefix=prefix), self.assertRaises(MODULE.CloudError):
                        MODULE.parse_args([*prefix, *common])

    def test_current_verifier_api_keeps_the_existing_name(self):
        with mock.patch.object(
            MODULE,
            "verify_candidate_result",
            return_value={"code_tip": "1" * 40},
        ) as verify:
            result = MODULE.verify_current_candidate(
                {},
                options=mock.Mock(),
                pull_request=mock.Mock(),
                root=Path("C:/repo"),
                git=mock.Mock(),
            )
        self.assertEqual("1" * 40, result["code_tip"])
        verify.assert_called_once()

    def test_guarded_import_fast_forwards_only_a_verified_code_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options = self.options(root)
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
            snapshot = MODULE.WorktreeSnapshot(
                root, "owner/repo", "origin", "feature", "1" * 40
            )
            git = mock.Mock()
            git.snapshot.return_value = snapshot
            git.head.return_value = "3" * 40
            with mock.patch.object(
                MODULE,
                "verify_current_candidate",
                return_value={"code_tip": "3" * 40, "commits": ["3" * 40]},
            ):
                imported = MODULE.guarded_fast_forward_candidate(
                    {},
                    options=options,
                    pull_request=pull_request,
                    root=root,
                    git=git,
                )

            git.require_unchanged.assert_called_once_with(snapshot)
            git.fast_forward.assert_called_once_with(snapshot, "3" * 40)
            git.require_clean.assert_called_once_with(root)
            git.require_no_operation.assert_called_once_with(root)
            self.assertEqual("fast_forwarded", imported["application"])
            self.assertEqual("3" * 40, imported["final_local_head"])

            report = replace(
                options,
                report=True,
                apply_with_report=False,
                policy=MODULE.MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR,
            )
            with self.assertRaisesRegex(MODULE.CloudError, "only code candidates"):
                MODULE.guarded_fast_forward_candidate(
                    {},
                    options=report,
                    pull_request=pull_request,
                    root=root,
                    git=git,
                )


class ManagedResultPersistenceTest(unittest.TestCase):
    def test_pinned_execution_runtime_digest_matches_shared_source(self):
        source = SCRIPT.with_name("execution.py")
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).hexdigest(),
            MODULE.EXECUTION_SHA256,
        )

    def test_unexpected_failure_writes_terminal_result(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.json"
            options = SimpleNamespace(result_file=result_path, pipeline_mode=None)
            with (
                mock.patch.object(MODULE, "parse_args", return_value=options),
                mock.patch.object(
                    MODULE, "execute", side_effect=RuntimeError("helper failed")
                ),
            ):
                code = MODULE.main(
                    ["--result-file", str(result_path)],
                    stderr=io.StringIO(),
                )

            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(2, code)
            self.assertEqual("error", result["status"])
            self.assertEqual("unexpected_helper_error", result["error"]["code"])

    def test_runtime_bootstrap_failure_writes_terminal_result(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.json"
            with (
                mock.patch.dict(
                    MODULE.os.environ,
                    {"TRASK_EXECUTION_PARENT": str(Path(directory) / "parent.json")},
                    clear=True,
                ),
                mock.patch.object(
                    MODULE.sys,
                    "argv",
                    ["cloud_task.py", "--result-file", str(result_path)],
                ),
                mock.patch.object(
                    MODULE,
                    "_load_execution",
                    side_effect=RuntimeError("runtime unavailable"),
                ),
            ):
                code = MODULE.execution_main()

            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(2, code)
            self.assertEqual(
                "execution_runtime_unavailable", result["error"]["code"]
            )

    def test_pipeline_bootstrap_failure_does_not_create_final_result(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            result_path = Path(directory) / "result.json"
            output = io.StringIO()
            with (
                mock.patch.dict(
                    MODULE.os.environ,
                    {"TRASK_EXECUTION_PARENT": str(Path(directory) / "parent.json")},
                    clear=True,
                ),
                mock.patch.object(
                    MODULE.sys,
                    "argv",
                    [
                        "cloud_task.py", "--pipeline-dispatch",
                        "--pipeline-run", "a" * 32,
                        "--result-file", str(result_path),
                    ],
                ),
                mock.patch.object(MODULE.sys, "stdout", output),
                mock.patch.object(
                    MODULE, "_load_execution",
                    side_effect=RuntimeError("runtime unavailable"),
                ),
            ):
                self.assertEqual(2, MODULE.execution_main())
            self.assertFalse(result_path.exists())
            self.assertEqual(
                "execution_runtime_unavailable",
                json.loads(output.getvalue())["error"]["code"],
            )


if __name__ == "__main__":
    unittest.main()
