import copy
from pathlib import Path
import unittest
from unittest import mock

import test_pr_conflict_resolver as existing


MODULE = existing.MODULE
CLOUD = existing.CLOUD_MODULE


class NativeStackClearanceDecisionTest(unittest.TestCase):
    def setUp(self):
        self.invoked = existing.pr_metadata(
            number=7,
            pr_url="https://github.com/owner/repo/pull/7",
            head_owner="owner",
            head_repo="repo",
            head_branch="lower",
            head_sha="b" * 40,
            base_branch="main",
            base_sha="a" * 40,
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
        )
        self.member = {
            "position": 0,
            "number": 7,
            "head_branch": "lower",
            "head_sha": "b" * 40,
            "base_branch": "main",
            "base_sha": "a" * 40,
            "mergeable": "MERGEABLE",
            "state": "OPEN",
        }
        self.target = MODULE.stack_member_target(self.invoked, 7)

    def test_member_observation_accepts_the_frozen_identity(self):
        MODULE.validate_native_stack_member_observation(
            copy.deepcopy(self.invoked),
            self.member,
            self.invoked,
            self.target,
        )

    def test_member_observation_rejects_every_identity_and_state_drift(self):
        defects = [
            ("head_sha", "c" * 40),
            ("head_branch", "other"),
            ("base_branch", "other"),
            ("head_owner", "foreign"),
            ("head_repo", "foreign"),
            ("repo_name", "foreign/repo"),
            ("upstream_owner", "foreign"),
            ("upstream_repo", "foreign"),
            ("number", 99),
            ("pr_url", "https://github.com/owner/repo/pull/99"),
            ("state", "CLOSED"),
        ]
        for field, value in defects:
            current = copy.deepcopy(self.invoked)
            current[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                MODULE.WorkflowError,
                "identity, head, or direct base changed",
            ):
                MODULE.validate_native_stack_member_observation(
                    current,
                    self.member,
                    self.invoked,
                    self.target,
                )

    def test_member_observation_allows_base_oid_movement(self):
        current = copy.deepcopy(self.invoked)
        current["base_sha"] = "d" * 40

        MODULE.validate_native_stack_member_observation(
            current,
            self.member,
            self.invoked,
            self.target,
        )

    def test_member_observation_rejects_unstable_mergeability(self):
        for mergeable in ("UNKNOWN", None, "BLOCKED"):
            current = copy.deepcopy(self.invoked)
            current["mergeable"] = mergeable
            with self.subTest(mergeable=mergeable), self.assertRaisesRegex(
                MODULE.WorkflowError,
                "stable native stack conflict status",
            ):
                MODULE.validate_native_stack_member_observation(
                    current,
                    self.member,
                    self.invoked,
                    self.target,
                )

    def test_alignment_requires_every_member_to_be_mergeable_on_its_parent(self):
        members = [
            {
                "direct_base_sha": "a" * 40,
                "merge_base": "a" * 40,
                "mergeable": "MERGEABLE",
            },
            {
                "direct_base_sha": "b" * 40,
                "merge_base": "b" * 40,
                "mergeable": "MERGEABLE",
            },
        ]
        self.assertTrue(MODULE.native_stack_members_aligned(members))
        for index, field, value in (
            (0, "merge_base", "c" * 40),
            (0, "mergeable", "CONFLICTING"),
            (1, "merge_base", "d" * 40),
            (1, "mergeable", "UNKNOWN"),
        ):
            changed = copy.deepcopy(members)
            changed[index][field] = value
            with self.subTest(index=index, field=field):
                self.assertFalse(MODULE.native_stack_members_aligned(changed))

    def test_refresh_rejects_topology_mergeability_and_dependent_drift(self):
        members = [
            {
                "position": 0,
                "number": 7,
                "head_branch": "lower",
                "head_sha": "b" * 40,
                "base_branch": "main",
                "base_sha": "a" * 40,
                "mergeable": "MERGEABLE",
                "state": "OPEN",
            },
            {
                "position": 1,
                "number": 8,
                "head_branch": "upper",
                "head_sha": "c" * 40,
                "base_branch": "lower",
                "base_sha": "b" * 40,
                "mergeable": "MERGEABLE",
                "state": "OPEN",
            },
        ]
        detection = existing.native_stack_detection(members=members)
        outside = [{"number": 9, "head_sha": "d" * 40}]
        MODULE.validate_native_stack_clearance_refresh(
            detection,
            copy.deepcopy(detection),
            outside,
            copy.deepcopy(outside),
        )
        mutations = [
            lambda value: value["stack"]["members"].reverse(),
            lambda value: value["stack"].update(id="different"),
            lambda value: value["stack"]["members"][0].update(position=9),
            lambda value: value["stack"]["members"][0].update(state="CLOSED"),
            lambda value: value["stack"]["members"][1].update(mergeable="CONFLICTING"),
        ]
        for mutate in mutations:
            refreshed = copy.deepcopy(detection)
            mutate(refreshed)
            with self.subTest(mutation=mutate), self.assertRaisesRegex(
                MODULE.WorkflowError,
                "scope changed",
            ):
                MODULE.validate_native_stack_clearance_refresh(
                    detection,
                    refreshed,
                    outside,
                    outside,
                )
        with self.assertRaisesRegex(MODULE.WorkflowError, "scope changed"):
            MODULE.validate_native_stack_clearance_refresh(
                detection,
                copy.deepcopy(detection),
                outside,
                [],
            )

    def test_refresh_allows_base_oid_movement(self):
        detection = existing.native_stack_detection()
        for member in detection["stack"]["members"]:
            member["mergeable"] = "MERGEABLE"
        refreshed = copy.deepcopy(detection)
        refreshed["stack"]["members"][0]["base_sha"] = "e" * 40

        MODULE.validate_native_stack_clearance_refresh(
            detection,
            refreshed,
            [],
            [],
        )


