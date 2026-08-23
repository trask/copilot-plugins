from contextlib import redirect_stdout
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "pr_pipeline.py"
AGENT = Path(__file__).parents[1] / "agents" / "pr-pipeline.agent.md"
SPEC = importlib.util.spec_from_file_location("pr_pipeline", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

HEAD = "a" * 40
NEXT_HEAD = "b" * 40


def target() -> dict:
    return MODULE.build_target("owner", "repo", 7)


def pull_request(head: str = HEAD, state: str = "OPEN") -> dict:
    return {
        **target(),
        "title": "Add a thing",
        "state": state,
        "is_draft": True,
        "head_branch": "feature",
        "base_branch": "main",
        "head_sha": head,
    }


def clear_stage(stage: str, head: str = HEAD) -> dict:
    return {
        "stage": stage,
        "clear": True,
        "clear_at_head_sha": head,
        "outcome": "cleared",
        "reason": None,
        "installed": True,
        "status_state": "state.json",
        "status": {},
    }


def uncleared_stage(stage: str, outcome: str | None = "carried") -> dict:
    return {
        "stage": stage,
        "clear": False,
        "clear_at_head_sha": None,
        "outcome": outcome,
        "reason": outcome or "not_cleared",
        "installed": True,
        "status_state": "state.json",
        "status": {},
    }


class TargetTest(unittest.TestCase):
    def test_parses_supported_targets(self):
        expected = target()
        self.assertEqual(expected, MODULE.parse_target(expected["pr_url"]))
        self.assertEqual(expected, MODULE.parse_target("owner/repo#7"))
        self.assertEqual(expected, MODULE.parse_target("#7", "owner/repo"))

    def test_reads_commit_links_for_the_pull_request(self):
        with mock.patch.object(
            MODULE,
            "gh_json",
            return_value={
                "commits": [
                    {
                        "oid": HEAD,
                        "messageHeadline": "Fix the thing",
                    }
                ]
            },
        ):
            self.assertEqual(
                [
                    {
                        "sha": HEAD,
                        "title": "Fix the thing",
                        "url": f"{target()['pr_url']}/commits/{HEAD}",
                    }
                ],
                MODULE.read_pr_commits(target()),
            )

    def test_marks_rewritten_history_and_lists_replacement_commits(self):
        replacement = {
            "sha": NEXT_HEAD,
            "title": "Rebased commit",
            "url": f"{target()['pr_url']}/commits/{NEXT_HEAD}",
        }
        added, errors, rewritten = MODULE.commits_added(
            {"commits": [{"sha": HEAD, "title": "Old commit"}]},
            {"commits": [replacement]},
        )
        self.assertEqual([replacement], added)
        self.assertEqual([], errors)
        self.assertTrue(rewritten)

    def test_bare_number_needs_repository_context(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "repository context"):
            MODULE.parse_target("7")

    def test_normalizes_msys_copilot_home_on_windows(self):
        self.assertEqual(
            "C:/Users/example/.copilot",
            MODULE.normalize_cli_path(
                "/c/Users/example/.copilot",
                windows=True,
            ),
        )


class StageContractTest(unittest.TestCase):
    def test_stage_order_is_fixed(self):
        self.assertEqual(
            (
                "conflict-fix-loop",
                "copilot-review-loop",
                "self-review-loop",
                "ci-fix-loop",
                "pr-description",
            ),
            MODULE.STAGE_NAMES,
        )

    def test_every_agent_is_plugin_qualified(self):
        for entry in MODULE.STAGES:
            self.assertEqual(f"{entry['plugin']}:{entry['stage']}", entry["agent"])

    def test_each_stage_has_one_head_marker(self):
        self.assertEqual(
            {
                MODULE.STAGE_CONFLICT: ("mergeable_at_head_sha",),
                MODULE.STAGE_SELF_REVIEW: ("review", "clean_at_head_sha"),
                MODULE.STAGE_COPILOT_REVIEW: ("clean_at_head_sha",),
                MODULE.STAGE_CI: ("clean_at_head_sha",),
                MODULE.STAGE_DESCRIPTION: ("validated_head_sha",),
            },
            {entry["stage"]: entry["marker"] for entry in MODULE.STAGES},
        )

    def test_only_self_review_requires_claude(self):
        models = MODULE.stage_models(None)
        self.assertIn("claude", models[MODULE.STAGE_SELF_REVIEW])
        with self.assertRaisesRegex(MODULE.WorkflowError, "requires a claude model"):
            MODULE.stage_models(["self-review-loop=gpt-5.6-sol"])

    def test_pipeline_position_is_one_run_and_two_sweeps(self):
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_CI]
        with mock.patch.object(MODULE, "stage_accepts_pipeline_position", return_value=True):
            arguments = MODULE.pipeline_arguments(entry, "run-1", 2)
        self.assertEqual(
            [
                "--pipeline-run",
                "run-1",
                "--pipeline-iteration",
                "2",
                "--pipeline-max-iterations",
                "2",
            ],
            arguments,
        )


