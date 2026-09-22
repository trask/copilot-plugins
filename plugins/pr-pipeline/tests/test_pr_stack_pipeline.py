from contextlib import redirect_stdout
import hashlib
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


class TargetSelectionTest(unittest.TestCase):
    def test_session_title_is_exact(self):
        self.assertEqual(
            "PR Stack Pipeline: #11 - Add a thing",
            MODULE.session_title(kickoff(), "Add a thing"),
        )

    def test_starting_in_the_middle_selects_only_that_suffix(self):
        live = stack(members=(9, 10, 11, 12))
        selected = MODULE.selection_from_stack(
            COMMON.target_for("owner/repo", 11), live
        )

        self.assertEqual("owner/repo", selected["repository"])
        self.assertEqual(77, selected["stackNumber"])
        self.assertEqual(11, selected["startPullRequest"])
        self.assertEqual([11, 12], selected["pullRequests"])
        self.assertEqual(
            MODULE.topology_fingerprint(live), selected["topologyFingerprint"]
        )
        self.assertEqual(
            MODULE.stack_source_identity(live)[1], selected["sourceSnapshot"]
        )
        self.assertEqual(
            [9, 10, 11, 12],
            [member["number"] for member in selected["sourceStack"]["members"]],
        )

    def test_starting_at_the_root_selects_the_whole_stack(self):
        selected = MODULE.selection_from_stack(
            COMMON.target_for("owner/repo", 11), stack()
        )
        self.assertEqual([11, 12, 13], selected["pullRequests"])

    def test_starting_at_the_top_selects_only_the_top(self):
        selected = MODULE.selection_from_stack(
            COMMON.target_for("owner/repo", 13), stack()
        )
        self.assertEqual([13], selected["pullRequests"])

    def test_selection_skips_inactive_descendants_but_freezes_full_topology(self):
        live = stack(members=(9, 10, 11, 12))
        live["members"][1]["state"] = "MERGED"
        selected = MODULE.selection_from_stack(
            COMMON.target_for("owner/repo", 9), live
        )

        self.assertEqual([9, 11, 12], selected["pullRequests"])
        self.assertEqual(
            [9, 10, 11, 12],
            [member["number"] for member in selected["sourceStack"]["members"]],
        )

    def test_rejects_an_inactive_starting_pull_request(self):
        live = stack()
        live["members"][1]["state"] = "MERGED"
        with self.assertRaisesRegex(MODULE.WorkflowError, "not an open"):
            MODULE.selection_from_stack(
                COMMON.target_for("owner/repo", 12), live
            )

    def test_rejects_a_pull_request_without_a_native_stack(self):
        with self.assertRaisesRegex(
            MODULE.WorkflowError, "not a member of a native stack"
        ):
            MODULE.selection_from_stack(
                COMMON.target_for("owner/repo", 11), None
            )

    def test_rejects_a_starting_pull_request_missing_from_the_stack(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "missing from"):
            MODULE.selection_from_stack(
                COMMON.target_for("owner/repo", 99), stack()
            )


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
                "--github-mutation-policy",
                "allow",
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

        self.assertEqual(
            "source-only", command[command.index("--github-mutation-policy") + 1]
        )
        self.assertNotIn("-p", command)

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

    def test_an_inactive_descendant_stays_out_of_the_frozen_selection(self):
        live = stack(members=(9, 10, 11, 12))
        live["members"][2]["state"] = "MERGED"
        selected = MODULE.selection_from_stack(
            COMMON.target_for("owner/repo", 10), live
        )

        result = MODULE.validate_selection(selected, live)

        self.assertEqual("ready", result["result"])
        self.assertEqual(
            [10, 12],
            [member["number"] for member in result["selected"]],
        )

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

    def test_frozen_selection_rejects_topology_drift(self):
        live = stack()
        selected = MODULE.selection_from_stack(
            COMMON.target_for("owner/repo", 11), live
        )
        moved = stack()
        moved["members"][1]["base_branch"] = "other"

        result = MODULE.validate_selection(selected, moved)

        self.assertEqual("stopped", result["result"])
        self.assertEqual("topology_changed", result["reason"])

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
                                            "baseRefOid": "a" * 40,
                                            "baseRef": {"target": {"oid": BASE}},
                                            "mergeable": "MERGEABLE",
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
        self.assertEqual(BASE, live["members"][0]["base_sha"])
        self.assertEqual("MERGEABLE", live["members"][0]["mergeable"])
        self.assertIn("baseRef { target { oid } }", MODULE.STACK_QUERY)

        payload["data"]["repository"]["pullRequest"]["stack"]["entries"]["nodes"][
            0
        ]["position"] = 1
        with self.assertRaisesRegex(MODULE.WorkflowError, "positions are malformed"):
            MODULE.read_native_stack(
                "owner/repo", 11, api=lambda arguments: payload
            )


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

    def propagate(self, repository, number, head_sha, stack_number, **authorization):
        request = COMMON.read_json(Path(authorization["request_path"]))
        self.assertEqual(self.pipeline_kickoff["pullRequests"], request["selected"])
        self.assertEqual(number, request["fixed_pr"])
        self.assertEqual(head_sha, request["fixed_head"])
        self.assertEqual("run-1", request["owner"]["run_id"])
        self.assertEqual(str(Path(authorization["state_path"]).resolve()), request["state"])
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
        self.pipeline_kickoff = payload or kickoff()
        options = {
            "models": COMMON.stage_models(None),
            "effort": "high",
            "run_id": "run-1",
            "report": self.events.append,
            "launcher": self.launcher,
            "state_path": self.root / "state.json",
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
        self.stack = stack(members=(11, 12))
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
        self.assertIn("--whole-stack", request["arguments"])

    def test_partial_selection_never_authorizes_publication_of_unselected_prefix(self):
        self.stack = stack(members=(9, 10, 11, 12))
        self.stack["members"][0]["is_draft"] = False
        pipeline = self.pipeline(kickoff([11, 12]))
        selected = MODULE.validate_selection(pipeline.kickoff, self.stack)["selected"]
        for member in selected:
            member.update(mergeable="MERGEABLE", base_sha=BASE)

        result = pipeline.run_conflict_phase(1, selected)

        self.assertEqual(0, result["dispatches"])
        self.assertTrue(result["clear"])
        self.assertEqual([], self.launcher.started)

    def test_unselected_predecessors_are_never_dispatched(self):
        self.stack = stack(members=(9, 10, 11, 12))
        selected_scope = MODULE.selection_from_stack(
            COMMON.target_for("owner/repo", 11), self.stack
        )
        pipeline = self.pipeline(selected_scope)
        selected = MODULE.validate_selection(
            selected_scope, self.stack
        )["selected"]

        pipeline.run_parallel_phase(
            MODULE.STAGE_COPILOT_REVIEW, 1, selected
        )

        self.assertEqual(
            [11, 12], [request["number"] for request in self.launcher.started]
        )

    def test_partial_selection_blocks_conflicting_unknown_and_stale_metadata(self):
        for mergeable, base_sha in (
            ("CONFLICTING", BASE), ("UNKNOWN", BASE), ("MERGEABLE", "a" * 40),
        ):
            with self.subTest(mergeable=mergeable, base_sha=base_sha):
                self.stack = stack(members=(10, 11))
                member = self.stack["members"][-1]
                member.update(mergeable=mergeable, base_sha=base_sha)
                pipeline = self.pipeline(kickoff([11]))

                result = pipeline.run_conflict_phase(1, [member])

                self.assertEqual(
                    "unsupported_partial_selection_conflict",
                    result["stopped"]["reason"],
                )
                self.assertEqual(0, result["dispatches"])
                self.assertEqual([], self.launcher.started)

    def test_partial_selection_final_snapshot_rechecks_mergeability(self):
        self.stack = stack(members=(10, 11))
        member = self.stack["members"][-1]
        member.update(mergeable="MERGEABLE", base_sha=BASE)
        self.clear_everything()
        pipeline = self.pipeline(kickoff([11]))
        self.assertEqual("complete", pipeline.final_snapshot()["result"])

        member["mergeable"] = "CONFLICTING"

        result = pipeline.final_snapshot()
        self.assertEqual("incomplete", result["result"])
        self.assertIn(MODULE.STAGE_CONFLICT, result["pull_requests"][0]["uncleared"])

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

    def test_nonzero_worker_exit_blocks_even_a_current_clearance_marker(self):
        self.stack = stack(members=(11,))
        self.inspect_sequences[(11, MODULE.STAGE_COPILOT_REVIEW)] = [
            {},
            {"clear": True, "outcome": "cleared"},
        ]
        pipeline = self.pipeline(kickoff([11]))
        with mock.patch.object(self.launcher, "wait", return_value={"returncode": 1}):
            result = pipeline.run_parallel_phase(
                MODULE.STAGE_COPILOT_REVIEW, 1, self.stack["members"]
            )
        self.assertEqual("stage_execution_failed", result["stopped"]["reason"])
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

    def test_conflict_head_movement_without_terminal_outcome_blocks_review(self):
        self.stack = stack(members=(11, 12))
        status_path = str(self.root / "conflict-state.json")
        self.inspect_sequences[(11, MODULE.STAGE_CONFLICT)] = [
            {},
            {
                "outcome": None,
                "reason": "not_cleared",
                "status_state": status_path,
                "status": {"agent_task": {"status": "completed"}},
            },
        ]

        def publish_heads(_request):
            self.stack["members"][0]["head_sha"] = "a" * 40
            self.stack["members"][1]["head_sha"] = "c" * 40

        self.launcher.on_start = publish_heads
        pipeline = self.pipeline(kickoff([11, 12]))

        result = pipeline.execute()

        self.assertEqual("blocked", result["result"])
        self.assertEqual("conflict_did_not_record_outcome", result["reason"])
        self.assertEqual(
            [MODULE.STAGE_CONFLICT],
            [request["stage"] for request in self.launcher.started],
        )
        recorded = COMMON.read_json(pipeline.result_path)["pipeline_result"]
        stage = recorded["pull_requests"]["11"]["stages"][MODULE.STAGE_CONFLICT]
        self.assertEqual(head_of(11), stage["dispatched_head_sha"])
        self.assertEqual("a" * 40, stage["current_head_sha"])
        self.assertFalse(stage["clear"])
        self.assertEqual(status_path, stage["stage_result"]["status_state"])
        self.assertEqual(
            {"status": "completed"}, stage["stage_result"]["agent_task"]
        )

    def test_completed_uncleared_conflict_can_continue_without_claiming_clearance(self):
        self.stack = stack(members=(11,))
        self.clear_everything()
        self.clear.remove((11, MODULE.STAGE_CONFLICT))
        self.completed.add((11, MODULE.STAGE_CONFLICT))
        def inspect(entry, *args):
            result = self.inspect(entry, *args)
            if entry["stage"] == MODULE.STAGE_DESCRIPTION:
                result["status"] = {
                    "run_id": "description-1",
                    "pipeline_run": "run-1",
                    "pipeline_iteration": 1,
                    "pipeline_max_iterations": 2,
                    "clearance_verification": {
                        "result": "current",
                        "reason": "description_snapshot_current",
                        "expected_snapshot_sha256": "a" * 64,
                        "observed_snapshot_sha256": "a" * 64,
                    },
                    "agent_task": {
                        "status": "completed",
                        "task": {"id": "task-1", "state": "completed"},
                        "model": "gpt-5.6-sol",
                        "github_mutation_policy": "allow",
                    },
                }
            return result

        pipeline = self.pipeline(kickoff([11]), inspect=inspect)

        result = pipeline.execute()

        self.assertEqual("partial", result["result"])
        self.assertEqual(2, result["passes"])
        self.assertTrue(any(
            request["stage"] == MODULE.STAGE_COPILOT_REVIEW
            for request in self.launcher.started
        ))
        self.assertFalse(result["phases"][0]["clear"])
        self.assertEqual(["completed"], result["phases"][0]["reasons"])
        self.assertIn(
            MODULE.STAGE_CONFLICT, result["snapshot"]["pull_requests"][0]["uncleared"]
        )

    def test_conflict_clearance_requires_terminal_outcome_and_exact_head_and_base(self):
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_CONFLICT]
        target = COMMON.target_for("owner/repo", 11)
        current = {
            "stage_outcome": "cleared",
            "mergeable_at_head_sha": head_of(11),
            "attempt": {"base_sha": BASE},
        }
        cases = [
            (current, True, None),
            ({**current, "stage_outcome": None}, False, "not_cleared"),
            (
                {**current, "mergeable_at_head_sha": "a" * 40},
                False,
                "clearance_is_for_an_older_head",
            ),
            (
                {**current, "attempt": {"base_sha": "c" * 40}},
                False,
                "clearance_is_for_an_older_base",
            ),
        ]
        for payload, clear, reason in cases:
            with self.subTest(reason=reason), mock.patch.object(
                MODULE,
                "read_stage_status",
                return_value={
                    "ok": True,
                    "installed": True,
                    "state": str(self.root / "conflict-state.json"),
                    "payload": payload,
                },
            ):
                result = MODULE.inspect_stage(entry, target, head_of(11), BASE)
                self.assertEqual(clear, result["clear"])
                self.assertEqual(reason, result["reason"])

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

        def align(repository, number, head_sha, stack_number, **authorization):
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

        def align(repository, number, head_sha, stack_number, **authorization):
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
        pipeline.propagate = lambda *args, **kwargs: {"result": "conflicted"}

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

    def test_all_already_clear_ci_members_report_verified_clearance(self):
        for member in self.stack["members"]:
            self.clear.add((member["number"], MODULE.STAGE_CI))
        pipeline = self.pipeline()

        result = pipeline.run_ci_phase(1, self.stack["members"])

        self.assertTrue(result["clear"])

        self.assertEqual(0, result["dispatches"])
        event = self.events_named("phase_finished")[0]
        self.assertTrue(event["clear"])

    def test_gate_uses_verified_current_head_without_calling_no_checks_green(self):
        pipeline = self.pipeline()
        predecessor, member = self.stack["members"][:2]
        with (
            mock.patch.object(pipeline, "clearance", return_value={"clear": True, "outcome": "skipped"}),
            mock.patch.object(pipeline, "contains", return_value=True) as contains,
        ):
            result = pipeline.ci_gate(member, predecessor, {"clear": True, "head_sha": "stale-head"})
        self.assertTrue(result["ready"])
        self.assertEqual("predecessor_has_no_checks", result["reason"])
        self.assertEqual(predecessor["head_sha"], contains.call_args.args[1])

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
        pipeline = self.pipeline(conflict_strategy="merge")
        member = self.stack["members"][0]
        request = pipeline.request_for(member, MODULE.STAGE_CONFLICT, 2)
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

    def test_stack_conflict_scope_is_an_explicit_coordinator_argument(self):
        pipeline = self.pipeline()
        member = self.stack["members"][0]
        request = pipeline.request_for(
            member, MODULE.STAGE_CONFLICT, 1, scope="Resolve the complete native stack"
        )
        self.assertIn("--whole-stack", request["arguments"])

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
        pipeline.propagate = lambda *args, **kwargs: next(outcomes)
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

    def test_new_same_run_heads_keep_the_original_topology_authorization(self):
        pipeline = self.pipeline()
        first_path, first_state = pipeline.authorize_stack_publication(
            11, head_of(11), operation="descendant-propagation"
        )
        first_bytes = first_path.read_bytes()
        self.stack["members"][0]["head_sha"] = "a" * 40
        self.stack["members"][1]["head_sha"] = "c" * 40
        second_path, second_state = pipeline.authorize_stack_publication(
            11, "a" * 40, operation="descendant-propagation"
        )
        first, second = COMMON.read_json(first_path), COMMON.read_json(second_path)
        self.assertEqual(first["topology_fingerprint"], second["topology_fingerprint"])
        self.assertEqual(first["owner"], second["owner"])
        self.assertEqual(first["selected"], second["selected"])
        self.assertNotEqual(first["source_snapshot"], second["source_snapshot"])
        self.assertNotEqual(first_state, second_state)
        self.assertEqual(first_bytes, first_path.read_bytes())
        resolver_spec = importlib.util.spec_from_file_location(
            "pipeline_stack_request_resolver",
            SCRIPT.parents[2] / "pr-conflict-resolver" / "scripts" / "pr_conflict_resolver.py",
        )
        resolver = importlib.util.module_from_spec(resolver_spec)
        resolver_spec.loader.exec_module(resolver)
        self.assertEqual(
            second,
            resolver.load_stack_request(
                str(second_path), operation="descendant-propagation",
                run_id=pipeline.run_id,
            ),
        )

    def test_changed_descendants_cannot_replace_an_existing_propagation_request(self):
        pipeline = self.pipeline()
        path, _ = pipeline.authorize_stack_publication(
            11, head_of(11), operation="descendant-propagation"
        )
        original = path.read_bytes()
        self.stack["members"][1]["head_sha"] = "c" * 40
        with self.assertRaisesRegex(MODULE.WorkflowError, "request changed"):
            pipeline.authorize_stack_publication(
                11, head_of(11), operation="descendant-propagation"
            )
        self.assertEqual(original, path.read_bytes())

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

        def align(repository, number, head_sha, stack_number, **authorization):
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
        pipeline.propagate = lambda *args, **kwargs: {"result": "published"}

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

    def test_source_drift_consumes_one_pass_and_later_pass_can_clear(self):
        self.stack = stack(members=(11,))
        self.clear_everything()
        self.clear.remove((11, MODULE.STAGE_DESCRIPTION))
        original_head = self.stack["members"][0]["head_sha"]
        current_head = "f" * 40
        source_drift = {
            "expected_head_sha": original_head,
            "observed_head_sha": current_head,
            "pipeline_iteration": 1,
            "pipeline_max_iterations": 2,
            "consumed_allowance": 1,
            "remaining_allowance": 1,
            "mutation_performed": False,
            "recommendation_adopted": False,
            "publication_performed": False,
        }

        def inspect(entry, target, head_sha, base_sha=None):
            result = self.inspect(entry, target, head_sha, base_sha)
            if (
                entry["stage"] == MODULE.STAGE_DESCRIPTION
                and (target["number"], entry["stage"]) not in self.clear
                and head_sha == current_head
            ):
                result.update(
                    {
                        "outcome": None,
                        "reason": "source_drift",
                        "source_drift": source_drift,
                        "status": {
                            "agent_task": {
                                "status": "head_changed",
                                "source_drift": source_drift,
                            }
                        },
                    }
                )
            return result

        def complete(request):
            if request["stage"] != MODULE.STAGE_DESCRIPTION:
                return
            if request["pass"] == 1:
                self.stack = stack(members=(11,), heads={11: current_head})
            else:
                self.clear.add((11, MODULE.STAGE_DESCRIPTION))

        self.launcher.on_start = complete
        pipeline = self.pipeline(kickoff(numbers=(11,)), inspect=inspect)

        result = pipeline.execute()

        self.assertEqual("complete", result["result"])
        self.assertEqual(2, result["passes"])
        description_requests = [
            request
            for request in self.launcher.started
            if request["stage"] == MODULE.STAGE_DESCRIPTION
        ]
        self.assertEqual([1, 2], [request["pass"] for request in description_requests])
        self.assertEqual(
            ["1", "2"],
            [
                request["arguments"][
                    request["arguments"].index("--pipeline-iteration") + 1
                ]
                for request in description_requests
            ],
        )
        self.assertTrue(
            all(
                request["arguments"][
                    request["arguments"].index("--pipeline-max-iterations") + 1
                ] == "2"
                for request in description_requests
            )
        )
        recorded = result["pull_requests"]["11"]["stages"][MODULE.STAGE_DESCRIPTION]
        self.assertTrue(recorded["clear"])
        self.assertEqual(
            source_drift,
            recorded["superseded_candidates"][0]["source_drift"],
        )
        drift_phase = next(
            phase
            for phase in result["phases"]
            if phase["phase"] == MODULE.STAGE_DESCRIPTION
            and phase.get("source_drifts")
        )
        self.assertEqual(1, drift_phase["source_drifts"][0]["consumed_allowance"])
        compact = MODULE.compact_terminal_result(
            result, result_path=pipeline.result_path
        )
        compact_drift = next(
            phase["source_drifts"][0]
            for phase in compact["phases"]
            if phase.get("source_drifts")
        )
        self.assertEqual(source_drift["expected_head_sha"], compact_drift["expected_head_sha"])
        self.assertEqual(source_drift["observed_head_sha"], compact_drift["observed_head_sha"])

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

        def drift(**kwargs):
            self.stack = stack(members=(11, 12))
            return original(**kwargs)

        with mock.patch.object(pipeline, "revalidate", side_effect=drift):
            result = pipeline.execute()

        self.assertEqual("stopped", result["result"])
        self.assertIn(
            result["reason"],
            {"topology_changed", "selection_is_not_the_stack_suffix"},
        )

    def test_source_drift_after_target_discovery_stops_before_mutation(self):
        selected = MODULE.selection_from_stack(
            COMMON.target_for("owner/repo", 11), self.stack
        )
        self.stack = stack(heads={11: "f" * 40})
        pipeline = self.pipeline(selected)

        result = pipeline.execute()

        self.assertEqual("stopped", result["result"])
        self.assertEqual("source_snapshot_changed_before_start", result["reason"])
        self.assertEqual([], self.launcher.calls)

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

    def test_runtime_error_cancels_workers(self):
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

    def test_finish_rejects_completion_when_worktree_removal_evidence_fails(self):
        failures = (
            {
                "result": "failed",
                "reason": "ownership_record_update_failed",
                "detail": "cannot retain tombstone",
                "number": 11,
                "ownership_record": "11.worktree.json",
                "worktree_removed": True,
            },
            {
                "result": "failed",
                "reason": "worktree_remove_failed",
                "detail": "remove failed",
                "number": 11,
                "ownership_record": "11.worktree.json",
                "worktree_removed": True,
            },
        )
        for failure in failures:
            with self.subTest(reason=failure["reason"]):
                pipeline = self.pipeline()
                with mock.patch.object(
                    pipeline, "cleanup", return_value=[failure]
                ):
                    result = pipeline.finish(
                        "complete",
                        snapshot={"result": "complete", "pull_requests": []},
                        passes=1,
                    )

                state = COMMON.read_json(pipeline.state_path)
                persisted = COMMON.read_json(
                    pipeline.result_path
                )["pipeline_result"]
                compact = MODULE.compact_terminal_result(
                    result, result_path=pipeline.result_path
                )
                for recorded in (result, state, persisted, compact):
                    self.assertEqual("error", recorded["result"])
                    self.assertEqual(
                        "worktree_cleanup_failed", recorded["reason"]
                    )
                    self.assertEqual(
                        [failure], recorded["cleanup_failures"]
                    )
                self.assertEqual(
                    "complete", result["snapshot"]["result"]
                )
                self.assertEqual(
                    "complete", compact["snapshot"]["result"]
                )
                transition = MODULE.progress_transition(
                    {"event": "stack_pipeline_finished", **compact}
                )
                self.assertEqual("error", transition["result"])
                self.assertEqual(
                    [failure],
                    transition["final_event"]["cleanup_failures"],
                )

    def test_finish_preserves_a_primary_blocker_when_cleanup_also_fails(self):
        failure = {
            "result": "failed",
            "reason": "ownership_record_update_failed",
            "detail": "cannot retain tombstone",
            "number": 11,
            "ownership_record": "11.worktree.json",
            "worktree_removed": True,
        }
        pipeline = self.pipeline()
        with mock.patch.object(
            pipeline, "cleanup", return_value=[failure]
        ):
            result = pipeline.finish(
                "blocked",
                reason="stage_execution_failed",
                detail="primary stage failure",
            )

        state = COMMON.read_json(pipeline.state_path)
        persisted = COMMON.read_json(pipeline.result_path)["pipeline_result"]
        compact = MODULE.compact_terminal_result(
            result, result_path=pipeline.result_path
        )
        for recorded in (result, state, persisted, compact):
            self.assertEqual("blocked", recorded["result"])
            self.assertEqual("stage_execution_failed", recorded["reason"])
            self.assertEqual([failure], recorded["cleanup_failures"])
        self.assertEqual("primary stage failure", result["detail"])
        self.assertEqual("primary stage failure", state["detail"])
        self.assertEqual("primary stage failure", persisted["detail"])
        self.assertEqual("primary stage failure", compact["detail"])

    def test_cleanup_failure_summary_stays_bounded(self):
        failures = [
            {
                "result": "failed",
                "reason": "ownership_record_update_failed",
                "detail": "cannot retain tombstone " * 100,
                "number": number,
                "worktree": f"C:\\worktrees\\{number}",
                "ownership_record": f"C:\\run\\worktrees\\{number}.worktree.json",
                "worktree_removed": True,
            }
            for number in range(100)
        ]
        compact = MODULE.compact_terminal_result(
            {
                "result": "error",
                "reason": "worktree_cleanup_failed",
                "detail": json.dumps(failures, sort_keys=True),
                "cleanup_failures": failures,
            }
        )

        self.assertLessEqual(
            len(
                json.dumps(
                    {"event": "stack_pipeline_finished", **compact},
                    sort_keys=True,
                ).encode("utf-8")
            )
            + len(os.linesep.encode()),
            MODULE.TERMINAL_RESULT_MAX_BYTES,
        )
        self.assertEqual("worktree_cleanup_failed", compact["reason"])
        self.assertEqual(
            100,
            len(compact["cleanup_failures"])
            + compact["cleanup_failures_omitted"],
        )

    def test_cleanup_failure_detail_truncation_keeps_full_result_evidence(self):
        detail = "cannot retain tombstone " * 100
        failure = {
            "result": "failed",
            "reason": "ownership_record_update_failed",
            "detail": detail,
            "number": 11,
            "ownership_record": "11.worktree.json",
            "worktree_removed": True,
        }
        pipeline = self.pipeline()
        with mock.patch.object(
            pipeline, "cleanup", return_value=[failure]
        ):
            result = pipeline.finish(
                "complete",
                snapshot={"result": "complete", "pull_requests": []},
                passes=1,
            )

        compact = MODULE.compact_terminal_result(
            result, result_path=pipeline.result_path
        )
        persisted = COMMON.read_json(pipeline.result_path)["pipeline_result"]
        self.assertTrue(compact["cleanup_failure_details_truncated"])
        self.assertNotIn("cleanup_failures_omitted", compact)
        self.assertEqual(
            MODULE.TERMINAL_TEXT_MAX_CHARS,
            len(compact["cleanup_failures"][0]["detail"]),
        )
        self.assertTrue(
            compact["cleanup_failures"][0]["detail"].endswith("...")
        )
        self.assertEqual(detail, persisted["cleanup_failures"][0]["detail"])
        self.assertEqual(failure, persisted["cleanup"][0])

    def test_failed_review_replay_retains_dirty_workspace_and_result_evidence(self):
        self.stack = stack(members=(11, 12))
        pipeline = self.pipeline(kickoff([11, 12]))
        with mock.patch.object(MODULE, "WINDOWS_WORKTREE_PATH_BUDGET", 4096):
            cleaner = MODULE.WorkerLauncher(
                repo_root=self.root / "repo",
                repository="owner/repo",
                run_id=pipeline.run_id,
                run_directory=pipeline.run_directory,
                worktree_root=self.root / "worktrees",
                models=COMMON.stage_models(None),
                effort="high",
            )
        paths = {
            number: MODULE.worktree_path(cleaner.worktree_root, number)
            for number in (11, 12)
        }
        records = {}
        for number, path in paths.items():
            path.mkdir(parents=True)
            records[number] = MODULE.worktree_ownership_path(
                cleaner.run_directory, number
            )
            COMMON.write_json_atomically(
                records[number], {"run_id": pipeline.run_id, "path": str(path)}
            )
        evidence = paths[11] / "unfinished.txt"
        evidence.write_bytes(b"synthetic unfinished change\n")
        original_record = records[11].read_bytes()
        task = {
            "status": "failed",
            "error": "local decision process timed out after 540 seconds",
            "source_before": {"head": head_of(11), "status": ""},
            "source_after": {
                "head": head_of(11), "status": " M unfinished.txt",
                "worktree": str(paths[11]),
            },
        }
        self.inspect_sequences[(11, MODULE.STAGE_COPILOT_REVIEW)] = [
            {},
            {"outcome": "escalated", "reason": "escalated", "status": {"agent_task": task}},
        ]
        self.clear.add((12, MODULE.STAGE_COPILOT_REVIEW))

        def replay_git(command, *, check):
            self.assertFalse(check)
            self.assertNotIn("--force", command)
            if command[3] == "status":
                output = " M unfinished.txt\n" if command[2] == str(paths[11]) else ""
            else:
                self.assertEqual(
                    ["git", "-C", str(cleaner.repo_root), "worktree", "remove", str(paths[12])],
                    command,
                )
                paths[12].rmdir()
                output = ""
            return subprocess.CompletedProcess(command, 0, output, "")

        with (
            mock.patch.object(
                self.launcher, "wait",
                side_effect=lambda worker: {"returncode": 1 if worker["number"] == 11 else 0},
            ),
            mock.patch.object(self.launcher, "cleanup", side_effect=cleaner.cleanup),
            mock.patch.object(COMMON, "run", side_effect=replay_git),
        ):
            phase = pipeline.run_parallel_phase(
                MODULE.STAGE_COPILOT_REVIEW, 1, self.stack["members"]
            )
            result = pipeline.finish(
                "blocked", reason=phase["stopped"]["reason"], phases=[phase]
            )

        self.assertEqual("stage_execution_failed", result["reason"])
        self.assertEqual("preserved", result["cleanup"][0]["result"])
        self.assertEqual("worktree_is_dirty", result["cleanup"][0]["reason"])
        self.assertEqual(str(paths[11]), result["cleanup"][0]["worktree"])
        self.assertEqual(str(records[11]), result["cleanup"][0]["ownership_record"])
        self.assertEqual(
            {
                "number": 12,
                "result": "removed",
                "ownership_record": str(records[12]),
            },
            result["cleanup"][1],
        )
        self.assertEqual(b"synthetic unfinished change\n", evidence.read_bytes())
        self.assertEqual(original_record, records[11].read_bytes())
        removed = COMMON.read_json(records[12])
        self.assertEqual(pipeline.run_id, removed["run_id"])
        self.assertEqual(str(paths[12]), removed["path"])
        self.assertEqual("removed", removed["status"])
        self.assertIsInstance(removed["removed_at"], str)
        saved = COMMON.read_json(pipeline.result_path)["pipeline_result"]
        self.assertEqual(result["cleanup"], saved["cleanup"])
        stage = saved["pull_requests"]["11"]["stages"][MODULE.STAGE_COPILOT_REVIEW]
        self.assertEqual(task, stage["stage_result"]["agent_task"])
        self.assertTrue(
            saved["pull_requests"]["12"]["stages"][MODULE.STAGE_COPILOT_REVIEW]["clear"]
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

    def test_stale_admission_lock_does_not_block_a_fresh_run(self):
        self.clear_everything()
        pipeline = self.pipeline()
        stale_lock = self.root / "state.lock"
        COMMON.write_json_atomically(
            stale_lock,
            {"run_id": "old-run", "pid": 123, "created_at": "2026-01-01T00:00:00Z"},
        )

        result = pipeline.execute()

        self.assertEqual("complete", result["result"])
        self.assertTrue(stale_lock.exists())

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


class CiWarningTest(StackFixture):
    def setUp(self):
        super().setUp()
        self.warnings = {}
        self.addCleanup(
            setattr, COMMON, "ACTIVE_GITHUB_MUTATION_POLICY",
            COMMON.ACTIVE_GITHUB_MUTATION_POLICY,
        )

    def record_warning(self, number, head=None, base=BASE):
        self.warnings[number] = {
            "stage_outcome": "warning",
            "clean_at_head_sha": None,
            "warning_at_head_sha": head or head_of(number),
            "warning_at_base_sha": base,
            "warning_verification": {
                "result": "current",
                "expected_snapshot_sha256": "e" * 64,
                "observed_snapshot_sha256": "e" * 64,
                "reason": "ci_warning_snapshot_current",
            },
            "ci_warnings": [{
                "check_key": f"check:{number}",
                "name": "Integration tests",
                "diagnosis": "unrelated",
                "reason": "A service outage affects the same test on the base.",
                "evidence": ["The base and PR job logs report the same service outage."],
            }],
        }

    def inspect(self, entry, target, head_sha, base_sha=None):
        if entry["stage"] != MODULE.STAGE_CI or target["number"] not in self.warnings:
            return super().inspect(entry, target, head_sha, base_sha)
        return COMMON.inspect_stage(
            entry, target, head_sha, base_sha,
            pipeline_run="run-1",
            read_status=lambda *_: {
                "ok": True, "installed": True, "state": "state.json",
                "payload": self.warnings[target["number"]],
            },
        )

    def complete_worker(self, request):
        self.clear.add((request["number"], request["stage"]))
        if request["stage"] == MODULE.STAGE_CI:
            self.record_warning(request["number"], request["head_sha"], request["base_sha"])

    def invalidate_warning(self, number):
        self.warnings[number] = {
            "stage_outcome": "pending",
            "outcome": None,
            "clean_at_head_sha": None,
            "warning_at_head_sha": None,
            "warning_at_base_sha": None,
            "ci_warnings": [],
            "all_ci_passed": False,
            "warning_verification": {
                "result": "stale",
                "expected_snapshot_sha256": "e" * 64,
                "observed_snapshot_sha256": "f" * 64,
                "reason": "ci_warning_snapshot_changed",
            },
        }

    def test_new_failure_at_same_revisions_invalidates_stack_snapshot(self):
        self.clear_everything()
        self.record_warning(11)
        pipeline = self.pipeline()
        self.assertEqual("complete", pipeline.final_snapshot()["result"])
        self.invalidate_warning(11)
        result = pipeline.final_snapshot()
        self.assertEqual("incomplete", result["result"])
        self.assertNotIn("ci_warnings", result)
        ci = next(
            stage for stage in result["pull_requests"][0]["stages"]
            if stage["stage"] == MODULE.STAGE_CI
        )
        self.assertFalse(ci["clear"])
        self.assertEqual("ci_warning_snapshot_changed", ci["reason"])
        self.assertEqual("stale", ci["identity"])

    def test_new_failure_at_same_revisions_is_not_reused_in_later_ci_phase(self):
        self.clear_everything()
        self.record_warning(11)
        pipeline = self.pipeline()
        pipeline.run_ci_phase(1, self.stack["members"])
        self.invalidate_warning(11)
        self.launcher.on_start = self.complete_worker
        result = pipeline.run_ci_phase(2, self.stack["members"])
        self.assertTrue(result["clear"])
        self.assertEqual([11], [request["number"] for request in self.launcher.started])
        request = self.launcher.started[0]
        self.assertEqual("run-1", request["arguments"][request["arguments"].index("--pipeline-run") + 1])
        self.assertEqual("2", request["arguments"][request["arguments"].index("--pipeline-iteration") + 1])

    def test_predecessor_snapshot_is_revalidated_before_releasing_successor(self):
        self.record_warning(11)

        def inspect(entry, target, head, base):
            result = self.inspect(entry, target, head, base)
            if entry["stage"] == MODULE.STAGE_CI and target["number"] == 11:
                self.invalidate_warning(11)
            return result

        result = self.pipeline(inspect=inspect).run_ci_phase(1, self.stack["members"])
        self.assertFalse(result["clear"])
        self.assertEqual(12, result["blocked"]["number"])
        self.assertEqual("ci_warning_snapshot_changed", result["blocked"]["reason"])
        self.assertEqual([], self.launcher.started)
        self.assertNotIn("ci_warnings", result)
        self.assertEqual("stale", result["gates"][-1]["stage_result"]["warning_verification"]["result"])

    def test_predecessor_snapshot_is_revalidated_after_descendant_alignment(self):
        self.record_warning(11)
        self.contains_pairs = set()

        def propagate(*args, **kwargs):
            result = self.propagate(*args, **kwargs)
            self.contains_pairs = None
            self.invalidate_warning(11)
            return result

        result = self.pipeline(propagate=propagate).run_ci_phase(1, self.stack["members"])
        self.assertFalse(result["clear"])
        self.assertEqual("ci_warning_snapshot_changed", result["blocked"]["reason"])
        self.assertEqual([(11, head_of(11))], self.propagated)
        self.assertEqual([], self.launcher.started)
        self.assertNotIn("ci_warnings", result)

    def test_blocked_stack_result_drops_changed_warning_snapshot(self):
        self.record_warning(11)
        self.clear_everything()
        pipeline = self.pipeline()
        pipeline.run_ci_phase(1, self.stack["members"])
        self.invalidate_warning(11)
        result = pipeline.finish("blocked", reason="stage_execution_failed")
        self.assertNotIn("ci_warnings", result)
        self.assertNotIn("all_ci_passed", result)

    def test_stack_completes_with_warnings_and_runs_description_for_every_member(self):
        self.launcher.on_start = self.complete_worker
        self.clear_everything()
        for member in self.stack["members"]:
            self.clear.remove((member["number"], MODULE.STAGE_CI))
        pipeline = self.pipeline()
        result = pipeline.execute()
        self.assertEqual("complete", result["result"])
        self.assertEqual(1, result["passes"])
        self.assertFalse(result["all_ci_passed"])
        self.assertEqual([11, 12, 13], [item["number"] for item in result["ci_warnings"]])
        for warning in result["ci_warnings"]:
            self.assertEqual(head_of(warning["number"]), warning["head_sha"])
            self.assertEqual(BASE, warning["base_sha"])
            self.assertTrue(warning["reason"])
            self.assertTrue(warning["evidence"])
        self.assertEqual(
            [11, 12, 13],
            [request["number"] for request in self.launcher.started if request["stage"] == MODULE.STAGE_DESCRIPTION],
        )
        compact = MODULE.compact_terminal_result(result, result_path=self.root / "result.json")
        self.assertEqual(result["ci_warnings"], compact["ci_warnings"])
        self.assertFalse(compact["all_ci_passed"])
        for event in [
            *[
                event for event in self.events
                if event["event"] in ("worker_finished", "phase_finished")
                and (event.get("stage") or event.get("phase")) == MODULE.STAGE_CI
            ],
            *self.events_named("snapshot_taken"),
            {"event": "stack_pipeline_finished", **compact},
        ]:
            transition = MODULE.progress_transition(event)
            self.assertIn("WITH CI WARNINGS", transition["message"])
            self.assertFalse(transition["all_ci_passed"])
        persisted = json.loads((self.root / "run" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["ci_warnings"], persisted["pipeline_result"]["ci_warnings"])

    def test_warning_predecessor_allows_bottom_up_ci_without_calling_it_green(self):
        self.launcher.on_start = self.complete_worker
        pipeline = self.pipeline()
        first = pipeline.run_ci_phase(1, self.stack["members"])
        second = pipeline.run_ci_phase(2, self.stack["members"])
        self.assertEqual(
            ["lowest_selected", "predecessor_has_ci_warning", "predecessor_has_ci_warning"],
            [gate["reason"] for gate in first["gates"]],
        )
        self.assertTrue(first["clear"])
        self.assertTrue(second["clear"])
        self.assertEqual(0, second["dispatches"])
        self.assertEqual(first["ci_warnings"], second["ci_warnings"])
        self.assertFalse(second["all_ci_passed"])
        self.assertEqual(3, len(self.launcher.started))
        for member in self.stack["members"]:
            state = pipeline.state["pull_requests"][str(member["number"])]["stages"][MODULE.STAGE_CI]
            self.assertEqual("already_clear", state["action"])
            self.assertEqual("ci_warning", state["clearance_kind"])

    def test_later_stage_failure_keeps_current_stack_warnings_visible(self):
        self.clear_everything()
        self.record_warning(11)
        original_wait = self.launcher.wait

        def wait(worker):
            result = original_wait(worker)
            if worker["stage"] == MODULE.STAGE_DESCRIPTION:
                result["returncode"] = 1
            return result

        self.launcher.wait = wait
        result = self.pipeline().execute()
        self.assertEqual("blocked", result["result"])
        self.assertFalse(result["all_ci_passed"])
        self.assertEqual([11], [warning["number"] for warning in result["ci_warnings"]])
        compact = MODULE.compact_terminal_result(result)
        self.assertFalse(compact["all_ci_passed"])
        self.assertEqual(result["ci_warnings"], compact["ci_warnings"])

    def test_stale_warnings_are_not_carried_to_a_later_blocked_result(self):
        self.record_warning(11)
        self.clear_everything()
        pipeline = self.pipeline()
        pipeline.run_ci_phase(1, self.stack["members"])
        self.stack = stack(heads={11: "e" * 40})
        result = pipeline.finish("blocked", reason="stage_execution_failed")
        self.assertNotIn("ci_warnings", result)
        self.assertNotIn("all_ci_passed", result)

    def test_warning_revalidation_failure_is_visible_in_the_terminal_result(self):
        self.record_warning(11)
        self.clear_everything()
        pipeline = self.pipeline()
        pipeline.run_ci_phase(1, self.stack["members"])
        pipeline.inspect = lambda *_: {"clear": False, "reason": "status_failed"}
        result = pipeline.finish("blocked", reason="stage_execution_failed")
        self.assertEqual("#11: status_failed", result["ci_warning_revalidation_error"])
        compact = MODULE.compact_terminal_result(result)
        transition = MODULE.progress_transition({"event": "stack_pipeline_finished", **compact})
        self.assertIn("#11: status_failed", transition["message"])

    def test_stale_warning_head_requires_a_fresh_worker(self):
        for number in (11, 12, 13):
            self.record_warning(number)
        self.stack = stack(heads={11: "e" * 40})
        self.launcher.on_start = self.complete_worker
        result = self.pipeline().run_ci_phase(2, self.stack["members"])
        self.assertTrue(result["clear"])
        self.assertEqual([11], [request["number"] for request in self.launcher.started])
        self.assertEqual("e" * 40, result["ci_warnings"][0]["head_sha"])

    def test_stale_warning_base_requires_fresh_workers(self):
        for number in (11, 12, 13):
            self.record_warning(number)
        self.launcher.on_start = self.complete_worker
        pipeline = self.pipeline(base_tip=lambda *_: "e" * 40)
        result = pipeline.run_ci_phase(2, self.stack["members"])
        self.assertTrue(result["clear"])
        self.assertEqual([11, 12, 13], [request["number"] for request in self.launcher.started])
        self.assertEqual({"e" * 40}, {warning["base_sha"] for warning in result["ci_warnings"]})

    def test_predecessor_warning_base_movement_blocks_the_next_member(self):
        self.record_warning(11)
        moved = False

        def inspect(entry, target, head, base):
            nonlocal moved
            result = self.inspect(entry, target, head, base)
            if entry["stage"] == MODULE.STAGE_CI and target["number"] == 11:
                moved = True
            return result

        pipeline = self.pipeline(
            inspect=inspect,
            base_tip=lambda *_: "e" * 40 if moved else BASE,
        )
        result = pipeline.run_ci_phase(1, self.stack["members"])
        self.assertFalse(result["clear"])
        self.assertEqual(12, result["blocked"]["number"])
        self.assertEqual([], self.launcher.started)

    def test_stale_warning_snapshot_is_not_complete(self):
        self.clear_everything()
        self.record_warning(11, base="e" * 40)
        result = self.pipeline().final_snapshot()
        self.assertEqual("incomplete", result["result"])
        self.assertNotIn("ci_warnings", result)
        self.assertEqual([MODULE.STAGE_CI], result["pull_requests"][0]["uncleared"])

    def test_warning_snapshot_that_moves_does_not_report_current_warnings(self):
        self.clear_everything()
        self.record_warning(11)
        calls = 0

        def read_stack(*_):
            nonlocal calls
            calls += 1
            return self.stack if calls == 1 else stack(heads={11: "e" * 40})

        result = self.pipeline(read_stack=read_stack).final_snapshot()
        self.assertEqual("heads_moved_during_snapshot", result["reason"])
        self.assertNotIn("ci_warnings", result)
        self.assertNotIn("all_ci_passed", result)

    def test_compact_warning_result_stays_bounded_and_never_loses_warning_flag(self):
        self.record_warning(11)
        warning = {
            **self.warnings[11]["ci_warnings"][0],
            "number": 11, "head_sha": head_of(11), "base_sha": BASE,
            "reason": "failure " * 1000,
            "evidence": ["evidence " * 1000] * 20,
        }
        payload = {"result": "complete", "all_ci_passed": False, "ci_warnings": [warning] * 100}
        result = MODULE.compact_terminal_result(payload, result_path=self.root / "result.json")
        self.assertLessEqual(
            len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()),
            MODULE.TERMINAL_RESULT_MAX_BYTES,
        )
        self.assertFalse(result["all_ci_passed"])
        self.assertTrue(result["ci_warning_details_truncated"])
        self.assertEqual(100, len(result["ci_warnings"]) + result["ci_warnings_omitted"])
        self.assertIn("WITH CI WARNINGS", MODULE.progress_transition({
            "event": "stack_pipeline_finished", **result,
        })["message"])

    def test_terminal_result_is_deterministic_and_bound_to_the_canonical_artifact(self):
        payload = {
            "result": "blocked",
            "reason": "stage_execution_failed",
            "run_id": "run-1",
            "repository": "owner/repo",
            "stack_number": 77,
            "start_pull_request": 11,
            "selected": [11, 12, 13],
            "passes": 1,
            "phases": [{
                "phase": MODULE.STAGE_SELF_REVIEW,
                "stopped": {
                    "number": 12,
                    "stage": MODULE.STAGE_SELF_REVIEW,
                    "reason": "stage_invocation_abandoned",
                    "stage_result": {
                        "stage": MODULE.STAGE_SELF_REVIEW,
                        "status": {
                            "agent_task": {
                                "error": {
                                    "code": "worker_failed",
                                    "message": "retained local commit",
                                }
                            }
                        },
                    },
                },
            }],
        }
        path = self.root / "canonical-result.json"
        COMMON.write_json_atomically(path, {
            "kind": MODULE.RUN_KIND,
            "run_id": payload["run_id"],
            "kickoff": kickoff(),
            "finished_at": "2026-09-21T00:00:00Z",
            "pipeline_result": payload,
        })

        first = MODULE.compact_terminal_result(payload, result_path=path)
        second = MODULE.compact_terminal_result(payload, result_path=path)

        self.assertEqual(first, second)
        self.assertEqual(
            hashlib.sha256(path.read_bytes()).hexdigest(),
            first["artifacts"]["result_sha256"],
        )
        self.assertEqual("worker_failed: retained local commit", first["stage_failure"]["error"])
        with self.assertRaisesRegex(MODULE.WorkflowError, "canonical artifact"):
            MODULE.compact_terminal_result(
                {**payload, "reason": "different"}, result_path=path
            )

    def test_stack_worker_command_preserves_frozen_policy_for_ci(self):
        for policy in ("allow", "source-only"):
            with self.subTest(policy=policy):
                pipeline = self.pipeline(github_mutation_policy=policy)
                request = pipeline.request_for(self.stack["members"][0], MODULE.STAGE_CI, 2)
                command = COMMON.stage_command(
                    MODULE.STAGE_BY_NAME[MODULE.STAGE_CI],
                    COMMON.target_for("owner/repo", 11),
                    model="gpt-5.6-sol", effort="high", arguments=request["arguments"],
                )
                self.assertEqual(policy, command[command.index("--github-mutation-policy") + 1])
                self.assertEqual("run-1", command[command.index("--pipeline-run") + 1])
                self.assertEqual("2", command[command.index("--pipeline-max-iterations") + 1])


class SnapshotTest(StackFixture):
    def test_completion_needs_all_five_markers_for_every_selected_member(self):
        self.clear_everything()
        pipeline = self.pipeline()
        complete = pipeline.final_snapshot()
        self.assertEqual("complete", complete["result"])
        self.assertTrue(complete["all_ci_passed"])

        self.clear.discard((13, MODULE.STAGE_DESCRIPTION))
        snapshot = pipeline.final_snapshot()
        self.assertEqual("incomplete", snapshot["result"])
        self.assertEqual("stages_not_clear", snapshot["reason"])
        self.assertNotIn("all_ci_passed", snapshot)
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
        live = stack()
        selected = MODULE.selection_from_stack(
            COMMON.target_for("owner/repo", 11), live
        )
        state = MODULE.new_state(
            selected, "run-1", selected["topologyFingerprint"]
        )
        MODULE.save_state(path, state)

        loaded = MODULE.load_state(path)
        self.assertEqual(MODULE.STATE_VERSION, loaded["state_version"])
        self.assertEqual(selected, loaded["kickoff"])
        self.assertEqual(
            {
                "repository": "owner/repo",
                "stack_number": 77,
                "start_pull_request": 11,
                "selected": [11, 12, 13],
                "topology_fingerprint": selected["topologyFingerprint"],
                "source_snapshot": selected["sourceSnapshot"],
                "source_stack": selected["sourceStack"],
            },
            loaded["selection"],
        )
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
            request_path=self.root / "request.json",
            state_path=self.root / "state.json",
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
                "--stack-request",
                str(self.root / "request.json"),
                "--state",
                str(self.root / "state.json"),
            ],
            seen[0][2:],
        )

    def test_propagation_reports_an_uninstalled_conflict_plugin(self):
        outcome = MODULE.propagate_descendants(
            "owner/repo",
            11,
            "a" * 40,
            77,
            request_path=self.root / "request.json",
            state_path=self.root / "state.json",
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
        self.enterContext(mock.patch.object(MODULE, "WINDOWS_WORKTREE_PATH_BUDGET", 4096))
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
            {
                "run_id": self.launcher.run_id,
                "repository": self.launcher.repository,
                "number": 11,
                "path": str(path),
                "head_sha": head_of(11),
                "status": "active",
                "created_at": "2026-09-21T00:00:00Z",
            },
        )

        def remove_worktree(command, *, check):
            self.assertFalse(check)
            if command[3] == "status":
                return subprocess.CompletedProcess(command, 0, "", "")
            self.assertEqual(
                ["git", "-C", str(self.launcher.repo_root), "worktree", "remove", str(path)],
                command,
            )
            path.rmdir()
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(COMMON, "run", side_effect=remove_worktree):
            cleaned = self.launcher.cleanup(11)

        self.assertEqual("removed", cleaned["result"])
        self.assertFalse(self.launcher.worktree_root.exists())
        self.assertEqual(str(record), cleaned["ownership_record"])
        retained = COMMON.read_json(record)
        self.assertEqual(self.launcher.run_id, retained["run_id"])
        self.assertEqual(self.launcher.repository, retained["repository"])
        self.assertEqual(11, retained["number"])
        self.assertEqual(str(path), retained["path"])
        self.assertEqual(head_of(11), retained["head_sha"])
        self.assertEqual("2026-09-21T00:00:00Z", retained["created_at"])
        self.assertEqual("removed", retained["status"])
        self.assertIsInstance(retained["removed_at"], str)
        self.assertTrue(self.launcher.run_directory.exists())

        repeated = self.launcher.cleanup(11)

        self.assertEqual(
            {
                "result": "removed",
                "number": 11,
                "ownership_record": str(record),
                "already_removed": True,
            },
            repeated,
        )

    def test_verification_rejects_a_removed_ownership_tombstone(self):
        path = MODULE.worktree_path(self.launcher.worktree_root, 11)
        path.mkdir(parents=True)
        record = MODULE.worktree_ownership_path(self.launcher.run_directory, 11)
        COMMON.write_json_atomically(
            record,
            {
                "run_id": self.launcher.run_id,
                "path": str(path),
                "status": "removed",
                "removed_at": "2026-09-21T00:00:00Z",
            },
        )

        verified = self.launcher.verify(self.request(), path)

        self.assertEqual("failed", verified["result"])
        self.assertEqual("worktree_ownership_missing", verified["reason"])

    def test_malformed_ownership_status_cannot_authorize_worktree_actions(self):
        path = MODULE.worktree_path(self.launcher.worktree_root, 11)
        path.mkdir(parents=True)
        record = MODULE.worktree_ownership_path(
            self.launcher.run_directory, 11
        )
        for status in ([], {}):
            with self.subTest(status=status):
                COMMON.write_json_atomically(
                    record,
                    {
                        "run_id": self.launcher.run_id,
                        "path": str(path),
                        "status": status,
                    },
                )
                with (
                    mock.patch.object(COMMON, "fetch_pr_head") as fetch,
                    mock.patch.object(COMMON, "git_or_none") as git,
                    mock.patch.object(COMMON, "run") as run,
                    mock.patch.object(COMMON, "start_background") as start,
                ):
                    created = self.launcher.create(self.request())
                    verified = self.launcher.verify(self.request(), path)
                    cleaned = self.launcher.cleanup(11)
                    if verified["result"] == "verified":
                        self.launcher.start(self.request(), path)

                self.assertEqual("failed", created["result"])
                self.assertEqual(
                    "worktree_is_not_owned_by_this_run", created["reason"]
                )
                self.assertEqual("failed", verified["result"])
                self.assertEqual(
                    "worktree_ownership_missing", verified["reason"]
                )
                self.assertEqual("failed", cleaned["result"])
                self.assertEqual(
                    "worktree_is_not_owned_by_this_run", cleaned["reason"]
                )
                fetch.assert_not_called()
                git.assert_not_called()
                run.assert_not_called()
                start.assert_not_called()

    def test_cleanup_keeps_every_registered_ownership_state_hashable(self):
        path = MODULE.worktree_path(self.launcher.worktree_root, 11)
        path.mkdir(parents=True)
        record = MODULE.worktree_ownership_path(self.launcher.run_directory, 11)
        execution = SimpleNamespace(
            run_id=self.launcher.run_id,
            record_state=mock.Mock(),
        )

        def remove_worktree(command, *, check):
            self.assertFalse(check)
            if command[3] == "status":
                return subprocess.CompletedProcess(command, 0, "", "")
            path.rmdir()
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch.object(COMMON, "_EXECUTION", execution),
            mock.patch.object(COMMON, "run", side_effect=remove_worktree),
        ):
            COMMON.write_json_atomically(
                record,
                {
                    "run_id": self.launcher.run_id,
                    "path": str(path),
                    "status": "active",
                },
            )
            cleaned = self.launcher.cleanup(11)

        self.assertEqual("removed", cleaned["result"])
        registered = {
            call.args[0].resolve()
            for call in execution.record_state.call_args_list
        }
        self.assertEqual({record.resolve()}, registered)
        self.assertTrue(all(registered_path.is_file() for registered_path in registered))

    def owned_worktree(self):
        path = MODULE.worktree_path(self.launcher.worktree_root, 11)
        path.mkdir(parents=True)
        record = MODULE.worktree_ownership_path(self.launcher.run_directory, 11)
        COMMON.write_json_atomically(
            record, {"run_id": self.launcher.run_id, "path": str(path)}
        )
        return path, record

    def test_cleanup_preserves_tracked_staged_and_untracked_changes(self):
        path, record = self.owned_worktree()
        evidence = path / "unfinished.txt"
        evidence.write_bytes(b"synthetic evidence\n")
        record_bytes = record.read_bytes()
        for status in (" M unfinished.txt\n", "R  old.txt -> new.txt\n", "?? new.txt\n"):
            with self.subTest(status=status), mock.patch.object(
                COMMON, "run",
                return_value=subprocess.CompletedProcess([], 0, status, ""),
            ) as run:
                cleaned = self.launcher.cleanup(11)

                self.assertEqual("preserved", cleaned["result"])
                self.assertEqual("worktree_is_dirty", cleaned["reason"])
                self.assertEqual(status.strip(), cleaned["detail"])
                self.assertEqual(str(path), cleaned["worktree"])
                self.assertEqual(str(record), cleaned["ownership_record"])
                run.assert_called_once_with(
                    [
                        "git", "-C", str(path), "status",
                        "--porcelain=v1", "--untracked-files=all",
                    ],
                    check=False,
                )
                self.assertEqual(record_bytes, record.read_bytes())
                self.assertEqual(b"synthetic evidence\n", evidence.read_bytes())

    def test_cleanup_preserves_unreadable_worktree(self):
        path, record = self.owned_worktree()
        for outcome in (
            subprocess.CompletedProcess([], 128, "", "cannot read index"),
            OSError("git unavailable"),
            MODULE.WorkflowError("status timed out"),
        ):
            with self.subTest(outcome=outcome), mock.patch.object(
                COMMON, "run",
                **(
                    {"side_effect": outcome}
                    if isinstance(outcome, Exception)
                    else {"return_value": outcome}
                ),
            ) as run:
                cleaned = self.launcher.cleanup(11)

                self.assertEqual("preserved", cleaned["result"])
                self.assertEqual("worktree_status_unavailable", cleaned["reason"])
                self.assertTrue(cleaned["detail"])
                self.assertTrue(path.exists())
                self.assertTrue(record.exists())
                self.assertEqual(1, run.call_count)

    def test_cleanup_does_not_force_remove_when_the_worktree_changes_after_status(self):
        path, record = self.owned_worktree()
        evidence = path / "late-edit.txt"

        def replay(command, *, check):
            self.assertFalse(check)
            if command[3] == "status":
                return subprocess.CompletedProcess(command, 0, "", "")
            self.assertNotIn("--force", command)
            evidence.write_bytes(b"late synthetic edit\n")
            return subprocess.CompletedProcess(command, 128, "", "worktree is dirty")

        with mock.patch.object(COMMON, "run", side_effect=replay) as run:
            cleaned = self.launcher.cleanup(11)

        self.assertEqual(2, run.call_count)
        self.assertEqual("preserved", cleaned["result"])
        self.assertEqual("worktree_remove_failed", cleaned["reason"])
        self.assertEqual("worktree is dirty", cleaned["detail"])
        self.assertEqual(b"late synthetic edit\n", evidence.read_bytes())
        self.assertTrue(record.exists())

    def test_cleanup_preserves_worktree_when_remove_raises(self):
        path, record = self.owned_worktree()
        with mock.patch.object(
            COMMON, "run",
            side_effect=[
                subprocess.CompletedProcess([], 0, "", ""),
                OSError("remove failed"),
            ],
        ):
            cleaned = self.launcher.cleanup(11)

        self.assertEqual("preserved", cleaned["result"])
        self.assertEqual("worktree_remove_failed", cleaned["reason"])
        self.assertTrue(path.exists())
        self.assertTrue(record.exists())

    def test_cleanup_does_not_record_removal_after_a_failed_remove(self):
        path, record = self.owned_worktree()

        def remove_then_fail(command, *, check):
            self.assertFalse(check)
            if command[3] == "status":
                return subprocess.CompletedProcess(command, 0, "", "")
            path.rmdir()
            return subprocess.CompletedProcess(command, 1, "", "remove failed")

        with mock.patch.object(COMMON, "run", side_effect=remove_then_fail):
            cleaned = self.launcher.cleanup(11)

        self.assertEqual("failed", cleaned["result"])
        self.assertEqual("worktree_remove_failed", cleaned["reason"])
        self.assertTrue(cleaned["worktree_removed"])
        self.assertFalse(path.exists())
        self.assertNotEqual("removed", COMMON.read_json(record).get("status"))

    def test_cleanup_reports_tombstone_write_failure_without_claiming_removal(self):
        path, record = self.owned_worktree()

        def remove_worktree(command, *, check):
            self.assertFalse(check)
            if command[3] == "status":
                return subprocess.CompletedProcess(command, 0, "", "")
            path.rmdir()
            return subprocess.CompletedProcess(command, 0, "", "")

        original_write = COMMON.write_json_atomically

        def fail_tombstone(target, payload):
            if target == record and payload.get("status") == "removed":
                raise OSError("cannot retain tombstone")
            original_write(target, payload)

        with (
            mock.patch.object(COMMON, "run", side_effect=remove_worktree),
            mock.patch.object(COMMON, "write_json_atomically", side_effect=fail_tombstone),
        ):
            cleaned = self.launcher.cleanup(11)

        self.assertEqual("failed", cleaned["result"])
        self.assertEqual("ownership_record_update_failed", cleaned["reason"])
        self.assertTrue(cleaned["worktree_removed"])
        self.assertFalse(path.exists())
        self.assertTrue(record.exists())
        self.assertNotEqual("removed", COMMON.read_json(record).get("status"))

    def test_cleanup_status_and_remove_use_hidden_windows_processes(self):
        path, _record = self.owned_worktree()

        def replay(command, **_kwargs):
            if command[3] == "worktree":
                path.rmdir()
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch.object(COMMON, "IS_WINDOWS", True),
            mock.patch.object(COMMON.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
            mock.patch.object(COMMON.subprocess, "run", side_effect=replay) as run,
        ):
            cleaned = self.launcher.cleanup(11)

        self.assertEqual("removed", cleaned["result"])
        self.assertEqual(2, run.call_count)
        for call in run.call_args_list:
            self.assertEqual(0x08000000, call.kwargs["creationflags"])

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

    def test_a_silent_live_coordinator_is_ready_while_waiting_for_its_child(self):
        request = self.request()
        record = self.launcher.record_path(request)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("{}", encoding="utf-8")
        ready = self.launcher.confirm_ready(
            request,
            {
                "handle": FakeHandle(alive_polls=100),
                "pid": 99,
                "log_path": self.launcher.log_path(request),
                "record_path": record,
            },
        )
        self.assertEqual("active", ready["result"])
        self.assertEqual(0, ready["evidence"]["log_bytes"])
        self.assertIsNone(ready["evidence"]["exited"])

    def test_nonzero_startup_exit_is_failure_even_with_log_output(self):
        request = self.request()
        record = self.launcher.record_path(request)
        log = self.launcher.log_path(request)
        record.parent.mkdir(parents=True, exist_ok=True)
        log.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("{}", encoding="utf-8")
        log.write_text("success\n", encoding="utf-8")
        ready = self.launcher.confirm_ready(
            request,
            {
                "handle": FakeHandle(returncode=1),
                "pid": 99,
                "log_path": log,
                "record_path": record,
            },
        )
        self.assertEqual("failed", ready["result"])

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
        self.assertIn("only with model `gpt-5.6-sol`", self.text)
        self.assertIn("require `high`", self.text)
        self.assertIn("An unavailable effort value is allowed", self.text)
        self.assertIn("cannot be determined", self.text)

    def test_the_agent_only_runs_and_reports_the_foreground_helper(self):
        self.assertIn('pr_stack_pipeline.py" run <target>', self.text)
        self.assertNotIn('pr_stack_pipeline.py" watch', self.text)
        self.assertIn("Run the installed helper once", self.text)
        self.assertIn("Invoke the helper synchronously", self.text)
        self.assertIn(
            "Do not send a user-visible response while the command is running",
            self.text,
        )
        self.assertIn("There is no intermediate user-visible outcome", self.text)
        self.assertNotIn("execution tool's asynchronous mode", self.text)
        self.assertIn("shared Runtime owns execution identity", self.text)
        self.assertIn("verified terminal `workflow_result`", self.text)

    def test_the_agent_hides_runtime_plumbing(self):
        for text in (
            "--execution-handle",
            "--stack-request",
            "request file",
            "--repo-root",
        ):
            with self.subTest(text=text):
                self.assertNotIn(text, self.text)
        self.assertNotIn("execution-status", self.text)
        self.assertNotIn("execution-cancel", self.text)

    def test_the_agent_states_the_session_title(self):
        self.assertIn("result's `session_title`", self.text)

    def test_the_agent_documents_semantic_target_selection(self):
        self.assertIn("GitHub PR URL", self.text)
        self.assertIn("`owner/repo#number`", self.text)
        self.assertIn("bare PR number", self.text)
        self.assertIn("starting pull request plus every open descendant", self.text)
        self.assertIn("Predecessors are not selected", self.text)
        self.assertIn("Draft and non-draft open members are included", self.text)
        self.assertNotIn("kickoff", self.text.lower())
        self.assertNotIn("--kickoff", self.text)
        self.assertNotIn("JSON object", self.text)

    def test_the_agent_documents_normal_authorization_and_explicit_restrictions(self):
        self.assertIn("default `allow` policy", self.text)
        self.assertIn("bot-thread replies and resolution", self.text)
        self.assertIn("never permits merging, approval", self.text)
        self.assertIn("only when the user requests source-only", self.text)

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
        self.assertIn("Each deterministic stage coordinator owns", self.text)
        self.assertNotIn("mergeable_at_head_sha", self.text)
        self.assertNotIn("clean_at_head_sha", self.text)


