from contextlib import redirect_stdout
import importlib.util
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "pr_stack_pipeline.py"
PIPELINE_SCRIPT = Path(__file__).parents[1] / "scripts" / "pr_pipeline.py"
COMMON_SCRIPT = Path(__file__).parents[1] / "scripts" / "pipeline_common.py"
AGENT = Path(__file__).parents[1] / "agents" / "pr-stack-pipeline.agent.md"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = load("pr_stack_pipeline", SCRIPT)
PIPELINE = load("pr_pipeline", PIPELINE_SCRIPT)
COMMON = MODULE.common

BASE = "b" * 40


def head_of(number: int) -> str:
    return f"{number:040d}"


def kickoff(numbers=(11, 12, 13), start=11, stack_number=77) -> dict:
    return {
        "version": 1,
        "repository": "owner/repo",
        "stackNumber": stack_number,
        "startPullRequest": start,
        "pullRequests": list(numbers),
    }


def stack(members=(11, 12, 13), number=77, heads=None) -> dict:
    heads = heads or {}
    entries = []
    for index, member in enumerate(members):
        entries.append(
            {
                "position": index,
                "number": member,
                "title": f"Pull request {member}",
                "head_branch": f"branch-{member}",
                "base_branch": "main" if index == 0 else f"branch-{members[index - 1]}",
                "head_sha": heads.get(member, head_of(member)),
                "is_draft": member % 2 == 1,
                "state": "OPEN",
            }
        )
    return {
        "id": "S_stack",
        "number": number,
        "size": len(entries),
        "trunk": "main",
        "members": entries,
    }


class FakeHandle:
    def __init__(self, alive_polls: int = 0, returncode: int = 0):
        self.alive_polls = alive_polls
        self.returncode = returncode
        self.pid = 4242

    def poll(self):
        if self.alive_polls > 0:
            self.alive_polls -= 1
            return None
        return self.returncode

    def wait(self):
        self.alive_polls = 0
        return self.returncode


class FakeLauncher:
    """Record every launch step so serialization is observable in order."""

    def __init__(self, *, fail_step=None, fail_number=None, alive_polls=0):
        self.calls: list[tuple[str, int]] = []
        self.fail_step = fail_step
        self.fail_number = fail_number
        self.alive_polls = alive_polls
        self.cleaned: list[int] = []
        self.started: list[dict] = []
        self.on_start = None

    def _fails(self, step: str, request: dict) -> bool:
        return self.fail_step == step and request["number"] == self.fail_number

    def create(self, request):
        self.calls.append(("create", request["number"]))
        if self._fails("create", request):
            return {"result": "failed", "reason": "worktree_create_failed"}
        return {"result": "ready", "worktree": Path(f"/w/{request['number']}")}

    def verify(self, request, worktree):
        self.calls.append(("verify", request["number"]))
        if self._fails("verify", request):
            return {"result": "failed", "reason": "worktree_head_mismatch"}
        return {"result": "verified", "head_sha": request["head_sha"]}

    def start(self, request, worktree):
        self.calls.append(("start", request["number"]))
        if self._fails("start", request):
            return {"result": "failed", "reason": "worker_start_failed"}
        if self.on_start is not None:
            self.on_start(request)
        self.started.append(request)
        return {
            "result": "started",
            "handle": FakeHandle(alive_polls=self.alive_polls),
            "pid": 1000 + request["number"],
            "log_path": Path(f"/logs/{request['number']}.log"),
            "record_path": Path(f"/records/{request['number']}.json"),
        }

    def confirm_ready(self, request, started):
        self.calls.append(("confirm_ready", request["number"]))
        if self._fails("readiness", request):
            return {"result": "failed", "reason": "worker_readiness_timeout"}
        return {"result": "active", "evidence": {"pid": started["pid"]}}

    def cancel(self, started):
        number = started["pid"] - 1000
        self.calls.append(("cancel", number))
        started["handle"].wait()

    def is_running(self, worker):
        return worker["handle"].poll() is None

    def wait(self, worker):
        self.calls.append(("wait", worker["number"]))
        return {"returncode": worker["handle"].wait()}

    def cleanup(self, number):
        self.cleaned.append(number)
        return {"result": "removed", "number": number}


class KickoffTest(unittest.TestCase):
    def test_accepts_the_documented_schema(self):
        self.assertEqual(kickoff(), MODULE.parse_kickoff(kickoff()))

    def test_session_title_is_exact(self):
        self.assertEqual(
            "PR Stack Pipeline: #11 - Add a thing",
            MODULE.session_title(kickoff(), "Add a thing"),
        )

    def test_rejects_payloads_that_are_not_this_schema(self):
        cases = {
            "version": {**kickoff(), "version": 2},
            "repository": {**kickoff(), "repository": "owner"},
            "stack": {**kickoff(), "stackNumber": "77"},
            "start": {**kickoff(), "startPullRequest": 0},
            "empty": {**kickoff(), "pullRequests": []},
            "duplicate": {**kickoff(), "pullRequests": [11, 11]},
            "not_a_suffix_start": {**kickoff(), "pullRequests": [12, 13]},
            "not_an_object": [1, 2],
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(MODULE.WorkflowError):
                    MODULE.parse_kickoff(payload)


class DelegationTest(unittest.TestCase):
    def test_every_phase_delegates_to_a_plugin_qualified_agent(self):
        self.assertEqual(
            {
                "conflict-fix-loop": "conflict-fix-loop:conflict-fix-loop",
                "copilot-review-loop": "copilot-review-loop:copilot-review-loop",
                "self-review-loop": "self-review-loop:self-review-loop",
                "ci-fix-loop": "ci-fix-loop:ci-fix-loop",
                "pr-description": "pr-description:pr-description",
            },
            MODULE.PHASE_AGENTS,
        )

    def test_phase_order_and_modes_are_fixed(self):
        self.assertEqual(
            (
                "conflict-fix-loop",
                "copilot-review-loop",
                "self-review-loop",
                "ci-fix-loop",
                "pr-description",
            ),
            MODULE.PHASE_NAMES,
        )
        self.assertEqual(
            ["stack-dispatch", "parallel", "parallel", "bottom-up", "parallel"],
            [phase["mode"] for phase in MODULE.PHASES],
        )

    def test_the_helper_owns_no_stage_policy(self):
        source = SCRIPT.read_text(encoding="utf-8")
        shared = COMMON_SCRIPT.read_text(encoding="utf-8")
        for token in (
            "mergeable_at_head_sha",
            "clean_at_head_sha",
            "validated_head_sha",
            "CLEARING_OUTCOMES =",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, source)
                self.assertIn(token, shared)

    def test_the_helper_never_rebases_on_its_own(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for token in ('"rebase"', '"cherry-pick"', '"push"'):
            self.assertNotIn(token, source)
        self.assertIn("descendant-propagate", source)

    def test_the_stage_registry_is_the_shared_one(self):
        self.assertIs(COMMON.STAGES, MODULE.STAGES)
        self.assertEqual(COMMON.STAGE_NAMES, PIPELINE.STAGE_NAMES)

    def test_the_single_pull_request_pipeline_still_exposes_its_api(self):
        self.assertEqual(2, PIPELINE.MAX_SWEEPS)
        for name in (
            "run_pipeline",
            "sync_worktree",
            "settle_after_stage",
            "inspect_stage",
            "read_stage_status",
            "run_stage",
            "stage_models",
            "command_run",
            "build_parser",
        ):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(PIPELINE, name)))


