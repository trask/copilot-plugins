import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "pr_pipeline.py"
AGENT = Path(__file__).parents[1] / "agents" / "pr-pipeline.agent.md"
SPEC = importlib.util.spec_from_file_location("pr_pipeline", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


HEAD = "head1"
NEXT_HEAD = "head2"


def base_pr() -> dict:
    return {
        "number": 7,
        "title": "Add a thing",
        "pr_url": "https://github.com/owner/repo/pull/7",
        "repo_name": "owner/repo",
        "owner": "owner",
        "repo": "repo",
        "head_owner": "fork",
        "head_repo": "repo",
        "head_branch": "feature",
        "base_branch": "main",
        "is_draft": True,
    }


def build_state(**overrides) -> dict:
    state = {
        "version": MODULE.STATE_VERSION,
        "created_at": "2026-01-01T00:00:00Z",
        "repo_root": "/repo",
        "pr": base_pr(),
        "max_iterations": MODULE.DEFAULT_MAX_ITERATIONS,
        "iteration": 1,
        "stage_high_water": None,
        "stage_models": {
            stage: MODULE.DEFAULT_STAGE_MODEL for stage in MODULE.STAGE_NAMES
        },
        "cleared": {},
        "no_progress": {},
        "running": None,
        "history": [],
        "escalation": None,
        "completed": None,
    }
    state.update(overrides)
    return state


def write_state(directory: Path, **overrides) -> Path:
    path = directory / "pipeline.json"
    path.write_text(json.dumps(build_state(**overrides)), encoding="utf-8")
    return path


def observation(
    *,
    head_sha: str = HEAD,
    mergeable: str = "MERGEABLE",
    merge_state_status: str | None = None,
    checks: str = "success",
    coverage: dict | None = None,
    self_review: str | None = None,
    copilot_review: str | None = None,
    description: str | None = None,
    state: str = "OPEN",
    head_moved_on_last_read: bool = False,
    unavailable: dict | None = None,
) -> dict:
    markers = {
        MODULE.STAGE_CONFLICT: {
            "source": "github",
            "available": True,
            "installed": True,
        },
        MODULE.STAGE_CI: {"source": "github", "available": True, "installed": True},
        MODULE.STAGE_SELF_REVIEW: {
            "source": "helper",
            "available": True,
            "installed": True,
            "clean_at_head_sha": self_review,
        },
        MODULE.STAGE_COPILOT_REVIEW: {
            "source": "helper",
            "available": True,
            "installed": True,
            "clean_at_head_sha": copilot_review,
        },
        MODULE.STAGE_DESCRIPTION: {
            "source": "helper",
            "available": True,
            "installed": True,
            "clean_at_head_sha": description,
        },
    }
    for stage, marker in (unavailable or {}).items():
        markers[stage] = marker
    if merge_state_status is None:
        merge_state_status = {"MERGEABLE": "CLEAN", "CONFLICTING": "DIRTY"}.get(
            mergeable, "UNKNOWN"
        )
    return {
        "pr": base_pr(),
        "state": state,
        "head_sha": head_sha,
        "base_sha": "base1",
        "mergeable": mergeable,
        "merge_state_status": merge_state_status,
        "mergeability": MODULE.corroborate_mergeability(mergeable, merge_state_status),
        "checks": {
            "state": checks,
            "total": 3,
            "counts": {},
            "failing": [],
            "pending": [],
            "coverage": coverage
            or {
                "state": "complete",
                "reason": "covers_earlier_checks",
                "missing": [],
            },
        },
        "reads": {
            "attempts": 1,
            "head_moved": head_moved_on_last_read,
            "head_moved_on_last_read": head_moved_on_last_read,
        },
        "stage_markers": markers,
    }


def all_green(head: str = HEAD) -> dict:
    return observation(
        head_sha=head,
        self_review=head,
        copilot_review=head,
        description=head,
    )


def install_stage_script(root: Path, *stages: str, body: str = "") -> Path:
    """Lay out a fake COPILOT_HOME holding one or more stage helper scripts."""

    for stage in stages:
        entry = MODULE.STAGE_BY_NAME[stage]
        script = (
            root
            / "installed-plugins"
            / "trask-plugins"
            / entry["plugin"]
            / "scripts"
            / f"{entry['module']}.py"
        )
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(body, encoding="utf-8")
    return root


class StageOrderTest(unittest.TestCase):
    def test_order_matches_the_design(self):
        self.assertEqual(
            (
                "conflict-fix-loop",
                "self-review-loop",
                "copilot-review-loop",
                "ci-fix-loop",
                "pr-description",
            ),
            MODULE.STAGE_NAMES,
        )

    def test_every_stage_reference_is_plugin_qualified(self):
        for entry in MODULE.STAGES:
            self.assertEqual(f"{entry['plugin']}:{entry['stage']}", entry["agent"])
            self.assertIn(":", entry["agent"])

    def test_review_stages_read_helpers_and_facts_read_github(self):
        evidence = {entry["stage"]: entry["evidence"] for entry in MODULE.STAGES}
        self.assertEqual("github", evidence[MODULE.STAGE_CONFLICT])
        self.assertEqual("github", evidence[MODULE.STAGE_CI])
        self.assertEqual("helper", evidence[MODULE.STAGE_SELF_REVIEW])
        self.assertEqual("helper", evidence[MODULE.STAGE_COPILOT_REVIEW])
        self.assertEqual("helper", evidence[MODULE.STAGE_DESCRIPTION])

    def test_only_self_review_requires_a_model_family(self):
        required = {
            entry["stage"]: entry["requires_family"] for entry in MODULE.STAGES
        }
        self.assertEqual(MODULE.CLAUDE_FAMILY, required[MODULE.STAGE_SELF_REVIEW])
        for stage, family in required.items():
            if stage != MODULE.STAGE_SELF_REVIEW:
                self.assertIsNone(family)

    def test_default_iteration_cap_is_two(self):
        self.assertEqual(2, MODULE.DEFAULT_MAX_ITERATIONS)


class DecideNextTest(unittest.TestCase):
    def test_conflict_stage_leads_on_a_fresh_pull_request(self):
        decision = MODULE.decide_next(
            build_state(), observation(mergeable="CONFLICTING", checks="failing")
        )
        self.assertEqual("run_stage", decision["result"])
        self.assertEqual(MODULE.STAGE_CONFLICT, decision["stage"])
        self.assertEqual(1, decision["iteration"])
        self.assertFalse(decision["loop_back"])

    def test_mergeable_pull_request_clears_the_conflict_stage_without_running_it(self):
        decision = MODULE.decide_next(build_state(), observation(checks="failing"))
        self.assertEqual(MODULE.STAGE_SELF_REVIEW, decision["stage"])
        self.assertTrue(decision["stage_states"][MODULE.STAGE_CONFLICT]["green"])
        self.assertEqual(
            "github", decision["stage_states"][MODULE.STAGE_CONFLICT]["evidence"]
        )

    def test_self_review_runs_before_copilot_review(self):
        decision = MODULE.decide_next(build_state(), observation())
        self.assertEqual(MODULE.STAGE_SELF_REVIEW, decision["stage"])

    def test_copilot_review_runs_once_self_review_cleared_at_head(self):
        decision = MODULE.decide_next(build_state(), observation(self_review=HEAD))
        self.assertEqual(MODULE.STAGE_COPILOT_REVIEW, decision["stage"])

    def test_check_stage_runs_after_both_review_stages(self):
        decision = MODULE.decide_next(
            build_state(),
            observation(self_review=HEAD, copilot_review=HEAD, checks="failing"),
        )
        self.assertEqual(MODULE.STAGE_CI, decision["stage"])

    def test_description_stage_runs_last(self):
        decision = MODULE.decide_next(
            build_state(), observation(self_review=HEAD, copilot_review=HEAD)
        )
        self.assertEqual(MODULE.STAGE_DESCRIPTION, decision["stage"])

    def test_all_green_completes(self):
        decision = MODULE.decide_next(build_state(), all_green())
        self.assertEqual("complete", decision["result"])
        self.assertEqual(HEAD, decision["head_sha"])

    def test_pending_checks_are_not_success(self):
        decision = MODULE.decide_next(
            build_state(),
            observation(self_review=HEAD, copilot_review=HEAD, checks="pending"),
        )
        self.assertEqual(MODULE.STAGE_CI, decision["stage"])

    def test_a_repository_with_no_checks_still_runs_the_check_stage(self):
        decision = MODULE.decide_next(
            build_state(),
            observation(self_review=HEAD, copilot_review=HEAD, checks="none"),
        )
        self.assertEqual(MODULE.STAGE_CI, decision["stage"])

    def test_a_recorded_clearance_survives_a_helper_that_forgot(self):
        state = build_state(cleared={MODULE.STAGE_SELF_REVIEW: HEAD})
        decision = MODULE.decide_next(state, observation())
        self.assertEqual(MODULE.STAGE_COPILOT_REVIEW, decision["stage"])

    def test_a_recorded_clearance_at_another_head_does_not_count(self):
        state = build_state(cleared={MODULE.STAGE_SELF_REVIEW: "old"})
        decision = MODULE.decide_next(state, observation())
        self.assertEqual(MODULE.STAGE_SELF_REVIEW, decision["stage"])

    def test_a_recorded_clearance_cannot_speak_for_the_checks_on_github(self):
        state = build_state(
            cleared={
                MODULE.STAGE_SELF_REVIEW: HEAD,
                MODULE.STAGE_COPILOT_REVIEW: HEAD,
                MODULE.STAGE_CI: HEAD,
            }
        )
        decision = MODULE.decide_next(state, observation(checks="failing"))
        self.assertEqual("run_stage", decision["result"])
        self.assertEqual(MODULE.STAGE_CI, decision["stage"])
        self.assertEqual("github", decision["stage_states"][MODULE.STAGE_CI]["evidence"])

    def test_a_recorded_clearance_cannot_speak_for_a_conflict_on_github(self):
        state = build_state(cleared={MODULE.STAGE_CONFLICT: HEAD})
        decision = MODULE.decide_next(state, observation(mergeable="CONFLICTING"))
        self.assertEqual(MODULE.STAGE_CONFLICT, decision["stage"])

    def test_a_closed_pull_request_escalates(self):
        decision = MODULE.decide_next(build_state(), observation(state="CLOSED"))
        self.assertEqual("escalate", decision["result"])
        self.assertEqual("pr_not_open", decision["reason"])
        self.assertFalse(decision["recorded"])

    def test_a_recorded_escalation_is_returned_without_recording_it_again(self):
        state = build_state(
            escalation={
                "stage": MODULE.STAGE_CI,
                "reason": "stage_escalated",
                "detail": "the stage stopped",
                "next_action": "read the session",
            }
        )
        decision = MODULE.decide_next(state, all_green())
        self.assertEqual("escalate", decision["result"])
        self.assertTrue(decision["recorded"])
        self.assertEqual(MODULE.STAGE_CI, decision["stage"])

    def test_a_missing_stage_helper_escalates_when_that_stage_is_next(self):
        decision = MODULE.decide_next(
            build_state(),
            observation(
                unavailable={
                    MODULE.STAGE_SELF_REVIEW: {
                        "source": "helper",
                        "available": False,
                        "installed": False,
                        "reason": "helper_missing",
                    }
                }
            ),
        )
        self.assertEqual("escalate", decision["result"])
        self.assertEqual("helper_missing", decision["reason"])
        self.assertEqual(MODULE.STAGE_SELF_REVIEW, decision["stage"])

    def test_a_missing_github_backed_plugin_escalates_instead_of_launching(self):
        decision = MODULE.decide_next(
            build_state(),
            observation(
                mergeable="CONFLICTING",
                unavailable={
                    MODULE.STAGE_CONFLICT: {
                        "source": "github",
                        "available": True,
                        "installed": False,
                    }
                },
            ),
        )
        self.assertEqual("escalate", decision["result"])
        self.assertEqual("helper_missing", decision["reason"])
        self.assertEqual(MODULE.STAGE_CONFLICT, decision["stage"])
        self.assertIn("conflict-fix-loop:conflict-fix-loop", decision["detail"])

    def test_a_missing_check_plugin_escalates_instead_of_launching(self):
        decision = MODULE.decide_next(
            build_state(),
            observation(
                self_review=HEAD,
                copilot_review=HEAD,
                checks="failing",
                unavailable={
                    MODULE.STAGE_CI: {
                        "source": "github",
                        "available": True,
                        "installed": False,
                    }
                },
            ),
        )
        self.assertEqual("escalate", decision["result"])
        self.assertEqual("helper_missing", decision["reason"])
        self.assertEqual(MODULE.STAGE_CI, decision["stage"])
        self.assertEqual(
            MODULE.ESCALATION_ACTIONS["helper_missing"], decision["next_action"]
        )

    def test_a_missing_plugin_whose_stage_is_green_stops_nothing(self):
        decision = MODULE.decide_next(
            build_state(),
            observation(
                unavailable={
                    MODULE.STAGE_CONFLICT: {
                        "source": "github",
                        "available": True,
                        "installed": False,
                    }
                }
            ),
        )
        self.assertEqual("run_stage", decision["result"])
        self.assertEqual(MODULE.STAGE_SELF_REVIEW, decision["stage"])

    def test_a_missing_later_plugin_does_not_stop_an_earlier_stage(self):
        decision = MODULE.decide_next(
            build_state(),
            observation(
                checks="failing",
                unavailable={
                    MODULE.STAGE_CI: {
                        "source": "github",
                        "available": True,
                        "installed": False,
                    }
                },
            ),
        )
        self.assertEqual("run_stage", decision["result"])
        self.assertEqual(MODULE.STAGE_SELF_REVIEW, decision["stage"])

    def test_a_missing_later_helper_does_not_stop_an_earlier_stage(self):
        decision = MODULE.decide_next(
            build_state(),
            observation(
                unavailable={
                    MODULE.STAGE_DESCRIPTION: {
                        "source": "helper",
                        "available": False,
                        "installed": False,
                        "reason": "helper_missing",
                    }
                }
            ),
        )
        self.assertEqual("run_stage", decision["result"])
        self.assertEqual(MODULE.STAGE_SELF_REVIEW, decision["stage"])

    def test_two_stalled_runs_escalate_before_launching_again(self):
        state = build_state(
            no_progress={MODULE.STAGE_SELF_REVIEW: {"count": 2, "head_sha": HEAD}}
        )
        decision = MODULE.decide_next(state, observation())
        self.assertEqual("escalate", decision["result"])
        self.assertEqual("no_progress", decision["reason"])

    def test_one_stalled_run_still_launches(self):
        state = build_state(
            no_progress={MODULE.STAGE_SELF_REVIEW: {"count": 1, "head_sha": HEAD}}
        )
        decision = MODULE.decide_next(state, observation())
        self.assertEqual("run_stage", decision["result"])


class LoopBackTest(unittest.TestCase):
    def test_moving_forward_keeps_the_iteration(self):
        state = build_state(
            iteration=1, stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_SELF_REVIEW]
        )
        decision = MODULE.decide_next(state, observation(self_review=HEAD))
        self.assertEqual(MODULE.STAGE_COPILOT_REVIEW, decision["stage"])
        self.assertEqual(1, decision["iteration"])
        self.assertFalse(decision["loop_back"])

    def test_new_commits_send_an_earlier_stage_round_again(self):
        state = build_state(
            iteration=1,
            stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_COPILOT_REVIEW],
            cleared={MODULE.STAGE_SELF_REVIEW: HEAD},
        )
        decision = MODULE.decide_next(state, observation(head_sha=NEXT_HEAD))
        self.assertEqual(MODULE.STAGE_SELF_REVIEW, decision["stage"])
        self.assertEqual(2, decision["iteration"])
        self.assertTrue(decision["loop_back"])

    def test_a_loop_back_past_the_cap_escalates(self):
        state = build_state(
            iteration=2,
            max_iterations=2,
            stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_CI],
        )
        decision = MODULE.decide_next(state, observation(head_sha=NEXT_HEAD))
        self.assertEqual("escalate", decision["result"])
        self.assertEqual("max_iterations_reached", decision["reason"])
        self.assertIn("iteration 3 of a maximum of 2", decision["detail"])

    def test_the_same_stage_again_is_not_a_loop_back(self):
        state = build_state(
            iteration=1,
            stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_SELF_REVIEW],
        )
        decision = MODULE.decide_next(state, observation())
        self.assertEqual(MODULE.STAGE_SELF_REVIEW, decision["stage"])
        self.assertEqual(1, decision["iteration"])
        self.assertFalse(decision["loop_back"])

    def test_base_branch_movement_alone_changes_nothing(self):
        state = build_state(
            iteration=1,
            stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_DESCRIPTION],
        )
        moved_base = all_green()
        moved_base["base_sha"] = "base2"
        decision = MODULE.decide_next(state, moved_base)
        self.assertEqual("complete", decision["result"])

    def test_a_description_edit_does_not_move_the_head_or_loop_back(self):
        state = build_state(
            iteration=1,
            stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_DESCRIPTION],
            cleared={
                MODULE.STAGE_CONFLICT: HEAD,
                MODULE.STAGE_SELF_REVIEW: HEAD,
                MODULE.STAGE_COPILOT_REVIEW: HEAD,
                MODULE.STAGE_CI: HEAD,
            },
        )
        decision = MODULE.decide_next(state, all_green())
        self.assertEqual("complete", decision["result"])
        self.assertEqual(1, state["iteration"])


