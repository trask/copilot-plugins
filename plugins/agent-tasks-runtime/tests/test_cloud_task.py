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

    def test_historical_merged_source_dispatches_on_its_immutable_head(self):
        self.check_candidate("trask-pr-audit-7", historical=True)

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

    def check_candidate(self, branch, *, drift_phase=None, drift_fields=None, historical=False):
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
        stderr = io.StringIO()
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
        repository.identity.return_value = MODULE.LocalIdentity("feature", base_sha, "", None)
        with self.assertRaisesRegex(MODULE.CloudError, "frozen audit branch"):
            MODULE.verify_candidate_result(
                historical_result, options=historical_options, pull_request=historical_pr,
                root=root, git=repository,
            )


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


if __name__ == "__main__":
    unittest.main()
