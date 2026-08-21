import contextlib
import importlib.util
import argparse
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "pr_pipeline.py"
AGENT = Path(__file__).parents[1] / "agents" / "pr-pipeline.agent.md"
SPEC = importlib.util.spec_from_file_location("pr_pipeline", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

REAL_DIAGNOSE_LOCAL_HEAD = MODULE.diagnose_local_head


HEAD = "head1"
NEXT_HEAD = "head2"


def git_in(repo: Path, *arguments: str) -> str:
    """Run one git command in a real repository built for a test."""

    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
        },
    )
    return result.stdout.strip()


def git_succeeds_in(repo: Path, *arguments: str) -> bool:
    """Whether one git command succeeds, used to prove an object still exists."""

    return (
        subprocess.run(
            ["git", "-C", str(repo), *arguments],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def make_pull_request_remote(root: Path) -> dict:
    """A real repository that publishes `refs/pull/<n>/head`, as GitHub does.

    The pipeline fetches the pull request head through that ref, so a test that
    stubs the fetch would never find out whether the ref it asks for exists.
    """

    remote = root / "remote"
    remote.mkdir()
    git_in(remote, "init", "-q", "-b", "main")
    git_in(remote, "commit", "-q", "--allow-empty", "-m", "base")
    base = git_in(remote, "rev-parse", "HEAD")
    git_in(remote, "checkout", "-q", "-b", base_pr()["head_branch"])
    git_in(remote, "commit", "-q", "--allow-empty", "-m", "the pull request")
    pr_head = git_in(remote, "rev-parse", "HEAD")
    git_in(remote, "update-ref", f"refs/pull/{base_pr()['number']}/head", pr_head)
    git_in(remote, "checkout", "-q", "main")
    return {"remote": remote, "base": base, "pr_head": pr_head}


def clone_for_pipeline(root: Path, remote: Path, name: str) -> Path:
    """A worktree the pipeline could run in, holding only the base branch.

    Cloning a single branch matches what a session worktree carries: the pull
    request head is not present until something fetches it.
    """

    local = root / name
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--single-branch",
            "--branch",
            "main",
            str(remote),
            str(local),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    git_in(local, "config", "user.email", "t@e.st")
    git_in(local, "config", "user.name", "test")
    return local


def fetch_pr_head(local: Path) -> None:
    git_in(local, "fetch", "-q", "origin", f"refs/pull/{base_pr()['number']}/head")


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
        "mergeability": MODULE.corroborate_mergeability(mergeable),
        "checks": {
            "state": checks,
            "total": 3,
            "counts": {},
            "failing": [],
            "pending": [],
            "coverage": coverage
            or {
                "state": "satisfied",
                "source": "declared",
                "reason": "required_contexts_present",
                "missing": [],
                "declared": ["build"],
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
        # The pass runs out first: every stage from the high-water mark on is
        # green at the new head, so the only thing left is the clearance the push
        # staled behind it, and going back for it starts the next pass.
        state = build_state(
            iteration=1,
            stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_COPILOT_REVIEW],
            cleared={MODULE.STAGE_SELF_REVIEW: HEAD},
        )
        decision = MODULE.decide_next(
            state,
            observation(
                head_sha=NEXT_HEAD,
                copilot_review=NEXT_HEAD,
                description=NEXT_HEAD,
            ),
        )
        self.assertEqual(MODULE.STAGE_SELF_REVIEW, decision["stage"])
        self.assertEqual(2, decision["iteration"])
        self.assertTrue(decision["loop_back"])

    def test_a_loop_back_past_the_cap_escalates(self):
        state = build_state(
            iteration=2,
            max_iterations=2,
            stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_CI],
        )
        decision = MODULE.decide_next(
            state, observation(head_sha=NEXT_HEAD, description=NEXT_HEAD)
        )
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

    def test_a_push_by_the_check_stage_sends_an_earlier_stage_round_again(self):
        # The check stage clears on GitHub evidence, so its clearance is not
        # pinned to a head and it can be re-picked after saying it cleared.
        # A push still costs it an iteration, because the review stages ahead
        # of it cleared at the old head and stop being green at the new one.
        # It is charged at the end of the pass rather than the moment it pushes.
        state = build_state(
            iteration=1,
            stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_CI],
            cleared={
                MODULE.STAGE_SELF_REVIEW: HEAD,
                MODULE.STAGE_COPILOT_REVIEW: HEAD,
            },
        )
        decision = MODULE.decide_next(
            state, observation(head_sha=NEXT_HEAD, description=NEXT_HEAD)
        )
        self.assertEqual(MODULE.STAGE_SELF_REVIEW, decision["stage"])
        self.assertEqual(2, decision["iteration"])
        self.assertTrue(decision["loop_back"])

    def test_the_first_stage_has_no_earlier_stage_to_charge_an_iteration(self):
        # Nothing sits ahead of the conflict stage, so a push by it invalidates
        # no clearance and the pipeline picks it again at the same iteration.
        # Every later stage is charged for a push; this one is not.
        state = build_state(
            iteration=1,
            stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_CONFLICT],
        )
        decision = MODULE.decide_next(
            state, observation(head_sha=NEXT_HEAD, mergeable="UNKNOWN")
        )
        self.assertEqual(MODULE.STAGE_CONFLICT, decision["stage"])
        self.assertEqual(1, decision["iteration"])
        self.assertFalse(decision["loop_back"])

    def test_a_helper_stage_is_never_both_cleared_and_picked_again(self):
        # A helper stage's clearance is only recorded when its marker names the
        # head, and that same head makes it green. So it cannot reset the
        # no-progress streak with a clearing outcome and still be re-picked,
        # which is what keeps a push by one of them bounded.
        for stage in MODULE.HELPER_EVIDENCE_STAGES:
            with self.subTest(stage=stage):
                state = build_state(
                    iteration=1,
                    stage_high_water=MODULE.STAGE_INDEX[stage],
                    cleared={stage: NEXT_HEAD},
                )
                marker = {
                    MODULE.STAGE_SELF_REVIEW: "self_review",
                    MODULE.STAGE_COPILOT_REVIEW: "copilot_review",
                    MODULE.STAGE_DESCRIPTION: "description",
                }[stage]
                decision = MODULE.decide_next(
                    state, observation(head_sha=NEXT_HEAD, **{marker: NEXT_HEAD})
                )
                self.assertNotEqual(stage, decision.get("stage"))

    def test_no_stage_but_the_first_relies_on_nothing_to_hold_it(self):
        # The brake on relaunching one stage for ever is the no-progress streak:
        # a clearing outcome the pipeline cannot confirm never resets it, so two
        # in a row stop the pipeline whatever the stage and wherever it sits.
        #
        # The ordering is a second, independent defense. A stage that clears on
        # GitHub evidence and pushes a commit invalidates the head-pinned marker
        # of any helper stage ahead of it, so the next pick is earlier, which is
        # a loop-back and charges an iteration. Every GitHub-evidence stage below
        # index 0 has at least one helper ahead of it, and that is what makes the
        # iteration cap bind on it at all.
        #
        # That defense is a property of the order rather than of any stage, so
        # nothing about a stage reveals when a reorder removes it. This asserts
        # the property directly, so the design keeps the two brakes it assumes
        # instead of quietly falling back to one.
        for index, entry in enumerate(MODULE.STAGES):
            if index == 0 or entry["stage"] in MODULE.HELPER_EVIDENCE_STAGES:
                continue
            with self.subTest(stage=entry["stage"]):
                ahead = {
                    earlier["stage"] for earlier in MODULE.STAGES[:index]
                } & set(MODULE.HELPER_EVIDENCE_STAGES)
                self.assertTrue(
                    ahead,
                    f"{entry['stage']} clears on GitHub evidence and has no "
                    "helper stage ahead of it, so a push it makes invalidates "
                    "nothing and it can be re-picked without charging an "
                    "iteration",
                )

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


class FlowForwardTest(unittest.TestCase):
    """A pass runs the order forward once and only loops at its end.

    Reaching behind the high-water mark the moment a later stage pushed made an
    outer iteration cost a backward hop rather than a pass, so `max_iterations`
    of 2 meant one backward jump ever, and the per-stage budgets could never be
    spent.
    """

    def test_a_push_by_a_later_stage_does_not_drag_the_pass_backwards(self):
        state = build_state(
            iteration=1,
            stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_COPILOT_REVIEW],
            cleared={MODULE.STAGE_SELF_REVIEW: HEAD},
        )

        decision = MODULE.decide_next(state, observation(head_sha=NEXT_HEAD))

        self.assertEqual(MODULE.STAGE_COPILOT_REVIEW, decision["stage"])
        self.assertEqual(1, decision["iteration"])
        self.assertFalse(decision["loop_back"])

    def test_the_stage_that_pushed_keeps_running_while_it_is_still_not_green(self):
        state = build_state(
            iteration=1,
            stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_CI],
            cleared={
                MODULE.STAGE_SELF_REVIEW: HEAD,
                MODULE.STAGE_COPILOT_REVIEW: HEAD,
            },
        )

        decision = MODULE.decide_next(
            state, observation(head_sha=NEXT_HEAD, checks="failure")
        )

        self.assertEqual(MODULE.STAGE_CI, decision["stage"])
        self.assertEqual(1, decision["iteration"])
        self.assertFalse(decision["loop_back"])

    def test_a_pass_ends_only_when_every_stage_from_the_floor_on_is_green(self):
        """One stale stage behind the floor is not enough on its own."""
        floor = MODULE.STAGE_INDEX[MODULE.STAGE_CI]
        forward = {"checks": "success", "description": NEXT_HEAD}
        for held_back, expected in (
            ({"checks": "failure"}, MODULE.STAGE_CI),
            ({"description": None}, MODULE.STAGE_DESCRIPTION),
            ({}, MODULE.STAGE_SELF_REVIEW),
        ):
            with self.subTest(held_back=held_back):
                state = build_state(
                    iteration=1,
                    stage_high_water=floor,
                    cleared={
                        MODULE.STAGE_SELF_REVIEW: HEAD,
                        MODULE.STAGE_COPILOT_REVIEW: HEAD,
                    },
                )
                decision = MODULE.decide_next(
                    state,
                    observation(head_sha=NEXT_HEAD, **{**forward, **held_back}),
                )
                self.assertEqual(expected, decision["stage"])

    def test_a_within_pass_move_never_raises_the_iteration_at_the_cap(self):
        """The cap bounds passes, so it must not bind on a forward move."""
        for stage in (MODULE.STAGE_COPILOT_REVIEW, MODULE.STAGE_CI):
            with self.subTest(stage=stage):
                state = build_state(
                    iteration=2,
                    max_iterations=2,
                    stage_high_water=MODULE.STAGE_INDEX[stage],
                    cleared={
                        MODULE.STAGE_SELF_REVIEW: HEAD,
                        MODULE.STAGE_COPILOT_REVIEW: HEAD,
                    },
                )
                decision = MODULE.decide_next(
                    state, observation(head_sha=NEXT_HEAD, checks="failure")
                )
                self.assertEqual("run_stage", decision["result"])
                self.assertEqual(2, decision["iteration"])
                self.assertFalse(decision["loop_back"])

    def test_the_review_stage_pushing_still_leaves_the_check_stage_in_this_pass(self):
        """The shape that used to guarantee an escalation before CI could finish.

        The Copilot review stage pushed, which staled the self-review clearance.
        The pipeline used to spend a whole pass going back for it, so the check
        stage only ever came up on a pass the cap had already refused.
        """
        state = build_state(
            iteration=1,
            max_iterations=2,
            stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_COPILOT_REVIEW],
            cleared={MODULE.STAGE_SELF_REVIEW: HEAD},
        )

        decision = MODULE.decide_next(
            state,
            observation(
                head_sha=NEXT_HEAD, copilot_review=NEXT_HEAD, checks="failure"
            ),
        )

        self.assertEqual(MODULE.STAGE_CI, decision["stage"])
        self.assertEqual(1, decision["iteration"])
        self.assertFalse(decision["loop_back"])

    def test_an_unset_high_water_mark_puts_the_floor_at_the_first_stage(self):
        """A fresh or resumed run starts a pass rather than resuming one."""
        state = build_state(iteration=1, stage_high_water=None)

        decision = MODULE.decide_next(state, observation(mergeable="CONFLICTING"))

        self.assertEqual(MODULE.STAGE_CONFLICT, decision["stage"])
        self.assertEqual(1, decision["iteration"])
        self.assertFalse(decision["loop_back"])


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