class ProjectedIterationTest(unittest.TestCase):
    def test_first_stage_of_a_fresh_run(self):
        projection = MODULE.projected_iteration(build_state(), MODULE.STAGE_CONFLICT)
        self.assertEqual(
            {"iteration": 1, "loop_back": False, "high_water": 0}, projection
        )

    def test_high_water_only_ever_advances_forward(self):
        state = build_state(stage_high_water=3)
        projection = MODULE.projected_iteration(state, MODULE.STAGE_CI)
        self.assertEqual(3, projection["high_water"])
        self.assertFalse(projection["loop_back"])

    def test_stepping_back_resets_the_high_water_and_counts_an_iteration(self):
        state = build_state(iteration=1, stage_high_water=3)
        projection = MODULE.projected_iteration(state, MODULE.STAGE_SELF_REVIEW)
        self.assertEqual(
            {"iteration": 2, "loop_back": True, "high_water": 1}, projection
        )


class CheckSummaryTest(unittest.TestCase):
    def test_an_empty_rollup_is_none_and_never_success(self):
        summary = MODULE.summarize_checks([])
        self.assertEqual("none", summary["state"])
        self.assertEqual(0, summary["total"])

    def test_a_missing_rollup_is_none(self):
        self.assertEqual("none", MODULE.summarize_checks(None)["state"])

    def test_all_passing_checks_are_success(self):
        summary = MODULE.summarize_checks(
            [
                {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "lint", "state": "SUCCESS"},
            ]
        )
        self.assertEqual("success", summary["state"])

    def test_a_neutral_or_skipped_check_still_passes(self):
        summary = MODULE.summarize_checks(
            [
                {"name": "build", "status": "COMPLETED", "conclusion": "NEUTRAL"},
                {"name": "docs", "status": "COMPLETED", "conclusion": "SKIPPED"},
            ]
        )
        self.assertEqual("success", summary["state"])

    def test_one_failure_makes_the_rollup_failing(self):
        summary = MODULE.summarize_checks(
            [
                {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "test", "status": "COMPLETED", "conclusion": "FAILURE"},
            ]
        )
        self.assertEqual("failing", summary["state"])
        self.assertEqual([{"name": "test", "state": "FAILURE"}], summary["failing"])

    def test_a_running_check_makes_the_rollup_pending(self):
        summary = MODULE.summarize_checks(
            [{"name": "build", "status": "IN_PROGRESS", "conclusion": None}]
        )
        self.assertEqual("pending", summary["state"])
        self.assertEqual([{"name": "build", "state": "IN_PROGRESS"}], summary["pending"])

    def test_a_check_awaiting_maintainer_approval_is_reported_separately(self):
        summary = MODULE.summarize_checks(
            [{"name": "build", "status": "COMPLETED", "conclusion": "ACTION_REQUIRED"}]
        )
        self.assertEqual("failing", summary["state"])
        self.assertEqual(
            [{"name": "build", "state": "ACTION_REQUIRED"}], summary["action_required"]
        )

    def test_a_legacy_context_uses_its_state_and_context_name(self):
        summary = MODULE.summarize_checks([{"context": "ci/legacy", "state": "FAILURE"}])
        self.assertEqual([{"name": "ci/legacy", "state": "FAILURE"}], summary["failing"])

    def test_a_check_with_nothing_usable_is_unknown_and_not_success(self):
        summary = MODULE.summarize_checks([{"name": "mystery"}])
        self.assertEqual("pending", summary["state"])
        self.assertEqual("UNKNOWN", MODULE.check_conclusion({"name": "mystery"}))


class CheckCoverageTest(unittest.TestCase):
    def expected(self, *names: str, head_sha: str = HEAD) -> dict:
        return {"head_sha": head_sha, "names": sorted(names)}

    def judge(self, names, expected, *, waited=0.0, grace=180):
        return MODULE.judge_check_coverage(
            set(names), expected, waited_seconds=waited, grace_seconds=grace
        )

    def test_a_rollup_missing_an_earlier_check_is_not_finished(self):
        coverage = self.judge({"build"}, self.expected("build", "test", "lint"))
        self.assertEqual("incomplete", coverage["state"])
        self.assertEqual(["lint", "test"], coverage["missing"])
        self.assertEqual("missing_earlier_checks", coverage["reason"])

    def test_a_rollup_holding_every_earlier_check_is_finished(self):
        coverage = self.judge({"build", "test"}, self.expected("build", "test"))
        self.assertEqual("complete", coverage["state"])
        self.assertEqual("covers_earlier_checks", coverage["reason"])

    def test_a_rollup_that_adds_a_check_is_not_a_subset(self):
        coverage = self.judge({"build", "extra"}, self.expected("build", "test"))
        self.assertEqual("complete", coverage["state"])

    def test_the_first_head_has_no_expectation_to_measure_against(self):
        for expected in (None, {}, {"head_sha": "old", "names": []}):
            with self.subTest(expected=expected):
                coverage = self.judge({"build"}, expected)
                self.assertEqual("unverified", coverage["state"])
                self.assertEqual("no_earlier_head", coverage["reason"])

    def test_the_wait_for_missing_checks_is_bounded(self):
        coverage = self.judge(
            {"build"}, self.expected("build", "test"), waited=181, grace=180
        )
        self.assertEqual("complete", coverage["state"])
        self.assertEqual("settled_smaller", coverage["reason"])
        self.assertEqual(["test"], coverage["missing"])

    def test_a_wait_that_cannot_be_measured_does_not_deadlock(self):
        coverage = MODULE.judge_check_coverage(
            {"build"}, self.expected("build", "test"), waited_seconds=None
        )
        self.assertEqual("complete", coverage["state"])
        self.assertEqual("wait_not_measurable", coverage["reason"])