class NativeStackArtifactDecisionTest(unittest.TestCase):
    def setUp(self):
        self.request = existing.ManagedTaskPromptTest().minimal_request()
        self.request.update(
            strategy="native-stack",
            native_stack={
                "trunk": {"ref": "main", "sha": "a" * 40},
                "members": [
                    {
                        "pr_number": 7,
                        "repository": "owner/repo",
                        "head_ref": "lower",
                        "head_sha": "b" * 40,
                        "direct_base_ref": "main",
                        "direct_base_sha": "a" * 40,
                        "observed_base_sha": "a" * 40,
                        "history_boundary_sha": "a" * 40,
                        "direct_merge_base": "a" * 40,
                        "old_commits": [],
                        "sync_merges": [],
                        "lease_sha": "b" * 40,
                    },
                    {
                        "pr_number": 8,
                        "repository": "owner/repo",
                        "head_ref": "upper",
                        "head_sha": "c" * 40,
                        "direct_base_ref": "lower",
                        "direct_base_sha": "b" * 40,
                        "observed_base_sha": "b" * 40,
                        "history_boundary_sha": "b" * 40,
                        "direct_merge_base": "b" * 40,
                        "old_commits": [],
                        "sync_merges": [],
                        "lease_sha": "c" * 40,
                    },
                ],
                "outside_dependents": [],
            },
        )
        self.request["request_sha256"] = MODULE.request_digest(self.request)
        self.code_refs = [
            {
                "role": f"member:{number}",
                "pr_number": number,
                "ref": branch,
                "base_sha": base,
                "new_sha": new,
            }
            for number, branch, base, new in (
                (7, "copilot/lower", "a" * 40, "d" * 40),
                (8, "copilot/upper", "d" * 40, "e" * 40),
            )
        ]
        artifacts = []
        for index, (member, code_ref) in enumerate(
            zip(self.request["native_stack"]["members"], self.code_refs), 1
        ):
            projected = CLOUD.stack_member_request(
                self.request,
                member,
                code_ref["base_sha"],
            )
            task_id = f"task-{index}"
            artifacts.append(
                {
                    "pr_number": member["pr_number"],
                    "task": {
                        "id": task_id,
                        "url": None,
                        "state": "completed",
                        "base_ref": code_ref["base_sha"],
                        "base_sha": code_ref["base_sha"],
                    },
                    "request": {
                        "id": projected["request_id"],
                        "sha256": projected["request_sha256"],
                    },
                    "branch": code_ref["ref"],
                    "head_sha": code_ref["new_sha"],
                    "source_tip_sha": code_ref["new_sha"],
                    "report": None,
                    "attribution": {
                        "task_id": task_id,
                        "creator_id": 1,
                        "creator_login": "owner",
                    },
                }
            )
        self.artifact = {
            **{
                key: artifacts[-1][key]
                for key in (
                    "branch",
                    "head_sha",
                    "source_tip_sha",
                    "report",
                    "attribution",
                )
            },
            "members": artifacts,
        }

    def test_valid_sequence_preserves_member_order_and_task_ownership(self):
        self.assertEqual(
            self.artifact["members"],
            MODULE.validate_stack_task_artifacts(
                self.request,
                self.code_refs,
                self.artifact,
            ),
        )

    def test_sequence_rejects_missing_reordered_or_divergent_evidence(self):
        mutations = [
            lambda refs, artifact: artifact["members"].pop(),
            lambda refs, artifact: artifact["members"].reverse(),
            lambda refs, artifact: refs[1].update(base_sha="a" * 40),
            lambda refs, artifact: refs[0].update(role="member:8"),
            lambda refs, artifact: artifact["members"][1]["task"].update(id="task-1"),
            lambda refs, artifact: artifact["members"][0]["task"].update(
                base_ref="b" * 40,
                base_sha="b" * 40,
            ),
            lambda refs, artifact: artifact["members"][1].update(branch="copilot/lower"),
            lambda refs, artifact: artifact["members"][1].update(branch="upper"),
            lambda refs, artifact: artifact.update(head_sha="f" * 40),
        ]
        for mutate in mutations:
            refs = copy.deepcopy(self.code_refs)
            artifact = copy.deepcopy(self.artifact)
            mutate(refs, artifact)
            with self.subTest(mutation=mutate), self.assertRaises(MODULE.WorkflowError):
                MODULE.validate_stack_task_artifacts(
                    self.request,
                    refs,
                    artifact,
                )