class RequiredContextsTest(unittest.TestCase):
    def target(self) -> dict:
        return MODULE.build_target("owner", "repo", 7)

    def read(self, response):
        with mock.patch.object(MODULE, "gh_json", return_value=response):
            return MODULE.required_contexts(self.target(), "main")

    def rule(self, *contexts: str) -> dict:
        return {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [{"context": name} for name in contexts]
            },
        }

    def test_every_rule_contributes_its_contexts(self):
        answer = self.read(
            [
                self.rule("EasyCLA"),
                {"type": "pull_request", "parameters": {}},
                self.rule("build / required-status-check", "gradle-wrapper-validation"),
            ]
        )
        self.assertTrue(answer["available"])
        self.assertEqual(
            [
                "EasyCLA",
                "build / required-status-check",
                "gradle-wrapper-validation",
            ],
            answer["contexts"],
        )

    def test_a_branch_with_rules_but_no_required_checks_declares_nothing(self):
        answer = self.read([{"type": "deletion", "parameters": {}}])
        self.assertFalse(answer["available"])
        self.assertEqual("none_declared", answer["reason"])

    def test_a_branch_with_no_rules_declares_nothing(self):
        answer = self.read([])
        self.assertFalse(answer["available"])
        self.assertEqual("none_declared", answer["reason"])

    def test_a_private_repository_on_a_free_plan_is_not_an_error(self):
        with mock.patch.object(
            MODULE,
            "gh_json",
            side_effect=MODULE.WorkflowError("Upgrade to GitHub Pro (HTTP 403)"),
        ):
            answer = MODULE.required_contexts(self.target(), "main")
        self.assertFalse(answer["available"])
        self.assertEqual("not_available_here", answer["reason"])

    def test_a_branch_that_is_not_found_is_not_an_error(self):
        with mock.patch.object(
            MODULE, "gh_json", side_effect=MODULE.WorkflowError("Not Found (HTTP 404)")
        ):
            answer = MODULE.required_contexts(self.target(), "main")
        self.assertFalse(answer["available"])
        self.assertEqual("no_rules", answer["reason"])

    def test_any_other_failure_is_still_survivable(self):
        with mock.patch.object(
            MODULE, "gh_json", side_effect=MODULE.WorkflowError("gh exploded")
        ):
            answer = MODULE.required_contexts(self.target(), "main")
        self.assertFalse(answer["available"])
        self.assertEqual("lookup_failed", answer["reason"])

    def test_no_base_branch_reads_nothing(self):
        with mock.patch.object(MODULE, "gh_json") as reader:
            answer = MODULE.required_contexts(self.target(), None)
        reader.assert_not_called()
        self.assertEqual("no_base_branch", answer["reason"])

    def test_the_classic_protection_endpoint_is_never_called(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("/protection/", source)

    def test_the_answer_is_read_once_per_base_branch(self):
        state: dict = {}
        with mock.patch.object(
            MODULE,
            "required_contexts",
            return_value={"available": True, "reason": "declared", "contexts": ["a"]},
        ) as reader:
            first = MODULE.cached_required_contexts(state, self.target(), "main")
            second = MODULE.cached_required_contexts(state, self.target(), "main")
        self.assertEqual(1, reader.call_count)
        self.assertEqual(first, second)
        self.assertEqual("main", state["required_contexts"]["base_branch"])

    def test_a_different_base_branch_is_read_again(self):
        state: dict = {}
        with mock.patch.object(
            MODULE,
            "required_contexts",
            return_value={"available": False, "reason": "none_declared", "contexts": []},
        ) as reader:
            MODULE.cached_required_contexts(state, self.target(), "main")
            MODULE.cached_required_contexts(state, self.target(), "release")
        self.assertEqual(2, reader.call_count)


class CheckCoverageTest(unittest.TestCase):
    def declared(self, *contexts: str) -> dict:
        return {"available": True, "reason": "declared", "contexts": sorted(contexts)}

    def judge(self, names, required, *, age=0.0, grace=180, deadline=1800):
        return MODULE.judge_check_coverage(
            set(names),
            required,
            head_age_seconds=age,
            grace_seconds=grace,
            deadline_seconds=deadline,
        )

    def test_every_declared_context_present_satisfies_coverage(self):
        coverage = self.judge({"build", "test", "extra"}, self.declared("build", "test"))
        self.assertEqual("satisfied", coverage["state"])
        self.assertEqual("declared", coverage["source"])
        self.assertEqual("required_contexts_present", coverage["reason"])

    def test_a_declared_context_that_is_missing_holds_coverage(self):
        coverage = self.judge({"build"}, self.declared("build", "test"))
        self.assertEqual("unsatisfied", coverage["state"])
        self.assertEqual(["test"], coverage["missing"])
        self.assertEqual("required_contexts_missing", coverage["reason"])

    def test_a_declared_context_is_satisfied_immediately_without_waiting(self):
        coverage = self.judge({"build"}, self.declared("build"), age=0.0)
        self.assertEqual("satisfied", coverage["state"])

    def test_an_undeclared_check_missing_from_the_rollup_holds_nothing(self):
        coverage = self.judge({"build"}, self.declared("build"), age=0.0)
        self.assertEqual([], coverage["missing"])

    def test_a_declared_context_that_never_arrives_becomes_overdue(self):
        coverage = self.judge(
            {"build"}, self.declared("build", "test"), age=1801, deadline=1800
        )
        self.assertEqual("overdue", coverage["state"])
        self.assertEqual(["test"], coverage["missing"])
        self.assertEqual("required_contexts_never_registered", coverage["reason"])

    def test_nothing_declared_falls_back_to_the_head_settling(self):
        for required in (
            None,
            {},
            {"available": False, "reason": "none_declared", "contexts": []},
            {"available": False, "reason": "not_available_here", "contexts": []},
            {"available": False, "reason": "no_rules", "contexts": []},
            {"available": False, "reason": "lookup_failed", "contexts": []},
        ):
            with self.subTest(required=required):
                fresh = self.judge({"build"}, required, age=0.0, grace=180)
                self.assertEqual("unsatisfied", fresh["state"])
                self.assertEqual("head_too_new", fresh["reason"])
                settled = self.judge({"build"}, required, age=181, grace=180)
                self.assertEqual("satisfied", settled["state"])
                self.assertEqual("age", settled["source"])
                self.assertEqual("head_settled", settled["reason"])

    def test_the_fallback_never_holds_a_stage_forever(self):
        coverage = self.judge({"build"}, None, age=10**6, grace=180)
        self.assertEqual("satisfied", coverage["state"])

    def test_an_age_that_cannot_be_measured_does_not_deadlock(self):
        coverage = MODULE.judge_check_coverage(
            {"build"}, None, head_age_seconds=None
        )
        self.assertEqual("satisfied", coverage["state"])
        self.assertEqual("age_not_measurable", coverage["reason"])

    def test_an_unmeasurable_age_still_waits_on_a_declared_context(self):
        coverage = MODULE.judge_check_coverage(
            {"build"}, self.declared("build", "test"), head_age_seconds=None
        )
        self.assertEqual("unsatisfied", coverage["state"])

    def test_no_inferred_expectation_is_ever_consulted(self):
        for name in ("base_check_names", "checks_expected"):
            with self.subTest(name=name):
                self.assertNotIn(name, SCRIPT.read_text(encoding="utf-8"))


class ApplyCheckCoverageTest(unittest.TestCase):
    def rollup(self, *names: str) -> list[dict]:
        return [
            {"name": name, "status": "COMPLETED", "conclusion": "SUCCESS"}
            for name in names
        ]

    def declared(self, *contexts: str) -> dict:
        return {"available": True, "reason": "declared", "contexts": sorted(contexts)}

    def observe(self, state, head_sha, *names, required=None, now="2024-01-01T00:00:00Z"):
        seen = {
            "pr": base_pr(),
            "head_sha": head_sha,
            "checks": MODULE.summarize_checks(self.rollup(*names)),
        }
        with mock.patch.object(MODULE, "utc_now", return_value=now):
            MODULE.apply_check_coverage(state, seen, required)
        return seen

    def test_a_partial_rollup_missing_a_declared_context_reports_pending(self):
        state: dict = {}
        seen = self.observe(state, HEAD, "build", required=self.declared("build", "test"))
        self.assertEqual("pending", seen["checks"]["state"])
        self.assertEqual(["test"], seen["checks"]["coverage"]["missing"])

    def test_a_complete_rollup_reports_success_on_the_very_first_head(self):
        state: dict = {}
        seen = self.observe(
            state, HEAD, "build", "test", required=self.declared("build", "test")
        )
        self.assertEqual("success", seen["checks"]["state"])
        self.assertEqual("satisfied", seen["checks"]["coverage"]["state"])

    def test_a_fresh_head_with_nothing_declared_reports_pending(self):
        state: dict = {}
        seen = self.observe(state, HEAD, "build")
        self.assertEqual("pending", seen["checks"]["state"])
        self.assertEqual("head_too_new", seen["checks"]["coverage"]["reason"])

    def test_a_settled_head_with_nothing_declared_reports_success(self):
        state: dict = {}
        self.observe(state, HEAD, "build")
        seen = self.observe(state, HEAD, "build", now="2024-01-01T01:00:00Z")
        self.assertEqual("success", seen["checks"]["state"])
        self.assertEqual("head_settled", seen["checks"]["coverage"]["reason"])

    def test_the_settling_clock_restarts_when_the_head_moves(self):
        state: dict = {}
        self.observe(state, HEAD, "build")
        self.observe(state, HEAD, "build", now="2024-01-01T01:00:00Z")
        seen = self.observe(state, NEXT_HEAD, "build", now="2024-01-01T01:00:05Z")
        self.assertEqual("pending", seen["checks"]["state"])
        self.assertEqual(NEXT_HEAD, state["checks_watch"]["head_sha"])
        self.assertEqual("2024-01-01T01:00:05Z", state["checks_watch"]["first_seen_at"])

    def test_the_clock_does_not_restart_while_the_head_stands_still(self):
        state: dict = {}
        self.observe(state, HEAD, "build")
        self.observe(state, HEAD, "build", "test", now="2024-01-01T00:00:30Z")
        self.assertEqual("2024-01-01T00:00:00Z", state["checks_watch"]["first_seen_at"])

    def test_coverage_never_turns_a_failure_into_something_softer(self):
        state: dict = {}
        seen = {
            "pr": base_pr(),
            "head_sha": HEAD,
            "checks": MODULE.summarize_checks(
                [{"name": "build", "status": "COMPLETED", "conclusion": "FAILURE"}]
            ),
        }
        with mock.patch.object(MODULE, "utc_now", return_value="2024-01-01T00:00:00Z"):
            MODULE.apply_check_coverage(state, seen, self.declared("build"))
        self.assertEqual("failing", seen["checks"]["state"])

    def test_a_failing_undeclared_check_still_holds_the_stage(self):
        state: dict = {}
        seen = {
            "pr": base_pr(),
            "head_sha": HEAD,
            "checks": MODULE.summarize_checks(
                [
                    {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
                    {"name": "extra", "status": "COMPLETED", "conclusion": "FAILURE"},
                ]
            ),
        }
        with mock.patch.object(MODULE, "utc_now", return_value="2024-01-01T00:00:00Z"):
            MODULE.apply_check_coverage(state, seen, self.declared("build"))
        self.assertEqual("failing", seen["checks"]["state"])

    def test_an_empty_rollup_is_still_none_and_not_pending(self):
        state: dict = {}
        seen = self.observe(state, HEAD, now="2024-01-01T01:00:00Z")
        self.assertEqual("none", seen["checks"]["state"])


class CorroborateMergeabilityTest(unittest.TestCase):
    def test_a_mergeable_answer_settles(self):
        verdict = MODULE.corroborate_mergeability("MERGEABLE")
        self.assertTrue(verdict["settled"])
        self.assertEqual("mergeable", verdict["state"])
        self.assertEqual("settled", verdict["reason"])

    def test_a_conflicting_answer_settles(self):
        verdict = MODULE.corroborate_mergeability("CONFLICTING")
        self.assertTrue(verdict["settled"])
        self.assertEqual("conflicting", verdict["state"])

    def test_an_unknown_mergeable_settles_nothing(self):
        for value in ("UNKNOWN", "", None):
            with self.subTest(value=value):
                verdict = MODULE.corroborate_mergeability(value)
                self.assertFalse(verdict["settled"])
                self.assertEqual("unsettled", verdict["state"])
                self.assertEqual("mergeable_unknown", verdict["reason"])

    def test_a_value_nobody_recognises_settles_nothing(self):
        verdict = MODULE.corroborate_mergeability("SOMEDAY")
        self.assertFalse(verdict["settled"])
        self.assertEqual("unrecognized", verdict["reason"])

    def test_the_field_is_read_case_insensitively(self):
        self.assertTrue(MODULE.corroborate_mergeability("mergeable")["settled"])
        self.assertEqual(
            "conflicting", MODULE.corroborate_mergeability("conflicting")["state"]
        )

    def test_the_merge_state_drives_nothing(self):
        # Measured across 81 open draft pull requests, the two fields never
        # disagreed. They are two views of one asynchronous computation, so
        # requiring agreement cannot catch the stale answer a guard would exist
        # to catch. A check that can never fire reads as a defense that has been
        # holding, so the field is recorded and never consulted.
        source = SCRIPT.read_text(encoding="utf-8")
        signature = source.split("def corroborate_mergeability(")[1].split(")")[0]
        self.assertNotIn("merge_state", signature)
        body = source.split("def corroborate_mergeability(")[1].split("\ndef ")[0]
        code = body.split('"""')[2]
        self.assertNotIn("merge_state", code)
        self.assertNotIn("DIRTY", code)

    def test_the_recorded_merge_state_survives_for_the_history(self):
        verdict = MODULE.stage_green(
            MODULE.STAGE_BY_NAME[MODULE.STAGE_CONFLICT],
            head_sha=HEAD,
            cleared={},
            marker={},
            observation=observation(
                mergeable="MERGEABLE", merge_state_status="BLOCKED"
            ),
        )
        self.assertTrue(verdict["green"])
        self.assertEqual("BLOCKED", verdict["merge_state_status"])


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

    def test_the_merge_state_never_causes_another_read(self):
        # A response whose two fields disagree was once re-read. Measurement
        # found they never disagree, so the re-read was unreachable code that
        # read as a live defense.
        for status in ("DIRTY", "CLEAN", "UNKNOWN", "BLOCKED"):
            with self.subTest(status=status):
                result = self.observe(
                    self.payload(mergeable="MERGEABLE", status=status)
                )
                self.assertEqual(1, self.reads)
                self.assertEqual("mergeable", result["mergeability"]["state"])
                self.assertEqual(status, result["merge_state_status"])

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
        self.assertEqual("unsatisfied", result["checks"]["coverage"]["state"])
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

    def test_a_settled_mergeable_clears_the_conflict_stage(self):
        verdict = self.verdict(MODULE.STAGE_CONFLICT, observation())
        self.assertTrue(verdict["green"])

    def test_an_unsettled_mergeable_does_not_clear_it(self):
        verdict = self.verdict(
            MODULE.STAGE_CONFLICT, observation(mergeable="UNKNOWN")
        )
        self.assertFalse(verdict["green"])
        self.assertEqual("mergeable_unknown", verdict["reason"])

    def test_the_merge_state_never_changes_the_conflict_verdict(self):
        for status in ("DIRTY", "CLEAN", "UNKNOWN", "BLOCKED", "BEHIND", "UNSTABLE"):
            with self.subTest(status=status):
                verdict = self.verdict(
                    MODULE.STAGE_CONFLICT,
                    observation(mergeable="MERGEABLE", merge_state_status=status),
                )
                self.assertTrue(verdict["green"])
                self.assertEqual(status, verdict["merge_state_status"])

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

    def test_a_missing_declared_context_sends_the_check_stage_round_again(self):
        observed = observation(
            checks="pending",
            coverage={
                "state": "unsatisfied",
                "source": "declared",
                "reason": "required_contexts_missing",
                "missing": ["test"],
                "declared": ["build", "test"],
            },
            self_review=HEAD,
            copilot_review=HEAD,
            description=HEAD,
        )
        decision = MODULE.decide_next(build_state(), observed)
        self.assertEqual("run_stage", decision["result"])
        self.assertEqual(MODULE.STAGE_CI, decision["stage"])
        self.assertEqual(
            ["test"], decision["stage_states"][MODULE.STAGE_CI]["missing_contexts"]
        )

    def test_a_passing_rollup_that_is_not_covered_is_not_green(self):
        verdict = self.verdict(
            MODULE.STAGE_CI,
            observation(
                coverage={
                    "state": "unsatisfied",
                    "source": "age",
                    "reason": "head_too_new",
                    "missing": [],
                    "declared": [],
                }
            ),
        )
        self.assertFalse(verdict["green"])
        self.assertEqual("head_too_new", verdict["reason"])

    def test_a_declared_context_that_never_registers_escalates(self):
        observed = observation(
            checks="pending",
            coverage={
                "state": "overdue",
                "source": "declared",
                "reason": "required_contexts_never_registered",
                "missing": ["build / required-status-check"],
                "declared": ["build / required-status-check"],
            },
            self_review=HEAD,
            copilot_review=HEAD,
            description=HEAD,
        )
        decision = MODULE.decide_next(build_state(), observed)
        self.assertEqual("escalate", decision["result"])
        self.assertEqual("checks_never_registered", decision["reason"])
        self.assertEqual(MODULE.STAGE_CI, decision["stage"])
        self.assertEqual(
            ["build / required-status-check"], decision["missing_contexts"]
        )
        self.assertIn("build / required-status-check", decision["detail"])
        self.assertIn("draft", decision["next_action"])

    def test_a_conflicting_answer_sends_the_stage_round_again(self):
        observed = observation(mergeable="CONFLICTING")
        decision = MODULE.decide_next(build_state(), observed)
        self.assertEqual("run_stage", decision["result"])
        self.assertEqual(MODULE.STAGE_CONFLICT, decision["stage"])

    def test_the_merge_state_alone_never_sends_the_stage_round_again(self):
        observed = observation(mergeable="MERGEABLE", merge_state_status="DIRTY")
        decision = MODULE.decide_next(build_state(), observed)
        self.assertEqual(MODULE.STAGE_SELF_REVIEW, decision["stage"])


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

    def test_a_reading_carries_the_head_the_stage_pinned_its_clearance_to(self):
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_SELF_REVIEW]
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
                stdout=json.dumps(
                    {
                        "result": "ready",
                        "stage_outcome": "cleared",
                        "review": {"outcome": "clean", "clean_at_head_sha": NEXT_HEAD},
                    }
                ),
                stderr="",
            )
            with mock.patch.object(MODULE, "copilot_home", return_value=root):
                with mock.patch.object(MODULE, "stage_state_path", return_value=state):
                    with mock.patch.object(MODULE, "run", return_value=payload):
                        reading = MODULE.read_stage_outcome(
                            entry, MODULE.build_target("owner", "repo", 7)
                        )
                        resolution = MODULE.resolve_finish_outcome(
                            entry,
                            MODULE.build_target("owner", "repo", 7),
                            "cleared",
                            head_sha=HEAD,
                        )
        self.assertEqual(NEXT_HEAD, reading["clean_at_head_sha"])
        self.assertEqual("helper", reading["evidence"])
        self.assertEqual("no_progress", resolution["outcome"])
        self.assertEqual("clean_marker_head_mismatch", resolution["outcome_reason"])

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
    def resolve(
        self,
        requested: str,
        reading: dict,
        *,
        stage: str = MODULE.STAGE_CI,
        head: str = HEAD,
    ) -> dict:
        with mock.patch.object(MODULE, "read_stage_outcome", return_value=reading):
            return MODULE.resolve_finish_outcome(
                MODULE.STAGE_BY_NAME[stage],
                MODULE.build_target("owner", "repo", 7),
                requested,
                head_sha=head,
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

    def test_a_clearance_pinned_to_this_head_is_accepted(self):
        for stage in MODULE.HELPER_EVIDENCE_STAGES:
            with self.subTest(stage=stage):
                resolution = self.resolve(
                    "cleared",
                    {
                        "available": True,
                        "outcome": "cleared",
                        "clean_at_head_sha": HEAD,
                    },
                    stage=stage,
                )
                self.assertEqual("cleared", resolution["outcome"])
                self.assertIsNone(resolution["outcome_reason"])

    def test_a_clearance_marked_at_another_head_is_refused(self):
        for stage in MODULE.HELPER_EVIDENCE_STAGES:
            with self.subTest(stage=stage):
                resolution = self.resolve(
                    "cleared",
                    {
                        "available": True,
                        "outcome": "cleared",
                        "clean_at_head_sha": NEXT_HEAD,
                    },
                    stage=stage,
                )
                self.assertEqual("no_progress", resolution["outcome"])
                self.assertEqual(
                    "clean_marker_head_mismatch", resolution["outcome_reason"]
                )
                self.assertEqual("cleared", resolution["stage_outcome"])
                self.assertEqual(NEXT_HEAD, resolution["clean_at_head_sha"])

    def test_a_clearance_with_no_marker_at_all_is_refused(self):
        resolution = self.resolve(
            "cleared",
            {"available": True, "outcome": "cleared", "clean_at_head_sha": None},
            stage=MODULE.STAGE_SELF_REVIEW,
        )
        self.assertEqual("no_progress", resolution["outcome"])
        self.assertEqual("clean_marker_head_mismatch", resolution["outcome_reason"])

    def test_a_github_backed_stage_is_not_asked_for_a_marker(self):
        resolution = self.resolve(
            "cleared",
            {"available": True, "outcome": "cleared", "clean_at_head_sha": None},
            stage=MODULE.STAGE_CI,
        )
        self.assertEqual("cleared", resolution["outcome"])
        self.assertIsNone(resolution["outcome_reason"])

    def test_only_a_clearance_needs_a_marker(self):
        for outcome in ("skipped", "no_progress", "escalated"):
            with self.subTest(outcome=outcome):
                resolution = self.resolve(
                    outcome,
                    {
                        "available": True,
                        "outcome": outcome,
                        "clean_at_head_sha": None,
                    },
                    stage=MODULE.STAGE_SELF_REVIEW,
                )
                self.assertEqual(outcome, resolution["outcome"])
                self.assertIsNone(resolution["outcome_reason"])


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


class PipelinePositionSupportTest(unittest.TestCase):
    def probe(self, body: str | None) -> bool:
        entry = MODULE.STAGE_BY_NAME[MODULE.STAGE_CI]
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "ci_fix_loop.py"
            if body is not None:
                script.write_text(body, encoding="utf-8")
            with mock.patch.object(
                MODULE, "stage_script_path", return_value=script
            ):
                return MODULE.stage_accepts_pipeline_position(entry)

    def test_a_helper_that_takes_the_argument_is_told_so(self):
        self.assertTrue(self.probe('parser.add_argument("--pipeline-run")'))

    def test_a_helper_that_does_not_take_it_is_left_alone(self):
        self.assertFalse(self.probe('parser.add_argument("--max-iterations")'))

    def test_a_missing_helper_is_not_an_error(self):
        self.assertFalse(self.probe(None))

    def test_the_probe_looks_for_the_argument_the_pipeline_actually_sends(self):
        # If the probe and the launcher ever named different flags, a stage
        # would be told it supports an argument that it stops on.
        arguments = MODULE.pipeline_position_arguments(
            MODULE.STAGE_BY_NAME[MODULE.STAGE_CI],
            run_id="abc123",
            iteration=1,
            max_iterations=2,
        )
        probed = arguments or [MODULE.PIPELINE_RUN_FLAG]
        self.assertTrue(self.probe(f'parser.add_argument("{probed[0]}")'))


class PipelineRunIdTest(unittest.TestCase):
    def test_a_run_carries_its_own_token(self):
        state = build_state()
        state["run_id"] = "abc123"
        self.assertEqual("abc123", MODULE.pipeline_run_id(state))

    def test_two_runs_on_one_pull_request_get_different_tokens(self):
        target = MODULE.build_target("owner", "repo", 7)
        observation = {"pr": {"owner": "owner"}, "head_sha": "a" * 40}
        first = MODULE.new_state(target, observation, Path("."), 3)
        second = MODULE.new_state(target, observation, Path("."), 3)
        self.assertNotEqual(
            MODULE.pipeline_run_id(first), MODULE.pipeline_run_id(second)
        )

    def test_a_state_written_before_the_token_falls_back_to_its_creation_time(self):
        state = build_state()
        state.pop("run_id", None)
        state["created_at"] = "2026-08-19T00:00:00Z"
        self.assertEqual("2026-08-19T00:00:00Z", MODULE.pipeline_run_id(state))

    def test_a_state_with_nothing_stable_reports_no_token(self):
        self.assertIsNone(MODULE.pipeline_run_id({}))

    def test_the_token_never_changes_between_launches_in_one_run(self):
        # A token that changed per launch would reset a stage's budget every
        # time the pipeline relaunched it, which is the failure it exists to
        # prevent rather than one it may cause.
        target = MODULE.build_target("owner", "repo", 7)
        observation = {"pr": {"owner": "owner"}, "head_sha": "a" * 40}
        state = MODULE.new_state(target, observation, Path("."), 3)
        first = MODULE.pipeline_run_id(state)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, state)
            reloaded = MODULE.load_state(path)
        self.assertEqual(first, MODULE.pipeline_run_id(state))
        self.assertEqual(first, MODULE.pipeline_run_id(reloaded))


