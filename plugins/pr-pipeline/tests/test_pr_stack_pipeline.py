from contextlib import redirect_stdout
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
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


class InstalledRuntimeTest(unittest.TestCase):
    def test_dynamic_common_import_does_not_write_bytecode(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            installed = Path(raw_directory)
            copied_script = installed / SCRIPT.name
            copied_script.write_bytes(SCRIPT.read_bytes())
            (installed / COMMON_SCRIPT.name).write_bytes(COMMON_SCRIPT.read_bytes())
            environment = os.environ.copy()
            environment.pop("PYTHONDONTWRITEBYTECODE", None)
            completed = subprocess.run(
                [os.fsdecode(os.environ.get("PYTHON", "python")), str(copied_script), "--help"],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                ),
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertFalse((installed / "__pycache__").exists())


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
        polls = (
            self.alive_polls.get(request["number"], 0)
            if isinstance(self.alive_polls, dict)
            else self.alive_polls
        )
        return {
            "result": "started",
            "handle": FakeHandle(alive_polls=polls),
            "pid": 1000 + request["number"],
            "log_path": Path(f"/logs/{request['number']}.log"),
            "record_path": Path(f"/records/{request['number']}.json"),
        }

    def confirm_ready(self, request, started, *, should_cancel=None):
        self.calls.append(("confirm_ready", request["number"]))
        if should_cancel is not None and should_cancel():
            return {"result": "cancelled"}
        if self._fails("readiness", request):
            return {"result": "failed", "reason": "worker_readiness_timeout"}
        return {"result": "active", "evidence": {"pid": started["pid"]}}

    def cancel(self, started):
        number = started["pid"] - 1000
        self.calls.append(("cancel", number))
        return {"returncode": started["handle"].wait()}

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
                "pr-conflict-resolver": "pr-conflict-resolver:pr-conflict-resolver",
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
                "pr-conflict-resolver",
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
        for stage in MODULE.PHASE_NAMES:
            self.assertEqual("gpt-5.6-sol", models[stage])

    def test_ci_workers_run_the_coordinator_directly(self):
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_CI]
        target = COMMON.target_for("owner/repo", 11)
        with mock.patch.object(
            COMMON,
            "stage_script_path",
            return_value=Path("installed-ci-fix-loop.py"),
        ):
            command = COMMON.stage_command(
                entry,
                target,
                model="gpt-5.6-sol",
                effort="high",
                arguments=[
                    "--pipeline-run",
                    "run-1",
                    "--pipeline-iteration",
                    "1",
                    "--pipeline-max-iterations",
                    "2",
                    "--state",
                    "state.json",
                ],
                prompt="ignored for direct CI execution",
                resolve_program=lambda name: name,
            )
        self.assertEqual(
            [
                MODULE.sys.executable,
                "installed-ci-fix-loop.py",
                "pipeline",
                "owner/repo#11",
                "--model",
                "sol",
                "--pipeline-run",
                "run-1",
                "--pipeline-iteration",
                "1",
                "--pipeline-max-iterations",
                "2",
                "--state",
                "state.json",
            ],
            command,
        )

    def test_stack_workers_forward_source_only_to_pr_description(self):
        previous = COMMON.ACTIVE_GITHUB_MUTATION_POLICY
        COMMON.ACTIVE_GITHUB_MUTATION_POLICY = "source-only"
        self.addCleanup(
            setattr, COMMON, "ACTIVE_GITHUB_MUTATION_POLICY", previous
        )
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_DESCRIPTION]
        command = COMMON.stage_command(
            entry,
            COMMON.target_for("owner/repo", 11),
            model="gpt-5.6-sol",
            effort="high",
            arguments=["--pipeline-run", "a" * 32],
            prompt="frozen worker prompt",
            resolve_program=lambda name: name,
        )

        prompt = command[command.index("-p") + 1]
        self.assertIn("--github-mutation-policy source-only", prompt)
        self.assertIn("immutable argument", prompt)

    def test_stack_pipeline_freezes_source_only_in_run_state(self):
        previous = COMMON.ACTIVE_GITHUB_MUTATION_POLICY
        self.addCleanup(
            setattr, COMMON, "ACTIVE_GITHUB_MUTATION_POLICY", previous
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = MODULE.StackPipeline(
                kickoff(),
                root,
                models=COMMON.stage_models(None),
                effort="high",
                github_mutation_policy="source-only",
                run_id="a" * 32,
                state_path=root / "state.json",
                lock_path=root / "state.lock",
                run_directory=root / "run",
            )
            pipeline.state = MODULE.new_state(
                kickoff(), pipeline.run_id, "fingerprint"
            )
            pipeline.state["github_mutation_policy"] = (
                pipeline.github_mutation_policy
            )
            pipeline.save()

            saved = COMMON.read_json(pipeline.state_path)
            self.assertEqual(
                "source-only", saved["github_mutation_policy"]
            )
            self.assertEqual(
                "source-only", COMMON.ACTIVE_GITHUB_MUTATION_POLICY
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
        self.completed: set[tuple[int, str]] = set()
        self.attempt_ids: dict[tuple[int, str], str] = {}
        self.nonce_count = 0
        self.checkpoint_map: dict[int, list[dict]] = {}
        self.worker_progress_map: dict[int, dict] = {}
        self.propagated: list[tuple[int, str]] = []
        self.contains_pairs: set[tuple[str, str]] | None = None
        self.inspect_sequences: dict[tuple[int, str], list[dict]] = {}

    def next_nonce(self) -> str:
        self.nonce_count += 1
        return f"nonce-{self.nonce_count}"

    def read_stack(self, repository, number):
        return self.stack

    def inspect(self, entry, target, head_sha, base_sha=None):
        clear = (target["number"], entry["stage"]) in self.clear
        completed = (target["number"], entry["stage"]) in self.completed
        result = {
            "stage": entry["stage"],
            "clear": clear,
            "clear_at_head_sha": head_sha if clear else None,
            "clear_at_base_sha": None,
            "outcome": "cleared" if clear else "completed" if completed else "carried",
            "reason": None if clear else "completed" if completed else "carried",
            "installed": True,
            "status_state": "state.json",
            "status": {
                "attempt": {
                    "id": self.attempt_ids.get(
                        (target["number"], entry["stage"]), "old-attempt"
                    )
                }
            },
        }
        sequence = self.inspect_sequences.get((target["number"], entry["stage"]))
        if sequence:
            result.update(sequence.pop(0))
        return result

    def checkpoints(self, repository, number):
        return self.checkpoint_map.get(number, [])

    def worker_progress(self, repository, number, stage):
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
        self.assertEqual("pr-conflict-resolver:pr-conflict-resolver", request["agent"])
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

    def test_parallel_workers_are_collected_in_completion_order(self):
        self.launcher = FakeLauncher(alive_polls={11: 3, 12: 0, 13: 1})
        pipeline = self.pipeline()

        result = pipeline.run_parallel_phase(
            MODULE.STAGE_COPILOT_REVIEW, 1, self.stack["members"]
        )

        self.assertEqual(
            [12, 13, 11],
            [event["number"] for event in self.events_named("worker_finished")],
        )
        self.assertEqual(
            [11, 12, 13],
            [completion["number"] for completion in result["completions"]],
        )

    def test_finished_worker_is_removed_before_its_event_is_emitted(self):
        pipeline = self.pipeline()
        active_at_finish = {}

        def report(event):
            if event["event"] == "worker_finished":
                active_at_finish[event["number"]] = [
                    worker["number"]
                    for worker in pipeline.state.get("active_workers", [])
                ]

        pipeline.report = report
        pipeline.run_parallel_phase(
            MODULE.STAGE_COPILOT_REVIEW, 1, self.stack["members"]
        )

        for number in (11, 12, 13):
            self.assertNotIn(number, active_at_finish[number])

    def test_active_agent_task_blocks_each_fresh_run_without_duplicates(self):
        self.stack = stack(members=(11,))
        active = {
            "reason": "not_cleared",
            "status": {
                "agent_task": {
                    "status": "running",
                    "task_id": "task-1",
                }
            },
        }
        self.inspect_sequences[(11, MODULE.STAGE_CONFLICT)] = [active, active]

        first = self.pipeline(kickoff([11]))
        first_result = first.execute()
        second = self.pipeline(
            kickoff([11]),
            run_id="run-2",
            state_path=self.root / "state-2.json",
            run_directory=self.root / "run-2",
        )
        second_result = second.execute()

        self.assertEqual("stage_still_active", first_result["reason"])
        self.assertEqual("stage_still_active", second_result["reason"])
        self.assertIn("task-1", first_result["detail"])
        self.assertEqual([], [call for call in self.launcher.calls if call[0] == "start"])

    def test_post_exit_active_agent_task_blocks_the_pipeline(self):
        self.stack = stack(members=(11,))
        self.inspect_sequences[(11, MODULE.STAGE_COPILOT_REVIEW)] = [
            {},
            {
                "reason": "not_cleared",
                "status": {
                    "agent_task": {
                        "status": "running",
                        "task_id": "task-1",
                    }
                },
            },
        ]
        pipeline = self.pipeline(kickoff([11]))

        result = pipeline.run_parallel_phase(
            MODULE.STAGE_COPILOT_REVIEW, 1, self.stack["members"]
        )

        self.assertEqual("stage_still_active", result["stopped"]["reason"])
        self.assertEqual("blocked", self.events_named("worker_finished")[0]["status"])

    def test_description_without_outcome_has_a_specific_blocking_reason(self):
        self.stack = stack(members=(11,))
        self.inspect_sequences[(11, MODULE.STAGE_DESCRIPTION)] = [
            {},
            {
                "outcome": None,
                "reason": "not_cleared",
                "status": {"validation": None, "agent_task": {"status": "completed"}},
            },
        ]
        pipeline = self.pipeline(kickoff([11]))

        result = pipeline.run_parallel_phase(
            MODULE.STAGE_DESCRIPTION, 1, self.stack["members"]
        )

        self.assertEqual(
            "description_did_not_record_outcome", result["stopped"]["reason"]
        )

    def test_stale_clearance_is_collected_but_not_reported_complete(self):
        self.stack = stack(members=(11,))
        old_head = "9" * 40
        self.inspect_sequences[(11, MODULE.STAGE_COPILOT_REVIEW)] = [
            {},
            {
                "clear": False,
                "clear_at_head_sha": old_head,
                "outcome": "cleared",
                "reason": "clearance_is_for_an_older_head",
            },
        ]
        pipeline = self.pipeline(kickoff([11]))

        result = pipeline.run_parallel_phase(
            MODULE.STAGE_COPILOT_REVIEW, 1, self.stack["members"]
        )

        completion = result["completions"][0]
        self.assertFalse(completion["clear"])
        self.assertEqual("clearance_is_for_an_older_head", completion["reason"])
        self.assertIsNone(result["stopped"])
        phase = self.events_named("phase_finished")[0]
        self.assertFalse(phase["clear"])
        self.assertEqual(["clearance_is_for_an_older_head"], phase["reasons"])

    def test_parallel_worker_progress_is_polled_and_coalesced_for_every_worker(self):
        self.launcher = FakeLauncher(alive_polls=2)
        for number in (11, 12, 13):
            self.worker_progress_map[number] = {
                "phase": "addressing_comments",
                "observed_at": "2026-08-31T12:00:00Z",
            }
        pipeline = self.pipeline()

        pipeline.run_parallel_phase(
            MODULE.STAGE_COPILOT_REVIEW, 1, self.stack["members"]
        )

        self.assertEqual(
            [11, 12, 13],
            [event["number"] for event in self.events_named("worker_progress")],
        )

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

    def test_cancellation_during_serialized_launch_stops_later_workers(self):
        pipeline = self.pipeline()

        def request_cancel(request):
            if request["number"] == 11:
                COMMON.write_json_atomically(
                    pipeline.cancellation_path,
                    {
                        "kind": MODULE.RUN_KIND,
                        "run_id": pipeline.run_id,
                        "kickoff": pipeline.kickoff,
                        "status": "requested",
                    },
                )

        self.launcher.on_start = request_cancel
        requests = [
            pipeline.request_for(member, MODULE.STAGE_COPILOT_REVIEW, 1)
            for member in self.stack["members"]
        ]

        with self.assertRaises(MODULE.PipelineCancelled):
            pipeline.dispatch(requests, MODULE.STAGE_COPILOT_REVIEW, 1)

        self.assertEqual(
            [11], [call[1] for call in self.launcher.calls if call[0] == "start"]
        )
        self.assertIn(("cancel", 11), self.launcher.calls)
        self.assertEqual([], pipeline.state["active_workers"])

    def test_cancellation_during_parallel_execution_stops_all_live_workers(self):
        self.launcher = FakeLauncher(alive_polls=100)
        pipeline = self.pipeline()
        requested = False

        def cancel_on_first_wait(_seconds):
            nonlocal requested
            if not requested:
                requested = True
                COMMON.write_json_atomically(
                    pipeline.cancellation_path,
                    {
                        "kind": MODULE.RUN_KIND,
                        "run_id": pipeline.run_id,
                        "kickoff": pipeline.kickoff,
                        "status": "requested",
                    },
                )

        pipeline.sleep = cancel_on_first_wait
        with self.assertRaises(MODULE.PipelineCancelled):
            pipeline.run_parallel_phase(
                MODULE.STAGE_COPILOT_REVIEW, 1, self.stack["members"]
            )
        pipeline.cancel_active_workers()

        self.assertEqual(
            [11, 12, 13],
            [call[1] for call in self.launcher.calls if call[0] == "cancel"],
        )
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

    def test_cancellation_prevents_predecessor_alignment(self):
        self.contains_pairs = set()
        pipeline = self.pipeline()
        self.clear.add((11, MODULE.STAGE_CI))
        COMMON.write_json_atomically(
            pipeline.cancellation_path,
            {
                "kind": MODULE.RUN_KIND,
                "run_id": pipeline.run_id,
                "kickoff": pipeline.kickoff,
                "status": "requested",
            },
        )

        with self.assertRaises(MODULE.PipelineCancelled):
            pipeline.run_ci_phase(1, self.stack["members"])

        self.assertEqual([], self.propagated)

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

    def test_all_already_clear_ci_members_report_verified_clearance(self):
        for member in self.stack["members"]:
            self.clear.add((member["number"], MODULE.STAGE_CI))
        pipeline = self.pipeline()

        result = pipeline.run_ci_phase(1, self.stack["members"])

        self.assertTrue(result["clear"])
        self.assertEqual(0, result["dispatches"])
        event = self.events_named("phase_finished")[0]
        self.assertTrue(event["clear"])

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
                "--state",
                str(
                    MODULE.stage_state_path(
                        MODULE.STAGE_BY_NAME[MODULE.STAGE_CI],
                        COMMON.target_for("owner/repo", member["number"]),
                        "run-1",
                    )
                ),
            ],
            request["arguments"],
        )

    def test_the_conflict_stage_carries_state_and_strategy(self):
        """PR Conflict Resolver integrates once per launch and takes no budget.

        Its helper rejects pipeline position flags, but an explicit canonical
        state path keeps the worker and scheduler on the same durable record.
        """
        pipeline = self.pipeline(conflict_strategy="merge")
        member = self.stack["members"][0]
        request = pipeline.request_for(member, MODULE.STAGE_CONFLICT, 2)
        self.assertEqual(
            [
                "--state",
                str(
                    MODULE.stage_state_path(
                        MODULE.STAGE_BY_NAME[MODULE.STAGE_CONFLICT],
                        COMMON.target_for("owner/repo", member["number"]),
                        "run-1",
                    )
                ),
                "--strategy",
                "merge",
            ],
            request["arguments"],
        )

    def test_taskless_resolver_failures_abandon_the_invocation(self):
        failures = (
            (
                "normalized_stack",
                "failed",
                {
                    "code": "conflict_preflight_failed",
                    "message": (
                        "commit 5e98da713ed1c6b2146439fcbeacd32e95c33d36 "
                        "is not a supported linear commit"
                    ),
                },
                {},
            ),
            (
                "updated_plugin",
                "failed",
                {
                    "code": "stale_target",
                    "message": "a checked-out branch is required",
                },
                {},
            ),
            (
                "changed_descendant",
                "normalization_required",
                {
                    "code": "native_stack_normalization_required",
                    "message": (
                        "native stack member requires explicit owner normalization "
                        "for merge commits: "
                        "056730d0fe9f0d57dcf8ebcf3e4e9c8689c515dc"
                    ),
                },
                {
                    "normalization_sha256": (
                        "b3b4adfa42790ccc60becc91e28492ce1578cc259706298784fa2b2a032954d6"
                    ),
                },
            ),
        )
        for case, status, error, retained in failures:
            with self.subTest(case=case):
                pipeline = self.pipeline()
                member = self.stack["members"][0]
                self.inspect_sequences[(member["number"], MODULE.STAGE_CONFLICT)] = [
                    {
                        "status": {
                            "agent_task": {
                                "status": status,
                                "task_id": None,
                                "task_id_status": "not_created",
                                "error": error,
                                **retained,
                            }
                        }
                    }
                ]
                request = pipeline.request_for(member, MODULE.STAGE_CONFLICT, 1)

                launched = pipeline.dispatch([request], MODULE.STAGE_CONFLICT, 1)

                self.assertEqual(
                    "stage_invocation_abandoned",
                    launched["stopped"]["reason"],
                )
                self.assertEqual([], launched["workers"])

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

    def test_cancellation_prevents_a_final_ci_propagation_after_worker_exit(self):
        pipeline = self.pipeline()
        member = self.stack["members"][0]
        request = pipeline.request_for(member, MODULE.STAGE_CI, 1)
        launched = pipeline.dispatch([request], MODULE.STAGE_CI, 1)
        self.checkpoint_map[11] = [
            {
                "id": "push-1",
                "head_sha": head_of(11),
                "pipeline_run": "run-1",
                "pipeline_iteration": 1,
            }
        ]
        COMMON.write_json_atomically(
            pipeline.cancellation_path,
            {
                "kind": MODULE.RUN_KIND,
                "run_id": pipeline.run_id,
                "kickoff": pipeline.kickoff,
                "status": "requested",
            },
        )

        with self.assertRaises(MODULE.PipelineCancelled):
            pipeline.monitor_ci_worker(launched["workers"][0], request)

        self.assertEqual([], self.propagated)

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
            ["pr-conflict-resolver", "copilot-review-loop", "self-review-loop", "ci-fix-loop", "pr-description"],
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

    def test_second_pass_does_not_relaunch_a_completed_conflict_resolution(self):
        self.completed.add((11, MODULE.STAGE_CONFLICT))
        def complete_with_current_clearance(request):
            self.attempt_ids[(request["number"], request["stage"])] = "current-attempt"
            self.clear.add((request["number"], request["stage"]))

        self.launcher.on_start = complete_with_current_clearance
        pipeline = self.pipeline()

        result = pipeline.execute()

        conflict_phases = [
            phase for phase in result["phases"] if phase["phase"] == MODULE.STAGE_CONFLICT
        ]
        self.assertEqual(2, len(conflict_phases))
        self.assertEqual(1, conflict_phases[0]["dispatches"])
        self.assertEqual(0, conflict_phases[1]["dispatches"])
        self.assertEqual("completed_this_run", conflict_phases[1]["action"])
        skipped = [
            event
            for event in self.events
            if event["event"] == "phase_finished"
            and event["pull_request_pass"] == 2
            and event.get("action") == "completed_this_run"
        ]
        self.assertEqual([11], skipped[0]["numbers"])

    def test_stale_completed_state_does_not_suppress_the_second_pass(self):
        self.completed.add((11, MODULE.STAGE_CONFLICT))
        pipeline = self.pipeline()

        result = pipeline.execute()

        conflict_phases = [
            phase for phase in result["phases"] if phase["phase"] == MODULE.STAGE_CONFLICT
        ]
        self.assertEqual([1, 1], [phase["dispatches"] for phase in conflict_phases])

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

    def test_cancellation_before_first_launch_finishes_the_run_as_cancelled(self):
        pipeline = self.pipeline()
        COMMON.write_json_atomically(
            pipeline.cancellation_path,
            {
                "kind": MODULE.RUN_KIND,
                "run_id": pipeline.run_id,
                "kickoff": pipeline.kickoff,
                "status": "requested",
            },
        )

        result = pipeline.execute()

        self.assertEqual("cancelled", result["result"])
        self.assertEqual("cancel_requested", result["reason"])
        self.assertEqual([], self.launcher.calls)
        self.assertEqual(
            "cancelled",
            COMMON.read_json(pipeline.result_path)["pipeline_result"]["result"],
        )
        self.assertFalse(pipeline.lock_path.exists())

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

    def test_runtime_error_cancels_workers_and_releases_the_lock(self):
        self.launcher = FakeLauncher(alive_polls=100)

        def fail_progress(_repository, _number, _stage):
            raise MODULE.WorkflowError("GitHub unavailable")

        pipeline = self.pipeline(worker_progress=fail_progress)

        result = pipeline.execute()

        self.assertEqual("error", result["result"])
        self.assertEqual("scheduler_error", result["reason"])
        self.assertIn("GitHub unavailable", result["detail"])
        self.assertIn(("cancel", 11), self.launcher.calls)
        self.assertEqual([], pipeline.state["active_workers"])
        self.assertFalse(pipeline.lock_path.exists())
        self.assertEqual(
            "error",
            COMMON.read_json(pipeline.result_path)["pipeline_result"]["result"],
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

    def test_finish_releases_the_lock_when_result_persistence_fails(self):
        pipeline = self.pipeline()
        MODULE.acquire_lock(pipeline.lock_path, pipeline.run_id)
        with (
            mock.patch.object(
                pipeline, "persist_result", side_effect=OSError("disk full")
            ),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            pipeline.finish("cancelled")
        self.assertFalse(pipeline.lock_path.exists())

    def test_a_duplicate_run_stops_on_the_lock(self):
        self.clear_everything()
        first = self.pipeline()
        MODULE.acquire_lock(first.lock_path, "other-run")
        with mock.patch.object(MODULE.common, "process_is_alive", return_value=True):
            result = first.execute()

        self.assertEqual("stopped", result["result"])
        self.assertEqual("another_run_holds_the_lock", result["reason"])
        self.assertEqual([], self.launcher.calls)
        self.assertEqual(
            "stopped",
            COMMON.read_json(first.result_path)["pipeline_result"]["result"],
        )

    def test_exact_run_path_refuses_a_sealed_state_from_an_older_owner(self):
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

        result = pipeline.execute()

        self.assertEqual("stopped", result["result"])
        self.assertEqual("run_state_already_exists", result["reason"])
        self.assertEqual([], self.launcher.calls)
        sealed = COMMON.read_json(pipeline.state_path)
        self.assertEqual("old-run", sealed["run_id"])
        self.assertEqual("old-nonce", sealed["active_workers"][0]["nonce"])

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

    def test_an_unreadable_base_is_unverified_not_stale(self):
        pipeline = self.pipeline(
            base_tip=mock.Mock(side_effect=MODULE.WorkflowError("base unavailable"))
        )

        snapshot = pipeline.final_snapshot()

        self.assertEqual("incomplete", snapshot["result"])
        self.assertEqual("base_status_unavailable", snapshot["reason"])
        self.assertTrue(
            all(
                stage["identity"] == "unverified"
                for stage in snapshot["pull_requests"][0]["stages"]
            )
        )


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

    def test_each_run_has_a_distinct_state_path(self):
        first = MODULE.state_path_for(kickoff(), "a" * 32)
        second = MODULE.state_path_for(kickoff(), "b" * 32)

        self.assertNotEqual(first, second)
        self.assertEqual("state.json", first.name)
        self.assertEqual("a" * 32, first.parent.name)
        self.assertEqual("b" * 32, second.parent.name)

    def test_new_state_never_imports_old_owner_or_result_fields(self):
        old = MODULE.new_state(kickoff(), "old-run", "old-fingerprint")
        old.update(
            result="complete",
            active_workers=[{"nonce": "old-owner"}],
            recovered_from={"run_id": "older-run"},
        )

        fresh = MODULE.new_state(kickoff(), "new-run", "new-fingerprint")

        self.assertEqual("new-run", fresh["run_id"])
        self.assertIsNone(fresh["result"])
        self.assertNotIn("active_workers", fresh)
        self.assertNotIn("recovered_from", fresh)

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


class WorktreePathTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_windows_worktrees_use_a_short_run_id_root(self):
        run_directory = self.root / ("repository-and-stack-slug-" * 10) / "run-id"
        run_id = "0b5659a09b3a4f3bb5ba1a7f467bbe38"

        root = MODULE.worktree_root_for(
            run_directory,
            run_id,
            platform_name="nt",
            environ={"LOCALAPPDATA": str(self.root / "local")},
        )

        self.assertEqual((self.root / "local" / "cpw" / run_id).resolve(), root)
        self.assertNotIn("repository-and-stack-slug", str(root))
        self.assertLessEqual(
            len(str(MODULE.worktree_path(root, 19871))),
            MODULE.WINDOWS_WORKTREE_PATH_BUDGET,
        )

    def test_windows_roots_keep_the_full_run_id_unique(self):
        environment = {"LOCALAPPDATA": str(self.root / "local")}

        first = MODULE.worktree_root_for(
            self.root / "run",
            "00000000000000000000000000000001",
            platform_name="nt",
            environ=environment,
        )
        second = MODULE.worktree_root_for(
            self.root / "run",
            "00000000000000000000000000000002",
            platform_name="nt",
            environ=environment,
        )

        self.assertNotEqual(first, second)
        self.assertEqual("00000000000000000000000000000001", first.name)
        self.assertEqual("00000000000000000000000000000002", second.name)

    def test_windows_same_run_id_uses_the_same_physical_root(self):
        arguments = {
            "run_directory": self.root / ("long-run-directory-" * 10),
            "run_id": "0b5659a09b3a4f3bb5ba1a7f467bbe38",
            "platform_name": "nt",
            "environ": {"LOCALAPPDATA": str(self.root / "local")},
        }

        original = MODULE.worktree_root_for(**arguments)
        repeated = MODULE.worktree_root_for(**arguments)

        self.assertEqual(original, repeated)

    def test_windows_rejects_a_root_that_exhausts_the_path_budget(self):
        local_app_data = self.root / ("long-local-app-data-" * 10)

        with self.assertRaisesRegex(MODULE.WorkflowError, "path budget"):
            MODULE.worktree_root_for(
                self.root / "run",
                "0" * 32,
                platform_name="nt",
                environ={"LOCALAPPDATA": str(local_app_data)},
            )

    def test_non_windows_worktrees_stay_in_the_run_directory(self):
        run_directory = self.root / "run"

        worktree_root = MODULE.worktree_root_for(
            run_directory,
            "0" * 32,
            platform_name="posix",
            environ={},
        )

        self.assertEqual(run_directory / "worktrees", worktree_root)

    def test_ownership_records_stay_in_the_run_directory(self):
        run_directory = self.root / ("long-run-directory-" * 10)

        self.assertEqual(
            run_directory / "worktrees" / "19871.worktree.json",
            MODULE.worktree_ownership_path(run_directory, 19871),
        )


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

    def test_worker_exit_without_clearance_is_reported_as_result_collected(self):
        reporter, _ = self.reporter()
        reporter(
            {
                "event": "worker_finished",
                "stage": MODULE.STAGE_DESCRIPTION,
                "pull_request_pass": 2,
                "number": 20073,
                "returncode": 0,
                "accepted": True,
                "clear": False,
                "reason": "not_cleared",
                "status": "result_collected",
                "clear_at_head_sha": "1" * 40,
                "current_head_sha": "2" * 40,
            }
        )
        reporter(
            {
                "event": "phase_finished",
                "phase": MODULE.STAGE_DESCRIPTION,
                "pull_request_pass": 2,
                "numbers": [20073],
                "clear": False,
                "reasons": ["not_cleared"],
            }
        )

        updates = MODULE.read_progress_log(self.event_log)
        self.assertIn("result collected", updates[0]["message"])
        self.assertNotIn("completed", updates[0]["message"])
        self.assertIn("clearance was not verified", updates[1]["message"])
        self.assertNotIn(" complete.", updates[1]["message"])

    def test_stale_worker_progress_names_recorded_and_live_revisions(self):
        reporter, _ = self.reporter()
        reporter(
            {
                "event": "worker_finished",
                "stage": MODULE.STAGE_COPILOT_REVIEW,
                "pull_request_pass": 1,
                "number": 11,
                "returncode": 0,
                "accepted": True,
                "clear": False,
                "reason": "clearance_is_for_an_older_head",
                "clear_at_head_sha": "1" * 40,
                "current_head_sha": "2" * 40,
            }
        )

        message = MODULE.read_progress_log(self.event_log)[0]["message"]
        self.assertIn("recorded 11111111, live 22222222", message)

    def test_a_completed_resolver_skip_is_reported_without_a_start_event(self):
        reporter, _ = self.reporter()
        reporter(
            {
                "event": "phase_finished",
                "phase": MODULE.STAGE_CONFLICT,
                "pull_request_pass": 2,
                "numbers": [11],
                "action": "completed_this_run",
            }
        )

        update = MODULE.read_progress_log(self.event_log)[0]
        self.assertIn("not run again", update["message"])
        self.assertEqual("phase_finished", update["source_event"])
        self.assertEqual([11], update["pull_requests"])

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

    def test_copilot_review_progress_names_all_live_substates(self):
        expectations = {
            "waiting_for_review": "waiting for Copilot's review",
            "addressing_comments": "addressing Copilot review comments",
            "validating": "validating Copilot review fixes",
        }
        for phase, phrase in expectations.items():
            with self.subTest(phase=phase):
                event_log = self.root / f"{phase}.jsonl"
                reporter = MODULE.ProgressReporter(
                    event_log=event_log,
                    output=lambda _payload: None,
                    wall_time=lambda: 1000.0,
                )
                reporter(
                    {
                        "event": "worker_progress",
                        "stage": MODULE.STAGE_COPILOT_REVIEW,
                        "pull_request_pass": 1,
                        "number": 11,
                        "phase": phase,
                    }
                )
                update = MODULE.read_progress_log(event_log)[0]
                self.assertIn("#11", update["message"])
                self.assertIn(phrase, update["message"])
                self.assertIn("#11", update["wait_reason"])

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
                "--conflict-strategy",
                "merge",
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
        self.assertEqual(
            "merge", command[command.index("--conflict-strategy") + 1]
        )
        self.assertEqual(
            "allow", command[command.index("--github-mutation-policy") + 1]
        )

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
        script = self.root / "pr_conflict_resolver.py"
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
            "owner/repo",
            11,
            run_id="run-1",
            state_for=lambda entry, target, run_id: state,
        )

        self.assertEqual([{"id": "push-1", "head_sha": "a" * 40}], checkpoints)

    def test_a_missing_ci_state_reports_no_checkpoints(self):
        self.assertEqual(
            [],
            MODULE.accepted_push_checkpoints(
                "owner/repo",
                11,
                run_id="run-1",
                state_for=lambda entry, target, run_id: self.root / "absent.json",
            ),
        )

    def test_live_progress_reads_the_current_run_state(self):
        state = self.root / "ci.json"
        state.write_text(
            json.dumps(
                {
                    "stage_progress": {
                        "phase": "fixing",
                        "observed_at": "2026-01-01T00:00:00Z",
                    }
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(
            MODULE, "stage_state_path", return_value=state
        ) as stage_state:
            progress = MODULE.worker_live_progress(
                "owner/repo", 11, MODULE.STAGE_CI, run_id="run-1"
            )

        self.assertEqual("fixing", progress["phase"])
        stage_state.assert_called_once_with(
            MODULE.STAGE_BY_NAME[MODULE.STAGE_CI],
            MODULE.common.target_for("owner/repo", 11),
            "run-1",
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
            worktree_root=self.root / "worktrees",
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
        path = MODULE.worktree_path(self.launcher.worktree_root, 11)
        path.mkdir(parents=True)

        created = self.launcher.create(self.request())

        self.assertEqual("failed", created["result"])
        self.assertEqual("worktree_is_not_owned_by_this_run", created["reason"])

    def test_verification_needs_this_run_s_ownership_record(self):
        verified = self.launcher.verify(
            self.request(), MODULE.worktree_path(self.launcher.worktree_root, 11)
        )
        self.assertEqual("failed", verified["result"])
        self.assertEqual("worktree_ownership_missing", verified["reason"])

    def test_cleanup_refuses_a_worktree_owned_by_another_run(self):
        path = MODULE.worktree_path(self.launcher.worktree_root, 11)
        path.mkdir(parents=True)
        record = MODULE.worktree_ownership_path(self.launcher.run_directory, 11)
        COMMON.write_json_atomically(
            record,
            {"run_id": "another-run", "path": str(path)},
        )

        cleaned = self.launcher.cleanup(11)

        self.assertEqual("failed", cleaned["result"])
        self.assertEqual("worktree_is_not_owned_by_this_run", cleaned["reason"])
        self.assertTrue(path.exists())

    def test_cleanup_removes_the_short_root_but_keeps_run_metadata(self):
        path = MODULE.worktree_path(self.launcher.worktree_root, 11)
        path.mkdir(parents=True)
        record = MODULE.worktree_ownership_path(self.launcher.run_directory, 11)
        COMMON.write_json_atomically(
            record,
            {"run_id": self.launcher.run_id, "path": str(path)},
        )

        def remove_worktree(_command, *, check):
            self.assertFalse(check)
            path.rmdir()
            return mock.Mock(returncode=0)

        with mock.patch.object(COMMON, "run", side_effect=remove_worktree):
            cleaned = self.launcher.cleanup(11)

        self.assertEqual("removed", cleaned["result"])
        self.assertFalse(self.launcher.worktree_root.exists())
        self.assertFalse(record.exists())
        self.assertTrue(self.launcher.run_directory.exists())

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

    def test_requires_the_exact_primary_model_and_exposed_effort(self):
        self.assertIn("model is exactly `gpt-5.6-sol`", self.text)
        self.assertIn(
            "effort is either exactly `high` or unavailable",
            self.text,
        )
        self.assertIn("an unavailable effort does not fail the gate", self.text)
        self.assertIn("If you cannot determine the model", self.text)
        self.assertIn("The user cannot override this gate", self.text)

    def test_requires_a_chat_only_retrospective_after_the_terminal_response(self):
        self.assertIn("## Retrospective", self.text)
        for category in (
            "**Agent**",
            "**Helper**",
            "**General instructions**",
            "**Repository**",
        ):
            self.assertIn(category, self.text)
        self.assertIn("After every terminal outcome", self.text)
        self.assertIn("Keep this advisory and", self.text)
        self.assertIn("Omit this section when the run encountered no friction", self.text)
        self.assertGreater(
            self.text.index("## Retrospective"),
            self.text.index("Write a concise final response"),
        )

    def test_the_agent_only_runs_and_reports_the_helper(self):
        self.assertIn('pr_stack_pipeline.py" start --kickoff', self.text)
        self.assertIn('pr_stack_pipeline.py" watch --run-id', self.text)
        self.assertIn('pr_stack_pipeline.py" cancel --kickoff', self.text)
        self.assertIn("Interrupting `watch` does not cancel", self.text)
        self.assertIn("The helper owns all control flow", self.text)
        self.assertIn("Run `start` synchronously exactly once", self.text)
        self.assertIn("exactly as returned", self.text)
        self.assertIn("Never reconstruct", self.text)
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
        self.assertIn(
            "If `updates` is empty, invoke the returned `next_watch.arguments`",
            self.text,
        )

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


class MonitorHandleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.payload = kickoff()
        self.run_id = "a" * 32
        self.run_root = mock.patch.object(MODULE, "run_root", return_value=self.root)
        self.run_root.start()
        self.addCleanup(self.run_root.stop)

    def write_monitor_run(self, *, launch_run_id=None):
        launch = {
            "kind": MODULE.RUN_KIND,
            "run_id": launch_run_id or self.run_id,
            "kickoff": self.payload,
            "pid": 4321,
            "event_log": str(
                MODULE.progress_log_path(self.payload, self.run_id)
            ),
            "github_mutation_policy": "source-only",
        }
        COMMON.write_json_atomically(
            MODULE.launch_state_path(self.payload, self.run_id), launch
        )
        COMMON.write_json_atomically(
            MODULE.monitor_locator_path(self.run_id),
            MODULE.monitor_locator(self.payload, self.run_id),
        )

    def test_start_returns_a_versioned_run_only_watch_handle(self):
        args = MODULE.build_parser().parse_args(
            [
                "start",
                "--kickoff",
                json.dumps(self.payload),
                "--github-mutation-policy",
                "source-only",
            ]
        )
        output = StringIO()
        process = SimpleNamespace(pid=4321, terminate=mock.Mock())
        with (
            mock.patch.object(COMMON, "resolve_repo_root", return_value=self.root),
            mock.patch.object(MODULE, "start_scheduler", return_value=process),
            mock.patch.object(
                MODULE.uuid, "uuid4", return_value=SimpleNamespace(hex=self.run_id)
            ),
            redirect_stdout(output),
        ):
            MODULE.command_start(args)

        launch = COMMON.read_json(
            MODULE.launch_state_path(self.payload, self.run_id)
        )
        locator = COMMON.read_json(MODULE.monitor_locator_path(self.run_id))
        event = json.loads(output.getvalue())
        self.assertEqual("source-only", launch["github_mutation_policy"])
        self.assertEqual(MODULE.MONITOR_SCHEMA, locator["schema"])
        self.assertEqual(MODULE.MONITOR_VERSION, locator["version"])
        self.assertEqual(self.payload, locator["kickoff"])
        self.assertEqual(
            [
                "watch",
                "--run-id",
                self.run_id,
                "--cursor",
                "0",
                "--wait-seconds",
                "300",
            ],
            event["next_watch"]["arguments"],
        )
        self.assertNotIn("--kickoff", event["next_watch"]["arguments"])

    def test_watch_uses_only_the_exact_monitor_handle(self):
        self.write_monitor_run()
        args = MODULE.build_parser().parse_args(
            ["watch", "--run-id", self.run_id, "--cursor", "4"]
        )
        output = StringIO()
        with (
            mock.patch.object(
                MODULE,
                "watch_progress",
                return_value={
                    "event": MODULE.PROGRESS_UPDATE_EVENT,
                    "cursor": 5,
                    "updates": [],
                    "finished": False,
                },
            ) as watch,
            redirect_stdout(output),
        ):
            MODULE.command_watch(args)

        watch.assert_called_once_with(
            event_log=MODULE.progress_log_path(self.payload, self.run_id),
            launch_path=MODULE.launch_state_path(self.payload, self.run_id),
            observer_path=MODULE.observer_state_path(self.payload, self.run_id),
            cursor=4,
            wait_seconds=MODULE.PROGRESS_HEARTBEAT_INTERVAL,
        )
        event = json.loads(output.getvalue())
        self.assertEqual(self.run_id, event["run_id"])
        self.assertEqual(
            MODULE.watch_arguments(self.run_id, 5),
            event["next_watch"]["arguments"],
        )

    def test_watch_never_scans_or_falls_back_to_another_run(self):
        requested = "a" * 32
        decoy = "b" * 32
        COMMON.write_json_atomically(
            MODULE.monitor_locator_path(decoy),
            MODULE.monitor_locator(self.payload, decoy),
        )
        COMMON.write_json_atomically(
            self.root / "latest.json",
            {"run_id": decoy, "kickoff": self.payload},
        )
        args = MODULE.build_parser().parse_args(
            ["watch", "--run-id", requested]
        )
        with self.assertRaisesRegex(
            MODULE.WorkflowError,
            f"monitor handle does not exist for run {requested}",
        ):
            MODULE.command_watch(args)

    def test_watch_rejects_a_launch_record_from_another_run(self):
        self.write_monitor_run(launch_run_id="b" * 32)
        args = MODULE.build_parser().parse_args(
            ["watch", "--run-id", self.run_id]
        )
        with self.assertRaisesRegex(
            MODULE.WorkflowError,
            f"launch record identity is invalid for run {self.run_id}",
        ):
            MODULE.command_watch(args)

    def test_terminal_watch_has_no_next_command(self):
        payload = MODULE.bind_next_watch(
            {
                "event": MODULE.PROGRESS_UPDATE_EVENT,
                "cursor": 5,
                "updates": [],
                "finished": True,
            },
            kickoff=self.payload,
            run_id=self.run_id,
        )

        self.assertNotIn("next_watch", payload)


class CancelCommandTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.payload = kickoff()
        self.run_id = "a" * 32
        self.run_root = mock.patch.object(MODULE, "run_root", return_value=self.root)
        self.run_root.start()
        self.addCleanup(self.run_root.stop)

    def args(self):
        return SimpleNamespace(
            kickoff=json.dumps(self.payload),
            kickoff_file=None,
            run_id=self.run_id,
            wait_seconds=0,
        )

    def write_launch(self, **overrides):
        launch = {
            "kind": MODULE.RUN_KIND,
            "run_id": self.run_id,
            "kickoff": self.payload,
            "pid": 123,
        }
        launch.update(overrides)
        COMMON.write_json_atomically(
            MODULE.launch_state_path(self.payload, self.run_id), launch
        )

    def invoke(self, *, alive=True):
        output = StringIO()
        with (
            mock.patch.object(COMMON, "process_is_alive", return_value=alive),
            redirect_stdout(output),
        ):
            MODULE.command_cancel(self.args())
        return json.loads(output.getvalue())

    def test_unknown_run_is_safe_and_deterministic(self):
        result = self.invoke()
        self.assertEqual("unknown_run", result["result"])

    def test_wrong_run_identity_is_rejected(self):
        self.write_launch(run_id="b" * 32)
        self.assertEqual("run_identity_mismatch", self.invoke()["result"])

    def test_stale_run_records_a_safe_terminal_cancellation_result(self):
        self.write_launch()
        self.assertEqual("stale_run", self.invoke(alive=False)["result"])
        request = COMMON.read_json(
            MODULE.cancellation_request_path(self.payload, self.run_id)
        )
        self.assertEqual("stale", request["status"])

    def test_repeated_cancellation_is_idempotent(self):
        self.write_launch()
        self.assertEqual("requested", self.invoke()["result"])
        self.assertEqual("already_requested", self.invoke()["result"])

    def test_finished_run_is_not_changed(self):
        self.write_launch()
        pipeline_result = {"result": "complete", "passes": 1}
        COMMON.write_json_atomically(
            MODULE.run_result_path(self.payload, self.run_id),
            {
                "kind": MODULE.RUN_KIND,
                "run_id": self.run_id,
                "kickoff": self.payload,
                "pipeline_result": pipeline_result,
            },
        )
        result = self.invoke()
        self.assertEqual("already_finished", result["result"])
        self.assertEqual(pipeline_result, result["pipeline_result"])

    def test_malformed_existing_cancellation_record_is_not_overwritten(self):
        self.write_launch()
        path = MODULE.cancellation_request_path(self.payload, self.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]\n", encoding="utf-8")
        self.assertEqual("cancellation_record_malformed", self.invoke()["result"])
        self.assertEqual([], COMMON.read_json(path))


class ParserTest(unittest.TestCase):
    def test_the_progress_protocol_adds_start_and_watch_commands(self):
        parser = MODULE.build_parser()
        action = next(
            action
            for action in parser._actions
            if isinstance(action, __import__("argparse")._SubParsersAction)
        )
        self.assertEqual({"run", "start", "watch", "cancel"}, set(action.choices))

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
                "--run-id",
                "a" * 32,
                "--cursor",
                "4",
                "--wait-seconds",
                "300",
            ]
        )
        self.assertFalse(hasattr(args, "kickoff"))
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

    def test_terminal_result_bounds_the_observed_large_worker_payload(self):
        build_output = "BUILD OUTPUT\n" * 1800
        stages = [
            {
                "stage": stage,
                "clear": False,
                "identity": "stale",
                "outcome": "cleared",
                "reason": "clearance_is_for_an_older_head",
                "clear_at_head_sha": "1" * 40,
                "status": {"build_output": build_output},
            }
            for stage in MODULE.STAGE_NAMES
        ]
        payload = {
            "result": "partial",
            "reason": "two_passes_finished",
            "run_id": "run-1",
            "repository": "owner/repo",
            "stack_number": 77,
            "start_pull_request": 11,
            "selected": [11, 12],
            "passes": 2,
            "state_path": str(Path("state.json")),
            "phases": [
                {
                    "phase": MODULE.STAGE_DESCRIPTION,
                    "mode": MODULE.PHASE_PARALLEL,
                    "dispatches": 2,
                    "accepted": [11, 12],
                    "clear": False,
                    "reasons": ["not_cleared"],
                }
            ]
            * 10,
            "snapshot": {
                "result": "incomplete",
                "reason": "stages_not_clear",
                "pull_requests": [
                    {
                        "number": number,
                        "head_sha": str(number) * 40,
                        "base_sha": "a" * 40,
                        "uncleared": list(MODULE.STAGE_NAMES),
                        "stages": stages,
                    }
                    for number in (11, 12)
                ],
            },
            "propagations": [
                {
                    "number": 11,
                    "head_sha": "3" * 40,
                    "result": "published",
                    "output": build_output,
                }
            ],
        }

        first = MODULE.compact_terminal_result(
            payload, result_path=Path("result.json")
        )
        second = MODULE.compact_terminal_result(
            payload, result_path=Path("result.json")
        )
        encoded = json.dumps(first, sort_keys=True, separators=(",", ":")).encode()

        self.assertEqual(first, second)
        self.assertLessEqual(len(encoded), MODULE.TERMINAL_RESULT_MAX_BYTES)
        self.assertNotIn("BUILD OUTPUT", encoded.decode())
        self.assertEqual(
            "clearance_is_for_an_older_head",
            first["snapshot"]["pull_requests"][0]["stages"][0]["reason"],
        )
        self.assertEqual("result.json", first["artifacts"]["result"])
        self.assertEqual(
            {
                "number": 11,
                "head_sha": "3" * 40,
                "result": "published",
            },
            first["propagations"][0],
        )

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