class MarkerTest(unittest.TestCase):
    def status(self, stage: str, payload: dict) -> dict:
        entry = MODULE.STAGE_BY_NAME[stage]
        with mock.patch.object(
            MODULE,
            "read_stage_status",
            return_value={
                "ok": True,
                "installed": True,
                "state": "state.json",
                "payload": {
                    "result": "ready",
                    "stage_outcome": "cleared",
                    **payload,
                },
            },
        ):
            return MODULE.inspect_stage(entry, target(), HEAD)

    def test_reads_every_stage_marker(self):
        payloads = {
            MODULE.STAGE_CONFLICT: {"mergeable_at_head_sha": HEAD},
            MODULE.STAGE_SELF_REVIEW: {
                "review": {"outcome": "clean", "clean_at_head_sha": HEAD}
            },
            MODULE.STAGE_COPILOT_REVIEW: {"clean_at_head_sha": HEAD},
            MODULE.STAGE_CI: {"clean_at_head_sha": HEAD},
            MODULE.STAGE_DESCRIPTION: {"validated_head_sha": HEAD},
        }
        for stage, payload in payloads.items():
            with self.subTest(stage=stage):
                self.assertTrue(self.status(stage, payload)["clear"])

    def test_old_marker_is_not_clear(self):
        result = self.status(
            MODULE.STAGE_DESCRIPTION,
            {"validated_head_sha": NEXT_HEAD, "stage_outcome": "cleared"},
        )
        self.assertFalse(result["clear"])
        self.assertEqual("clearance_is_for_an_older_head", result["reason"])

    def test_cap_is_incomplete_not_blocked(self):
        result = self.status(MODULE.STAGE_CI, {"stage_outcome": "carried"})
        self.assertFalse(result["clear"])
        self.assertEqual("carried", result["reason"])

    def test_preserves_stage_status_details(self):
        escalation = {
            "reason": "unfixable_failure",
            "detail": "library defect",
            "checks": ["check:test"],
        }
        result = self.status(
            MODULE.STAGE_CI,
            {
                "stage_outcome": "escalated",
                "escalation": escalation,
                "counts": {"failed": 1},
            },
        )
        self.assertEqual(escalation, result["status"]["escalation"])
        self.assertEqual({"failed": 1}, result["status"]["counts"])


class SweepTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path("C:/repo")
        self.sync_heads = [HEAD]
        self.clear_at = {stage: None for stage in MODULE.STAGE_NAMES}
        self.launched: list[tuple[str, int]] = []
        self.events: list[dict] = []

        self.patches = [
            mock.patch.object(MODULE, "read_pull_request", side_effect=self.read_pr),
            mock.patch.object(MODULE, "sync_worktree", side_effect=self.sync),
            mock.patch.object(MODULE, "run_stage", side_effect=self.run_stage),
            mock.patch.object(MODULE, "settle_after_stage", side_effect=self.settle),
            mock.patch.object(MODULE, "inspect_stage", side_effect=self.inspect),
            mock.patch.object(MODULE, "inspect_stages", side_effect=self.inspect_all),
            mock.patch.object(
                MODULE, "snapshot_pr_commits", side_effect=self.snapshot_commits
            ),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def read_pr(self, _target):
        return pull_request(self.sync_heads[-1])

    def sync(self, _repo, _target, _pr, *, known_safe_head):
        return {
            "result": "ready",
            "head_sha": self.sync_heads[-1],
            "changed": known_safe_head not in (None, self.sync_heads[-1]),
        }

    def run_stage(
        self, entry, _target, _repo, *, model, effort, run_id, sweep
    ):
        self.launched.append((entry["stage"], sweep))
        self.clear_at[entry["stage"]] = self.sync_heads[-1]
        return {
            "returncode": 0,
            "log_path": f"{sweep}-{entry['stage']}.log",
            "started_at": "start",
            "ended_at": "end",
        }

    def snapshot_commits(self, _target):
        head = self.sync_heads[-1]
        return {
            "commits": [
                {
                    "sha": head,
                    "title": f"Commit {head[0]}",
                    "url": f"https://github.com/owner/repo/pull/7/commits/{head}",
                }
            ]
        }

    def settle(self, _repo, _target, *, started_head_sha):
        return {
            "result": "ready",
            "head_sha": self.sync_heads[-1],
            "changed": self.sync_heads[-1] != started_head_sha,
        }

    def inspect(self, entry, _target, head):
        if self.clear_at[entry["stage"]] == head:
            return clear_stage(entry["stage"], head)
        return uncleared_stage(entry["stage"])

    def inspect_all(self, _target, head):
        return [self.inspect(entry, _target, head) for entry in MODULE.STAGES]

    def execute(self):
        return MODULE.run_pipeline(
            target(),
            self.repo,
            models=MODULE.stage_models(None),
            effort="high",
            report=self.events.append,
        )

    def test_one_sweep_runs_all_stages_in_order(self):
        result = self.execute()
        self.assertEqual("complete", result["result"])
        self.assertEqual(1, result["sweeps"])
        self.assertEqual(
            [(stage, 1) for stage in MODULE.STAGE_NAMES],
            self.launched,
        )

    def test_reports_sweep_and_stage_progress_in_order(self):
        self.execute()
        self.assertEqual(
            [
                "pipeline_started",
                "sweep_started",
                *(["stage_started", "stage_finished"] * len(MODULE.STAGES)),
                "sweep_finished",
            ],
            [event["event"] for event in self.events],
        )
        stage_events = [
            event["stage"]
            for event in self.events
            if event["event"] == "stage_started"
        ]
        self.assertEqual(list(MODULE.STAGE_NAMES), stage_events)

    def test_capped_stage_does_not_block_later_stages(self):
        original = self.run_stage

        def cap_self_review(entry, *args, **kwargs):
            result = original(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_SELF_REVIEW:
                self.clear_at[entry["stage"]] = None
            return result

        MODULE.run_stage.side_effect = cap_self_review
        result = self.execute()
        self.assertEqual("incomplete", result["result"])
        self.assertEqual(1, result["sweeps"])
        self.assertEqual(
            [(stage, 1) for stage in MODULE.STAGE_NAMES],
            self.launched,
        )

    def test_head_change_runs_a_second_sweep_for_stale_stages(self):
        first_sweep_calls = 0

        def move_head(entry, *args, **kwargs):
            nonlocal first_sweep_calls
            first_sweep_calls += 1
            if first_sweep_calls == 3:
                self.sync_heads.append(NEXT_HEAD)
                for stage in (MODULE.STAGE_CONFLICT, MODULE.STAGE_COPILOT_REVIEW):
                    self.clear_at[stage] = None
            result = self.run_stage(entry, *args, **kwargs)
            return result

        MODULE.run_stage.side_effect = move_head
        result = self.execute()
        self.assertEqual("complete", result["result"])
        self.assertEqual(2, result["sweeps"])
        self.assertIn((MODULE.STAGE_CONFLICT, 2), self.launched)
        self.assertIn((MODULE.STAGE_COPILOT_REVIEW, 2), self.launched)
        self.assertNotIn((MODULE.STAGE_SELF_REVIEW, 2), self.launched)
        self_review_run = next(
            run
            for run in result["runs"]
            if run["stage"] == MODULE.STAGE_SELF_REVIEW and run["sweep"] == 1
        )
        self.assertEqual(
            [
                {
                    "sha": NEXT_HEAD,
                    "title": "Commit b",
                    "url": (
                        "https://github.com/owner/repo/pull/7/commits/"
                        f"{NEXT_HEAD}"
                    ),
                }
            ],
            self_review_run["published_commits"],
        )

    def test_second_sweep_retries_an_uncleared_stage(self):
        def move_and_stall(entry, *args, **kwargs):
            if entry["stage"] == MODULE.STAGE_COPILOT_REVIEW:
                self.sync_heads.append(NEXT_HEAD)
                self.clear_at[MODULE.STAGE_CONFLICT] = None
                self.clear_at[MODULE.STAGE_SELF_REVIEW] = None
            result = self.run_stage(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_COPILOT_REVIEW:
                self.clear_at[entry["stage"]] = None
            return result

        MODULE.run_stage.side_effect = move_and_stall
        result = self.execute()
        launches = [
            item for item in self.launched if item[0] == MODULE.STAGE_COPILOT_REVIEW
        ]
        self.assertEqual(
            [
                (MODULE.STAGE_COPILOT_REVIEW, 1),
                (MODULE.STAGE_COPILOT_REVIEW, 2),
            ],
            launches,
        )
        self.assertEqual("incomplete", result["result"])

    def test_two_sweeps_bound_the_run_at_ten_stage_launches(self):
        def never_clear(entry, *args, **kwargs):
            result = self.run_stage(entry, *args, **kwargs)
            self.clear_at[entry["stage"]] = None
            if entry["stage"] == MODULE.STAGE_DESCRIPTION and kwargs["sweep"] == 1:
                self.sync_heads.append(NEXT_HEAD)
            return result

        MODULE.run_stage.side_effect = never_clear
        result = self.execute()
        self.assertEqual("incomplete", result["result"])
        self.assertEqual("two_sweeps_finished", result["reason"])
        self.assertEqual(10, len(self.launched))

    def test_nonzero_stage_exit_does_not_block_later_stages(self):
        original = self.run_stage

        def fail_conflict(entry, *args, **kwargs):
            result = original(entry, *args, **kwargs)
            if entry["stage"] == MODULE.STAGE_CONFLICT:
                result["returncode"] = 1
                self.clear_at[entry["stage"]] = None
            return result

        MODULE.run_stage.side_effect = fail_conflict
        result = self.execute()
        self.assertEqual("incomplete", result["result"])
        self.assertEqual(
            [(stage, 1) for stage in MODULE.STAGE_NAMES],
            self.launched,
        )

    def test_unsafe_worktree_stops_the_sweep(self):
        MODULE.settle_after_stage.side_effect = None
        MODULE.settle_after_stage.return_value = {
            "result": "blocked",
            "reason": "stage_left_dirty_worktree",
            "detail": "dirty",
        }
        result = self.execute()
        self.assertEqual("blocked", result["result"])
        self.assertEqual("stage_left_dirty_worktree", result["reason"])
        self.assertEqual([(MODULE.STAGE_CONFLICT, 1)], self.launched)

    def test_blocked_stage_preserves_escalation_and_retained_commits(self):
        local_head = "c" * 40
        escalation = {
            "reason": "unfixable_failure",
            "detail": "upstream defect",
            "checks": ["check:test"],
            "next_action": "Decide the fix.",
        }
        MODULE.settle_after_stage.side_effect = None
        MODULE.settle_after_stage.return_value = {
            "result": "blocked",
            "reason": "stage_left_unpublished_commits",
            "detail": "local commit was not pushed",
            "local_head_sha": local_head,
            "pr_head_sha": HEAD,
        }
        MODULE.inspect_stages.side_effect = None
        MODULE.inspect_stages.return_value = [
            {
                **uncleared_stage(stage, "escalated"),
                "status": {"escalation": escalation},
            }
            if stage == MODULE.STAGE_CONFLICT
            else uncleared_stage(stage)
            for stage in MODULE.STAGE_NAMES
        ]
        retained = [{"sha": local_head, "title": "Keep partial fix"}]
        with mock.patch.object(
            MODULE, "local_commits_between", return_value=retained
        ):
            result = self.execute()

        self.assertEqual("blocked", result["result"])
        self.assertEqual("stage_left_unpublished_commits", result["reason"])
        self.assertEqual(local_head, result["local_head_sha"])
        self.assertEqual(retained, result["retained_commits"])
        self.assertEqual(escalation, result["stage_result"]["status"]["escalation"])
        self.assertEqual(retained, result["runs"][0]["retained_commits"])
        self.assertEqual(5, len(result["stages"]))

    def test_closed_pull_request_runs_nothing(self):
        MODULE.read_pull_request.side_effect = lambda _target: pull_request(
            HEAD, state="CLOSED"
        )
        result = self.execute()
        self.assertEqual("blocked", result["result"])
        self.assertEqual("pr_not_open", result["reason"])
        self.assertEqual([], self.launched)


class WorktreeSafetyTest(unittest.TestCase):
    def git(self, repo: Path, *args: str) -> str:
        process = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.com",
            },
        )
        return process.stdout.strip()

    def make_remote(self, root: Path) -> tuple[Path, str, str]:
        remote = root / "remote"
        remote.mkdir()
        self.git(remote, "init", "-q", "-b", "main")
        self.git(remote, "commit", "-q", "--allow-empty", "-m", "base")
        base = self.git(remote, "rev-parse", "HEAD")
        self.git(remote, "checkout", "-q", "-b", "feature")
        self.git(remote, "commit", "-q", "--allow-empty", "-m", "pull request")
        head = self.git(remote, "rev-parse", "HEAD")
        self.git(remote, "update-ref", "refs/pull/7/head", head)
        self.git(remote, "checkout", "-q", "main")
        return remote, base, head

    def clone(self, root: Path, remote: Path) -> Path:
        local = root / "local"
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--single-branch",
                "--branch",
                "main",
                str(remote),
                str(local),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.git(local, "config", "user.name", "t")
        self.git(local, "config", "user.email", "t@example.com")
        return local

    def sync(self, local: Path, remote: Path, known_safe_head=None):
        with mock.patch.object(MODULE, "target_remote", return_value=str(remote)):
            return MODULE.sync_worktree(
                local,
                target(),
                pull_request(),
                known_safe_head=known_safe_head,
            )

    def test_unreachable_local_commit_is_not_discarded(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            self.git(repo, "init", "-q", "-b", "main")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "base")
            published = self.git(repo, "rev-parse", "HEAD")
            self.git(repo, "checkout", "-q", "--detach")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "local")
            with mock.patch.object(
                MODULE,
                "fetch_pr_head",
                return_value={"result": "ready", "head_sha": published},
            ):
                result = MODULE.sync_worktree(
                    repo,
                    target(),
                    pull_request(published),
                    known_safe_head=None,
                )
            self.assertEqual("blocked", result["result"])
            self.assertEqual("local_head_not_published", result["reason"])
            self.assertNotEqual(published, self.git(repo, "rev-parse", "HEAD"))

    def test_pr_branch_behind_remote_is_checked_out(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, _base, head = self.make_remote(root)
            local = self.clone(root, remote)
            self.git(local, "checkout", "-q", "-b", "feature")

            result = self.sync(local, remote)

            self.assertEqual("ready", result["result"])
            self.assertEqual(head, self.git(local, "rev-parse", "HEAD"))
            self.assertEqual("", self.git(local, "branch", "--show-current"))

    def test_unpublished_commit_on_pr_branch_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, _base, head = self.make_remote(root)
            local = self.clone(root, remote)
            self.git(local, "fetch", "-q", str(remote), "refs/pull/7/head")
            self.git(local, "checkout", "-q", "-b", "feature", "FETCH_HEAD")
            self.git(local, "commit", "-q", "--allow-empty", "-m", "local")
            local_head = self.git(local, "rev-parse", "HEAD")

            result = self.sync(local, remote)

            self.assertEqual("blocked", result["result"])
            self.assertEqual("local_head_not_published", result["reason"])
            self.assertEqual(local_head, self.git(local, "rev-parse", "HEAD"))
            self.assertNotEqual(head, local_head)

    def test_detached_old_pr_head_moves_to_new_pr_head(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, _base, old_head = self.make_remote(root)
            local = self.clone(root, remote)
            self.git(local, "fetch", "-q", str(remote), "refs/pull/7/head")
            self.git(local, "checkout", "-q", "--detach", "FETCH_HEAD")
            self.git(remote, "checkout", "-q", "feature")
            self.git(remote, "commit", "-q", "--allow-empty", "-m", "next")
            new_head = self.git(remote, "rev-parse", "HEAD")
            self.git(remote, "update-ref", "refs/pull/7/head", new_head)
            self.git(remote, "checkout", "-q", "main")

            result = self.sync(local, remote)

            self.assertEqual("ready", result["result"])
            self.assertNotEqual(old_head, new_head)
            self.assertEqual(new_head, self.git(local, "rev-parse", "HEAD"))

    def test_published_stage_commit_followed_by_another_push_is_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, _base, started = self.make_remote(root)
            local = self.clone(root, remote)
            self.git(local, "fetch", "-q", str(remote), "refs/pull/7/head")
            self.git(local, "checkout", "-q", "--detach", "FETCH_HEAD")
            self.git(local, "commit", "-q", "--allow-empty", "-m", "stage")
            stage_head = self.git(local, "rev-parse", "HEAD")
            self.git(local, "push", "-q", str(remote), "HEAD:refs/pull/7/head")
            self.git(remote, "checkout", "-q", "feature")
            self.git(remote, "reset", "-q", "--hard", stage_head)
            self.git(remote, "commit", "-q", "--allow-empty", "-m", "other")
            final_head = self.git(remote, "rev-parse", "HEAD")
            self.git(remote, "update-ref", "refs/pull/7/head", final_head)
            self.git(remote, "checkout", "-q", "main")

            with mock.patch.object(MODULE, "target_remote", return_value=str(remote)):
                result = MODULE.settle_after_stage(
                    local,
                    target(),
                    started_head_sha=started,
                )

            self.assertEqual("ready", result["result"])
            self.assertEqual(final_head, self.git(local, "rev-parse", "HEAD"))

    def test_lists_retained_first_parent_commits(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            self.git(repo, "init", "-q", "-b", "main")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "base")
            base = self.git(repo, "rev-parse", "HEAD")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "first fix")
            first = self.git(repo, "rev-parse", "HEAD")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "second fix")
            second = self.git(repo, "rev-parse", "HEAD")

            self.assertEqual(
                [
                    {"sha": first, "title": "first fix"},
                    {"sha": second, "title": "second fix"},
                ],
                MODULE.local_commits_between(repo, base, second),
            )