class LaunchPlanTest(unittest.TestCase):
    def setUp(self):
        # Whether a stage takes the pipeline's position is read from the helper
        # installed on this machine, so every plan test says which answer it
        # wants rather than inheriting whatever happens to be installed.
        patcher = mock.patch.object(
            MODULE, "stage_accepts_pipeline_position", return_value=False
        )
        self.accepts = patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_plan_names_the_agent_plugin_qualified(self):
        plan = MODULE.launch_plan(build_state(), MODULE.STAGE_SELF_REVIEW)
        self.assertEqual("self-review-loop:self-review-loop", plan["agent"])
        self.assertIn("--agent", plan["command"])
        self.assertEqual(
            "self-review-loop:self-review-loop",
            plan["command"][plan["command"].index("--agent") + 1],
        )

    def test_the_plan_carries_the_pipelines_position(self):
        state = build_state(iteration=2, max_iterations=4)
        state["run_id"] = "abc123"
        plan = MODULE.launch_plan(state, MODULE.STAGE_CI)
        self.assertEqual("abc123", plan["pipeline_run"])
        self.assertEqual(2, plan["pipeline_iteration"])
        self.assertEqual(4, plan["pipeline_max_iterations"])

    def test_a_stage_that_takes_the_position_is_told_it_verbatim(self):
        self.accepts.return_value = True
        state = build_state(iteration=2, max_iterations=4)
        state["run_id"] = "abc123"
        plan = MODULE.launch_plan(state, MODULE.STAGE_CI)
        self.assertEqual(
            [
                "--pipeline-run",
                "abc123",
                "--pipeline-iteration",
                "2",
                "--pipeline-max-iterations",
                "4",
            ],
            plan["pipeline_arguments"],
        )
        self.assertIn(
            "--pipeline-run abc123 --pipeline-iteration 2 "
            "--pipeline-max-iterations 4",
            plan["prompt"],
        )
        self.assertIn(
            "pipeline-run: abc123 pipeline-iteration: 2 "
            "pipeline-max-iterations: 4",
            plan["prompt"],
        )
        self.assertTrue(plan["prompt"].startswith("owner/repo#7"))
        self.assertEqual(plan["prompt"], plan["command"][2])

    def test_the_position_goes_out_in_both_spellings_a_stage_may_read(self):
        # Stages key on different spellings of the same position: one watches
        # for the keyed line, another for the flags. A stage that never sees
        # the spelling it reads keeps its own budget, and says nothing about
        # having done so, so both go out every time.
        self.accepts.return_value = True
        state = build_state(iteration=2, max_iterations=4)
        state["run_id"] = "abc123"
        prompt = MODULE.launch_plan(state, MODULE.STAGE_CI)["prompt"]
        for spelling in (
            "pipeline-run: abc123",
            "pipeline-iteration: 2",
            "pipeline-max-iterations: 4",
            "--pipeline-run abc123",
            "--pipeline-iteration 2",
            "--pipeline-max-iterations 4",
        ):
            with self.subTest(spelling=spelling):
                self.assertIn(spelling, prompt)

    def test_the_keyed_line_never_reads_as_a_flag(self):
        # The keyed line and the flag list carry the same values, so the keyed
        # one has to stay distinguishable from the flags a stage copies.
        line = MODULE.position_line(
            ["--pipeline-run", "abc123", "--pipeline-iteration", "2"]
        )
        self.assertEqual("pipeline-run: abc123 pipeline-iteration: 2", line)
        self.assertNotIn("--", line)

    def test_a_stage_that_does_not_take_the_position_is_never_sent_it(self):
        # An older helper would stop on an unrecognized argument, so a stage
        # that cannot take the position is launched exactly as before.
        self.accepts.return_value = False
        state = build_state()
        state["run_id"] = "abc123"
        plan = MODULE.launch_plan(state, MODULE.STAGE_SELF_REVIEW)
        self.assertEqual([], plan["pipeline_arguments"])
        self.assertEqual("owner/repo#7", plan["prompt"])
        self.assertNotIn("--pipeline-run", plan["prompt"])

    def test_a_run_with_no_token_sends_no_position(self):
        # Half a position is worse than none: an iteration with no run cannot
        # be told from the same number in a different run.
        self.accepts.return_value = True
        state = build_state()
        state.pop("run_id", None)
        state.pop("created_at", None)
        plan = MODULE.launch_plan(state, MODULE.STAGE_CI)
        self.assertEqual([], plan["pipeline_arguments"])
        self.assertEqual("owner/repo#7", plan["prompt"])

    def test_a_position_is_never_sent_in_halves(self):
        # Stages disagree about what a half position means: one ignores it and
        # keeps a per-pull-request budget that then never resets, another reads
        # the half it can and scopes on that. Neither reading is exercised while
        # the launcher only ever sends all three or none, so that property is
        # what keeps the disagreement off the wire, and it is checked here
        # rather than left to the shape of one literal.
        self.accepts.return_value = True
        for iteration, max_iterations in ((1, 2), (2, 4), (7, 7)):
            for run_id in ("abc123", "", None):
                with self.subTest(iteration=iteration, run=run_id):
                    state = build_state(
                        iteration=iteration, max_iterations=max_iterations
                    )
                    if run_id is None:
                        state.pop("run_id", None)
                        state.pop("created_at", None)
                    else:
                        state["run_id"] = run_id
                    arguments = MODULE.launch_plan(state, MODULE.STAGE_CI)[
                        "pipeline_arguments"
                    ]
                    self.assertIn(len(arguments), (0, 6))
                    if arguments:
                        self.assertEqual(
                            [
                                MODULE.PIPELINE_RUN_FLAG,
                                MODULE.PIPELINE_ITERATION_FLAG,
                                MODULE.PIPELINE_MAX_ITERATIONS_FLAG,
                            ],
                            arguments[::2],
                        )
                        self.assertTrue(all(arguments[1::2]))

    def test_relaunching_one_stage_keeps_the_same_iteration(self):
        # A stage resets its budget when this number moves, so a relaunch
        # inside one pass must hand it the number it already had.
        state = build_state(
            iteration=2, stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_CI]
        )
        first = MODULE.launch_plan(state, MODULE.STAGE_CI)
        second = MODULE.launch_plan(state, MODULE.STAGE_CI)
        self.assertEqual(2, first["pipeline_iteration"])
        self.assertEqual(
            first["pipeline_iteration"], second["pipeline_iteration"]
        )

    def test_a_loop_back_hands_the_stage_the_advanced_iteration(self):
        # The plan is built before start records the advance, so reading the
        # stored number here would hand the stage the previous pass's.
        state = build_state(iteration=1, stage_high_water=3)
        plan = MODULE.launch_plan(state, MODULE.STAGE_SELF_REVIEW)
        self.assertEqual(1, state["iteration"])
        self.assertEqual(2, plan["pipeline_iteration"])

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
        # No command test may reach the ruleset API, and none re-judges coverage:
        # the observation fixture already carries the verdict under test. The
        # wiring that connects the two is asserted on its own.
        self.required = {
            "available": False,
            "reason": "none_declared",
            "contexts": [],
            "base_branch": "main",
        }
        contexts = mock.patch.object(
            MODULE, "cached_required_contexts", side_effect=lambda *_: self.required
        )
        self.required_reads = contexts.start()
        self.addCleanup(contexts.stop)
        coverage = mock.patch.object(
            MODULE,
            "apply_check_coverage",
            side_effect=lambda _state, observed, _required: (
                (observed.get("checks") or {}).get("coverage")
            ),
        )
        self.coverage_calls = coverage.start()
        self.addCleanup(coverage.stop)
        # No command test may reach GitHub to confirm a clearance. The default is
        # a pull request that is green at the head under test, so a clearing
        # outcome confirms; a test about an unconfirmable clearance sets
        # self.confirmation itself.
        self.confirmation = {"checked": True, "green": True, "reason": None}
        confirm = mock.patch.object(
            MODULE, "confirm_clearance", side_effect=lambda *_: self.confirmation
        )
        self.confirm_calls = confirm.start()
        self.addCleanup(confirm.stop)
        # No command test may reach git or GitHub to work out how the local head
        # stands against the pull request head. The default is a worktree already
        # sitting on the pull request head, so `reset` moves nothing and `finish`
        # records the ending. A test about a stage that committed without
        # pushing, or about a session that has not been put on the pull request
        # yet, sets self.local_head itself.
        self.local_head = MODULE.LOCAL_HEAD_AT_PR_HEAD
        diagnosis = mock.patch.object(
            MODULE,
            "diagnose_local_head",
            side_effect=lambda *_, **__: {
                "verdict": self.local_head,
                "local_head": HEAD,
                "pr_head": HEAD,
                "branch": base_pr()["head_branch"],
                "head_branch": base_pr()["head_branch"],
                "ahead_count": 0,
                "behind_count": 0,
                "detail": f"the worktree reads as {self.local_head}",
            },
        )
        diagnosis.start()
        self.addCleanup(diagnosis.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def in_directory(self, directory: Path):
        """Run a command from another working directory, as the agent would."""

        @contextlib.contextmanager
        def moved():
            previous = os.getcwd()
            os.chdir(directory)
            try:
                yield
            finally:
                os.chdir(previous)

        return moved()

    def preflight_state(self, repo: Path, **values) -> Path:
        """State as the real `preflight` writes it, run from inside `repo`.

        The repo root is not stubbed: `preflight` resolves it from the working
        directory the same way it does in a session, so the recorded value is
        the one a real run would carry.
        """

        path = Path(values.pop("state", self.root / "pipeline.json"))
        home = self.root / "home"
        home.mkdir(exist_ok=True)
        install_stage_script(home, *MODULE.STAGE_NAMES)
        arguments = {
            "target": "owner/repo#7",
            "state": str(path),
            "max_iterations": 2,
            "stage_model": None,
            "no_pin": False,
        }
        arguments.update(values)
        previous = os.getcwd()
        os.chdir(repo)
        try:
            with mock.patch.object(MODULE, "require_tools"):
                with mock.patch.object(
                    MODULE, "collect_observation", return_value=observation()
                ):
                    with mock.patch.object(MODULE, "copilot_home", return_value=home):
                        MODULE.command_preflight(self.args(**arguments))
        finally:
            os.chdir(previous)
        return path

    def github_disagrees(self, reason: str = "mergeable_unknown") -> None:
        self.confirmation = {"checked": True, "green": False, "reason": reason}

    def stage_says(self, outcome: str, clean_at_head_sha: str | None = None) -> None:
        self.stage_reading = {
            "available": True,
            "outcome": outcome,
            "clean_at_head_sha": clean_at_head_sha,
        }

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

    def finish(
        self,
        path: Path,
        stage: str,
        outcome: str,
        head: str | None = None,
        detail: str | None = "what the stage did, in one sentence",
    ):
        MODULE.command_finish(
            self.args(
                state=str(path),
                stage=stage,
                outcome=outcome,
                head=head,
                session=None,
                process=None,
                detail=detail,
            )
        )

    def test_a_cleared_stage_records_its_head_and_frees_the_session(self):
        path = self.running_state(MODULE.STAGE_SELF_REVIEW)
        self.finish(path, MODULE.STAGE_SELF_REVIEW, "cleared", NEXT_HEAD)
        state = MODULE.load_state(path)
        self.assertEqual(NEXT_HEAD, state["cleared"][MODULE.STAGE_SELF_REVIEW])
        self.assertIsNone(state["running"])
        self.assertEqual(1, len(state["history"]))
        self.assertNotIn("keep_session", self.emitted[-1])

    def test_an_unpushed_commit_refuses_the_ending_through_real_git(self):
        """A stage that committed without pushing must not have its ending sealed.

        This is the #19517 loss, driven through the real git detector rather than
        a stubbed one: a run made a real commit whose parent is the pull request
        head, then died before pushing. Recording the ending would let the next
        stage reset the worktree and turn that commit into a dangling object. The
        artifacts are asserted, not a result string: the escalation is recorded,
        the stage's `running` is cleared, and no history ending is written.
        """

        repo = Path(self.directory.name) / "repo"
        repo.mkdir()

        def git(*arguments: str) -> str:
            result = subprocess.run(
                ["git", "-C", str(repo), *arguments],
                capture_output=True,
                text=True,
                check=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@e",
                    "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@e",
                },
            )
            return result.stdout.strip()

        git("init", "-q")
        git("commit", "-q", "--allow-empty", "-m", "pr head")
        pr_head = git("rev-parse", "HEAD")
        git("commit", "-q", "--allow-empty", "-m", "the fix the stage never pushed")
        fix_sha = git("rev-parse", "HEAD")

        path = self.running_state(MODULE.STAGE_COPILOT_REVIEW, repo_root=str(repo))
        # The real diagnosis runs; only the network read of the PR head is stubbed
        # to the commit that is actually published.
        with (
            mock.patch.object(
                MODULE,
                "diagnose_local_head",
                REAL_DIAGNOSE_LOCAL_HEAD,
            ),
            mock.patch.object(MODULE, "target_remote_head", return_value=pr_head),
        ):
            self.finish(
                path, MODULE.STAGE_COPILOT_REVIEW, "cleared", fix_sha
            )

        state = MODULE.load_state(path)
        self.assertEqual("escalated", self.emitted[-1]["result"])
        self.assertEqual(
            "local_head_ahead_of_remote", state["escalation"]["reason"]
        )
        self.assertIn(pr_head, state["escalation"]["detail"])
        self.assertIsNone(state["running"])
        self.assertEqual([], state["history"])
        self.assertEqual({}, state["cleared"])

    def test_a_skipped_stage_also_clears(self):
        path = self.running_state(MODULE.STAGE_CI)
        self.finish(path, MODULE.STAGE_CI, "skipped")
        state = MODULE.load_state(path)
        self.assertEqual(HEAD, state["cleared"][MODULE.STAGE_CI])
        self.assertNotIn("keep_session", self.emitted[-1])

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
        self.assertNotIn("keep_session", self.emitted[-1])

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

    def test_repeating_the_same_answer_at_the_same_head_is_not_progress(self):
        path = self.running_state(
            MODULE.STAGE_CI,
            history=[
                {
                    "stage": MODULE.STAGE_CI,
                    "outcome": "cleared",
                    "head_sha": HEAD,
                }
            ],
        )
        self.finish(path, MODULE.STAGE_CI, "cleared")
        state = MODULE.load_state(path)
        self.assertTrue(state["history"][-1]["repeat"])
        self.assertEqual(1, state["no_progress"][MODULE.STAGE_CI]["count"])
        self.assertEqual(HEAD, state["cleared"][MODULE.STAGE_CI])

    def test_a_second_repeat_escalates_rather_than_relaunching_forever(self):
        path = self.running_state(
            MODULE.STAGE_CI,
            history=[
                {
                    "stage": MODULE.STAGE_CI,
                    "outcome": "cleared",
                    "head_sha": HEAD,
                }
            ],
            no_progress={MODULE.STAGE_CI: {"count": 1, "head_sha": HEAD}},
        )
        self.finish(path, MODULE.STAGE_CI, "cleared")
        state = MODULE.load_state(path)
        self.assertEqual("no_progress", state["escalation"]["reason"])
        self.assertIn("repeated its cleared answer", state["escalation"]["detail"])

    def test_the_same_answer_at_a_new_head_is_fresh_evidence(self):
        path = self.running_state(
            MODULE.STAGE_CI,
            history=[
                {
                    "stage": MODULE.STAGE_CI,
                    "outcome": "cleared",
                    "head_sha": HEAD,
                }
            ],
            no_progress={MODULE.STAGE_CI: {"count": 1, "head_sha": HEAD}},
        )
        self.finish(path, MODULE.STAGE_CI, "cleared", NEXT_HEAD)
        state = MODULE.load_state(path)
        self.assertFalse(state["history"][-1]["repeat"])
        self.assertNotIn(MODULE.STAGE_CI, state["no_progress"])
        self.assertIsNone(state["escalation"])

    def test_a_different_answer_at_the_same_head_is_fresh_evidence(self):
        path = self.running_state(
            MODULE.STAGE_CI,
            history=[
                {
                    "stage": MODULE.STAGE_CI,
                    "outcome": "no_progress",
                    "head_sha": HEAD,
                }
            ],
        )
        self.finish(path, MODULE.STAGE_CI, "cleared")
        state = MODULE.load_state(path)
        self.assertFalse(state["history"][-1]["repeat"])
        self.assertNotIn(MODULE.STAGE_CI, state["no_progress"])

    def test_a_repeated_escalation_still_stops_the_pipeline_as_an_escalation(self):
        path = self.running_state(
            MODULE.STAGE_CI,
            history=[
                {
                    "stage": MODULE.STAGE_CI,
                    "outcome": "escalated",
                    "head_sha": HEAD,
                }
            ],
        )
        self.finish(path, MODULE.STAGE_CI, "escalated")
        state = MODULE.load_state(path)
        self.assertEqual("stage_escalated", state["escalation"]["reason"])

    def test_a_clearance_from_a_marker_at_another_head_records_nothing(self):
        path = self.running_state(MODULE.STAGE_SELF_REVIEW)
        self.stage_says("cleared", clean_at_head_sha=NEXT_HEAD)
        self.finish(path, MODULE.STAGE_SELF_REVIEW, "cleared", HEAD)
        state = MODULE.load_state(path)
        self.assertNotIn(MODULE.STAGE_SELF_REVIEW, state["cleared"])
        entry = state["history"][-1]
        self.assertEqual("no_progress", entry["outcome"])
        self.assertEqual("cleared", entry["stage_outcome"])
        self.assertEqual("clean_marker_head_mismatch", entry["outcome_reason"])
        self.assertEqual(NEXT_HEAD, entry["clean_at_head_sha"])
        self.assertEqual(
            "clean_marker_head_mismatch", self.emitted[-1]["outcome_reason"]
        )

    def test_a_stale_state_file_cannot_clear_a_stage_the_run_never_reached(self):
        path = self.running_state(MODULE.STAGE_DESCRIPTION)
        self.stage_says("cleared", clean_at_head_sha="0" * 40)
        self.finish(path, MODULE.STAGE_DESCRIPTION, "no_progress", HEAD)
        state = MODULE.load_state(path)
        self.assertEqual({}, state["cleared"])
        self.assertEqual(1, state["no_progress"][MODULE.STAGE_DESCRIPTION]["count"])

    def test_a_clearance_pinned_to_the_recorded_head_still_clears(self):
        path = self.running_state(MODULE.STAGE_SELF_REVIEW)
        self.stage_says("cleared", clean_at_head_sha=NEXT_HEAD)
        self.finish(path, MODULE.STAGE_SELF_REVIEW, "cleared", NEXT_HEAD)
        state = MODULE.load_state(path)
        self.assertEqual(NEXT_HEAD, state["cleared"][MODULE.STAGE_SELF_REVIEW])
        self.assertIsNone(state["history"][-1]["outcome_reason"])

    def test_a_github_backed_stage_clears_without_any_marker(self):
        path = self.running_state(MODULE.STAGE_CI)
        self.stage_says("cleared")
        self.finish(path, MODULE.STAGE_CI, "cleared", HEAD)
        state = MODULE.load_state(path)
        self.assertEqual(HEAD, state["cleared"][MODULE.STAGE_CI])

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
        self.assertNotIn("keep_session", self.emitted[-1])

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


