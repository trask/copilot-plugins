from __future__ import annotations

from collections.abc import Iterable

import pytest


GIT_E2E_SENTINELS = frozenset(
    {
        "plugins/pr-conflict-resolver/tests/test_native_stack_noop.py::NativeStackNoopTest::test_fresh_normal_pipeline_preserves_heads_metadata_budget_and_truthful_status",
        "plugins/pr-conflict-resolver/tests/test_native_stack_noop.py::NativeStackNoopTest::test_equal_tree_changed_trunk_ancestry_still_dispatches",
        "plugins/pr-conflict-resolver/tests/test_sequential_stack.py::SequentialStackTest::test_whole_stack_uses_only_authoritative_branches_with_extra_fix_and_report",
        "plugins/pr-conflict-resolver/tests/test_sequential_stack.py::SequentialStackTest::test_publication_rejects_concurrent_writer_without_partial_push",
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::StackFormatCommandTest::test_a_failed_formatter_restores_the_workspace_and_can_be_retried",
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::StackFormatCommandTest::test_formatter_changes_are_committed_to_the_current_member",
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::StackFormatCommandTest::test_formatter_commit_hook_cannot_move_another_stack_ref",
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::StackValidationFixCommandTest::test_undeclared_changes_restore_the_workspace_and_stack_ref",
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::StackValidationFixCommandTest::test_multiple_validation_fixes_form_a_linear_final_member_history",
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::StackValidationFixCommandTest::test_moving_a_lower_stack_ref_rolls_back_every_member",
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::StackCascadeTopologyTest::test_a_moved_lower_branch_cascades_through_every_descendant",
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::StackCascadeTopologyTest::test_a_conflict_stops_with_zero_publication",
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::StackCascadeTopologyTest::test_a_stale_member_lease_rejects_every_update_atomically",
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::RealGitConflictTest::test_a_resolution_that_keeps_both_sides_is_recorded_and_committed",
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::RealGitConflictTest::test_aborting_restores_the_branch",
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::NativeStackSynchronizationMergeIntegrationTest::test_automatic_direct_base_sync_merge_is_safely_omitted",
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::NativeStackSynchronizationMergeIntegrationTest::test_sync_merge_with_resolution_changes_fails_closed",
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::ManagedTaskWorkingDirectoryTest::test_execute_rechecks_local_identity_through_the_exact_start_path",
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::HeadBranchHeldElsewhereTest::test_the_checkout_reaches_the_head_the_branch_is_held_elsewhere",
        "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::CompanionPathIntegrationTest::test_move_destination_can_preserve_the_base_change",
        "plugins/pr-pipeline/tests/test_pr_pipeline.py::WorktreeSafetyTest::test_unreachable_local_commit_is_not_discarded",
        "plugins/pr-pipeline/tests/test_pr_pipeline.py::WorktreeSafetyTest::test_detached_old_pr_head_moves_to_new_pr_head",
        "plugins/agent-tasks-runtime/tests/test_cloud_task.py::DetachedCandidateCheckoutTest::test_exact_head_detached_candidate_preserves_checkout",
        "plugins/agent-tasks-runtime/tests/test_cloud_task.py::DetachedCandidateCheckoutTest::test_detached_candidate_rejects_head_drift",
        "plugins/ci-fix-loop/tests/test_ci_fix_loop.py::CommitSuppressionTest::test_reports_a_skip_added_to_a_test_that_was_running",
        "plugins/ci-fix-loop/tests/test_ci_fix_loop.py::CommitSuppressionTest::test_an_honest_fix_passes",
    }
)

LEGACY_E2E_PREFIXES = (
    "plugins/pr-conflict-resolver/tests/test_native_stack_noop.py::NativeStackNoopTest::",
    "plugins/pr-conflict-resolver/tests/test_sequential_stack.py::SequentialStackTest::",
    "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::NativeStackSynchronizationMergeIntegrationTest::",
    "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::ManagedTaskWorkingDirectoryTest::",
    "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::CompanionPathIntegrationTest::",
    "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::RealGitConflictTest::",
    "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::HeadBranchHeldElsewhereTest::",
    "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::StackCascadeTopologyTest::",
    "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::StackFormatCommandTest::",
    "plugins/pr-conflict-resolver/tests/test_pr_conflict_resolver.py::StackValidationFixCommandTest::",
    "plugins/pr-pipeline/tests/test_pr_pipeline.py::WorktreeSafetyTest::",
    "plugins/agent-tasks-runtime/tests/test_cloud_task.py::DetachedCandidateCheckoutTest::",
    "plugins/ci-fix-loop/tests/test_ci_fix_loop.py::CommitSuppressionTest::",
    "plugins/copilot-review-loop/tests/test_hosted_review_candidate.py::HostedDispatcherOwnershipTest::test_native_windows_timeout_reaps_owned_descendant",
)

WINDOWS_E2E_PREFIXES = (
    "plugins/agent-tasks-runtime/tests/test_execution.py::WindowsRealProcessTest::",
)


def normalized_nodeid(item: pytest.Item) -> str:
    return item.nodeid.replace("\\", "/")


def starts_with_any(value: str, prefixes: Iterable[str]) -> bool:
    return any(value.startswith(prefix) for prefix in prefixes)


def pyramid_marker(nodeid: str) -> str | None:
    if starts_with_any(nodeid, WINDOWS_E2E_PREFIXES):
        return "windows_e2e"
    if nodeid in GIT_E2E_SENTINELS:
        return "git_e2e"
    if starts_with_any(nodeid, LEGACY_E2E_PREFIXES):
        return "legacy_e2e"
    return None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        marker = pyramid_marker(normalized_nodeid(item))
        if marker is not None:
            item.add_marker(getattr(pytest.mark, marker))