class ApplyCheckCoverageTest(unittest.TestCase):
    def rollup(self, *names: str) -> list[dict]:
        return [
            {"name": name, "status": "COMPLETED", "conclusion": "SUCCESS"}
            for name in names
        ]

    def observe(self, state, head_sha, *names, now="2024-01-01T00:00:00Z"):
        seen = {
            "pr": base_pr(),
            "head_sha": head_sha,
            "checks": MODULE.summarize_checks(self.rollup(*names)),
        }
        with mock.patch.object(MODULE, "utc_now", return_value=now):
            MODULE.apply_check_coverage(state, seen)
        return seen

    def test_the_first_head_seen_is_only_recorded(self):
        state: dict = {}
        seen = self.observe(state, HEAD, "build", "test")
        self.assertEqual("success", seen["checks"]["state"])
        self.assertEqual("unverified", seen["checks"]["coverage"]["state"])
        self.assertEqual(HEAD, state["checks_seen"]["head_sha"])
        self.assertEqual(["build", "test"], state["checks_seen"]["names"])
        self.assertNotIn("checks_expected", state)

    def test_a_partial_rollup_at_a_new_head_reports_pending(self):
        state: dict = {}
        self.observe(state, HEAD, "build", "test")
        seen = self.observe(state, NEXT_HEAD, "build", now="2024-01-01T00:00:10Z")
        self.assertEqual("pending", seen["checks"]["state"])
        self.assertEqual("incomplete", seen["checks"]["coverage"]["state"])
        self.assertEqual(["test"], seen["checks"]["coverage"]["missing"])
        self.assertEqual(HEAD, state["checks_expected"]["head_sha"])

    def test_a_complete_rollup_at_a_new_head_stays_success(self):
        state: dict = {}
        self.observe(state, HEAD, "build", "test")
        seen = self.observe(
            state, NEXT_HEAD, "build", "test", now="2024-01-01T00:00:10Z"
        )
        self.assertEqual("success", seen["checks"]["state"])
        self.assertEqual("complete", seen["checks"]["coverage"]["state"])

    def test_a_smaller_set_is_accepted_once_the_wait_is_spent(self):
        state: dict = {}
        self.observe(state, HEAD, "build", "test")
        self.observe(state, NEXT_HEAD, "build", now="2024-01-01T00:00:10Z")
        seen = self.observe(state, NEXT_HEAD, "build", now="2024-01-01T01:00:00Z")
        self.assertEqual("success", seen["checks"]["state"])
        self.assertEqual("settled_smaller", seen["checks"]["coverage"]["reason"])

    def test_a_check_appearing_late_restarts_the_wait(self):
        state: dict = {}
        self.observe(state, HEAD, "build", "test", "lint")
        self.observe(state, NEXT_HEAD, "build", now="2024-01-01T00:00:10Z")
        self.observe(state, NEXT_HEAD, "build", "test", now="2024-01-01T01:00:00Z")
        self.assertEqual("2024-01-01T01:00:00Z", state["checks_seen"]["changed_at"])
        seen = self.observe(
            state, NEXT_HEAD, "build", "test", now="2024-01-01T01:00:30Z"
        )
        self.assertEqual("pending", seen["checks"]["state"])
        self.assertEqual(["lint"], seen["checks"]["coverage"]["missing"])

    def test_names_seen_at_one_head_are_remembered_across_reads(self):
        state: dict = {}
        self.observe(state, HEAD, "build")
        self.observe(state, HEAD, "test", now="2024-01-01T00:00:10Z")
        self.assertEqual(["build", "test"], state["checks_seen"]["names"])

    def test_coverage_never_turns_a_failure_into_something_softer(self):
        state: dict = {}
        self.observe(state, HEAD, "build", "test")
        seen = {
            "pr": base_pr(),
            "head_sha": NEXT_HEAD,
            "checks": MODULE.summarize_checks(
                [{"name": "build", "status": "COMPLETED", "conclusion": "FAILURE"}]
            ),
        }
        with mock.patch.object(MODULE, "utc_now", return_value="2024-01-01T00:00:10Z"):
            MODULE.apply_check_coverage(state, seen)
        self.assertEqual("failing", seen["checks"]["state"])

    def test_an_empty_rollup_is_still_none_and_not_pending(self):
        state: dict = {}
        self.observe(state, HEAD, "build")
        seen = self.observe(state, NEXT_HEAD, now="2024-01-01T00:00:10Z")
        self.assertEqual("none", seen["checks"]["state"])

    def test_the_base_commit_is_never_consulted(self):
        self.assertFalse(hasattr(MODULE, "base_check_names"))


class CorroborateMergeabilityTest(unittest.TestCase):
    def test_the_two_fields_agreeing_on_mergeable_settles_it(self):
        verdict = MODULE.corroborate_mergeability("MERGEABLE", "CLEAN")
        self.assertTrue(verdict["settled"])
        self.assertEqual("mergeable", verdict["state"])
        self.assertEqual("corroborated", verdict["reason"])

    def test_a_merge_blocked_by_review_is_still_free_of_conflicts(self):
        verdict = MODULE.corroborate_mergeability("MERGEABLE", "BLOCKED")
        self.assertTrue(verdict["settled"])
        self.assertEqual("mergeable", verdict["state"])

    def test_the_two_fields_agreeing_on_a_conflict_settles_it(self):
        verdict = MODULE.corroborate_mergeability("CONFLICTING", "DIRTY")
        self.assertTrue(verdict["settled"])
        self.assertEqual("conflicting", verdict["state"])

    def test_a_dirty_state_never_reads_as_mergeable(self):
        verdict = MODULE.corroborate_mergeability("MERGEABLE", "DIRTY")
        self.assertFalse(verdict["settled"])
        self.assertEqual("conflicting", verdict["state"])
        self.assertEqual("disagreed", verdict["reason"])

    def test_an_unknown_merge_state_settles_nothing(self):
        for status in ("UNKNOWN", "", None):
            with self.subTest(status=status):
                verdict = MODULE.corroborate_mergeability("MERGEABLE", status)
                self.assertFalse(verdict["settled"])
                self.assertEqual("merge_state_unknown", verdict["reason"])

    def test_an_unknown_mergeable_settles_nothing(self):
        verdict = MODULE.corroborate_mergeability("UNKNOWN", "CLEAN")
        self.assertFalse(verdict["settled"])
        self.assertEqual("unsettled", verdict["state"])
        self.assertEqual("mergeable_unknown", verdict["reason"])

    def test_a_conflict_the_merge_state_denies_settles_nothing(self):
        verdict = MODULE.corroborate_mergeability("CONFLICTING", "CLEAN")
        self.assertFalse(verdict["settled"])
        self.assertEqual("disagreed", verdict["reason"])

    def test_a_dirty_state_beside_an_unknown_mergeable_is_still_not_green(self):
        verdict = MODULE.corroborate_mergeability(None, "DIRTY")
        self.assertFalse(verdict["settled"])
        self.assertEqual("conflicting", verdict["state"])
        self.assertEqual("mergeable_unknown", verdict["reason"])

    def test_a_value_nobody_recognises_settles_nothing(self):
        verdict = MODULE.corroborate_mergeability("SOMEDAY", "CLEAN")
        self.assertFalse(verdict["settled"])
        self.assertEqual("unrecognized", verdict["reason"])

    def test_the_fields_are_read_case_insensitively(self):
        verdict = MODULE.corroborate_mergeability("mergeable", "clean")
        self.assertTrue(verdict["settled"])


