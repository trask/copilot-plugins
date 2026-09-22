import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
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
    @classmethod
    def setUpClass(cls):
        cls.template = tempfile.TemporaryDirectory()
        template = Path(cls.template.name).resolve()
        cls.template_root = template / "repo"
        cls.template_root.mkdir()
        cls.template_remote = template / "remote.git"
        MODULE.git(cls.template_root, "init", "--initial-branch=main")
        MODULE.git(cls.template_root, "config", "user.name", "Test")
        MODULE.git(cls.template_root, "config", "user.email", "test@example.com")
        MODULE.git(cls.template_root, "config", "commit.gpgsign", "false")
        cls.template_commit("base.txt", "base")
        cls.trunk = MODULE.git(cls.template_root, "rev-parse", "HEAD")
        MODULE.git(cls.template_root, "checkout", "-b", "lower")
        cls.template_commit("lower.txt", "lower")
        cls.lower = MODULE.git(cls.template_root, "rev-parse", "HEAD")
        MODULE.git(cls.template_root, "checkout", "-b", "upper")
        cls.template_commit("upper.txt", "upper")
        cls.upper = MODULE.git(cls.template_root, "rev-parse", "HEAD")
        MODULE.git(cls.template_root, "checkout", "--detach", cls.lower)
        MODULE.git(
            cls.template_root,
            "clone",
            "--no-checkout",
            str(cls.template_root),
            str(cls.template_remote),
        )
        MODULE.git(cls.template_remote, "config", "user.name", "Test")
        MODULE.git(cls.template_remote, "config", "user.email", "test@example.com")
        MODULE.git(cls.template_root, "remote", "add", "origin", str(cls.template_remote))
        for branch, head in (("main", cls.trunk), ("lower", cls.lower), ("upper", cls.upper)):
            MODULE.git(cls.template_remote, "update-ref", f"refs/heads/{branch}", head)
        for number, head in ((7, cls.lower), (8, cls.upper)):
            MODULE.git(cls.template_remote, "update-ref", f"refs/pull/{number}/head", head)

    @classmethod
    def tearDownClass(cls):
        cls.template.cleanup()

    @classmethod
    def template_commit(cls, path, content):
        (cls.template_root / path).write_text(content, encoding="utf-8")
        MODULE.git(cls.template_root, "add", path)
        MODULE.git(cls.template_root, "commit", "-m", content)

    def setUp(self):
        self.directory = existing.temporary_directory(self)
        self.root = self.directory / "repo"
        self.remote = self.directory / "remote.git"
        shutil.copytree(self.template_root, self.root)
        shutil.copytree(self.template_remote, self.remote)
        MODULE.git(self.root, "remote", "set-url", "origin", str(self.remote))
        self.trunk = type(self).trunk
        self.lower = type(self).lower
        self.upper = type(self).upper
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

    def test_noop_ancestry_subprocess_uses_windows_launch_helper(self):
        completed = subprocess.CompletedProcess(["git"], 0, self.trunk, "")
        with mock.patch.object(MODULE, "IS_WINDOWS", True), \
             mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
             mock.patch.object(subprocess, "run", return_value=completed) as launch:
            MODULE.git(self.root, "merge-base", "--all", self.trunk, self.lower)
        self.assertEqual(0x08000000, launch.call_args.kwargs["creationflags"])