class NativeStackTaskProgressDecisionTest(unittest.TestCase):
    def test_source_head_drift_records_the_completed_candidate_before_stopping(self):
        result = CLOUD.Result(
            schema=CLOUD.RESULT_SCHEMA,
            policy=CLOUD.POLICY,
            model="gpt-5.6-sol",
            repository="owner/repo",
            strategy="native-stack",
            request_id="request-1",
            request_sha256="a" * 64,
            pull_request={"number": 7},
        )
        code_ref = {"role": "member:7", "new_sha": "b" * 40}
        artifact = {"pr_number": 7, "head_sha": "b" * 40}
        drift = CLOUD.SourceHeadChanged(
            pr_number=7,
            expected_head="c" * 40,
            actual_head="d" * 40,
        )
        with (
            mock.patch.object(CLOUD, "atomic_write_json") as write,
            self.assertRaises(CLOUD.SourceHeadChanged) as failure,
        ):
            CLOUD.record_native_stack_member_result(
                mock.Mock(result_file=Path("result.json")),
                result,
                [],
                [],
                code_ref,
                artifact,
                drift,
            )
        self.assertIs(drift, failure.exception)
        self.assertEqual([code_ref], result.code_refs)
        self.assertEqual({"members": [artifact]}, result.artifact)
        write.assert_called_once_with(Path("result.json"), result.as_dict())