class ModelTest(unittest.TestCase):
    def test_models_match_the_single_pull_request_pipeline(self):
        models = COMMON.stage_models(None)
        self.assertEqual("claude-opus-5", models[MODULE.STAGE_SELF_REVIEW])
        for stage in MODULE.PHASE_NAMES:
            if stage != MODULE.STAGE_SELF_REVIEW:
                self.assertEqual("gpt-5.6-sol", models[stage])

    def test_worker_commands_use_the_pipeline_flags(self):
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_CI]
        target = COMMON.target_for("owner/repo", 11)
        command = COMMON.stage_command(
            entry,
            target,
            model="gpt-5.6-sol",
            effort="high",
            arguments=[],
            prompt="owner/repo#11",
            resolve_program=lambda name: name,
        )
        self.assertEqual(
            [
                "copilot",
                "-p",
                "owner/repo#11",
                "--agent",
                "ci-fix-loop:ci-fix-loop",
                "--model",
                "gpt-5.6-sol",
                "--effort",
                "high",
                *COMMON.STAGE_AUTOPILOT_FLAGS,
                *COMMON.STAGE_PERMISSION_FLAGS,
            ],
            command,
        )


class TopologyTest(unittest.TestCase):
    def test_a_suffix_selection_is_accepted_with_drafts_included(self):
        live = stack(members=(9, 10, 11, 12))
        result = MODULE.validate_selection(kickoff([11, 12]), live)
        self.assertEqual("ready", result["result"])
        self.assertEqual([11, 12], [member["number"] for member in result["selected"]])
        self.assertEqual({True, False}, {m["is_draft"] for m in result["selected"]})

    def test_a_changed_stack_stops_the_run(self):
        cases = {
            "not_a_native_stack": (kickoff(), None),
            "stack_identity_changed": (kickoff(), stack(number=78)),
            "start_is_not_a_member": (kickoff([11, 12]), stack(members=(12, 13))),
            "selection_is_not_the_stack_suffix": (
                kickoff([11, 12]),
                stack(members=(11, 12, 13)),
            ),
        }
        for reason, (payload, live) in cases.items():
            with self.subTest(reason=reason):
                result = MODULE.validate_selection(payload, live)
                self.assertEqual("stopped", result["result"])
                self.assertEqual(reason, result["reason"])

    def test_the_fingerprint_follows_membership_and_order(self):
        first = MODULE.topology_fingerprint(stack())
        self.assertEqual(first, MODULE.topology_fingerprint(stack()))
        self.assertNotEqual(first, MODULE.topology_fingerprint(stack((11, 13, 12))))
        self.assertNotEqual(first, MODULE.topology_fingerprint(stack((11, 12))))
        moved = stack()
        moved["members"][1]["base_branch"] = "other"
        self.assertNotEqual(first, MODULE.topology_fingerprint(moved))

    def test_a_moved_head_alone_keeps_the_fingerprint(self):
        moved = stack(heads={12: "f" * 40})
        self.assertEqual(
            MODULE.topology_fingerprint(stack()),
            MODULE.topology_fingerprint(moved),
        )

    def test_reads_a_live_native_stack(self):
        payload = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "stack": {
                            "id": "S_1",
                            "number": 77,
                            "size": 1,
                            "baseRefName": "main",
                            "entries": {
                                "nodes": [
                                    {
                                        "position": 0,
                                        "pullRequest": {
                                            "number": 11,
                                            "title": "Pull request 11",
                                            "headRefName": "branch-11",
                                            "baseRefName": "main",
                                            "headRefOid": head_of(11),
                                            "isDraft": True,
                                            "state": "OPEN",
                                        },
                                    }
                                ]
                            },
                        }
                    }
                }
            }
        }
        live = MODULE.read_native_stack(
            "owner/repo", 11, api=lambda arguments: payload
        )
        self.assertEqual(77, live["number"])
        self.assertEqual([11], [member["number"] for member in live["members"]])


class StackFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.events: list[dict] = []
        self.launcher = FakeLauncher()
        self.stack = stack()
        self.clear: set[tuple[int, str]] = set()
        self.nonce_count = 0
        self.checkpoint_map: dict[int, list[dict]] = {}
        self.worker_progress_map: dict[int, dict] = {}
        self.propagated: list[tuple[int, str]] = []
        self.contains_pairs: set[tuple[str, str]] | None = None

    def next_nonce(self) -> str:
        self.nonce_count += 1
        return f"nonce-{self.nonce_count}"

    def read_stack(self, repository, number):
        return self.stack

    def inspect(self, entry, target, head_sha, base_sha=None):
        clear = (target["number"], entry["stage"]) in self.clear
        return {
            "stage": entry["stage"],
            "clear": clear,
            "clear_at_head_sha": head_sha if clear else None,
            "clear_at_base_sha": None,
            "outcome": "cleared" if clear else "carried",
            "reason": None if clear else "carried",
            "installed": True,
            "status_state": "state.json",
            "status": {},
        }

    def checkpoints(self, repository, number):
        return self.checkpoint_map.get(number, [])

    def worker_progress(self, repository, number):
        return self.worker_progress_map.get(number)

    def propagate(self, repository, number, head_sha, stack_number):
        self.propagated.append((number, head_sha))
        return {
            "result": "published",
            "number": number,
            "head_sha": head_sha,
            "stack_number": stack_number,
        }

    def contains(self, repo_root, ancestor, descendant):
        if self.contains_pairs is None:
            return True
        return (ancestor, descendant) in self.contains_pairs

    def pipeline(self, payload=None, **overrides):
        options = {
            "models": COMMON.stage_models(None),
            "effort": "high",
            "run_id": "run-1",
            "report": self.events.append,
            "launcher": self.launcher,
            "state_path": self.root / "state.json",
            "lock_path": self.root / "state.lock",
            "run_directory": self.root / "run",
            "read_stack": self.read_stack,
            "inspect": self.inspect,
            "base_tip": lambda repository, branch: BASE,
            "contains": self.contains,
            "checkpoints": self.checkpoints,
            "worker_progress": self.worker_progress,
            "propagate": self.propagate,
            "dependencies": lambda: [],
            "sleep": lambda _seconds: None,
            "nonces": self.next_nonce,
        }
        options.update(overrides)
        pipeline = MODULE.StackPipeline(
            payload or kickoff(), self.root / "repo", **options
        )
        pipeline.state = MODULE.new_state(
            pipeline.kickoff, pipeline.run_id, MODULE.topology_fingerprint(self.stack)
        )
        return pipeline

    def clear_everything(self):
        for member in self.stack["members"]:
            for stage in MODULE.STAGE_NAMES:
                self.clear.add((member["number"], stage))

    def events_named(self, name):
        return [event for event in self.events if event["event"] == name]


