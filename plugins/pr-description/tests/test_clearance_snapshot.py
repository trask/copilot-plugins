import copy
from pathlib import Path
import unittest
from unittest import mock

from test_pr_description import MODULE, agent_task_preflight

AUTHENTICATED_PREFLIGHT = MODULE.agent_task_preflight


class ClearanceSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.live = agent_task_preflight()
        self.state = {
            **copy.deepcopy(self.live),
            "version": MODULE.STATE_VERSION,
            "kind": MODULE.RUN_KIND,
            "run_id": "description-1",
            "repo_root": "repo",
            "pipeline_run": "pipeline-1",
            "pipeline_iteration": 1,
            "pipeline_max_iterations": 2,
            "validated_head_sha": self.live["pr"]["head_sha"],
            "agent_task": {
                "status": "completed",
                "task": {"id": "task-1", "state": "completed"},
                "model": "gpt-5.6-sol",
                "github_mutation_policy": "allow",
            },
            "validation": {
                "mode": "no_change",
                "run_id": "description-1",
                **{key: self.live["pr"][key] for key in ("head_sha", "title", "body")},
            },
        }
        self.target = MODULE.parse_target("owner/repo#7")
        self.enterContext(mock.patch.object(MODULE, "require_tools"))
        self.preflight = self.enterContext(
            mock.patch.object(MODULE, "agent_task_preflight", side_effect=lambda *_: self.live)
        )
        self.save = self.enterContext(mock.patch.object(MODULE, "save_state"))
        self.launch = self.enterContext(mock.patch.object(MODULE, "discover_cloud_task"))

    def record(self):
        MODULE.record_clearance_snapshot(self.state, Path("repo"), self.target)

    def test_keep_and_applied_clearance_bind_exact_final_metadata(self):
        for mode in ("no_change", "applied"):
            with self.subTest(mode=mode):
                self.state["validation"]["mode"] = mode
                self.live["pr"].update(title="Title &amp; `literal` ", body="Body &lt;x&gt;\n\n")
                self.state["pr"].update(self.live["pr"])
                self.state["validation"].update(
                    {key: self.live["pr"][key] for key in ("title", "body")}
                )
                self.state["agent_task"]["preflight"] = agent_task_preflight()
                self.record()
                before = copy.deepcopy(self.state)
                result = MODULE.verify_clearance_snapshot(self.state)
                self.assertEqual("current", result["result"])
                self.assertEqual(result["expected_snapshot_sha256"], result["observed_snapshot_sha256"])
                self.assertEqual(before, self.state)
                self.assertEqual("Body &lt;x&gt;\n\n", before["validation"]["clearance_snapshot"]["body"])
        self.save.assert_not_called()
        self.launch.assert_not_called()

    def test_missing_historical_snapshot_is_not_reconstructed(self):
        self.assertEqual("unverified", MODULE.verify_clearance_snapshot(self.state)["result"])
        self.preflight.assert_not_called()
        self.assertNotIn("clearance_snapshot", self.state["validation"])

    def test_changed_or_unknown_input_never_clears(self):
        self.record()
        original = copy.deepcopy(self.live)
        mutations = {
            "head": lambda live: live["pr"]["head"].update(sha="9" * 40),
            "unknown base": lambda live: live["pr"]["base"].pop("sha"),
            "base ref": lambda live: live["pr"]["base"].update(ref="other"),
            "head ref": lambda live: live["pr"]["head"].update(ref="other"),
            "title": lambda live: live["pr"].update(title="Current title "),
            "body": lambda live: live["pr"].update(body="Current body\n"),
            "entity": lambda live: live["pr"].update(body="Current &amp; body"),
            "draft": lambda live: live["pr"].update(is_draft=True),
            "viewer": lambda live: live["viewer"].update(login="other"),
            "permission": lambda live: live["viewer"]["permissions"].update(push=False),
            "unknown permission": lambda live: live["viewer"]["permissions"].pop("push"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                self.live = copy.deepcopy(original)
                mutate(self.live)
                self.assertEqual("stale", MODULE.verify_clearance_snapshot(self.state)["result"])
        self.save.assert_not_called()
        self.launch.assert_not_called()

    def test_failed_task_or_changed_validation_cannot_reuse_snapshot(self):
        self.record()
        original = copy.deepcopy(self.state)
        for mutate in (
            lambda state: state["agent_task"].update(status="failed_after_mutation"),
            lambda state: state["agent_task"]["task"].update(state="failed"),
            lambda state: state["validation"].update(run_id="other"),
            lambda state: state["validation"].update(body="other"),
            lambda state: state["pr"]["base"].update(sha="9" * 40),
        ):
            self.state = copy.deepcopy(original)
            mutate(self.state)
            self.assertEqual("unverified", MODULE.verify_clearance_snapshot(self.state)["result"])

    def test_capture_accepts_moved_base_and_retains_pinned_provenance(self):
        pinned = self.state["pr"]["base"]["sha"]
        self.live["pr"]["base"]["sha"] = "9" * 40
        self.record()
        self.assertEqual(pinned, self.state["validation"]["clearance_snapshot"]["base_sha"])
        result = MODULE.verify_clearance_snapshot(self.state)
        self.assertEqual("current", result["result"])
        self.assertEqual(result["expected_snapshot_sha256"], result["observed_snapshot_sha256"])

    def test_live_base_binding_ignores_stale_reported_base_and_tip_movement(self):
        initial = copy.deepcopy(self.live)
        tip = initial["pr"]["base"]["sha"]
        reported = "8" * 40
        repository = {
            "role_name": initial["viewer"]["repository_role"],
            "permissions": initial["viewer"]["permissions"],
        }

        def gh_json(command):
            endpoint = command[1]
            if endpoint.endswith("/pulls/7"):
                return {
                    "state": "open",
                    "title": initial["pr"]["title"],
                    "body": initial["pr"]["body"],
                    "base": {"repo": {"full_name": "owner/repo"}, "ref": "main", "sha": reported},
                    "head": {"repo": {"full_name": "owner/repo"}, "ref": "feature", "sha": initial["pr"]["head_sha"]},
                }
            if endpoint == "repos/owner/repo":
                return repository
            if endpoint == "user":
                return {"login": "viewer"}
            if endpoint == "repos/owner/repo/git/ref/heads/feature":
                return {
                    "ref": "refs/heads/feature",
                    "object": {"type": "commit", "sha": initial["pr"]["head_sha"]},
                }
            self.assertEqual("repos/owner/repo/git/ref/heads/main", endpoint)
            return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": tip}}

        self.preflight.side_effect = AUTHENTICATED_PREFLIGHT
        with (
            mock.patch.object(MODULE, "metadata_for", return_value=initial["pr"]),
            mock.patch.object(MODULE, "gh_json", side_effect=gh_json),
        ):
            pinned = MODULE.agent_task_preflight(Path("repo"), self.target)
            self.assertEqual(tip, pinned["pr"]["base"]["sha"])
            self.assertNotEqual(reported, pinned["pr"]["base"]["sha"])
            self.state["pr"] = pinned["pr"]
            self.record()
            self.assertEqual("current", MODULE.verify_clearance_snapshot(self.state)["result"])
            tip = "9" * 40
            self.assertEqual("current", MODULE.verify_clearance_snapshot(self.state)["result"])
            del self.state["validation"]["clearance_snapshot"]
            self.record()
            self.assertEqual(
                initial["pr"]["base"]["sha"],
                self.state["validation"]["clearance_snapshot"]["base_sha"],
            )

    def test_status_exports_run_position_and_verification_without_writes(self):
        self.record()
        before = copy.deepcopy(self.state)
        with (
            mock.patch.object(MODULE, "load_state", return_value=self.state),
            mock.patch.object(MODULE, "emit") as emit,
        ):
            MODULE.command_status(MODULE.build_parser().parse_args([
                "status", "--state", "state.json", "--verify-clearance-snapshot",
            ]))
        result = emit.call_args.args[0]
        self.assertEqual("pipeline-1", result["pipeline_run"])
        self.assertEqual(1, result["pipeline_iteration"])
        self.assertEqual(2, result["pipeline_max_iterations"])
        self.assertEqual("current", result["clearance_verification"]["result"])
        self.assertEqual(before, self.state)
        self.save.assert_not_called()

    def test_unreadable_live_snapshot_is_an_explicit_error(self):
        self.record()
        self.preflight.side_effect = MODULE.WorkflowError("live base unavailable")
        with self.assertRaisesRegex(MODULE.WorkflowError, "live base unavailable"):
            MODULE.verify_clearance_snapshot(self.state)
