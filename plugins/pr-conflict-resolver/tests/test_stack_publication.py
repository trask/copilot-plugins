import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

import test_pr_conflict_resolver as existing


MODULE = existing.MODULE


def write_authorization(root, stack, *, fixed=11, selected=None, operation="descendant-propagation"):
    selected = selected or [member["number"] for member in stack["members"]]
    state_path = root / "propagation.json"
    owner_path = root / "owner.json"
    request_path = root / "request.json"
    request = {
        "schema": {"id": "github.copilot.stack-publication-request", "version": 1},
        "operation": operation,
        "request_id": "run-1-propagation",
        "request_sha256": "",
        "owner": {"kind": "pr-stack-pipeline", "run_id": "run-1", "state": str(owner_path)},
        "repository": "owner/repo",
        "selected": selected,
        "topology_fingerprint": MODULE.stack_topology_fingerprint(stack),
        "source_stack": copy.deepcopy(stack),
        "source_snapshot": MODULE.stack_snapshot_fingerprint(stack),
        "fixed_pr": fixed,
        "fixed_head": next(member["head_sha"] for member in stack["members"] if member["number"] == fixed),
        "state": str(state_path),
    }
    request["request_sha256"] = MODULE.request_digest(request)
    request_path.write_text(json.dumps(request), encoding="utf-8")
    owner = {
        "kind": "pr-stack-pipeline", "run_id": "run-1", "result": None,
        "stack_owner_pid": os.getpid(),
        "stack_owner_recorded_at": time.time(),
        "kickoff": {"repository": "owner/repo", "pullRequests": selected},
        "topology_fingerprint": request["topology_fingerprint"],
        "stack_requests": {request["request_id"]: request["request_sha256"]},
    }
    owner_path.write_text(json.dumps(owner), encoding="utf-8")
    return request, request_path, state_path, owner_path


class StackPublicationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.stack = existing.DescendantPropagationTest().stack()
        self.request, self.request_path, self.state_path, self.owner_path = write_authorization(
            self.root, self.stack, selected=[11, 12]
        )
        self.args = MODULE.build_parser().parse_args([
            "descendant-propagate", "owner/repo#11", "--fixed-pr", "11",
            "--expected-head", "fixed-head", "--stack-number", "77",
            "--state", str(self.state_path), "--stack-request", str(self.request_path),
        ])
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.calls = {}
        for name, options in {
            "require_tools": {},
            "resolve_repo_root": {"return_value": self.root},
            "local_object_source": {"return_value": None},
            "metadata_for": {"return_value": existing.pr_metadata(number=11)},
            "stack_membership": {"side_effect": lambda *a: {
                "stack": copy.deepcopy(self.stack), "default_branch": "main",
            }},
            "external_stack_dependents": {"return_value": []},
            "create_stack_workspace": {"return_value": self.workspace},
            "command_agent_task": {"side_effect": self.hosted},
            "run_stack_cascade": {"side_effect": AssertionError("local cascade must stay disabled")},
            "emit": {},
        }.items():
            patcher = mock.patch.object(MODULE, name, **options)
            self.calls[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def hosted(self, args, *, result_sink):
        self.assertEqual("owner/repo#12", args.target)
        self.assertEqual(1, args.max_iterations)
        self.assertEqual("run-1", args.invocation_run)
        self.assertEqual(self.request, args._propagation_request)
        state = MODULE.load_state(self.state_path)
        state["agent_task"] = {
            "status": "completed",
            "code_refs": [{"pr_number": 12, "new_sha": "published-tip"}],
        }
        MODULE.save_state(self.state_path, state)
        result_sink({"result": "published"})

    def run_cli(self):
        with mock.patch.object(sys, "argv", ["resolver",
            "descendant-propagate", "owner/repo#11", "--fixed-pr", "11",
            "--expected-head", "fixed-head", "--stack-number", "77",
            "--state", str(self.state_path), "--stack-request", str(self.request_path),
        ]):
            return MODULE.main()

    def test_real_cli_admits_only_bound_hosted_propagation(self):
        self.assertEqual(0, self.run_cli())
        self.calls["command_agent_task"].assert_called_once()
        self.calls["run_stack_cascade"].assert_not_called()
        result = self.calls["emit"].call_args.args[0]
        self.assertEqual([{"number": 12, "head_sha": "published-tip"}], result["members_published"])
        self.assertFalse(self.workspace.exists())

    def test_cli_rejects_unbound_and_other_legacy_commands(self):
        with mock.patch.object(
            sys,
            "argv",
            ["resolver", "descendant-propagate", "owner/repo#11"],
        ):
            self.assertEqual(1, MODULE.main())
        for command in (
            ["stack-format", "--state", "old.json", "--no-format"],
            ["stack-continue", "--state", "old.json"],
        ):
            with (
                self.subTest(command=command),
                mock.patch.object(sys, "argv", ["resolver", *command]),
                self.assertRaises(SystemExit),
            ):
                MODULE.main()
        self.calls["create_stack_workspace"].assert_not_called()
        self.calls["command_agent_task"].assert_not_called()

    def test_topology_and_heads_cannot_change_before_child_capture(self):
        original = copy.deepcopy(self.stack)
        for change in (
            lambda s: s["members"].append({**s["members"][-1], "number": 13, "head_branch": "unselected"}),
            lambda s: s["members"].pop(0),
            lambda s: s["members"].reverse(),
            lambda s: s.update(id="another-stack"),
            lambda s: s["members"][0].update(head_branch="renamed-prefix"),
            lambda s: s["members"][-1].update(head_sha="external-head"),
        ):
            self.stack = copy.deepcopy(original)
            change(self.stack)
            self.stack["size"] = len(self.stack["members"])
            with self.subTest(change=change), self.assertRaises(MODULE.WorkflowError):
                MODULE.command_descendant_propagate(self.args)
        self.calls["create_stack_workspace"].assert_not_called()
        self.calls["command_agent_task"].assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_foreign_legacy_interrupted_and_landed_receipts_are_read_only(self):
        for status in ("planned", "resolved", "published_refs", "published", "interrupted"):
            for operation in ("descendant_propagation", "hosted_descendant_propagation"):
                state = {
                    "version": MODULE.STATE_VERSION, "operation": operation,
                    "status": status, "workspace": str(self.workspace),
                    "stack_request": {**self.request, "owner": {**self.request["owner"], "run_id": "old"}},
                    "retry_allowed": True, "agent_task": {"status": "verified"},
                }
                MODULE.save_state(self.state_path, state)
                before = self.state_path.read_bytes()
                with self.subTest(status=status, operation=operation), self.assertRaises(MODULE.WorkflowError):
                    MODULE.command_descendant_propagate(self.args)
                self.assertEqual(before, self.state_path.read_bytes())
                self.assertTrue(self.workspace.exists())
        self.calls["create_stack_workspace"].assert_not_called()
        self.calls["command_agent_task"].assert_not_called()

    def test_owner_run_selection_cancellation_and_liveness_fail_closed(self):
        original = json.loads(self.owner_path.read_text(encoding="utf-8"))
        for change in (
            lambda s: s.update(run_id="foreign"),
            lambda s: s.update(kind="native_stack"),
            lambda s: s.update(result={"result": "interrupted"}),
            lambda s: s.update(stack_owner_pid=-1),
            lambda s: s.update(stack_owner_recorded_at=1),
            lambda s: s.update(stack_requests={}),
            lambda s: s["kickoff"].update(pullRequests=[10, 11, 12]),
            lambda s: s.update(topology_fingerprint="different"),
        ):
            owner = copy.deepcopy(original)
            change(owner)
            self.owner_path.write_text(json.dumps(owner), encoding="utf-8")
            before = self.owner_path.read_bytes()
            with self.subTest(change=change), self.assertRaises(MODULE.WorkflowError):
                MODULE.command_descendant_propagate(self.args)
            self.assertEqual(before, self.owner_path.read_bytes())
        self.calls["create_stack_workspace"].assert_not_called()
        self.calls["command_agent_task"].assert_not_called()

    def test_changed_request_digest_fails_before_work(self):
        self.request["selected"].append(13)
        self.request_path.write_text(json.dumps(self.request), encoding="utf-8")
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.command_descendant_propagate(self.args)
        self.calls["create_stack_workspace"].assert_not_called()

    def test_same_active_run_retries_verified_receipts_without_hosted_work(self):
        code_refs = [{"pr_number": 12, "new_sha": "published-tip"}]
        state = {
            "version": MODULE.STATE_VERSION, "operation": "hosted_descendant_propagation",
            "stack_request": self.request, "retry_allowed": True, "workspace": str(self.workspace),
            "repo_root": str(self.workspace),
            "agent_task": {
                "status": "verified", "invocation_id": "run-1",
                "result": {}, "code_refs": code_refs, "artifact": {},
                "preflight": {
                    "stack_request": self.request, "request": {},
                    "repository_root": str(self.workspace),
                },
            },
        }
        MODULE.save_state(self.state_path, state)
        with mock.patch.object(MODULE, "validate_conflict_result_identity", return_value=(code_refs, {})), \
             mock.patch.object(MODULE, "verify_quarantined_result") as verify, \
             mock.patch.object(MODULE, "publish_conflict_result", return_value={"result": "published"}) as publish:
            MODULE.command_descendant_propagate(self.args)
        verify.assert_called_once()
        publish.assert_called_once()
        self.calls["create_stack_workspace"].assert_not_called()
        self.calls["command_agent_task"].assert_not_called()

    def test_interrupted_same_run_cannot_reuse_verified_candidate(self):
        MODULE.save_state(self.state_path, {
            "version": MODULE.STATE_VERSION, "operation": "hosted_descendant_propagation",
            "stack_request": self.request, "retry_allowed": False, "workspace": str(self.workspace),
            "agent_task": {"status": "verified"},
        })
        before = self.state_path.read_bytes()
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.command_descendant_propagate(self.args)
        self.assertEqual(before, self.state_path.read_bytes())
        self.calls["command_agent_task"].assert_not_called()

    def test_publication_guard_rejects_changes_during_hosted_work(self):
        preflight = {
            "stack_request": self.request,
            "request": {"pull_request": {"url": "https://github.com/owner/repo/pull/12"}},
        }
        for change in (
            lambda: self.stack["members"].reverse(),
            lambda: self.stack["members"][-1].update(head_sha="moved"),
        ):
            self.stack = copy.deepcopy(self.request["source_stack"])
            change()
            with self.subTest(change=change), mock.patch.object(MODULE, "git") as git, \
                 self.assertRaises(MODULE.WorkflowError):
                MODULE.require_live_conflict_guards(self.workspace, preflight)
            git.assert_not_called()

    def test_full_stack_guard_rejects_expansion_before_checkout_or_hosted_work(self):
        request, path, _, _ = write_authorization(
            self.root, self.stack, operation="whole-stack"
        )
        self.stack["members"].append({**self.stack["members"][-1], "number": 13})
        with mock.patch.object(MODULE, "checkout_pr_branch") as checkout, \
             self.assertRaises(MODULE.WorkflowError):
            MODULE.conflict_preflight(
                self.workspace, MODULE.parse_target("owner/repo#11"),
                requested_strategy="auto", whole_stack=True, iteration_id="iteration",
                iteration_number=1, iteration_budget=2, model="gpt-5.6-sol",
                stack_request=request,
            )
        checkout.assert_not_called()

    def test_hosted_request_and_atomic_push_include_only_authorized_descendants(self):
        for index, member in enumerate(self.stack["members"]):
            member["head_sha"] = chr(ord("a") + index) * 40
            member["base_sha"] = chr(ord("0") if index == 0 else ord("a") + index - 1) * 40
        self.stack["members"].append({
            "number": 13, "head_branch": "tip2", "base_branch": "tip",
            "head_sha": "d" * 40, "base_sha": "c" * 40,
        })
        self.stack["size"] = 4
        request, _, _, _ = write_authorization(self.root, self.stack, selected=[11, 12, 13])
        metadata = existing.pr_metadata(
            number=12, pr_url="https://github.com/owner/repo/pull/12",
            head_branch="tip", head_sha="c" * 40, base_branch="fixed",
            base_sha="b" * 40, mergeable="MERGEABLE",
        )
        self.calls["metadata_for"].return_value = metadata
        with (
            mock.patch.object(MODULE, "require_clean_worktree"),
            mock.patch.object(MODULE, "require_no_integration_in_progress"),
            mock.patch.object(MODULE, "live_mergeability", return_value=metadata),
            mock.patch.object(MODULE, "checkout_pr_branch"),
            mock.patch.object(MODULE, "conflict_preflight_identity", return_value={}),
            mock.patch.object(MODULE, "find_remote", return_value="origin"),
            mock.patch.object(MODULE, "fetch_preflight_ref"),
            mock.patch.object(MODULE, "stack_relations", return_value=existing.NO_RELATIONS),
            mock.patch.object(MODULE, "repository_merge_methods", return_value=existing.ALL_MERGE_METHODS),
            mock.patch.object(MODULE, "git", return_value="f" * 40),
            mock.patch.object(MODULE, "ordered_commits", return_value=[]),
            mock.patch.object(MODULE, "merge_tree_conflicts", return_value=[]),
            mock.patch.object(MODULE, "base_ref_tip", side_effect=lambda repo, ref: {
                "fixed": "b" * 40, "tip": "c" * 40,
            }[ref]),
            mock.patch.object(MODULE, "native_stack_member_history", side_effect=lambda root, **kw: (
                "f" * 40, [kw["head"]], [],
            )),
            mock.patch.object(MODULE, "commit_identity", side_effect=lambda root, sha, **kw: {
                "sha": sha, "subject": "Change", "trailers": [],
                "patch_sha256": "a" * 64, "paths": ["file.txt"],
            }),
        ):
            preflight = MODULE.conflict_preflight(
                self.workspace, MODULE.parse_target("owner/repo#12"),
                requested_strategy="auto", whole_stack=True, iteration_id="propagation",
                iteration_number=1, iteration_budget=1, model="gpt-5.6-sol",
                stack_request=request,
            )
            hosted = preflight["request"]
            existing.CLOUD_MODULE.validate_request(
                hosted, expected_strategy="native-stack", expected_model="gpt-5.6-sol",
                expected_pr_url=metadata["pr_url"], expected_policy=MODULE.CONFLICT_POLICY_IDENTITY,
            )
            command = MODULE.conflict_push_command(self.workspace, hosted, [
                {"pr_number": 12, "new_sha": "e" * 40, "lease_sha": "c" * 40},
                {"pr_number": 13, "new_sha": "f" * 40, "lease_sha": "d" * 40},
            ])
        self.assertEqual({"ref": "fixed", "sha": "b" * 40}, hosted["native_stack"]["trunk"])
        self.assertEqual([12, 13], [member["pr_number"] for member in hosted["native_stack"]["members"]])
        self.assertIn("--atomic", command)
        self.assertIn("--force-with-lease=refs/heads/tip:" + "c" * 40, command)
        self.assertIn("--force-with-lease=refs/heads/tip2:" + "d" * 40, command)
        self.assertFalse(any("refs/heads/fixed" in arg or "refs/heads/lower" in arg for arg in command))

    def test_owner_cancellation_blocks_publication(self):
        cancel = self.root / "cancel.json"
        self.request["owner"]["cancellation"] = str(cancel)
        self.request["request_sha256"] = MODULE.request_digest(self.request)
        owner = json.loads(self.owner_path.read_text(encoding="utf-8"))
        owner["stack_requests"][self.request["request_id"]] = self.request["request_sha256"]
        self.owner_path.write_text(json.dumps(owner), encoding="utf-8")
        cancel.write_text(json.dumps({"run_id": "run-1", "status": "requested"}), encoding="utf-8")
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.require_authorized_stack(self.request, existing.pr_metadata(number=11), self.stack)

    def test_full_source_snapshot_includes_inactive_unselected_prefix(self):
        projected = {**self.stack, "members": self.stack["members"][1:], "size": 2,
                     "source_stack": copy.deepcopy(self.stack)}
        MODULE.require_authorized_stack(self.request, existing.pr_metadata(number=11), projected)
        projected["source_stack"]["members"][0]["head_branch"] = "changed-prefix"
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.require_authorized_stack(self.request, existing.pr_metadata(number=11), projected)

    def test_existing_legacy_default_is_never_an_execution_input(self):
        legacy = self.root / "legacy.json"
        legacy.write_text('{"status":"resolved","workspace":"preserved"}', encoding="utf-8")
        with mock.patch.object(MODULE, "default_propagation_state_path", return_value=legacy) as default:
            self.assertEqual(0, self.run_cli())
        default.assert_not_called()
        self.assertEqual('{"status":"resolved","workspace":"preserved"}', legacy.read_text(encoding="utf-8"))

    def test_retry_cannot_reuse_a_changed_cached_push_command(self):
        state = {
            "agent_task": {
                "preflight": {
                    "repository_root": str(self.workspace),
                    "stack_request": self.request,
                    "request": {"pull_request": {"number": 12}},
                },
                "code_refs": [{"pr_number": 12, "lease_sha": "old", "new_sha": "new"}],
                "push_command": ["git", "push", "unselected"],
            },
        }
        with (
            mock.patch.object(MODULE, "require_clean_worktree"),
            mock.patch.object(MODULE, "require_no_integration_in_progress"),
            mock.patch.object(MODULE, "require_live_conflict_guards"),
            mock.patch.object(MODULE, "remote_publication_heads", return_value=["old"]),
            mock.patch.object(MODULE, "conflict_push_command", return_value=["git", "push", "--atomic"]),
            mock.patch.object(MODULE, "save_state") as save,
            mock.patch.object(MODULE, "run") as run,
            self.assertRaisesRegex(MODULE.WorkflowError, "publication command changed"),
        ):
            MODULE.publish_conflict_result(self.state_path, state)
        save.assert_not_called()
        run.assert_not_called()