class ParserTest(unittest.TestCase):
    def test_only_run_is_a_pipeline_command(self):
        parser = MODULE.build_parser()
        action = next(
            action
            for action in parser._actions
            if isinstance(action, __import__("argparse")._SubParsersAction)
        )
        self.assertEqual({"run"}, set(action.choices))

    def test_removed_commands_fail_before_side_effects(self):
        for command in ("start", "watch", "cancel"):
            side_effect = mock.Mock()
            with (
                self.subTest(command=command),
                mock.patch.object(MODULE.sys, "argv", ["pr_stack_pipeline.py", command]),
                mock.patch.object(MODULE.common, "resolve_target", side_effect=side_effect),
                self.assertRaises(SystemExit),
            ):
                MODULE.main()
            side_effect.assert_not_called()

    def test_run_accepts_a_pr_target_and_model_overrides(self):
        args = MODULE.build_parser().parse_args(
            ["run", "owner/repo#11", "--stage-model", "ci-fix-loop=claude-sonnet-5"]
        )
        self.assertEqual("owner/repo#11", args.target)
        self.assertEqual(["ci-fix-loop=claude-sonnet-5"], args.stage_model)

    def test_run_accepts_every_documented_target_form(self):
        parser = MODULE.build_parser()
        for target in (
            "https://github.com/owner/repo/pull/11",
            "owner/repo#11",
            "11",
        ):
            with self.subTest(target=target):
                self.assertEqual(
                    target, parser.parse_args(["run", target]).target
                )

    def test_run_rejects_removed_json_options(self):
        parser = MODULE.build_parser()
        for option in ("--kickoff", "--kickoff-file"):
            with self.subTest(option=option), self.assertRaises(SystemExit):
                parser.parse_args(["run", "11", option, "{}"])

    def test_command_discovers_and_freezes_the_live_suffix(self):
        args = MODULE.build_parser().parse_args(["run", "11"])
        target = COMMON.target_for("owner/repo", 11)
        live = stack(members=(9, 10, 11, 12))
        controller = mock.Mock()
        controller.execute.return_value = {"result": "stopped"}
        controller.result_path = None
        with (
            mock.patch.object(MODULE.common, "require_tools"),
            mock.patch.object(
                MODULE.common, "resolve_repo_root", return_value=Path("repo")
            ),
            mock.patch.object(
                MODULE.common, "resolve_target", return_value=target
            ) as resolve_target,
            mock.patch.object(MODULE, "read_native_stack", return_value=live),
            mock.patch.object(
                MODULE, "StackPipeline", return_value=controller
            ) as stack_pipeline,
            mock.patch.object(MODULE, "ProgressReporter", return_value=mock.Mock()),
        ):
            MODULE.command_run(args)

        resolve_target.assert_called_once_with("11", Path("repo"))
        selected = stack_pipeline.call_args.args[0]
        self.assertEqual([11, 12], selected["pullRequests"])
        self.assertEqual(
            MODULE.topology_fingerprint(live), selected["topologyFingerprint"]
        )
        self.assertEqual(
            MODULE.stack_source_identity(live)[1], selected["sourceSnapshot"]
        )

    def test_execution_controls_use_the_runtime_entrypoint(self):
        runtime = mock.Mock()
        runtime.entrypoint.return_value = 17
        with (
            mock.patch.object(MODULE.sys, "argv", ["pr_stack_pipeline.py", "execution-cancel"]),
            mock.patch.object(MODULE, "_load_execution", return_value=runtime),
        ):
            self.assertEqual(17, MODULE.execution_main())
        runtime.entrypoint.assert_called_once_with(MODULE.main, MODULE.__dict__, commands=("run",))

    def test_session_owned_run_uses_the_runtime_entrypoint(self):
        runtime = mock.Mock()
        runtime.entrypoint.return_value = 17
        with (
            mock.patch.object(MODULE.sys, "argv", ["pr_stack_pipeline.py", "run", "11"]),
            mock.patch.dict(
                MODULE.os.environ,
                {"COPILOT_AGENT_SESSION_ID": "87654321-4321-4321-4321-cba987654321"},
                clear=True,
            ),
            mock.patch.object(MODULE, "_load_execution", return_value=runtime),
        ):
            self.assertEqual(17, MODULE.execution_main())
        runtime.entrypoint.assert_called_once_with(
            MODULE.main, MODULE.__dict__, commands=("run",)
        )


if __name__ == "__main__":
    unittest.main()
