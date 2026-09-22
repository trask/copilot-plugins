import ast
import os
from pathlib import Path
import unittest
from unittest import mock

import conftest
from tools import validate


ROOT = Path(__file__).parents[1]

FAIL_CLOSED_COVERAGE = {
    "native clearance identity and mergeability drift": (
        "plugins/pr-conflict-resolver/tests/test_decision_seams.py",
        "NativeStackClearanceDecisionTest",
        "test_member_observation_rejects_every_identity_and_state_drift",
    ),
    "native clearance topology and owner drift": (
        "plugins/pr-conflict-resolver/tests/test_decision_seams.py",
        "NativeStackClearanceDecisionTest",
        "test_refresh_rejects_topology_mergeability_and_dependent_drift",
    ),
    "native sequence missing or reordered evidence": (
        "plugins/pr-conflict-resolver/tests/test_decision_seams.py",
        "NativeStackArtifactDecisionTest",
        "test_sequence_rejects_missing_reordered_or_divergent_evidence",
    ),
    "native sequence source drift": (
        "plugins/pr-conflict-resolver/tests/test_decision_seams.py",
        "NativeStackTaskProgressDecisionTest",
        "test_source_head_drift_records_the_completed_candidate_before_stopping",
    ),
    "native mergeability and ancestry decision": (
        "plugins/pr-conflict-resolver/tests/test_decision_seams.py",
        "NativeStackClearanceDecisionTest",
        "test_alignment_requires_every_member_to_be_mergeable_on_its_parent",
    ),
    "native replay task base": (
        "plugins/pr-conflict-resolver/tests/test_sequential_stack.py",
        "ReplayTaskBaseTest",
        "test_creation_and_collection_share_the_policy_10_task_base",
    ),
    "native replay completeness": (
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py",
        "PushRangeVerificationTest",
        "test_a_rebase_that_dropped_a_commit_is_refused",
    ),
    "native replay attribution identity": (
        "plugins/pr-conflict-resolver/tests/test_decision_seams.py",
        "ReplayAttributionDecisionTest",
        "test_attribution_rejects_forged_or_malformed_identity",
    ),
    "native replay message preservation": (
        "plugins/pr-conflict-resolver/tests/test_decision_seams.py",
        "ReplayAttributionDecisionTest",
        "test_message_bytes_allow_only_the_verified_attribution_appendix",
    ),
    "native publication command atomicity": (
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py",
        "ManagedConflictCoordinatorTest",
        "test_native_stack_publication_is_atomic_with_one_lease_per_branch",
    ),
    "formatter workspace rollback": (
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py",
        "StackFormatCommandTest",
        "test_a_failed_formatter_restores_the_workspace_and_can_be_retried",
    ),
    "formatter ref rollback": (
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py",
        "StackFormatCommandTest",
        "test_formatter_commit_hook_cannot_move_another_stack_ref",
    ),
    "validation fix workspace rollback": (
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py",
        "StackValidationFixCommandTest",
        "test_undeclared_changes_restore_the_workspace_and_stack_ref",
    ),
    "validation fix ref rollback": (
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py",
        "StackValidationFixCommandTest",
        "test_moving_a_lower_stack_ref_rolls_back_every_member",
    ),
    "atomic stack publication lease": (
        "plugins/pr-conflict-resolver/tests/test_sequential_stack.py",
        "SequentialStackTest",
        "test_publication_rejects_concurrent_writer_without_partial_push",
    ),
    "pipeline unpublished commit retention": (
        "plugins/pr-pipeline/tests/test_pr_pipeline.py",
        "WorktreeSafetyTest",
        "test_unreachable_local_commit_is_not_discarded",
    ),
    "runtime detached checkout drift": (
        "plugins/agent-tasks-runtime/tests/test_cloud_task.py",
        "DetachedCandidateCheckoutTest",
        "test_detached_candidate_rejects_head_drift",
    ),
    "test suppression detection": (
        "plugins/ci-fix-loop/tests/test_ci_fix_loop.py",
        "CommitSuppressionTest",
        "test_reports_a_skip_added_to_a_test_that_was_running",
    ),
    "hosted review task envelope drift": (
        "plugins/copilot-review-loop/tests/test_hosted_review_candidate.py",
        "HostedReviewCandidateTest",
        "test_identity_manifest_and_completion_drift_fail_before_import",
    ),
    "hosted review candidate history": (
        "plugins/copilot-review-loop/tests/test_copilot_review_loop.py",
        "AgentTaskCoordinatorTest",
        "test_rejects_merge_artifacts_and_unexpected_history",
    ),
    "hosted review candidate artifact identity": (
        "plugins/copilot-review-loop/tests/test_copilot_review_loop.py",
        "AgentTaskCoordinatorTest",
        "test_rejects_malformed_mismatched_and_credential_artifacts",
    ),
    "hosted review decision identity": (
        "plugins/copilot-review-loop/tests/test_hosted_review_candidate.py",
        "HostedReviewDecisionTest",
        "test_decisions_reject_missing_duplicate_unknown_and_malformed_findings",
    ),
    "hosted review verified commit selection": (
        "plugins/copilot-review-loop/tests/test_hosted_review_candidate.py",
        "HostedReviewDecisionTest",
        "test_fixed_decision_requires_a_verified_current_or_historical_commit",
    ),
    "hosted review source and GitHub drift": (
        "plugins/copilot-review-loop/tests/test_copilot_review_loop.py",
        "AgentTaskCoordinatorTest",
        "test_rejects_stale_head_threads_and_local_drift",
    ),
    "hosted review suppressed identity": (
        "plugins/copilot-review-loop/tests/test_copilot_review_loop.py",
        "AgentTaskCoordinatorTest",
        "test_decision_list_restores_suppressed_identities_by_opaque_id",
    ),
}