class ObservePullRequestTest(unittest.TestCase):
    def payload(self, *, head: str = HEAD, mergeable: str, status: str) -> dict:
        return {
            "number": 7,
            "title": "A pull request",
            "state": "OPEN",
            "isDraft": True,
            "mergeable": mergeable,
            "mergeStateStatus": status,
            "headRefName": "topic",
            "headRefOid": head,
            "headRepositoryOwner": {"login": "owner"},
            "headRepository": {"name": "repo"},
            "baseRefName": "main",
            "baseRefOid": "base1",
            "statusCheckRollup": [
                {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"}
            ],
        }

    def observe(self, *responses, known_head_sha: str | None = None):
        self.sleeps: list[float] = []
        with mock.patch.object(MODULE, "gh_json", side_effect=list(responses)) as reader:
            with mock.patch.object(MODULE, "time") as clock:
                clock.sleep = self.sleeps.append
                result = MODULE.observe_pull_request(
                    MODULE.build_target("owner", "repo", 7),
                    known_head_sha=known_head_sha,
                )
        self.reads = reader.call_count
        return result

    def test_a_corroborated_answer_is_taken_on_the_first_read(self):
        result = self.observe(self.payload(mergeable="MERGEABLE", status="CLEAN"))
        self.assertEqual(1, self.reads)
        self.assertTrue(result["mergeability"]["settled"])
        self.assertFalse(result["reads"]["head_moved_on_last_read"])

    def test_an_unknown_answer_is_asked_again_until_the_delays_run_out(self):
        unknown = self.payload(mergeable="UNKNOWN", status="UNKNOWN")
        result = self.observe(*[unknown] * (len(MODULE.MERGEABLE_RETRY_DELAYS) + 1))
        self.assertEqual(len(MODULE.MERGEABLE_RETRY_DELAYS) + 1, self.reads)
        self.assertFalse(result["mergeability"]["settled"])

    def test_two_fields_that_disagree_are_asked_again(self):
        result = self.observe(
            self.payload(mergeable="MERGEABLE", status="DIRTY"),
            self.payload(mergeable="CONFLICTING", status="DIRTY"),
        )
        self.assertEqual(2, self.reads)
        self.assertEqual("conflicting", result["mergeability"]["state"])

    def test_the_first_answer_after_a_push_is_never_taken(self):
        result = self.observe(
            self.payload(head=NEXT_HEAD, mergeable="MERGEABLE", status="CLEAN"),
            self.payload(head=NEXT_HEAD, mergeable="CONFLICTING", status="DIRTY"),
            known_head_sha=HEAD,
        )
        self.assertEqual(2, self.reads)
        self.assertEqual("conflicting", result["mergeability"]["state"])
        self.assertTrue(result["reads"]["head_moved"])
        self.assertFalse(result["reads"]["head_moved_on_last_read"])

    def test_a_head_moving_on_every_read_leaves_the_last_one_uncorroborated(self):
        result = self.observe(
            *[
                self.payload(head=f"sha{index}", mergeable="MERGEABLE", status="CLEAN")
                for index in range(len(MODULE.MERGEABLE_RETRY_DELAYS) + 1)
            ],
            known_head_sha=HEAD,
        )
        self.assertTrue(result["reads"]["head_moved_on_last_read"])

    def test_a_first_read_with_no_earlier_head_is_not_a_push(self):
        result = self.observe(self.payload(mergeable="MERGEABLE", status="CLEAN"))
        self.assertFalse(result["reads"]["head_moved"])

    def test_the_rollup_names_reach_the_summary_unjudged(self):
        result = self.observe(self.payload(mergeable="MERGEABLE", status="CLEAN"))
        self.assertEqual(["build"], result["checks"]["names"])
        self.assertEqual("unverified", result["checks"]["coverage"]["state"])
        self.assertEqual("not_judged", result["checks"]["coverage"]["reason"])


class GithubEvidenceTest(unittest.TestCase):
    def verdict(self, stage: str, observed: dict) -> dict:
        return MODULE.stage_green(
            MODULE.STAGE_BY_NAME[stage],
            head_sha=HEAD,
            cleared={},
            marker=observed["stage_markers"][stage],
            observation=observed,
        )

    def test_a_corroborated_mergeable_clears_the_conflict_stage(self):
        verdict = self.verdict(MODULE.STAGE_CONFLICT, observation())
        self.assertTrue(verdict["green"])

    def test_an_uncorroborated_mergeable_does_not_clear_it(self):
        verdict = self.verdict(
            MODULE.STAGE_CONFLICT,
            observation(mergeable="MERGEABLE", merge_state_status="UNKNOWN"),
        )
        self.assertFalse(verdict["green"])
        self.assertEqual("merge_state_unknown", verdict["reason"])

    def test_a_mergeable_answer_read_right_after_a_push_does_not_clear_it(self):
        verdict = self.verdict(
            MODULE.STAGE_CONFLICT, observation(head_moved_on_last_read=True)
        )
        self.assertFalse(verdict["green"])
        self.assertEqual("head_moved", verdict["reason"])

    def test_an_observation_with_no_mergeability_at_all_does_not_clear_it(self):
        observed = observation()
        observed.pop("mergeability")
        verdict = self.verdict(MODULE.STAGE_CONFLICT, observed)
        self.assertFalse(verdict["green"])
        self.assertEqual("not_observed", verdict["reason"])

    def test_a_passing_rollup_clears_the_check_stage(self):
        verdict = self.verdict(MODULE.STAGE_CI, observation())
        self.assertTrue(verdict["green"])

    def test_a_passing_rollup_read_right_after_a_push_does_not_clear_it(self):
        verdict = self.verdict(
            MODULE.STAGE_CI, observation(head_moved_on_last_read=True)
        )
        self.assertFalse(verdict["green"])
        self.assertEqual("head_moved", verdict["reason"])

    def test_an_incomplete_rollup_sends_the_check_stage_round_again(self):
        observed = observation(
            checks="pending",
            coverage={
                "state": "incomplete",
                "reason": "missing_earlier_checks",
                "missing": ["test"],
            },
            self_review=HEAD,
            copilot_review=HEAD,
            description=HEAD,
        )
        decision = MODULE.decide_next(build_state(), observed)
        self.assertEqual("run_stage", decision["result"])
        self.assertEqual(MODULE.STAGE_CI, decision["stage"])
        self.assertEqual(
            ["test"], decision["stage_states"][MODULE.STAGE_CI]["missing_checks"]
        )

    def test_a_conflict_the_two_fields_disagree_about_sends_the_stage_round_again(self):
        observed = observation(mergeable="MERGEABLE", merge_state_status="DIRTY")
        decision = MODULE.decide_next(build_state(), observed)
        self.assertEqual("run_stage", decision["result"])
        self.assertEqual(MODULE.STAGE_CONFLICT, decision["stage"])


class StageMarkerTest(unittest.TestCase):
    def test_self_review_needs_a_clean_outcome_and_a_matching_sha(self):
        payload = {
            "result": "ready",
            "review": {"outcome": "clean", "clean_at_head_sha": HEAD},
        }
        self.assertEqual(
            HEAD, MODULE.extract_clean_at_head_sha(MODULE.STAGE_SELF_REVIEW, payload)
        )

    def test_self_review_without_a_clean_outcome_reports_nothing(self):
        payload = {
            "result": "ready",
            "review": {"outcome": "active", "clean_at_head_sha": HEAD},
        }
        self.assertIsNone(
            MODULE.extract_clean_at_head_sha(MODULE.STAGE_SELF_REVIEW, payload)
        )

    def test_self_review_with_no_review_reports_nothing(self):
        self.assertIsNone(
            MODULE.extract_clean_at_head_sha(
                MODULE.STAGE_SELF_REVIEW, {"result": "ready", "review": None}
            )
        )

    def test_copilot_review_reads_a_top_level_marker(self):
        self.assertEqual(
            HEAD,
            MODULE.extract_clean_at_head_sha(
                MODULE.STAGE_COPILOT_REVIEW,
                {"result": "ready", "clean_at_head_sha": HEAD},
            ),
        )

    def test_copilot_review_falls_back_to_a_nested_marker(self):
        self.assertEqual(
            HEAD,
            MODULE.extract_clean_at_head_sha(
                MODULE.STAGE_COPILOT_REVIEW,
                {"result": "ready", "queue": {"clean_at_head_sha": HEAD}},
            ),
        )

    def test_description_reads_its_validated_head(self):
        self.assertEqual(
            HEAD,
            MODULE.extract_clean_at_head_sha(
                MODULE.STAGE_DESCRIPTION,
                {"result": "ready", "validated_head_sha": HEAD},
            ),
        )

    def test_a_status_that_is_not_ready_reports_nothing(self):
        self.assertIsNone(
            MODULE.extract_clean_at_head_sha(
                MODULE.STAGE_DESCRIPTION,
                {"result": "no_state", "validated_head_sha": HEAD},
            )
        )

    def test_a_blank_marker_reports_nothing(self):
        self.assertIsNone(
            MODULE.extract_clean_at_head_sha(
                MODULE.STAGE_DESCRIPTION, {"result": "ready", "validated_head_sha": "  "}
            )
        )

    def test_a_github_backed_stage_needs_no_helper_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = install_stage_script(Path(directory), MODULE.STAGE_CI)
            with mock.patch.object(MODULE, "copilot_home", return_value=root):
                marker = MODULE.read_stage_marker(
                    MODULE.STAGE_BY_NAME[MODULE.STAGE_CI],
                    MODULE.build_target("owner", "repo", 7),
                )
        self.assertEqual("github", marker["source"])
        self.assertTrue(marker["available"])
        self.assertTrue(marker["installed"])

    def test_a_github_backed_stage_reports_a_plugin_that_is_not_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(MODULE, "copilot_home", return_value=Path(directory)):
                marker = MODULE.read_stage_marker(
                    MODULE.STAGE_BY_NAME[MODULE.STAGE_CONFLICT],
                    MODULE.build_target("owner", "repo", 7),
                )
        self.assertEqual("github", marker["source"])
        self.assertFalse(marker["installed"])

    def test_every_stage_reports_whether_its_plugin_is_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(MODULE, "copilot_home", return_value=Path(directory)):
                for stage in MODULE.STAGE_NAMES:
                    with self.subTest(stage=stage):
                        marker = MODULE.read_stage_marker(
                            MODULE.STAGE_BY_NAME[stage],
                            MODULE.build_target("owner", "repo", 7),
                        )
                        self.assertIn("installed", marker)
                        self.assertFalse(marker["installed"])

    def test_a_stage_whose_helper_is_not_installed_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(MODULE, "copilot_home", return_value=Path(directory)):
                marker = MODULE.read_stage_marker(
                    MODULE.STAGE_BY_NAME[MODULE.STAGE_SELF_REVIEW],
                    MODULE.build_target("owner", "repo", 7),
                )
        self.assertFalse(marker["available"])
        self.assertEqual("helper_missing", marker["reason"])

    def test_a_stage_that_never_ran_is_available_with_no_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_SELF_REVIEW]
            script = (
                root
                / "installed-plugins"
                / "trask-plugins"
                / entry["plugin"]
                / "scripts"
                / f"{entry['module']}.py"
            )
            script.parent.mkdir(parents=True)
            script.write_text("", encoding="utf-8")
            with mock.patch.object(MODULE, "copilot_home", return_value=root):
                with mock.patch.object(
                    MODULE, "stage_state_path", return_value=root / "missing.json"
                ):
                    marker = MODULE.read_stage_marker(
                        entry, MODULE.build_target("owner", "repo", 7)
                    )
        self.assertTrue(marker["available"])
        self.assertEqual("no_state", marker["reason"])
        self.assertIsNone(marker["clean_at_head_sha"])

    def test_a_helper_status_that_fails_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_SELF_REVIEW]
            script = (
                root
                / "installed-plugins"
                / "trask-plugins"
                / entry["plugin"]
                / "scripts"
                / f"{entry['module']}.py"
            )
            script.parent.mkdir(parents=True)
            script.write_text("", encoding="utf-8")
            state = root / "state.json"
            state.write_text("{}", encoding="utf-8")
            failure = SimpleNamespace(returncode=1, stdout="", stderr="boom")
            with mock.patch.object(MODULE, "copilot_home", return_value=root):
                with mock.patch.object(MODULE, "stage_state_path", return_value=state):
                    with mock.patch.object(MODULE, "run", return_value=failure):
                        marker = MODULE.read_stage_marker(
                            entry, MODULE.build_target("owner", "repo", 7)
                        )
        self.assertFalse(marker["available"])
        self.assertEqual("status_failed", marker["reason"])
        self.assertEqual("boom", marker["detail"])

    def test_a_helper_status_that_is_not_json_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_DESCRIPTION]
            script = (
                root
                / "installed-plugins"
                / "trask-plugins"
                / entry["plugin"]
                / "scripts"
                / f"{entry['module']}.py"
            )
            script.parent.mkdir(parents=True)
            script.write_text("", encoding="utf-8")
            state = root / "state.json"
            state.write_text("{}", encoding="utf-8")
            broken = SimpleNamespace(returncode=0, stdout="not json", stderr="")
            with mock.patch.object(MODULE, "copilot_home", return_value=root):
                with mock.patch.object(MODULE, "stage_state_path", return_value=state):
                    with mock.patch.object(MODULE, "run", return_value=broken):
                        marker = MODULE.read_stage_marker(
                            entry, MODULE.build_target("owner", "repo", 7)
                        )
        self.assertFalse(marker["available"])
        self.assertEqual("invalid_status_json", marker["reason"])

    def test_a_helper_status_that_reports_clean_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_DESCRIPTION]
            script = (
                root
                / "installed-plugins"
                / "trask-plugins"
                / entry["plugin"]
                / "scripts"
                / f"{entry['module']}.py"
            )
            script.parent.mkdir(parents=True)
            script.write_text("", encoding="utf-8")
            state = root / "state.json"
            state.write_text("{}", encoding="utf-8")
            payload = SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"result": "ready", "validated_head_sha": HEAD}),
                stderr="",
            )
            with mock.patch.object(MODULE, "copilot_home", return_value=root):
                with mock.patch.object(MODULE, "stage_state_path", return_value=state):
                    with mock.patch.object(MODULE, "run", return_value=payload):
                        marker = MODULE.read_stage_marker(
                            entry, MODULE.build_target("owner", "repo", 7)
                        )
        self.assertEqual(HEAD, marker["clean_at_head_sha"])

    def test_stage_state_paths_follow_the_shared_layout(self):
        target = MODULE.build_target("owner", "repo", 7)
        path = MODULE.stage_state_path("self-review-loop", target)
        self.assertEqual("owner--repo--7.json", path.name)
        self.assertEqual("self-review-loop", path.parent.name)
        self.assertEqual("run", path.parent.parent.name)

    def test_the_pipeline_state_path_lives_under_pr_pipeline(self):
        path = MODULE.default_state_path(MODULE.build_target("owner", "repo", 7))
        self.assertEqual("owner--repo--7.json", path.name)
        self.assertEqual("pr-pipeline", path.parent.name)


class StageOutcomeTest(unittest.TestCase):
    def payload(self, **values) -> dict:
        return {"result": "ready", **values}

    def test_a_reported_outcome_is_read(self):
        for outcome in MODULE.STAGE_OUTCOMES:
            with self.subTest(outcome=outcome):
                self.assertEqual(
                    outcome,
                    MODULE.extract_stage_outcome(self.payload(stage_outcome=outcome)),
                )

    def test_a_status_that_is_not_ready_reports_nothing(self):
        self.assertIsNone(
            MODULE.extract_stage_outcome(
                {"result": "no_state", "stage_outcome": "no_progress"}
            )
        )

    def test_a_word_outside_the_vocabulary_reports_nothing(self):
        self.assertIsNone(
            MODULE.extract_stage_outcome(self.payload(stage_outcome="green"))
        )

    def test_a_stage_that_does_not_report_one_reports_nothing(self):
        self.assertIsNone(MODULE.extract_stage_outcome(self.payload()))
        self.assertIsNone(MODULE.extract_stage_outcome(None))
        self.assertIsNone(MODULE.extract_stage_outcome("ready"))

    def test_a_github_backed_stage_still_gets_its_outcome_read(self):
        status = {
            "installed": True,
            "script": "s.py",
            "state": "x.json",
            "ok": True,
            "payload": {"result": "ready", "stage_outcome": "skipped"},
        }
        with mock.patch.object(MODULE, "run_stage_status", return_value=status):
            reading = MODULE.read_stage_outcome(
                MODULE.STAGE_BY_NAME[MODULE.STAGE_CI],
                MODULE.build_target("owner", "repo", 7),
            )
        self.assertTrue(reading["available"])
        self.assertEqual("skipped", reading["outcome"])
        self.assertEqual("stage_status", reading["source"])

    def test_a_stage_that_reports_nothing_is_not_available(self):
        status = {
            "installed": True,
            "script": "s.py",
            "state": "x.json",
            "ok": True,
            "payload": {"result": "ready"},
        }
        with mock.patch.object(MODULE, "run_stage_status", return_value=status):
            reading = MODULE.read_stage_outcome(
                MODULE.STAGE_BY_NAME[MODULE.STAGE_DESCRIPTION],
                MODULE.build_target("owner", "repo", 7),
            )
        self.assertFalse(reading["available"])
        self.assertEqual("not_reported", reading["reason"])

    def test_a_helper_that_cannot_run_is_not_available(self):
        status = {
            "installed": False,
            "script": "s.py",
            "state": "x.json",
            "ok": False,
            "reason": "helper_missing",
        }
        with mock.patch.object(MODULE, "run_stage_status", return_value=status):
            reading = MODULE.read_stage_outcome(
                MODULE.STAGE_BY_NAME[MODULE.STAGE_CONFLICT],
                MODULE.build_target("owner", "repo", 7),
            )
        self.assertFalse(reading["available"])
        self.assertEqual("helper_missing", reading["reason"])

    def test_a_github_backed_stage_actually_runs_its_helper_status(self):
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_CI]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = (
                root
                / "installed-plugins"
                / "trask-plugins"
                / entry["plugin"]
                / "scripts"
                / f"{entry['module']}.py"
            )
            script.parent.mkdir(parents=True)
            script.write_text("", encoding="utf-8")
            state = root / "state.json"
            state.write_text("{}", encoding="utf-8")
            payload = SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"result": "ready", "stage_outcome": "cleared"}),
                stderr="",
            )
            with mock.patch.object(MODULE, "copilot_home", return_value=root):
                with mock.patch.object(MODULE, "stage_state_path", return_value=state):
                    with mock.patch.object(
                        MODULE, "run", return_value=payload
                    ) as runner:
                        reading = MODULE.read_stage_outcome(
                            entry, MODULE.build_target("owner", "repo", 7)
                        )
        self.assertTrue(reading["available"])
        self.assertEqual("cleared", reading["outcome"])
        command = runner.call_args.args[0]
        self.assertIn("status", command)
        self.assertIn(str(state), command)

    def test_an_outcome_never_makes_a_github_backed_stage_green(self):
        state = build_state()
        observed = observation(mergeable="CONFLICTING")
        observed["stage_markers"][MODULE.STAGE_CONFLICT]["stage_outcome"] = "cleared"
        decision = MODULE.decide_next(state, observed)
        self.assertEqual("run_stage", decision["result"])
        self.assertEqual(MODULE.STAGE_CONFLICT, decision["stage"])
        self.assertFalse(decision["stage_states"][MODULE.STAGE_CONFLICT]["green"])

    def test_a_reported_outcome_does_not_reach_the_greenness_decision(self):
        self.assertNotIn("stage_outcome", MODULE.stage_green.__doc__ or "")
        source = SCRIPT.read_text(encoding="utf-8")
        body = source.split("def stage_green(")[1].split("\ndef ")[0]
        self.assertNotIn("stage_outcome", body)


