import copy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_pr_conflict_resolver as existing


MODULE = existing.MODULE


class ConflictPipelineSweepTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "repo"
        self.root.mkdir()
        self.path = self.directory / "state.json"
        self.args = MODULE.build_parser().parse_args([
            "pipeline", "owner/repo#7", "--repo-root", str(self.root),
            "--state", str(self.path), "--pipeline-run", "run-1",
            "--pipeline-iteration", "1", "--pipeline-max-iterations", "3",
        ])
        self.metadata = existing.pr_metadata(
            mergeable="MERGEABLE", head_sha="a" * 40, base_sha="b" * 40,
        )
        patches = {
            "require_tools": {},
            "resolve_repo_root": {"return_value": self.root},
            "conflict_preflight": {"side_effect": lambda *a, **kw: {
                "already_mergeable": True, "pr": copy.deepcopy(self.metadata), "strategy": None,
            }},
            "require_clean_worktree": {},
            "require_no_integration_in_progress": {},
            "conflict_preflight_identity": {"return_value": {"head": self.metadata["head_sha"]}},
            "live_mergeability": {"side_effect": lambda *a, **kw: copy.deepcopy(self.metadata)},
            "stack_membership": {"return_value": {"default_branch": "main", "stack": None}},
            "base_ref_tip": {"side_effect": lambda *a: self.metadata["base_sha"]},
            "discover_conflict_task": {},
            "emit": {},
        }
        self.calls = {}
        for name, options in patches.items():
            patch = mock.patch.object(MODULE, name, **options)
            self.calls[name] = patch.start()
            self.addCleanup(patch.stop)

    def first_sweep(self):
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        self.args.pipeline_iteration = 2
        return MODULE.load_state(self.path)

    def test_later_sweep_revalidates_changed_head_without_hosted_work(self):
        previous = self.first_sweep()
        self.metadata["head_sha"] = "c" * 40
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        current = MODULE.load_state(self.path)
        self.assertEqual("c" * 40, MODULE.cleared_head_sha(current))
        self.assertEqual(previous["managed_attempts"], current["managed_attempts"])
        self.assertEqual(1, self.calls["conflict_preflight"].call_count)
        self.calls["discover_conflict_task"].assert_not_called()
        self.assertTrue(self.calls["live_mergeability"].called)
        self.assertEqual(previous["agent_task"], current["pipeline_sweep_history"][0]["agent_task"])
        self.assertEqual(2, current["pipeline"]["iteration"])
        self.assertEqual("a" * 40, current["history"][0]["head_sha"])

    def test_unchanged_head_still_checks_fresh_mergeability_and_live_base(self):
        self.first_sweep()
        for iteration, base in ((2, "b" * 40), (3, "d" * 40)):
            self.args.pipeline_iteration = iteration
            self.metadata["base_sha"] = base
            with self.subTest(iteration=iteration):
                self.assertEqual(0, MODULE.command_pipeline(self.args))
                current = MODULE.load_state(self.path)
                self.assertEqual("a" * 40, MODULE.cleared_head_sha(current))
                self.assertEqual(base, current["attempt"]["base_sha"])
                self.assertEqual(0, current["managed_attempts"])
        self.assertGreaterEqual(self.calls["live_mergeability"].call_count, 4)
        self.assertEqual(1, self.calls["conflict_preflight"].call_count)
        self.calls["discover_conflict_task"].assert_not_called()

    def test_invalid_or_unfinished_state_never_changes_or_reads_live(self):
        original = self.first_sweep()
        for mutate in (
            lambda s: s["pipeline"].update(iteration=2),
            lambda s: s["pipeline"].update(iteration=3),
            lambda s: s["pipeline"].update(run="foreign"),
            lambda s: s["pipeline"].update(budget=4),
            lambda s: s["pipeline"].update(target="https://github.com/owner/repo/pull/8"),
            lambda s: s["pipeline"].update(repo_root="different"),
            lambda s: s["pipeline"].update(model="different"),
            lambda s: s["pipeline"].update(strategy="rebase"),
            lambda s: s["pipeline"].update(whole_stack=True),
            lambda s: s["pipeline"].update(extra="invalid"),
            lambda s: s["pipeline"].update(iteration=True),
            lambda s: s["agent_task"].update(status="running"),
            lambda s: s["agent_task"].update(status="interrupted"),
            lambda s: s["agent_task"].update(status="failed"),
            lambda s: s["agent_task"].update(invocation_id="foreign"),
            lambda s: s["agent_task"].update(policy="marketplace-conflict-worker@8"),
            lambda s: s["agent_task"].update(model="different"),
            lambda s: s["agent_task"].update(task_id="active-task"),
            lambda s: s.update(last_result="published"),
            lambda s: s.update(pr=None),
            lambda s: s.update(managed_attempts=-1),
            lambda s: s.update(attempts=True),
            lambda s: s.update(history=None),
            lambda s: s.update(pipeline_sweep_history={}),
            lambda s: s.update(escalation={"kind": "blocked"}),
            lambda s: s.pop("pipeline"),
        ):
            changed = copy.deepcopy(original)
            mutate(changed)
            MODULE.save_state(self.path, changed)
            before = self.path.read_bytes()
            with self.subTest(mutation=mutate), self.assertRaises(MODULE.WorkflowError):
                MODULE.command_pipeline(self.args)
            self.assertEqual(before, self.path.read_bytes())
        self.calls["live_mergeability"].assert_not_called()
        self.calls["discover_conflict_task"].assert_not_called()

    def test_active_lock_and_iteration_budget_cannot_be_bypassed(self):
        self.first_sweep()
        before = self.path.read_bytes()
        lock = self.path.with_name(self.path.name + ".lock")
        lock.write_text("123", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.WorkflowError, "another invocation"):
            MODULE.command_pipeline(self.args)
        self.assertTrue(lock.exists())
        lock.unlink()
        for iteration, budget in ((0, 3), (4, 3), (2, None), (True, 3)):
            self.args.pipeline_iteration, self.args.pipeline_max_iterations = iteration, budget
            with self.subTest(iteration=iteration), self.assertRaises(MODULE.WorkflowError):
                MODULE.command_pipeline(self.args)
        self.assertEqual(before, self.path.read_bytes())

    def test_standalone_scope_and_stale_state_hash_are_rejected(self):
        self.first_sweep()
        before = self.path.read_bytes()
        for option, value in (
            ("new_invocation", True), ("invocation_run", "run-1"),
            ("expected_state_sha256", "0" * 64),
        ):
            original = getattr(self.args, option)
            setattr(self.args, option, value)
            with self.subTest(option=option), self.assertRaises(MODULE.WorkflowError):
                MODULE.command_pipeline(self.args)
            setattr(self.args, option, original)
            self.assertEqual(before, self.path.read_bytes())

    def test_real_conflict_or_unknown_blocks_without_stale_clearance_or_task(self):
        original = self.first_sweep()
        for mergeable in ("CONFLICTING", "UNKNOWN"):
            MODULE.save_state(self.path, copy.deepcopy(original))
            self.metadata.update(mergeable=mergeable, head_sha="c" * 40)
            with self.subTest(mergeable=mergeable), self.assertRaisesRegex(
                MODULE.WorkflowError, "not freshly mergeable"
            ):
                MODULE.command_pipeline(self.args)
            current = MODULE.load_state(self.path)
            self.assertIsNone(MODULE.cleared_head_sha(current))
            self.assertEqual("failed", current["agent_task"]["status"])
            self.assertEqual(0, current["managed_attempts"])
        self.calls["discover_conflict_task"].assert_not_called()
        self.assertEqual(1, self.calls["conflict_preflight"].call_count)

    def test_interrupted_revalidation_cannot_be_retried_as_a_later_sweep(self):
        self.first_sweep()
        self.calls["live_mergeability"].side_effect = KeyboardInterrupt
        self.assertEqual(1, MODULE.command_pipeline(self.args))
        current = MODULE.load_state(self.path)
        self.assertEqual("interrupted", current["agent_task"]["status"])
        self.assertIsNone(MODULE.cleared_head_sha(current))
        self.args.pipeline_iteration = 3
        before = self.path.read_bytes()
        with self.assertRaisesRegex(MODULE.WorkflowError, "fresh invocation"):
            MODULE.command_pipeline(self.args)
        self.assertEqual(before, self.path.read_bytes())

    def test_changed_identity_or_concurrent_head_base_scope_blocks(self):
        original = self.first_sweep()
        metadata = copy.deepcopy(self.metadata)
        for field, value in (
            ("head_branch", "different"), ("base_branch", "different"),
            ("head_owner", "different"), ("state", "CLOSED"),
        ):
            MODULE.save_state(self.path, copy.deepcopy(original))
            self.metadata = {**metadata, field: value}
            with self.subTest(field=field), self.assertRaises(MODULE.WorkflowError):
                MODULE.command_pipeline(self.args)
            self.assertIsNone(MODULE.cleared_head_sha(MODULE.load_state(self.path)))
        self.metadata = metadata
        for field in ("head_sha", "base_sha"):
            MODULE.save_state(self.path, copy.deepcopy(original))
            self.calls["live_mergeability"].side_effect = [
                metadata, {**metadata, field: "f" * 40},
            ]
            with self.subTest(drift=field), self.assertRaisesRegex(MODULE.WorkflowError, "changed"):
                MODULE.command_pipeline(self.args)
        self.calls["live_mergeability"].side_effect = lambda *a, **kw: copy.deepcopy(metadata)
        MODULE.save_state(self.path, copy.deepcopy(original))
        self.calls["base_ref_tip"].side_effect = lambda *a: "f" * 40
        with self.assertRaisesRegex(MODULE.WorkflowError, "live base changed"):
            MODULE.command_pipeline(self.args)
        self.calls["base_ref_tip"].side_effect = lambda *a: metadata["base_sha"]
        MODULE.save_state(self.path, copy.deepcopy(original))
        self.calls["stack_membership"].side_effect = [
            {"default_branch": "main", "stack": None}, existing.native_stack_detection(),
        ]
        with self.assertRaisesRegex(MODULE.WorkflowError, "scope changed"):
            MODULE.command_pipeline(self.args)

    def test_single_member_native_clearance_does_not_expand_scope(self):
        for base_branch in ("main", "v143"):
            if self.path.exists():
                self.path.unlink()
            self.args.pipeline_iteration = 1
            self.metadata["base_branch"] = base_branch
            self.calls["stack_membership"].return_value = existing.native_stack_detection()
            self.first_sweep()
            self.metadata["head_sha"] = "c" * 40
            self.calls["live_mergeability"].reset_mock()
            with self.subTest(base=base_branch):
                self.assertEqual(0, MODULE.command_pipeline(self.args))
                self.assertTrue(all(
                    call.args[0]["number"] == 7
                    for call in self.calls["live_mergeability"].call_args_list
                ))
                self.assertFalse(MODULE.load_state(self.path)["pipeline"]["whole_stack"])
        self.calls["discover_conflict_task"].assert_not_called()

    def full_stack(self):
        self.args.whole_stack = True
        self.metadata.update(base_branch="v143")
        state = self.first_sweep()
        state["pipeline_native_scope"] = {
            "trunk": {"ref": "main", "sha": "0" * 40},
            "members": [
                {"pr_number": 19483, "head_ref": "v143", "direct_base_ref": "main"},
                {"pr_number": 7, "head_ref": "feature", "direct_base_ref": "v143"},
            ],
        }
        MODULE.save_state(self.path, state)
        lower = existing.pr_metadata(
            number=19483, pr_url="https://github.com/owner/repo/pull/19483",
            head_branch="v143", head_sha="b" * 40, base_sha="d" * 40,
            mergeable="MERGEABLE",
        )
        detection = existing.native_stack_detection(members=[
            {**lower, "base_sha": "cached-old-base"},
            {**self.metadata, "base_sha": "cached-old-prefix"},
        ])
        self.calls["stack_membership"].return_value = detection
        self.calls["base_ref_tip"].side_effect = lambda repo, ref: (
            "d" * 40 if ref == "main" else "b" * 40
        )
        self.calls["live_mergeability"].side_effect = lambda target, **kw: copy.deepcopy(
            lower if target["number"] == 19483 else self.metadata
        )
        return state, detection, lower

    def test_full_native_scope_revalidates_all_members_against_live_base_refs(self):
        self.full_stack()
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        self.assertEqual({7, 19483}, {
            call.args[0]["number"] for call in self.calls["live_mergeability"].call_args_list
        })
        self.assertEqual("b" * 40, MODULE.load_state(self.path)["attempt"]["base_sha"])
        self.calls["discover_conflict_task"].assert_not_called()

    def test_full_scope_rejects_changed_membership_or_conflicting_prefix(self):
        original, detection, lower = self.full_stack()
        for mutate in (
            lambda: detection["stack"]["members"].reverse(),
            lambda: lower.update(mergeable="CONFLICTING"),
        ):
            MODULE.save_state(self.path, copy.deepcopy(original))
            mutate()
            with self.subTest(mutation=mutate), self.assertRaises(MODULE.WorkflowError):
                MODULE.command_pipeline(self.args)
            self.assertIsNone(MODULE.cleared_head_sha(MODULE.load_state(self.path)))
            detection["stack"]["members"].sort(key=lambda m: 0 if m["number"] == 19483 else 1)
        self.calls["discover_conflict_task"].assert_not_called()

    def test_full_scope_cannot_expand_from_missing_or_malformed_native_evidence(self):
        original, _, _ = self.full_stack()
        for scope in (None, {}, {"trunk": {}}, {"trunk": {"ref": "main"}, "members": [None]}):
            MODULE.save_state(self.path, {**copy.deepcopy(original), "pipeline_native_scope": scope})
            with self.subTest(scope=scope), self.assertRaisesRegex(MODULE.WorkflowError, "scope changed"):
                MODULE.command_pipeline(self.args)
            self.assertIsNone(MODULE.cleared_head_sha(MODULE.load_state(self.path)))

    def test_published_terminal_preserves_managed_budget_without_dispatch(self):
        state = self.first_sweep()
        state.update(last_result="published", managed_attempts=2)
        state["agent_task"].update(
            result={"status": "success", "task": {"state": "completed"}}, task_id="completed-task",
            task_id_status="known",
        )
        MODULE.save_state(self.path, state)
        self.metadata["head_sha"] = "c" * 40
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        self.assertEqual(2, MODULE.load_state(self.path)["managed_attempts"])
        self.calls["discover_conflict_task"].assert_not_called()