class StackRunTest(StackFixture):
    # Conflict dispatch -------------------------------------------------

    def test_conflicts_are_delegated_once_for_the_clicked_pull_request(self):
        self.stack = stack(members=(9, 10, 11, 12))
        pipeline = self.pipeline(kickoff([11, 12]))
        selected = MODULE.validate_selection(pipeline.kickoff, self.stack)["selected"]

        result = pipeline.run_conflict_phase(1, selected)

        self.assertEqual(1, result["dispatches"])
        self.assertEqual([("create", 11)], self.launcher.calls[:1])
        self.assertEqual([11], [request["number"] for request in self.launcher.started])
        request = self.launcher.started[0]
        self.assertEqual("conflict-fix-loop:conflict-fix-loop", request["agent"])
        self.assertIn("stack 77", request["prompt"])
        self.assertIn("as a whole", request["prompt"])

    # Serialized startup ------------------------------------------------

    def test_each_worker_is_verified_and_active_before_the_next_starts(self):
        pipeline = self.pipeline()
        selected = self.stack["members"]

        pipeline.run_parallel_phase(MODULE.STAGE_COPILOT_REVIEW, 1, selected)

        launch_calls = [
            call for call in self.launcher.calls if call[0] != "wait"
        ]
        self.assertEqual(
            [
                ("create", 11),
                ("verify", 11),
                ("start", 11),
                ("confirm_ready", 11),
                ("create", 12),
                ("verify", 12),
                ("start", 12),
                ("confirm_ready", 12),
                ("create", 13),
                ("verify", 13),
                ("start", 13),
                ("confirm_ready", 13),
            ],
            launch_calls,
        )

    def test_selected_workers_run_concurrently_once_they_are_active(self):
        pipeline = self.pipeline()

        pipeline.run_parallel_phase(
            MODULE.STAGE_COPILOT_REVIEW, 1, self.stack["members"]
        )

        last_start = max(
            index
            for index, call in enumerate(self.launcher.calls)
            if call[0] == "confirm_ready"
        )
        first_wait = min(
            index
            for index, call in enumerate(self.launcher.calls)
            if call[0] == "wait"
        )
        self.assertLess(last_start, first_wait)

    def test_a_failed_launch_stops_later_launches_and_is_never_retried(self):
        self.launcher = FakeLauncher(fail_step="verify", fail_number=12)
        pipeline = self.pipeline()

        result = pipeline.run_parallel_phase(
            MODULE.STAGE_COPILOT_REVIEW, 1, self.stack["members"]
        )

        self.assertEqual("verify", result["stopped"]["step"])
        self.assertEqual(12, result["stopped"]["number"])
        self.assertEqual(
            [11], [call[1] for call in self.launcher.calls if call[0] == "start"]
        )
        self.assertEqual(
            1, len([call for call in self.launcher.calls if call == ("create", 12)])
        )
        self.assertNotIn(("create", 13), self.launcher.calls)
        self.assertEqual([11], [completion["number"] for completion in result["completions"]])

    def test_a_worker_that_never_becomes_active_stops_the_dispatch(self):
        self.launcher = FakeLauncher(fail_step="readiness", fail_number=11)
        pipeline = self.pipeline()

        result = pipeline.run_parallel_phase(
            MODULE.STAGE_COPILOT_REVIEW, 1, self.stack["members"]
        )

        self.assertEqual("readiness", result["stopped"]["step"])
        self.assertEqual([], result["completions"])
        self.assertNotIn(("create", 12), self.launcher.calls)
        self.assertIn(("cancel", 11), self.launcher.calls)
        self.assertEqual([], pipeline.state["active_workers"])

    # Stale results ------------------------------------------------------

    def test_a_result_from_an_old_dispatch_is_ignored(self):
        completion = {"nonce": "nonce-1", "head_sha": head_of(11)}
        self.assertTrue(
            MODULE.accept_completion(
                completion, expected_nonce="nonce-1", expected_head_sha=head_of(11)
            )
        )
        self.assertFalse(
            MODULE.accept_completion(
                completion, expected_nonce="nonce-2", expected_head_sha=head_of(11)
            )
        )
        self.assertFalse(
            MODULE.accept_completion(
                completion, expected_nonce="nonce-1", expected_head_sha=head_of(12)
            )
        )

    def test_a_worker_dispatched_for_an_older_head_is_not_accepted(self):
        pipeline = self.pipeline()
        member = self.stack["members"][0]
        request = pipeline.request_for(member, MODULE.STAGE_COPILOT_REVIEW, 1)
        worker = {
            "number": member["number"],
            "stage": MODULE.STAGE_COPILOT_REVIEW,
            "nonce": request["nonce"],
            "head_sha": "0" * 40,
            "handle": FakeHandle(),
        }

        completion = pipeline.finish_worker(worker, request)

        self.assertFalse(completion["accepted"])

    # CI ordering --------------------------------------------------------

    def test_ci_starts_at_the_bottom_and_waits_for_a_green_predecessor(self):
        pipeline = self.pipeline()
        self.launcher.on_start = lambda request: self.clear.add(
            (request["number"], MODULE.STAGE_CI)
        )

        result = pipeline.run_ci_phase(1, self.stack["members"])

        self.assertEqual(
            [11, 12, 13],
            [call[1] for call in self.launcher.calls if call[0] == "start"],
        )
        self.assertEqual(
            ["lowest_selected", "predecessor_is_green", "predecessor_is_green"],
            [gate["reason"] for gate in result["gates"]],
        )

    def test_ci_refreshes_propagated_heads_before_starting_the_next_worker(self):
        pipeline = self.pipeline()

        def advance(request):
            self.clear.add((request["number"], MODULE.STAGE_CI))
            if request["number"] == 11:
                self.stack = stack(heads={11: "a" * 40, 12: "b" * 40})

        self.launcher.on_start = advance

        pipeline.run_ci_phase(1, self.stack["members"])

        self.assertEqual("b" * 40, self.launcher.started[1]["head_sha"])

    def test_a_red_predecessor_stops_the_members_above_it(self):
        pipeline = self.pipeline()
        self.launcher.on_start = lambda request: (
            self.clear.add((request["number"], MODULE.STAGE_CI))
            if request["number"] != 11
            else None
        )

        result = pipeline.run_ci_phase(1, self.stack["members"])

        self.assertEqual(
            [11], [call[1] for call in self.launcher.calls if call[0] == "start"]
        )
        self.assertEqual("predecessor_is_not_green", result["gates"][-1]["reason"])
        self.assertEqual(12, result["blocked"]["number"])
        self.assertIsNone(result["stopped"])

    def test_a_child_that_does_not_contain_its_predecessor_is_aligned(self):
        self.contains_pairs = set()
        pipeline = self.pipeline()
        for number in (11, 12, 13):
            self.clear.add((number, MODULE.STAGE_CI))

        def align(repository, number, head_sha, stack_number):
            self.propagated.append((number, head_sha))
            self.stack = stack(heads={12: "a" * 40, 13: "c" * 40})
            self.contains_pairs.update(
                {
                    (head_of(11), "a" * 40),
                    ("a" * 40, "c" * 40),
                }
            )
            return {"result": "published"}

        pipeline.propagate = align

        result = pipeline.run_ci_phase(1, self.stack["members"])

        self.assertEqual(
            [], [call[1] for call in self.launcher.calls if call[0] == "start"]
        )
        self.assertEqual([(11, head_of(11))], self.propagated)
        self.assertIsNone(result["blocked"])
        self.assertEqual(
            ["lowest_selected", "predecessor_is_green", "predecessor_is_green"],
            [gate["reason"] for gate in result["gates"]],
        )

    def test_alignment_rebuilds_the_ci_request_for_the_rebased_child(self):
        self.contains_pairs = set()
        pipeline = self.pipeline()
        self.clear.add((11, MODULE.STAGE_CI))

        def align(repository, number, head_sha, stack_number):
            self.stack = stack(heads={12: "a" * 40, 13: "c" * 40})
            self.contains_pairs.update(
                {
                    (head_of(11), "a" * 40),
                    ("a" * 40, "c" * 40),
                }
            )
            return {"result": "published"}

        pipeline.propagate = align
        self.launcher.on_start = lambda request: self.clear.add(
            (request["number"], MODULE.STAGE_CI)
        )

        pipeline.run_ci_phase(1, self.stack["members"])

        self.assertEqual(
            ["a" * 40, "c" * 40],
            [request["head_sha"] for request in self.launcher.started],
        )

    def test_a_conflicted_live_head_alignment_blocks_the_descendants(self):
        self.contains_pairs = set()
        pipeline = self.pipeline()
        self.clear.add((11, MODULE.STAGE_CI))
        pipeline.propagate = lambda *args: {"result": "conflicted"}

        result = pipeline.run_ci_phase(1, self.stack["members"])

        self.assertEqual("descendant_propagation_incomplete", result["blocked"]["reason"])
        self.assertEqual(12, result["blocked"]["number"])
        self.assertEqual("predecessor_head_is_not_contained", result["gates"][-1]["reason"])
        self.assertEqual("predecessor_alignment", result["propagations"][-1]["trigger"])

    def test_an_already_clear_member_is_green_without_a_worker(self):
        self.clear.add((11, MODULE.STAGE_CI))
        pipeline = self.pipeline()
        self.launcher.on_start = lambda request: self.clear.add(
            (request["number"], MODULE.STAGE_CI)
        )

        pipeline.run_ci_phase(1, self.stack["members"])

        self.assertEqual(
            [12, 13], [call[1] for call in self.launcher.calls if call[0] == "start"]
        )

    def test_ci_workers_carry_the_pipeline_position_with_a_two_pass_budget(self):
        pipeline = self.pipeline()
        member = self.stack["members"][0]
        with mock.patch.object(COMMON, "stage_accepts_pipeline_position", return_value=True):
            request = pipeline.request_for(member, MODULE.STAGE_CI, 2)
        self.assertEqual(
            [
                "--pipeline-run",
                "run-1",
                "--pipeline-iteration",
                "2",
                "--pipeline-max-iterations",
                "2",
            ],
            request["arguments"],
        )

    # Push propagation ---------------------------------------------------

    def test_an_accepted_push_is_propagated_while_the_worker_runs(self):
        self.launcher = FakeLauncher(alive_polls=2)
        pipeline = self.pipeline()
        self.checkpoint_map[11] = [
            {
                "id": "push-1",
                "head_sha": "1" * 40,
                "pipeline_run": "run-1",
                "pipeline_iteration": 1,
            },
        ]
        member = self.stack["members"][0]
        request = pipeline.request_for(member, MODULE.STAGE_CI, 1)
        launched = pipeline.dispatch([request], MODULE.STAGE_CI, 1)
        self.stack = stack(heads={11: "1" * 40})

        monitored = pipeline.monitor_ci_worker(launched["workers"][0], request)

        self.assertEqual([(11, "1" * 40)], self.propagated)
        self.assertEqual(1, len(monitored["propagations"]))
        self.assertEqual(
            ["push_propagated"],
            [event["event"] for event in self.events if event["event"] == "push_propagated"],
        )

    def test_each_checkpoint_is_propagated_once(self):
        self.launcher = FakeLauncher(alive_polls=3)
        pipeline = self.pipeline()
        self.checkpoint_map[11] = [
            {
                "id": "push-1",
                "head_sha": "1" * 40,
                "pipeline_run": "run-1",
                "pipeline_iteration": 1,
            }
        ]
        member = self.stack["members"][0]
        request = pipeline.request_for(member, MODULE.STAGE_CI, 1)
        launched = pipeline.dispatch([request], MODULE.STAGE_CI, 1)
        self.stack = stack(heads={11: "1" * 40})

        pipeline.monitor_ci_worker(launched["workers"][0], request)

        self.assertEqual([(11, "1" * 40)], self.propagated)

    def test_ci_monitor_reports_known_failure_diagnostics_once(self):
        self.launcher = FakeLauncher(alive_polls=2)
        self.worker_progress_map[11] = {
            "phase": "diagnosing",
            "action": "attribute",
            "reason": "unattributed_failures",
            "action_checks": ["check:build"],
            "pending_checks": ["check:test"],
            "head_sha": head_of(11),
        }
        pipeline = self.pipeline()
        member = self.stack["members"][0]
        request = pipeline.request_for(member, MODULE.STAGE_CI, 1)
        launched = pipeline.dispatch([request], MODULE.STAGE_CI, 1)

        pipeline.monitor_ci_worker(launched["workers"][0], request)

        progress = [
            event for event in self.events if event["event"] == "worker_progress"
        ]
        self.assertEqual(1, len(progress))
        self.assertEqual("diagnosing", progress[0]["phase"])
        self.assertEqual(["check:test"], progress[0]["pending_checks"])

    def test_stale_push_checkpoints_are_ignored(self):
        self.launcher = FakeLauncher(alive_polls=1)
        pipeline = self.pipeline()
        self.checkpoint_map[11] = [
            {
                "id": "old-run",
                "head_sha": "1" * 40,
                "pipeline_run": "run-0",
                "pipeline_iteration": 1,
            },
            {
                "id": "old-pass",
                "head_sha": "2" * 40,
                "pipeline_run": "run-1",
                "pipeline_iteration": 2,
            },
        ]
        member = self.stack["members"][0]
        request = pipeline.request_for(member, MODULE.STAGE_CI, 1)
        launched = pipeline.dispatch([request], MODULE.STAGE_CI, 1)

        pipeline.monitor_ci_worker(launched["workers"][0], request)

        self.assertEqual([], self.propagated)

    def test_a_failed_propagation_checkpoint_is_retried_in_the_next_pass(self):
        current_head = "1" * 40
        self.stack = stack(heads={11: current_head})
        pipeline = self.pipeline()
        self.checkpoint_map[11] = [
            {
                "id": "push-1",
                "head_sha": current_head,
                "pipeline_run": "run-1",
                "pipeline_iteration": 1,
            }
        ]
        outcomes = iter(
            [
                {"result": "failed", "reason": "temporary"},
                {"result": "published"},
            ]
        )
        pipeline.propagate = lambda *args: next(outcomes)
        member = self.stack["members"][0]

        first = pipeline.propagate_ci_pushes(
            pipeline.request_for(member, MODULE.STAGE_CI, 1), set()
        )
        second = pipeline.propagate_ci_pushes(
            pipeline.request_for(member, MODULE.STAGE_CI, 2), set()
        )

        self.assertEqual("failed", first[0]["result"])
        self.assertEqual("published", second[0]["result"])
        self.assertEqual(["push-1"], pipeline.state["propagated_pushes"])

    def test_a_failed_checkpoint_is_retired_after_its_source_head_moves(self):
        self.stack = stack(heads={11: "2" * 40})
        pipeline = self.pipeline()
        self.checkpoint_map[11] = [
            {
                "id": "push-1",
                "head_sha": "1" * 40,
                "pipeline_run": "run-1",
                "pipeline_iteration": 1,
            }
        ]
        pipeline.propagate = mock.Mock(side_effect=AssertionError("must not retry"))
        member = self.stack["members"][0]

        outcomes = pipeline.propagate_ci_pushes(
            pipeline.request_for(member, MODULE.STAGE_CI, 2), set()
        )

        self.assertEqual("superseded", outcomes[0]["result"])
        self.assertEqual("source_head_moved", outcomes[0]["reason"])
        self.assertEqual("2" * 40, outcomes[0]["superseded_by"])
        self.assertEqual(["push-1"], pipeline.state["propagated_pushes"])
        pipeline.propagate.assert_not_called()

    def test_a_checkpoint_matching_the_live_head_survives_a_stale_request(self):
        self.stack = stack(heads={11: "1" * 40})
        pipeline = self.pipeline()
        member = self.stack["members"][0]
        request = pipeline.request_for(member, MODULE.STAGE_CI, 2)
        self.stack = stack(heads={11: "2" * 40})
        self.checkpoint_map[11] = [
            {
                "id": "push-1",
                "head_sha": "2" * 40,
                "pipeline_run": "run-1",
                "pipeline_iteration": 1,
            }
        ]

        outcomes = pipeline.propagate_ci_pushes(request, set())

        self.assertEqual("published", outcomes[0]["result"])
        self.assertEqual([(11, "2" * 40)], self.propagated)

    def test_a_current_pass_checkpoint_matching_the_stale_request_is_retired(self):
        self.stack = stack(heads={11: "1" * 40})
        pipeline = self.pipeline()
        request = pipeline.request_for(
            self.stack["members"][0], MODULE.STAGE_CI, 2
        )
        self.stack = stack(heads={11: "2" * 40})
        self.checkpoint_map[11] = [
            {
                "id": "push-1",
                "head_sha": "1" * 40,
                "pipeline_run": "run-1",
                "pipeline_iteration": 2,
            }
        ]
        pipeline.propagate = mock.Mock(side_effect=AssertionError("must not retry"))

        outcomes = pipeline.propagate_ci_pushes(request, set())

        self.assertEqual("superseded", outcomes[0]["result"])
        self.assertEqual("2" * 40, outcomes[0]["superseded_by"])
        pipeline.propagate.assert_not_called()

    def test_retiring_a_stale_checkpoint_rebuilds_the_ci_worker_request(self):
        self.stack = stack(members=(11,), heads={11: "1" * 40})
        pipeline = self.pipeline(kickoff(numbers=(11,)))
        selected = self.stack["members"]
        self.stack = stack(members=(11,), heads={11: "2" * 40})
        self.checkpoint_map[11] = [
            {
                "id": "push-1",
                "head_sha": "1" * 40,
                "pipeline_run": "run-1",
                "pipeline_iteration": 1,
            }
        ]
        pipeline.propagate = mock.Mock(side_effect=AssertionError("must not retry"))
        self.launcher.on_start = lambda request: self.clear.add(
            (request["number"], MODULE.STAGE_CI)
        )

        result = pipeline.run_ci_phase(2, selected)

        self.assertIsNone(result["blocked"])
        self.assertEqual(
            ["2" * 40],
            [request["head_sha"] for request in self.launcher.started],
        )
        pipeline.propagate.assert_not_called()

    def test_an_old_checkpoint_is_not_retried_without_a_live_source_head(self):
        self.stack = stack(heads={11: "1" * 40})
        pipeline = self.pipeline(read_stack=lambda repository, number: None)
        self.checkpoint_map[11] = [
            {
                "id": "push-1",
                "head_sha": "1" * 40,
                "pipeline_run": "run-1",
                "pipeline_iteration": 1,
            }
        ]
        pipeline.propagate = mock.Mock(side_effect=AssertionError("must not retry"))

        outcomes = pipeline.propagate_ci_pushes(
            pipeline.request_for(self.stack["members"][0], MODULE.STAGE_CI, 2),
            set(),
        )

        self.assertEqual("failed", outcomes[0]["result"])
        self.assertEqual("source_head_unknown", outcomes[0]["reason"])
        pipeline.propagate.assert_not_called()

    def test_alignment_retires_the_child_checkpoint_from_its_old_head(self):
        self.contains_pairs = set()
        self.stack = stack()
        pipeline = self.pipeline()
        for number in (11, 12, 13):
            self.clear.add((number, MODULE.STAGE_CI))
        self.checkpoint_map[12] = [
            {
                "id": "child-push",
                "head_sha": head_of(12),
                "pipeline_run": "run-1",
                "pipeline_iteration": 1,
            }
        ]

        def align(repository, number, head_sha, stack_number):
            self.propagated.append((number, head_sha))
            self.stack = stack(heads={12: "a" * 40, 13: "c" * 40})
            self.contains_pairs.update(
                {
                    (head_of(11), "a" * 40),
                    ("a" * 40, "c" * 40),
                }
            )
            return {"result": "published"}

        pipeline.propagate = align

        result = pipeline.run_ci_phase(2, self.stack["members"])

        self.assertIsNone(result["blocked"])
        self.assertEqual([(11, head_of(11))], self.propagated)
        self.assertEqual(["child-push"], pipeline.state["propagated_pushes"])
        retired = [
            outcome
            for outcome in result["propagations"]
            if outcome.get("reason") == "source_head_moved"
        ]
        self.assertEqual("a" * 40, retired[0]["superseded_by"])

    def test_a_newer_successful_push_retires_an_older_failed_checkpoint(self):
        self.stack = stack(heads={11: "2" * 40})
        pipeline = self.pipeline()
        self.checkpoint_map[11] = [
            {
                "id": "push-1",
                "head_sha": "1" * 40,
                "pipeline_run": "run-1",
                "pipeline_iteration": 1,
            },
            {
                "id": "push-2",
                "head_sha": "2" * 40,
                "pipeline_run": "run-1",
                "pipeline_iteration": 1,
            },
        ]
        pipeline.propagate = lambda *args: {"result": "published"}

        propagated = pipeline.propagate_ci_pushes(
            pipeline.request_for(
                self.stack["members"][0], MODULE.STAGE_CI, 1
            ),
            set(),
        )

        self.assertEqual("2" * 40, propagated[0]["superseded_by"])
        self.assertEqual("published", propagated[1]["result"])
        self.assertEqual(
            ["push-1", "push-2"], pipeline.state["propagated_pushes"]
        )

    # Passes and completion ---------------------------------------------

    def test_one_pass_completes_when_every_marker_is_current(self):
        self.clear_everything()
        pipeline = self.pipeline()

        result = pipeline.execute()

        self.assertEqual("complete", result["result"])
        self.assertEqual(1, result["passes"])
        self.assertEqual(
            ["conflict-fix-loop", "copilot-review-loop", "self-review-loop", "ci-fix-loop", "pr-description"],
            [phase["phase"] for phase in result["phases"]],
        )

    def test_two_passes_bound_the_run_and_report_partial_state(self):
        pipeline = self.pipeline()

        result = pipeline.execute()

        self.assertEqual("partial", result["result"])
        self.assertEqual("two_passes_finished", result["reason"])
        self.assertEqual(2, result["passes"])
        self.assertEqual(
            2, len([phase for phase in result["phases"] if phase["phase"] == "ci-fix-loop"])
        )
        self.assertEqual("incomplete", result["snapshot"]["result"])

    def test_a_changed_topology_stops_the_run(self):
        pipeline = self.pipeline()
        self.clear_everything()
        original = pipeline.revalidate

        def drift():
            self.stack = stack(members=(11, 12))
            return original()

        with mock.patch.object(pipeline, "revalidate", side_effect=drift):
            result = pipeline.execute()

        self.assertEqual("stopped", result["result"])
        self.assertIn(
            result["reason"],
            {"topology_changed", "selection_is_not_the_stack_suffix"},
        )

    def test_missing_stage_plugins_stop_the_run_before_any_worker(self):
        pipeline = self.pipeline(dependencies=lambda: ["ci-fix-loop"])

        result = pipeline.execute()

        self.assertEqual("stopped", result["result"])
        self.assertEqual("missing_dependencies", result["reason"])
        self.assertEqual([], self.launcher.calls)

    def test_a_stopped_launch_ends_the_run_with_a_partial_summary(self):
        self.launcher = FakeLauncher(fail_step="start", fail_number=11)
        pipeline = self.pipeline()

        result = pipeline.execute()

        self.assertEqual("stopped", result["result"])
        self.assertEqual("worker_launch_stopped", result["reason"])
        self.assertIn("worker_start_failed", result["detail"])
        self.assertEqual(0, result["passes"])
        self.assertEqual(
            "PR Stack Pipeline: #11 - Pull request 11", result["session_title"]
        )

    def test_the_run_cleans_up_the_worktrees_it_created(self):
        self.clear_everything()
        pipeline = self.pipeline()

        result = pipeline.execute()

        self.assertEqual([11, 12, 13], sorted(self.launcher.cleaned))
        self.assertEqual(
            [{"result": "removed", "number": number} for number in (11, 12, 13)],
            result["cleanup"],
        )

    def test_a_duplicate_run_stops_on_the_lock(self):
        self.clear_everything()
        first = self.pipeline()
        MODULE.acquire_lock(first.lock_path, "other-run")
        with mock.patch.object(MODULE.common, "process_is_alive", return_value=True):
            result = first.execute()

        self.assertEqual("stopped", result["result"])
        self.assertEqual("another_run_holds_the_lock", result["reason"])
        self.assertEqual([], self.launcher.calls)

    def test_recovery_does_not_duplicate_a_still_active_worker(self):
        pipeline = self.pipeline()
        state = MODULE.new_state(
            pipeline.kickoff, "old-run", MODULE.topology_fingerprint(self.stack)
        )
        state["active_workers"] = [
            {
                "nonce": "old-nonce",
                "number": 11,
                "stage": MODULE.STAGE_CI,
                "pid": 1234,
                "head_sha": head_of(11),
                "pass": 1,
            }
        ]
        MODULE.save_state(pipeline.state_path, state)

        with mock.patch.object(
            MODULE, "live_recorded_workers", return_value=state["active_workers"]
        ):
            result = pipeline.execute()

        self.assertEqual("incomplete", result["result"])
        self.assertEqual("previous_workers_still_active", result["reason"])
        self.assertEqual([], self.launcher.calls)

    def test_progress_events_name_every_phase(self):
        self.clear_everything()
        pipeline = self.pipeline()

        pipeline.execute()

        self.assertEqual(
            ["stack_pipeline_started", "topology_validated", "pass_started"],
            [event["event"] for event in self.events[:3]],
        )
        self.assertEqual(
            list(MODULE.PHASE_NAMES),
            [event["phase"] for event in self.events_named("phase_started")],
        )
        self.assertEqual(1, len(self.events_named("snapshot_taken")))