class AgentInstructionTest(unittest.TestCase):
    def test_agent_has_one_helper_command_and_no_manual_lifecycle(self):
        text = AGENT.read_text(encoding="utf-8")
        self.assertIn("pr_pipeline.py\" run", text)
        for command in ("`next`", "`start`", "`wait`", "`finish`", "`reset`"):
            self.assertNotIn(command, text)
        self.assertIn("at most two foreground sweeps", text)
        self.assertIn("reaches its limit does not block", text)
        self.assertIn("asynchronously as an attached process", text)
        self.assertIn("30-second initial wait", text)
        self.assertIn("same shell at least once every five minutes", text)
        self.assertIn("Never end your turn", text)
        self.assertIn("A clean run that pushed no commits", text)
        self.assertIn("Do not organize the response by sweep", text)
        self.assertNotIn("### Sweep 1", text)
        self.assertNotIn("### Sweep 2", text)
        self.assertNotIn("### Final stage status", text)
        self.assertNotIn("Pushed commits: none", text)
        self.assertIn("published_commits", text)
        self.assertIn("retained_commits", text)
        self.assertIn("stage_result.status", text)
        self.assertLess(
            text.index("2. `copilot-review-loop`"),
            text.index("3. `self-review-loop`"),
        )


class CommandOutputTest(unittest.TestCase):
    def test_run_emits_json_lines_ending_with_pipeline_result(self):
        def fake_pipeline(_target, _repo, *, models, effort, report):
            self.assertIsNotNone(models)
            self.assertEqual("high", effort)
            report({"event": "pipeline_started", "run_id": "run-1"})
            report(
                {
                    "event": "stage_started",
                    "stage": MODULE.STAGE_CONFLICT,
                    "sweep": 1,
                }
            )
            return {
                "result": "complete",
                "run_id": "run-1",
                "head_sha": HEAD,
            }

        args = MODULE.build_parser().parse_args(["run", "owner/repo#7"])
        output = StringIO()
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=Path("C:/repo")),
            mock.patch.object(MODULE, "resolve_target", return_value=target()),
            mock.patch.object(MODULE, "run_pipeline", side_effect=fake_pipeline),
            redirect_stdout(output),
        ):
            MODULE.command_run(args)

        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            ["pipeline_started", "stage_started", "pipeline_finished"],
            [event["event"] for event in events],
        )
        self.assertEqual("complete", events[-1]["result"])
        self.assertEqual(HEAD, events[-1]["head_sha"])

    def test_error_is_a_terminal_json_event(self):
        output = StringIO()
        with (
            mock.patch.object(
                MODULE, "require_tools", side_effect=MODULE.WorkflowError("broken")
            ),
            mock.patch.object(
                __import__("sys"), "argv", ["pr_pipeline.py", "run", "owner/repo#7"]
            ),
            redirect_stdout(output),
        ):
            result = MODULE.main()

        self.assertEqual(1, result)
        event = json.loads(output.getvalue())
        self.assertEqual("pipeline_finished", event["event"])
        self.assertEqual("error", event["result"])
        self.assertEqual("broken", event["error"])


class ParserTest(unittest.TestCase):
    def test_run_is_the_only_command(self):
        parser = MODULE.build_parser()
        action = next(
            action
            for action in parser._actions
            if isinstance(action, __import__("argparse")._SubParsersAction)
        )
        self.assertEqual({"run"}, set(action.choices))

    def test_run_accepts_model_overrides(self):
        args = MODULE.build_parser().parse_args(
            [
                "run",
                "owner/repo#7",
                "--stage-model",
                "ci-fix-loop=claude-sonnet-5",
            ]
        )
        self.assertEqual("owner/repo#7", args.target)
        self.assertEqual(["ci-fix-loop=claude-sonnet-5"], args.stage_model)


if __name__ == "__main__":
    unittest.main()
