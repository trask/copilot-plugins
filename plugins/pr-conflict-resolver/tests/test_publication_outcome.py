import copy
from contextlib import ExitStack
import subprocess
import unittest
from unittest import mock

import test_pr_conflict_resolver as existing


MODULE = existing.MODULE


class PublicationOutcomeTest(unittest.TestCase):
    def setUp(self):
        self.root = existing.temporary_directory(self)
        self.path = self.root / "state.json"
        fixture = existing.ManagedConflictCoordinatorTest()
        self.request = fixture.request()
        self.refs = fixture.success_result(self.request)["generated"]["code_refs"]
        self.state = {
            "version": MODULE.STATE_VERSION, "attempts": 2, "managed_attempts": 2,
            "history": [], "repo_root": str(self.root),
            "agent_task": {
                "status": "verified", "code_refs": self.refs,
                "preflight": {"repository_root": str(self.root), "request": self.request},
            },
        }
        self.metadata = existing.pr_metadata()
        self.metadata.update(head_sha="c" * 40, base_sha="a" * 40)
        self.context = self.enterContext(ExitStack())
        self.sleep = self.context.enter_context(mock.patch.object(MODULE.time, "sleep"))
        self.mocks = {}
        for name, options in {
            "require_clean_worktree": {},
            "require_no_integration_in_progress": {},
            "require_live_conflict_guards": {},
            "require_stack_request_owner": {},
            "record_stack_member_clearances": {},
            "conflict_push_command": {"return_value": ["git", "push", "--atomic"]},
            "remote_publication_heads": {"side_effect": lambda *_: [
                item["new_sha"] for item in self.refs
            ]},
            "metadata_for": {"side_effect": lambda *_: copy.deepcopy(self.metadata)},
            "run": {"return_value": subprocess.CompletedProcess([], 0, "", "")},
            "git_try": {},
        }.items():
            self.mocks[name] = self.context.enter_context(mock.patch.object(MODULE, name, **options))

    def publish(self):
        result = MODULE.publish_conflict_result(self.path, self.state)
        persisted = MODULE.load_state(self.path)
        self.assertEqual(result["stage_outcome"], MODULE.stage_outcome(persisted))
        args = MODULE.build_parser().parse_args(["status", "--state", str(self.path)])
        with mock.patch.object(MODULE, "emit") as emit:
            MODULE.command_status(args)
        self.assertEqual(result["stage_outcome"], existing.emitted(emit)["stage_outcome"])
        self.assertEqual(2, persisted["attempts"])
        self.assertEqual(2, persisted["managed_attempts"])
        return result

    def test_live_mergeability_is_required_and_ci_is_not_consulted(self):
        for mergeable, outcome, marker in (
            ("MERGEABLE", "cleared", "c" * 40),
            ("CONFLICTING", "completed", None),
            ("UNKNOWN", "completed", None),
        ):
            with self.subTest(mergeable=mergeable):
                self.metadata["mergeable"] = mergeable
                self.metadata["statusCheckRollup"] = [{"conclusion": "FAILURE"}]
                result = self.publish()
                self.assertEqual(outcome, result["stage_outcome"])
                self.assertEqual(marker, MODULE.cleared_head_sha(self.state))
                self.assertEqual("a" * 40, self.state["attempt"]["base_sha"])

    def test_native_stack_binds_invoked_member_base_selection_and_owner(self):
        self.request["strategy"] = "native-stack"
        self.refs.insert(0, {**self.refs[0], "pr_number": 6, "new_sha": "d" * 40})
        self.refs[1]["base_sha"] = "d" * 40
        self.metadata["base_sha"] = "d" * 40
        authorization = {
            "request_sha256": "selected-request-digest",
            "owner": {"run_id": "run-1", "kind": "pr-stack-pipeline", "state": "owner.json"},
            "selected": [6, 7], "topology_fingerprint": "ordered-topology",
        }
        self.state["agent_task"]["preflight"]["stack_request"] = authorization
        result = self.publish()
        self.assertEqual("c" * 40, result["head_sha"])
        publication = self.state["agent_task"]["publication"]
        self.assertEqual(7, publication["invoked_pr"])
        self.assertEqual([6, 7], [item["number"] for item in publication["members"]])
        self.assertEqual("d" * 40, publication["members"][1]["base_sha"])
        self.assertEqual(authorization, publication["stack_authorization"])
        self.mocks["require_stack_request_owner"].assert_called_with(authorization)

        self.state["agent_task"]["preflight"]["stack_request"] = {
            **authorization, "selected": [7, 6],
        }
        self.assertEqual("escalated", MODULE.stage_outcome(self.state))
        self.assertIsNone(MODULE.cleared_head_sha(self.state))

    def test_post_push_unknown_is_bounded_observation_not_another_task(self):
        self.mocks["metadata_for"].side_effect = [
            {**self.metadata, "mergeable": "UNKNOWN"},
            {**self.metadata, "mergeable": "MERGEABLE"},
        ]
        with mock.patch.object(MODULE, "discover_conflict_task") as discover:
            self.assertEqual("cleared", self.publish()["stage_outcome"])
        self.sleep.assert_called_once()
        discover.assert_not_called()

    def test_owner_failure_does_not_complete_or_discard_evidence(self):
        self.state["agent_task"]["preflight"]["stack_request"] = {
            "request_sha256": "digest", "owner": {"run_id": "run-1"},
            "selected": [7], "topology_fingerprint": "topology",
        }
        self.mocks["require_stack_request_owner"].side_effect = MODULE.WorkflowError("owner changed")
        with self.assertRaisesRegex(MODULE.WorkflowError, "owner changed"):
            MODULE.publish_conflict_result(self.path, self.state)
        self.assertIsNone(MODULE.stage_outcome(MODULE.load_state(self.path)))
        self.mocks["git_try"].assert_not_called()

    def test_stale_base_preserves_evidence_without_terminal_clearance(self):
        self.metadata["base_sha"] = "e" * 40
        evidence = self.root / "candidate.json"
        evidence.write_text("retained", encoding="utf-8")
        self.state["agent_task"]["recovery_files"] = [str(evidence)]
        with self.assertRaisesRegex(MODULE.WorkflowError, "head, base, or member"):
            MODULE.publish_conflict_result(self.path, self.state)
        self.assertEqual("retained", evidence.read_text(encoding="utf-8"))
        self.assertIsNone(MODULE.stage_outcome(MODULE.load_state(self.path)))
        self.assertIsNone(MODULE.cleared_head_sha(self.state))
        self.mocks["git_try"].assert_not_called()

    def test_partial_or_failed_push_never_records_clearance(self):
        for heads in (["unexpected"], ["b" * 40]):
            with self.subTest(heads=heads):
                self.mocks["remote_publication_heads"].side_effect = None
                self.mocks["remote_publication_heads"].return_value = heads
                with self.assertRaises(MODULE.WorkflowError):
                    MODULE.publish_conflict_result(self.path, self.state)
                self.assertIsNone(MODULE.stage_outcome(self.state))
                self.assertIsNone(MODULE.cleared_head_sha(self.state))

    def test_member_change_after_publication_does_not_complete(self):
        self.mocks["remote_publication_heads"].side_effect = [["c" * 40], ["changed"]]
        with self.assertRaisesRegex(MODULE.WorkflowError, "heads changed"):
            MODULE.publish_conflict_result(self.path, self.state)
        self.assertIsNone(MODULE.stage_outcome(MODULE.load_state(self.path)))
        self.mocks["git_try"].assert_not_called()

    def test_missing_invoked_member_cannot_publish(self):
        self.refs[0]["pr_number"] = 8
        with self.assertRaisesRegex(MODULE.WorkflowError, "invoked pull request"):
            MODULE.publish_conflict_result(self.path, self.state)
        self.mocks["run"].assert_not_called()
        self.mocks["remote_publication_heads"].assert_not_called()

    def test_incomplete_failed_and_changed_receipts_cannot_reuse_clearance(self):
        self.metadata["mergeable"] = "MERGEABLE"
        self.publish()
        for change, expected in (
            (lambda s: s["agent_task"].pop("publication"), None),
            (lambda s: s["agent_task"].update(status="published_pending_verification"), None),
            (lambda s: s["agent_task"].update(status="unrecognized"), None),
            (lambda s: s["agent_task"].update(status="failed"), "escalated"),
            (lambda s: s["agent_task"].update(status="interrupted"), "escalated"),
            (lambda s: s["pr"].update(head_sha="changed"), "escalated"),
            (lambda s: s["pr"].update(base_sha="changed"), "escalated"),
            (lambda s: s["agent_task"]["code_refs"][0].update(new_sha="changed"), "escalated"),
            (lambda s: s["agent_task"]["preflight"]["request"].update(request_id="changed"), "escalated"),
        ):
            changed = copy.deepcopy(self.state)
            change(changed)
            with self.subTest(change=change):
                self.assertEqual(expected, MODULE.stage_outcome(changed))
                self.assertIsNone(MODULE.cleared_head_sha(changed))