class RequiredDetailTest(CommandTestCase):
    """The stage session is archived, so the sentence is the only account left."""

    def running_state(self, stage: str, **overrides):
        running = {
            "stage": stage,
            "iteration": 1,
            "head_sha": HEAD,
            "launch": "session",
            "session_id": None,
            "process_id": None,
            "model": MODULE.DEFAULT_STAGE_MODEL,
            "started_at": "2026-01-01T00:00:00Z",
        }
        return write_state(self.root, running=running, **overrides)

    def call(self, stage: str, outcome: str, detail):
        path = self.running_state(stage)
        MODULE.command_finish(
            self.args(
                state=str(path),
                stage=stage,
                outcome=outcome,
                head=HEAD,
                session=None,
                process=None,
                detail=detail,
            )
        )
        return MODULE.load_state(path)

    def test_an_outcome_that_explains_nothing_by_itself_needs_a_sentence(self):
        for outcome in MODULE.DETAIL_REQUIRED_OUTCOMES:
            for detail in (None, "", "   "):
                with self.subTest(outcome=outcome, detail=detail):
                    with self.assertRaises(MODULE.WorkflowError) as caught:
                        self.call(MODULE.STAGE_SELF_REVIEW, outcome, detail)
                    self.assertIn("--detail", str(caught.exception))

    def test_an_outcome_a_head_sha_already_accounts_for_does_not(self):
        for outcome in MODULE.CLEARING_OUTCOMES:
            with self.subTest(outcome=outcome):
                state = self.call(MODULE.STAGE_SELF_REVIEW, outcome, None)
                self.assertEqual(1, len(state["history"]))

    def test_the_two_lists_do_not_overlap(self):
        self.assertEqual(
            set(),
            set(MODULE.DETAIL_REQUIRED_OUTCOMES) & set(MODULE.CLEARING_OUTCOMES),
        )
        self.assertEqual(
            set(MODULE.STAGE_OUTCOMES),
            set(MODULE.DETAIL_REQUIRED_OUTCOMES) | set(MODULE.CLEARING_OUTCOMES),
        )

    def test_a_reclassified_outcome_says_so_rather_than_leaving_a_blank(self):
        # The caller asked for a clearance, which needs no sentence, and the
        # stage's own record disagreed. Refusing here would demand a reason for
        # something the caller never saw, so the disagreement is the record.
        self.stage_says("escalated")
        state = self.call(MODULE.STAGE_CI, "cleared", None)
        entry = state["history"][0]
        self.assertEqual("escalated", entry["outcome"])
        self.assertIn("escalated", entry["detail"])
        self.assertIn("cleared", entry["detail"])
        self.assertEqual(entry["detail"], state["escalation"]["detail"])

    def test_a_supplied_sentence_is_never_replaced(self):
        self.stage_says("escalated")
        state = self.call(MODULE.STAGE_CI, "cleared", "the build never came back")
        self.assertEqual(
            "the build never came back", state["history"][0]["detail"]
        )

    def test_a_clearance_keeps_its_missing_sentence_missing(self):
        state = self.call(MODULE.STAGE_SELF_REVIEW, "cleared", None)
        self.assertIsNone(state["history"][0]["detail"])

    def test_nothing_reports_whether_to_keep_a_session(self):
        # Every stage session is archived, so a field answering "keep this one?"
        # decides nothing. Left in place it reads as a switch that has been
        # working, and the next reader rebuilds the branch from it.
        self.assertNotIn("keep_session", SCRIPT.read_text(encoding="utf-8"))


class BeginRunTest(unittest.TestCase):
    """A fresh invocation is a new run, because the loop can never cause one."""

    def restart(self, **overrides):
        state = build_state(**overrides)
        started = MODULE.begin_run(state)
        return state, started

    def test_a_stored_escalation_never_outlives_the_run_that_recorded_it(self):
        state, _ = self.restart(
            escalation={"stage": MODULE.STAGE_CONFLICT, "reason": "stage_escalated"}
        )
        self.assertIsNone(state["escalation"])

    def test_the_iteration_budget_bounds_one_run_rather_than_one_pull_request(self):
        state, _ = self.restart(iteration=2, stage_high_water=4)
        self.assertEqual(1, state["iteration"])
        self.assertIsNone(state["stage_high_water"])

    def test_a_streak_from_an_earlier_run_does_not_escalate_this_one(self):
        state, _ = self.restart(
            no_progress={MODULE.STAGE_CI: {"count": 1, "head_sha": HEAD}}
        )
        self.assertEqual({}, state["no_progress"])

    def test_a_streak_at_the_limit_cannot_outlive_the_escalation_it_produced(self):
        # A streak is what produces a no-progress escalation, so keeping one
        # while clearing the other leaves the next run a single strike from the
        # limit and it re-derives the same escalation from the residue.
        state, _ = self.restart(
            escalation={"stage": MODULE.STAGE_CONFLICT, "reason": "no_progress"},
            no_progress={
                MODULE.STAGE_CONFLICT: {
                    "count": MODULE.NO_PROGRESS_LIMIT,
                    "head_sha": HEAD,
                }
            },
        )
        self.assertIsNone(state["escalation"])
        self.assertEqual({}, state["no_progress"])
        self.assertEqual(
            0, MODULE.no_progress_streak(state, MODULE.STAGE_CONFLICT)
        )

    def test_a_surviving_streak_would_deadlock_the_pull_request_on_its_own(self):
        # decide_next escalates on a streak at the limit before it launches
        # anything, so a streak that outlived its run would rebuild the stored
        # escalation from scratch on the first look. Clearing the escalation
        # without clearing the streak would leave the pull request exactly as
        # stuck, by a different route and with no run in between.
        stuck = build_state(
            escalation=None,
            no_progress={
                MODULE.STAGE_CONFLICT: {
                    "count": MODULE.NO_PROGRESS_LIMIT,
                    "head_sha": HEAD,
                }
            },
        )
        blocked = MODULE.decide_next(stuck, observation(mergeable="UNKNOWN"))
        self.assertEqual("escalate", blocked["result"])
        self.assertEqual("no_progress", blocked["reason"])

        MODULE.begin_run(stuck)
        freed = MODULE.decide_next(stuck, observation(mergeable="UNKNOWN"))
        self.assertEqual("run_stage", freed["result"])
        self.assertEqual(MODULE.STAGE_CONFLICT, freed["stage"])

    def test_the_report_survives_because_it_is_the_report(self):
        history = [{"stage": MODULE.STAGE_CI, "outcome": "escalated"}]
        state, _ = self.restart(history=list(history))
        self.assertEqual(history, state["history"])

    def test_a_clearance_survives_because_it_names_its_own_commit(self):
        state, _ = self.restart(cleared={MODULE.STAGE_CONFLICT: HEAD})
        self.assertEqual({MODULE.STAGE_CONFLICT: HEAD}, state["cleared"])

    def test_the_run_identity_changes_so_the_stages_reset_their_budgets_too(self):
        state = build_state()
        before = MODULE.pipeline_run_id(state)
        MODULE.begin_run(state)
        self.assertNotEqual(before, MODULE.pipeline_run_id(state))

    def test_a_stage_left_running_is_recorded_rather_than_dropped(self):
        state, started = self.restart(
            running={
                "stage": MODULE.STAGE_CI,
                "iteration": 2,
                "head_sha": HEAD,
                "session_id": "abc",
                "started_at": "2026-01-01T00:00:00Z",
            }
        )
        self.assertIsNone(state["running"])
        entry = state["history"][-1]
        self.assertEqual(MODULE.STAGE_CI, entry["stage"])
        self.assertEqual("abandoned", entry["outcome"])
        self.assertEqual("run_restarted", entry["outcome_reason"])
        self.assertEqual("abc", entry["session_id"])
        self.assertTrue(entry["detail"])
        self.assertEqual(MODULE.STAGE_CI, started["abandoned_stage"])

    def test_a_run_that_ended_tidily_adds_no_abandoned_entry(self):
        state, started = self.restart(running=None, history=[])
        self.assertEqual([], state["history"])
        self.assertIsNone(started["abandoned_stage"])

    def test_every_run_is_recorded_so_a_relaunched_pipeline_is_visible(self):
        state = build_state()
        first = MODULE.pipeline_run_id(state)
        MODULE.begin_run(state)
        MODULE.begin_run(state)
        runs = state["runs"]
        self.assertEqual(2, len(runs))
        self.assertEqual(first, runs[0]["previous_run_id"])
        self.assertEqual(runs[0]["run_id"], runs[1]["previous_run_id"])
        self.assertEqual(runs[-1]["run_id"], state["run_id"])

    def test_a_fresh_state_records_its_own_first_run(self):
        state = MODULE.new_state(
            MODULE.build_target("o", "r", 1), observation(), Path("."), 2
        )
        self.assertEqual(1, len(state["runs"]))
        self.assertEqual(state["run_id"], state["runs"][0]["run_id"])
        self.assertIsNone(state["runs"][0]["previous_run_id"])