class TestPyramidContractTest(unittest.TestCase):
    @staticmethod
    def methods(path, class_name):
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        test_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        )
        return {
            node.name
            for node in test_class.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        }

    def test_every_fail_closed_branch_maps_to_a_live_test(self):
        for branch, (path, class_name, method) in FAIL_CLOSED_COVERAGE.items():
            with self.subTest(branch=branch):
                self.assertIn(method, self.methods(path, class_name))
                nodeid = f"{path}::{class_name}::{method}"
                self.assertNotEqual(
                    "legacy_e2e",
                    conftest.pyramid_marker(nodeid),
                    f"{branch} is excluded from required validation",
                )

    def test_each_legacy_family_retains_a_fast_git_sentinel(self):
        for prefix in conftest.LEGACY_E2E_PREFIXES:
            if "HostedDispatcherOwnershipTest" in prefix:
                continue
            with self.subTest(prefix=prefix):
                self.assertTrue(
                    any(nodeid.startswith(prefix) for nodeid in conftest.GIT_E2E_SENTINELS)
                )

    def test_windows_kernel_coverage_has_one_owner(self):
        self.assertEqual(
            (
                "plugins/agent-tasks-runtime/tests/test_execution.py::WindowsRealProcessTest::",
            ),
            conftest.WINDOWS_E2E_PREFIXES,
        )

    def test_validation_runner_drops_inherited_indexed_git_configuration(self):
        with mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_COUNT": "broken",
                "GIT_CONFIG_KEY_9": "core.fsmonitor",
                "GIT_CONFIG_VALUE_9": "",
                "PYTEST_KEEP_ME": "yes",
            },
            clear=True,
        ):
            environment = validate.pytest_environment()
        self.assertEqual("yes", environment["PYTEST_KEEP_ME"])
        self.assertNotIn("GIT_CONFIG_COUNT", environment)
        self.assertFalse(
            any(
                name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
                for name in environment
            )
        )
