"""Formatter-only regressions using synthetic run results."""

import copy
from contextlib import redirect_stdout
import importlib.util
from io import BytesIO, TextIOWrapper
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location(
        f"stage_failure_reporting_{name}", SCRIPTS / f"{name}.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STACK = load_script("pr_stack_pipeline")
STANDALONE = load_script("pr_pipeline")
ERROR = "hosted review candidate manifest or decisions artifact is invalid"
STAGE = STACK.STAGE_COPILOT_REVIEW
RUN_ID = "1" * 32


def stage_result():
    return {
        "stage": STAGE,
        "clear": False,
        "outcome": "escalated",
        "reason": "escalated",
        "agent_task": {
            "status": "failed", "error": ERROR,
            "diagnostics": {"retained": ["synthetic evidence"] * 2000},
        },
    }


def stack_result():
    stage = stage_result()
    return {
        "result": "blocked", "reason": "stage_execution_failed",
        "detail": f"{STAGE} exited with code 1",
        "run_id": RUN_ID, "repository": "owner/repo",
        "stack_number": 77, "start_pull_request": 7, "selected": [7, 8],
        "phases": [{
            "phase": STAGE, "mode": STACK.PHASE_PARALLEL,
            "dispatches": 2, "accepted": [7, 8], "clear": False,
            "reasons": ["escalated"],
            "stopped": {
                "step": "stage_status", "number": 8, "stage": STAGE,
                "reason": "stage_execution_failed",
                "detail": f"{STAGE} exited with code 1", "stage_result": stage,
            },
        }],
        "pull_requests": {"8": {"stages": {STAGE: {"stage_result": stage}}}},
    }


def native_conflict_result():
    error = {"code": "stale_target", "message": "pull request target changed"}
    stage = {
        "stage": STACK.STAGE_CONFLICT, "clear": False,
        "outcome": "escalated", "reason": "escalated",
        "agent_task": {
            "status": "interrupted", "error": error, "task_id": "synthetic-task",
            "preflight": {"evidence": ["preflight-only evidence"] * 2000},
            "result": {
                "status": "error", "error": error,
                "task": {"id": "synthetic-task", "state": "completed"},
                "application": {"status": "not_started"},
                "generated": {"artifact": None, "code_refs": []},
            },
        },
    }
    return {
        "result": "blocked", "reason": "stage_execution_failed",
        "detail": f"{STACK.STAGE_CONFLICT} exited with code 1",
        "run_id": RUN_ID, "repository": "owner/repo",
        "stack_number": 77, "start_pull_request": 7, "selected": [7, 8], "passes": 0,
        "phases": [{
            "phase": STACK.STAGE_CONFLICT, "mode": STACK.PHASE_STACK_DISPATCH,
            "dispatches": 1, "accepted": [7], "ignored": [], "clear": False,
            "reasons": ["escalated"],
            "stopped": {
                "step": "stage_status", "number": 7, "stage": STACK.STAGE_CONFLICT,
                "reason": "stage_execution_failed",
                "detail": f"{STACK.STAGE_CONFLICT} exited with code 1",
                "stage_result": stage,
            },
        }],
    }


def captured_bytes(callback):
    with TextIOWrapper(BytesIO(), encoding="utf-8", newline=os.linesep) as output:
        with redirect_stdout(output):
            result = callback()
        output.flush()
        return result, output.buffer.getvalue()


class StageFailureReportingTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def stack_summary(self, payload):
        original = copy.deepcopy(payload)
        path = self.root / "result.json"
        pipeline = SimpleNamespace(result_path=path, run_id=RUN_ID, kickoff={})
        STACK.StackPipeline.persist_result(pipeline, payload)
        durable_bytes = path.read_bytes()
        compact = STACK.compact_terminal_result(payload, result_path=path)
        self.assertEqual(original, payload)
        self.assertEqual(original, json.loads(durable_bytes)["pipeline_result"])
        self.assertEqual(durable_bytes, path.read_bytes())
        self.assertEqual(str(path), compact["artifacts"]["result"])
        _, output = captured_bytes(
            lambda: STACK.common.emit({"event": "stack_pipeline_finished", **compact})
        )
        self.assertLessEqual(
            len(output), STACK.TERMINAL_RESULT_MAX_BYTES,
        )
        return compact

    def standalone_summary(self, stage):
        payload = {
            "event": "pipeline_finished", "result": "blocked",
            "reason": "stage_execution_failed", "detail": f"{STAGE} exited with code 1",
            "stage": STAGE, "run_id": RUN_ID, "number": 7, "stage_result": stage,
        }
        original = copy.deepcopy(payload)
        target = STANDALONE.build_target("owner", "repo", 7)
        path = self.root / "standalone.json"
        compact = STANDALONE.persist_terminal_result(payload, path)
        self.assertEqual(original, payload)
        self.assertEqual(original, json.loads(path.read_bytes()))
        self.assertLessEqual(
            STANDALONE.serialized_size(compact), STANDALONE.TERMINAL_RESULT_MAX_BYTES,
        )
        self.assertEqual(target["number"], compact["number"])
        self.assertEqual(original, json.loads(path.read_bytes()))
        self.assertLessEqual(
            STANDALONE.serialized_size(compact),
            STANDALONE.TERMINAL_RESULT_MAX_BYTES,
        )
        return compact

    def test_stack_retains_nested_task_error_without_replacing_safety_fields(self):
        payload = stack_result()
        result = self.stack_summary(payload)
        self.assertEqual(
            {"stage": STAGE, "number": 8, "error": ERROR}, result["stage_failure"],
        )
        for key in ("result", "reason", "detail"):
            self.assertEqual(payload[key], result[key])
        self.assertEqual(["escalated"], result["phases"][0]["reasons"])
        self.assertEqual("stage_execution_failed", result["phases"][0]["stopped"]["reason"])
        self.assertNotIn("all_ci_passed", result)
        self.assertNotIn("synthetic evidence", json.dumps(result))

    def test_native_conflict_structured_error_is_reported_by_both_summaries(self):
        payload = native_conflict_result()
        stage = payload["phases"][0]["stopped"]["stage_result"]
        stack = self.stack_summary(payload)
        standalone_stage = copy.deepcopy(stage)
        standalone_stage["status"] = {"agent_task": standalone_stage.pop("agent_task")}
        standalone = self.standalone_summary(standalone_stage)
        for result in (stack, standalone):
            self.assertEqual(
                "stale_target: pull request target changed", result["stage_failure"]["error"],
            )
            self.assertEqual(STACK.STAGE_CONFLICT, result["stage_failure"]["stage"])
            self.assertEqual("blocked", result["result"])
            self.assertEqual("stage_execution_failed", result["reason"])
            self.assertNotIn("all_ci_passed", result)
        self.assertEqual(payload["detail"], stack["detail"])
        self.assertEqual(7, stack["stage_failure"]["number"])
        self.assertEqual(0, stack["passes"])
        self.assertNotIn("preflight-only evidence", json.dumps(stack))
        self.assertNotIn("synthetic-task", json.dumps(stack))

    def test_large_native_structured_error_is_bounded_and_deterministic(self):
        for field in ("code", "message"):
            with self.subTest(field=field):
                payload = native_conflict_result()
                payload["phases"] *= 10
                stage = payload["phases"][-1]["stopped"]["stage_result"]
                error = stage["agent_task"]["error"]
                error[field] += "\U0001f680" * 2000 + "\x00\"\\\r\n" * 2000
                result = self.stack_summary(payload)
                repeated = STACK.compact_terminal_result(
                    payload, result_path=self.root / "result.json",
                )
                self.assertEqual(result, repeated)
                self.assertNotIn("phases", result)
                self.assertTrue(result["terminal_detail_omitted"])
                failure = result["stage_failure"]
                self.assertEqual(
                    f"{error['code']}: {error['message']}"[:509] + "...", failure["error"],
                )
                self.assertTrue(failure["error_details_truncated"])
                self.assertEqual(7, failure["number"])
                for key in ("result", "reason", "detail"):
                    self.assertEqual(payload[key], result[key])
                self.assertNotIn("preflight-only evidence", json.dumps(result))
                self.assertNotIn("synthetic-task", json.dumps(result))

    def test_stack_durable_result_keeps_the_complete_review_commit_sequence(self):
        code_shas = [
            "1111111111111111111111111111111111111111",
            "2222222222222222222222222222222222222222",
        ]
        output_sha = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        stage = stage_result()
        task = stage.pop("agent_task")
        task.update({
            "remote": {"ordered_commits": code_shas},
            "diagnostics": {"output_commit_sha": output_sha},
        })
        stage["status"] = {"agent_task": task}
        retained = STACK.stage_result_summary(stage)
        payload = stack_result()
        payload["phases"][0]["stopped"]["stage_result"] = retained
        payload["pull_requests"]["8"]["stages"][STAGE]["stage_result"] = retained
        compact = self.stack_summary(payload)
        saved = json.loads((self.root / "result.json").read_bytes())["pipeline_result"]
        for stored in (
            saved["phases"][0]["stopped"]["stage_result"],
            saved["pull_requests"]["8"]["stages"][STAGE]["stage_result"],
        ):
            self.assertEqual(code_shas, stored["agent_task"]["remote"]["ordered_commits"])
            self.assertNotIn(output_sha, stored["agent_task"]["remote"]["ordered_commits"])
            self.assertEqual(output_sha, stored["agent_task"]["diagnostics"]["output_commit_sha"])
        self.assertEqual(ERROR, compact["stage_failure"]["error"])
        self.assertNotIn(output_sha, json.dumps(compact))

    def test_other_safety_stop_keeps_precedence_over_task_error(self):
        payload = stack_result()
        payload.update({"reason": "source_changed", "detail": "Source changed during review."})
        result = self.stack_summary(payload)
        self.assertEqual("source_changed", result["reason"])
        self.assertEqual(payload["detail"], result["detail"])
        self.assertEqual(ERROR, result["stage_failure"]["error"])
        self.assertEqual("blocked", result["result"])

    def test_large_escaped_error_survives_dropping_phase_previews(self):
        payload = stack_result()
        payload["phases"] *= 10
        error = "\U0001f680" * 2000 + "\n\"retained suffix\""
        payload["phases"][-1]["stopped"]["stage_result"]["agent_task"]["error"] = error
        result = self.stack_summary(payload)
        self.assertTrue(result["terminal_detail_omitted"])
        self.assertNotIn("phases", result)
        failure = result["stage_failure"]
        self.assertEqual(error[:509] + "...", failure["error"])
        self.assertEqual(512, len(failure["error"]))
        self.assertTrue(failure["error_details_truncated"])
        self.assertEqual(8, failure["number"])
        self.assertEqual(payload["reason"], result["reason"])
        self.assertEqual(payload["detail"], result["detail"])

    def test_terminal_failure_is_not_lost_to_phase_collection_limit(self):
        payload = stack_result()
        last = payload["phases"][0]
        payload["phases"] = [
            {"phase": STAGE, "mode": STACK.PHASE_PARALLEL, "clear": True}
        ] * STACK.TERMINAL_RESULT_MAX_PHASES + [last]
        result = self.stack_summary(payload)
        self.assertEqual(ERROR, result["stage_failure"]["error"])

    def test_oversized_diagnostic_has_explicit_artifact_retrieval_flag(self):
        payload = stack_result()
        stage = payload["phases"][0]["stopped"]["stage_result"]
        stage["stage"] = "\U0001f680" * 2000
        stage["agent_task"]["error"] = "\U0001f680" * 2000
        result = self.stack_summary(payload)
        self.assertNotIn("stage_failure", result)
        self.assertTrue(result["stage_failure_omitted"])
        self.assertEqual(payload["reason"], result["reason"])
        self.assertEqual(payload["detail"], result["detail"])
        self.assertEqual("blocked", result["result"])

    def test_adversarial_diagnostic_and_metadata_fit_actual_event_bytes(self):
        for text in ("\U0001f680", "\x00\x1b", "\"\\\r\n\t", "\u2028\u2029"):
            with self.subTest(text=repr(text)):
                payload = stack_result()
                payload["detail"] = text * 40
                payload["session_title"] = text * 32
                payload["ci_warning_revalidation_error"] = text * 32
                stage = payload["phases"][0]["stopped"]["stage_result"]
                stage["stage"] = stage["agent_task"]["error"] = "retained:" + text * 2000
                result_path = "C:\\run\\" + text * 80 + "\\result.json"
                original = copy.deepcopy(payload)
                compact = STACK.compact_terminal_result(payload, result_path=result_path)
                _, output = captured_bytes(
                    lambda: STACK.common.emit({"event": "stack_pipeline_finished", **compact})
                )
                self.assertLessEqual(len(output), STACK.TERMINAL_RESULT_MAX_BYTES)
                self.assertTrue(output.endswith(os.linesep.encode("utf-8")))
                self.assertEqual(original, payload)
                self.assertEqual("blocked", compact["result"])
                self.assertEqual(payload["reason"], compact["reason"])
                self.assertEqual(payload["detail"], compact["detail"])
                self.assertEqual(result_path, compact["artifacts"]["result"])
                if "stage_failure" in compact:
                    self.assertTrue(compact["stage_failure"]["stage_details_truncated"])
                    self.assertTrue(compact["stage_failure"]["error_details_truncated"])
                else:
                    self.assertTrue(compact["stage_failure_omitted"])

    def test_unbounded_terminal_metadata_fails_reporting_without_touching_durable_result(self):
        for field in ("detail", "reason", "session_title", "ci_warning_revalidation_error"):
            with self.subTest(field=field):
                payload = stack_result()
                payload[field] = "\U0001f680" * 512
                stage = payload["phases"][0]["stopped"]["stage_result"]
                stage["stage"] = stage["agent_task"]["error"] = "\x00\U0001f680" * 2000
                result_path = "C:\\" + "\U0001f680" * 480 + "\\result.json"
                path = self.root / "result.json"
                pipeline = SimpleNamespace(result_path=path, run_id=RUN_ID, kickoff={})
                STACK.StackPipeline.persist_result(pipeline, payload)
                durable_bytes = path.read_bytes()

                def report(_args):
                    compact = STACK.compact_terminal_result(payload, result_path=result_path)
                    STACK.common.emit({"event": "stack_pipeline_finished", **compact})

                with (
                    mock.patch.object(STACK, "command_run", side_effect=report),
                    mock.patch.object(STACK.sys, "argv", ["pr_stack_pipeline.py", "run"]),
                ):
                    code, output = captured_bytes(STACK.main)
                self.assertEqual(1, code)
                self.assertLessEqual(len(output), STACK.TERMINAL_RESULT_MAX_BYTES)
                self.assertEqual(
                    {
                        "event": "stack_pipeline_finished", "result": "error",
                        "error": "terminal result metadata exceeds the output byte limit",
                    },
                    json.loads(output),
                )
                self.assertEqual(durable_bytes, path.read_bytes())
                self.assertEqual(payload, json.loads(durable_bytes)["pipeline_result"])

    def test_only_the_last_stopped_phase_supplies_the_failure(self):
        payload = stack_result()
        previous = copy.deepcopy(payload["phases"][0])
        previous["stopped"]["number"] = 7
        previous["stopped"]["stage_result"]["agent_task"]["error"] = "Historical error"
        payload["phases"].insert(0, previous)
        result = self.stack_summary(payload)
        self.assertEqual({"stage": STAGE, "number": 8, "error": ERROR}, result["stage_failure"])

    def test_clipped_stage_name_is_flagged_in_both_summaries(self):
        payload = stack_result()
        stage = payload["phases"][0]["stopped"]["stage_result"]
        stage["stage"] = "synthetic-stage-" * 50
        stack = self.stack_summary(payload)
        for summary in (stack, self.standalone_summary(stage)):
            self.assertTrue(summary["stage_failure"]["stage_details_truncated"])
            self.assertEqual(512, len(summary["stage_failure"]["stage"]))
            self.assertEqual(ERROR, summary["stage_failure"]["error"])

    def test_complete_results_do_not_promote_stale_task_errors(self):
        payload = stack_result()
        payload["result"] = "complete"
        result = self.stack_summary(payload)
        self.assertNotIn("stage_failure", result)
        self.assertNotIn(ERROR, json.dumps(result))
        stage = stage_result()
        stage["status"] = {"agent_task": stage.pop("agent_task")}
        standalone = STANDALONE.compact_terminal_result(
            {"result": "complete", "runs": [stage]},
            result_path=self.root / "standalone.json", result_sha256="0" * 64,
        )
        self.assertNotIn("stage_failure", standalone)
        self.assertNotIn(ERROR, json.dumps(standalone))

    def test_history_is_not_used_as_the_terminal_stage_failure(self):
        for terminal in (None, {"phase": STACK.STAGE_DESCRIPTION}, {"stopped": None}):
            with self.subTest(terminal=terminal):
                payload = stack_result()
                payload["phases"].append(terminal)
                result = self.stack_summary(payload)
                self.assertNotIn("stage_failure", result)
        payload = stack_result()
        payload["result"] = "complete"
        self.assertNotIn("stage_failure", self.stack_summary(payload))

    def test_malformed_stopped_and_stage_payloads_do_not_invent_diagnostics(self):
        malformed = (None, False, 17, "", "bad shape", [], ["error"], {})
        for field in ("stopped", "stage_result", "agent_task", "error"):
            for value in malformed:
                with self.subTest(field=field, value=value):
                    payload = stack_result()
                    stopped = payload["phases"][0]["stopped"]
                    if field == "stopped":
                        payload["phases"][0]["stopped"] = value
                    elif field == "stage_result":
                        stopped["stage_result"] = value
                    elif field == "agent_task":
                        stopped["stage_result"]["agent_task"] = value
                    else:
                        stopped["stage_result"]["agent_task"]["error"] = value
                    result = self.stack_summary(payload)
                    if field == "error" and value == "bad shape":
                        self.assertEqual(value, result["stage_failure"]["error"])
                    else:
                        self.assertNotIn("stage_failure", result)
                    self.assertEqual("blocked", result["result"])
                    self.assertEqual("stage_execution_failed", result["reason"])

    def test_malformed_phases_are_safe(self):
        for phases in (None, False, 17, "", "bad shape", {}, [None], [[]]):
            with self.subTest(phases=phases):
                payload = {**stack_result(), "phases": phases}
                self.assertNotIn("stage_failure", self.stack_summary(payload))

    def test_standalone_status_error_is_promoted_without_changing_safety(self):
        stage = stage_result()
        stage["status"] = {"agent_task": stage.pop("agent_task")}
        result = self.standalone_summary(stage)
        self.assertEqual({"stage": STAGE, "error": ERROR}, result["stage_failure"])
        self.assertEqual("stage_execution_failed", result["reason"])
        self.assertEqual(f"{STAGE} exited with code 1", result["detail"])
        self.assertEqual("blocked", result["result"])
        self.assertNotIn("all_ci_passed", result)

    def test_standalone_long_error_stays_visible_when_full_stage_preview_is_dropped(self):
        stage = stage_result()
        stage["status"] = {"agent_task": stage.pop("agent_task")}
        stage["status"]["agent_task"]["error"] = "\U0001f680" * 3000
        result = self.standalone_summary(stage)
        self.assertTrue(result["stage_result_omitted"])
        self.assertTrue(result["stage_failure"]["error_details_truncated"])
        self.assertEqual("\U0001f680" * 509 + "...", result["stage_failure"]["error"])

    def test_direct_task_error_precedes_status_and_generic_stage_reasons(self):
        stage = {
            **stage_result(), "detail": "Generic stage detail",
            "status": {"agent_task": {"error": "Other task error"}},
        }
        result = STACK.common.stage_failure_summary(stage)
        self.assertEqual(ERROR, result["error"])
        self.assertNotIn("Generic stage detail", json.dumps(result))
        stage["agent_task"]["error"] = {"code": "stale_target", "message": "Target changed"}
        self.assertEqual(
            "stale_target: Target changed", STACK.common.stage_failure_summary(stage)["error"],
        )
        stage["agent_task"]["error"] = " \n "
        self.assertEqual(
            "Other task error", STACK.common.stage_failure_summary(stage)["error"],
        )

    def test_structured_error_uses_only_nonempty_code_and_message_strings(self):
        for error, expected in (
            ({"code": "stale_target"}, "stale_target"),
            ({"message": "Target changed"}, "Target changed"),
            ({"code": " \n ", "message": "Target changed"}, "Target changed"),
            ({"code": "stale_target", "message": ["invalid"]}, "stale_target"),
            ({"code": False, "message": "Target changed"}, "Target changed"),
            ({"code": {"nested": "ignored"}, "message": "Target changed"}, "Target changed"),
            ({"code": "stale_target", "message": "Target changed", "detail": "ignored"},
             "stale_target: Target changed"),
        ):
            with self.subTest(error=error):
                self.assertEqual(
                    {"error": expected},
                    STACK.common.stage_failure_summary({"agent_task": {"error": error}}),
                )

    def test_malformed_status_and_errors_are_not_stringified_or_searched(self):
        for status in (None, False, 17, "", "bad shape", [], ["error"], {}):
            with self.subTest(status=status):
                self.assertEqual({}, STACK.common.stage_failure_summary({"status": status}))
        for error in (
            None, False, 17, "", " \n ", [], ["error"], {"detail": ERROR},
            {"code": 17, "message": []}, {"code": " \n ", "message": "\t"},
            {"error": {"code": "stale_target", "message": ERROR}},
        ):
            with self.subTest(error=error):
                stage = {
                    "reason": "escalated",
                    "status": {
                        "agent_task": {"error": error},
                        "history": [{"agent_task": {"error": ERROR}}],
                    },
                }
                self.assertEqual({}, STACK.common.stage_failure_summary(stage))