class ResolveFinishOutcomeTest(unittest.TestCase):
    def resolve(self, requested: str, reading: dict) -> dict:
        with mock.patch.object(MODULE, "read_stage_outcome", return_value=reading):
            return MODULE.resolve_finish_outcome(
                MODULE.STAGE_BY_NAME[MODULE.STAGE_CI],
                MODULE.build_target("owner", "repo", 7),
                requested,
            )

    def test_the_stage_answer_beats_the_reported_one(self):
        resolution = self.resolve(
            "cleared", {"available": True, "outcome": "no_progress"}
        )
        self.assertEqual("no_progress", resolution["outcome"])
        self.assertEqual("cleared", resolution["requested_outcome"])
        self.assertEqual("stage_status", resolution["outcome_source"])

    def test_an_escalation_the_stage_reports_beats_a_clearance(self):
        resolution = self.resolve(
            "cleared", {"available": True, "outcome": "escalated"}
        )
        self.assertEqual("escalated", resolution["outcome"])

    def test_a_stage_that_says_nothing_keeps_the_reported_outcome(self):
        resolution = self.resolve(
            "skipped", {"available": False, "reason": "not_reported"}
        )
        self.assertEqual("skipped", resolution["outcome"])
        self.assertEqual("reported", resolution["outcome_source"])
        self.assertEqual("not_reported", resolution["outcome_reason"])

    def test_a_helper_that_cannot_run_keeps_the_reported_outcome(self):
        resolution = self.resolve(
            "escalated", {"available": False, "reason": "status_failed"}
        )
        self.assertEqual("escalated", resolution["outcome"])
        self.assertEqual("reported", resolution["outcome_source"])


class ModelGateTest(unittest.TestCase):
    def test_pinned_claude_models_satisfy_every_stage(self):
        models = {stage: "claude-sonnet-4.6" for stage in MODULE.STAGE_NAMES}
        gate = MODULE.gate_stage_models(models, can_pin=True)
        self.assertEqual("ready", gate["result"])
        self.assertEqual([], gate["blocked"])

    def test_a_gpt_model_blocks_the_self_review_stage(self):
        models = {stage: "claude-sonnet-4.6" for stage in MODULE.STAGE_NAMES}
        models[MODULE.STAGE_SELF_REVIEW] = "gpt-5.6-sol"
        gate = MODULE.gate_stage_models(models, can_pin=True)
        self.assertEqual("blocked", gate["result"])
        self.assertEqual([MODULE.STAGE_SELF_REVIEW], gate["blocked"])

    def test_other_stages_accept_any_model(self):
        models = {stage: "gpt-5.6-sol" for stage in MODULE.STAGE_NAMES}
        models[MODULE.STAGE_SELF_REVIEW] = "claude-opus-4.8"
        gate = MODULE.gate_stage_models(models, can_pin=True)
        self.assertEqual("ready", gate["result"])

    def test_the_gate_reports_whether_the_launcher_can_pin(self):
        models = {stage: "claude-sonnet-4.6" for stage in MODULE.STAGE_NAMES}
        gate = MODULE.gate_stage_models(models, can_pin=False)
        self.assertFalse(gate["can_pin"])
        for entry in gate["stages"]:
            self.assertFalse(entry["pinned"])

    def test_model_families(self):
        self.assertEqual("claude", MODULE.model_family("claude-opus-4.8"))
        self.assertEqual("gpt", MODULE.model_family("gpt-5.6-sol"))
        self.assertEqual("gemini", MODULE.model_family("gemini-3.1-pro-preview"))
        self.assertEqual("grok", MODULE.model_family("grok-4.6"))
        self.assertEqual("other", MODULE.model_family("mystery-1"))
        self.assertEqual("other", MODULE.model_family(""))

    def test_stage_models_fall_back_to_the_default(self):
        models = MODULE.stage_models(build_state(stage_models={}))
        self.assertEqual(MODULE.default_stage_models(), models)

    def test_a_stage_without_its_own_model_uses_the_uniform_default(self):
        for entry in MODULE.STAGES:
            if entry.get("model"):
                continue
            with self.subTest(stage=entry["stage"]):
                self.assertEqual(
                    MODULE.DEFAULT_STAGE_MODEL, MODULE.stage_default_model(entry)
                )

    def test_a_stage_may_carry_its_own_default_model(self):
        entry = {**MODULE.STAGE_BY_NAME[MODULE.STAGE_CONFLICT], "model": "gpt-5.6-sol"}
        self.assertEqual("gpt-5.6-sol", MODULE.stage_default_model(entry))

    def test_a_blank_per_stage_model_falls_back_to_the_default(self):
        entry = {**MODULE.STAGE_BY_NAME[MODULE.STAGE_CONFLICT], "model": "   "}
        self.assertEqual(MODULE.DEFAULT_STAGE_MODEL, MODULE.stage_default_model(entry))

    def test_a_per_stage_default_seeds_the_model_map(self):
        stage = MODULE.STAGE_BY_NAME[MODULE.STAGE_CONFLICT]
        patched = tuple(
            {**entry, "model": "gpt-5.6-sol"} if entry is stage else entry
            for entry in MODULE.STAGES
        )
        with mock.patch.object(MODULE, "STAGES", patched):
            models = MODULE.stage_models(build_state(stage_models={}))
        self.assertEqual("gpt-5.6-sol", models[MODULE.STAGE_CONFLICT])
        self.assertEqual(
            MODULE.DEFAULT_STAGE_MODEL, models[MODULE.STAGE_COPILOT_REVIEW]
        )

    def test_an_explicit_override_beats_a_per_stage_default(self):
        stage = MODULE.STAGE_BY_NAME[MODULE.STAGE_CONFLICT]
        patched = tuple(
            {**entry, "model": "gpt-5.6-sol"} if entry is stage else entry
            for entry in MODULE.STAGES
        )
        with mock.patch.object(MODULE, "STAGES", patched):
            models = MODULE.stage_models(
                build_state(stage_models={MODULE.STAGE_CONFLICT: "claude-opus-4.8"})
            )
        self.assertEqual("claude-opus-4.8", models[MODULE.STAGE_CONFLICT])

    def test_the_family_gate_still_judges_a_per_stage_default(self):
        stage = MODULE.STAGE_BY_NAME[MODULE.STAGE_SELF_REVIEW]
        patched = tuple(
            {**entry, "model": "gpt-5.6-sol"} if entry is stage else entry
            for entry in MODULE.STAGES
        )
        with mock.patch.object(MODULE, "STAGES", patched):
            gate = MODULE.gate_stage_models(
                MODULE.default_stage_models(), can_pin=True
            )
        self.assertEqual("blocked", gate["result"])
        self.assertEqual([MODULE.STAGE_SELF_REVIEW], gate["blocked"])

    def test_no_stage_ships_a_model_its_family_gate_would_block(self):
        gate = MODULE.gate_stage_models(MODULE.default_stage_models(), can_pin=True)
        self.assertEqual("ready", gate["result"])

    def test_an_unknown_stage_model_override_is_ignored(self):
        models = MODULE.stage_models(
            build_state(stage_models={"nonsense": "gpt-5.6-sol"})
        )
        self.assertNotIn("nonsense", models)


class LaunchPlanTest(unittest.TestCase):
    def test_the_plan_names_the_agent_plugin_qualified(self):
        plan = MODULE.launch_plan(build_state(), MODULE.STAGE_SELF_REVIEW)
        self.assertEqual("self-review-loop:self-review-loop", plan["agent"])
        self.assertIn("--agent", plan["command"])
        self.assertEqual(
            "self-review-loop:self-review-loop",
            plan["command"][plan["command"].index("--agent") + 1],
        )

    def test_the_plan_targets_the_pull_request_unambiguously(self):
        plan = MODULE.launch_plan(build_state(), MODULE.STAGE_CI)
        self.assertEqual("owner/repo#7", plan["target"])
        self.assertEqual(
            ["copilot", "-p", "owner/repo#7", "--agent", "ci-fix-loop:ci-fix-loop"],
            plan["command"][:5],
        )

    def test_the_plan_pins_the_model_and_the_effort(self):
        plan = MODULE.launch_plan(build_state(), MODULE.STAGE_CONFLICT)
        self.assertEqual(MODULE.DEFAULT_STAGE_MODEL, plan["model"])
        self.assertEqual("high", plan["effort"])
        self.assertEqual(
            MODULE.DEFAULT_STAGE_MODEL,
            plan["command"][plan["command"].index("--model") + 1],
        )
        self.assertEqual("high", plan["command"][plan["command"].index("--effort") + 1])

    def test_a_per_stage_model_override_reaches_the_plan(self):
        state = build_state(stage_models={MODULE.STAGE_CI: "claude-opus-4.8"})
        plan = MODULE.launch_plan(state, MODULE.STAGE_CI)
        self.assertEqual("claude-opus-4.8", plan["model"])

    def test_the_plan_names_the_session_after_the_pull_request(self):
        plan = MODULE.launch_plan(build_state(), MODULE.STAGE_DESCRIPTION)
        self.assertEqual(
            "PR Pipeline pr-description: 7 - Add a thing", plan["session_name"]
        )

    def test_every_stage_has_a_plan(self):
        for stage in MODULE.STAGE_NAMES:
            plan = MODULE.launch_plan(build_state(), stage)
            self.assertEqual(stage, plan["stage"])
            self.assertTrue(plan["agent"].startswith(f"{plan['plugin']}:"))