class StreakEffectTest(unittest.TestCase):
    """What may reset the brake decides whether anything is bounded."""

    def test_a_stalled_run_charges(self):
        self.assertEqual(
            "charge",
            MODULE.streak_effect("no_progress", repeat=False, confirmed=None),
        )

    def test_a_repeated_answer_charges_whatever_it_says(self):
        for outcome in MODULE.STAGE_OUTCOMES:
            with self.subTest(outcome=outcome):
                self.assertEqual(
                    "charge",
                    MODULE.streak_effect(outcome, repeat=True, confirmed=True),
                )

    def test_a_confirmed_clearance_is_the_only_thing_that_resets(self):
        for outcome in MODULE.CLEARING_OUTCOMES:
            with self.subTest(outcome=outcome):
                self.assertEqual(
                    "reset",
                    MODULE.streak_effect(outcome, repeat=False, confirmed=True),
                )

    def test_a_clearance_github_contradicts_charges(self):
        for outcome in MODULE.CLEARING_OUTCOMES:
            with self.subTest(outcome=outcome):
                self.assertEqual(
                    "charge",
                    MODULE.streak_effect(outcome, repeat=False, confirmed=False),
                )

    def test_a_clearance_nothing_answered_about_charges_too(self):
        # An answer that was never read is not one the pipeline can act on.
        for outcome in MODULE.CLEARING_OUTCOMES:
            with self.subTest(outcome=outcome):
                self.assertEqual(
                    "charge",
                    MODULE.streak_effect(outcome, repeat=False, confirmed=None),
                )

    def test_nothing_a_stage_can_do_to_itself_resets_the_brake(self):
        # The reset has one input the stage cannot produce: GitHub agreeing at
        # this commit. Pushing, relaunching, and reporting a clearance are all
        # things the stage does, and none of them reaches the reset.
        resets = {
            (outcome, repeat, confirmed)
            for outcome in MODULE.STAGE_OUTCOMES
            for repeat in (True, False)
            for confirmed in (True, False, None)
            if MODULE.streak_effect(outcome, repeat=repeat, confirmed=confirmed)
            == "reset"
        }
        self.assertTrue(resets)
        for outcome, repeat, confirmed in resets:
            with self.subTest(outcome=outcome, repeat=repeat, confirmed=confirmed):
                if outcome in MODULE.CLEARING_OUTCOMES:
                    self.assertIs(True, confirmed)
                self.assertFalse(repeat)


class UnconfirmableClearanceTest(CommandTestCase):
    """The first stage has no stage ahead of it, so the streak is its only brake."""

    def running_state(self, stage: str, head: str, **overrides):
        running = {
            "stage": stage,
            "iteration": 1,
            "head_sha": head,
            "launch": "session",
            "session_id": None,
            "process_id": None,
            "model": MODULE.DEFAULT_STAGE_MODEL,
            "started_at": "2026-01-01T00:00:00Z",
        }
        return write_state(self.root, running=running, **overrides)

    def finish_at(self, path, stage, head, outcome="cleared"):
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
        return MODULE.load_state(path)

    def test_a_push_at_a_new_head_no_longer_buys_another_relaunch(self):
        # conflict-fix-loop pushes a merge, reports cleared, and GitHub answers
        # UNKNOWN because it computes mergeability asynchronously. The head is
        # new every time, so the repeat brake never engages, and nothing sits
        # ahead of index 0 to charge an iteration. Without this the stage can be
        # relaunched for ever, a push at a time.
        self.github_disagrees("mergeable_unknown")
        path = self.running_state(MODULE.STAGE_CONFLICT, HEAD)
        state = self.finish_at(path, MODULE.STAGE_CONFLICT, HEAD)
        self.assertEqual(1, state["no_progress"][MODULE.STAGE_CONFLICT]["count"])
        self.assertIsNone(state["escalation"])

        state["running"] = {
            "stage": MODULE.STAGE_CONFLICT,
            "iteration": 1,
            "head_sha": NEXT_HEAD,
            "launch": "session",
            "model": MODULE.DEFAULT_STAGE_MODEL,
            "started_at": "2026-01-01T00:00:00Z",
        }
        MODULE.save_state(path, state)
        state = self.finish_at(path, MODULE.STAGE_CONFLICT, NEXT_HEAD)
        self.assertEqual("no_progress", state["escalation"]["reason"])
        self.assertIn("could not confirm", state["escalation"]["detail"])
        self.assertIn("mergeable_unknown", state["escalation"]["detail"])

    def test_the_escalation_does_not_accuse_the_stage_of_doing_nothing(self):
        # The stage may well have merged it. The pipeline just cannot see that.
        self.github_disagrees()
        path = self.running_state(
            MODULE.STAGE_CONFLICT,
            HEAD,
            no_progress={
                MODULE.STAGE_CONFLICT: {"count": 1, "head_sha": "older"}
            },
        )
        state = self.finish_at(path, MODULE.STAGE_CONFLICT, HEAD)
        detail = state["escalation"]["detail"]
        self.assertIn("may well have done the work", detail)
        self.assertNotIn("without changing anything", detail)

    def test_a_confirmed_clearance_still_clears_the_brake(self):
        path = self.running_state(
            MODULE.STAGE_CONFLICT,
            HEAD,
            no_progress={MODULE.STAGE_CONFLICT: {"count": 1, "head_sha": "older"}},
        )
        state = self.finish_at(path, MODULE.STAGE_CONFLICT, HEAD)
        self.assertNotIn(MODULE.STAGE_CONFLICT, state["no_progress"])
        self.assertIsNone(state["escalation"])

    def test_the_history_records_whether_the_pipeline_could_confirm_it(self):
        self.github_disagrees("mergeable_unknown")
        path = self.running_state(MODULE.STAGE_CONFLICT, HEAD)
        state = self.finish_at(path, MODULE.STAGE_CONFLICT, HEAD)
        entry = state["history"][0]
        self.assertIs(False, entry["clearance_confirmed"])
        self.assertEqual("mergeable_unknown", entry["clearance_reason"])
        self.assertIs(False, self.emitted[-1]["clearance_confirmed"])

    def test_an_unconfirmed_clearance_is_still_recorded_as_a_clearance(self):
        # The stage's word is kept; it just stops resetting the brake. Demoting
        # the outcome as well would put two different corrections on one reading.
        self.github_disagrees()
        path = self.running_state(MODULE.STAGE_CONFLICT, HEAD)
        state = self.finish_at(path, MODULE.STAGE_CONFLICT, HEAD)
        self.assertEqual("cleared", state["history"][0]["outcome"])


class ConfirmClearanceTest(unittest.TestCase):
    def setUp(self):
        self.target = MODULE.build_target("owner", "repo", 7)

    def confirm(self, outcome, head, observed):
        with mock.patch.object(MODULE, "collect_observation", return_value=observed):
            return MODULE.confirm_clearance(
                MODULE.STAGE_BY_NAME[MODULE.STAGE_CONFLICT], self.target, outcome, head
            )

    def test_only_a_clearing_outcome_is_worth_confirming(self):
        for outcome in MODULE.DETAIL_REQUIRED_OUTCOMES:
            with self.subTest(outcome=outcome):
                with mock.patch.object(MODULE, "collect_observation") as reader:
                    result = MODULE.confirm_clearance(
                        MODULE.STAGE_BY_NAME[MODULE.STAGE_CONFLICT],
                        self.target,
                        outcome,
                        HEAD,
                    )
                reader.assert_not_called()
                self.assertFalse(result["checked"])
                self.assertIsNone(result["green"])

    def test_github_agreeing_confirms_it(self):
        result = self.confirm("cleared", HEAD, observation())
        self.assertTrue(result["checked"])
        self.assertIs(True, result["green"])

    def test_github_not_having_computed_it_yet_does_not(self):
        result = self.confirm("cleared", HEAD, observation(mergeable="UNKNOWN"))
        self.assertIs(False, result["green"])
        self.assertEqual("mergeable_unknown", result["reason"])

    def test_a_head_that_moved_under_the_look_confirms_nothing(self):
        result = self.confirm("cleared", HEAD, observation(head_sha=NEXT_HEAD))
        self.assertIs(False, result["green"])
        self.assertEqual("head_moved", result["reason"])

    def test_an_answer_that_could_not_be_read_is_not_a_confirmation(self):
        with mock.patch.object(
            MODULE, "collect_observation", side_effect=OSError("no network")
        ):
            result = MODULE.confirm_clearance(
                MODULE.STAGE_BY_NAME[MODULE.STAGE_CONFLICT],
                self.target,
                "cleared",
                HEAD,
            )
        self.assertFalse(result["checked"])
        self.assertIsNone(result["green"])
        self.assertIn("no network", result["reason"])

    def test_the_pipelines_own_record_never_confirms_its_own_record(self):
        # Passing the cleared map would let a clearance the pipeline wrote at
        # this head vouch for the clearance it is about to write.
        captured = {}

        def spy(entry, **values):
            captured.update(values)
            return {"green": True}

        with mock.patch.object(
            MODULE, "collect_observation", return_value=observation()
        ):
            with mock.patch.object(MODULE, "stage_green", side_effect=spy):
                MODULE.confirm_clearance(
                    MODULE.STAGE_BY_NAME[MODULE.STAGE_CONFLICT],
                    self.target,
                    "cleared",
                    HEAD,
                )
        self.assertEqual({}, captured["cleared"])


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

    def test_the_declared_contexts_are_read_for_the_base_branch_and_judged(self):
        path = write_state(self.root)
        self.call_next(path, observation())
        self.assertEqual("main", self.required_reads.call_args.args[2])
        self.assertIs(self.required, self.coverage_calls.call_args.args[2])

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
        self.call_next(
            path, observation(head_sha=NEXT_HEAD, description=NEXT_HEAD)
        )
        payload = self.emitted[-1]
        self.assertEqual(MODULE.STAGE_SELF_REVIEW, payload["stage"])
        self.assertEqual(2, payload["iteration"])
        self.assertTrue(payload["loop_back"])

    def test_next_does_not_charge_an_iteration_by_itself(self):
        path = write_state(
            self.root,
            stage_high_water=MODULE.STAGE_INDEX[MODULE.STAGE_DESCRIPTION],
        )
        for _ in range(2):
            self.call_next(
                path, observation(head_sha=NEXT_HEAD, description=NEXT_HEAD)
            )
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
        MODULE.command_status(self.args(state=str(path), current=False))
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
                    state=str(self.root / "missing.json"), current=False
                )
            )


class StageActivityTest(CommandTestCase):
    """Observability only. Nothing here bounds, throttles, or escalates anything."""

    def running_state(self, **overrides) -> dict:
        running = {
            "stage": MODULE.STAGE_CI,
            "head_sha": HEAD,
            "iteration": 1,
            "started_at": "2026-01-01T00:00:00Z",
        }
        running.update(overrides)
        return build_state(running=running)

    def test_a_stage_that_is_not_running_has_nothing_to_wait_on(self):
        self.assertIsNone(MODULE.stage_activity(build_state()))

    def test_a_running_stage_reports_both_waits_from_recorded_timestamps(self):
        state = self.running_state()
        with mock.patch.object(MODULE, "utc_now", return_value="2026-01-01T01:00:00Z"):
            with mock.patch.object(
                MODULE,
                "run_stage_status",
                return_value={
                    "ok": True,
                    "payload": {"last_helper_activity": "2026-01-01T00:45:00Z"},
                },
            ):
                activity = MODULE.stage_activity(state)
        self.assertEqual(MODULE.STAGE_CI, activity["stage"])
        self.assertEqual(3600.0, activity["running_for_seconds"])
        self.assertEqual("2026-01-01T00:45:00Z", activity["last_helper_activity"])
        self.assertEqual(900.0, activity["helper_silent_for_seconds"])
        self.assertNotIn("reason", activity)

    def test_it_says_plainly_that_it_is_not_proof_the_stage_is_alive(self):
        """`status` reports timestamps, not a probe.

        The pipeline's `wait` owns the stage process and decides liveness from
        its pid; `status` does not, so it must not claim more than the recorded
        timestamps support.
        """
        with mock.patch.object(
            MODULE, "run_stage_status", return_value={"ok": False, "reason": "no_state"}
        ):
            activity = MODULE.stage_activity(self.running_state())
        self.assertIn("a timestamp view, not a probe", activity["note"])
        self.assertIn("`status` does not", activity["note"])

    def test_a_helper_that_does_not_report_the_stamp_reads_as_unknown(self):
        """An unanswerable question is not the answer "just now"."""

        with mock.patch.object(
            MODULE, "run_stage_status", return_value={"ok": True, "payload": {}}
        ):
            activity = MODULE.stage_activity(self.running_state())
        self.assertIsNone(activity["last_helper_activity"])
        self.assertIsNone(activity["helper_silent_for_seconds"])
        self.assertEqual("not_reported", activity["reason"])

    def test_a_helper_that_cannot_be_asked_carries_the_reason_it_gave(self):
        with mock.patch.object(
            MODULE,
            "run_stage_status",
            return_value={"ok": False, "reason": "helper_missing"},
        ):
            activity = MODULE.stage_activity(self.running_state())
        self.assertIsNone(activity["last_helper_activity"])
        self.assertEqual("helper_missing", activity["reason"])

    def test_status_carries_the_block_in_the_envelope_and_the_snapshot(self):
        path = write_state(
            self.root,
            running={
                "stage": MODULE.STAGE_CI,
                "head_sha": HEAD,
                "iteration": 1,
                "started_at": "2026-01-01T00:00:00Z",
            },
        )
        with mock.patch.object(
            MODULE,
            "run_stage_status",
            return_value={
                "ok": True,
                "payload": {"last_helper_activity": "2026-01-01T00:45:00Z"},
            },
        ):
            MODULE.command_status(
                self.args(state=str(path), current=False)
            )
        payload = self.emitted[-1]
        self.assertEqual(MODULE.STAGE_CI, payload["activity"]["stage"])
        snapshot = json.loads(Path(payload["status_path"]).read_text(encoding="utf-8"))
        self.assertEqual(
            "2026-01-01T00:45:00Z", snapshot["activity"]["last_helper_activity"]
        )

    def test_status_without_a_running_stage_reports_no_activity(self):
        path = write_state(self.root)
        MODULE.command_status(self.args(state=str(path), current=False))
        payload = self.emitted[-1]
        self.assertIsNone(payload["activity"])
        snapshot = json.loads(Path(payload["status_path"]).read_text(encoding="utf-8"))
        self.assertIsNone(snapshot["activity"])

    def test_nothing_the_block_reports_reaches_the_stage_decision(self):
        """Observability that steers the pipeline stops being observability."""

        source = inspect.getsource(MODULE.decide_next)
        for name in (
            "stage_activity",
            "last_helper_activity",
            "helper_silent_for_seconds",
            "running_for_seconds",
        ):
            self.assertNotIn(name, source)