class SnapshotTest(StackFixture):
    def test_completion_needs_all_five_markers_for_every_selected_member(self):
        self.clear_everything()
        pipeline = self.pipeline()
        self.assertEqual("complete", pipeline.final_snapshot()["result"])

        self.clear.discard((13, MODULE.STAGE_DESCRIPTION))
        snapshot = pipeline.final_snapshot()
        self.assertEqual("incomplete", snapshot["result"])
        self.assertEqual("stages_not_clear", snapshot["reason"])
        self.assertEqual(
            ["pr-description"],
            [
                stage
                for entry in snapshot["pull_requests"]
                if entry["number"] == 13
                for stage in entry["uncleared"]
            ],
        )

    def test_a_stack_that_moves_during_the_snapshot_is_not_complete(self):
        self.clear_everything()
        pipeline = self.pipeline()
        reads = iter([stack(), stack(members=(11, 12, 13, 14))])

        with mock.patch.object(
            pipeline, "read_stack", side_effect=lambda *args: next(reads)
        ):
            snapshot = pipeline.final_snapshot()

        self.assertEqual("incomplete", snapshot["result"])

    def test_a_head_that_moves_during_the_snapshot_is_not_complete(self):
        self.clear_everything()
        pipeline = self.pipeline()
        reads = iter([stack(), stack(heads={12: "e" * 40})])

        with mock.patch.object(
            pipeline, "read_stack", side_effect=lambda *args: next(reads)
        ):
            snapshot = pipeline.final_snapshot()

        self.assertEqual("incomplete", snapshot["result"])
        self.assertEqual("heads_moved_during_snapshot", snapshot["reason"])
        self.assertEqual([12], snapshot["moved"])

    def test_a_base_that_moves_during_the_snapshot_is_not_complete(self):
        self.clear_everything()
        reads: dict[str, int] = {}

        def base_tip(repository, branch):
            reads[branch] = reads.get(branch, 0) + 1
            if branch == "branch-11" and reads[branch] > 1:
                return "e" * 40
            return BASE

        pipeline = self.pipeline(base_tip=base_tip)

        snapshot = pipeline.final_snapshot()

        self.assertEqual("incomplete", snapshot["result"])
        self.assertEqual("bases_moved_during_snapshot", snapshot["reason"])
        self.assertEqual([12], snapshot["moved"])


class StateTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_state_is_versioned_and_written_in_one_step(self):
        path = self.root / "state.json"
        state = MODULE.new_state(kickoff(), "run-1", "fingerprint")
        MODULE.save_state(path, state)

        loaded = MODULE.load_state(path)
        self.assertEqual(MODULE.STATE_VERSION, loaded["state_version"])
        self.assertEqual(kickoff(), loaded["kickoff"])
        self.assertEqual([], list(self.root.glob("*.tmp")))

    def test_an_unfinished_run_is_resumed_from_its_durable_state(self):
        path = self.root / "state.json"
        state = MODULE.new_state(kickoff(), "run-1", "fingerprint")
        state["pass"] = 1
        state["phase"] = MODULE.STAGE_CI
        MODULE.save_state(path, state)

        resumed = MODULE.resume_state(path, kickoff(), "run-2", "fingerprint")

        self.assertEqual("run-2", resumed["run_id"])
        self.assertEqual(1, resumed["pass"])
        self.assertEqual("run-1", resumed["recovered_from"]["run_id"])

    def test_a_changed_topology_starts_a_fresh_state(self):
        path = self.root / "state.json"
        MODULE.save_state(path, MODULE.new_state(kickoff(), "run-1", "old"))

        resumed = MODULE.resume_state(path, kickoff(), "run-2", "new")

        self.assertEqual(0, resumed["pass"])
        self.assertEqual("topology_changed", resumed["recovered_from"]["reason"])

    def test_a_finished_run_is_not_resumed(self):
        path = self.root / "state.json"
        state = MODULE.new_state(kickoff(), "run-1", "fingerprint")
        state["result"] = "complete"
        MODULE.save_state(path, state)

        resumed = MODULE.resume_state(path, kickoff(), "run-2", "fingerprint")

        self.assertIsNone(resumed["result"])
        self.assertNotIn("recovered_from", resumed)

    def test_a_lock_held_by_a_live_process_is_not_taken(self):
        path = self.root / "state.lock"
        MODULE.acquire_lock(path, "run-1", alive=lambda pid: True)

        held = MODULE.acquire_lock(path, "run-2", alive=lambda pid: True)
        self.assertEqual("held", held["result"])
        self.assertEqual("run-1", held["holder"]["run_id"])

    def test_a_lock_left_by_a_dead_process_is_taken(self):
        path = self.root / "state.lock"
        MODULE.acquire_lock(path, "run-1", alive=lambda pid: False)

        taken = MODULE.acquire_lock(path, "run-2", alive=lambda pid: False)
        self.assertEqual("acquired", taken["result"])

        MODULE.release_lock(path, "run-2")
        self.assertFalse(path.exists())

    def test_a_lock_is_not_released_by_another_run(self):
        path = self.root / "state.lock"
        MODULE.acquire_lock(path, "run-1", alive=lambda pid: True)

        MODULE.release_lock(path, "run-2")

        self.assertTrue(path.exists())