class CommandTestCase(unittest.TestCase):
    def setUp(self):
        self.emitted: list[dict] = []
        patcher = mock.patch.object(MODULE, "emit", self.emitted.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        # No test may shell out to a real stage helper. A test that cares about
        # the stage's own answer patches this with stage_says.
        self.stage_reading = {
            "available": False,
            "outcome": None,
            "reason": "not_reported",
        }
        reader = mock.patch.object(
            MODULE, "read_stage_outcome", side_effect=lambda *_: self.stage_reading
        )
        reader.start()
        self.addCleanup(reader.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def stage_says(self, outcome: str) -> None:
        self.stage_reading = {"available": True, "outcome": outcome}

    def args(self, **values) -> SimpleNamespace:
        return SimpleNamespace(**values)


class StartCommandTest(CommandTestCase):
    def test_starting_the_first_stage_records_the_run(self):
        path = write_state(self.root)
        MODULE.command_start(
            self.args(
                state=str(path),
                stage=MODULE.STAGE_CONFLICT,
                head=HEAD,
                launch="session",
                session="abc",
                process=None,
            )
        )
        state = MODULE.load_state(path)
        self.assertEqual("started", self.emitted[-1]["result"])
        self.assertEqual(MODULE.STAGE_CONFLICT, state["running"]["stage"])
        self.assertEqual("abc", state["running"]["session_id"])
        self.assertEqual("session", state["running"]["launch"])
        self.assertEqual(1, state["iteration"])
        self.assertEqual(0, state["stage_high_water"])

    def test_starting_a_second_stage_while_one_runs_is_refused(self):
        path = write_state(
            self.root, running={"stage": MODULE.STAGE_CONFLICT, "head_sha": HEAD}
        )
        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.command_start(
                self.args(
                    state=str(path),
                    stage=MODULE.STAGE_SELF_REVIEW,
                    head=HEAD,
                    launch="session",
                    session=None,
                    process=None,
                )
            )
        self.assertIn("already recorded as running", str(error.exception))

    def test_starting_after_an_escalation_is_refused(self):
        path = write_state(self.root, escalation={"stage": MODULE.STAGE_CI})
        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.command_start(
                self.args(
                    state=str(path),
                    stage=MODULE.STAGE_CI,
                    head=HEAD,
                    launch="session",
                    session=None,
                    process=None,
                )
            )
        self.assertIn("already escalated", str(error.exception))

    def test_stepping_back_counts_an_iteration(self):
        path = write_state(
            self.root,
            iteration=1,
            stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_CI],
        )
        MODULE.command_start(
            self.args(
                state=str(path),
                stage=MODULE.STAGE_SELF_REVIEW,
                head=NEXT_HEAD,
                launch="subprocess",
                session=None,
                process="4242",
            )
        )
        state = MODULE.load_state(path)
        self.assertEqual(2, state["iteration"])
        self.assertTrue(self.emitted[-1]["loop_back"])
        self.assertEqual(
            MODULE.STAGE_INDEX[MODULE.STAGE_SELF_REVIEW], state["stage_high_water"]
        )
        self.assertEqual("4242", state["running"]["process_id"])

    def test_a_start_past_the_cap_escalates_instead_of_running(self):
        path = write_state(
            self.root,
            iteration=2,
            max_iterations=2,
            stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_DESCRIPTION],
        )
        MODULE.command_start(
            self.args(
                state=str(path),
                stage=MODULE.STAGE_CONFLICT,
                head=NEXT_HEAD,
                launch="session",
                session=None,
                process=None,
            )
        )
        state = MODULE.load_state(path)
        self.assertEqual("escalated", self.emitted[-1]["result"])
        self.assertEqual("max_iterations_reached", state["escalation"]["reason"])
        self.assertIsNone(state["running"])

    def test_an_unknown_stage_is_refused(self):
        path = write_state(self.root)
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.command_start(
                self.args(
                    state=str(path),
                    stage="nonsense",
                    head=HEAD,
                    launch="session",
                    session=None,
                    process=None,
                )
            )


class FinishCommandTest(CommandTestCase):
    def running_state(self, stage: str, **overrides) -> Path:
        running = {
            "stage": stage,
            "head_sha": HEAD,
            "iteration": 1,
            "launch": "session",
            "session_id": "abc",
            "process_id": None,
            "model": MODULE.DEFAULT_STAGE_MODEL,
            "started_at": "2026-01-01T00:00:00Z",
        }
        return write_state(self.root, running=running, **overrides)

    def finish(self, path: Path, stage: str, outcome: str, head: str | None = None):
        MODULE.command_finish(
            self.args(
                state=str(path),
                stage=stage,
                outcome=outcome,
                head=head,
                session=None,
                process=None,
                detail=None,
            )
        )

    def test_a_cleared_stage_records_its_head_and_frees_the_session(self):
        path = self.running_state(MODULE.STAGE_SELF_REVIEW)
        self.finish(path, MODULE.STAGE_SELF_REVIEW, "cleared", NEXT_HEAD)
        state = MODULE.load_state(path)
        self.assertEqual(NEXT_HEAD, state["cleared"][MODULE.STAGE_SELF_REVIEW])
        self.assertIsNone(state["running"])
        self.assertEqual(1, len(state["history"]))
        self.assertFalse(self.emitted[-1]["keep_session"])

    def test_a_skipped_stage_also_clears(self):
        path = self.running_state(MODULE.STAGE_CI)
        self.finish(path, MODULE.STAGE_CI, "skipped")
        state = MODULE.load_state(path)
        self.assertEqual(HEAD, state["cleared"][MODULE.STAGE_CI])
        self.assertTrue(self.emitted[-1]["keep_session"])

    def test_the_history_entry_holds_what_a_dashboard_needs(self):
        path = self.running_state(MODULE.STAGE_CI)
        MODULE.command_finish(
            self.args(
                state=str(path),
                stage=MODULE.STAGE_CI,
                outcome="cleared",
                head=NEXT_HEAD,
                session="xyz",
                process=None,
                detail="fixed the build",
            )
        )
        entry = MODULE.load_state(path)["history"][0]
        self.assertEqual(MODULE.STAGE_CI, entry["stage"])
        self.assertEqual("cleared", entry["outcome"])
        self.assertEqual(1, entry["iteration"])
        self.assertEqual(HEAD, entry["started_head_sha"])
        self.assertEqual(NEXT_HEAD, entry["head_sha"])
        self.assertEqual("xyz", entry["session_id"])
        self.assertEqual("session", entry["launch"])
        self.assertEqual(MODULE.DEFAULT_STAGE_MODEL, entry["model"])
        self.assertEqual("fixed the build", entry["detail"])
        self.assertEqual("2026-01-01T00:00:00Z", entry["started_at"])
        self.assertTrue(entry["ended_at"].endswith("Z"))

    def test_an_escalating_stage_stops_the_pipeline(self):
        path = self.running_state(MODULE.STAGE_COPILOT_REVIEW)
        MODULE.command_finish(
            self.args(
                state=str(path),
                stage=MODULE.STAGE_COPILOT_REVIEW,
                outcome="escalated",
                head=HEAD,
                session=None,
                process=None,
                detail="hit its own cap of 5",
            )
        )
        state = MODULE.load_state(path)
        self.assertEqual("stage_escalated", state["escalation"]["reason"])
        self.assertEqual("hit its own cap of 5", state["escalation"]["detail"])
        self.assertNotIn(MODULE.STAGE_COPILOT_REVIEW, state["cleared"])
        self.assertTrue(self.emitted[-1]["keep_session"])

    def test_one_stalled_run_does_not_escalate(self):
        path = self.running_state(MODULE.STAGE_SELF_REVIEW)
        self.finish(path, MODULE.STAGE_SELF_REVIEW, "no_progress")
        state = MODULE.load_state(path)
        self.assertIsNone(state["escalation"])
        self.assertEqual(1, state["no_progress"][MODULE.STAGE_SELF_REVIEW]["count"])

    def test_two_stalled_runs_in_a_row_escalate(self):
        path = self.running_state(
            MODULE.STAGE_SELF_REVIEW,
            no_progress={MODULE.STAGE_SELF_REVIEW: {"count": 1, "head_sha": HEAD}},
        )
        self.finish(path, MODULE.STAGE_SELF_REVIEW, "no_progress")
        state = MODULE.load_state(path)
        self.assertEqual("no_progress", state["escalation"]["reason"])
        self.assertEqual(2, state["no_progress"][MODULE.STAGE_SELF_REVIEW]["count"])

    def test_clearing_resets_the_stalled_streak(self):
        path = self.running_state(
            MODULE.STAGE_SELF_REVIEW,
            no_progress={MODULE.STAGE_SELF_REVIEW: {"count": 1, "head_sha": HEAD}},
        )
        self.finish(path, MODULE.STAGE_SELF_REVIEW, "cleared")
        state = MODULE.load_state(path)
        self.assertNotIn(MODULE.STAGE_SELF_REVIEW, state["no_progress"])

    def test_finishing_a_stage_that_never_started_is_refused(self):
        path = write_state(self.root)
        with self.assertRaises(MODULE.WorkflowError) as error:
            self.finish(path, MODULE.STAGE_CI, "cleared")
        self.assertIn("not recorded as running", str(error.exception))

    def test_a_stage_that_reports_its_own_outcome_overrides_the_prose(self):
        path = self.running_state(MODULE.STAGE_CI)
        self.stage_says("no_progress")
        self.finish(path, MODULE.STAGE_CI, "cleared", NEXT_HEAD)
        state = MODULE.load_state(path)
        self.assertNotIn(MODULE.STAGE_CI, state["cleared"])
        self.assertEqual(1, state["no_progress"][MODULE.STAGE_CI]["count"])
        entry = state["history"][0]
        self.assertEqual("no_progress", entry["outcome"])
        self.assertEqual("cleared", entry["requested_outcome"])
        self.assertEqual("stage_status", entry["outcome_source"])
        self.assertEqual("no_progress", self.emitted[-1]["outcome"])

    def test_a_stage_that_reports_an_escalation_stops_the_pipeline(self):
        path = self.running_state(MODULE.STAGE_CI)
        self.stage_says("escalated")
        self.finish(path, MODULE.STAGE_CI, "cleared", NEXT_HEAD)
        state = MODULE.load_state(path)
        self.assertEqual("stage_escalated", state["escalation"]["reason"])
        self.assertTrue(self.emitted[-1]["keep_session"])

    def test_a_stage_that_reports_nothing_keeps_the_reported_outcome(self):
        path = self.running_state(MODULE.STAGE_SELF_REVIEW)
        self.finish(path, MODULE.STAGE_SELF_REVIEW, "cleared", NEXT_HEAD)
        state = MODULE.load_state(path)
        self.assertEqual(NEXT_HEAD, state["cleared"][MODULE.STAGE_SELF_REVIEW])
        entry = state["history"][0]
        self.assertEqual("cleared", entry["outcome"])
        self.assertEqual("reported", entry["outcome_source"])

    def test_the_stage_is_asked_about_its_own_run(self):
        path = self.running_state(MODULE.STAGE_CI)
        with mock.patch.object(
            MODULE, "read_stage_outcome", return_value={"available": False}
        ) as reader:
            self.finish(path, MODULE.STAGE_CI, "cleared", NEXT_HEAD)
        entry = reader.call_args.args[0]
        self.assertEqual(MODULE.STAGE_CI, entry["stage"])

    def test_finishing_the_wrong_stage_is_refused(self):
        path = self.running_state(MODULE.STAGE_CI)
        with self.assertRaises(MODULE.WorkflowError):
            self.finish(path, MODULE.STAGE_SELF_REVIEW, "cleared")


class OutcomeCommandTest(CommandTestCase):
    def call_outcome(self, stage: str, reading: dict):
        path = write_state(self.root)
        with mock.patch.object(MODULE, "read_stage_outcome", return_value=reading):
            MODULE.command_outcome(self.args(state=str(path), stage=stage))
        return self.emitted[-1]

    def test_a_reported_outcome_is_authoritative(self):
        payload = self.call_outcome(
            MODULE.STAGE_CI, {"available": True, "outcome": "escalated"}
        )
        self.assertEqual("ready", payload["result"])
        self.assertEqual("escalated", payload["outcome"])
        self.assertTrue(payload["authoritative"])

    def test_a_stage_that_says_nothing_sends_the_caller_to_the_report(self):
        payload = self.call_outcome(
            MODULE.STAGE_SELF_REVIEW, {"available": False, "reason": "not_reported"}
        )
        self.assertEqual("not_reported", payload["result"])
        self.assertFalse(payload["authoritative"])
        self.assertIsNone(payload["outcome"])
        self.assertIn("report", payload["next_action"].lower())

    def test_an_unknown_stage_is_refused(self):
        path = write_state(self.root)
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.command_outcome(self.args(state=str(path), stage="nope"))