class CleanupCommandTest(CommandTestCase):
    def test_cleanup_removes_the_state_and_its_snapshot(self):
        path = write_state(self.root)
        MODULE.command_status(self.args(state=str(path), current=False))
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

    def test_the_declared_contexts_are_read_for_the_base_branch_and_judged(self):
        self.call_preflight(observation(), installed=MODULE.STAGE_NAMES)
        self.assertEqual("main", self.required_reads.call_args.args[2])
        self.assertIs(self.required, self.coverage_calls.call_args.args[2])

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

    def test_an_escalated_pull_request_can_be_run_again(self):
        # A stored escalation is read before anything live, so keeping it would
        # make one escalation permanent and put the only escape behind deleting
        # the history of why it happened.
        self.call_preflight(observation())
        path = self.root / "pipeline.json"
        state = MODULE.load_state(path)
        state["escalation"] = {
            "stage": MODULE.STAGE_CONFLICT,
            "reason": "stage_escalated",
            "detail": "a person had to merge it by hand",
        }
        state["iteration"] = 2
        state["stage_high_water"] = 4
        state["history"] = [{"stage": MODULE.STAGE_CONFLICT, "outcome": "escalated"}]
        MODULE.save_state(path, state)

        self.call_preflight(observation())
        resumed = MODULE.load_state(path)
        self.assertIsNone(resumed["escalation"])
        self.assertEqual(1, resumed["iteration"])
        self.assertEqual(1, len(resumed["history"]))
        self.assertEqual(2, self.emitted[-1]["run_count"])
        self.assertEqual(resumed["run_id"], self.emitted[-1]["run_id"])
        self.assertIsNotNone(self.emitted[-1]["restarted"])

    def test_a_restarted_run_then_decides_from_what_is_live(self):
        self.call_preflight(observation())
        path = self.root / "pipeline.json"
        state = MODULE.load_state(path)
        state["escalation"] = {"stage": MODULE.STAGE_CI, "reason": "stage_escalated"}
        MODULE.save_state(path, state)
        self.call_preflight(observation())

        decision = MODULE.decide_next(MODULE.load_state(path), observation())
        self.assertNotEqual("escalate", decision["result"])

    def test_a_fresh_run_reports_no_restart(self):
        self.call_preflight(observation())
        self.assertIsNone(self.emitted[-1]["restarted"])
        self.assertEqual(1, self.emitted[-1]["run_count"])

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

    def test_the_launch_command_carries_the_pipelines_position(self):
        # The plan's command embeds the prompt, which carries the pipeline's
        # position; the agent runs that command verbatim through `launch` rather
        # than choosing between target and prompt itself.
        self.assertIn(
            "`launch --state <path> --log <plan log_path> -- <plan command>`",
            self.instructions,
        )
        state = build_state(run_id="abc123")
        plan = MODULE.launch_plan(state, MODULE.STAGE_CI)
        self.assertIn("-p", plan["command"])
        prompt = plan["command"][plan["command"].index("-p") + 1]
        self.assertIn(MODULE.PIPELINE_RUN_FLAG.lstrip("-"), prompt)

    def test_the_documented_invocations_never_pass_a_repository_path(self):
        # The agent file is the only place the invocations are written down. A
        # documented `--repo-root` is how a second tree gets named in one run.
        self.assertNotIn("--repo-root", self.instructions)

    def test_every_local_head_escalation_is_named_for_the_reader(self):
        # Read off the source map, so a new verdict has to be documented rather
        # than left for the agent to meet unannounced.
        for verdict, reason in MODULE.LOCAL_HEAD_ESCALATIONS.items():
            with self.subTest(verdict=verdict):
                self.assertIn(reason, self.instructions)

    def test_the_pass_flows_forward_and_only_loops_at_its_end(self):
        """The prose is what the next reader reinstates, so it has to say which it is.

        A reader who believes a push sends the pipeline straight back also
        believes an outer iteration is spent on that hop, which is the accounting
        this order removed.
        """
        self.assertIn("The loop back waits for the end of the pass.", self.instructions)
        self.assertIn(
            "a commit pushed by a later stage never sends the pipeline backwards "
            "in the middle of that pass",
            self.instructions,
        )
        self.assertIn(
            "Only when every stage from that point to the end is green does the "
            "pipeline go back for a clearance the push staled",
            self.instructions,
        )
        self.assertIn(
            "Two iterations means two passes down the stage order, not two "
            "backward jumps.",
            self.instructions,
        )
        self.assertIn(
            "This is where the start of a new pass increments the iteration",
            self.instructions,
        )

    def test_the_activity_block_is_documented_as_not_proof_of_liveness(self):
        """Prose that oversells the block is worse than no block at all."""

        self.assertIn("adds an `activity` block", self.instructions)
        self.assertIn(
            "That block is a timestamp view, not a probe", self.instructions
        )
        self.assertIn(
            "`wait` owns the stage process and judges liveness from its pid",
            self.instructions,
        )
        self.assertIn(
            "separates a stage that was active minutes ago from one silent for "
            "an hour",
            self.instructions,
        )
        self.assertIn(
            "A helper that cannot answer reports `null` beside a `reason` rather "
            "than a zero.",
            self.instructions,
        )

    def test_frontmatter_matches_the_sibling_shape(self):
        self.assertIn("name: PR Pipeline", self.instructions)
        self.assertIn("argument-hint:", self.instructions)
        self.assertIn("user-invocable: true", self.instructions)
        self.assertIn(
            "tools: [read, search, execute, todo, rename_session]",
            self.instructions,
        )
        tools_line = next(
            line for line in self.instructions.splitlines()
            if line.startswith("tools:")
        )
        self.assertNotIn("create_session", tools_line)
        self.assertNotIn("get_session", tools_line)
        self.assertNotIn("archive_session", tools_line)

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
            "Confirm it with `outcome --stage <plan stage>`", self.instructions
        )
        self.assertIn(
            "When `outcome`'s `result` is `not_reported`, the stage does not "
            "answer for itself yet, so fall back to reading",
            self.instructions,
        )
        first = self.instructions.index("`wait` already gives the outcome")
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

    def test_it_says_a_clearance_must_carry_its_commit(self):
        self.assertIn(
            "A clearance has to carry the commit it is about.",
            self.instructions,
        )
        self.assertIn(
            "a state file left behind by an earlier run answers `cleared` just as "
            "readily as one the current run wrote",
            self.instructions,
        )

    def test_it_says_a_repeat_is_not_fresh_evidence(self):
        self.assertIn(
            "A stage that gives the same answer at the same head it already gave "
            "there has told the pipeline nothing new.",
            self.instructions,
        )

    def test_it_rules_out_every_inferred_coverage_reference(self):
        self.assertIn(
            "Absence only means \"has not arrived yet\" for a check that was "
            "declared.",
            self.instructions,
        )
        self.assertIn(
            "Neither the base branch commit nor the pull request's own previous "
            "head says what this head is supposed to produce",
            self.instructions,
        )

    def test_it_says_every_guard_has_a_way_out(self):
        self.assertIn(
            "Every one of these guards has a way out, because a stage held not "
            "green by something that can never clear is a deadlock rather than a "
            "conservative failure.",
            self.instructions,
        )
        self.assertIn(
            "the wait ends in an escalation naming them rather than in silence",
            self.instructions,
        )

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

    def test_it_documents_the_single_subprocess_launch_path(self):
        self.assertIn("## The Launch Path", self.instructions)
        self.assertIn(
            "Every stage runs as a subprocess in this session's own worktree",
            self.instructions,
        )
        self.assertIn("There is no child-session path", self.instructions)
        self.assertIn("The pipeline owns the stage process, not you", self.instructions)
        self.assertNotIn("Child sessions.", self.instructions)

    def test_it_documents_the_model_gate(self):
        self.assertIn("## Model Gate", self.instructions)
        self.assertIn("models --pipeline-model", self.instructions)
        self.assertIn("fixed GPT-5.6 Sol evaluator", self.instructions)

    def test_it_documents_the_stage_logs_and_detail(self):
        self.assertIn("## Stage Logs", self.instructions)
        self.assertIn(
            "combined output goes to the log file at the `log_path`",
            self.instructions,
        )
        self.assertIn(
            "read at most the last 100 lines of the log", self.instructions
        )
        self.assertIn(
            "the sentence you write there is the only answer the report gives",
            self.instructions,
        )
        # Nothing may reintroduce per-stage sessions.
        self.assertNotIn("archive_session", self.instructions)
        self.assertNotIn("keep_session", self.instructions)

    def test_it_says_a_resumed_state_file_starts_a_new_run(self):
        self.assertIn("Resuming a state file starts a **new run**", self.instructions)
        self.assertIn("The loop below never returns to it", self.instructions)
        self.assertIn(
            "`--detail` is **required** for `no_progress` and `escalated`",
            self.instructions,
        )

    def test_it_says_an_unconfirmable_clearance_is_not_progress(self):
        self.assertIn(
            "A clearance the pipeline cannot see is not progress", self.instructions
        )
        self.assertIn("relaunched for ever, a push at a time", self.instructions)
        self.assertIn("clearance_confirmed", self.instructions)

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


class StageLogPathTest(unittest.TestCase):
    def test_the_path_carries_the_pull_request_stage_and_iteration(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                MODULE, "copilot_home", return_value=Path(directory)
            ):
                path = MODULE.stage_log_path(
                    {"owner": "octo", "repo": "hello", "number": 42},
                    MODULE.STAGE_CI,
                    3,
                )
        self.assertEqual(
            f"octo--hello--42--{MODULE.STAGE_CI}--3.log", path.name
        )
        self.assertEqual(("run", "pr-pipeline", "logs"), path.parts[-4:-1])

    def test_a_rerun_at_a_higher_iteration_keeps_its_own_log(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                MODULE, "copilot_home", return_value=Path(directory)
            ):
                first = MODULE.stage_log_path(
                    {"owner": "o", "repo": "r", "number": 1}, MODULE.STAGE_CI, 1
                )
                second = MODULE.stage_log_path(
                    {"owner": "o", "repo": "r", "number": 1}, MODULE.STAGE_CI, 2
                )
        self.assertNotEqual(first, second)


class LaunchPlanLogTest(unittest.TestCase):
    def test_the_plan_names_a_log_and_carries_the_permission_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                MODULE, "copilot_home", return_value=Path(directory)
            ):
                plan = MODULE.launch_plan(build_state(), MODULE.STAGE_CONFLICT)
        self.assertIn("log_path", plan)
        self.assertTrue(plan["log_path"].endswith(".log"))
        for flag in MODULE.STAGE_PERMISSION_FLAGS:
            self.assertIn(flag, plan["command"])

    def test_the_flag_set_stays_narrow(self):
        # The narrowest set proven to run a stage unattended is tools plus
        # paths; urls stay off because stages reach GitHub through the gh CLI.
        self.assertEqual(
            ("--allow-all-tools", "--allow-all-paths"),
            MODULE.STAGE_PERMISSION_FLAGS,
        )
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                MODULE, "copilot_home", return_value=Path(directory)
            ):
                plan = MODULE.launch_plan(build_state(), MODULE.STAGE_CONFLICT)
        self.assertNotIn("--allow-all-urls", plan["command"])
        self.assertNotIn("--allow-all", plan["command"])


class StartLogRoundTripTest(CommandTestCase):
    def test_start_records_the_log_path_on_running(self):
        path = write_state(self.root)
        MODULE.command_start(
            self.args(
                state=str(path),
                stage=MODULE.STAGE_CONFLICT,
                head=HEAD,
                launch="subprocess",
                session=None,
                process="4242",
                log="/tmp/some--stage.log",
            )
        )
        state = MODULE.load_state(path)
        self.assertEqual("/tmp/some--stage.log", state["running"]["log_path"])

    def test_an_old_state_file_with_a_session_launch_still_loads(self):
        # Old state files recorded launch=session; reading them must not break.
        path = write_state(
            self.root,
            running={
                "stage": MODULE.STAGE_CONFLICT,
                "head_sha": HEAD,
                "iteration": 1,
                "launch": "session",
                "session_id": "abc",
            },
        )
        state = MODULE.load_state(path)
        self.assertEqual("session", state["running"]["launch"])


class WaitCommandTest(CommandTestCase):
    def wait_args(self, path, **overrides):
        base = dict(
            state=str(path),
            stage=MODULE.STAGE_CI,
            pid="4242",
            process_create_time="100.0",
            timeout=None,
            poll=None,
        )
        base.update(overrides)
        return self.args(**base)

    def test_it_returns_the_outcome_only_after_the_process_exits(self):
        path = write_state(self.root)
        self.stage_reading = {"available": True, "outcome": "cleared"}
        with mock.patch.object(MODULE, "process_alive", return_value=False):
            with mock.patch.object(MODULE, "time") as clock:
                clock.monotonic.side_effect = [0.0, 0.0]
                MODULE.command_wait(self.wait_args(path))
        result = self.emitted[-1]
        self.assertEqual("finished", result["result"])
        self.assertEqual("cleared", result["outcome"])

    def test_a_process_gone_without_an_outcome_escalates(self):
        path = write_state(self.root)
        self.stage_reading = {"available": False, "reason": "not_reported"}
        with mock.patch.object(MODULE, "process_alive", return_value=False):
            with mock.patch.object(MODULE, "time") as clock:
                clock.monotonic.side_effect = [0.0, 0.0]
                MODULE.command_wait(self.wait_args(path))
        result = self.emitted[-1]
        self.assertEqual("escalate", result["result"])
        self.assertEqual("process_exited_without_outcome", result["reason"])

    def test_a_process_still_alive_at_the_ceiling_escalates(self):
        path = write_state(self.root)
        with mock.patch.object(MODULE, "process_alive", return_value=True):
            with mock.patch.object(MODULE, "time") as clock:
                # first sample under ceiling schedules a sleep, second is over it
                clock.monotonic.side_effect = [0.0, 0.0, 999999.0, 999999.0]
                clock.sleep.return_value = None
                MODULE.command_wait(self.wait_args(path, timeout="10"))
        result = self.emitted[-1]
        self.assertEqual("escalate", result["result"])
        self.assertEqual("wait_timeout_exceeded", result["reason"])

    def test_seeing_an_outcome_while_alive_is_not_a_reason_to_return(self):
        # The load-bearing ordering: exit is observed before the outcome is
        # read. A stage that has written its outcome but is still pushing must
        # not be returned, because the next stage would launch into the shared
        # worktree while this one is still mutating it. This reads like a
        # redundant liveness check; it is not, and must not be simplified away.
        path = write_state(self.root)
        self.stage_reading = {"available": True, "outcome": "cleared"}
        alive_states = [True, False]
        with mock.patch.object(
            MODULE, "process_alive", side_effect=lambda *_: alive_states.pop(0)
        ):
            with mock.patch.object(MODULE, "time") as clock:
                clock.monotonic.side_effect = [0.0, 0.0, 0.0, 0.0]
                clock.sleep.return_value = None
                MODULE.command_wait(self.wait_args(path))
        # It looped once while alive, then returned finished only after exit.
        self.assertEqual([], alive_states)
        self.assertEqual("finished", self.emitted[-1]["result"])


