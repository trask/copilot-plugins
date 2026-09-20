import copy
import importlib.util
import io
import json
import os
from pathlib import Path
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
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.repo = self.directory / "source"
        self.repo.mkdir()
        runtime_path = self.directory / "runtime" / "cloud_task.py"
        runtime_path.parent.mkdir()
        runtime_path.write_bytes(RUNTIME_PATH.read_bytes())
        self.runtime = MODULE.load_candidate_runtime(runtime_path)
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
            else:
                item["fixes"] = [{"commit_index": index} for index in range(1, len(code) + 1)]
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
            mock.patch.object(
                self.runtime, "fetch_report",
                side_effect=AssertionError("advisory report must stay inert"),
            ),
        ):
            self.assertEqual(0, self.runtime.execute(
                options, cwd=self.repo, result=result,
                uuid_factory=lambda: "dispatcher-fixture",
                stdout=io.StringIO(), stderr=io.StringIO(),
            ))
        repository.fast_forward.assert_not_called()
        repository.cherry_pick.assert_not_called()
        api.request_json.assert_not_called()
        self.result = result.as_dict()
        self.assertEqual(code, self.result["generated"]["commits"])
        self.assertEqual(generated_head, self.result["generated"]["head_sha"])
        self.assertEqual(code_tip, self.result["candidate"]["generated"]["code_tip_sha"])
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))
        return code_tip

    def decisions(self, *items):
        return {"schema": MODULE.HOSTED_DECISION_REPORT_SCHEMA, "decisions": list(items)}

    def fixed(self, *indexes, position=0):
        return {
            "finding_id": MODULE.decision_finding_id("fresh-run", position),
            "disposition": "fixed",
            "fixes": [{"commit_index": index} for index in indexes],
        }

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
        self.assertEqual([], bundle["report"]["comments"][0]["fixes"])
        self.assertEqual("No code change.\n\nAlready handled.", MODULE.reply_body({
            **MODULE.review_comment_commit_fields(bundle["report"]["comments"][0]),
            "commit": "stale-reference", "reply": "Already handled.",
        }))

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
        self.candidate(decision=self.decisions())
        with self.assertRaisesRegex(MODULE.WorkflowError, "missing finding IDs"):
            self.run_worker()
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))
        self.assertTrue(self.result_path.is_file())
        self.assertTrue(self.decision_path.is_file())

    def test_version_9_decisions_are_not_upgraded_for_two_code_commits(self):
        decision = json.loads((
            Path(__file__).parent / "fixtures" / "hosted-two-code-commits-decisions.json"
        ).read_text(encoding="utf-8"))
        self.assertEqual(
            MODULE.decision_finding_id("fresh-run", 0),
            decision["decisions"][0]["finding_id"],
        )
        self.candidate(extra_code=True, decision=decision)
        self.assertEqual(2, len(self.result["candidate"]["code_commits"]))
        with self.assertRaisesRegex(
            MODULE.WorkflowError, "schema version 3.*legacy decisions are not accepted",
        ):
            self.run_worker()
        self.assertEqual(1, len(self.commands))
        self.assertEqual(self.result, json.loads(self.result_path.read_text(encoding="utf-8")))
        self.assertEqual(decision, json.loads(self.decision_path.read_text(encoding="utf-8")))
        self.assertFalse(self.canonical_path.exists())
        self.assertEqual(self.before_source, MODULE.local_source_fingerprint(self.repo))

    def test_version_9_prompt_cannot_start_a_fresh_dispatch(self):
        self.prompt_path.write_text(
            "Copilot Review Loop hosted worker prompt version 9.\n\n"
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
        MODULE.apply_verified_import(
            self.repo, result_path=self.result_path,
            result_sha256=MODULE.sha256_file(self.result_path),
            report_content=bundle["report_content"],
            preflight=self.preflight, remote=bundle["remote"],
        )
        self.assertEqual(tip, self.git("rev-parse", "HEAD"))
        self.assertEqual(tree, self.git("rev-parse", "HEAD^{tree}"))
        self.assertEqual(commits, self.git("rev-list", "--reverse", f"{self.head}..HEAD").splitlines())
        self.assertFalse((self.repo / MODULE.HOSTED_DECISION_PATH).exists())
        self.assertEqual("", self.git("status", "--porcelain"))

    def test_multiple_findings_can_explicitly_share_commits(self):
        second = {**self.preflight["comments"][0], "id": 18, "thread_id": "PRRT_second"}
        self.preflight["comments"].append(second)
        self.preflight["comment_identities"].append(MODULE.comment_identity(second))
        self.prompt = MODULE.build_worker_prompt(
            self.preflight, request_id="fresh-run", iteration_allowance=1, prior_history=[],
        )
        self.prompt_path.write_text(self.prompt, encoding="utf-8")
        self.candidate(extra_code=True, decision=self.decisions(
            self.fixed(1, 2, position=1), self.fixed(2),
        ))
        bundle = self.run_worker()
        commits = bundle["remote"]["commits"]
        first, second = bundle["report"]["comments"]
        self.assertEqual([commits[1]], [fix["commit"] for fix in first["fixes"]])
        self.assertEqual(commits, [fix["commit"] for fix in second["fixes"]])

    def test_controller_publishes_and_retains_every_mapped_commit_once(self):
        self.preflight["pr"].update(head_owner="owner", head_repo="repo")
        tip = self.candidate(extra_code=True)
        commits = self.result["generated"]["commits"]
        args = MODULE.build_parser().parse_args([
            "agent-task", "owner/repo#7", "--model", "sol",
            "--repo-root", str(self.repo), "--state", str(self.directory / "state.json"),
            "--preserve-artifacts",
        ])
        published = self.head
        pushes = []
        original_run = MODULE.run
        emitted = []

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
            mock.patch.object(MODULE, "run_local_decision_worker", side_effect=AssertionError("local fallback")),
            mock.patch.object(MODULE, "run", side_effect=run),
            mock.patch.object(MODULE.secrets, "token_hex", return_value="fresh-run"),
            mock.patch.object(MODULE, "metadata_for", side_effect=lambda _target: {
                **self.preflight["pr"], "head_sha": published,
            }),
            mock.patch.object(MODULE, "remote_head", side_effect=lambda *_args: published),
            mock.patch.object(MODULE, "wait_for_remote_head", side_effect=lambda *_args: published),
            mock.patch.object(MODULE, "find_push_remote", return_value="origin"),
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
        self.assertEqual(0, state["agent_task"]["resume_attempts"])
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

    def test_missing_commit_mapping_does_not_infer_a_commit(self):
        self.candidate(decision=self.decisions({
            "finding_id": MODULE.decision_finding_id("fresh-run", 0), "disposition": "fixed",
        }))
        with self.assertRaisesRegex(MODULE.WorkflowError, "decision fields"):
            self.run_worker()
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_unaccounted_code_commit_is_rejected(self):
        self.candidate(extra_code=True, decision=self.decisions(self.fixed(1)))
        with self.assertRaisesRegex(MODULE.WorkflowError, "account for every fix commit"):
            self.run_worker()
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_fixed_decision_cannot_select_source_without_candidate_code(self):
        self.candidate(fixed=False, decision=self.decisions(self.fixed(1)))
        with self.assertRaisesRegex(MODULE.WorkflowError, "invalid commit index"):
            self.run_worker()
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_invalid_index_mappings_fail_closed(self):
        self.candidate(extra_code=True)
        remote = {"commits": self.result["generated"]["commits"], "requires_apply": True}
        paths = {item["sha"]: item["changed_paths"] for item in self.result["candidate"]["code_commits"]}
        for indexes in ([], [0], [-1], [True], [False], [1.0], ["1"], [3], [1, 1], [2, 1]):
            with self.subTest(indexes=indexes), self.assertRaises(MODULE.WorkflowError):
                MODULE.validate_copilot_review_report(
                    json.dumps(self.decisions(self.fixed(*indexes))),
                    request_id="fresh-run", preflight=self.preflight, remote=remote,
                    paths_by_commit=paths, hosted_decisions=True,
                )
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_model_authored_shas_and_malformed_fix_objects_are_rejected(self):
        self.candidate()
        commits = self.result["generated"]["commits"]
        paths = {item["sha"]: item["changed_paths"] for item in self.result["candidate"]["code_commits"]}
        for fixes in (
            None, {}, [1], [{"commit": commits[0]}],
            [{"commit_index": 1, "commit": commits[0]}],
            [{"commit_index": 1, "changed_paths": ["example.txt"]}],
        ):
            decision = {**self.fixed(1), "fixes": fixes}
            with self.subTest(fixes=fixes), self.assertRaises(MODULE.WorkflowError):
                MODULE.validate_copilot_review_report(
                    json.dumps(self.decisions(decision)), request_id="fresh-run",
                    preflight=self.preflight, remote={"commits": commits, "requires_apply": True},
                    paths_by_commit=paths, hosted_decisions=True,
                )
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_no_change_or_rejected_decisions_cannot_claim_code(self):
        self.candidate()
        commits = self.result["generated"]["commits"]
        paths = {item["sha"]: item["changed_paths"] for item in self.result["candidate"]["code_commits"]}
        for disposition in ("no_change", "rejected", None, []):
            decision = {**self.fixed(1), "disposition": disposition}
            with self.subTest(disposition=disposition), self.assertRaises(MODULE.WorkflowError):
                MODULE.validate_copilot_review_report(
                    json.dumps(self.decisions(decision)), request_id="fresh-run",
                    preflight=self.preflight, remote={"commits": commits, "requires_apply": True},
                    paths_by_commit=paths, hosted_decisions=True,
                )
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_unknown_duplicate_or_missing_findings_fail_closed(self):
        self.candidate(extra_code=True)
        remote = {"commits": self.result["generated"]["commits"], "requires_apply": True}
        paths = {item["sha"]: item["changed_paths"] for item in self.result["candidate"]["code_commits"]}
        for decisions in (
            self.decisions(), self.decisions(self.fixed(1, 2, position=1)),
            self.decisions(self.fixed(1, 2), self.fixed(1, 2)),
        ):
            with self.subTest(decisions=decisions), self.assertRaises(MODULE.WorkflowError):
                MODULE.validate_copilot_review_report(
                    json.dumps(decisions), request_id="fresh-run", preflight=self.preflight,
                    remote=remote, paths_by_commit=paths, hosted_decisions=True,
                )
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_missing_workflow_artifact_is_not_a_runtime_manifest_failure(self):
        self.candidate(artifact=False)
        with self.assertRaisesRegex(MODULE.WorkflowError, "requires a final decisions artifact"):
            self.run_worker()
        self.assertFalse(self.decision_path.exists())
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_unrelated_output_cannot_replace_required_decisions(self):
        self.candidate(decision_artifact=False)
        with self.assertRaisesRegex(
            MODULE.WorkflowError, "final artifact commit must include .*review-decisions.json",
        ):
            self.run_worker()
        self.assertFalse(self.decision_path.exists())
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_reserved_agent_paths_cannot_be_imported_as_code(self):
        self.candidate(code_path=".github/agent-task-private/metadata.json")
        with self.assertRaisesRegex(MODULE.WorkflowError, "code commits contain reserved"):
            self.run_worker()
        self.assertFalse(self.decision_path.exists())
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_generated_commit_list_must_match_verified_history(self):
        self.candidate()
        self.result["generated"]["commits"] = []
        with self.assertRaisesRegex(MODULE.WorkflowError, "generated commits do not match"):
            self.run_worker()
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_manifest_drift_rejects_mapped_multicommit_candidate(self):
        self.candidate(extra_code=True)
        self.result["candidate"]["code_commits"][0]["patch_sha256"] = "f" * 64
        with self.assertRaisesRegex(MODULE.WorkflowError, "manifest does not match verified history"):
            self.run_worker()
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_canonical_mapping_cannot_drop_reorder_or_forge_commit_paths(self):
        self.candidate(extra_code=True)
        bundle = self.run_worker()
        report = bundle["report"]
        self.assertEqual(report, MODULE.validate_copilot_review_report(
            bundle["report_content"], request_id="fresh-run", preflight=self.preflight,
            remote=bundle["remote"], paths_by_commit=bundle["paths_by_commit"],
        ))
        fixes = report["comments"][0]["fixes"]
        for altered in (
            [], [fixes[0]], list(reversed(fixes)), [fixes[0], fixes[0], fixes[1]],
            [{**fixes[0], "commit": self.result["generated"]["head_sha"]}, fixes[1]],
            [{**fixes[0], "changed_paths": ["unrelated.txt"]}, fixes[1]],
        ):
            tampered = copy.deepcopy(report)
            tampered["comments"][0]["fixes"] = altered
            with self.subTest(altered=altered), self.assertRaises(MODULE.WorkflowError):
                MODULE.validate_copilot_review_report(
                    json.dumps(tampered), request_id="fresh-run", preflight=self.preflight,
                    remote=bundle["remote"], paths_by_commit=bundle["paths_by_commit"],
                )
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def append_candidate_paths(self, paths):
        self.git("checkout", "-q", "generated")
        for name in paths:
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("untrusted mutation\n", encoding="utf-8")
            self.git("add", str(path))
        self.git("commit", "-q", "-m", "Untrusted history")
        self.result["generated"]["head_sha"] = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "--detach", self.head)

    def test_code_after_output_commit_is_rejected_before_decisions(self):
        self.candidate()
        self.append_candidate_paths(["example.txt"])
        with self.assertRaisesRegex(MODULE.WorkflowError, "candidate history rejected"):
            self.run_worker()
        self.assertFalse(self.decision_path.exists())
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_mixed_code_and_output_commit_is_rejected_before_decisions(self):
        self.candidate(artifact=False)
        self.append_candidate_paths(["example.txt", MODULE.HOSTED_DECISION_PATH])
        with self.assertRaisesRegex(MODULE.WorkflowError, "mixes candidate code and output"):
            self.run_worker()
        self.assertFalse(self.decision_path.exists())
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_nonlinear_candidate_is_rejected_before_decisions(self):
        self.candidate(artifact=False)
        self.git("checkout", "-q", "-b", "side", self.head)
        (self.repo / "side.txt").write_text("side\n", encoding="utf-8")
        self.git("add", "side.txt")
        self.git("commit", "-q", "-m", "Side")
        self.git("checkout", "-q", "generated")
        self.git("merge", "-q", "--no-ff", "side", "-m", "Merge")
        self.result["generated"]["head_sha"] = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "--detach", self.head)
        with self.assertRaisesRegex(MODULE.WorkflowError, "candidate history rejected"):
            self.run_worker()
        self.assertFalse(self.decision_path.exists())
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_foreign_candidate_history_is_rejected_before_decisions(self):
        self.candidate()
        self.git("checkout", "-q", "--orphan", "foreign")
        self.git("commit", "-q", "-m", "Foreign root")
        self.result["generated"]["head_sha"] = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "--detach", self.head)
        with self.assertRaisesRegex(MODULE.WorkflowError, "candidate history rejected"):
            self.run_worker()
        self.assertFalse(self.decision_path.exists())
        self.assertEqual(self.head, self.git("rev-parse", "HEAD"))

    def test_empty_advisory_report_does_not_invalidate_candidate(self):
        self.candidate(advisory=b"")
        self.assertEqual("fixed", self.run_worker()["report"]["comments"][0]["disposition"])

    def test_non_utf8_advisory_report_does_not_invalidate_candidate(self):
        self.candidate(advisory=b"\xff\x00not Markdown")
        self.assertEqual("fixed", self.run_worker()["report"]["comments"][0]["disposition"])

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

    def test_ccr_v2_body_finding_reaches_hosted_decisions_without_thread_mutation(self):
        self.assert_body_finding_reaches_hosted_decisions(
            "ccr-v2-previously-missed-review.json", -5259532804000
        )

    def test_legacy_review_details_reach_hosted_decisions_without_thread_mutation(self):
        self.assert_body_finding_reaches_hosted_decisions(
            "legacy-review-details-review.json", -5203651790000
        )

    def assert_body_finding_reaches_hosted_decisions(self, fixture, synthetic_id):
        review = json.loads((
            Path(__file__).parent / "fixtures" / fixture
        ).read_text(encoding="utf-8"))
        review["commit_id"] = self.head
        pr = {
            **self.preflight["pr"], "head_owner": "owner", "head_repo": "repo",
            "upstream_owner": "owner", "upstream_repo": "repo",
        }
        with (
            mock.patch.object(MODULE, "metadata_for", return_value=pr),
            mock.patch.object(MODULE, "gh_json", side_effect=[
                {"permissions": {key: True for key in (
                    "admin", "maintain", "push", "triage", "pull"
                )}},
                {"login": "fixture"},
            ]),
            mock.patch.object(MODULE, "remote_head", return_value=self.head),
            mock.patch.object(MODULE, "find_push_remote", return_value="origin"),
            mock.patch.object(MODULE, "fetch_copilot_threads", return_value=([], [])),
            mock.patch.object(MODULE, "fetch_reviews", return_value=[review]),
        ):
            self.preflight = MODULE.agent_task_preflight(
                self.repo, {"owner": "owner", "repo": "repo", "number": 7},
                allow_detached=True,
            )
        self.assertFalse(self.preflight["head_review_clean"])
        comments = self.preflight["comments"]
        self.assertEqual(len(comments), 1)
        self.prompt = MODULE.build_worker_prompt(
            self.preflight, request_id="fresh-run", iteration_allowance=1, prior_history=[]
        )
        self.prompt_path.write_text(self.prompt, encoding="utf-8")
        self.assertIn(json.dumps(comments[0]["body"]), self.prompt)
        self.candidate(fixed=False)
        bundle = self.run_worker()
        decisions = bundle["report"]["comments"]
        self.assertEqual(decisions[0]["id"], synthetic_id)
        self.assertEqual(decisions[0]["source"], "suppressed")
        self.assertEqual(decisions[0]["disposition"], "no_change")
        self.assertIsNone(decisions[0]["thread_id"])
        with (
            mock.patch.object(MODULE, "gh_json", side_effect=AssertionError("mutation")),
            mock.patch.object(MODULE, "graphql", side_effect=AssertionError("mutation")),
            mock.patch.object(
                MODULE, "fetch_review_comments", side_effect=AssertionError("reply")
            ),
        ):
            self.assertEqual(MODULE.post_missing_replies({}, decisions), {})
            MODULE.resolve_threads(decisions)

    def test_no_change_cannot_conceal_candidate_code(self):
        self.candidate(decision=self.decisions({
                "finding_id": MODULE.decision_finding_id("fresh-run", 0),
                "disposition": "no_change", "reason": "No fix", "proposed_reply": "No fix",
        }))
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
