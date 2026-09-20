import copy
from contextlib import redirect_stdout
from io import StringIO
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_pr_stack_pipeline import BASE, COMMON, MODULE, StackFixture, kickoff, load, stack


CONFLICT = load(
    "aggregate_conflict_source",
    Path(__file__).parents[2] / "pr-conflict-resolver" / "scripts" / "pr_conflict_resolver.py",
)


class NativeStackClearanceTest(StackFixture):
    def setUp(self):
        super().setUp()
        self.stack = stack(members=(11, 12))
        self.metadata = {}
        parent = BASE
        for member in self.stack["members"]:
            member.update(base_sha=parent, mergeable="MERGEABLE", is_draft=False)
            self.metadata[member["number"]] = {
                **member, **COMMON.target_for("owner/repo", member["number"]),
                "upstream_owner": "owner", "upstream_repo": "repo",
                "head_owner": "owner", "head_repo": "repo",
                "merge_state_status": "CLEAN",
            }
            parent = member["head_sha"]
        self.tips = {
            "main": BASE,
            **{member["head_branch"]: member["head_sha"] for member in self.stack["members"]},
        }
        self.controller = self.pipeline(
            kickoff(numbers=(11, 12)), inspect=self.inspect_receipt,
            base_tip=lambda repo, branch: self.tips[branch],
        )
        self.controller.repo_root.mkdir()
        self.launcher.on_start = self.complete
        self.inspections = []
        self.status_overrides = {}
        self.calls = {}
        patches = {
            (MODULE, "stage_state_path"): {"side_effect": self.state_path},
            (COMMON, "stage_accepts_pipeline_position"): {"return_value": True},
            (COMMON, "run"): {"side_effect": self.status_command},
            (CONFLICT, "run"): {"side_effect": AssertionError("unexpected external command")},
            (CONFLICT, "gh_json"): {"side_effect": AssertionError("unexpected network request")},
            (CONFLICT, "git"): {"side_effect": self.git},
            (CONFLICT, "require_tools"): {},
            (CONFLICT, "resolve_repo_root"): {"return_value": self.controller.repo_root.resolve()},
            (CONFLICT, "require_clean_worktree"): {},
            (CONFLICT, "require_no_integration_in_progress"): {},
            (CONFLICT, "checkout_pr_branch"): {},
            (CONFLICT, "conflict_preflight_identity"): {"return_value": {"head": self.stack["members"][0]["head_sha"]}},
            (CONFLICT, "metadata_for"): {"side_effect": lambda target: copy.deepcopy(self.metadata[target["number"]])},
            (CONFLICT, "stack_membership"): {"side_effect": lambda pr: {
                "default_branch": "main", "stack": copy.deepcopy(self.stack),
            }},
            (CONFLICT, "base_ref_tip"): {"side_effect": lambda repo, branch: self.tips[branch]},
            (CONFLICT, "find_remote"): {"return_value": "origin"},
            (CONFLICT, "fetch_preflight_ref"): {},
            (CONFLICT, "external_stack_dependents"): {"return_value": []},
            (CONFLICT, "stack_owner_is_running"): {"return_value": True},
            (CONFLICT, "discover_conflict_task"): {"side_effect": AssertionError("unexpected hosted task")},
            (CONFLICT, "publish_conflict_result"): {"side_effect": AssertionError("unexpected publication")},
            (CONFLICT, "write_result_file"): {},
            (CONFLICT, "emit"): {},
        }
        for (module, name), options in patches.items():
            patch = mock.patch.object(module, name, **options)
            self.calls[(module, name)] = patch.start()
            self.addCleanup(patch.stop)

    def state_path(self, entry, target, run_id=None):
        return self.root / "receipts" / str(run_id) / f"{entry['stage']}-{target['number']}.json"

    def receipt_path(self, stage, number=11):
        return self.state_path(
            MODULE.STAGE_BY_NAME[stage], COMMON.target_for("owner/repo", number), self.controller.run_id,
        )

    def git(self, root, *args):
        self.assertEqual(("merge-base", "--all"), args[:2])
        return args[2]

    def status_command(self, command, **kwargs):
        self.assertEqual("status", command[2])
        path = Path(command[command.index("--state") + 1])
        if path.name.startswith(MODULE.STAGE_CONFLICT):
            CONFLICT.command_status(CONFLICT.build_parser().parse_args(["status", "--state", str(path)]))
            payload = copy.deepcopy(self.calls[(CONFLICT, "emit")].call_args.args[0])
        else:
            payload = COMMON.read_json(path)
        payload.update(self.status_overrides.get(path, {}))
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    def inspect_receipt(self, entry, target, head, base):
        self.inspections.append((entry["stage"], target["number"], head, base))
        return COMMON.inspect_stage(
            entry, target, head, base, pipeline_run=self.controller.run_id,
            read_status=lambda stage, pr: COMMON.read_stage_status(
                stage, pr,
                script_for=lambda selected: (
                    Path(__file__).parents[2] / selected["plugin"] / "scripts" / f"{selected['module']}.py"
                ),
                state_for=lambda stage, pr: self.state_path(stage, pr, self.controller.run_id),
            ),
        )

    def complete(self, request):
        path = self.receipt_path(request["stage"], request["number"])
        if request["stage"] == MODULE.STAGE_CONFLICT:
            args = CONFLICT.build_parser().parse_args([
                "pipeline", f"owner/repo#{request['number']}",
                "--repo-root", str(self.controller.repo_root),
                *request["arguments"],
            ])
            self.assertEqual(0, CONFLICT.command_pipeline(args))
            return
        entry = MODULE.STAGE_BY_NAME[request["stage"]]
        payload = {
            "result": "ready", "state": str(path),
            "pr": COMMON.target_for("owner/repo", request["number"]),
            "stage_outcome": "cleared",
        }
        for keys, value in ((entry["marker"], request["head_sha"]), (entry.get("base_marker"), request["base_sha"])):
            if keys:
                node = payload
                for key in keys[:-1]:
                    node = node.setdefault(key, {})
                node[keys[-1]] = value
        if request["stage"] == MODULE.STAGE_CI:
            payload["clearance_verification"] = {
                "result": "current", "reason": "ci_snapshot_current",
                "expected_snapshot_sha256": "a" * 64, "observed_snapshot_sha256": "a" * 64,
            }
        if request["stage"] == MODULE.STAGE_DESCRIPTION:
            payload.update(
                run_id=f"description-{request['number']}",
                pipeline_run=self.controller.run_id,
                pipeline_iteration=request["pass"], pipeline_max_iterations=MODULE.MAX_PASSES,
                agent_task={
                    "status": "completed", "model": "gpt-5.6-sol",
                    "github_mutation_policy": "allow", "task": {"id": request["nonce"], "state": "completed"},
                },
                clearance_verification={
                    "result": "current", "reason": "description_snapshot_current",
                    "expected_snapshot_sha256": "a" * 64, "observed_snapshot_sha256": "a" * 64,
                },
            )
        COMMON.write_json_atomically(path, payload)

    def phase(self):
        self.controller.state["pass"] = 1
        return self.controller.run_conflict_phase(1, self.stack["members"])

    def fill_other_stages(self):
        for member in self.stack["members"]:
            for stage in MODULE.STAGE_NAMES[1:]:
                self.complete({
                    "number": member["number"], "head_sha": member["head_sha"],
                    "base_sha": member["base_sha"], "stage": stage,
                    "pass": 1, "nonce": f"{stage}-{member['number']}",
                })

    def assert_incomplete(self):
        result = self.controller.final_snapshot()
        self.assertEqual("incomplete", result["result"])
        self.assertFalse(any(
            stage.get("clearance_kind") == "native_stack_clearance"
            for member in result.get("pull_requests", []) for stage in member["stages"]
        ))
        return result

    def test_normal_producer_completes_controller_without_child_receipt_or_hosted_work(self):
        output = StringIO()
        with redirect_stdout(output):
            result = self.controller.execute()
        self.assertEqual("", output.getvalue())
        self.assertEqual("complete", result["result"])
        self.assertEqual(1, result["passes"])
        self.assertEqual(2, MODULE.MAX_PASSES)
        self.assertEqual(5, len(result["phases"]))
        self.assertEqual([1, 2, 2, 2, 2], [phase["dispatches"] for phase in result["phases"]])
        for member in result["snapshot"]["pull_requests"]:
            self.assertEqual([], member["uncleared"])
            self.assertEqual(list(MODULE.STAGE_NAMES), [stage["stage"] for stage in member["stages"]])
            for stage in member["stages"]:
                self.assertTrue(stage["clear"])
                self.assertEqual(member["head_sha"], stage["clear_at_head_sha"])
                self.assertEqual(member["head_sha"], stage["inspected_head_sha"])
                self.assertEqual(member["base_sha"], stage["inspected_base_sha"])
        child = result["snapshot"]["pull_requests"][1]["stages"][0]
        self.assertEqual("native_stack_clearance", child["clearance_kind"])
        self.assertEqual(self.stack["members"][1]["base_sha"], child["clear_at_base_sha"])
        self.assertEqual(11, child["clearance_source"]["number"])
        self.assertEqual({}, child["status"])
        self.assertFalse(self.receipt_path(MODULE.STAGE_CONFLICT, 12).exists())
        producer = COMMON.read_json(self.receipt_path(MODULE.STAGE_CONFLICT))
        self.assertEqual(0, producer["managed_attempts"])
        self.assertEqual(2, producer["pipeline"]["budget"])
        self.assertEqual("already_mergeable", producer["agent_task"]["outcome"])
        self.assertIsNone(producer["attempt"]["published_head_sha"])
        self.assertIsNone(producer["agent_task"]["task_id"])
        for name in ("run", "gh_json", "discover_conflict_task", "publish_conflict_result"):
            self.calls[(CONFLICT, name)].assert_not_called()
        self.assertEqual([], list(self.controller.repo_root.rglob("*")))

    def test_snapshot_is_read_only_and_retains_no_state_without_accepted_evidence(self):
        self.phase()
        self.fill_other_stages()
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        state = copy.deepcopy(self.controller.state)
        with redirect_stdout(StringIO()) as output:
            self.assertEqual("complete", self.controller.final_snapshot()["result"])
        self.assertEqual("", output.getvalue())
        self.assertEqual(state, self.controller.state)
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})
        self.controller.state["pull_requests"] = {}
        result = self.assert_incomplete()
        self.assertEqual("no_state", result["pull_requests"][1]["stages"][0]["reason"])
        self.assertEqual([MODULE.STAGE_CONFLICT], result["pull_requests"][1]["uncleared"])

    def test_without_aggregate_consumption_reproduces_two_passes_with_clear_phases_and_partial(self):
        with mock.patch.object(self.controller, "apply_native_stack_clearance"):
            result = self.controller.execute()
        self.assertEqual("partial", result["result"])
        self.assertEqual("two_passes_finished", result["reason"])
        self.assertEqual(2, result["passes"])
        self.assertEqual(10, len(result["phases"]))
        self.assertTrue(all(phase["clear"] for phase in result["phases"]))
        self.assertEqual([], result["snapshot"]["pull_requests"][0]["uncleared"])
        self.assertEqual([MODULE.STAGE_CONFLICT], result["snapshot"]["pull_requests"][1]["uncleared"])
        self.assertEqual("no_state", result["snapshot"]["pull_requests"][1]["stages"][0]["reason"])
        self.assertEqual(0, result["phases"][5]["dispatches"])
        self.assertEqual(0, result["phases"][9]["dispatches"])
        self.assertEqual([11, 12], result["phases"][9]["reused"])

    def test_second_pass_uses_same_aggregate_without_resetting_budget_or_description_reuse(self):
        def complete(request):
            self.complete(request)
            if request["stage"] == MODULE.STAGE_DESCRIPTION and request["number"] == 12 and request["pass"] == 1:
                path = self.receipt_path(MODULE.STAGE_COPILOT_REVIEW, 12)
                payload = COMMON.read_json(path)
                payload["stage_outcome"] = "completed"
                COMMON.write_json_atomically(path, payload)

        self.launcher.on_start = complete
        result = self.controller.execute()
        self.assertEqual("complete", result["result"])
        self.assertEqual(2, result["passes"])
        self.assertEqual(1, sum(r["stage"] == MODULE.STAGE_CONFLICT for r in self.launcher.started))
        self.assertEqual(0, result["phases"][9]["dispatches"])
        self.assertEqual([11, 12], result["phases"][9]["reused"])
        producer = COMMON.read_json(self.receipt_path(MODULE.STAGE_CONFLICT))
        self.assertEqual(1, producer["pipeline"]["iteration"])
        self.assertEqual(2, producer["pipeline"]["budget"])
        self.assertEqual(0, producer["managed_attempts"])
        self.assertTrue(result["snapshot"]["pull_requests"][1]["stages"][0]["clear"])

    def test_rejects_changed_live_identity_scope_and_configuration(self):
        self.phase()
        self.fill_other_stages()
        original_stack = copy.deepcopy(self.stack)
        state = copy.deepcopy(self.controller.state)
        config = copy.deepcopy(self.controller.kickoff)
        mutations = {
            "parent head": lambda: self.stack["members"][0].update(head_sha="c" * 40),
            "child head": lambda: self.stack["members"][1].update(head_sha="c" * 40),
            "trunk base": lambda: self.tips.update(main="c" * 40),
            "child base": lambda: self.tips.update({"branch-11": "c" * 40}),
            "base unknown": lambda: self.tips.update(main=None),
            "base metadata": lambda: self.stack["members"][1].update(base_sha="c" * 40),
            "unknown mergeability": lambda: self.stack["members"][1].update(mergeable="UNKNOWN"),
            "conflicting": lambda: self.stack["members"][1].update(mergeable="CONFLICTING"),
            "closed": lambda: self.stack["members"][1].update(state="CLOSED"),
            "head ref": lambda: self.stack["members"][1].update(head_branch="other"),
            "base ref": lambda: self.stack["members"][1].update(base_branch="main"),
            "trunk ref": lambda: self.stack.update(trunk="other"),
            "stack id": lambda: self.stack.update(id="other"),
            "stack number": lambda: self.stack.update(number=78),
            "stack size": lambda: self.stack.update(size=3),
            "member order": lambda: self.stack["members"].reverse(),
            "selection": lambda: self.controller.kickoff.update(pullRequests=[11]),
            "owner run": lambda: self.controller.state.update(run_id="other"),
            "model": lambda: self.controller.models.update({MODULE.STAGE_CONFLICT: "gpt-6-astra"}),
            "strategy": lambda: setattr(self.controller, "conflict_strategy", "rebase"),
            "policy": lambda: setattr(self.controller, "github_mutation_policy", "source-only"),
            "effort": lambda: setattr(self.controller, "effort", "low"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                self.stack = copy.deepcopy(original_stack)
                self.controller.state = copy.deepcopy(state)
                self.controller.kickoff = copy.deepcopy(config)
                self.controller.models[MODULE.STAGE_CONFLICT] = "gpt-5.6-sol"
                self.controller.conflict_strategy = "auto"
                self.controller.github_mutation_policy = "allow"
                self.controller.effort = "high"
                self.tips = {"main": BASE, **{m["head_branch"]: m["head_sha"] for m in self.stack["members"]}}
                mutate()
                self.assert_incomplete()

    def test_rejects_unaccepted_failed_foreign_or_changed_producer_evidence(self):
        self.phase()
        self.fill_other_stages()
        path = self.receipt_path(MODULE.STAGE_CONFLICT)
        receipt = COMMON.read_json(path)
        state = copy.deepcopy(self.controller.state)
        mutations = {
            "unaccepted": lambda prior, payload: prior.update(accepted=False),
            "nonzero exit": lambda prior, payload: prior.update(returncode=1),
            "missing exit": lambda prior, payload: prior.pop("returncode"),
            "not clear": lambda prior, payload: prior.update(clear=False),
            "no recorded outcome": lambda prior, payload: prior.update(outcome=None),
            "unknown pass": lambda prior, payload: prior.update({"pass": True}),
            "future pass": lambda prior, payload: prior.update({"pass": 3}),
            "unknown configuration": lambda prior, payload: prior.pop("configuration"),
            "wrong dispatched head": lambda prior, payload: prior.update(dispatched_head_sha="c" * 40),
            "wrong accepted base": lambda prior, payload: prior.update(current_base_sha="c" * 40),
            "missing aggregate": lambda prior, payload: payload.pop("native_stack_clearance"),
            "unknown aggregate": lambda prior, payload: payload.update(native_stack_clearance=[]),
            "uncollected aggregate": lambda prior, payload: prior["stage_result"].pop("native_stack_clearance"),
            "invocation": lambda prior, payload: payload["agent_task"].update(invocation_id="other"),
            "producer run": lambda prior, payload: payload["agent_task"].update(run_id="other"),
            "failed producer": lambda prior, payload: payload["agent_task"].update(status="failed"),
            "producer model": lambda prior, payload: payload["agent_task"].update(model="gpt-6-astra"),
            "producer policy": lambda prior, payload: payload["agent_task"].update(policy="other"),
            "task identity": lambda prior, payload: payload["agent_task"].update(task_id="other"),
            "budget": lambda prior, payload: payload["agent_task"]["iteration"].update(budget=3),
            "authorization missing": lambda prior, payload: payload["native_stack_clearance"].pop("authorization"),
            "request not owned": lambda prior, payload: self.controller.state.update(stack_requests={}),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                self.controller.state = copy.deepcopy(state)
                payload = copy.deepcopy(receipt)
                prior = self.controller.state["pull_requests"]["11"]["stages"][MODULE.STAGE_CONFLICT]
                mutate(prior, payload)
                COMMON.write_json_atomically(path, payload)
                self.assert_incomplete()

    def test_rejects_aggregate_mismatch_even_when_collected_in_accepted_completion(self):
        self.phase()
        self.fill_other_stages()
        path = self.receipt_path(MODULE.STAGE_CONFLICT)
        receipt = COMMON.read_json(path)
        state = copy.deepcopy(self.controller.state)
        mutations = {
            "topology": lambda c: c.update(topology_fingerprint="other"),
            "source snapshot": lambda c: c.update(source_snapshot="other"),
            "trunk": lambda c: c["trunk"].update(sha="c" * 40),
            "unknown observation": lambda c: c.pop("observed_at"),
            "member order": lambda c: c["members"].reverse(),
            "member missing": lambda c: c["members"].pop(),
            "member head": lambda c: c["members"][1].update(head_sha="c" * 40),
            "member base": lambda c: c["members"][1].update(direct_base_sha="c" * 40),
            "member ref": lambda c: c["members"][1].update(head_ref="other"),
            "merge base": lambda c: c["members"][1].update(merge_base="c" * 40),
            "unknown member": lambda c: c["members"][1].update(mergeable="UNKNOWN"),
            "repository": lambda c: c["members"][1].update(repository="other/repo"),
            "authorization digest": lambda c: c["authorization"].update(request_sha256="c" * 64),
            "authorization run": lambda c: c["authorization"]["owner"].update(run_id="other"),
            "authorization order": lambda c: c["authorization"]["selected"].reverse(),
            "authorization operation": lambda c: c["authorization"].update(operation="descendant-propagation"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                self.controller.state = copy.deepcopy(state)
                payload = copy.deepcopy(receipt)
                clearance = payload["native_stack_clearance"]
                mutate(clearance)
                prior = self.controller.state["pull_requests"]["11"]["stages"][MODULE.STAGE_CONFLICT]
                prior["stage_result"]["native_stack_clearance"] = copy.deepcopy(clearance)
                COMMON.write_json_atomically(path, payload)
                self.assert_incomplete()

    def test_authorization_is_bound_to_current_owner_selection_and_source_not_just_digest(self):
        self.phase()
        self.fill_other_stages()
        path = self.receipt_path(MODULE.STAGE_CONFLICT)
        receipt = COMMON.read_json(path)
        state = copy.deepcopy(self.controller.state)
        mutations = {
            "owner kind": lambda a: a["owner"].update(kind="native_stack"),
            "owner state": lambda a: a["owner"].update(state=str(self.root / "other.json")),
            "owner cancellation": lambda a: a["owner"].update(cancellation=str(self.root / "other.json")),
            "owner run": lambda a: a["owner"].update(run_id="other"),
            "repository": lambda a: a.update(repository="other/repo"),
            "operation": lambda a: a.update(operation="descendant-propagation"),
            "selection": lambda a: a.update(selected=[11]),
            "fixed pr": lambda a: a.update(fixed_pr=12),
            "fixed head": lambda a: a.update(fixed_head="c" * 40),
            "source head": lambda a: a["source_stack"]["members"][1].update(head_sha="c" * 40),
            "source snapshot": lambda a: a.update(source_snapshot="c" * 64),
            "topology": lambda a: a.update(topology_fingerprint="c" * 64),
            "request state": lambda a: a.update(state=str(self.root / "other.json")),
            "request id": lambda a: a.update(request_id="other"),
            "schema": lambda a: a["schema"].update(version=2),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                self.controller.state = copy.deepcopy(state)
                payload = copy.deepcopy(receipt)
                clearance = payload["native_stack_clearance"]
                authorization = clearance["authorization"]
                mutate(authorization)
                authorization["request_sha256"] = hashlib.sha256(json.dumps(
                    {**authorization, "request_sha256": ""},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                self.controller.state["stack_requests"] = {
                    authorization["request_id"]: authorization["request_sha256"],
                }
                clearance["source_snapshot"] = authorization["source_snapshot"]
                prior = self.controller.state["pull_requests"]["11"]["stages"][MODULE.STAGE_CONFLICT]
                prior["stage_result"]["native_stack_clearance"] = copy.deepcopy(clearance)
                COMMON.write_json_atomically(path, payload)
                self.assert_incomplete()

    def test_other_stage_blockers_and_existing_child_conflict_state_stay_unresolved(self):
        self.phase()
        self.fill_other_stages()
        for stage in MODULE.STAGE_NAMES[1:]:
            for number in (11, 12):
                with self.subTest(stage=stage, number=number):
                    path = self.receipt_path(stage, number)
                    self.status_overrides[path] = {"stage_outcome": "escalated"}
                    result = self.controller.final_snapshot()
                    self.assertEqual("incomplete", result["result"])
                    self.assertIn(stage, next(item for item in result["pull_requests"] if item["number"] == number)["uncleared"])
                    self.status_overrides.clear()
        original = COMMON.read_json(self.receipt_path(MODULE.STAGE_CONFLICT))
        path = self.receipt_path(MODULE.STAGE_CONFLICT, 12)
        for outcome, head, base in (
            ("escalated", self.stack["members"][1]["head_sha"], self.stack["members"][1]["base_sha"]),
            ("cleared", "c" * 40, self.stack["members"][1]["base_sha"]),
            ("cleared", self.stack["members"][1]["head_sha"], "c" * 40),
        ):
            with self.subTest(outcome=outcome, head=head, base=base):
                receipt = copy.deepcopy(original)
                receipt["pr"].update(self.metadata[12])
                receipt["attempt"].update(mergeable_at_head_sha=head, base_sha=base)
                if outcome == "escalated":
                    receipt["agent_task"]["status"] = "failed"
                COMMON.write_json_atomically(path, receipt)
                before = path.read_bytes()
                self.assert_incomplete()
                self.assertEqual(before, path.read_bytes())

    def test_unverified_status_and_non_absent_child_results_never_use_aggregate(self):
        self.phase()
        self.fill_other_stages()
        parent = self.receipt_path(MODULE.STAGE_CONFLICT)
        for override in (
            {"state": str(self.root / "foreign.json")},
            {"pr": COMMON.target_for("other/repo", 11)},
            {"result": "no_state"},
        ):
            with self.subTest(override=override):
                self.status_overrides[parent] = override
                self.assert_incomplete()
        self.status_overrides.clear()
        inspect = self.controller.inspect
        for change in (
            {"reason": "status_failed"}, {"reason": "status_timeout"},
            {"reason": "plugin_not_installed", "installed": False},
            {"reason": "status_identity_mismatch"}, {"reason": "status_state_mismatch"},
            {"reason": "clearance_is_for_an_older_head"},
            {"reason": "clearance_is_for_an_older_base"},
            {"outcome": "escalated"}, {"status": {"agent_task": {"status": "failed"}}},
            {"clear_at_head_sha": "c" * 40}, {"clear_at_base_sha": "c" * 40},
            {"status_state": str(self.root / "other.json")},
        ):
            def changed(entry, target, head, base):
                result = inspect(entry, target, head, base)
                if entry["stage"] == MODULE.STAGE_CONFLICT and target["number"] == 12:
                    result.update(change)
                return result
            with self.subTest(change=change), mock.patch.object(self.controller, "inspect", side_effect=changed):
                self.assert_incomplete()

    def test_closing_snapshot_must_still_be_mergeable_and_open(self):
        self.phase()
        self.fill_other_stages()
        original = copy.deepcopy(self.stack)
        for mutation in ({"mergeable": "UNKNOWN"}, {"state": "CLOSED"}, {"base_sha": "c" * 40}):
            closing = copy.deepcopy(original)
            closing["members"][1].update(mutation)
            with self.subTest(mutation=mutation), mock.patch.object(
                self.controller, "read_stack", side_effect=[original, closing],
            ):
                self.assert_incomplete()

    def test_foreign_worker_nonce_or_dispatch_head_is_not_accepted_as_provenance(self):
        for returned in ({"nonce": "foreign"}, {"head_sha": "c" * 40}):
            with self.subTest(returned=returned):
                with mock.patch.object(self.launcher, "wait", return_value={"returncode": 0, **returned}):
                    self.phase()
                self.fill_other_stages()
                self.assert_incomplete()
                self.receipt_path(MODULE.STAGE_CONFLICT).unlink()

    def test_nonzero_worker_exit_cannot_clear_the_controller(self):
        with mock.patch.object(self.launcher, "wait", return_value={"returncode": 1}):
            result = self.controller.execute()
        self.assertEqual("blocked", result["result"])
        self.assertEqual("stage_execution_failed", result["reason"])
        self.assertEqual(1, len(self.launcher.started))
        self.assertFalse(self.receipt_path(MODULE.STAGE_CONFLICT, 12).exists())