class ProgressProtocolTest(StackFixture):
    def setUp(self):
        super().setUp()
        self.event_log = self.root / "progress.jsonl"

    def reporter(self, now=1000.0):
        output = []
        reporter = MODULE.ProgressReporter(
            event_log=self.event_log,
            output=output.append,
            wall_time=lambda: now,
        )
        return reporter, output

    def test_transitions_include_pass_pr_stage_wait_and_next_action(self):
        reporter, output = self.reporter()
        reporter(
            {
                "event": "phase_started",
                "phase": MODULE.STAGE_COPILOT_REVIEW,
                "pull_request_pass": 1,
                "numbers": [11, 12],
            }
        )
        reporter(
            {
                "event": "worker_finished",
                "stage": MODULE.STAGE_COPILOT_REVIEW,
                "pull_request_pass": 1,
                "number": 11,
                "returncode": 1,
                "accepted": True,
            }
        )

        updates = MODULE.read_progress_log(self.event_log)
        self.assertEqual(2, len(updates))
        self.assertEqual(2, len(output))
        self.assertIn("Pass 1/2", updates[0]["message"])
        self.assertIn("#11, #12", updates[0]["message"])
        self.assertEqual(MODULE.STAGE_COPILOT_REVIEW, updates[0]["stage"])
        self.assertIn("starting workers", updates[0]["wait_reason"])
        self.assertTrue(updates[0]["next_action"])
        self.assertIn("failed for #11", updates[1]["message"])

    def test_worker_progress_names_known_failure_diagnostics(self):
        reporter, _ = self.reporter()
        reporter(
            {
                "event": "worker_progress",
                "stage": MODULE.STAGE_CI,
                "pull_request_pass": 1,
                "number": 11,
                "phase": "diagnosing",
                "action_checks": ["check:build"],
                "pending_checks": ["check:test"],
            }
        )
        update = MODULE.read_progress_log(self.event_log)[0]
        self.assertIn("diagnosing 1 known failure", update["message"])
        self.assertIn("diagnosing a known CI failure", update["wait_reason"])

    def test_real_scheduler_events_keep_pass_and_pull_request_context(self):
        self.clear_everything()
        reporter, _ = self.reporter()
        pipeline = self.pipeline(report=reporter)

        pipeline.execute()

        updates = MODULE.read_progress_log(self.event_log)
        finished = next(
            update
            for update in updates
            if update["source_event"] == "worker_finished"
        )
        phase = next(
            update
            for update in updates
            if update["source_event"] == "phase_finished"
        )
        self.assertEqual(1, finished["pull_request_pass"])
        self.assertEqual([11], finished["pull_requests"])
        self.assertEqual(1, phase["pull_request_pass"])
        self.assertEqual([11], phase["pull_requests"])

    def test_unchanged_wait_transitions_are_coalesced(self):
        reporter, _ = self.reporter()
        event = {
            "event": "worker_wait_started",
            "stage": MODULE.STAGE_CI,
            "pull_request_pass": 1,
            "number": 11,
        }
        reporter(event)
        reporter(event)

        self.assertEqual(1, len(MODULE.read_progress_log(self.event_log)))

    def test_reporting_failures_do_not_escape_into_pipeline_control_flow(self):
        def fail(_payload):
            raise OSError("closed output")

        reporter = MODULE.ProgressReporter(
            event_log=self.root,
            output=fail,
        )
        reporter({"event": "pass_started", "pull_request_pass": 1})
        MODULE.report_safely(fail, "worker_active", number=11)

    def test_scheduler_command_carries_the_monitor_handle_and_options(self):
        args = MODULE.build_parser().parse_args(
            [
                "start",
                "--kickoff",
                json.dumps(kickoff()),
                "--stage-model",
                "ci-fix-loop=claude-sonnet-5",
                "--effort",
                "high",
            ]
        )
        command = MODULE.scheduler_command(
            args,
            kickoff(),
            self.root,
            "a" * 32,
            self.event_log,
        )

        self.assertIn("run", command)
        self.assertIn("--run-id", command)
        self.assertIn("a" * 32, command)
        self.assertIn("--event-log", command)
        self.assertIn("ci-fix-loop=claude-sonnet-5", command)

    def test_watch_emits_one_heartbeat_only_after_five_unchanged_minutes(self):
        class Clock:
            def __init__(self):
                self.value = 1000.0

            def now(self):
                return self.value

            def sleep(self, seconds):
                self.value += seconds

        clock = Clock()
        reporter = MODULE.ProgressReporter(
            event_log=self.event_log,
            output=lambda _payload: None,
            wall_time=clock.now,
        )
        reporter(
            {
                "event": "worker_wait_started",
                "stage": MODULE.STAGE_CI,
                "pull_request_pass": 1,
                "number": 11,
            }
        )
        launch = self.root / "launch.json"
        observer = self.root / "observer.json"
        COMMON.write_json_atomically(launch, {"pid": 123})

        initial = MODULE.watch_progress(
            event_log=self.event_log,
            launch_path=launch,
            observer_path=observer,
            cursor=0,
            wait_seconds=1,
            wall_time=clock.now,
            monotonic=clock.now,
            sleep=clock.sleep,
            alive=lambda _pid: True,
        )
        with mock.patch.object(COMMON, "PROGRESS_WATCH_POLL_INTERVAL", 299):
            early = MODULE.watch_progress(
                event_log=self.event_log,
                launch_path=launch,
                observer_path=observer,
                cursor=initial["cursor"],
                wait_seconds=299,
                wall_time=clock.now,
                monotonic=clock.now,
                sleep=clock.sleep,
                alive=lambda _pid: True,
            )
        due = MODULE.watch_progress(
            event_log=self.event_log,
            launch_path=launch,
            observer_path=observer,
            cursor=initial["cursor"],
            wait_seconds=1,
            wall_time=clock.now,
            monotonic=clock.now,
            sleep=clock.sleep,
            alive=lambda _pid: True,
        )
        again = MODULE.watch_progress(
            event_log=self.event_log,
            launch_path=launch,
            observer_path=observer,
            cursor=initial["cursor"],
            wait_seconds=1,
            wall_time=clock.now,
            monotonic=clock.now,
            sleep=clock.sleep,
            alive=lambda _pid: True,
        )

        self.assertEqual(1, len(initial["updates"]))
        self.assertEqual([], early["updates"])
        self.assertEqual("heartbeat", due["updates"][0]["kind"])
        self.assertEqual(300, due["updates"][0]["elapsed_seconds"])
        self.assertEqual([], again["updates"])

    def test_watch_rechecks_the_journal_after_the_scheduler_exits(self):
        reporter, _ = self.reporter()
        reporter(
            {
                "event": "worker_wait_started",
                "stage": MODULE.STAGE_CI,
                "pull_request_pass": 1,
                "number": 11,
            }
        )
        launch = self.root / "launch.json"
        observer = self.root / "observer.json"
        COMMON.write_json_atomically(launch, {"pid": 123})
        MODULE.watch_progress(
            event_log=self.event_log,
            launch_path=launch,
            observer_path=observer,
            cursor=0,
            wait_seconds=1,
            alive=lambda _pid: True,
        )

        def finish_before_exit(_pid):
            reporter(
                {
                    "event": "stack_pipeline_finished",
                    "result": "complete",
                    "run_id": "run-1",
                }
            )
            return False

        result = MODULE.watch_progress(
            event_log=self.event_log,
            launch_path=launch,
            observer_path=observer,
            cursor=1,
            wait_seconds=1,
            alive=finish_before_exit,
        )

        self.assertTrue(result["finished"])
        self.assertNotIn("monitor_failure", result)
        self.assertEqual("complete", result["updates"][0]["final_event"]["result"])

    def test_missing_launch_record_stops_the_monitor(self):
        result = MODULE.watch_progress(
            event_log=self.event_log,
            launch_path=self.root / "missing.json",
            observer_path=self.root / "observer.json",
            cursor=0,
            wait_seconds=1,
        )

        self.assertTrue(result["finished"])
        self.assertEqual("launch_record_missing", result["monitor_failure"])


class DependencyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_missing_stage_plugins_are_named(self):
        missing = MODULE.missing_dependencies(
            script_for=lambda entry: self.root / f"{entry['module']}.py"
        )
        self.assertEqual(list(MODULE.STAGE_NAMES), missing)

    def test_propagation_calls_the_conflict_plugin(self):
        script = self.root / "conflict_fix_loop.py"
        script.write_text("", encoding="utf-8")
        seen: list[list[str]] = []

        class Result:
            returncode = 0
            stdout = json.dumps({"result": "published", "members_published": []})
            stderr = ""

        def runner(command, **_options):
            seen.append(command)
            return Result()

        outcome = MODULE.propagate_descendants(
            "owner/repo",
            11,
            "a" * 40,
            77,
            script_for=lambda entry: script,
            runner=runner,
        )

        self.assertEqual("published", outcome["result"])
        self.assertEqual(
            [
                "descendant-propagate",
                "--repo",
                "owner/repo",
                "--pull-request",
                "11",
                "--head-sha",
                "a" * 40,
                "--stack-number",
                "77",
            ],
            seen[0][2:],
        )

    def test_propagation_reports_an_uninstalled_conflict_plugin(self):
        outcome = MODULE.propagate_descendants(
            "owner/repo",
            11,
            "a" * 40,
            77,
            script_for=lambda entry: self.root / "absent.py",
            runner=lambda *args, **kwargs: None,
        )
        self.assertEqual("unavailable", outcome["result"])
        self.assertEqual("plugin_not_installed", outcome["reason"])

    def test_accepted_pushes_are_read_from_the_ci_stage_state(self):
        state = self.root / "ci.json"
        state.write_text(
            json.dumps({"accepted_pushes": [{"id": "push-1", "head_sha": "a" * 40}]}),
            encoding="utf-8",
        )

        checkpoints = MODULE.accepted_push_checkpoints(
            "owner/repo", 11, state_for=lambda entry, target: state
        )

        self.assertEqual([{"id": "push-1", "head_sha": "a" * 40}], checkpoints)

    def test_a_missing_ci_state_reports_no_checkpoints(self):
        self.assertEqual(
            [],
            MODULE.accepted_push_checkpoints(
                "owner/repo", 11, state_for=lambda entry, target: self.root / "absent.json"
            ),
        )


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.launcher = MODULE.WorkerLauncher(
            repo_root=self.root / "repo",
            repository="owner/repo",
            run_id="run-1",
            run_directory=self.root / "run",
            models=COMMON.stage_models(None),
            effort="high",
            readiness_timeout=1.0,
            poll_interval=0.0,
            sleep=lambda _seconds: None,
            monotonic=self.clock,
        )
        self.ticks = 0

    def clock(self) -> float:
        self.ticks += 1
        return float(self.ticks)

    def request(self, number: int = 11) -> dict:
        return {
            "number": number,
            "stage": MODULE.STAGE_CI,
            "pass": 1,
            "nonce": "nonce-1",
            "head_sha": head_of(number),
            "arguments": [],
            "prompt": "owner/repo#11",
        }

    def test_a_worktree_this_run_does_not_own_is_refused(self):
        path = MODULE.worktree_path(self.launcher.run_directory, 11)
        path.mkdir(parents=True)

        created = self.launcher.create(self.request())

        self.assertEqual("failed", created["result"])
        self.assertEqual("worktree_is_not_owned_by_this_run", created["reason"])

    def test_verification_needs_this_run_s_ownership_record(self):
        verified = self.launcher.verify(
            self.request(), MODULE.worktree_path(self.launcher.run_directory, 11)
        )
        self.assertEqual("failed", verified["result"])
        self.assertEqual("worktree_ownership_missing", verified["reason"])

    def test_readiness_needs_durable_evidence(self):
        request = self.request()
        record = self.launcher.record_path(request)
        log = self.launcher.log_path(request)
        record.parent.mkdir(parents=True, exist_ok=True)
        log.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("{}", encoding="utf-8")
        log.write_text("working\n", encoding="utf-8")

        ready = self.launcher.confirm_ready(
            request,
            {
                "handle": FakeHandle(alive_polls=5),
                "pid": 99,
                "log_path": log,
                "record_path": record,
            },
        )

        self.assertEqual("active", ready["result"])
        self.assertGreater(ready["evidence"]["log_bytes"], 0)

    def test_a_worker_that_exits_before_writing_output_is_a_failed_launch(self):
        request = self.request()
        record = self.launcher.record_path(request)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("{}", encoding="utf-8")

        ready = self.launcher.confirm_ready(
            request,
            {
                "handle": FakeHandle(alive_polls=0, returncode=1),
                "pid": 99,
                "log_path": self.launcher.log_path(request),
                "record_path": record,
            },
        )

        self.assertEqual("failed", ready["result"])
        self.assertEqual("worker_exited_before_readiness", ready["reason"])

    def test_readiness_times_out_instead_of_waiting_forever(self):
        request = self.request()
        ready = self.launcher.confirm_ready(
            request,
            {
                "handle": FakeHandle(alive_polls=100),
                "pid": 99,
                "log_path": self.launcher.log_path(request),
                "record_path": self.launcher.record_path(request),
            },
        )

        self.assertEqual("failed", ready["result"])
        self.assertEqual("worker_readiness_timeout", ready["reason"])


