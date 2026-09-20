import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

import test_pr_conflict_resolver as existing
from test_stack_publication import write_authorization


MODULE = existing.MODULE
COMMON_PATH = Path(__file__).parents[2] / "pr-pipeline" / "scripts" / "pipeline_common.py"
SPEC = importlib.util.spec_from_file_location("noop_pipeline_common", COMMON_PATH)
COMMON = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMMON)


class NativeStackNoopTest(unittest.TestCase):
    def setUp(self):
        self.directory = existing.temporary_directory(self)
        self.root = self.directory / "repo"
        self.root.mkdir()
        self.remote = self.directory / "remote.git"
        MODULE.git(self.root, "init", "--initial-branch=main")
        MODULE.git(self.root, "config", "user.name", "Test")
        MODULE.git(self.root, "config", "user.email", "test@example.com")
        MODULE.git(self.root, "config", "commit.gpgsign", "false")
        self.commit("base.txt", "base")
        self.trunk = MODULE.git(self.root, "rev-parse", "HEAD")
        MODULE.git(self.root, "checkout", "-b", "lower")
        self.commit("lower.txt", "lower")
        self.lower = MODULE.git(self.root, "rev-parse", "HEAD")
        MODULE.git(self.root, "checkout", "-b", "upper")
        self.commit("upper.txt", "upper")
        self.upper = MODULE.git(self.root, "rev-parse", "HEAD")
        MODULE.git(self.root, "checkout", "--detach", self.lower)
        MODULE.git(self.root, "clone", "--no-checkout", str(self.root), str(self.remote))
        MODULE.git(self.remote, "config", "user.name", "Test")
        MODULE.git(self.remote, "config", "user.email", "test@example.com")
        MODULE.git(self.root, "remote", "add", "origin", str(self.remote))
        for branch, head in (("main", self.trunk), ("lower", self.lower), ("upper", self.upper)):
            MODULE.git(self.remote, "update-ref", f"refs/heads/{branch}", head)
        for number, head in ((7, self.lower), (8, self.upper)):
            MODULE.git(self.remote, "update-ref", f"refs/pull/{number}/head", head)
        self.metadata = {
            number: existing.pr_metadata(
                number=number, pr_url=f"https://github.com/owner/repo/pull/{number}",
                head_owner="owner", head_branch=branch, head_sha=head,
                base_branch=base, base_sha=base_sha, mergeable="MERGEABLE",
                merge_state_status="CLEAN",
            )
            for number, branch, head, base, base_sha in (
                (7, "lower", self.lower, "main", self.trunk),
                (8, "upper", self.upper, "lower", self.lower),
            )
        }
        self.stack = {
            "id": "stack-id", "number": 77, "trunk": "main", "size": 2,
            "members": [
                {
                    **self.metadata[number], "position": index,
                    "commits": [self.metadata[number]["head_sha"]], "commits_complete": True,
                }
                for index, number in enumerate((7, 8))
            ],
        }
        self.request, self.request_path, self.state_path, self.owner_path = write_authorization(
            self.directory, self.stack, fixed=7, operation="whole-stack"
        )
        self.args = MODULE.build_parser().parse_args([
            "pipeline", "owner/repo#7", "--repo-root", str(self.root),
            "--state", str(self.state_path), "--pipeline-run", "run-1",
            "--pipeline-iteration", "1", "--pipeline-max-iterations", "3",
            "--whole-stack", "--stack-request", str(self.request_path),
        ])
        self.calls = {}
        for name, options in {
            "require_tools": {},
            "checkout_pr_branch": {},
            "metadata_for": {"side_effect": lambda target: copy.deepcopy(self.metadata[target["number"]])},
            "stack_membership": {"side_effect": lambda pr: {
                "default_branch": "main", "stack": copy.deepcopy(self.stack),
            }},
            "base_ref_tip": {"side_effect": lambda repo, branch: MODULE.git(
                self.remote, "rev-parse", "--verify", f"refs/heads/{branch}"
            )},
            "find_remote": {"return_value": "origin"},
            "external_stack_dependents": {"return_value": []},
            "stack_relations": {"return_value": existing.NO_RELATIONS},
            "repository_merge_methods": {"return_value": existing.ALL_MERGE_METHODS},
            "discover_conflict_task": {"return_value": self.directory / "fake-dispatcher.py"},
            "publish_conflict_result": {"side_effect": AssertionError("unexpected publication")},
            "gh_json": {"side_effect": AssertionError("unexpected network request")},
            "emit": {},
        }.items():
            patch = mock.patch.object(MODULE, name, **options)
            self.calls[name] = patch.start()
            self.addCleanup(patch.stop)
        self.commands = []
        self.dispatches = []
        real_run = MODULE.run

        def run(command, **kwargs):
            self.commands.append(command)
            if command[0] == sys.executable:
                self.dispatches.append(command)
                raise OSError("fake dispatcher stops before hosted execution")
            if command[0] != "git" or "push" in command:
                raise AssertionError(f"unexpected command: {command}")
            return real_run(command, **kwargs)

        patch = mock.patch.object(MODULE, "run", side_effect=run)
        patch.start()
        self.addCleanup(patch.stop)

    def commit(self, path, content):
        (self.root / path).write_text(content, encoding="utf-8")
        MODULE.git(self.root, "add", path)
        MODULE.git(self.root, "commit", "-m", content)

    def status(self):
        MODULE.command_status(MODULE.build_parser().parse_args([
            "status", "--state", str(self.state_path),
        ]))
        return self.calls["emit"].call_args.args[0]

    def assert_no_dispatch(self):
        self.assertEqual([], self.dispatches)
        self.calls["discover_conflict_task"].assert_not_called()
        self.calls["publish_conflict_result"].assert_not_called()
        self.assertFalse(any("push" in command for command in self.commands))

    def assert_not_clear(self):
        if self.state_path.exists():
            state = MODULE.load_state(self.state_path)
            self.assertIsNone(MODULE.cleared_head_sha(state))
            self.assertNotEqual("cleared", MODULE.stage_outcome(state))
        self.assert_no_dispatch()

    def test_fresh_normal_pipeline_preserves_heads_metadata_budget_and_truthful_status(self):
        before_refs = MODULE.git(self.remote, "show-ref")
        before_objects = [MODULE.git(self.root, "cat-file", "commit", sha)
                          for sha in (self.lower, self.upper)]
        before_request = self.request_path.read_bytes()
        before_owner = self.owner_path.read_bytes()
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        result = self.calls["emit"].call_args.args[0]
        self.assertEqual("mergeable", result["result"])
        self.assertEqual("cleared", result["stage_outcome"])
        self.assertEqual([], result["published_commits"])
        state = MODULE.load_state(self.state_path)
        self.assertEqual(0, state["managed_attempts"])
        self.assertEqual(3, state["pipeline"]["budget"])
        self.assertEqual(1, state["pipeline"]["iteration"])
        self.assertEqual({"id": "run-1-1", "number": 1, "budget": 3},
                         state["agent_task"]["iteration"])
        self.assertIsNone(state["agent_task"]["task_id"])
        self.assertEqual("not_needed", state["agent_task"]["task_id_status"])
        for field in ("code_refs", "publication", "request_file", "result", "helper_command"):
            self.assertNotIn(field, state["agent_task"])
        self.assertIsNone(state["attempt"]["published_head_sha"])
        self.assertEqual([self.lower, self.upper], [
            member["head_sha"] for member in state["native_stack_clearance"]["members"]
        ])
        self.assertEqual([self.trunk, self.lower], [
            member["merge_base"] for member in state["native_stack_clearance"]["members"]
        ])
        self.assertEqual(self.request, state["native_stack_clearance"]["authorization"])
        status = self.status()
        for head, base, expected in (
            (self.lower, self.trunk, True),
            (self.upper, self.trunk, False),
            (self.lower, self.lower, False),
        ):
            inspected = COMMON.inspect_stage(
                COMMON.STAGES[0], MODULE.parse_target("owner/repo#7"), head, base,
                read_status=lambda *a: {
                    "installed": True, "ok": True, "state": "ready", "payload": status,
                },
            )
            self.assertEqual(expected, inspected["clear"])
        self.assertEqual(before_refs, MODULE.git(self.remote, "show-ref"))
        self.assertEqual(before_objects, [
            MODULE.git(self.root, "cat-file", "commit", sha) for sha in (self.lower, self.upper)
        ])
        self.assertEqual(before_request, self.request_path.read_bytes())
        self.assertEqual(before_owner, self.owner_path.read_bytes())
        self.assert_no_dispatch()
        self.calls["repository_merge_methods"].assert_not_called()

    def test_later_sweep_uses_retained_noop_scope_without_spending_task_budget(self):
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        self.args.pipeline_iteration = 2
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        state = MODULE.load_state(self.state_path)
        self.assertEqual(0, state["managed_attempts"])
        self.assertEqual(3, state["pipeline"]["budget"])
        self.assertEqual("cleared", MODULE.stage_outcome(state))
        self.assertEqual([7, 8], [
            item["pr_number"] for item in state["pipeline_native_scope"]["members"]
        ])
        self.assert_no_dispatch()

    def test_upper_invocation_checks_the_whole_stack_without_replay(self):
        MODULE.git(self.root, "checkout", "--detach", self.upper)
        self.request, _, _, _ = write_authorization(
            self.directory, self.stack, fixed=8, operation="whole-stack"
        )
        self.args.target = "owner/repo#8"
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        state = MODULE.load_state(self.state_path)
        self.assertEqual(self.upper, MODULE.cleared_head_sha(state))
        self.assertEqual([7, 8], [
            member["pr_number"] for member in state["native_stack_clearance"]["members"]
        ])
        self.assert_no_dispatch()

    def move_trunk_without_tree_change(self):
        tree = MODULE.git(self.remote, "rev-parse", f"{self.trunk}^{{tree}}")
        moved = MODULE.git(self.remote, "commit-tree", tree, "-p", self.trunk, "-m", "new ancestry")
        MODULE.git(self.remote, "update-ref", "refs/heads/main", moved)
        self.metadata[7]["base_sha"] = moved
        return moved

    def assert_dispatch(self):
        self.assertEqual(1, MODULE.command_pipeline(self.args))
        self.assertEqual(1, len(self.dispatches))
        state = MODULE.load_state(self.state_path)
        self.assertEqual(1, state["managed_attempts"])
        self.assertIsNone(MODULE.cleared_head_sha(state))
        self.assertEqual("native-stack", state["agent_task"]["preflight"]["request"]["strategy"])
        self.calls["publish_conflict_result"].assert_not_called()

    def test_equal_tree_changed_trunk_ancestry_still_dispatches(self):
        moved = self.move_trunk_without_tree_change()
        self.assertEqual(
            MODULE.git(self.remote, "rev-parse", f"{self.trunk}^{{tree}}"),
            MODULE.git(self.remote, "rev-parse", f"{moved}^{{tree}}"),
        )
        self.assert_dispatch()

    def test_equal_tree_changed_predecessor_ancestry_still_dispatches(self):
        tree = MODULE.git(self.remote, "rev-parse", f"{self.lower}^{{tree}}")
        moved = MODULE.git(self.remote, "commit-tree", tree, "-p", self.trunk, "-m", "rewritten lower")
        MODULE.git(self.remote, "update-ref", "refs/heads/lower", moved)
        MODULE.git(self.remote, "update-ref", "refs/pull/7/head", moved)
        MODULE.git(self.root, "fetch", "origin", "lower")
        MODULE.git(self.root, "checkout", "--detach", moved)
        self.metadata[7]["head_sha"] = moved
        self.metadata[8]["base_sha"] = moved
        self.stack["members"][0]["head_sha"] = moved
        self.request, _, _, _ = write_authorization(
            self.directory, self.stack, fixed=7, operation="whole-stack"
        )
        self.assert_dispatch()

    def test_later_sweep_does_not_clear_new_unintegrated_ancestry(self):
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        self.move_trunk_without_tree_change()
        self.args.pipeline_iteration = 2
        with self.assertRaisesRegex(MODULE.WorkflowError, "not freshly mergeable and aligned"):
            MODULE.command_pipeline(self.args)
        self.assert_not_clear()
        self.assertEqual(0, MODULE.load_state(self.state_path)["managed_attempts"])

    def test_explicit_strategies_keep_hosted_preparation(self):
        for strategy in ("merge", "rebase"):
            with self.subTest(strategy=strategy):
                if self.state_path.exists():
                    self.state_path.unlink()
                self.dispatches.clear()
                self.args.strategy = strategy
                self.assert_dispatch()

    def test_conflicting_member_does_not_clear(self):
        self.metadata[8]["mergeable"] = "CONFLICTING"
        self.assert_dispatch()

    def test_unknown_member_exhausts_bounded_observation_without_clearance(self):
        for number in (7, 8):
            if self.state_path.exists():
                self.state_path.unlink()
            self.metadata[number]["mergeable"] = "UNKNOWN"
            with self.subTest(number=number), mock.patch.object(MODULE.time, "sleep") as sleep:
                self.assertEqual(1, MODULE.command_pipeline(self.args))
                self.assertEqual(len(MODULE.MERGEABILITY_RETRY_DELAYS), sleep.call_count)
                self.assert_not_clear()
            self.metadata[number]["mergeable"] = "MERGEABLE"

    def test_unknown_can_settle_only_through_fresh_observation(self):
        reads = []
        self.stack["members"][-1]["mergeable"] = "UNKNOWN"

        def metadata(target):
            current = copy.deepcopy(self.metadata[target["number"]])
            if target["number"] == 8:
                reads.append(8)
                if len(reads) == 1:
                    current["mergeable"] = "UNKNOWN"
                else:
                    self.stack["members"][-1]["mergeable"] = "MERGEABLE"
            return current

        self.calls["metadata_for"].side_effect = metadata
        with mock.patch.object(MODULE.time, "sleep") as sleep:
            self.assertEqual(0, MODULE.command_pipeline(self.args))
        self.assertEqual(1, sleep.call_count)
        self.assertGreaterEqual(len(reads), 3)
        self.assert_no_dispatch()

    def test_final_member_identity_and_mergeability_races_fail_closed(self):
        original = self.calls["metadata_for"].side_effect
        for field, value in (
            ("head_sha", self.lower), ("base_sha", self.trunk),
            ("head_branch", "other"), ("base_branch", "main"),
            ("head_owner", "foreign"), ("head_repo", "foreign"),
            ("repo_name", "foreign/repo"), ("number", 99),
            ("state", "CLOSED"), ("mergeable", "CONFLICTING"),
            ("mergeable", "UNKNOWN"), ("mergeable", None),
        ):
            reads = []

            def changed(target):
                current = original(target)
                if target["number"] == 8:
                    reads.append(8)
                    if len(reads) >= 2:
                        current[field] = value
                return current

            self.calls["metadata_for"].side_effect = changed
            if self.state_path.exists():
                self.state_path.unlink()
            with self.subTest(field=field, value=value), mock.patch.object(MODULE.time, "sleep"):
                self.assertEqual(1, MODULE.command_pipeline(self.args))
                self.assert_not_clear()
        self.calls["metadata_for"].side_effect = original

    def test_final_topology_owner_and_advertised_head_races_fail_closed(self):
        original_stack = copy.deepcopy(self.stack)
        original_owner = self.owner_path.read_bytes()
        real_identity = MODULE.conflict_preflight_identity
        for race in (
            "order", "unselected", "stack-id", "position", "owner", "local",
            "source-ref", "base-ref", "stack-conflicting", "stack-unknown",
        ):
            if self.state_path.exists():
                self.state_path.unlink()
            self.stack = copy.deepcopy(original_stack)
            self.owner_path.write_bytes(original_owner)
            reads = []

            def tip(repo, branch):
                reads.append(branch)
                if branch == "upper":
                    if race == "order":
                        self.stack["members"].reverse()
                    elif race == "unselected":
                        self.stack["members"].append({**self.stack["members"][-1], "number": 9})
                        self.stack["size"] = 3
                    elif race == "stack-id":
                        self.stack["id"] = "different"
                    elif race == "position":
                        self.stack["members"][-1]["position"] = 99
                    elif race == "stack-conflicting":
                        self.stack["members"][-1]["mergeable"] = "CONFLICTING"
                    elif race == "stack-unknown":
                        self.stack["members"][-1]["mergeable"] = "UNKNOWN"
                    elif race == "source-ref":
                        return self.lower
                if race == "base-ref" and branch == "main" and reads.count("main") >= 3:
                    return self.upper
                return MODULE.git(self.remote, "rev-parse", f"refs/heads/{branch}")

            def identity(root, metadata):
                result = real_identity(root, metadata)
                if race == "owner" and "upper" in reads:
                    owner = json.loads(original_owner)
                    owner["result"] = {"result": "cancelled"}
                    self.owner_path.write_text(json.dumps(owner), encoding="utf-8")
                if race == "local" and "upper" in reads:
                    return {**result, "branch": "different"}
                return result

            self.calls["base_ref_tip"].side_effect = tip
            with self.subTest(race=race), mock.patch.object(
                MODULE, "conflict_preflight_identity", side_effect=identity
            ):
                self.assertEqual(1, MODULE.command_pipeline(self.args))
                self.assert_not_clear()

    def test_invalid_selection_and_incomplete_snapshot_fail_before_dispatch(self):
        for change in (
            lambda r: r.update(selected=[8]),
            lambda r: r.update(selected=[7, 8, 9]),
            lambda r: r["source_stack"].update(size=3),
            lambda r: r["source_stack"]["members"][1].update(head_sha=""),
        ):
            request = copy.deepcopy(self.request)
            change(request)
            request["request_sha256"] = MODULE.request_digest(request)
            self.request_path.write_text(json.dumps(request), encoding="utf-8")
            with self.subTest(change=change), self.assertRaises(MODULE.WorkflowError):
                MODULE.command_pipeline(self.args)
            self.assert_not_clear()

    def test_selected_inactive_member_cannot_disappear_from_noop_evidence(self):
        source = copy.deepcopy(self.stack)
        source["members"].append({
            **source["members"][-1], "number": 9, "state": "CLOSED",
            "head_branch": "closed", "base_branch": "upper",
        })
        source["size"] = 3
        self.stack["source_stack"] = source
        self.request, _, _, _ = write_authorization(
            self.directory, source, fixed=7, operation="whole-stack"
        )
        self.assertEqual(1, MODULE.command_pipeline(self.args))
        self.assert_not_clear()

    def test_incomplete_native_base_evidence_is_not_clear(self):
        self.stack["members"][-1]["base_sha"] = None
        self.assertEqual(1, MODULE.command_pipeline(self.args))
        self.assert_not_clear()

    def test_ancestry_command_error_is_not_noop_or_replay_authorization(self):
        real_git = MODULE.git

        def git(root, *args):
            if args[:2] == ("merge-base", "--all"):
                raise MODULE.WorkflowError("missing commit object")
            return real_git(root, *args)

        with mock.patch.object(MODULE, "git", side_effect=git):
            self.assertEqual(1, MODULE.command_pipeline(self.args))
        self.assert_not_clear()

    def test_noop_ancestry_subprocess_uses_windows_launch_helper(self):
        completed = subprocess.CompletedProcess(["git"], 0, self.trunk, "")
        with mock.patch.object(MODULE, "IS_WINDOWS", True), \
             mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
             mock.patch.object(subprocess, "run", return_value=completed) as launch:
            MODULE.git(self.root, "merge-base", "--all", self.trunk, self.lower)
        self.assertEqual(0x08000000, launch.call_args.kwargs["creationflags"])