class NextCommandTest(CommandTestCase):
    def call_next(self, path: Path, observed: dict):
        with mock.patch.object(MODULE, "require_tools"):
            with mock.patch.object(
                MODULE, "collect_observation", return_value=observed
            ) as collector:
                MODULE.command_next(self.args(state=str(path), effort="high"))
        self.collector = collector

    def test_the_head_it_last_saw_is_carried_into_the_next_look(self):
        path = write_state(self.root, observed_head_sha=HEAD)
        self.call_next(path, observation(head_sha=NEXT_HEAD))
        self.assertEqual(HEAD, self.collector.call_args.kwargs["known_head_sha"])

    def test_the_head_it_just_saw_is_remembered_for_the_next_look(self):
        path = write_state(self.root)
        self.call_next(path, observation(head_sha=NEXT_HEAD))
        self.assertEqual(NEXT_HEAD, MODULE.load_state(path)["observed_head_sha"])

    def test_a_runnable_stage_returns_a_launch_plan(self):
        path = write_state(self.root)
        self.call_next(path, observation())
        payload = self.emitted[-1]
        self.assertEqual("run_stage", payload["result"])
        self.assertEqual(MODULE.STAGE_SELF_REVIEW, payload["stage"])
        self.assertEqual(
            "self-review-loop:self-review-loop", payload["plan"]["agent"]
        )
        self.assertEqual(HEAD, payload["head_sha"])

    def test_live_evidence_is_written_into_the_cleared_map(self):
        path = write_state(self.root)
        self.call_next(path, observation())
        state = MODULE.load_state(path)
        self.assertEqual(HEAD, state["cleared"][MODULE.STAGE_CONFLICT])
        self.assertEqual(HEAD, state["cleared"][MODULE.STAGE_CI])
        self.assertNotIn(MODULE.STAGE_SELF_REVIEW, state["cleared"])

    def test_completion_is_recorded_and_says_the_draft_stays(self):
        path = write_state(self.root)
        self.call_next(path, all_green())
        payload = self.emitted[-1]
        state = MODULE.load_state(path)
        self.assertEqual("complete", payload["result"])
        self.assertEqual(HEAD, state["completed"]["head_sha"])
        self.assertIn("never marks a pull request ready for review", payload["reminder"])

    def test_an_escalation_is_recorded_once(self):
        path = write_state(self.root)
        self.call_next(path, observation(state="MERGED"))
        state = MODULE.load_state(path)
        self.assertEqual("pr_not_open", state["escalation"]["reason"])
        self.assertIsNotNone(state["escalation"]["next_action"])

    def test_an_already_recorded_escalation_keeps_its_original_detail(self):
        path = write_state(
            self.root,
            escalation={
                "stage": MODULE.STAGE_CI,
                "reason": "stage_escalated",
                "detail": "the original reason",
                "next_action": "read the session",
                "head_sha": HEAD,
                "at": "2026-01-01T00:00:00Z",
            },
        )
        self.call_next(path, all_green())
        state = MODULE.load_state(path)
        self.assertEqual("the original reason", state["escalation"]["detail"])
        self.assertEqual("2026-01-01T00:00:00Z", state["escalation"]["at"])

    def test_a_moved_head_sends_the_pipeline_back_to_self_review(self):
        path = write_state(
            self.root,
            stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_DESCRIPTION],
            cleared={
                MODULE.STAGE_CONFLICT: HEAD,
                MODULE.STAGE_SELF_REVIEW: HEAD,
                MODULE.STAGE_COPILOT_REVIEW: HEAD,
                MODULE.STAGE_CI: HEAD,
            },
        )
        self.call_next(path, observation(head_sha=NEXT_HEAD))
        payload = self.emitted[-1]
        self.assertEqual(MODULE.STAGE_SELF_REVIEW, payload["stage"])
        self.assertEqual(2, payload["iteration"])
        self.assertTrue(payload["loop_back"])

    def test_next_does_not_charge_an_iteration_by_itself(self):
        path = write_state(
            self.root,
            stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_DESCRIPTION],
        )
        self.call_next(path, observation(head_sha=NEXT_HEAD))
        self.call_next(path, observation(head_sha=NEXT_HEAD))
        self.assertEqual(1, MODULE.load_state(path)["iteration"])
        self.assertEqual(2, self.emitted[-1]["iteration"])


class EscalateCommandTest(CommandTestCase):
    def test_a_manual_escalation_records_the_reason_and_the_next_action(self):
        path = write_state(self.root)
        MODULE.command_escalate(
            self.args(
                state=str(path),
                stage=MODULE.STAGE_CI,
                reason="flake",
                detail="the same test failed again after one re-run",
                next_action="Look at the test yourself.",
                head=HEAD,
            )
        )
        state = MODULE.load_state(path)
        self.assertEqual("flake", state["escalation"]["reason"])
        self.assertEqual("Look at the test yourself.", state["escalation"]["next_action"])
        self.assertIsNone(state["running"])

    def test_a_known_reason_supplies_its_own_next_action(self):
        path = write_state(self.root)
        MODULE.command_escalate(
            self.args(
                state=str(path),
                stage=None,
                reason="max_iterations_reached",
                detail="two full passes did not converge",
                next_action=None,
                head=HEAD,
            )
        )
        state = MODULE.load_state(path)
        self.assertEqual(
            MODULE.ESCALATION_ACTIONS["max_iterations_reached"],
            state["escalation"]["next_action"],
        )

    def test_an_unknown_stage_is_refused(self):
        path = write_state(self.root)
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.command_escalate(
                self.args(
                    state=str(path),
                    stage="nonsense",
                    reason="flake",
                    detail="detail",
                    next_action=None,
                    head=HEAD,
                )
            )