class AgentInstructionTest(unittest.TestCase):
    def setUp(self):
        self.text = AGENT.read_text(encoding="utf-8")

    def test_the_agent_only_runs_and_reports_the_helper(self):
        self.assertIn('pr_stack_pipeline.py" start --kickoff', self.text)
        self.assertIn('pr_stack_pipeline.py" watch --kickoff', self.text)
        self.assertIn("The helper owns all control flow", self.text)
        self.assertIn("Run `start` synchronously exactly once", self.text)
        self.assertIn("--wait-seconds 300", self.text)
        self.assertIn("no more than one per five minutes", self.text)
        self.assertIn("Never end your turn", self.text)
        self.assertIn("`final_event`", self.text)
        watch_lines = [
            line
            for line in self.text.splitlines()
            if "pr_stack_pipeline.py" in line and " watch " in line
        ]
        self.assertTrue(any("copilot_home=" in line for line in watch_lines))
        self.assertTrue(any("$copilotHome =" in line for line in watch_lines))

    def test_progress_belongs_in_the_session_conversation(self):
        self.assertIn("visible assistant line in this session conversation", self.text)
        self.assertIn("Waiting: <wait_reason>.", self.text)
        self.assertIn("Next: <next_action>.", self.text)
        self.assertIn("Do not send these updates to the PR Flight canvas", self.text)
        self.assertIn("If `updates` is empty, call `watch` again", self.text)

    def test_the_agent_states_the_session_title(self):
        self.assertIn(
            "PR Stack Pipeline: #<startPullRequest> - <PR title>",
            self.text,
        )
        self.assertIn("After monitoring finishes, rename the session", self.text)

    def test_the_agent_documents_the_kickoff_schema(self):
        self.assertIn('"version":1', self.text)
        self.assertIn('"stackNumber"', self.text)
        self.assertIn('"startPullRequest"', self.text)
        self.assertIn('"pullRequests"', self.text)
        self.assertIn("Draft and non-draft members are both included", self.text)

    def test_the_agent_names_the_delegated_agents_in_order(self):
        for agent in MODULE.PHASE_AGENTS.values():
            self.assertIn(agent, self.text)
        self.assertLess(
            self.text.index("copilot-review-loop:copilot-review-loop"),
            self.text.index("self-review-loop:self-review-loop"),
        )
        self.assertLess(
            self.text.index("self-review-loop:self-review-loop"),
            self.text.index("ci-fix-loop:ci-fix-loop"),
        )

    def test_the_agent_owns_no_stage_policy(self):
        self.assertIn("Do not launch stages yourself", self.text)
        self.assertIn("not app sessions", self.text)
        self.assertNotIn("mergeable_at_head_sha", self.text)
        self.assertNotIn("clean_at_head_sha", self.text)


class ParserTest(unittest.TestCase):
    def test_the_progress_protocol_adds_start_and_watch_commands(self):
        parser = MODULE.build_parser()
        action = next(
            action
            for action in parser._actions
            if isinstance(action, __import__("argparse")._SubParsersAction)
        )
        self.assertEqual({"run", "start", "watch"}, set(action.choices))

    def test_run_accepts_a_kickoff_payload_and_model_overrides(self):
        args = MODULE.build_parser().parse_args(
            [
                "run",
                "--kickoff",
                json.dumps(kickoff()),
                "--stage-model",
                "ci-fix-loop=claude-sonnet-5",
            ]
        )
        self.assertEqual(kickoff(), MODULE.load_kickoff(args))
        self.assertEqual(["ci-fix-loop=claude-sonnet-5"], args.stage_model)

    def test_watch_accepts_a_bounded_wait_and_cursor(self):
        args = MODULE.build_parser().parse_args(
            [
                "watch",
                "--kickoff",
                json.dumps(kickoff()),
                "--run-id",
                "a" * 32,
                "--cursor",
                "4",
                "--wait-seconds",
                "300",
            ]
        )
        self.assertEqual(4, args.cursor)
        self.assertEqual(300, args.wait_seconds)

    def test_the_run_emits_json_lines_ending_with_the_final_event(self):
        args = MODULE.build_parser().parse_args(
            ["run", "--kickoff", json.dumps(kickoff()), "--repo-root", "."]
        )
        output = StringIO()

        class FakePipeline:
            def __init__(self, *args, **kwargs):
                self.report = kwargs["report"]

            def execute(self):
                self.report({"event": "pass_started", "pull_request_pass": 1})
                return {"result": "complete", "run_id": "run-1"}

        with (
            mock.patch.object(MODULE.common, "require_tools"),
            mock.patch.object(MODULE, "StackPipeline", FakePipeline),
            redirect_stdout(output),
        ):
            MODULE.command_run(args)

        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            ["pass_started", "stack_pipeline_finished"],
            [event["event"] for event in events],
        )
        self.assertEqual("complete", events[-1]["result"])

    def test_an_error_is_a_terminal_json_event(self):
        output = StringIO()
        with (
            mock.patch.object(
                MODULE.common,
                "require_tools",
                side_effect=MODULE.WorkflowError("broken"),
            ),
            mock.patch.object(
                __import__("sys"),
                "argv",
                ["pr_stack_pipeline.py", "run", "--kickoff", json.dumps(kickoff())],
            ),
            redirect_stdout(output),
        ):
            result = MODULE.main()

        self.assertEqual(1, result)
        event = json.loads(output.getvalue())
        self.assertEqual("stack_pipeline_finished", event["event"])
        self.assertEqual("error", event["result"])
        self.assertEqual("broken", event["error"])


if __name__ == "__main__":
    unittest.main()