class ResetCommandTest(CommandTestCase):
    def reset_args(self, path):
        return self.args(state=str(path), stage=MODULE.STAGE_SELF_REVIEW)

    def make_dirty_repo(self) -> Path:
        """A real git repo dirtied the way a user or a stage would leave it.

        The gate keys on real `git status`, so a fake porcelain string cannot
        prove it protects the files on disk. This builds an actual repo with a
        committed tracked file, then modifies it, adds an untracked file, and
        drops a gitignored build artifact.
        """

        repo = Path(tempfile.mkdtemp(dir=self.root))

        def run_git(*argv):
            import subprocess

            subprocess.run(
                ["git", *argv], cwd=repo, check=True, capture_output=True, text=True
            )

        run_git("init", "-q")
        run_git("config", "user.email", "t@e.st")
        run_git("config", "user.name", "test")
        (repo / "tracked.txt").write_text("committed content\n", encoding="utf-8")
        (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
        run_git("add", "-A")
        run_git("commit", "-qm", "init")
        (repo / "tracked.txt").write_text("USER EDIT\n", encoding="utf-8")
        (repo / "user_notes.txt").write_text("scratch\n", encoding="utf-8")
        (repo / "build").mkdir()
        (repo / "build" / "out.class").write_text("artifact\n", encoding="utf-8")
        return repo

    def begun_state(self, **overrides) -> dict:
        """State as `begin_run` actually leaves it, not a hand-made literal.

        A fresh run has empty history and `iteration == 1`. A test that omits
        `iteration` or sets it to 0 hides the very bug the gate exists to catch,
        so the fixture is produced by the real code path instead.
        """

        state = build_state(**overrides)
        MODULE.begin_run(state)
        return state

    def test_dirt_before_any_stage_has_run_is_the_users_and_is_not_reset(self):
        # The load-bearing case: a fresh run with the user's own uncommitted
        # edits present. Misattributing this to a stage is unrecoverable data
        # loss, so the gate must refuse and leave every file untouched.
        state = self.begun_state()
        self.assertEqual(1, state["iteration"])  # what begin_run produces
        self.assertEqual([], state["history"])
        self.assertIsNone(state["running"])

        repo = self.make_dirty_repo()
        outcome = MODULE.ensure_clean_worktree_for_launch(state, repo)

        self.assertEqual("escalate", outcome["result"])
        self.assertEqual("dirty_worktree_before_run", outcome["reason"])
        # Assert on the artifact, not the verdict: the files must be intact.
        self.assertEqual(
            "USER EDIT\n", (repo / "tracked.txt").read_text(encoding="utf-8")
        )
        self.assertTrue((repo / "user_notes.txt").exists())

    def test_a_resumed_run_after_escalation_does_not_reset_the_users_fix(self):
        # The route my first fix missed: the pipeline escalated asking for a
        # human decision, the user edited the source to address it and has not
        # committed, then relaunched. Resuming mints a fresh run_id while the
        # prior run's history survives, so a history-based gate would call the
        # user's fix a stage's dirt and destroy the very change the escalation
        # asked for. Scoped to the current run, it must refuse.
        state = self.begun_state(
            history=[
                {"stage": MODULE.STAGE_CI, "outcome": "escalated", "run_id": "OLDRUN"}
            ]
        )
        # begin_run left a fresh run_id, cleared running, and kept the history.
        self.assertNotEqual("OLDRUN", state["run_id"])
        self.assertTrue(state["history"])
        self.assertIsNone(state["running"])

        repo = self.make_dirty_repo()
        outcome = MODULE.ensure_clean_worktree_for_launch(state, repo)

        self.assertEqual("escalate", outcome["result"])
        self.assertEqual("dirty_worktree_before_run", outcome["reason"])
        self.assertEqual(
            "USER EDIT\n", (repo / "tracked.txt").read_text(encoding="utf-8")
        )
        self.assertTrue((repo / "user_notes.txt").exists())

    def test_dirt_after_a_stage_finished_in_this_run_is_reset_on_a_real_repo(self):
        # A stage finished earlier in this same run, so its leftover dirt is the
        # pipeline's to clear. The gitignored build/ survives.
        state = self.begun_state()
        state["history"] = [
            {
                "stage": MODULE.STAGE_CONFLICT,
                "outcome": "cleared",
                "run_id": state["run_id"],
            }
        ]

        repo = self.make_dirty_repo()
        outcome = MODULE.ensure_clean_worktree_for_launch(state, repo)

        self.assertEqual("reset", outcome["result"])
        self.assertEqual(
            "committed content\n",
            (repo / "tracked.txt").read_text(encoding="utf-8"),
        )
        self.assertFalse((repo / "user_notes.txt").exists())
        self.assertTrue((repo / "build" / "out.class").exists())

    def test_finish_stamps_the_run_id_on_the_history_entry(self):
        # The gate scopes by run, so the history entry must carry the run it
        # belongs to. Without this stamp an old run's dirt is misattributed.
        running = {
            "stage": MODULE.STAGE_CONFLICT,
            "head_sha": HEAD,
            "iteration": 1,
            "launch": "subprocess",
            "process_id": "1",
            "model": MODULE.DEFAULT_STAGE_MODEL,
            "started_at": "2026-01-01T00:00:00Z",
        }
        path = write_state(
            self.root, run_id="RUN123", running=running, escalation=None
        )
        self.stage_says("skipped")
        MODULE.command_finish(
            self.args(
                state=str(path),
                stage=MODULE.STAGE_CONFLICT,
                outcome="skipped",
                head=HEAD,
                detail=None,
                session=None,
                process=None,
            )
        )
        state = MODULE.load_state(path)
        self.assertEqual("RUN123", state["history"][-1]["run_id"])

    def test_dirt_after_a_stage_has_run_is_reset_on_a_real_repo(self):
        # A stage is still recorded as running, so leftover dirt is the stage's
        # and is cleared. The gitignored build/ survives.
        state = self.begun_state()
        state["running"] = {"stage": MODULE.STAGE_CONFLICT, "head_sha": HEAD}

        repo = self.make_dirty_repo()
        outcome = MODULE.ensure_clean_worktree_for_launch(state, repo)

        self.assertIn(outcome["result"], ("reset",))
        self.assertEqual(
            "committed content\n",
            (repo / "tracked.txt").read_text(encoding="utf-8"),
        )
        self.assertFalse((repo / "user_notes.txt").exists())
        self.assertTrue((repo / "build" / "out.class").exists())

    def test_dirt_before_any_stage_has_run_is_the_users_and_escalates(self):
        path = write_state(self.root, history=[], iteration=1, running=None)
        with mock.patch.object(MODULE, "worktree_dirt", return_value=" M file.py"):
            MODULE.command_reset(self.reset_args(path))
        state = MODULE.load_state(path)
        self.assertEqual("escalated", self.emitted[-1]["result"])
        self.assertEqual("dirty_worktree_before_run", state["escalation"]["reason"])

    def test_dirt_after_a_stage_has_run_is_reset(self):
        path = write_state(
            self.root,
            iteration=1,
            run_id="RUNX",
            history=[{"stage": MODULE.STAGE_CONFLICT, "run_id": "RUNX"}],
        )
        dirt = iter([" M file.py", ""])
        with mock.patch.object(
            MODULE, "worktree_dirt", side_effect=lambda *_: next(dirt)
        ):
            with mock.patch.object(MODULE, "git", return_value="") as git:
                MODULE.command_reset(self.reset_args(path))
        self.assertEqual("ready", self.emitted[-1]["result"])
        self.assertTrue(self.emitted[-1]["reset"])
        calls = [tuple(call.args[1:]) for call in git.call_args_list]
        self.assertIn(("reset", "--hard", "HEAD"), calls)
        self.assertIn(("clean", "-fd"), calls)
        # Never -x: a gitignored build/ must survive for the warm-compile win.
        for call in git.call_args_list:
            self.assertNotIn("-x", call.args)

    def test_a_local_head_ahead_of_remote_escalates_instead_of_resetting(self):
        path = write_state(
            self.root, iteration=1, history=[{"stage": MODULE.STAGE_CONFLICT}]
        )
        self.local_head = MODULE.LOCAL_HEAD_AHEAD
        MODULE.command_reset(self.reset_args(path))
        state = MODULE.load_state(path)
        self.assertEqual("escalated", self.emitted[-1]["result"])
        self.assertEqual(
            MODULE.LOCAL_HEAD_ESCALATIONS[MODULE.LOCAL_HEAD_AHEAD],
            state["escalation"]["reason"],
        )

    def test_a_diverged_local_head_escalates_instead_of_resetting(self):
        path = write_state(
            self.root, iteration=1, history=[{"stage": MODULE.STAGE_CONFLICT}]
        )
        self.local_head = MODULE.LOCAL_HEAD_DIVERGED
        MODULE.command_reset(self.reset_args(path))
        state = MODULE.load_state(path)
        self.assertEqual("escalated", self.emitted[-1]["result"])
        self.assertEqual(
            MODULE.LOCAL_HEAD_ESCALATIONS[MODULE.LOCAL_HEAD_DIVERGED],
            state["escalation"]["reason"],
        )

    def test_a_clean_tree_is_ready_without_a_reset(self):
        path = write_state(
            self.root, iteration=1, history=[{"stage": MODULE.STAGE_CONFLICT}]
        )
        with mock.patch.object(MODULE, "worktree_dirt", return_value=""):
            MODULE.command_reset(self.reset_args(path))
        self.assertEqual("ready", self.emitted[-1]["result"])
        self.assertFalse(self.emitted[-1]["reset"])


class RecordedRepoRootTest(CommandTestCase):
    """One worktree per run, established once and read back everywhere."""

    def make_repo(self, name: str) -> Path:
        repo = self.root / name
        repo.mkdir()
        git_in(repo, "init", "-q", "-b", "main")
        git_in(repo, "config", "user.email", "t@e.st")
        git_in(repo, "config", "user.name", "test")
        git_in(repo, "commit", "-q", "--allow-empty", "-m", "base")
        return repo

    def test_no_subcommand_accepts_a_repo_root_flag(self):
        # Walked off the parser rather than listed here, so a flag added back to
        # any subcommand fails without the test having to name it.
        parser = MODULE.build_parser()
        subparsers = [
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        self.assertTrue(subparsers)
        offenders = []
        for action in subparsers:
            for name, sub in action.choices.items():
                for option in sub._actions:
                    if any(
                        flag.startswith("--repo-root") for flag in option.option_strings
                    ):
                        offenders.append(name)
        self.assertEqual([], offenders)

    def test_the_worktree_commands_read_the_recorded_root(self):
        # `reset`, `launch`, and `finish` are the three that act on a worktree.
        # Reading `args.repo_root` in any of them is the defect returning.
        for command in ("command_reset", "command_launch", "command_finish"):
            with self.subTest(command=command):
                source = inspect.getsource(getattr(MODULE, command))
                self.assertIn("recorded_repo_root(state)", source)
                self.assertNotIn("args.repo_root", source)

    def test_preflight_records_the_worktree_it_was_run_from(self):
        repo = self.make_repo("session")
        other = self.make_repo("elsewhere")
        path = self.preflight_state(repo)
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(repo.resolve(), Path(state["repo_root"]).resolve())
        self.assertNotEqual(other.resolve(), Path(state["repo_root"]).resolve())

    def test_a_state_without_a_recorded_root_refuses_to_guess(self):
        state = build_state()
        del state["repo_root"]
        with self.assertRaises(MODULE.WorkflowError) as caught:
            MODULE.recorded_repo_root(state)
        self.assertIn("repo_root", str(caught.exception))

    def test_a_stage_runs_in_the_recorded_worktree_not_the_callers(self):
        # The production topology: `preflight` ran in one worktree and the agent
        # invokes `launch` from another. The stage has to land in the recorded
        # one, or every guard on that tree inspects a tree nothing writes to.
        recorded = self.make_repo("recorded")
        caller = self.make_repo("caller")
        path = self.preflight_state(recorded)
        witness = self.root / "cwd.txt"
        program = (
            "import os, pathlib; "
            f"pathlib.Path({str(witness)!r}).write_text(os.getcwd(), encoding='utf-8')"
        )
        with self.in_directory(caller):
            MODULE.command_launch(
                self.args(
                    state=str(path),
                    log=str(self.root / "stage.log"),
                    command=[sys.executable, "-c", program],
                )
            )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not witness.is_file():
            time.sleep(0.05)
        self.assertTrue(witness.is_file(), "the stage process never reported a cwd")
        self.assertEqual(
            recorded.resolve(),
            Path(witness.read_text(encoding="utf-8")).resolve(),
        )
        self.assertEqual(
            recorded.resolve(), Path(self.emitted[-1]["repo_root"]).resolve()
        )


class FinishGuardFollowsTheRecordedRootTest(CommandTestCase):
    """The unpushed-commit guard must read the tree the stage wrote in."""

    def test_an_unpushed_commit_in_the_recorded_tree_refuses_the_ending(self):
        # Two trees, as in the live run. The stage's commit is in the recorded
        # one; the pipeline agent's own directory is a clean tree sitting on the
        # published head. A guard that followed the caller would see nothing.
        published = make_pull_request_remote(self.root)
        stage_tree = clone_for_pipeline(self.root, published["remote"], "stage")
        fetch_pr_head(stage_tree)
        git_in(stage_tree, "checkout", "-q", "--detach", published["pr_head"])
        caller = clone_for_pipeline(self.root, published["remote"], "caller")
        fetch_pr_head(caller)
        git_in(caller, "checkout", "-q", "--detach", published["pr_head"])

        with mock.patch.object(
            MODULE, "target_remote_head", return_value=published["pr_head"]
        ):
            path = self.preflight_state(stage_tree)
            (stage_tree / "fix.txt").write_text("the stage's work\n", encoding="utf-8")
            git_in(stage_tree, "add", "fix.txt")
            git_in(stage_tree, "commit", "-q", "-m", "fix, never pushed")
            unpushed = git_in(stage_tree, "rev-parse", "HEAD")
            MODULE.command_start(
                self.args(
                    state=str(path),
                    stage=MODULE.STAGE_SELF_REVIEW,
                    head=published["pr_head"],
                    launch="subprocess",
                    session=None,
                    process=None,
                    log=None,
                )
            )
            with (
                mock.patch.object(
                    MODULE, "diagnose_local_head", REAL_DIAGNOSE_LOCAL_HEAD
                ),
                self.in_directory(caller),
            ):
                MODULE.command_finish(
                    self.args(
                        state=str(path),
                        stage=MODULE.STAGE_SELF_REVIEW,
                        outcome=MODULE.CLEARING_OUTCOMES[0],
                        head=published["pr_head"],
                        detail=None,
                        session=None,
                        process=None,
                        commit=None,
                    )
                )

        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("escalated", self.emitted[-1]["result"])
        self.assertEqual(
            MODULE.LOCAL_HEAD_ESCALATIONS[MODULE.LOCAL_HEAD_AHEAD],
            state["escalation"]["reason"],
        )
        self.assertIn(published["pr_head"], state["escalation"]["detail"])
        # The commit the guard was protecting is still there.
        self.assertEqual(unpushed, git_in(stage_tree, "rev-parse", "HEAD"))

    def test_a_stale_api_head_does_not_halt_a_stage_that_did_push(self):
        # The normal cycle: the stage pushed and finished at once, so the API
        # still serves the old head. `ls-remote` reads the ref itself and says
        # the commits are published, so the ending is recorded.
        published = make_pull_request_remote(self.root)
        stage_tree = clone_for_pipeline(self.root, published["remote"], "stage")
        fetch_pr_head(stage_tree)
        git_in(stage_tree, "checkout", "-q", "--detach", published["pr_head"])

        with mock.patch.object(
            MODULE, "target_remote_head", return_value=published["pr_head"]
        ):
            path = self.preflight_state(stage_tree)
            git_in(stage_tree, "commit", "-q", "--allow-empty", "-m", "pushed fix")
            pushed = git_in(stage_tree, "rev-parse", "HEAD")
            git_in(
                stage_tree,
                "push",
                "-q",
                "origin",
                f"HEAD:refs/pull/{base_pr()['number']}/head",
            )
            MODULE.command_start(
                self.args(
                    state=str(path),
                    stage=MODULE.STAGE_SELF_REVIEW,
                    head=pushed,
                    launch="subprocess",
                    session=None,
                    process=None,
                    log=None,
                )
            )
            with mock.patch.object(
                MODULE, "diagnose_local_head", REAL_DIAGNOSE_LOCAL_HEAD
            ):
                MODULE.command_finish(
                    self.args(
                        state=str(path),
                        stage=MODULE.STAGE_SELF_REVIEW,
                        outcome=MODULE.CLEARING_OUTCOMES[0],
                        head=pushed,
                        detail=None,
                        session=None,
                        process=None,
                        commit=None,
                    )
                )

        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("recorded", self.emitted[-1]["result"])
        self.assertIsNone(state.get("escalation"))


class ClassifyLocalHeadTest(unittest.TestCase):
    """The verdict table, read off the source constants."""

    def test_the_fact_space_maps_to_the_source_verdicts(self):
        cases = [
            (
                "neither head could be read",
                {
                    "local_head": None,
                    "pr_head": "aaa",
                    "on_pr_branch": False,
                    "descends_from_pr_head": False,
                    "ahead_count": 0,
                    "unreachable_count": 0,
                },
                MODULE.LOCAL_HEAD_UNKNOWN,
            ),
            (
                "the pull request head is unreadable",
                {
                    "local_head": "aaa",
                    "pr_head": None,
                    "on_pr_branch": True,
                    "descends_from_pr_head": True,
                    "ahead_count": 3,
                    "unreachable_count": 3,
                },
                MODULE.LOCAL_HEAD_UNKNOWN,
            ),
            (
                "already on the pull request head",
                {
                    "local_head": "aaa",
                    "pr_head": "aaa",
                    "on_pr_branch": True,
                    "descends_from_pr_head": True,
                    "ahead_count": 0,
                    "unreachable_count": 0,
                },
                MODULE.LOCAL_HEAD_AT_PR_HEAD,
            ),
            (
                "a stage committed without pushing, on the branch",
                {
                    "local_head": "bbb",
                    "pr_head": "aaa",
                    "on_pr_branch": True,
                    "descends_from_pr_head": True,
                    "ahead_count": 1,
                    "unreachable_count": 0,
                },
                MODULE.LOCAL_HEAD_AHEAD,
            ),
            (
                "a stage committed without pushing, detached",
                {
                    "local_head": "bbb",
                    "pr_head": "aaa",
                    "on_pr_branch": False,
                    "descends_from_pr_head": True,
                    "ahead_count": 1,
                    "unreachable_count": 1,
                },
                MODULE.LOCAL_HEAD_AHEAD,
            ),
            (
                "detached work the pull request head moved out from under",
                # Ancestry is broken, the branch says nothing, and the commits
                # exist only under HEAD. Without the reachability arm this falls
                # through to a checkout and the commits become garbage.
                {
                    "local_head": "bbb",
                    "pr_head": "aaa",
                    "on_pr_branch": False,
                    "descends_from_pr_head": False,
                    "ahead_count": 2,
                    "unreachable_count": 2,
                },
                MODULE.LOCAL_HEAD_UNREACHABLE,
            ),
            (
                "detached at a commit another ref still holds",
                # The false positive to avoid: a worktree parked on `main` has
                # commits absent from the pull request head, but every one of
                # them is reachable, so starting here is safe.
                {
                    "local_head": "bbb",
                    "pr_head": "aaa",
                    "on_pr_branch": False,
                    "descends_from_pr_head": False,
                    "ahead_count": 40,
                    "unreachable_count": 0,
                },
                MODULE.LOCAL_HEAD_NEEDS_CHECKOUT,
            ),
            (
                "on the pull request branch, but the histories parted",
                {
                    "local_head": "bbb",
                    "pr_head": "aaa",
                    "on_pr_branch": True,
                    "descends_from_pr_head": False,
                    "ahead_count": 7,
                    "unreachable_count": 0,
                },
                MODULE.LOCAL_HEAD_DIVERGED,
            ),
            (
                "a fresh session on its own branch, 7 ahead and 1 behind",
                {
                    "local_head": "bbb",
                    "pr_head": "aaa",
                    "on_pr_branch": False,
                    "descends_from_pr_head": False,
                    "ahead_count": 7,
                    "unreachable_count": 0,
                },
                MODULE.LOCAL_HEAD_NEEDS_CHECKOUT,
            ),
            (
                "simply behind the pull request head",
                {
                    "local_head": "bbb",
                    "pr_head": "aaa",
                    "on_pr_branch": True,
                    "descends_from_pr_head": False,
                    "ahead_count": 0,
                    "unreachable_count": 0,
                },
                MODULE.LOCAL_HEAD_NEEDS_CHECKOUT,
            ),
        ]
        for label, facts, expected in cases:
            with self.subTest(label):
                self.assertEqual(expected, MODULE.classify_local_head(**facts))

    def test_only_the_unsafe_verdicts_escalate(self):
        # Derived from the source map, so a verdict that gains or loses an
        # escalation has to be stated in the source, not in the test.
        self.assertEqual(
            {
                MODULE.LOCAL_HEAD_AHEAD,
                MODULE.LOCAL_HEAD_UNREACHABLE,
                MODULE.LOCAL_HEAD_DIVERGED,
            },
            set(MODULE.LOCAL_HEAD_ESCALATIONS),
        )
        for verdict, reason in MODULE.LOCAL_HEAD_ESCALATIONS.items():
            with self.subTest(verdict=verdict):
                self.assertIn(reason, MODULE.ESCALATION_ACTIONS)


class ResetAgainstRealRepositoriesTest(CommandTestCase):
    """`reset` decided against real branches, real ancestry, and real dirt."""

    def setUp(self):
        super().setUp()
        self.published = make_pull_request_remote(self.root)
        self.remote_repo = self.published["remote"]
        # The API's answer, which a test moves when it wants the head to lag.
        self.api_head = self.published["pr_head"]
        self.remote_head = mock.patch.object(
            MODULE, "target_remote_head", side_effect=lambda *_: self.api_head
        )
        self.remote_head.start()
        self.addCleanup(self.remote_head.stop)
        self.local = clone_for_pipeline(self.root, self.published["remote"], "session")
        self.diagnosis = mock.patch.object(
            MODULE, "diagnose_local_head", REAL_DIAGNOSE_LOCAL_HEAD
        )
        self.diagnosis.start()
        self.addCleanup(self.diagnosis.stop)

    def reset_from_state(self, **overrides):
        path = self.preflight_state(self.local)
        state = json.loads(path.read_text(encoding="utf-8"))
        state.update(overrides)
        path.write_text(json.dumps(state), encoding="utf-8")
        MODULE.command_reset(
            self.args(state=str(path), stage=MODULE.STAGE_SELF_REVIEW)
        )
        return path

    def head_of_local(self) -> str:
        return git_in(self.local, "rev-parse", "HEAD")

    def publish_a_new_head(self) -> str:
        """Move the pull request head on the remote, as a push from elsewhere does."""

        git_in(self.remote_repo, "checkout", "-q", base_pr()["head_branch"])
        git_in(self.remote_repo, "commit", "-q", "--allow-empty", "-m", "moved on")
        moved = git_in(self.remote_repo, "rev-parse", "HEAD")
        git_in(
            self.remote_repo, "update-ref", f"refs/pull/{base_pr()['number']}/head", moved
        )
        git_in(self.remote_repo, "checkout", "-q", "main")
        return moved

    def test_detached_work_the_head_moved_out_from_under_is_never_orphaned(self):
        # Ancestry cannot save this one: the pull request head moved, so it is no
        # longer an ancestor, and a detached worktree is on no branch. Only
        # reachability says the commits would be lost.
        fetch_pr_head(self.local)
        git_in(self.local, "checkout", "-q", "--detach", self.published["pr_head"])
        git_in(self.local, "commit", "-q", "--allow-empty", "-m", "stage work")
        stranded = self.head_of_local()
        moved = self.publish_a_new_head()
        self.api_head = moved
        path = self.reset_from_state(
            iteration=1, history=[{"stage": MODULE.STAGE_CONFLICT}]
        )
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("escalated", self.emitted[-1]["result"])
        self.assertEqual(
            MODULE.LOCAL_HEAD_ESCALATIONS[MODULE.LOCAL_HEAD_UNREACHABLE],
            state["escalation"]["reason"],
        )
        self.assertEqual(stranded, self.head_of_local())
        # The object is still there, which is the whole point of refusing.
        self.assertTrue(
            git_succeeds_in(self.local, "cat-file", "-e", stranded),
            "the commit the pipeline refused to orphan is gone",
        )

    def test_detached_at_a_commit_another_ref_holds_starts_normally(self):
        # The false positive to avoid. A worktree parked on `main` holds commits
        # the pull request head does not, but `origin/main` keeps every one of
        # them, so there is nothing to lose and nothing to escalate.
        for index in range(3):
            git_in(self.remote_repo, "commit", "-q", "--allow-empty", "-m", f"main {index}")
        git_in(self.local, "fetch", "-q", "origin", "main")
        git_in(self.local, "checkout", "-q", "--detach", "origin/main")
        # The arm under test only means something if these commits really are
        # absent from the pull request head, which is what makes a count-based
        # rule escalate here.
        self.assertGreater(
            MODULE.commit_count(self.local, self.published["pr_head"], "HEAD"), 0
        )
        self.assertEqual(0, MODULE.unreachable_commit_count(self.local))
        self.reset_from_state(iteration=1, history=[{"stage": MODULE.STAGE_CONFLICT}])
        self.assertEqual("ready", self.emitted[-1]["result"])
        self.assertEqual(
            MODULE.LOCAL_HEAD_NEEDS_CHECKOUT, self.emitted[-1]["local_head"]
        )
        self.assertTrue(self.emitted[-1]["checked_out"])
        self.assertEqual(self.published["pr_head"], self.head_of_local())

    def test_a_stale_api_head_is_corrected_from_the_ref_instead_of_halting(self):
        # A stage pushes and finishes at once, so `gh pr view` still serves the
        # old head. The commits are published; halting here would stop a healthy
        # run on its normal cycle.
        fetch_pr_head(self.local)
        git_in(self.local, "checkout", "-q", "--detach", self.published["pr_head"])
        git_in(self.local, "commit", "-q", "--allow-empty", "-m", "pushed by the stage")
        pushed = self.head_of_local()
        git_in(
            self.local,
            "push",
            "-q",
            "origin",
            f"HEAD:refs/pull/{base_pr()['number']}/head",
        )
        self.reset_from_state(iteration=1, history=[{"stage": MODULE.STAGE_CONFLICT}])
        self.assertEqual("ready", self.emitted[-1]["result"])
        self.assertEqual(MODULE.LOCAL_HEAD_AT_PR_HEAD, self.emitted[-1]["local_head"])
        self.assertEqual(pushed, self.emitted[-1]["head_sha"])
        self.assertEqual(pushed, self.head_of_local())

    def test_a_ref_that_agrees_the_commit_is_absent_still_escalates(self):
        # The other direction: `ls-remote` confirms the commit is not published,
        # so the escalation stands and the work is kept.
        fetch_pr_head(self.local)
        git_in(self.local, "checkout", "-q", "--detach", self.published["pr_head"])
        git_in(self.local, "commit", "-q", "--allow-empty", "-m", "never pushed")
        unpushed = self.head_of_local()
        self.assertEqual(
            self.published["pr_head"],
            MODULE.remote_pull_request_head(
                self.local, MODULE.build_target("owner", "repo", 7)
            ),
        )
        path = self.reset_from_state(
            iteration=1, history=[{"stage": MODULE.STAGE_CONFLICT}]
        )
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("escalated", self.emitted[-1]["result"])
        self.assertEqual(
            MODULE.LOCAL_HEAD_ESCALATIONS[MODULE.LOCAL_HEAD_AHEAD],
            state["escalation"]["reason"],
        )
        self.assertEqual(unpushed, self.head_of_local())

    def test_a_session_on_its_own_branch_is_checked_out_and_started(self):
        # The reproduction: a new session worktree on a branch of its own,
        # unrelated to the pull request. Refusing here is what blocked normal
        # use, so the pipeline establishes the precondition instead.
        git_in(self.local, "checkout", "-q", "-b", "trask-refactored-invention")
        for index in range(7):
            git_in(self.local, "commit", "-q", "--allow-empty", "-m", f"own {index}")
        self.reset_from_state(
            iteration=1, history=[{"stage": MODULE.STAGE_CONFLICT}]
        )
        self.assertEqual("ready", self.emitted[-1]["result"])
        self.assertEqual(
            MODULE.LOCAL_HEAD_NEEDS_CHECKOUT, self.emitted[-1]["local_head"]
        )
        self.assertTrue(self.emitted[-1]["checked_out"])
        self.assertEqual(self.published["pr_head"], self.head_of_local())

    def test_a_worktree_already_detached_at_the_head_is_left_alone(self):
        fetch_pr_head(self.local)
        git_in(self.local, "checkout", "-q", "--detach", self.published["pr_head"])
        self.reset_from_state(
            iteration=1, history=[{"stage": MODULE.STAGE_CONFLICT}]
        )
        self.assertEqual("ready", self.emitted[-1]["result"])
        self.assertEqual(MODULE.LOCAL_HEAD_AT_PR_HEAD, self.emitted[-1]["local_head"])
        self.assertFalse(self.emitted[-1]["checked_out"])
        self.assertEqual(self.published["pr_head"], self.head_of_local())

    def test_a_commit_on_top_of_the_head_escalates_and_survives(self):
        fetch_pr_head(self.local)
        git_in(self.local, "checkout", "-q", "--detach", self.published["pr_head"])
        git_in(self.local, "commit", "-q", "--allow-empty", "-m", "unpushed stage work")
        unpushed = self.head_of_local()
        path = self.reset_from_state(
            iteration=1, history=[{"stage": MODULE.STAGE_CONFLICT}]
        )
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("escalated", self.emitted[-1]["result"])
        self.assertEqual(
            MODULE.LOCAL_HEAD_ESCALATIONS[MODULE.LOCAL_HEAD_AHEAD],
            state["escalation"]["reason"],
        )
        self.assertEqual(unpushed, self.head_of_local())

    def test_a_diverged_pull_request_branch_says_diverged_not_ahead(self):
        git_in(self.local, "checkout", "-q", "-b", base_pr()["head_branch"])
        git_in(self.local, "commit", "-q", "--allow-empty", "-m", "someone else's work")
        diverged = self.head_of_local()
        path = self.reset_from_state(
            iteration=1, history=[{"stage": MODULE.STAGE_CONFLICT}]
        )
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("escalated", self.emitted[-1]["result"])
        self.assertEqual(
            MODULE.LOCAL_HEAD_ESCALATIONS[MODULE.LOCAL_HEAD_DIVERGED],
            state["escalation"]["reason"],
        )
        self.assertIn("diverged", state["escalation"]["detail"])
        self.assertEqual(diverged, self.head_of_local())

    def test_dirt_before_any_stage_is_never_checked_out_over(self):
        # The provenance gate runs before the checkout, so a user's uncommitted
        # work in a session that is not on the pull request yet survives.
        git_in(self.local, "checkout", "-q", "-b", "trask-refactored-invention")
        (self.local / "user_notes.txt").write_text("mine\n", encoding="utf-8")
        before = self.head_of_local()
        path = self.reset_from_state(iteration=1, history=[], running=None)
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("escalated", self.emitted[-1]["result"])
        self.assertEqual("dirty_worktree_before_run", state["escalation"]["reason"])
        self.assertEqual(before, self.head_of_local())
        self.assertEqual(
            "mine\n", (self.local / "user_notes.txt").read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
