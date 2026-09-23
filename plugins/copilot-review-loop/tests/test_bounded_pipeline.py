import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "copilot_review_loop.py"
SPEC = importlib.util.spec_from_file_location("review_bounded_test", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SESSION = "01234567-89ab-cdef-0123-456789abcdef"


class BoundedPipelineTest(unittest.TestCase):
    def setUp(self):
        self.path = Path.cwd() / "bounded-review-state.json"
        self.state = {}
        self.output = []
        self.target = MODULE.parse_target("owner/repo#7")
        self.args = SimpleNamespace(
            target="owner/repo#7", repo_root=str(Path.cwd()),
            state=str(self.path), pipeline_run="a" * 32,
            pipeline_iteration=1, pipeline_max_iterations=2,
            bounded_step=True, model="sol", max_iterations=5,
        )
        self.patches = [
            mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": SESSION}),
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=Path.cwd()),
            mock.patch.object(MODULE, "resolve_target", return_value=self.target),
            mock.patch.object(MODULE, "require_outside_repository"),
            mock.patch.object(MODULE.Path, "is_file", return_value=True),
            mock.patch.object(MODULE, "load_state", side_effect=lambda _: copy.deepcopy(self.state)),
            mock.patch.object(MODULE, "save_state", side_effect=lambda _, state: self.store(state)),
            mock.patch.object(MODULE, "emit", side_effect=self.emit),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def store(self, state):
        self.state = copy.deepcopy(state)

    def emit(self, payload):
        if MODULE._BOUNDED_CAPTURE:
            MODULE._BOUNDED_CAPTURE[-1].append(payload)
        else:
            self.output.append(payload)

    def test_review_request_waits_and_does_not_request_twice(self):
        def request(args):
            self.state["pr"] = {
                "pr_url": self.target["pr_url"], "number": 7,
                "upstream_owner": "owner", "upstream_repo": "repo",
                "base_sha": "base",
            }
            self.state["monitoring"] = {
                "status": "requested", "head_sha": "head",
                "baseline_review_id": 0, "copilot_bot_id": 1,
                "request_start": "2026-09-22T00:00:00Z",
            }
            MODULE.emit({"result": "review_requested", "state": str(self.path)})
            MODULE.bounded_review_wait(args, self.path, "review_request")

        with (
            mock.patch.object(MODULE, "command_agent_task", side_effect=request) as dispatch,
            mock.patch.object(MODULE, "metadata_for", return_value={"head_sha": "head"}),
            mock.patch.object(MODULE, "fetch_reviews", return_value=[]),
        ):
            MODULE.command_bounded_pipeline(self.args)
            MODULE.command_bounded_pipeline(self.args)
        self.assertEqual(1, dispatch.call_count)
        self.assertEqual(["waiting", "waiting"], [item["result"] for item in self.output])

    def test_review_clean_finalizes_after_wait(self):
        self.state = {
            "version": MODULE.STATE_VERSION, "created_at": "now",
            "iterations": 0, "history": [],
            "pr": {
                "pr_url": self.target["pr_url"], "number": 7,
                "upstream_owner": "owner", "upstream_repo": "repo",
                "base_sha": "base",
            },
            "monitoring": {
                "status": "requested", "head_sha": "head",
                "baseline_review_id": 0, "copilot_bot_id": 1,
                "request_start": "2026-09-22T00:00:00Z",
            },
        }
        review = {
            "id": 10, "state": "COMMENTED", "html_url": "https://github.com/example",
        }
        with (
            mock.patch.object(MODULE, "metadata_for", return_value={"head_sha": "head"}),
            mock.patch.object(MODULE, "fetch_reviews", return_value=[review]),
            mock.patch.object(MODULE, "matching_review", return_value=review),
            mock.patch.object(MODULE, "gh_paginated", return_value=[]),
            mock.patch.object(MODULE, "watcher_result", side_effect=self.complete_watch),
            mock.patch.object(MODULE, "stage_outcome", return_value="cleared"),
            mock.patch.object(MODULE, "command_agent_task") as dispatch,
        ):
            MODULE.command_bounded_pipeline(self.args)
            self.assertEqual("loop_completed", self.output[-1]["result"])
            MODULE.command_bounded_pipeline(self.args)
            dispatch.assert_not_called()
            self.assertEqual(self.output[-1], self.output[-2])

    def test_review_comments_wait_for_feedback_propagation(self):
        self.state = {
            "version": MODULE.STATE_VERSION, "created_at": "now",
            "iterations": 0, "history": [],
            "pr": {
                "pr_url": self.target["pr_url"], "number": 7,
                "upstream_owner": "owner", "upstream_repo": "repo",
                "base_sha": "base",
            },
            "monitoring": {
                "status": "requested", "head_sha": "head",
                "baseline_review_id": 0, "copilot_bot_id": 1,
                "request_start": "2026-09-22T00:00:00Z",
            },
        }
        review = {"id": 10, "state": "COMMENTED", "html_url": "https://github.com/example"}
        with (
            mock.patch.object(MODULE, "metadata_for", return_value={"head_sha": "head"}),
            mock.patch.object(MODULE, "fetch_reviews", return_value=[review]),
            mock.patch.object(MODULE, "matching_review", return_value=review),
            mock.patch.object(MODULE, "gh_paginated", return_value=[{"id": 17}]),
            mock.patch.object(MODULE, "watcher_result", side_effect=self.complete_watch),
            mock.patch.object(MODULE, "command_agent_task") as dispatch,
        ):
            MODULE.command_bounded_pipeline(self.args)
        self.assertEqual("waiting", self.output[-1]["result"])
        self.assertEqual("review_feedback", self.output[-1]["reason"])
        dispatch.assert_not_called()

    def complete_watch(self, state, result):
        state["monitoring"]["status"] = "completed"
        state["monitoring"]["result"] = result
        return result

    def test_wrong_session_and_run_cannot_observe(self):
        with mock.patch.object(MODULE, "command_agent_task",
                               side_effect=lambda args: MODULE.bounded_review_wait(
                                   args, self.path, "hosted_decision"
                               )):
            MODULE.command_bounded_pipeline(self.args)
            self.args.pipeline_run = "b" * 32
            with self.assertRaisesRegex(MODULE.WorkflowError, "another session or run"):
                MODULE.command_bounded_pipeline(self.args)
            self.args.pipeline_run = "a" * 32
            with mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": "fedcba98-7654-3210-fedc-ba9876543210"}):
                with self.assertRaisesRegex(MODULE.WorkflowError, "another session or run"):
                    MODULE.command_bounded_pipeline(self.args)

    def test_waiting_bypasses_terminal_clearance_but_final_result_does_not(self):
        self.args.command = "pipeline"
        with (
            mock.patch.object(MODULE, "build_parser") as parser,
            mock.patch.object(MODULE, "command_pipeline",
                              side_effect=lambda args: (
                                  setattr(args, "_bounded_waiting", True),
                                  MODULE.emit({"result": "waiting"}),
                              )),
            mock.patch.object(MODULE, "require_terminal_agent_task_clearance") as clearance,
        ):
            parser.return_value.parse_args.return_value = self.args
            self.assertEqual(0, MODULE.main())
            clearance.assert_not_called()
        self.args._bounded_waiting = False
        with (
            mock.patch.object(MODULE, "build_parser") as parser,
            mock.patch.object(MODULE, "command_pipeline",
                              side_effect=lambda _: MODULE.emit({"result": "loop_completed"})),
            mock.patch.object(MODULE, "require_terminal_agent_task_clearance") as clearance,
        ):
            parser.return_value.parse_args.return_value = self.args
            self.assertEqual(0, MODULE.main())
            clearance.assert_called_once_with(self.args)

    def test_hosted_observations_wait_without_sleep_then_validate_final_result(self):
        statuses = iter(("pending", "pending", "success"))
        current = {"status": "pending"}
        prompt_path = Path.cwd() / "prompt.txt"
        result_path = Path.cwd() / "result.json"
        helper = Path.cwd() / "cloud_task.py"
        decision = Path.cwd() / "decisions.json"
        canonical = Path.cwd() / "canonical.json"
        source = {"head": "source"}
        github = {"head": "source"}

        def pending():
            return {
                "schema": MODULE.CANDIDATE_AGENT_TASK_RESULT_SCHEMA,
                "status": "pending", "task": {"id": "task"},
                "pipeline": {
                    "run_id": "b" * 32, "session_id": SESSION,
                    "request_id": "request",
                },
                "candidate": None, "completion": None,
            }

        def run_worker(command, **options):
            current["status"] = next(statuses)
            self.assertIn("--pipeline-run", command)
            self.assertIn("--apply-with-report", command)
            self.assertIsNone(options["timeout"])
            output = (
                json.dumps(pending())
                if current["status"] == "pending" else ""
            )
            if MODULE._EXECUTION is not None:
                self.assertIs(options["require_execution"], True)
                MODULE._EXECUTION.children.append(SimpleNamespace(terminal_result={
                    "exit_code": 0, "local_status": "finished",
                    "workflow_result": (
                        pending() if current["status"] == "pending"
                        else {"status": "success", "task": {"id": "task"}}
                    ),
                }))
                output = "not-json"
            return subprocess.CompletedProcess(command, 0, output, "")

        def read_text(path, **_):
            if path == prompt_path:
                return f"Copilot Review Loop hosted worker prompt version {MODULE.WORKER_PROMPT_VERSION}.\n\nbody"
            return json.dumps({"status": current["status"], "task": {"id": "task"}})

        remote = {"generated_head": "head", "commits": [], "requires_apply": False}
        with (
            mock.patch.object(MODULE, "load_candidate_runtime", return_value=object()),
            mock.patch.object(
                MODULE.Path, "is_file",
                side_effect=lambda: current["status"] == "success",
            ),
            mock.patch.object(MODULE.Path, "read_text", autospec=True, side_effect=read_text),
            mock.patch.object(MODULE, "sha256_file", side_effect=lambda path: (
                MODULE.REQUIRED_CLOUD_TASK_SHA256 if path == helper else "digest"
            )),
            mock.patch.object(MODULE, "run_owned_local_worker", side_effect=run_worker) as worker,
            mock.patch.object(MODULE, "local_source_fingerprint", return_value=source),
            mock.patch.object(MODULE, "local_source_owner_fingerprint", side_effect=lambda item: item),
            mock.patch.object(MODULE, "github_decision_fingerprint", return_value=github),
            mock.patch.object(MODULE, "load_agent_task_result", return_value={"status": "success"}),
            mock.patch.object(MODULE, "validate_hosted_candidate", return_value=(remote, {})) as verify,
            mock.patch.object(MODULE, "git", return_value="decisions"),
            mock.patch.object(MODULE, "atomic_write_text"),
            mock.patch.object(MODULE, "validate_copilot_review_report", return_value={"comments": []}),
            mock.patch.object(MODULE, "render_canonical_review_report", return_value="canonical"),
        ):
            args = dict(
                repo_root=Path.cwd(), target=self.target,
                preflight={"pr": {"pr_url": self.target["pr_url"]}},
                prompt_path=prompt_path, result_path=result_path, decision_path=decision,
                canonical_path=canonical, run_id="b" * 32, requested_model="sol",
                before_source=source, before_github=github,
                helper=helper, timeout=7200,
            )
            self.assertEqual({"status": "pending"}, MODULE.run_hosted_decision_worker(
                **args, bounded_action="--pipeline-dispatch",
            ))
            with mock.patch.object(MODULE, "_EXECUTION", SimpleNamespace(children=[])):
                self.assertEqual({"status": "pending"}, MODULE.run_hosted_decision_worker(
                    **args, bounded_action="--pipeline-observe",
                ))
            with mock.patch.object(MODULE, "_EXECUTION", SimpleNamespace(children=[])):
                self.assertEqual("success", MODULE.run_hosted_decision_worker(
                    **args, bounded_action="--pipeline-observe",
                )["result"]["status"])
            self.assertEqual("success", MODULE.run_hosted_decision_worker(
                **args, bounded_action="--pipeline-observe",
            )["result"]["status"])
            self.assertEqual(3, worker.call_count)
            self.assertEqual(2, verify.call_count)

    def test_hosted_dispatch_reports_sealed_helper_failure_when_streams_are_empty(self):
        prompt_path = Path.cwd() / "prompt.txt"
        child = SimpleNamespace(terminal_result={
            "exit_code": 2,
            "local_status": "failed",
            "workflow_result": {
                "status": "error",
                "error": {
                    "message": "start Agent Task failed with HTTP 400: model not enabled",
                },
            },
        })
        execution = SimpleNamespace(children=[])

        def run_worker(command, **options):
            self.assertTrue(options["require_execution"])
            execution.children.append(child)
            return subprocess.CompletedProcess(command, 2, "", "")

        with (
            mock.patch.object(MODULE, "_EXECUTION", execution),
            mock.patch.object(MODULE, "load_candidate_runtime", return_value=object()),
            mock.patch.object(MODULE.Path, "read_text", return_value=(
                f"Copilot Review Loop hosted worker prompt version {MODULE.WORKER_PROMPT_VERSION}.\n\nbody"
            )),
            mock.patch.object(MODULE, "sha256_file", return_value="digest"),
            mock.patch.object(MODULE, "run_owned_local_worker", side_effect=run_worker),
        ):
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "HTTP 400: model not enabled",
            ):
                MODULE.run_hosted_decision_worker(
                    repo_root=Path.cwd(), target=self.target,
                    preflight={"pr": {"pr_url": self.target["pr_url"]}},
                    prompt_path=prompt_path, result_path=Path.cwd() / "result.json",
                    decision_path=Path.cwd() / "decisions.json",
                    canonical_path=Path.cwd() / "canonical.json",
                    run_id="b" * 32, requested_model="sol",
                    before_source={}, before_github={},
                    helper=Path.cwd() / "cloud_task.py", timeout=7200,
                    bounded_action="--pipeline-dispatch",
                )

    def test_completed_hosted_observation_imports_with_saved_prompt(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "bounded-review-state.json"
        self.args.state = str(self.path)
        run_id = "b" * 32
        prompt_path = self.path.with_name(
            f"{self.path.stem}--{run_id}--hosted-decision-prompt.txt"
        )
        helper = Path.cwd() / "cloud_task.py"
        prompt = f"Copilot Review Loop hosted worker prompt version {MODULE.WORKER_PROMPT_VERSION}.\n\nsaved request"
        prompt_path.write_text(prompt, encoding="utf-8")
        file_digest = MODULE.sha256_file
        pr = {
            "pr_url": self.target["pr_url"], "number": 7, "title": "Review",
            "head_sha": "head", "base_sha": "base",
        }
        preflight = {
            "pr": pr, "identity": {"branch": "review"},
            "comments": [], "head_review_clean": False,
        }
        source = {"head": "head"}
        github = {"head": "head"}
        self.state = {
            "version": MODULE.STATE_VERSION, "created_at": "now",
            "iterations": 0, "history": [], "pr": pr,
            "agent_task": {
                "status": "bounded_pending", "run_id": run_id,
                "preflight": preflight, "helper": str(helper),
                "prompt_sha256": file_digest(prompt_path),
                "source_before": source, "github_before": github,
            },
        }
        remote = {
            "commits": [], "final_local_head": "head", "requires_apply": False,
            "task_id": "task", "task_url": "https://github.com/task",
            "generated_branch": "generated", "generated_head": "head",
            "report_path": "report.md", "report_sha256": "report-digest",
        }
        bundle = {
            "result": {"completion": {}, "candidate": {}, "task": {"id": "task"}},
            "remote": remote, "report": {"comments": []},
            "report_content": "report", "paths_by_commit": {},
        }
        with (
            mock.patch.object(MODULE, "sha256_file", side_effect=lambda path: (
                MODULE.REQUIRED_CLOUD_TASK_SHA256 if path == helper
                else file_digest(path) if path == prompt_path else "digest"
            )),
            mock.patch.object(MODULE, "run_hosted_decision_worker", return_value=bundle) as observe,
            mock.patch.object(MODULE, "local_source_fingerprint", return_value=source),
            mock.patch.object(MODULE, "github_decision_fingerprint", return_value=github),
            mock.patch.object(
                MODULE, "local_identity",
                return_value={"branch": "review", "status": "", "head": "head"},
            ),
            mock.patch.object(MODULE, "metadata_for", return_value=pr),
            mock.patch.object(MODULE, "agent_task_preflight", return_value=preflight),
            mock.patch.object(MODULE, "source_only_policy_skip_head", return_value=None),
            mock.patch.object(MODULE, "same_ref_forward_head_drift", return_value=False),
            mock.patch.object(MODULE, "require_live_pr_snapshot"),
            mock.patch.object(MODULE, "require_live_comments"),
            mock.patch.object(MODULE, "load_agent_task_result", return_value={"task": {"id": "task"}}),
            mock.patch.object(
                MODULE, "apply_verified_import",
                side_effect=RuntimeError("import reached"),
            ) as apply,
        ):
            with self.assertRaisesRegex(RuntimeError, "import reached"):
                MODULE.command_agent_task(self.args)
        self.assertEqual("--pipeline-observe", observe.call_args.kwargs["bounded_action"])
        self.assertEqual(prompt, apply.call_args.kwargs["prompt"])


if __name__ == "__main__":
    unittest.main()
