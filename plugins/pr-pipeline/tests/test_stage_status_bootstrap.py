import unittest
from pathlib import Path
from unittest import mock

from test_pr_stack_pipeline import load


ROOT = Path(__file__).parents[2]
STAGES = (
    ("pr-conflict-resolver", "pr_conflict_resolver"),
    ("copilot-review-loop", "copilot_review_loop"),
    ("self-review-loop", "self_review_loop"),
    ("ci-fix-loop", "ci_fix_loop"),
    ("pr-description", "pr_description"),
)


class StageStatusBootstrapTest(unittest.TestCase):
    def test_status_does_not_load_execution_runtime_inside_owned_child(self):
        for plugin, script in STAGES:
            with self.subTest(plugin=plugin):
                module = load(
                    f"status_bootstrap_{script}",
                    ROOT / plugin / "scripts" / f"{script}.py",
                )
                with (
                    mock.patch.dict(
                        module.os.environ,
                        {
                            "TRASK_EXECUTION_PARENT": "request.json",
                            "COPILOT_AGENT_SESSION_ID": "session-1",
                        },
                    ),
                    mock.patch.object(
                        module.sys, "argv", [script, "status", "--state", "state.json"]
                    ),
                    mock.patch.object(module, "main", return_value=0) as main,
                    mock.patch.object(
                        module, "_load_execution", side_effect=FileNotFoundError("copilot")
                    ) as load_execution,
                ):
                    self.assertEqual(0, module.execution_main())
                main.assert_called_once_with()
                load_execution.assert_not_called()

    def test_pipeline_children_still_load_execution_runtime(self):
        for plugin, script in STAGES:
            with self.subTest(plugin=plugin):
                module = load(
                    f"pipeline_bootstrap_{script}",
                    ROOT / plugin / "scripts" / f"{script}.py",
                )
                runtime = mock.Mock()
                runtime.entrypoint.return_value = 17
                with (
                    mock.patch.dict(
                        module.os.environ, {"TRASK_EXECUTION_PARENT": "request.json"}
                    ),
                    mock.patch.object(module.sys, "argv", [script, "pipeline"]),
                    mock.patch.object(module, "main") as main,
                    mock.patch.object(
                        module, "_load_execution", return_value=runtime
                    ) as load_execution,
                ):
                    self.assertEqual(17, module.execution_main())
                load_execution.assert_called_once_with()
                runtime.entrypoint.assert_called_once()
                main.assert_not_called()