class ReplayAttributionDecisionTest(unittest.TestCase):
    def setUp(self):
        self.base = "a" * 40
        self.request = existing.ManagedTaskPromptTest().minimal_request()
        self.request["model"] = "gpt-5.6-sol"
        self.artifact = {
            "branch": "copilot/generated-task",
            "attribution": {
                "task_id": "task-1",
                "creator_id": 218610,
                "creator_login": "trask",
            },
        }
        self.task = {
            "id": "task-1",
            "state": "completed",
            "creator": {"id": 218610},
            "artifacts": [
                {
                    "provider": "github",
                    "type": "branch",
                    "data": {
                        "base_ref": self.base,
                        "head_ref": "copilot/generated-task",
                    },
                }
            ],
            "sessions": [
                {
                    "task_id": "task-1",
                    "state": "completed",
                    "model": "sweagent-capi:gpt-5.6-sol",
                    "base_ref": self.base,
                    "head_ref": "copilot/generated-task",
                }
            ],
        }

    def verify(self, artifact=None, task=None, user=None):
        with mock.patch.object(
            MODULE,
            "gh_json",
            side_effect=[
                self.task if task is None else task,
                {"id": 218610, "login": "trask"} if user is None else user,
            ],
        ):
            return MODULE.verify_replay_attribution(
                self.request,
                self.artifact if artifact is None else artifact,
                self.base,
            )

    def test_attribution_accepts_the_exact_task_session_and_creator(self):
        self.assertEqual(self.artifact["attribution"], self.verify())

    def test_attribution_rejects_forged_or_malformed_identity(self):
        mutations = [
            {"creator_id": 123},
            {"creator_login": "different"},
            {"task_id": "task-2"},
            {"creator_id": True},
            {"creator_login": "trask\nSigned-off-by: forged"},
            {"extra": "model declaration"},
        ]
        for mutation in mutations:
            artifact = copy.deepcopy(self.artifact)
            artifact["attribution"].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(
                MODULE.WorkflowError
            ):
                self.verify(artifact=artifact)

    def test_attribution_rejects_task_session_and_account_drift(self):
        task = copy.deepcopy(self.task)
        task["sessions"][0]["base_ref"] = "b" * 40
        with self.assertRaisesRegex(MODULE.WorkflowError, "session identity changed"):
            self.verify(task=task)
        with self.assertRaisesRegex(MODULE.WorkflowError, "creator account changed"):
            self.verify(user={"id": 218610, "login": "different"})

    def test_message_bytes_allow_only_the_verified_attribution_appendix(self):
        old = {
            "sha": "b" * 40,
            "subject": "Change",
            "trailers": ["Reviewed-by: Person"],
        }
        original = b"tree abc\n\nChange\n\nReviewed-by: Person\n"
        generated = (
            original
            + b"\nCo-authored-by: trask "
            b"<218610+trask@users.noreply.github.com>\n"
        )
        with (
            mock.patch.object(MODULE, "git_bytes", side_effect=[original, generated]),
            mock.patch.object(MODULE, "conflict_commit_subject", return_value="Change"),
            mock.patch.object(
                MODULE,
                "conflict_commit_trailers",
                return_value=["Reviewed-by: Person"],
            ),
        ):
            MODULE.verify_replay_message_bytes(
                Path("repo"),
                old,
                "c" * 40,
                self.artifact["attribution"],
            )
        with (
            mock.patch.object(
                MODULE,
                "git_bytes",
                side_effect=[original, generated.replace(b"trask", b"other")],
            ),
            mock.patch.object(MODULE, "conflict_commit_subject", return_value="Change"),
            mock.patch.object(
                MODULE,
                "conflict_commit_trailers",
                return_value=["Reviewed-by: Person"],
            ),
            self.assertRaisesRegex(MODULE.WorkflowError, "message bytes changed"),
        ):
            MODULE.verify_replay_message_bytes(
                Path("repo"),
                old,
                "c" * 40,
                self.artifact["attribution"],
            )


class TransactionDecisionTest(unittest.TestCase):
    def test_position_requires_the_expected_branch_and_head(self):
        MODULE.validate_transaction_position(
            branch="feature",
            head="a" * 40,
            expected_branch="feature",
            expected_head="a" * 40,
            operation="formatter",
        )
        for branch, head in (("other", "a" * 40), ("feature", "b" * 40)):
            with self.subTest(branch=branch, head=head), self.assertRaisesRegex(
                MODULE.WorkflowError,
                "moved the final branch or created a commit",
            ):
                MODULE.validate_transaction_position(
                    branch=branch,
                    head=head,
                    expected_branch="feature",
                    expected_head="a" * 40,
                    operation="formatter",
                )

    def test_refs_allow_only_the_declared_current_member_update(self):
        before = {"refs/heads/lower": "a" * 40, "refs/heads/upper": "b" * 40}
        MODULE.validate_transaction_refs(
            before,
            {"refs/heads/lower": "c" * 40, "refs/heads/upper": "b" * 40},
            operation="validation fix commit",
            mutable_ref="refs/heads/lower",
            mutable_value="c" * 40,
        )
        defects = [
            {"refs/heads/lower": "c" * 40},
            {"refs/heads/lower": "c" * 40, "refs/heads/upper": "d" * 40},
        ]
        for observed in defects:
            with self.subTest(observed=observed), self.assertRaisesRegex(
                MODULE.WorkflowError,
                "stack ref",
            ):
                MODULE.validate_transaction_refs(
                    before,
                    observed,
                    operation="validation fix commit",
                    mutable_ref="refs/heads/lower",
                    mutable_value="c" * 40,
                )

    def test_path_policy_distinguishes_subset_and_exact_transactions(self):
        MODULE.validate_transaction_paths(
            {"feature.py"},
            {"feature.py", "test_feature.py"},
            operation="formatter",
            exact=False,
        )
        MODULE.validate_transaction_paths(
            {"feature.py"},
            {"feature.py"},
            operation="validation fix",
            exact=True,
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "outside"):
            MODULE.validate_transaction_paths(
                {"feature.py", "main.py"},
                {"feature.py"},
                operation="formatter",
                exact=False,
            )
        for changed in (set(), {"feature.py", "other.py"}):
            with self.subTest(changed=changed), self.assertRaisesRegex(
                MODULE.WorkflowError,
                "exact declared path set",
            ):
                MODULE.validate_transaction_paths(
                    changed,
                    {"feature.py"},
                    operation="validation fix",
                    exact=True,
                )