class StatusCommandTest(CommandTestCase):
    def test_status_writes_the_full_snapshot_and_prints_a_summary(self):
        path = write_state(
            self.root,
            cleared={MODULE.STAGE_CONFLICT: HEAD},
            history=[
                {"stage": MODULE.STAGE_CONFLICT, "outcome": "cleared"},
                {"stage": MODULE.STAGE_SELF_REVIEW, "outcome": "no_progress"},
            ],
        )
        MODULE.command_status(self.args(state=str(path), current=False, repo_root=None))
        payload = self.emitted[-1]
        self.assertEqual("ready", payload["result"])
        self.assertEqual(2, payload["counts"]["history"])
        self.assertEqual(
            {"cleared": 1, "no_progress": 1}, payload["counts"]["outcomes"]
        )
        snapshot = json.loads(
            Path(payload["status_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(2, len(snapshot["history"]))
        self.assertEqual(
            {stage: MODULE.DEFAULT_STAGE_MODEL for stage in MODULE.STAGE_NAMES},
            snapshot["stage_models"],
        )

    def test_status_on_a_missing_state_file_fails_clearly(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.command_status(
                self.args(
                    state=str(self.root / "missing.json"), current=False, repo_root=None
                )
            )


class CleanupCommandTest(CommandTestCase):
    def test_cleanup_removes_the_state_and_its_snapshot(self):
        path = write_state(self.root)
        MODULE.command_status(self.args(state=str(path), current=False, repo_root=None))
        status_path = MODULE.status_path_for(path)
        self.assertTrue(status_path.is_file())
        MODULE.command_cleanup(self.args(state=str(path), force=False))
        self.assertFalse(path.exists())
        self.assertFalse(status_path.exists())

    def test_cleanup_refuses_while_a_stage_is_running(self):
        path = write_state(self.root, running={"stage": MODULE.STAGE_CI})
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.command_cleanup(self.args(state=str(path), force=False))

    def test_forced_cleanup_removes_a_running_state(self):
        path = write_state(self.root, running={"stage": MODULE.STAGE_CI})
        MODULE.command_cleanup(self.args(state=str(path), force=True))
        self.assertFalse(path.exists())


class PreflightCommandTest(CommandTestCase):
    def call_preflight(self, observed: dict, *, installed=(), **values):
        arguments = {
            "target": "owner/repo#7",
            "repo_root": str(self.root),
            "state": str(self.root / "pipeline.json"),
            "max_iterations": 2,
            "stage_model": None,
            "no_pin": False,
        }
        arguments.update(values)
        home = self.root / "home"
        home.mkdir(exist_ok=True)
        install_stage_script(home, *installed)
        with mock.patch.object(MODULE, "require_tools"):
            with mock.patch.object(
                MODULE, "resolve_repo_root", return_value=self.root
            ):
                with mock.patch.object(
                    MODULE, "collect_observation", return_value=observed
                ):
                    with mock.patch.object(
                        MODULE, "copilot_home", return_value=home
                    ):
                        MODULE.command_preflight(self.args(**arguments))

    def test_a_fresh_run_creates_the_state(self):
        self.call_preflight(observation(), installed=MODULE.STAGE_NAMES)
        payload = self.emitted[-1]
        self.assertEqual("ready", payload["result"])
        self.assertFalse(payload["resumed"])
        self.assertEqual(1, payload["iteration"])
        self.assertEqual(2, payload["max_iterations"])
        self.assertEqual(list(MODULE.STAGE_NAMES), payload["stages"])
        self.assertEqual([], payload["missing_plugins"])
        state = MODULE.load_state(self.root / "pipeline.json")
        self.assertEqual("owner/repo", state["pr"]["repo_name"])

    def test_preflight_names_the_plugins_that_are_not_installed(self):
        self.call_preflight(observation(), installed=[MODULE.STAGE_SELF_REVIEW])
        payload = self.emitted[-1]
        self.assertNotIn(MODULE.STAGE_SELF_REVIEW, payload["missing_plugins"])
        self.assertIn(MODULE.STAGE_CI, payload["missing_plugins"])

    def test_a_plugin_that_is_not_installed_does_not_block_preflight(self):
        self.call_preflight(observation())
        self.assertEqual("ready", self.emitted[-1]["result"])
        self.assertEqual(
            list(MODULE.STAGE_NAMES), self.emitted[-1]["missing_plugins"]
        )

    def test_a_second_run_resumes_the_same_state(self):
        self.call_preflight(observation())
        self.call_preflight(observation())
        self.assertTrue(self.emitted[-1]["resumed"])

    def test_a_fresh_run_remembers_the_head_it_saw(self):
        self.call_preflight(observation(head_sha=NEXT_HEAD))
        state = MODULE.load_state(self.root / "pipeline.json")
        self.assertEqual(NEXT_HEAD, state["observed_head_sha"])

    def test_a_resumed_run_carries_the_head_it_last_saw_into_the_look(self):
        self.call_preflight(observation())
        with mock.patch.object(MODULE, "require_tools"):
            with mock.patch.object(
                MODULE, "resolve_repo_root", return_value=self.root
            ):
                with mock.patch.object(
                    MODULE, "collect_observation", return_value=observation()
                ) as collector:
                    MODULE.command_preflight(
                        self.args(
                            target="owner/repo#7",
                            repo_root=str(self.root),
                            state=str(self.root / "pipeline.json"),
                            max_iterations=2,
                            stage_model=None,
                            no_pin=False,
                        )
                    )
        self.assertEqual(HEAD, collector.call_args.kwargs["known_head_sha"])

    def test_a_closed_pull_request_is_refused(self):
        with self.assertRaises(MODULE.WorkflowError) as error:
            self.call_preflight(observation(state="CLOSED"))
        self.assertIn("only drives an open pull request", str(error.exception))

    def test_a_state_file_from_another_pull_request_is_refused(self):
        self.call_preflight(observation())
        other = observation()
        other["pr"] = {**base_pr(), "number": 9}
        other["pr"]["pr_url"] = "https://github.com/owner/repo/pull/9"
        with self.assertRaises(MODULE.WorkflowError) as error:
            self.call_preflight(other, target="owner/repo#9")
        self.assertIn("belongs to", str(error.exception))

    def test_a_stage_model_override_is_stored(self):
        self.call_preflight(
            observation(), stage_model=[f"{MODULE.STAGE_CI}=claude-opus-4.8"]
        )
        state = MODULE.load_state(self.root / "pipeline.json")
        self.assertEqual("claude-opus-4.8", state["stage_models"][MODULE.STAGE_CI])

    def test_a_stage_model_override_that_breaks_a_gate_blocks_the_run(self):
        self.call_preflight(
            observation(), stage_model=[f"{MODULE.STAGE_SELF_REVIEW}=gpt-5.6-sol"]
        )
        payload = self.emitted[-1]
        self.assertEqual("blocked", payload["result"])
        self.assertEqual(
            [MODULE.STAGE_SELF_REVIEW], payload["model_gate"]["blocked"]
        )

    def test_a_malformed_stage_model_override_is_refused(self):
        with self.assertRaises(MODULE.WorkflowError):
            self.call_preflight(observation(), stage_model=["nonsense"])


class ModelsCommandTest(CommandTestCase):
    def test_models_reports_the_pipeline_family(self):
        MODULE.command_models(
            self.args(state=None, pipeline_model="claude-sonnet-4.6", no_pin=False)
        )
        payload = self.emitted[-1]
        self.assertEqual("ready", payload["result"])
        self.assertEqual("claude", payload["pipeline_model_family"])

    def test_models_reads_the_stored_pins(self):
        path = write_state(
            self.root, stage_models={MODULE.STAGE_SELF_REVIEW: "gpt-5.6-sol"}
        )
        MODULE.command_models(
            self.args(state=str(path), pipeline_model=None, no_pin=False)
        )
        payload = self.emitted[-1]
        self.assertEqual("blocked", payload["result"])
        self.assertIn("next_action", payload)


class PlanCommandTest(CommandTestCase):
    def test_plan_prints_one_stage(self):
        path = write_state(self.root)
        home = install_stage_script(self.root / "home", MODULE.STAGE_CONFLICT)
        with mock.patch.object(MODULE, "copilot_home", return_value=home):
            MODULE.command_plan(
                self.args(state=str(path), stage=MODULE.STAGE_CONFLICT, effort="high")
            )
        payload = self.emitted[-1]
        self.assertEqual("ready", payload["result"])
        self.assertEqual("conflict-fix-loop:conflict-fix-loop", payload["agent"])

    def test_plan_refuses_a_stage_whose_plugin_is_not_installed(self):
        path = write_state(self.root)
        empty = self.root / "empty-home"
        empty.mkdir()
        with mock.patch.object(MODULE, "copilot_home", return_value=empty):
            MODULE.command_plan(
                self.args(state=str(path), stage=MODULE.STAGE_CI, effort="high")
            )
        payload = self.emitted[-1]
        self.assertEqual("not_installed", payload["result"])
        self.assertNotIn("command", payload)
        self.assertEqual(
            MODULE.ESCALATION_ACTIONS["helper_missing"], payload["next_action"]
        )

    def test_plan_refuses_an_unknown_stage(self):
        path = write_state(self.root)
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.command_plan(
                self.args(state=str(path), stage="nonsense", effort="high")
            )


class TargetParsingTest(unittest.TestCase):
    def test_a_pull_request_url(self):
        target = MODULE.parse_target("https://github.com/owner/repo/pull/7")
        self.assertEqual("owner/repo", target["repo_name"])
        self.assertEqual(7, target["number"])

    def test_a_short_reference(self):
        self.assertEqual(7, MODULE.parse_target("owner/repo#7")["number"])

    def test_a_bare_number_needs_repository_context(self):
        self.assertEqual(
            "owner/repo", MODULE.parse_target("7", "owner/repo")["repo_name"]
        )
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.parse_target("#7")

    def test_nonsense_is_refused(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.parse_target("not a pull request")


class StateRoundTripTest(unittest.TestCase):
    def test_state_survives_a_save_and_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.json"
            state = build_state(
                cleared={MODULE.STAGE_CI: HEAD},
                history=[{"stage": MODULE.STAGE_CI, "outcome": "cleared"}],
            )
            MODULE.save_state(path, state)
            loaded = MODULE.load_state(path)
        self.assertEqual(HEAD, loaded["cleared"][MODULE.STAGE_CI])
        self.assertEqual(1, len(loaded["history"]))
        self.assertTrue(loaded["updated_at"].endswith("Z"))

    def test_an_unsupported_version_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.json"
            path.write_text(json.dumps({"version": 99}), encoding="utf-8")
            with self.assertRaises(MODULE.WorkflowError):
                MODULE.load_state(path)

    def test_invalid_json_is_refused_with_the_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.json"
            path.write_text("{oops", encoding="utf-8")
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.load_state(path)
        self.assertIn("invalid JSON", str(error.exception))

    def test_a_missing_state_file_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(MODULE.WorkflowError):
                MODULE.load_state(Path(directory) / "missing.json")


class ParserTest(unittest.TestCase):
    def test_every_documented_subcommand_parses(self):
        parser = MODULE.build_parser()
        for arguments in (
            ["preflight", "owner/repo#7"],
            ["next", "--state", "s.json"],
            [
                "start",
                "--state",
                "s.json",
                "--stage",
                MODULE.STAGE_CI,
                "--head",
                HEAD,
                "--launch",
                "session",
            ],
            [
                "finish",
                "--state",
                "s.json",
                "--stage",
                MODULE.STAGE_CI,
                "--outcome",
                "cleared",
            ],
            ["escalate", "--state", "s.json", "--reason", "flake", "--detail", "d"],
            ["outcome", "--state", "s.json", "--stage", MODULE.STAGE_CI],
            ["models"],
            ["plan", "--state", "s.json", "--stage", MODULE.STAGE_CI],
            ["status", "--state", "s.json"],
            ["cleanup", "--state", "s.json"],
        ):
            with self.subTest(arguments=arguments):
                parsed = parser.parse_args(arguments)
                self.assertTrue(callable(parsed.function))

    def test_status_requires_a_source(self):
        parser = MODULE.build_parser()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["status"])

    def test_an_unknown_stage_is_rejected_by_the_parser(self):
        parser = MODULE.build_parser()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(
                    [
                        "start",
                        "--state",
                        "s.json",
                        "--stage",
                        "nonsense",
                        "--head",
                        HEAD,
                        "--launch",
                        "session",
                    ]
                )

    def test_main_reports_an_error_as_json(self):
        emitted: list[dict] = []
        with mock.patch.object(MODULE, "emit", emitted.append):
            with mock.patch.object(
                MODULE.sys, "argv", ["pr_pipeline.py", "status", "--state", "missing"]
            ):
                self.assertEqual(1, MODULE.main())
        self.assertEqual("error", emitted[-1]["result"])


class HelperNeverPromotesTest(unittest.TestCase):
    def setUp(self):
        self.source = SCRIPT.read_text(encoding="utf-8")

    def test_the_helper_never_marks_a_pull_request_ready(self):
        self.assertNotIn("pr ready", self.source)
        self.assertNotIn("--undo", self.source)

    def test_the_helper_never_approves_or_reviews(self):
        self.assertNotIn("APPROVE", self.source)
        self.assertNotIn("pr review", self.source)
        self.assertNotIn("requested_reviewers", self.source)

    def test_the_helper_never_posts_a_comment(self):
        self.assertNotIn("pr comment", self.source)
        self.assertNotIn("issues/", self.source)


class AgentInstructionsTest(unittest.TestCase):
    def setUp(self):
        self.instructions = AGENT.read_text(encoding="utf-8")

    def test_frontmatter_matches_the_sibling_shape(self):
        self.assertIn("name: PR Pipeline", self.instructions)
        self.assertIn("argument-hint:", self.instructions)
        self.assertIn("user-invocable: true", self.instructions)
        self.assertIn(
            "tools: [read, search, execute, todo, rename_session, create_session, "
            "get_session, archive_session]",
            self.instructions,
        )

    def test_a_model_may_start_it_and_the_file_says_why(self):
        self.assertIn("disable-model-invocation: false", self.instructions)
        self.assertIn("## Why A Model May Start This Agent", self.instructions)
        self.assertIn(
            "hands off to this pipeline without a person in the loop", self.instructions
        )

    def test_it_never_promotes_the_pull_request(self):
        self.assertIn(
            "**Never mark the pull request ready for review, and never touch "
            "approval.**",
            self.instructions,
        )
        self.assertIn("Do not run `gh pr ready`", self.instructions)
        self.assertIn("promoting it out of draft is the user's call", self.instructions)

    def test_it_never_reads_prose_for_a_decision(self):
        self.assertIn(
            "**Never read a stage's prose report to make a decision.**",
            self.instructions,
        )
        self.assertIn(
            "Never let it choose the outcome when `outcome`, `next`, and the live "
            "state say otherwise",
            self.instructions,
        )

    def test_it_asks_the_stage_before_it_reads_the_report(self):
        self.assertIn(
            "Run `outcome --stage <plan stage>` first", self.instructions
        )
        self.assertIn(
            "When its `result` is `not_reported`, the stage does not answer for "
            "itself yet, so fall back to reading",
            self.instructions,
        )
        first = self.instructions.index("Run `outcome --stage <plan stage>` first")
        fallback = self.instructions.index("Run `next` again.")
        self.assertLess(first, fallback)

    def test_it_keeps_an_outcome_out_of_the_greenness_decision(self):
        self.assertIn(
            "A stage saying how its own run ended is not evidence of greenness.",
            self.instructions,
        )

    def test_it_says_an_uncorroborated_answer_leaves_a_stage_not_green(self):
        self.assertIn(
            "an answer it cannot corroborate leaves the stage **not green**",
            self.instructions,
        )
        self.assertIn("Mergeability lags the head.", self.instructions)
        self.assertIn("A check rollup can be incomplete.", self.instructions)

    def test_it_admits_the_guards_are_not_proofs(self):
        self.assertIn(
            "Neither guard is a proof, and neither is written as one.",
            self.instructions,
        )
        self.assertIn(
            "no GitHub field says which commit a mergeability answer was computed "
            "at",
            self.instructions.replace("No GitHub field", "no GitHub field"),
        )

    def test_it_rules_out_the_base_commit_as_a_coverage_reference(self):
        self.assertIn(
            "The previous head of the same pull request is the only sound reference",
            self.instructions,
        )
        self.assertIn(
            "The base branch commit is not one, because the base and the head are "
            "reached by different triggers",
            self.instructions,
        )

    def test_it_says_every_guard_has_a_way_out(self):
        self.assertIn(
            "Every one of these guards has a way out, because a stage held not "
            "green by something that can never clear is a deadlock rather than a "
            "conservative failure.",
            self.instructions,
        )
        self.assertIn("the wait for missing checks is bounded", self.instructions)

    def test_it_names_every_stage_plugin_qualified(self):
        self.assertIn(
            "Never launch a stage agent by a bare basename", self.instructions
        )
        self.assertIn(
            "A bare name silently resolves to the default agent and reports no error",
            self.instructions,
        )

    def test_it_documents_the_stage_order_and_the_stopping_point(self):
        for position, stage in enumerate(MODULE.STAGE_NAMES, start=1):
            self.assertIn(f"{position}. `{stage}`", self.instructions)
        self.assertIn(
            "The pipeline stops when the description stage goes green.",
            self.instructions,
        )

    def test_it_requires_every_stage_plugin_to_be_installed(self):
        self.assertIn(
            "Every stage needs its plugin installed before it can run, whatever "
            "kind of evidence makes it green.",
            self.instructions,
        )
        self.assertIn(
            "A missing plugin whose stage is already green stops nothing",
            self.instructions,
        )
        self.assertIn("The plugin a stage needs is not installed.", self.instructions)

    def test_it_documents_the_per_stage_default_model(self):
        self.assertIn(
            "Each stage carries its own default model", self.instructions
        )
        self.assertIn(
            "A `--stage-model <stage>=<model>` pin at `preflight` beats the "
            "stage's default",
            self.instructions,
        )

    def test_it_documents_both_launch_paths(self):
        self.assertIn("## Choosing The Launch Path", self.instructions)
        self.assertIn(
            "an agent that names `create_session` in its `tools:` allowlist does "
            "receive it",
            self.instructions,
        )
        self.assertIn(
            "The plain Copilot CLI does not provide `create_session`",
            self.instructions,
        )
        self.assertIn("plan.command", self.instructions)

    def test_it_documents_the_model_gate(self):
        self.assertIn("## Model Gate", self.instructions)
        self.assertIn("models --pipeline-model", self.instructions)
        self.assertIn("fixed GPT-5.6 Sol evaluator", self.instructions)

    def test_it_documents_session_hygiene_in_both_directions(self):
        self.assertIn("## Session Hygiene", self.instructions)
        self.assertIn("`keep_session` false", self.instructions)
        self.assertIn(
            "Keep the session otherwise", self.instructions
        )

    def test_it_says_base_movement_triggers_nothing(self):
        self.assertIn("Base-branch movement triggers nothing", self.instructions)

    def test_it_treats_an_internal_cap_as_an_escalation(self):
        self.assertIn(
            "A stage that hits its own internal iteration cap is an escalation, not "
            "a completion.",
            self.instructions,
        )

    def test_it_names_the_state_path(self):
        self.assertIn(
            "~/.copilot/run/pr-pipeline/{owner}--{repo}--{number}.json",
            self.instructions,
        )

    def test_it_documents_every_subcommand_the_helper_offers(self):
        parser = MODULE.build_parser()
        subparsers = [
            action
            for action in parser._actions
            if isinstance(action, MODULE.argparse._SubParsersAction)
        ][0]
        for name in subparsers.choices:
            with self.subTest(command=name):
                self.assertIn(f"`{name} ", self.instructions)

    def test_it_runs_unattended_without_approval_gates(self):
        self.assertIn("Run fully unattended", self.instructions)
        self.assertIn("There is no approval gate between stages", self.instructions)


if __name__ == "__main__":
    unittest.main()
