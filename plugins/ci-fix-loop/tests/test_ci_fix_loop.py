import argparse
import copy
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "ci_fix_loop.py"
PERMISSION_SCRIPT = (
    Path(__file__).parents[1] / "scripts" / "ci_fix_loop_permission.py"
)
AGENT = Path(__file__).parents[1] / "agents" / "ci-fix-loop.agent.md"
HOOKS = Path(__file__).parents[1] / "hooks.json"
PLUGIN = Path(__file__).parents[1] / "plugin.json"
EXTERNAL_ZIZMOR_CHECK = (
    Path(__file__).parent / "fixtures" / "external-zizmor-check-run.json"
)
COMPACT_ZIZMOR_REPORT = (
    Path(__file__).parent / "fixtures" / "compact-zizmor-v3-report.md"
)
SPEC = importlib.util.spec_from_file_location("ci_fix_loop", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
PERMISSION_SPEC = importlib.util.spec_from_file_location(
    "ci_fix_loop_permission", PERMISSION_SCRIPT
)
assert PERMISSION_SPEC is not None and PERMISSION_SPEC.loader is not None
PERMISSION_MODULE = importlib.util.module_from_spec(PERMISSION_SPEC)
PERMISSION_SPEC.loader.exec_module(PERMISSION_MODULE)


class AgentCommandAdmissionTest(unittest.TestCase):
    def payload(self, command, *, cwd=None, tool_name="powershell"):
        return {
            "sessionId": "session",
            "timestamp": 1,
            "cwd": cwd or str(Path.cwd()),
            "hookName": "permissionRequest",
            "toolName": tool_name,
            "toolInput": {"command": command},
            "permissionSuggestions": [],
        }

    def powershell_command(self, arguments):
        return f"{PERMISSION_MODULE.POWERSHELL_PREFIX}{arguments}"

    def test_admits_only_exact_coordinator_commands_for_current_workspace(self):
        cwd = str(Path.cwd())
        self.assertTrue(
            PERMISSION_MODULE.admission_allowed(
                self.payload(
                    self.powershell_command(
                        f'stack-start owner/repo#7 --repo-root "{cwd}"'
                    ),
                    cwd=cwd,
                )
            )
        )
        self.assertTrue(
            PERMISSION_MODULE.admission_allowed(
                self.payload(
                    (
                        f"{PERMISSION_MODULE.BASH_PREFIX}"
                        "loop owner/repo#7 --repo-root '/tmp/repo' "
                        "--model sol --new-invocation"
                    ),
                    cwd="/tmp/repo",
                    tool_name="bash",
                )
            )
        )
        self.assertTrue(
            PERMISSION_MODULE.admission_allowed(
                self.payload(
                    self.powershell_command(
                        f'loop owner/repo#7 --repo-root "{cwd}" '
                        "--model sol --new-invocation"
                    ),
                    cwd=cwd,
                )
            )
        )

    def test_rejects_broader_or_drifted_shell_commands(self):
        cwd = str(Path.cwd())
        rejected = [
            "python -c \"print('unrelated')\"",
            self.powershell_command(
                f'stack-start 7 --repo-root "{cwd}"'
            ),
            self.powershell_command(
                f'stack-start owner/repo#7 --repo-root "{cwd}" --model sol'
            ),
            self.powershell_command(
                f'loop owner/repo#7 --repo-root "{cwd}" --model astra'
            ),
            self.powershell_command(
                f'loop owner/repo#7 --repo-root "{cwd}\\other" --model sol'
            ),
            self.powershell_command(
                f'stack-cleanup --state "{cwd}\\state.json"'
            ),
            self.powershell_command(
                f'stack-status --state "{cwd}\\state.json"; whoami'
            ),
        ]
        for command in rejected:
            with self.subTest(command=command):
                self.assertFalse(
                    PERMISSION_MODULE.admission_allowed(
                        self.payload(command, cwd=cwd)
                    )
                )

    def test_hook_process_emits_only_the_permission_decision(self):
        cwd = str(Path.cwd())
        payload = self.payload(
            self.powershell_command(
                f'agent-task owner/repo#7 --repo-root "{cwd}" --model sol'
            ),
            cwd=cwd,
        )
        process_options = {}
        if os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NO_WINDOW
        completed = subprocess.run(
            [sys.executable, str(PERMISSION_SCRIPT)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            **process_options,
        )

        self.assertEqual('{"behavior":"allow"}\n', completed.stdout)
        self.assertEqual("", completed.stderr)

    def test_plugin_declares_narrow_permission_hook(self):
        plugin = json.loads(PLUGIN.read_text(encoding="utf-8"))
        hooks = json.loads(HOOKS.read_text(encoding="utf-8"))
        permission_hooks = hooks["hooks"]["permissionRequest"]

        self.assertEqual("hooks.json", plugin["hooks"])
        self.assertEqual(1, len(permission_hooks))
        self.assertEqual("bash|powershell", permission_hooks[0]["matcher"])
        self.assertIn("ci_fix_loop_permission.py", permission_hooks[0]["bash"])
        self.assertIn(
            "ci_fix_loop_permission.py", permission_hooks[0]["powershell"]
        )

    def test_admits_exact_reconciliation_argv_with_windows_spaces(self):
        with tempfile.TemporaryDirectory(prefix="ci fix admission ") as directory:
            root = Path(directory)
            repo = root / "source workspace"
            repo.mkdir()
            state = root / "legacy state.json"
            artifact = root / "sealed eligibility.json"
            digest = root / "sealed eligibility.json.sha256"
            manifest = root / "package manifest.json"
            for path in (state, artifact, digest, manifest):
                path.write_text("{}\n", encoding="utf-8")
            argv = [
                sys.executable,
                str(SCRIPT.resolve()),
                "verify-legacy-owner-reconciliation",
                "https://github.com/owner/repo/pull/7",
                "--repo-root",
                str(repo),
                "--state",
                str(state),
                "--eligibility-artifact",
                str(artifact),
                "--eligibility-sha256-file",
                str(digest),
                "--package-manifest",
                str(manifest),
                "--expected-package-manifest-sha256",
                "1" * 64,
                "--expected-seal",
                "2" * 64,
            ]
            command = subprocess.list2cmdline(argv)

            self.assertTrue(
                PERMISSION_MODULE.admission_allowed(
                    self.payload(command, cwd=str(repo))
                )
            )

    def test_reconciliation_admission_rejects_exploration_and_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            paths = [
                root / name
                for name in (
                    "state.json",
                    "artifact.json",
                    "artifact.json.sha256",
                    "manifest.json",
                )
            ]
            for path in paths:
                path.write_text("{}\n", encoding="utf-8")
            valid = [
                sys.executable,
                str(SCRIPT.resolve()),
                "apply-legacy-owner-reconciliation",
                "owner/repo#7",
                "--repo-root",
                str(repo),
                "--state",
                str(paths[0]),
                "--eligibility-artifact",
                str(paths[1]),
                "--eligibility-sha256-file",
                str(paths[2]),
                "--expected-artifact-sha256",
                "1" * 64,
                "--package-manifest",
                str(paths[3]),
                "--expected-package-manifest-sha256",
                "2" * 64,
                "--expected-seal",
                "3" * 64,
                "--expected-authorization-token",
                "4" * 64,
            ]
            self.assertTrue(
                PERMISSION_MODULE.admission_allowed(
                    self.payload(
                        subprocess.list2cmdline(valid),
                        cwd=str(repo),
                    )
                )
            )
            rejected = [
                "Select-String -Path helper.py -Pattern reconcile",
                subprocess.list2cmdline(valid[:-2]),
                subprocess.list2cmdline(
                    [
                        value if value != "3" * 64 else "invalid"
                        for value in valid
                    ]
                ),
                subprocess.list2cmdline(
                    valid + ["--model", "sol"]
                ),
            ]
            for command in rejected:
                with self.subTest(command=command):
                    self.assertFalse(
                        PERMISSION_MODULE.admission_allowed(
                            self.payload(command, cwd=str(repo))
                        )
                    )

    def test_agent_reconciliation_path_is_direct_and_denial_is_terminal(self):
        instructions = AGENT.read_text(encoding="utf-8")
        self.assertIn("invoke its `verifier_argv` directly", instructions)
        self.assertIn("never run `Select-String`", instructions)
        self.assertIn("A permission denial or verifier error is terminal", instructions)
        self.assertIn(
            "stop without `stack-start`, `loop`, `agent-task`", instructions
        )

    @unittest.skipUnless(
        os.environ.get("COPILOT_CI_FIX_AGENT_INTEGRATION") == "1",
        "set COPILOT_CI_FIX_AGENT_INTEGRATION=1 to exercise Copilot admission",
    )
    def test_user_facing_agent_command_passes_runtime_admission(self):
        plugin_root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            probe_plugin = Path(directory) / "plugin"
            shutil.copytree(plugin_root, probe_plugin)
            plugin = json.loads(
                (probe_plugin / "plugin.json").read_text(encoding="utf-8")
            )
            plugin["name"] = "ci-fix-loop-admission-test"
            (probe_plugin / "plugin.json").write_text(
                json.dumps(plugin, indent=2) + "\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.pop("COPILOT_ALLOW_ALL", None)
            environment["COPILOT_AUTO_UPDATE"] = "false"
            process_options = {}
            if os.name == "nt":
                process_options["creationflags"] = subprocess.CREATE_NO_WINDOW
            session_id = str(uuid.uuid4())
            completed = subprocess.run(
                [
                    "copilot",
                    "-p",
                    "owner/repo#1",
                    "--agent",
                    "ci-fix-loop-admission-test:ci-fix-loop",
                    "--plugin-dir",
                    str(probe_plugin),
                    "--model",
                    "gpt-5.6-sol",
                    "--reasoning-effort",
                    "high",
                    "--no-ask-user",
                    "--no-color",
                    "--silent",
                    "--session-id",
                    session_id,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=180,
                **process_options,
            )
            copilot_home = Path(
                environment.get("COPILOT_HOME", Path.home() / ".copilot")
            )
            events_path = copilot_home / "session-state" / session_id / "events.jsonl"
            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]
        output = f"{completed.stdout}\n{completed.stderr}"
        self.assertNotIn("Permission denied", output)
        self.assertNotIn("could not request permission", output)
        self.assertIn("owner/repo#1", output)
        starts = [
            event["data"]
            for event in events
            if event.get("type") == "tool.execution_start"
        ]
        self.assertEqual(1, len(starts))
        self.assertEqual("powershell", starts[0]["toolName"])
        self.assertTrue(
            starts[0]["arguments"]["command"].startswith(
                PERMISSION_MODULE.POWERSHELL_PREFIX
            )
        )
        permissions = [
            event["data"]
            for event in events
            if event.get("type") == "permission.completed"
        ]
        self.assertTrue(permissions)
        self.assertNotIn(
            "denied",
            json.dumps(permissions, sort_keys=True).lower(),
        )


class WindowsSubprocessTest(unittest.TestCase):
    def test_run_hides_windows_console_processes(self):
        completed = MODULE.subprocess.CompletedProcess(["formatter"], 0, "", "")
        with (
            mock.patch.object(MODULE, "IS_WINDOWS", True),
            mock.patch.object(
                MODULE.subprocess,
                "CREATE_NO_WINDOW",
                0x08000000,
                create=True,
            ),
            mock.patch.object(
                MODULE.subprocess, "run", return_value=completed
            ) as subprocess_run,
        ):
            MODULE.run(["formatter"])

        self.assertEqual(
            subprocess_run.call_args.kwargs["creationflags"], 0x08000000
        )
        environment = subprocess_run.call_args.kwargs["env"]
        self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")
        index = int(environment["GIT_CONFIG_COUNT"]) - 1
        self.assertEqual(environment[f"GIT_CONFIG_KEY_{index}"], "core.hooksPath")
        self.assertEqual(environment[f"GIT_CONFIG_VALUE_{index}"], os.devnull)

    def test_run_leaves_non_windows_process_options_unchanged(self):
        completed = MODULE.subprocess.CompletedProcess(["formatter"], 0, "", "")
        with (
            mock.patch.object(MODULE, "IS_WINDOWS", False),
            mock.patch.object(
                MODULE.subprocess, "run", return_value=completed
            ) as subprocess_run,
        ):
            MODULE.run(["formatter"])

        self.assertNotIn("creationflags", subprocess_run.call_args.kwargs)

    def test_run_bytes_hides_windows_console_processes(self):
        completed = MODULE.subprocess.CompletedProcess(["git"], 0, b"", b"")
        with (
            mock.patch.object(MODULE, "IS_WINDOWS", True),
            mock.patch.object(
                MODULE.subprocess,
                "CREATE_NO_WINDOW",
                0x08000000,
                create=True,
            ),
            mock.patch.object(
                MODULE.subprocess, "run", return_value=completed
            ) as subprocess_run,
        ):
            MODULE.run_bytes(["git"])

        self.assertEqual(
            subprocess_run.call_args.kwargs["creationflags"], 0x08000000
        )

    def test_owned_process_is_suspended_until_windows_job_assignment(self):
        process = mock.Mock(pid=17)
        owner = mock.Mock()
        with (
            mock.patch.object(MODULE, "IS_WINDOWS", True),
            mock.patch.object(
                MODULE.subprocess,
                "CREATE_NO_WINDOW",
                0x08000000,
                create=True,
            ),
            mock.patch.object(
                MODULE.subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0x00000200,
                create=True,
            ),
            mock.patch.object(
                MODULE.subprocess,
                "CREATE_BREAKAWAY_FROM_JOB",
                0x01000000,
                create=True,
            ),
            mock.patch.object(
                MODULE.subprocess,
                "CREATE_SUSPENDED",
                0x00000004,
                create=True,
            ),
            mock.patch.object(
                MODULE.subprocess, "Popen", return_value=process
            ) as popen,
            mock.patch.object(
                MODULE, "create_windows_kill_job", return_value=owner
            ) as create_job,
            mock.patch.object(MODULE, "resume_windows_process") as resume,
        ):
            actual_process, actual_owner = MODULE.popen_owned_process(
                ["helper"], cwd=Path.cwd()
            )

        self.assertIs(process, actual_process)
        self.assertIs(owner, actual_owner)
        self.assertEqual(
            0x09000204,
            popen.call_args.kwargs["creationflags"],
        )
        create_job.assert_called_once_with(17)
        resume.assert_called_once_with(17)

    def test_termination_uses_the_owned_windows_job(self):
        process = mock.Mock()
        process.poll.return_value = None
        owner = mock.Mock()

        MODULE.terminate_owned_process(process, owner, timeout=3)

        owner.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=3)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()


NOW = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 import os
-value = 1
+value = 2
 print(value)
"""


def check(
    key,
    name=None,
    klass="failed",
    url=None,
    completed_at=None,
    *,
    kind="check_run",
    description=None,
    workflow_run_id=None,
):
    return {
        "kind": kind,
        "key": key,
        "name": name or key.split(":", 1)[-1],
        "workflow": None,
        "status": None,
        "conclusion": None,
        "state": None,
        "class": klass,
        "url": url,
        "workflow_run_id": workflow_run_id,
        "started_at": None,
        "completed_at": completed_at,
        "description": description,
    }


def stamp(minutes_ago=0):
    moment = NOW - dt.timedelta(minutes=minutes_ago)
    return moment.isoformat().replace("+00:00", "Z")


def run_arguments(*arguments):
    parser = MODULE.build_parser()
    return parser.parse_args(list(arguments))


def call(*arguments):
    args = run_arguments(*arguments)
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        args.function(args)
    return json.loads(stream.getvalue())


def write_state(directory: Path, **overrides) -> Path:
    state = {
        "version": MODULE.STATE_VERSION,
        "created_at": "2026-01-01T00:00:00Z",
        "iterations": 1,
        "history": [],
        "reruns": {},
        "escalation": None,
        "repo_root": str(directory),
        "pr": {
            "number": 7,
            "title": "Add a thing",
            "pr_url": "https://github.com/owner/repo/pull/7",
            "repo_name": "owner/repo",
            "upstream_owner": "owner",
            "upstream_repo": "repo",
            "head_owner": "fork",
            "head_repo": "repo",
            "head_branch": "feature",
            "head_sha": "head1",
            "base_branch": "main",
            "base_sha": "base1",
            "is_fork": True,
            "is_draft": True,
            "commits": [],
        },
        "run": {
            "id": "pr-7-iteration-1",
            "status": "active",
            "iteration": 1,
            "head_sha": "head1",
            "base_sha": "base1",
            "diff_path": str(directory / "state.json.diff"),
            "changed_files": ["app.py"],
            "pr_commits": [],
            "checks": [],
            "attributions": {},
            "batches": [],
            "tracking": {},
            "decision": None,
        },
    }
    for key, value in overrides.items():
        if key in {"run", "pr"} and isinstance(value, dict):
            state[key] = {**state[key], **value}
        else:
            state[key] = value
    path = directory / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    (directory / "state.json.diff").write_text(DIFF, encoding="utf-8")
    return path


def attribution(key, verdict, *, source="baseline", baseline=None, conclusion=None):
    return {
        "key": key,
        "name": key.split(":", 1)[-1],
        "verdict": verdict,
        "source": source,
        "baseline_conclusion": conclusion,
        "baseline_verdict": baseline if baseline is not None else verdict,
        "rationale": None,
    }


def native_stack(heads=None, *, branches=None):
    heads = heads or {5: "lower1", 7: "middle1", 9: "upper1"}
    branches = branches or {
        5: ("lower", "main"),
        7: ("middle", "lower"),
        9: ("upper", "middle"),
    }
    members = []
    for position, number in enumerate((5, 7, 9)):
        head_branch, base_branch = branches[number]
        members.append(
            {
                "position": position,
                "number": number,
                "title": f"PR {number}",
                "head_branch": head_branch,
                "base_branch": base_branch,
                "head_sha": heads[number],
                "is_draft": True,
                "state": "OPEN",
            }
        )
    return {
        "id": "stack-id",
        "number": 77,
        "size": len(members),
        "trunk": "main",
        "members": members,
    }


LOCAL_VALIDATION_HEADING = "## Local Validation Before A Push"
def _agent_section(text, heading):
    """Return the body of one Markdown section, stopping at the next peer heading."""
    lines = text.split("\n")
    start = lines.index(heading)
    depth = len(heading) - len(heading.lstrip("#"))
    body = []
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            if level <= depth:
                break
        body.append(line)
    return "\n".join(body)


class LegacyAgentInstructionsReference:
    def setUp(self):
        self.instructions = AGENT.read_text(encoding="utf-8")

    def test_documents_the_helper_activity_stamp_without_overselling_it(self):
        """A reader who thinks the stamp proves liveness stops checking further.

        The helper writes only when a subcommand runs, so an hour of silence is
        as consistent with hard thinking as with a hang.
        """
        self.assertIn("`last_helper_activity`", self.instructions)
        self.assertIn(
            "the moment this helper last wrote its state", self.instructions
        )
        self.assertIn("not proof the stage is alive", self.instructions)
        self.assertIn(
            "the agent driving it can think for a long time between two of them",
            self.instructions,
        )

    def test_declares_the_frontmatter_the_siblings_use(self):
        self.assertIn("name: CI Fix Loop", self.instructions)
        self.assertIn(
            'argument-hint: "PR URL, PR number, or owner/repo#number; omit only '
            "from a worktree attached to the PR's branch\"",
            self.instructions,
        )
        self.assertIn(
            "tools: [read, edit, search, execute, agent, todo, rename_session]",
            self.instructions,
        )
        self.assertIn("user-invocable: true", self.instructions)
        self.assertIn("disable-model-invocation: true", self.instructions)

    def test_declares_no_model_frontmatter_key(self):
        frontmatter = self.instructions.split("---")[1]
        self.assertNotIn("\nmodel:", frontmatter)

    def test_documents_waiting_for_repository_automatic_retries(self):
        self.assertIn("wait-for-auto-retry", self.instructions)
        self.assertIn("retry_started", self.instructions)
        self.assertIn("retry_not_detected", self.instructions)
        self.assertIn(
            "Wait through every automatic attempt the workflow",
            self.instructions,
        )
        self.assertIn(
            "use the helper's `rerun` action once rather than escalating",
            self.instructions,
        )

    def test_dependency_download_failures_do_not_become_unfixable(self):
        self.assertIn(
            "download a dependency",
            self.instructions,
        )
        self.assertIn(
            "the helper initially marks the check `pr_caused`.",
            self.instructions,
        )
        self.assertIn(
            "Never call an infrastructure failure",
            self.instructions,
        )
        self.assertIn(
            "`unfixable_failure` merely because the exact CI command passes locally",
            self.instructions,
        )
        self.assertIn(
            "Do not run `skip`.",
            self.instructions,
        )

    def test_tells_the_agent_that_no_progress_is_its_claim_to_make(self):
        self.assertIn(
            "It reports `cleared`, `skipped`, `escalated`, and `carried`, and it "
            "leaves the field out entirely when the state names no ending.",
            self.instructions,
        )
        self.assertIn(
            "No progress is the one ending only you can report.", self.instructions
        )
        self.assertIn(
            "a run killed part way through leaves state that looks exactly like a "
            "run still going",
            self.instructions,
        )

    def test_states_the_suppression_refusal_and_what_to_do_about_it(self):
        self.assertIn(
            "`record` and `publish` both read the commit and stop the run when it "
            "deletes a test file, or adds a skip, disable, or ignore annotation to "
            "one.",
            self.instructions,
        )
        self.assertIn(
            "That refusal has no override and no rationale gets past it",
            self.instructions,
        )

    def test_passes_a_launchers_loop_position_through_without_reading_it(self):
        """The budget only bounds anything if the agent cannot supply the reset."""
        self.assertIn("### A Launcher's Loop Position", self.instructions)
        self.assertIn(
            "`pipeline-run: <token> pipeline-iteration: <number> "
            "pipeline-max-iterations: <number>`",
            self.instructions,
        )
        self.assertIn(
            "--pipeline-run <token> --pipeline-iteration <number> "
            "--pipeline-max-iterations <number>",
            self.instructions,
        )
        self.assertIn(
            "A value you produced would be this loop refreshing its own cap",
            self.instructions,
        )
        self.assertIn(
            "never invent one to keep working after `max_iterations_reached`",
            self.instructions,
        )

    def test_keys_the_position_on_the_values_rather_than_one_spelling(self):
        """A launcher that words it differently still gets its budget scoped.

        Making one phrasing the trigger drops a position supplied any other way,
        and it drops it silently: the run reports cleanly and the budget was
        simply never scoped. The rule is about where a value came from.
        """
        self.assertIn("Read the values, not the spelling.", self.instructions)
        self.assertIn(
            "a spelling you do not recognize is still the caller's instruction",
            self.instructions,
        )
        self.assertIn(
            "Omit all three only when the request names no position at all",
            self.instructions,
        )
        self.assertIn(
            "Send `--pipeline-run` and `--pipeline-iteration` together",
            self.instructions,
        )
        self.assertNotIn("if the line is absent, omit all three", self.instructions)

    def test_runs_the_whole_loop_from_a_bare_reference(self):
        self.assertIn("## Activation: Bare PR References Run The Full Loop", self.instructions)
        self.assertIn(
            "For a standalone user invocation, start with `stack-start`",
            self.instructions,
        )
        self.assertIn(
            "skip `stack-start` and begin with `preflight`", self.instructions
        )
        self.assertIn("Do not ask what action the user wants", self.instructions)

    def test_native_stack_protocol_is_ci_only_and_propagates_every_push(self):
        section = _agent_section(self.instructions, "## Native Stack Coordination")
        self.assertIn("Do not invoke PR Stack Pipeline", section)
        self.assertIn("Pass `--state <member_state>", section)
        self.assertIn(
            "After every `published` or `empty_commit_published` result", section
        )
        self.assertIn("immediately run `stack-propagate`", section)
        self.assertIn("If it returns `format`, run `stack-format`", section)
        self.assertIn("repeat that command for each returned checkpoint", section)
        self.assertIn("reason: formatter_failed", section)
        self.assertIn("If it returns `resolve_conflict`", section)
        self.assertIn("pr-conflict-resolver:pr-conflict-resolver", section)
        self.assertIn("rerun the exact `stack-propagate` action", section)
        self.assertIn("For `retired_attempt`, return to `stack-next`", section)
        self.assertIn(
            "Write the final report only when its status is still `complete`",
            section,
        )
        self.assertIn(
            "`stack-next` retires that clearance and every dependent descendant",
            section,
        )
        self.assertIn("keeps the same stack run and bounded pipeline budget", section)
        self.assertIn(
            "A higher member never starts until its direct predecessor is clear",
            section,
        )

    def test_documents_safe_actions_log_downloads(self):
        self.assertIn(
            "gh run view --log-failed --allow-escape-sequences",
            self.instructions,
        )

    def test_does_not_age_queued_jobs_while_their_workflow_executes(self):
        self.assertIn(
            "Queued matrix jobs do not spend their not-started grace while another "
            "job from the same Actions run is executing.",
            self.instructions,
        )

    def test_never_posts_anything_to_github(self):
        self.assertIn(
            "This agent never posts anything to GitHub.", self.instructions
        )
        self.assertIn(
            "It writes no comment, no review, no reply, and no label.",
            self.instructions,
        )
        self.assertIn(
            "say what you would have posted in your final response instead",
            self.instructions,
        )
        self.assertIn("Do not post any of this to GitHub.", self.instructions)

    def test_names_the_session_from_preflight_metadata_idempotently(self):
        self.assertIn("## Session Naming", self.instructions)
        self.assertIn(
            "ensure the session name is `CI Fix Loop: <PR number> - <PR title>`",
            self.instructions,
        )
        self.assertIn(
            "If the harness has already supplied a name beginning "
            "`CI Fix Loop: <PR number> - `",
            self.instructions,
        )
        self.assertIn("do not call `rename_session`", self.instructions)
        self.assertIn("Otherwise call `rename_session` once", self.instructions)

    def test_fixes_only_failures_this_pull_request_caused(self):
        self.assertIn(
            "Fix only a failure this pull request plausibly caused.", self.instructions
        )
        self.assertIn(
            "editing this pull request to hide it is worse than leaving it alone",
            self.instructions,
        )

    def test_reruns_a_suspected_flake_exactly_once(self):
        self.assertIn("Re-run a suspected flake exactly once.", self.instructions)
        self.assertIn(
            "If it fails again, it is not a flake, so escalate", self.instructions
        )

    def test_escalates_checks_that_cannot_resolve_on_their_own(self):
        self.assertIn(
            "A check that never starts, and a check that waits for a maintainer to "
            "approve a fork's workflow run, escalates straight away.",
            self.instructions,
        )
        self.assertIn("Never wait for one of those indefinitely", self.instructions)

    def test_treats_a_repository_without_checks_as_a_visible_skip(self):
        self.assertIn(
            "A pull request whose head reports no applicable checks is a skip, never "
            "a pass.",
            self.instructions,
        )
        self.assertIn(
            "the helper already recorded the terminal skip in the same atomic state "
            "write that observed it",
            self.instructions,
        )
        self.assertIn(
            "A broken continuous integration configuration must never look like a "
            "green pipeline.",
            self.instructions,
        )

    def test_caps_the_loop_at_five_iterations(self):
        self.assertIn("The maximum is 5 iterations", self.instructions)
        self.assertIn("max_iterations_reached", self.instructions)

    def test_starts_each_explicit_invocation_with_a_fresh_budget(self):
        self.assertIn(
            "Every explicit user invocation starts with a fresh five-iteration budget.",
            self.instructions,
        )
        self.assertIn("`--new-invocation`", self.instructions)
        self.assertIn("`--invocation-run <token>`", self.instructions)
        self.assertIn(
            "Do not use `--new-invocation` again during the same user invocation",
            self.instructions,
        )

    def test_says_an_iteration_is_charged_per_head_rather_than_per_launch(self):
        """The prose is what the next reader believes, so it has to say which it is.

        An agent that thinks a relaunch costs an iteration rations its own reads
        of the checks, and one that starts over at an unchanged head after a
        re-run would otherwise burn a fifth of the budget on the same analysis.
        """
        self.assertIn(
            "an iteration is charged per head rather than per launch",
            self.instructions,
        )
        self.assertIn(
            "only moving the head to a new commit spends the next one",
            self.instructions,
        )

    def test_never_weakens_a_check_to_make_it_pass(self):
        self.assertIn(
            "Never disable, delete, skip, or weaken a check to make it pass.",
            self.instructions,
        )
        self.assertIn(
            "Never touch a test's expectations to match broken behavior.",
            self.instructions,
        )

    def test_documents_the_helper_invocation_for_each_shell(self):
        self.assertIn("## Mechanical Helper", self.instructions)
        for shell in ("Git Bash on Windows", "PowerShell on Windows", "POSIX shells"):
            self.assertIn(shell, self.instructions)
        self.assertIn(
            "installed-plugins/trask-plugins/ci-fix-loop/scripts/ci_fix_loop.py",
            self.instructions,
        )
        self.assertIn(
            "Never pass a `~`-prefixed helper path to native Windows Python from "
            "Git Bash.",
            self.instructions,
        )

    def test_documents_every_helper_command(self):
        for command in (
            "`preflight ",
            "`checks --state",
            "`attribute --state",
            "`rerun --state",
            "`plan --state",
            "`record` and `skip`",
            "`escalate --state",
            "`resolve --state",
            "`publish --state",
            "`status [--state",
            "`cleanup --state",
        ):
            self.assertIn(command, self.instructions)

    def test_names_the_status_command_as_the_machine_readable_outcome(self):
        self.assertIn(
            "This is the machine-readable outcome an orchestrator reads.",
            self.instructions,
        )

    def test_carries_the_expected_workflow_sections(self):
        for heading in (
            "## Non-Negotiable Rules",
            "## Plain Language",
            "## Target And Preflight",
            "## What Green Means Here",
            "## Reading The Checks",
            "## Attributing A Failure",
            "## Fixing A Failure",
            "## Commit Content",
            "## Publishing And The Next Iteration",
            "## Final Report",
        ):
            self.assertIn(heading, self.instructions)

    def test_requires_a_chat_only_retrospective_after_the_final_report(self):
        self.assertIn("## Retrospective", self.instructions)
        for category in (
            "**Agent**",
            "**Helper**",
            "**General instructions**",
            "**Repository**",
        ):
            self.assertIn(category, self.instructions)
        self.assertIn("After every terminal outcome", self.instructions)
        self.assertIn("Keep this advisory and chat-only", self.instructions)
        self.assertIn("Omit this section when the run encountered no friction", self.instructions)
        self.assertGreater(
            self.instructions.index("## Retrospective"),
            self.instructions.index("## Final Report"),
        )

    def test_reads_greenness_from_github_rather_than_from_its_own_state(self):
        self.assertIn(
            "GitHub states whether the checks pass, and this loop's own state never "
            "does.",
            self.instructions,
        )
        self.assertIn(
            "checks that passed and then failed again at the same head must show "
            "through",
            self.instructions,
        )

    def test_treats_a_relaunch_at_a_cleared_head_as_ordinary(self):
        self.assertIn(
            "Being asked to run again at a head you already cleared is normal, not a "
            "fault.",
            self.instructions,
        )
        self.assertIn(
            "A run that finds nothing to fix spends no iteration", self.instructions
        )

    def test_states_a_skip_an_orchestrator_cannot_miss(self):
        self.assertIn(
            "`Outcome: skipped, because this repository runs no applicable checks on "
            "this pull request.`",
            self.instructions,
        )
        self.assertIn("the helper's `skip_note` verbatim", self.instructions)
        self.assertIn(
            "never let a run end without saying it when `checks` reported "
            "`no_checks`",
            self.instructions,
        )

    def test_never_ends_a_run_silently(self):
        self.assertIn("`Outcome: no progress.`", self.instructions)
        self.assertIn(
            "a run that says nothing reads as a stall and, twice in a row, stops a "
            "whole pipeline",
            self.instructions,
        )
        self.assertIn("Report it as no progress", self.instructions)

    def test_does_not_credit_the_failure_a_rerun_replaces(self):
        self.assertIn(
            "It records the moment it asked before it asks", self.instructions
        )
        self.assertIn(
            "Never read the failure still showing just after the request as the "
            "re-run's answer.",
            self.instructions,
        )
        self.assertIn(
            "A failure that was already on record when the re-run was requested is "
            "the old one, not a second failure.",
            self.instructions,
        )

    def test_ties_the_evidence_it_credits_to_the_pinned_head(self):
        self.assertIn(
            "Every check the loop credits belongs to the head it pinned.",
            self.instructions,
        )
        self.assertIn(
            "a check that ran on an earlier commit can never clear this one",
            self.instructions,
        )

    def test_ends_the_run_with_a_single_terminal_response(self):
        self.assertIn(
            "The terminal response is the run's last message.", self.instructions
        )
        self.assertIn("Send one message that calls no tool.", self.instructions)

    def test_names_no_build_tool_or_programming_language(self):
        """Each stage runs under the configuration its own repository supplies.

        This list exists to fail on the one wrong fix that is tempting here:
        pasting a concrete build command into the file so the agent does not
        have to work one out. Every name is matched on a word boundary,
        because a bare substring on a short token eventually fires on an
        innocent word and gets deleted by whoever trips over it, and the guard
        is then gone.
        """
        forbidden = [
            "bazel",
            "cargo",
            "dotnet",
            "golang",
            "gradle",
            "gradlew",
            "java",
            "javac",
            "jest",
            "junit",
            "kotlin",
            "maven",
            "mvn",
            "npm",
            "pnpm",
            "pytest",
            "rustc",
            "tsc",
            "typescript",
            "yarn",
        ]
        found = sorted(
            name
            for name in forbidden
            if re.search(rf"\b{name}\b", self.instructions, re.IGNORECASE)
        )
        self.assertEqual([], found)

    def test_the_local_validation_fallback_publishes_instead_of_stopping(self):
        """A repository with no usable narrow command must not become a stop.

        Halting there would create a second class of false escalation on
        exactly the repositories where local validation buys nothing, so every
        paragraph that reaches for the skip flag has to push, and none of them
        may reach for escalation vocabulary.
        """
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        paragraphs = [
            paragraph
            for paragraph in section.split("\n\n")
            if "--not-validated" in paragraph
        ]
        self.assertTrue(paragraphs)
        for paragraph in paragraphs:
            with self.subTest(paragraph=paragraph):
                self.assertIn("publish", paragraph)
                self.assertNotIn("escalat", paragraph.lower())

    def test_every_validation_flag_the_section_names_reaches_publish(self):
        """Prose naming a flag the helper rejects would stop a push outright."""
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        named = sorted(set(re.findall(r"--[a-z][a-z-]+", section)))
        self.assertTrue(named)
        parser = MODULE.build_parser()
        for flag in named:
            with self.subTest(flag=flag):
                args = parser.parse_args(
                    ["publish", "--state", "state.json", flag, "value"]
                )
                self.assertEqual("publish", args.command)

    def test_publish_documents_every_validation_flag_it_accepts(self):
        """A flag the helper grows and the file never mentions goes unused."""
        parser = MODULE.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        accepted = sorted(
            option
            for action in subparsers.choices["publish"]._actions
            for option in action.option_strings
            if "valid" in option or "rewrote" in option
        )
        self.assertTrue(accepted)
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        for flag in accepted:
            with self.subTest(flag=flag):
                self.assertIn(flag, section)

    def test_local_validation_is_wired_into_the_step_that_pushes(self):
        """The requirement is only real where the run reaches the push."""
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        elsewhere = self.instructions.replace(section, "")
        self.assertIn(f"**{LOCAL_VALIDATION_HEADING.lstrip('# ')}**", elsewhere)

    def test_covering_checks_are_not_narrowed_to_compilation(self):
        """The failure this requirement was written for compiled cleanly.

        It was a documentation comment that a separate documentation task
        rejected, so wording that let covering mean "it builds" would sail
        past the very cycle this is meant to save.
        """
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        for word in ["documentation", "lint", "format"]:
            with self.subTest(word=word):
                self.assertIn(word, section)

    def test_requires_committing_what_a_fixing_command_rewrote(self):
        """A rewrite left in the worktree fails silently.

        The push carries the earlier commit, the same check fails on the pull
        request anyway, and the next reset discards the rewritten files.
        """
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        self.assertIn("fixing form", section)
        rewrite_paragraphs = [
            paragraph
            for paragraph in section.split("\n\n")
            if re.search(r"rewr\w+", paragraph, re.IGNORECASE)
            and "commit" in paragraph.lower()
        ]
        self.assertTrue(rewrite_paragraphs)

    def test_local_success_does_not_stand_in_for_the_checks(self):
        self.assertIn(
            "GitHub says whether they pass and this loop never does",
            self.instructions,
        )

    def test_requires_reproducing_the_failing_check_before_fixing_it(self):
        """This stage knows which check failed, so it must not guess.

        A guess costs a whole CI cycle, which is the most expensive mistake
        available to a loop whose entire cost model is round trips.
        """
        self.assertIn("Reproduce the failure locally.", self.instructions)
        self.assertIn(
            "confirm it fails the same way it failed in CI", self.instructions
        )
        self.assertIn(
            "Run that same command again, and confirm it now passes",
            self.instructions,
        )

    def test_a_failure_that_cannot_be_reproduced_here_still_publishes(self):
        """Checks needing containers or credentials only run in CI.

        Refusing to push those would turn an ordinary repository into an
        escalation, which is worse than the failure this section prevents.
        """
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        self.assertIn("must never stop this loop", section)

    def test_routes_a_no_target_request_around_a_detached_worktree(self):
        """A stage the pipeline launches has no branch checked out.

        A reader who copies the bare no-target form there reaches a resolver
        that refuses on purpose, so both steps have to name what to pass
        instead of leaving the refusal as the answer.
        """
        self.assertIn(
            "`--current` resolves through the branch that is checked out, which "
            "a detached worktree does not have, so pass `--state <path>` when "
            "the worktree is detached.",
            self.instructions,
        )
        self.assertIn(
            "a stage the pipeline launches works in a worktree detached at the "
            "pull request head, so name the pull request as a URL or "
            "`owner/repo#number` instead",
            self.instructions,
        )
        self.assertIn(
            "The bare form is for a checkout still sitting on a branch, and "
            "this loop's ordinary case under a pipeline is not one.",
            self.instructions,
        )

    def test_the_current_rule_admits_a_detached_worktree_has_no_pull_request(self):
        """The rule still rightly forbids picking a state file by hand.

        It was only wrong to imply a checked-out branch is always there to ask.
        """
        self.assertIn(
            "`current` always means the pull request attached to the branch "
            "that is checked out, and a detached worktree has no such pull "
            "request",
            self.instructions,
        )

    def test_the_argument_hint_stops_selling_the_bare_form_as_the_default(self):
        """The hint is the shape a caller copies before reaching any step list.

        It used to promise the current branch's PR, which a detached worktree
        cannot supply, so the omission read as the ordinary way to call this.
        The sibling frontmatter test pins the whole line; this one says why the
        clause is worded the way it is.
        """
        self.assertIn(
            "omit only from a worktree attached to the PR's branch", self.instructions
        )
        self.assertNotIn("omit to use the current branch's PR", self.instructions)


class HostedDispatchOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.preflight = {
            "pr": {
                "repository": "owner/repo",
                "repo_name": "owner/repo",
                "number": 7,
                "pr_url": "https://github.com/owner/repo/pull/7",
                "state": "OPEN",
                "head_branch": "feature",
                "head_sha": "1" * 40,
                "cross_repository": False,
            }
        }
        self.report_path = ".github/agent-task-reports/request-1.md"
        self.consumer_prompt = (
            "worker instructions\n"
            f"write {MODULE.REPORT_PATH_PLACEHOLDER}\n"
        )
        rendered = self.consumer_prompt.replace(
            MODULE.REPORT_PATH_PLACEHOLDER, self.report_path
        )
        self.live_prompt = (
            f"{rendered}\n"
            f"Source PR: {self.preflight['pr']['pr_url']}\n"
            f"Exact source head SHA: {self.preflight['pr']['head_sha']}\n"
            f"Policy: {MODULE.AGENT_TASK_POLICY}\n"
        )
        self.task = {
            "id": "task-1",
            "state": "in_progress",
            "html_url": "https://github.com/owner/repo/agent-tasks/task-1",
            "created_at": "2026-01-01T00:00:01Z",
            "updated_at": "2026-01-01T00:00:02Z",
            "sessions": [
                {
                    "id": "session-1",
                    "task_id": "task-1",
                    "state": "in_progress",
                    "created_at": "2026-01-01T00:00:01Z",
                    "updated_at": "2026-01-01T00:00:02Z",
                    "model": "sweagent-capi:gpt-5.6-sol",
                    "base_ref": "feature",
                    "head_ref": "copilot/fix",
                    "prompt": self.live_prompt,
                }
            ],
        }

    def identity(self, task=None):
        return MODULE.hosted_dispatch_identity(
            self.task if task is None else task,
            consumer_prompt=self.consumer_prompt,
            preflight=self.preflight,
            requested_model="gpt-5.6-sol",
            started_at="2026-01-01T00:00:00Z",
        )

    def test_extracts_one_exact_hosted_dispatch_identity(self):
        identity = self.identity()

        self.assertEqual("task-1", identity["task_id"])
        self.assertEqual("session-1", identity["session_id"])
        self.assertEqual(self.report_path, identity["report_path"])
        self.assertEqual("request-1", identity["request_id"])
        self.assertEqual(
            MODULE.sha256_text(self.live_prompt),
            identity["live_prompt_sha256"],
        )

    def test_task_baseline_projects_only_opaque_ids(self):
        completed = MODULE.subprocess.CompletedProcess(
            ["gh"], 0, "task-1\ntask-2\n", ""
        )
        with mock.patch.object(
            MODULE, "run", return_value=completed
        ) as run_command:
            identifiers = MODULE.listed_agent_task_ids("owner/repo")

        self.assertEqual({"task-1", "task-2"}, identifiers)
        command = run_command.call_args.args[0]
        self.assertEqual(".tasks[].id", command[command.index("--jq") + 1])

    def test_rejects_any_material_hosted_identity_drift(self):
        mutations = {
            "task": lambda task: task.update(id="other"),
            "state": lambda task: task["sessions"][0].update(state="completed"),
            "model": lambda task: task["sessions"][0].update(
                model="sweagent-capi:gpt-6-astra"
            ),
            "base": lambda task: task["sessions"][0].update(base_ref="main"),
            "prompt": lambda task: task["sessions"][0].update(prompt="other"),
            "creation": lambda task: task.update(
                created_at="2025-12-31T23:59:59Z"
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                task = copy.deepcopy(self.task)
                mutate(task)
                self.assertIsNone(self.identity(task))

    def test_discovery_rejects_multiple_exact_matches(self):
        other = copy.deepcopy(self.task)
        other["id"] = "task-2"
        other["sessions"][0]["task_id"] = "task-2"
        with (
            mock.patch.object(
                MODULE,
                "listed_agent_task_ids",
                return_value={"old", "task-1", "task-2"},
            ),
            mock.patch.object(
                MODULE,
                "agent_task_api_json",
                side_effect=[self.task, other],
            ),
            self.assertRaisesRegex(MODULE.WorkflowError, "multiple hosted"),
        ):
            MODULE.discover_hosted_dispatch(
                repository="owner/repo",
                baseline_task_ids={"old"},
                consumer_prompt=self.consumer_prompt,
                preflight=self.preflight,
                requested_model="gpt-5.6-sol",
                started_at="2026-01-01T00:00:00Z",
            )

    def test_timeout_persists_known_task_and_reaps_owned_process(self):
        class FakeProcess:
            pid = 19
            returncode = None

            def poll(self):
                return self.returncode

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            MODULE.save_state(
                state_path,
                {
                    "version": MODULE.STATE_VERSION,
                    "agent_task": {
                        "run_id": "run-1",
                        "status": "running",
                        "phase": "hosted_fix",
                    }
                },
            )
            process = FakeProcess()
            owner = mock.Mock()
            identity = self.identity()

            def terminate(actual_process, _owner):
                actual_process.returncode = 1

            with (
                mock.patch.object(
                    MODULE, "listed_agent_task_ids", return_value={"old"}
                ),
                mock.patch.object(
                    MODULE,
                    "popen_owned_process",
                    return_value=(process, owner),
                ),
                mock.patch.object(
                    MODULE, "discover_hosted_dispatch", return_value=identity
                ),
                mock.patch.object(
                    MODULE.time, "monotonic", side_effect=[0.0, 1.0]
                ),
                mock.patch.object(MODULE, "terminate_owned_process", side_effect=terminate),
                self.assertRaisesRegex(
                    MODULE.WorkflowError, "known task task-1"
                ),
            ):
                MODULE.run_hosted_helper(
                    ["helper"],
                    repo_root=Path(directory),
                    state_path=state_path,
                    run_id="run-1",
                    preflight=self.preflight,
                    consumer_prompt=self.consumer_prompt,
                    requested_model="gpt-5.6-sol",
                    timeout=1,
                    discovery_interval=1,
                )

            task = MODULE.load_state(state_path)["agent_task"]
            self.assertEqual("known", task["task_id_status"])
            self.assertEqual("task-1", task["task_id"])
            self.assertEqual("timed_out", task["dispatch_monitor"]["status"])
            self.assertEqual("hosted_helper_timeout", task["dispatch_monitor"]["failure"])
            owner.close.assert_called_once_with()

    def test_dead_hosted_owner_is_finalized_without_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state = {
                "version": MODULE.STATE_VERSION,
                "agent_task": {
                    "run_id": "run-1",
                    "status": "running",
                    "phase": "hosted_fix",
                    "recovery_command": "must disappear",
                    "retry_command": "must disappear",
                    "dispatch_identity": self.identity(),
                    "dispatch_monitor": {
                        "status": "running",
                        "helper_pid": 19,
                    },
                },
            }
            MODULE.save_state(state_path, state)

            with mock.patch.object(
                MODULE, "process_is_running", return_value=False
            ):
                reconciled = MODULE.reconcile_dead_hosted_owner(
                    state_path,
                    MODULE.load_state(state_path),
                    repo_root=Path(directory),
                    target={"repo_name": "owner/repo", "number": 7},
                )

            task = reconciled["agent_task"]
            self.assertEqual("failed", task["status"])
            self.assertEqual("known", task["task_id_status"])
            self.assertEqual("task-1", task["task_id"])
            self.assertEqual("owner_lost", task["dispatch_monitor"]["status"])
            self.assertNotIn("recovery_command", task)
            self.assertNotIn("retry_command", task)

    def test_completed_helper_returns_captured_output_and_identity(self):
        class FakeProcess:
            pid = 23
            returncode = 0

            def poll(self):
                return self.returncode

            def wait(self):
                return self.returncode

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            MODULE.save_state(
                state_path,
                {
                    "version": MODULE.STATE_VERSION,
                    "agent_task": {
                        "run_id": "run-1",
                        "status": "running",
                        "phase": "hosted_fix",
                    },
                },
            )
            owner = mock.Mock()

            def start(_command, *, stdout, stderr, **_kwargs):
                stdout.write("stdout")
                stdout.flush()
                stderr.write("stderr")
                stderr.flush()
                return FakeProcess(), owner

            with (
                mock.patch.object(
                    MODULE, "listed_agent_task_ids", return_value={"old"}
                ),
                mock.patch.object(
                    MODULE, "popen_owned_process", side_effect=start
                ),
                mock.patch.object(
                    MODULE,
                    "discover_hosted_dispatch",
                    return_value=self.identity(),
                ),
                mock.patch.object(MODULE.time, "monotonic", return_value=0.0),
            ):
                result = MODULE.run_hosted_helper(
                    ["helper"],
                    repo_root=Path(directory),
                    state_path=state_path,
                    run_id="run-1",
                    preflight=self.preflight,
                    consumer_prompt=self.consumer_prompt,
                    requested_model="gpt-5.6-sol",
                    timeout=7200,
                    discovery_interval=5,
                )

            self.assertEqual(0, result.returncode)
            self.assertEqual("stdout", result.stdout)
            self.assertEqual("stderr", result.stderr)
            task = MODULE.load_state(state_path)["agent_task"]
            self.assertEqual("task-1", task["task_id"])
            self.assertEqual("exited", task["dispatch_monitor"]["status"])
            owner.close.assert_called_once_with()

    def test_live_hosted_owner_is_not_reclassified(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state = {
                "version": MODULE.STATE_VERSION,
                "agent_task": {
                    "run_id": "run-1",
                    "status": "running",
                    "phase": "hosted_fix",
                    "dispatch_monitor": {
                        "status": "running",
                        "helper_pid": 19,
                    },
                },
            }
            MODULE.save_state(state_path, state)

            with mock.patch.object(
                MODULE, "process_is_running", return_value=True
            ):
                reconciled = MODULE.reconcile_dead_hosted_owner(
                    state_path,
                    MODULE.load_state(state_path),
                    repo_root=Path(directory),
                    target={"repo_name": "owner/repo", "number": 7},
                )

            self.assertEqual("running", reconciled["agent_task"]["status"])

    def test_dead_hosted_owner_without_dispatch_identity_stays_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state = {
                "version": MODULE.STATE_VERSION,
                "agent_task": {
                    "run_id": "run-1",
                    "status": "running",
                    "phase": "hosted_fix",
                    "recovery_command": "must disappear",
                    "dispatch_monitor": {
                        "status": "running",
                        "helper_pid": 19,
                    },
                },
            }
            MODULE.save_state(state_path, state)

            with mock.patch.object(
                MODULE, "process_is_running", return_value=False
            ):
                reconciled = MODULE.reconcile_dead_hosted_owner(
                    state_path,
                    MODULE.load_state(state_path),
                    repo_root=Path(directory),
                    target={"repo_name": "owner/repo", "number": 7},
                )

            task = reconciled["agent_task"]
            self.assertEqual("failed", task["status"])
            self.assertEqual("unknown", task["task_id_status"])
            self.assertIsNone(task["task_id"])
            self.assertNotIn("recovery_command", task)

    def legacy_owner_state(self, directory):
        root = Path(directory)
        repo = root / "repo"
        repo.mkdir()
        prompt = root / "prompt.txt"
        result = root / "result.json"
        triage = root / "triage.json"
        prompt.write_text("prompt", encoding="utf-8")
        triage.write_text("{}", encoding="utf-8")
        identity = {"branch": "feature", "head": "1" * 40, "status": ""}
        preflight = {
            "repository_root": str(repo),
            "identity": identity,
            "pr": {
                "repo_name": "owner/repo",
                "number": 7,
                "pr_url": "https://github.com/owner/repo/pull/7",
                "head_sha": "1" * 40,
                "base_sha": "2" * 40,
                "head_branch": "feature",
                "base_branch": "main",
                "title": "Title",
                "body": "Body",
            },
            "check_snapshot": {"sha256": "3" * 64},
        }
        error = (
            "an unfinished Agent Task already owns this state; "
            "use its recovery_command"
        )
        return repo, identity, {
            "version": MODULE.STATE_VERSION,
            "iterations": 1,
            "agent_task": {
                "run_id": "run-1",
                "status": "running",
                "phase": "hosted_fix",
                "model": "gpt-5.6-sol",
                "policy": MODULE.AGENT_TASK_POLICY,
                "iteration_allowance": 1,
                "started_at": "2026-01-01T00:00:00Z",
                "recovery_command": "python helper.py agent-task --resume",
                "prompt_file": str(prompt),
                "result_file": str(result),
                "triage_result_file": str(triage),
                "preflight": preflight,
            },
            "coordinator": {
                "status": "blocked",
                "detail": error,
                "head_sha": "1" * 40,
                "base_sha": "2" * 40,
                "snapshot_sha256": "3" * 64,
                "observed_at": "2026-01-01T01:00:00Z",
            },
            "escalation": {
                "reason": "coordinator_error",
                "detail": error,
            },
        }

    def test_normal_agent_task_entry_does_not_finalize_legacy_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            repo, identity, state = self.legacy_owner_state(directory)
            MODULE.save_state(state_path, state)
            with (
                mock.patch.object(
                    MODULE, "metadata_for"
                ) as metadata,
            ):
                reconciled = MODULE.reconcile_dead_hosted_owner(
                    state_path,
                    MODULE.load_state(state_path),
                    repo_root=repo,
                    target={"repo_name": "owner/repo", "number": 7},
                )

            task = reconciled["agent_task"]
            self.assertEqual("running", task["status"])
            self.assertNotIn("dispatch_monitor", task)
            self.assertNotIn("task_id_status", task)
            self.assertEqual(1, reconciled["iterations"])
            self.assertIn("recovery_command", task)
            metadata.assert_not_called()

    def test_legacy_owner_stays_active_while_exact_helper_process_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            repo, identity, state = self.legacy_owner_state(directory)
            MODULE.save_state(state_path, state)
            with (
                mock.patch.object(MODULE, "local_identity", return_value=identity),
                mock.patch.object(
                    MODULE, "metadata_for", return_value=state["agent_task"]["preflight"]["pr"]
                ),
                mock.patch.object(MODULE, "require_live_pr_snapshot"),
                mock.patch.object(MODULE, "require_live_check_snapshot"),
                mock.patch.object(
                    MODULE, "command_fragment_process_ids", return_value=[77]
                ),
            ):
                reconciled = MODULE.reconcile_dead_hosted_owner(
                    state_path,
                    MODULE.load_state(state_path),
                    repo_root=repo,
                    target={"repo_name": "owner/repo", "number": 7},
                )

            self.assertEqual("running", reconciled["agent_task"]["status"])
            self.assertIn("recovery_command", reconciled["agent_task"])

    def test_legacy_owner_snapshot_requires_exact_zero_owner_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            repo, identity, state = self.legacy_owner_state(directory)
            MODULE.save_state(state_path, state)
            with (
                mock.patch.object(MODULE, "local_identity", return_value=identity),
                mock.patch.object(
                    MODULE,
                    "metadata_for",
                    return_value=state["agent_task"]["preflight"]["pr"],
                ),
                mock.patch.object(MODULE, "require_live_pr_snapshot"),
                mock.patch.object(MODULE, "require_live_check_snapshot"),
                mock.patch.object(
                    MODULE, "command_fragment_process_ids", return_value=[]
                ),
            ):
                snapshot = MODULE.legacy_hosted_owner_reconciliation_snapshot(
                    state_path=state_path,
                    repo_root=repo,
                    target=MODULE.parse_target("owner/repo#7"),
                )

            self.assertEqual(
                MODULE.LEGACY_OWNER_RECONCILIATION_SNAPSHOT_SCHEMA,
                snapshot["schema"],
            )
            self.assertEqual(MODULE.sha256_file(state_path), snapshot["state"]["sha256"])
            self.assertEqual([], snapshot["matching_process_ids"])
            self.assertEqual("run-1", snapshot["owner"])

            with (
                mock.patch.object(MODULE, "local_identity", return_value=identity),
                mock.patch.object(
                    MODULE,
                    "metadata_for",
                    return_value=state["agent_task"]["preflight"]["pr"],
                ),
                mock.patch.object(MODULE, "require_live_pr_snapshot"),
                mock.patch.object(MODULE, "require_live_check_snapshot"),
                mock.patch.object(
                    MODULE, "command_fragment_process_ids", return_value=[77]
                ),
                self.assertRaisesRegex(MODULE.WorkflowError, "still owns"),
            ):
                MODULE.legacy_hosted_owner_reconciliation_snapshot(
                    state_path=state_path,
                    repo_root=repo,
                    target=MODULE.parse_target("owner/repo#7"),
                )

    def test_package_manifest_verifies_exact_installed_helper_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "source"
            repo.mkdir()
            installed_root = root / "installed plugins"
            helper = (
                installed_root
                / "ci-fix-loop"
                / "scripts"
                / "ci_fix_loop.py"
            )
            helper.parent.mkdir(parents=True)
            helper.write_text("exact helper\n", encoding="utf-8", newline="\n")
            files = [
                {
                    "path": "scripts/ci_fix_loop.py",
                    "size": helper.stat().st_size,
                    "sha256": MODULE.sha256_file(helper),
                }
            ]
            package = {
                "name": "ci-fix-loop",
                "version": "1.6.31",
                "file_count": 1,
                "byte_count": helper.stat().st_size,
                "package_sha256": MODULE.canonical_package_digest(files),
                "published_git_tree_oid": "1" * 40,
                "files": files,
            }
            manifest = {
                "schema": MODULE.PLUGIN_PACKAGE_MANIFEST_SCHEMA,
                "generator": {
                    "name": "trask/copilot-plugins plugin_package_manifest",
                    "version": "1.0.0",
                    "sha256": "2" * 64,
                    "command_argv": [],
                },
                "generated_at": "2026-09-17T00:00:00Z",
                "source_commit": "3" * 40,
                "installed_root": str(installed_root.resolve()),
                "algorithm": MODULE.PLUGIN_PACKAGE_MANIFEST_ALGORITHM,
                "packages": [package],
            }
            manifest_path = root / "package manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            with mock.patch.object(MODULE, "__file__", str(helper)):
                verified = MODULE.verify_installed_package_manifest(
                    manifest_path,
                    MODULE.sha256_file(manifest_path),
                    repo,
                )

            self.assertEqual("ci-fix-loop", verified["package"]["name"])
            self.assertEqual(MODULE.sha256_file(helper), files[0]["sha256"])

            (installed_root / "ci-fix-loop" / "unexpected").write_text(
                "drift\n", encoding="utf-8"
            )
            with (
                mock.patch.object(MODULE, "__file__", str(helper)),
                self.assertRaisesRegex(MODULE.WorkflowError, "file set drifted"),
            ):
                MODULE.verify_installed_package_manifest(
                    manifest_path,
                    MODULE.sha256_file(manifest_path),
                    repo,
                )

    def test_sealed_verifier_authorizes_only_exact_two_pass_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="legacy owner ") as directory:
            root = Path(directory)
            state_path = root / "state.json"
            artifact_path = root / "eligibility artifact.json"
            digest_path = root / "eligibility artifact.json.sha256"
            manifest_path = root / "package manifest.json"
            repo, _identity, state = self.legacy_owner_state(directory)
            MODULE.save_state(state_path, state)
            manifest_path.write_text("{}\n", encoding="utf-8")
            package = {
                "path": str(manifest_path),
                "sha256": "a" * 64,
                "schema": MODULE.PLUGIN_PACKAGE_MANIFEST_SCHEMA,
                "source_commit": "b" * 40,
                "installed_root": str(root / "installed"),
                "package": {
                    "name": "ci-fix-loop",
                    "version": "1.6.31",
                    "file_count": 8,
                    "package_sha256": "c" * 64,
                },
            }
            snapshot = {
                "schema": MODULE.LEGACY_OWNER_RECONCILIATION_SNAPSHOT_SCHEMA,
                "state": {
                    "path": str(state_path),
                    "sha256": MODULE.sha256_file(state_path),
                },
                "owner": "run-1",
            }
            artifact = MODULE.legacy_owner_eligibility_artifact(
                target=MODULE.parse_target("owner/repo#7"),
                repo_root=repo,
                state_path=state_path,
                eligibility_path=artifact_path,
                digest_path=digest_path,
                snapshot=snapshot,
                package_manifest=package,
            )
            artifact_path.write_text(
                json.dumps(artifact, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            artifact_sha256 = MODULE.sha256_file(artifact_path)
            digest_path.write_text(
                f"{artifact_sha256}\n", encoding="ascii", newline="\n"
            )
            arguments = SimpleNamespace(
                target="owner/repo#7",
                repo_root=str(repo),
                state=str(state_path),
                eligibility_artifact=str(artifact_path),
                eligibility_sha256_file=str(digest_path),
                package_manifest=str(manifest_path),
                expected_package_manifest_sha256=package["sha256"],
                expected_seal=artifact["seal"],
            )
            emitted = []
            forbidden = [
                "listed_agent_task_ids",
                "run_hosted_helper",
                "apply_verified_import",
                "publish_empty_rerun_commit",
            ]
            with contextlib.ExitStack() as stack:
                forbidden_mocks = [
                    stack.enter_context(mock.patch.object(MODULE, name))
                    for name in forbidden
                ]
                with (
                    mock.patch.object(MODULE, "require_tools"),
                    mock.patch.object(
                        MODULE, "resolve_repo_root", return_value=repo
                    ),
                    mock.patch.object(
                        MODULE,
                        "resolve_target",
                        return_value=MODULE.parse_target("owner/repo#7"),
                    ),
                    mock.patch.object(
                        MODULE,
                        "verify_installed_package_manifest",
                        return_value=package,
                    ),
                    mock.patch.object(
                        MODULE,
                        "legacy_hosted_owner_reconciliation_snapshot",
                        side_effect=[snapshot, snapshot],
                    ) as live,
                    mock.patch.object(MODULE, "emit", emitted.append),
                ):
                    MODULE.command_verify_legacy_owner_reconciliation(
                        arguments
                    )
                for forbidden_mock in forbidden_mocks:
                    forbidden_mock.assert_not_called()

            self.assertEqual(2, live.call_count)
            authorization = emitted[-1]
            self.assertEqual("authorized", authorization["result"])
            self.assertEqual(2, authorization["passes"])
            self.assertFalse(authorization["mutation_performed"])
            self.assertFalse(authorization["workflow_started"])
            self.assertEqual(
                "apply-legacy-owner-reconciliation",
                authorization["reconciliation_argv"][2],
            )
            apply_arguments = copy.copy(arguments)
            apply_arguments.expected_artifact_sha256 = artifact_sha256
            apply_arguments.expected_authorization_token = authorization[
                "authorization_token"
            ]
            applied = []
            with contextlib.ExitStack() as stack:
                forbidden_mocks = [
                    stack.enter_context(mock.patch.object(MODULE, name))
                    for name in forbidden
                ]
                with (
                    mock.patch.object(MODULE, "require_tools"),
                    mock.patch.object(
                        MODULE, "resolve_repo_root", return_value=repo
                    ),
                    mock.patch.object(
                        MODULE,
                        "resolve_target",
                        return_value=MODULE.parse_target("owner/repo#7"),
                    ),
                    mock.patch.object(
                        MODULE,
                        "verify_installed_package_manifest",
                        return_value=package,
                    ),
                    mock.patch.object(
                        MODULE,
                        "legacy_hosted_owner_reconciliation_snapshot",
                        side_effect=[snapshot, snapshot, snapshot],
                    ),
                    mock.patch.object(MODULE, "emit", applied.append),
                ):
                    MODULE.command_apply_legacy_owner_reconciliation(
                        apply_arguments
                    )
                for forbidden_mock in forbidden_mocks:
                    forbidden_mock.assert_not_called()

            final = MODULE.load_state(state_path)
            self.assertEqual(1, final["iterations"])
            self.assertEqual("failed", final["agent_task"]["status"])
            self.assertEqual("unknown", final["agent_task"]["task_id_status"])
            self.assertIsNone(final["agent_task"]["task_id"])
            self.assertNotIn("recovery_command", final["agent_task"])
            self.assertEqual("owner_lost", applied[-1]["result"])
            self.assertFalse(applied[-1]["workflow_started"])
            self.assertFalse(applied[-1]["task_created"])
            self.assertIsNone(applied[-1]["continuation"])

    def test_prepare_writes_one_sealed_nonexecuted_artifact(self):
        with tempfile.TemporaryDirectory(prefix="legacy prepare ") as directory:
            root = Path(directory)
            state_path = root / "state.json"
            artifact_path = root / "eligibility artifact.json"
            digest_path = root / "eligibility artifact.json.sha256"
            manifest_path = root / "package manifest.json"
            repo, _identity, state = self.legacy_owner_state(directory)
            MODULE.save_state(state_path, state)
            state_sha256 = MODULE.sha256_file(state_path)
            manifest_path.write_text("{}\n", encoding="utf-8", newline="\n")
            package = {
                "path": str(manifest_path),
                "sha256": "a" * 64,
                "schema": MODULE.PLUGIN_PACKAGE_MANIFEST_SCHEMA,
                "source_commit": "b" * 40,
                "installed_root": str(root / "installed"),
                "package": {
                    "name": "ci-fix-loop",
                    "version": "1.6.31",
                    "file_count": 8,
                    "package_sha256": "c" * 64,
                },
            }
            snapshot = {
                "schema": MODULE.LEGACY_OWNER_RECONCILIATION_SNAPSHOT_SCHEMA,
                "state": {
                    "path": str(state_path),
                    "sha256": state_sha256,
                },
                "owner": "run-1",
            }
            arguments = SimpleNamespace(
                target="owner/repo#7",
                repo_root=str(repo),
                state=str(state_path),
                eligibility_artifact=str(artifact_path),
                eligibility_sha256_file=str(digest_path),
                package_manifest=str(manifest_path),
                expected_package_manifest_sha256=package["sha256"],
            )
            emitted = []
            with (
                mock.patch.object(MODULE, "require_tools"),
                mock.patch.object(
                    MODULE, "resolve_repo_root", return_value=repo
                ),
                mock.patch.object(
                    MODULE,
                    "resolve_target",
                    return_value=MODULE.parse_target("owner/repo#7"),
                ),
                mock.patch.object(
                    MODULE,
                    "verify_installed_package_manifest",
                    return_value=package,
                ),
                mock.patch.object(
                    MODULE,
                    "legacy_hosted_owner_reconciliation_snapshot",
                    return_value=snapshot,
                ),
                mock.patch.object(MODULE, "emit", emitted.append),
            ):
                MODULE.command_prepare_legacy_owner_reconciliation(arguments)

            result = emitted[-1]
            self.assertEqual("eligibility_written", result["result"])
            self.assertFalse(result["mutation_performed"])
            self.assertFalse(result["workflow_started"])
            self.assertEqual(state_sha256, MODULE.sha256_file(state_path))
            artifact_sha256, artifact = MODULE.load_legacy_owner_eligibility(
                eligibility_path=artifact_path,
                digest_path=digest_path,
                expected_artifact_sha256=result[
                    "eligibility_artifact_sha256"
                ],
                expected_seal=result["seal"],
            )
            self.assertEqual(
                result["eligibility_artifact_sha256"], artifact_sha256
            )
            self.assertEqual(result["verifier_argv"], artifact["verifier_argv"])

    def test_verifier_rejects_stale_second_pass_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            artifact_path = root / "artifact.json"
            digest_path = root / "artifact.json.sha256"
            manifest_path = root / "manifest.json"
            repo, _identity, state = self.legacy_owner_state(directory)
            MODULE.save_state(state_path, state)
            manifest_path.write_text("{}\n", encoding="utf-8")
            package = {
                "path": str(manifest_path),
                "sha256": "a" * 64,
                "schema": MODULE.PLUGIN_PACKAGE_MANIFEST_SCHEMA,
                "source_commit": "b" * 40,
                "installed_root": str(root / "installed"),
                "package": {
                    "name": "ci-fix-loop",
                    "version": "1.6.31",
                    "file_count": 8,
                    "package_sha256": "c" * 64,
                },
            }
            snapshot = {"state": {"sha256": MODULE.sha256_file(state_path)}}
            artifact = MODULE.legacy_owner_eligibility_artifact(
                target=MODULE.parse_target("owner/repo#7"),
                repo_root=repo,
                state_path=state_path,
                eligibility_path=artifact_path,
                digest_path=digest_path,
                snapshot=snapshot,
                package_manifest=package,
            )
            artifact_path.write_text(
                json.dumps(artifact, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            digest_path.write_text(
                f"{MODULE.sha256_file(artifact_path)}\n",
                encoding="ascii",
                newline="\n",
            )
            arguments = SimpleNamespace(
                target="owner/repo#7",
                repo_root=str(repo),
                state=str(state_path),
                eligibility_artifact=str(artifact_path),
                eligibility_sha256_file=str(digest_path),
                package_manifest=str(manifest_path),
                expected_package_manifest_sha256=package["sha256"],
                expected_seal=artifact["seal"],
            )
            with (
                mock.patch.object(MODULE, "require_tools"),
                mock.patch.object(
                    MODULE, "resolve_repo_root", return_value=repo
                ),
                mock.patch.object(
                    MODULE,
                    "resolve_target",
                    return_value=MODULE.parse_target("owner/repo#7"),
                ),
                mock.patch.object(
                    MODULE,
                    "verify_installed_package_manifest",
                    return_value=package,
                ),
                mock.patch.object(
                    MODULE,
                    "legacy_hosted_owner_reconciliation_snapshot",
                    side_effect=[snapshot, {"state": {"sha256": "d" * 64}}],
                ),
                self.assertRaisesRegex(
                    MODULE.WorkflowError, "two-pass verification"
                ),
            ):
                MODULE.command_verify_legacy_owner_reconciliation(arguments)

            self.assertEqual("running", MODULE.load_state(state_path)["agent_task"]["status"])


class ManagedAgentTaskContractTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.head = "1" * 40
        self.base = "2" * 40
        self.artifact = "3" * 40
        failure = {
            "key": "check:CI/test",
            "kind": "check_run",
            "name": "test",
            "workflow": "CI",
            "url": "https://github.com/owner/repo/actions/runs/1/job/2",
            "conclusion": "FAILURE",
            "baseline_conclusion": "SUCCESS",
            "baseline_verdict": "pr_caused",
            "log_path": str(self.root / "failed.log"),
            "log_sha256": MODULE.sha256_text("AssertionError: expected 2\n"),
        }
        (self.root / "failed.log").write_bytes(
            b"AssertionError: expected 2\n"
        )
        snapshot = {
            "head_sha": self.head,
            "base_sha": self.base,
            "observed_at": "2026-01-01T00:00:00Z",
            "rollup": [],
            "rollup_sha256": MODULE.sha256_text("[]"),
            "decision": {
                "decision": "failures",
                "reason": "checks_failed",
                "checks": [failure["key"]],
                "detail": "test failed",
            },
            "failures": [failure],
        }
        snapshot["sha256"] = MODULE.check_snapshot_sha256(snapshot)
        self.preflight = {
            "repository_root": str(self.root),
            "identity": {"branch": "feature", "head": self.head, "status": ""},
            "pr": {
                "number": 7,
                "title": "Fix widget",
                "body": "",
                "pr_url": "https://github.com/owner/repo/pull/7",
                "repo_name": "owner/repo",
                "state": "OPEN",
                "head_owner": "owner",
                "head_repo": "repo",
                "head_repository": "owner/repo",
                "head_branch": "feature",
                "head_sha": self.head,
                "base_branch": "main",
                "base_sha": self.base,
                "cross_repository": False,
            },
            "viewer": {"login": "viewer", "permissions": {"push": True}},
            "stack_guard": None,
            "check_snapshot": snapshot,
        }
        summary = (
            f"{MODULE.triage_identity_line(self.preflight)}\n"
            "The test failure is the root failure.\n"
        )

        def fake_triage(**kwargs):
            kwargs["summary_path"].write_text(summary, encoding="utf-8")
            kwargs["result_path"].write_text("{}\n", encoding="utf-8")
            return summary

        self.triage_worker = mock.patch.object(
            MODULE, "run_local_triage_worker", side_effect=fake_triage
        )
        self.retained_triage = mock.patch.object(
            MODULE, "validate_retained_local_triage", return_value=summary
        )
        self.github_fingerprint = mock.patch.object(
            MODULE, "github_triage_fingerprint", return_value={"state": "pinned"}
        )
        self.hosted_helper = mock.patch.object(
            MODULE,
            "run_hosted_helper",
            side_effect=lambda command, repo_root, **_kwargs: MODULE.run(
                command, cwd=repo_root, check=False
            ),
        )
        self.triage_worker_mock = self.triage_worker.start()
        self.retained_triage_mock = self.retained_triage.start()
        self.github_fingerprint.start()
        self.hosted_helper_mock = self.hosted_helper.start()
        self.addCleanup(self.triage_worker.stop)
        self.addCleanup(self.retained_triage.stop)
        self.addCleanup(self.github_fingerprint.stop)
        self.addCleanup(self.hosted_helper.stop)

    def result(self, commits=None):
        commits = [] if commits is None else commits
        return {
            "schema": MODULE.AGENT_TASK_RESULT_SCHEMA,
            "status": "success",
            "mode": "apply_with_report",
            "repository": {"name_with_owner": "owner/repo"},
            "pull_request": MODULE.expected_cloud_pull_request(self.preflight),
            "requested_model": "gpt-5.6-sol",
            "policy": {
                "id": "marketplace-agent-apply-report-worker",
                "version": 3,
                "sha256": MODULE.AGENT_TASK_POLICY_SHA256,
            },
            "task": {
                "id": "task-1",
                "url": "https://github.com/owner/repo/agent-tasks/task-1",
                "state": "completed",
                "base_ref": "feature",
                "base_sha": self.head,
            },
            "generated": {
                "branch": "copilot/agent-task",
                "head_sha": self.artifact,
                "commits": commits,
            },
            "application": {
                "status": "not_applied",
                "final_local_head": self.head,
            },
            "report": {
                "path": ".github/agent-task-reports/request-1.md",
                "commit": self.artifact,
                "sha256": "4" * 64,
            },
            "attestation": {
                "kind": "dispatcher_structural",
                "structural_complete": True,
            },
            "error": None,
        }

    def taskless_failure(self):
        failure = self.result()
        failure.update(
            {
                "status": "error",
                "task": {
                    "id": None,
                    "url": None,
                    "state": None,
                    "base_ref": None,
                    "base_sha": None,
                },
                "generated": {"branch": None, "head_sha": None, "commits": []},
                "application": {
                    "status": "not_applied",
                    "final_local_head": self.head,
                },
                "report": None,
                "attestation": {
                    "kind": "dispatcher_structural",
                    "structural_complete": False,
                },
                "error": {
                    "code": "api_failure",
                    "message": "user or repo does not have CCA enabled",
                },
            }
        )
        return failure

    def test_taskless_api_failure_keeps_its_trusted_error(self):
        error = MODULE.validate_task_creation_failure_result(
            self.taskless_failure(),
            preflight=self.preflight,
            requested_model="gpt-5.6-sol",
        )

        self.assertEqual("api_failure", error["code"])

    def remote(self, commits=None):
        return MODULE.validate_success_result(
            self.result(commits),
            preflight=self.preflight,
            requested_model="gpt-5.6-sol",
        )

    def report(
        self,
        *,
        commits=None,
        outcome="no_change",
        disposition="already_fixed",
        changed_paths=None,
        coverage=True,
    ):
        commits = [] if commits is None else commits
        failure = self.preflight["check_snapshot"]["failures"][0]
        return json.dumps(
            {
                "schema": MODULE.CI_FIX_REPORT_SCHEMA,
                "request_id": "request-1",
                "repository": "owner/repo",
                "pull_request": {
                    "number": 7,
                    "head_sha": self.head,
                    "base_sha": self.base,
                    "check_snapshot_sha256": self.preflight["check_snapshot"][
                        "sha256"
                    ],
                },
                "iteration_allowance": 1,
                "outcome": outcome,
                "failures": [
                    {
                        "key": failure["key"],
                        "name": failure["name"],
                        "disposition": disposition,
                        "reason": "The focused test proves the result.",
                        "commits": commits if disposition == "fixed" else [],
                    }
                ],
                "changed_paths": [] if changed_paths is None else changed_paths,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def validate_report(self, content, commits=None):
        return MODULE.validate_ci_fix_report(
            content,
            request_id="request-1",
            preflight=self.preflight,
            remote=self.remote(commits),
            iteration_allowance=1,
        )

    def compact_report_context(self):
        commits = [
            "de6179d8f0edcd9c94bc995f24fc1735fbbce896",
            "4076ad1e7b825752d99231ba1634ad0067c6d83b",
        ]
        report_path = (
            ".github/agent-task-reports/"
            "c4dfa59b-96ac-4892-a204-be9365130b47.md"
        )
        preflight = copy.deepcopy(self.preflight)
        preflight["pr"].update(
            {
                "repo_name": "open-telemetry/shared-workflows",
                "number": 377,
                "head_sha": "a56a1b77a015d4928625e2f0d708f00fe7f924c8",
                "base_sha": "ad5b9918d6ec0000000000000000000000000000",
            }
        )
        preflight["check_snapshot"] = {
            "sha256": (
                "d22740f4a724bf8c3339449924cb9463363493b797a68f708a0266d6cf9f43e7"
            ),
            "failures": [
                {
                    "key": "check:zizmor",
                    "name": "zizmor",
                    "kind": "check_run",
                    "workflow": None,
                    "url": (
                        "https://github.com/open-telemetry/shared-workflows/"
                        "runs/104705281292"
                    ),
                    "log": "",
                    "log_sha256": MODULE.sha256_text(""),
                    "baseline_verdict": "pull_request",
                }
            ],
        }
        remote = {
            "requires_apply": True,
            "commits": commits,
            "report_path": report_path,
        }
        return preflight, remote

    def test_agent_definition_is_a_thin_managed_coordinator(self):
        instructions = AGENT.read_text(encoding="utf-8")
        self.assertIn("agent-task <target>", instructions)
        self.assertIn("loop <canonical-target>", instructions)
        self.assertIn("marketplace-agent-apply-report-worker@3", instructions)
        self.assertIn("Never use Cloud Sandboxes", instructions)
        self.assertIn("`custom_agent`", instructions)
        self.assertIn("Never run `gh pr diff`", instructions)
        self.assertNotIn("tools: [read", instructions)
        self.assertNotIn("tools: [edit", instructions)
        self.assertIn("tools: [execute, agent, rename_session]", instructions)
        self.assertIn("model: gpt-5.6-sol", instructions)
        self.assertNotIn("tools: [execute, agent, todo", instructions)
        self.assertEqual("1.6.31", json.loads(PLUGIN.read_text())["version"])

    def test_agent_canonicalizes_stack_start_target(self):
        instructions = AGENT.read_text(encoding="utf-8")
        frontmatter = instructions.split("---", 2)[1]
        invocation = _agent_section(instructions, "## Invocation")

        self.assertIn(
            "argument-hint: \"Canonical PR URL or owner/repo#number; "
            "omit only from a worktree attached to the PR's branch\"",
            frontmatter,
        )
        self.assertIn(
            "combine it with the current workspace repository to form "
            "`owner/repo#19204`",
            invocation,
        )
        self.assertIn(
            "`stack-start <canonical-target> --repo-root <workspace>`",
            invocation,
        )
        self.assertIn(
            "Every `stack-start` command must include that canonical target",
            invocation,
        )

    def test_agent_keeps_reading_a_pending_coordinator_shell(self):
        instructions = AGENT.read_text(encoding="utf-8")
        invocation = _agent_section(instructions, "## Invocation")

        self.assertIn("delay of at most 540 seconds", invocation)
        self.assertIn("Repeat direct reads of that same shell until it exits", invocation)
        self.assertIn("Never end the turn", invocation)
        self.assertIn("do not retry or recover it", invocation)
        arguments = MODULE.build_parser().parse_args(["loop", "owner/repo#7"])
        self.assertGreater(arguments.hosted_timeout, 600)
        self.assertIn(
            "Never pass a bare number and never omit the target",
            invocation,
        )
        self.assertNotIn("stack-start <target>", invocation)
        self.assertIn("`stack-start` does not accept `--model`", invocation)
        self.assertIn("Use exactly `--model sol`", invocation)

    def test_report_parser_accepts_markdown_with_one_json_payload(self):
        content = "# Result\n\nReadable summary.\n\n```json\n{\"ok\":true}\n```"
        self.assertEqual(
            {"ok": True},
            MODULE.parse_markdown_report(content, description="test report"),
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "exactly one"):
            MODULE.parse_markdown_report("# Result", description="test report")

    def test_prompt_pins_snapshot_allowance_model_policy_and_worker_boundary(self):
        prompt = MODULE.build_worker_prompt(
            self.preflight,
            iteration_allowance=1,
            prior_history=[],
            requested_model="gpt-5.6-sol",
            triage_summary=(
                f"{MODULE.triage_identity_line(self.preflight)}\n"
                "The test failure is the root failure.\n"
            ),
        )
        self.assertIn("human-readable UTF-8 Markdown report", prompt)
        self.assertIn("sole repository worker", prompt)
        self.assertIn(self.preflight["check_snapshot"]["sha256"], prompt)
        self.assertIn(MODULE.AGENT_TASK_POLICY_SHA256, prompt)
        self.assertIn('"iteration_allowance": 1', prompt)
        self.assertIn("Never select a marketplace `custom_agent`", prompt)
        self.assertIn("use Cloud Sandboxes", prompt)
        self.assertIn("worker prompt version 4", prompt)
        self.assertNotIn("AssertionError: expected 2", prompt)
        self.assertIn("Never replace a check key with a numeric database ID", prompt)
        self.assertIn("`{{MARKETPLACE_REPORT_PATH}}`", prompt)
        self.assertNotIn("MARKETPLACE_VALIDATION_PATH", prompt)
        MODULE.require_no_credentials(prompt, source="prompt")

    def test_failed_log_download_is_scoped_to_the_exact_job(self):
        check = self.preflight["check_snapshot"]["failures"][0]
        destination = self.root.parent / f"{self.root.name}-download.log"
        self.addCleanup(destination.unlink, missing_ok=True)
        completed = MODULE.subprocess.CompletedProcess(
            ["gh"], 0, "focused failure log\n", ""
        )
        with (
            mock.patch.object(MODULE, "resolve_run_id", return_value=1),
            mock.patch.object(MODULE, "run", return_value=completed) as run,
        ):
            content = MODULE.fetch_failed_check_log(
                self.preflight["pr"],
                check,
                destination=destination,
                repo_root=self.root,
            )

        self.assertEqual("focused failure log\n", content)
        self.assertEqual(content, destination.read_text(encoding="utf-8"))
        self.assertIn("--job", run.call_args.args[0])
        self.assertIn("2", run.call_args.args[0])
        self.assertNotIn("--allow-escape-sequences", run.call_args.args[0])

    def test_failed_log_redacts_real_credentials_before_persistence(self):
        check = self.preflight["check_snapshot"]["failures"][0]
        destination = self.root.parent / f"{self.root.name}-redacted.log"
        self.addCleanup(destination.unlink, missing_ok=True)
        secret = "github_pat_abcdefghijklmnopqrstuvwxyz"
        completed = MODULE.subprocess.CompletedProcess(
            ["gh"], 0, f"before {secret} after\n", ""
        )
        with (
            mock.patch.object(MODULE, "resolve_run_id", return_value=1),
            mock.patch.object(MODULE, "run", return_value=completed),
        ):
            content = MODULE.fetch_failed_check_log(
                self.preflight["pr"],
                check,
                destination=destination,
                repo_root=self.root,
            )

        self.assertEqual(
            f"before {MODULE.REDACTED_CREDENTIAL} after\n",
            content,
        )
        self.assertNotIn(secret, content)
        self.assertEqual(content, destination.read_text(encoding="utf-8"))
        MODULE.require_no_credentials(content, source="redacted test log")

    def test_failed_log_redacts_complete_private_key_material(self):
        check = self.preflight["check_snapshot"]["failures"][0]
        completed = MODULE.subprocess.CompletedProcess(
            ["gh"],
            0,
            (
                "before\n"
                "-----BEGIN PRIVATE KEY-----\n"
                "private-base64-material\n"
                "-----END PRIVATE KEY-----\n"
                "after\n"
            ),
            "",
        )
        with (
            mock.patch.object(MODULE, "resolve_run_id", return_value=1),
            mock.patch.object(MODULE, "run", return_value=completed),
        ):
            content = MODULE.fetch_failed_check_log(self.preflight["pr"], check)

        self.assertEqual(
            f"before\n{MODULE.REDACTED_CREDENTIAL}\nafter\n",
            content,
        )
        self.assertNotIn("private-base64-material", content)
        MODULE.require_no_credentials(content, source="redacted private key log")

    def test_failed_log_retains_masked_and_placeholder_diagnostics(self):
        check = self.preflight["check_snapshot"]["failures"][0]
        completed = MODULE.subprocess.CompletedProcess(
            ["gh"],
            0,
            (
                "checkout Authorization: Basic ******\n"
                "test TOKEN=CONFIGURATION_SERVER failed\n"
            ),
            "",
        )
        with (
            mock.patch.object(MODULE, "resolve_run_id", return_value=1),
            mock.patch.object(MODULE, "run", return_value=completed),
        ):
            content = MODULE.fetch_failed_check_log(self.preflight["pr"], check)

        self.assertEqual(2, content.count(MODULE.REDACTED_CREDENTIAL))
        self.assertIn("checkout ", content)
        self.assertIn("test ", content)
        self.assertIn(" failed", content)
        MODULE.require_no_credentials(content, source="redacted test log")

    def test_failed_log_download_error_never_includes_raw_output(self):
        check = self.preflight["check_snapshot"]["failures"][0]
        completed = MODULE.subprocess.CompletedProcess(
            ["gh"],
            1,
            "RAW FAILURE LOG THAT MUST STAY OUT OF THE ERROR\n",
            "authentication failed\n",
        )
        with (
            mock.patch.object(MODULE, "resolve_run_id", return_value=1),
            mock.patch.object(MODULE, "run", return_value=completed),
            self.assertRaises(MODULE.WorkflowError) as raised,
        ):
            MODULE.fetch_failed_check_log(self.preflight["pr"], check)

        message = str(raised.exception)
        self.assertNotIn("RAW FAILURE LOG", message)
        self.assertNotIn("authentication failed", message)
        self.assertIn("exit status 1", message)
        self.assertIn(
            f"stdout bytes={len(completed.stdout.encode('utf-8'))}",
            message,
        )
        self.assertIn(MODULE.sha256_text(completed.stdout), message)

    def test_external_status_context_never_resolves_an_actions_job(self):
        check = {
            "kind": "status",
            "name": "zizmor",
            "url": "https://github.com/owner/repo/runs/104705281292",
            "description": "zizmor found an issue",
            "state": "FAILURE",
        }
        with mock.patch.object(MODULE, "resolve_run_id") as resolve:
            content = MODULE.fetch_failed_check_log(self.preflight["pr"], check)

        self.assertEqual("", content)
        resolve.assert_not_called()

    def test_external_check_run_page_never_resolves_an_actions_job(self):
        node = json.loads(EXTERNAL_ZIZMOR_CHECK.read_text(encoding="utf-8"))
        check = MODULE.normalize_rollup([node])[0]
        with mock.patch.object(MODULE, "resolve_run_id") as resolve:
            content = MODULE.fetch_failed_check_log(self.preflight["pr"], check)

        self.assertEqual("check_run", check["kind"])
        self.assertIsNone(check["workflow"])
        self.assertEqual("", content)
        resolve.assert_not_called()

    def test_accepts_noop_and_complete_relevant_validation(self):
        remote = self.remote()
        report = self.validate_report(self.report())
        self.assertEqual("no_change", report["outcome"])

    def test_accepts_a_fixed_result_with_ordered_commits_and_paths(self):
        commit = "5" * 40
        report = self.validate_report(
            self.report(
                commits=[commit],
                outcome="fixed",
                disposition="fixed",
                changed_paths=["src/widget.py"],
            ),
            commits=[commit],
        )
        self.assertEqual([commit], report["failures"][0]["fix_commits"])

    def test_accepts_multiple_ordered_commits_for_one_failure(self):
        commits = ["5" * 40, "6" * 40]
        report = self.validate_report(
            self.report(
                commits=commits,
                outcome="fixed",
                disposition="fixed",
                changed_paths=["src/widget.py"],
            ),
            commits=commits,
        )

        self.assertEqual(commits, report["failures"][0]["fix_commits"])
        self.assertEqual(commits[-1], report["failures"][0]["commit"])

    def test_accepts_legacy_v2_report_for_retained_task_recovery(self):
        commit = "5" * 40
        payload = json.loads(
            self.report(
                commits=[commit],
                outcome="fixed",
                disposition="fixed",
                changed_paths=["src/widget.py"],
            )
        )
        payload["schema"] = MODULE.LEGACY_CI_FIX_REPORT_SCHEMA
        payload["failures"][0]["log_sha256"] = self.preflight["check_snapshot"][
            "failures"
        ][0]["log_sha256"]
        payload["failures"][0]["commit"] = payload["failures"][0].pop("commits")[0]

        report = self.validate_report(
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            commits=[commit],
        )

        self.assertEqual(MODULE.CI_FIX_REPORT_SCHEMA, report["schema"])
        self.assertEqual([commit], report["failures"][0]["fix_commits"])

    def test_accepts_legacy_v3_report_for_retained_task_recovery(self):
        payload = json.loads(self.report())
        payload["schema"] = MODULE.LEGACY_CI_FIX_REPORT_SCHEMA_V3
        payload["failures"][0]["log_sha256"] = self.preflight["check_snapshot"][
            "failures"
        ][0]["log_sha256"]

        report = self.validate_report(
            json.dumps(payload, separators=(",", ":"), sort_keys=True)
        )

        self.assertEqual(MODULE.CI_FIX_REPORT_SCHEMA, report["schema"])

    def test_current_report_may_omit_cascading_failures(self):
        second = copy.deepcopy(self.preflight["check_snapshot"]["failures"][0])
        second.update(
            {
                "key": "check:CI/integration",
                "name": "integration",
                "log_path": str(self.root / "integration.log"),
            }
        )
        self.preflight["check_snapshot"]["failures"].append(second)

        report = self.validate_report(self.report())

        self.assertEqual(["check:CI/test"], [item["key"] for item in report["failures"]])

    def test_accepts_exact_policy_v3_compact_external_check_report(self):
        preflight, remote = self.compact_report_context()
        content = COMPACT_ZIZMOR_REPORT.read_text(encoding="utf-8")
        self.assertEqual(
            "567458e272999e0f3021b2746484d441d0b12de1f4998732773bf88bbc35888e",
            MODULE.sha256_text(content),
        )

        report = MODULE.validate_ci_fix_report(
            content,
            request_id="request-1",
            preflight=preflight,
            remote=remote,
            iteration_allowance=1,
        )

        failure = report["failures"][0]
        self.assertEqual("check:zizmor", failure["key"])
        self.assertEqual(MODULE.sha256_text(""), failure["log_sha256"])
        self.assertEqual(remote["commits"], failure["fix_commits"])
        self.assertEqual(remote["commits"][-1], failure["commit"])
        self.assertEqual(
            [".github/workflows/github-actions-queue-collector.yml"],
            report["changed_paths"],
        )

    def test_rejects_compact_external_check_report_identity_drift(self):
        preflight, remote = self.compact_report_context()
        original = MODULE.parse_markdown_report(
            COMPACT_ZIZMOR_REPORT.read_text(encoding="utf-8"),
            description="test report",
        )
        cases = {}
        wrong_id = copy.deepcopy(original)
        wrong_id["failing_checks"][0]["key"] = "104705281293"
        cases["numeric ID"] = (wrong_id, preflight, remote)
        wrong_name = copy.deepcopy(original)
        wrong_name["failing_checks"][0]["name"] = "other"
        cases["name"] = (wrong_name, preflight, remote)
        wrong_commits = copy.deepcopy(original)
        wrong_commits["ordered_commits"].reverse()
        cases["commit order"] = (wrong_commits, preflight, remote)
        wrong_fix_commits = copy.deepcopy(original)
        wrong_fix_commits["failing_checks"][0]["fix_commits"].reverse()
        cases["fix commit order"] = (wrong_fix_commits, preflight, remote)
        missing_report = copy.deepcopy(original)
        missing_report["changed_paths"].remove(remote["report_path"])
        cases["missing report path"] = (missing_report, preflight, remote)
        duplicate_report = copy.deepcopy(original)
        duplicate_report["changed_paths"].append(remote["report_path"])
        cases["duplicate report path"] = (duplicate_report, preflight, remote)
        extra_failure = copy.deepcopy(original)
        extra_failure["failing_checks"].append(
            copy.deepcopy(extra_failure["failing_checks"][0])
        )
        cases["extra failure"] = (extra_failure, preflight, remote)
        extra_key = copy.deepcopy(original)
        extra_key["unexpected"] = True
        cases["extra top-level key"] = (extra_key, preflight, remote)
        wrong_repo = copy.deepcopy(preflight)
        wrong_repo["check_snapshot"]["failures"][0]["url"] = (
            "https://github.com/other/repository/runs/104705281292"
        )
        cases["repository"] = (original, wrong_repo, remote)
        nonempty_log = copy.deepcopy(preflight)
        nonempty_log["check_snapshot"]["failures"][0]["log"] = "failure"
        nonempty_log["check_snapshot"]["failures"][0]["log_sha256"] = (
            MODULE.sha256_text("failure")
        )
        cases["nonempty log"] = (original, nonempty_log, remote)
        applied = copy.deepcopy(remote)
        applied["requires_apply"] = False
        cases["non-policy-v3 result"] = (original, preflight, applied)

        for name, (payload, case_preflight, case_remote) in cases.items():
            content = (
                "# CI Fix Loop Report\n\n```json\n"
                + json.dumps(payload, separators=(",", ":"), sort_keys=True)
                + "\n```\n"
            )
            with self.subTest(name=name), self.assertRaises(MODULE.WorkflowError):
                MODULE.validate_ci_fix_report(
                    content,
                    request_id="request-1",
                    preflight=case_preflight,
                    remote=case_remote,
                    iteration_allowance=1,
                )

    def test_accepts_rerun_preexisting_and_unfixable_outcomes(self):
        for outcome, disposition in (
            ("rerun", "flake"),
            ("pre_existing", "pre_existing"),
            ("unfixable", "unfixable"),
        ):
            with self.subTest(outcome=outcome):
                report = self.validate_report(
                    self.report(outcome=outcome, disposition=disposition)
                )
                self.assertEqual(outcome, report["outcome"])

    def test_rejects_malformed_mismatched_result_and_report(self):
        wrong = self.result()
        wrong["policy"]["sha256"] = "0" * 64
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.validate_success_result(
                wrong,
                preflight=self.preflight,
                requested_model="gpt-5.6-sol",
            )
        report = json.loads(self.report())
        report["pull_request"]["check_snapshot_sha256"] = "0" * 64
        with self.assertRaises(MODULE.WorkflowError):
            self.validate_report(json.dumps(report))
    def test_rejects_worker_validation_claim_and_incomplete_attestation(self):
        report = json.loads(self.report())
        report["validation"] = [
            {"name": "invented worker claim", "outcome": "passed"}
        ]
        with self.assertRaisesRegex(MODULE.WorkflowError, "malformed"):
            self.validate_report(json.dumps(report))
        incomplete = self.result()
        incomplete["attestation"]["structural_complete"] = False
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.validate_success_result(
                incomplete,
                preflight=self.preflight,
                requested_model="gpt-5.6-sol",
            )

    def test_rejects_credentials_in_result_report_and_logs(self):
        for source, value in (
            ("result", json.dumps(self.result()) + " token=secret-value"),
            ("report", self.report() + " password=hunter2"),
            ("log", "github_pat_abcdefghijklmnopqrstuvwxyz"),
        ):
            with self.subTest(source=source), self.assertRaisesRegex(
                MODULE.WorkflowError, "credentials"
            ):
                MODULE.require_no_credentials(value, source=source)

    def test_rejects_unexpected_history_merges_paths_and_commit_messages(self):
        commit = "5" * 40
        remote = self.remote([commit])
        with (
            mock.patch.object(MODULE, "git", return_value=""),
            self.assertRaisesRegex(MODULE.WorkflowError, "unexpected"),
        ):
            MODULE.validate_generated_history(
                self.root,
                base_sha=self.head,
                remote=remote,
                expected_paths=["src/widget.py"],
            )
        with (
            mock.patch.object(
                MODULE,
                "git",
                side_effect=[
                    f"{commit}\n{self.artifact}",
                    f"{commit} {self.head} {'9' * 40}",
                ],
            ),
            self.assertRaisesRegex(MODULE.WorkflowError, "merge"),
        ):
            MODULE.validate_generated_history(
                self.root,
                base_sha=self.head,
                remote=remote,
                expected_paths=["src/widget.py"],
            )

    def test_rejects_unexpected_artifact_and_fix_paths(self):
        commit = "5" * 40
        remote = self.remote([commit])
        messages = [
            f"{commit}\n{self.artifact}",
            f"{commit} {self.head}",
            f"{self.artifact} {commit}",
            (
                "Fix CI\n\nFinding: failing-test\n"
            ),
        ]
        with (
            mock.patch.object(MODULE, "git", side_effect=messages),
            mock.patch.object(
                MODULE,
                "git_z_paths",
                side_effect=[
                    [remote["report_path"], "unexpected.txt"],
                    ["src/widget.py"],
                ],
            ),
            self.assertRaisesRegex(MODULE.WorkflowError, "artifact commit"),
        ):
            MODULE.validate_generated_history(
                self.root,
                base_sha=self.head,
                remote=remote,
                expected_paths=["src/widget.py"],
            )

    def test_rejects_dirty_local_drift_stale_head_and_stale_checks(self):
        drifted = dict(self.preflight["pr"])
        drifted["head_sha"] = "9" * 40
        with self.assertRaisesRegex(MODULE.WorkflowError, "drifted"):
            MODULE.require_live_pr_snapshot(
                self.preflight["pr"], drifted, expected_head=self.head
            )
        with mock.patch.object(
            MODULE,
            "fetch_rollup",
            return_value=("9" * 40, []),
        ), self.assertRaisesRegex(MODULE.WorkflowError, "snapshot changed"):
            MODULE.require_live_check_snapshot(self.preflight)

    def test_waits_for_its_own_published_head_but_rejects_other_drift(self):
        fix = "5" * 40
        final = {**self.preflight["pr"], "head_sha": fix}
        with (
            mock.patch.object(
                MODULE,
                "metadata_for",
                side_effect=[self.preflight["pr"], final],
            ) as metadata,
            mock.patch.object(MODULE.time, "sleep") as sleep,
        ):
            actual = MODULE.wait_for_live_pr_snapshot(
                MODULE.parse_target("owner/repo#7"),
                self.preflight["pr"],
                expected_head=fix,
            )
        self.assertEqual(actual["head_sha"], fix)
        self.assertEqual(metadata.call_count, 2)
        sleep.assert_called_once()

        drifted = {**self.preflight["pr"], "base_sha": "9" * 40}
        with (
            mock.patch.object(MODULE, "metadata_for", return_value=drifted),
            mock.patch.object(MODULE.time, "sleep") as sleep,
            self.assertRaisesRegex(MODULE.WorkflowError, "drifted"),
        ):
            MODULE.wait_for_live_pr_snapshot(
                MODULE.parse_target("owner/repo#7"),
                self.preflight["pr"],
                expected_head=fix,
            )
        sleep.assert_not_called()

    def test_result_paths_must_be_outside_repository(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "outside"):
            MODULE.require_outside_repository(self.root / "result.json", self.root)
        MODULE.require_outside_repository(self.root.parent / "result.json", self.root)

    def test_recovery_command_preserves_task_identity_inputs(self):
        command = MODULE.agent_task_recovery_command(
            target="owner/repo#7",
            repo_root=self.root,
            state_path=self.root.parent / "state.json",
            model="sol",
        )
        self.assertIn("agent-task", command)
        self.assertIn("--resume", command)
        self.assertIn("--state", command)
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('"--input-result-file"', source)
        self.assertIn('"--result-file"', source)
        self.assertIn('"--policy"', source)

    def test_preserve_artifacts_keeps_exact_files_and_records_manifest(self):
        state_path = self.root / "state.json"
        prompt = self.root / "prompt.txt"
        result = self.root / "result.json"
        prior_result = self.root / "prior-result.json"
        prompt.write_bytes(b"exact prompt bytes\n")
        result.write_bytes(b'{"status":"success"}\n')
        prior_result.write_bytes(b'{"status":"error"}\n')
        state = {
            "version": MODULE.STATE_VERSION,
            "agent_task": {
                "prompt_file": str(prompt),
                "result_file": str(result),
                "prior_result_files": [str(prior_result)],
                "recovery_command": "resume",
                "recovery_files": [str(prompt), str(result)],
            }
        }

        MODULE.finalize_agent_task_artifacts(
            state_path,
            state,
            [prompt, result, prior_result],
            preserve=True,
        )

        stored = MODULE.load_state(state_path)["agent_task"]
        self.assertEqual(b"exact prompt bytes\n", prompt.read_bytes())
        self.assertEqual(b'{"status":"success"}\n', result.read_bytes())
        self.assertEqual(b'{"status":"error"}\n', prior_result.read_bytes())
        self.assertFalse(stored["artifacts_removed"])
        self.assertTrue(stored["artifacts_preserved"])
        self.assertEqual(str(prompt), stored["prompt_file"])
        self.assertEqual(str(result), stored["result_file"])
        self.assertEqual(
            [
                {
                    "path": str(prompt),
                    "sha256": MODULE.sha256_file(prompt),
                    "size": prompt.stat().st_size,
                },
                {
                    "path": str(result),
                    "sha256": MODULE.sha256_file(result),
                    "size": result.stat().st_size,
                },
                {
                    "path": str(prior_result),
                    "sha256": MODULE.sha256_file(prior_result),
                    "size": prior_result.stat().st_size,
                },
            ],
            stored["preserved_artifacts"],
        )
        self.assertNotIn("recovery_command", stored)
        self.assertNotIn("recovery_files", stored)

    def test_recovery_command_can_preserve_artifacts(self):
        command = MODULE.agent_task_recovery_command(
            target="owner/repo#7",
            repo_root=self.root,
            state_path=self.root.parent / "state.json",
            model="sol",
            preserve_artifacts=True,
        )
        args = MODULE.build_parser().parse_args(
            [
                "agent-task",
                "owner/repo#7",
                "--resume",
                "--preserve-artifacts",
            ]
        )

        self.assertIn('"--resume"', command)
        self.assertIn('"--preserve-artifacts"', command)
        self.assertTrue(args.preserve_artifacts)

    def test_recovery_rejects_a_failed_result_with_mismatched_identity(self):
        failed = self.result()
        failed["status"] = "error"
        failed["task"]["state"] = "in_progress"
        failed["application"] = {
            "status": "not_applied",
            "final_local_head": self.head,
        }
        failed["report"]["commit"] = None
        failed["report"]["sha256"] = None
        failed["attestation"]["structural_complete"] = False
        failed["error"] = {"code": "worker_failed", "message": "transient failure"}
        self.assertEqual(
            {
                "task_id": "task-1",
                "request_id": "request-1",
                "generated_branch": "copilot/agent-task",
                "generated_head": self.artifact,
            },
            MODULE.validate_recovery_result_identity(
                failed,
                preflight=self.preflight,
                requested_model="gpt-5.6-sol",
            ),
        )
        failed["pull_request"]["head_sha"] = "9" * 40
        with self.assertRaisesRegex(MODULE.WorkflowError, "pinned task identity"):
            MODULE.validate_recovery_result_identity(
                failed,
                preflight=self.preflight,
                requested_model="gpt-5.6-sol",
            )

    def test_failed_open_pr_task_cannot_resume_or_start_a_replacement(self):
        repo = self.root / "repo"
        repo.mkdir()
        state_path = self.root / "state.json"
        preflight = copy.deepcopy(self.preflight)
        preflight["repository_root"] = str(repo)
        report = self.report()
        failed = self.result()
        failed["status"] = "error"
        failed["task"]["state"] = "in_progress"
        failed["application"] = {
            "status": "not_applied",
            "final_local_head": self.head,
        }
        failed["report"]["commit"] = None
        failed["report"]["sha256"] = None
        failed["attestation"]["structural_complete"] = False
        failed["error"] = {"code": "worker_failed", "message": "transient failure"}
        helper_commands = []

        def run_helper(command, **kwargs):
            helper_commands.append(command)
            result_file = Path(command[command.index("--result-file") + 1])
            result_file.write_text(json.dumps(failed), encoding="utf-8")
            return MODULE.subprocess.CompletedProcess(command, 1, "", "")

        arguments = [
            "agent-task",
            self.preflight["pr"]["pr_url"],
            "--repo-root",
            str(repo),
            "--state",
            str(state_path),
        ]
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=repo),
            mock.patch.object(
                MODULE, "resolve_target", return_value={"repo_name": "owner/repo", "number": 7}
            ),
            mock.patch.object(
                MODULE,
                "agent_task_preflight",
                return_value=preflight,
            ),
            mock.patch.object(MODULE, "discover_cloud_task", return_value=self.root / "cloud_task.py"),
            mock.patch.object(MODULE, "run", side_effect=run_helper),
            mock.patch.object(MODULE, "local_identity", return_value=preflight["identity"]),
            mock.patch.object(
                MODULE, "fetch_committed_text", return_value=report
            ),
            mock.patch.object(MODULE, "validate_generated_history"),
            mock.patch.object(MODULE, "refuse_test_suppression"),
            mock.patch.object(MODULE, "require_live_check_snapshot"),
            mock.patch.object(MODULE, "metadata_for", return_value=preflight["pr"]),
            mock.patch.object(MODULE, "emit") as emit,
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "transient failure"):
                MODULE.command_agent_task(MODULE.build_parser().parse_args(arguments))
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "recovery is not permitted"
            ):
                MODULE.command_agent_task(
                    MODULE.build_parser().parse_args([*arguments, "--resume"])
                )

        self.assertEqual(1, len(helper_commands))
        self.assertNotIn("--resume-apply-with-report", helper_commands[0])
        state = MODULE.load_state(state_path)
        self.assertEqual("failed", state["agent_task"]["status"])
        self.assertNotIn("recovery_command", state["agent_task"])
        self.assertNotIn("retry_command", state["agent_task"])
        self.assertFalse(state["agent_task"].get("artifacts_removed", False))
        self.assertTrue(Path(state["agent_task"]["result_file"]).is_file())
        emit.assert_not_called()

    def test_taskless_failure_allows_one_fresh_replacement_without_recharging(self):
        repo = self.root / "repo"
        repo.mkdir()
        state_path = self.root / "state.json"
        preflight = copy.deepcopy(self.preflight)
        preflight["repository_root"] = str(repo)
        log_directory = self.root / "state--ci-fix-logs--retry"
        log_path = log_directory / "failed.log"
        preflight["check_snapshot"]["failures"][0]["log_path"] = str(log_path)
        failed = self.taskless_failure()
        helper_commands = []

        def preflight_for_retry(*_args, **_kwargs):
            log_directory.mkdir(exist_ok=True)
            log_path.write_bytes(b"AssertionError: expected 2\n")
            return preflight

        def run_helper(command, **kwargs):
            helper_commands.append(command)
            result_file = Path(command[command.index("--result-file") + 1])
            result_file.write_text(json.dumps(failed), encoding="utf-8")
            return MODULE.subprocess.CompletedProcess(command, 1, "", "")

        arguments = [
            "agent-task",
            self.preflight["pr"]["pr_url"],
            "--repo-root",
            str(repo),
            "--state",
            str(state_path),
            "--max-iterations",
            "1",
        ]
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=repo),
            mock.patch.object(
                MODULE,
                "resolve_target",
                return_value={"repo_name": "owner/repo", "number": 7},
            ),
            mock.patch.object(
                MODULE,
                "agent_task_preflight",
                side_effect=preflight_for_retry,
            ),
            mock.patch.object(
                MODULE,
                "discover_cloud_task",
                return_value=self.root / "cloud_task.py",
            ),
            mock.patch.object(MODULE, "run", side_effect=run_helper),
            mock.patch.object(
                MODULE,
                "local_identity",
                return_value=preflight["identity"],
            ),
            mock.patch.object(MODULE, "require_live_check_snapshot"),
        ):
            for _ in range(2):
                with self.assertRaisesRegex(MODULE.WorkflowError, "CCA enabled"):
                    MODULE.command_agent_task(
                        MODULE.build_parser().parse_args(arguments)
                    )

        self.assertEqual(2, len(helper_commands))
        self.assertNotIn("--resume", helper_commands[1])
        self.assertEqual(1, self.triage_worker_mock.call_count)
        self.assertEqual(1, self.retained_triage_mock.call_count)
        state = MODULE.load_state(state_path)
        self.assertEqual(1, state["iterations"])
        self.assertEqual("not_created", state["agent_task"]["task_id_status"])
        self.assertEqual(1, len(state["managed_task_history"]))

    def test_taskless_retry_exemption_requires_the_same_stable_snapshot(self):
        task = {"preflight": copy.deepcopy(self.preflight)}
        changed = copy.deepcopy(self.preflight)
        changed["check_snapshot"]["sha256"] = "9" * 64

        self.assertTrue(MODULE.task_matches_preflight(task, self.preflight))
        self.assertFalse(MODULE.task_matches_preflight(task, changed))

    def test_local_triage_failure_dispatches_no_hosted_task(self):
        repo = self.root / "repo"
        repo.mkdir()
        state_path = self.root / "state.json"
        preflight = copy.deepcopy(self.preflight)
        preflight["repository_root"] = str(repo)
        self.triage_worker_mock.side_effect = MODULE.WorkflowError(
            "triage failed"
        )
        arguments = MODULE.build_parser().parse_args(
            [
                "agent-task",
                self.preflight["pr"]["pr_url"],
                "--repo-root",
                str(repo),
                "--state",
                str(state_path),
            ]
        )
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=repo),
            mock.patch.object(
                MODULE,
                "resolve_target",
                return_value={"repo_name": "owner/repo", "number": 7},
            ),
            mock.patch.object(
                MODULE, "agent_task_preflight", return_value=preflight
            ),
            mock.patch.object(
                MODULE, "local_identity", return_value=preflight["identity"]
            ),
            mock.patch.object(MODULE, "require_live_check_snapshot"),
            mock.patch.object(MODULE, "discover_cloud_task") as discover,
            self.assertRaisesRegex(MODULE.WorkflowError, "triage failed"),
        ):
            MODULE.command_agent_task(arguments)

        discover.assert_not_called()
        state = MODULE.load_state(state_path)
        self.assertEqual("failed", state["agent_task"]["status"])
        self.assertEqual(
            "not_created", state["agent_task"]["task_id_status"]
        )
        self.assertIn("retry_command", state["agent_task"])

    def test_hosted_timeout_with_known_task_has_no_generic_recovery(self):
        repo = self.root / "repo"
        repo.mkdir()
        state_path = self.root / "state.json"
        preflight = copy.deepcopy(self.preflight)
        preflight["repository_root"] = str(repo)
        arguments = MODULE.build_parser().parse_args(
            [
                "agent-task",
                self.preflight["pr"]["pr_url"],
                "--repo-root",
                str(repo),
                "--state",
                str(state_path),
            ]
        )

        def timeout_with_identity(
            _command, *, state_path, run_id, **_kwargs
        ):
            state = MODULE.load_state(state_path)
            task = state["agent_task"]
            task["task_id_status"] = "known"
            task["task_id"] = "task-timeout"
            task["dispatch_identity"] = {
                "schema": MODULE.HOSTED_DISPATCH_IDENTITY_SCHEMA,
                "task_id": "task-timeout",
            }
            MODULE.save_state(state_path, state)
            raise MODULE.WorkflowError(
                "hosted Agent Task helper exceeded 7200 seconds; "
                "known task task-timeout"
            )

        self.hosted_helper_mock.side_effect = timeout_with_identity
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=repo),
            mock.patch.object(
                MODULE,
                "resolve_target",
                return_value={"repo_name": "owner/repo", "number": 7},
            ),
            mock.patch.object(
                MODULE, "agent_task_preflight", return_value=preflight
            ),
            mock.patch.object(
                MODULE,
                "local_identity",
                return_value=preflight["identity"],
            ),
            mock.patch.object(MODULE, "require_live_check_snapshot"),
            mock.patch.object(
                MODULE,
                "discover_cloud_task",
                return_value=self.root / "cloud_task.py",
            ),
            self.assertRaisesRegex(
                MODULE.WorkflowError, "known task task-timeout"
            ),
        ):
            MODULE.command_agent_task(arguments)

        task = MODULE.load_state(state_path)["agent_task"]
        self.assertEqual("failed", task["status"])
        self.assertEqual("known", task["task_id_status"])
        self.assertEqual("task-timeout", task["task_id"])
        self.assertNotIn("retry_command", task)
        self.assertNotIn("recovery_command", task)

    def test_managed_fix_publishes_only_the_verified_fix_commit(self):
        repo = self.root / "repo"
        repo.mkdir()
        state_path = self.root / "state.json"
        preflight = copy.deepcopy(self.preflight)
        preflight["repository_root"] = str(repo)
        commit = "5" * 40
        report = self.report(
            commits=[commit],
            outcome="fixed",
            disposition="fixed",
            changed_paths=["src/widget.py"],
        )
        result = self.result([commit])
        result["report"]["sha256"] = MODULE.sha256_text(report)
        commands = []

        def run_command(command, **kwargs):
            commands.append(command)
            if "--result-file" in command:
                Path(command[command.index("--result-file") + 1]).write_text(
                    json.dumps(result), encoding="utf-8"
                )
            return MODULE.subprocess.CompletedProcess(command, 0, "", "")

        arguments = MODULE.build_parser().parse_args(
            [
                "agent-task",
                self.preflight["pr"]["pr_url"],
                "--repo-root",
                str(repo),
                "--state",
                str(state_path),
            ]
        )
        imported_identity = {"branch": "feature", "head": commit, "status": ""}
        live_after_push = dict(preflight["pr"], head_sha=commit)
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=repo),
            mock.patch.object(
                MODULE, "resolve_target", return_value={"repo_name": "owner/repo", "number": 7}
            ),
            mock.patch.object(MODULE, "agent_task_preflight", return_value=preflight),
            mock.patch.object(MODULE, "discover_cloud_task", return_value=self.root / "cloud_task.py"),
            mock.patch.object(MODULE, "run", side_effect=run_command),
            mock.patch.object(MODULE, "local_identity", return_value=imported_identity),
            mock.patch.object(
                MODULE, "fetch_committed_text", return_value=report
            ),
            mock.patch.object(MODULE, "validate_generated_history"),
            mock.patch.object(MODULE, "refuse_test_suppression"),
            mock.patch.object(MODULE, "require_live_check_snapshot"),
            mock.patch.object(
                MODULE, "metadata_for", side_effect=[preflight["pr"], live_after_push]
            ),
            mock.patch.object(MODULE, "remote_head", return_value=self.head),
            mock.patch.object(MODULE, "find_push_remote", return_value="origin"),
            mock.patch.object(MODULE, "wait_for_remote_head", return_value=commit),
            mock.patch.object(MODULE, "emit") as emit,
        ):
            MODULE.command_agent_task(arguments)

        pushes = [
            command
            for command in commands
            if command[:4] == ["git", "-C", str(repo), "push"]
        ]
        self.assertEqual(
            [
                [
                    "git",
                    "-C",
                    str(repo),
                    "push",
                    f"--force-with-lease=refs/heads/feature:{self.head}",
                    "origin",
                    f"{commit}:feature",
                ]
            ],
            pushes,
        )
        self.assertEqual("published", emit.call_args.args[0]["result"])
        state = MODULE.load_state(state_path)
        self.assertEqual(commit, state["agent_task"]["published_head_sha"])
        self.assertEqual([commit], state["agent_task"]["ordered_commits"])

    def test_managed_fix_recovers_an_uncertain_push_without_pushing_twice(self):
        repo = self.root / "repo"
        repo.mkdir()
        state_path = self.root / "state.json"
        preflight = copy.deepcopy(self.preflight)
        preflight["repository_root"] = str(repo)
        commit = "5" * 40
        report = self.report(
            commits=[commit],
            outcome="fixed",
            disposition="fixed",
            changed_paths=["src/widget.py"],
        )
        result = self.result([commit])
        result["report"]["sha256"] = MODULE.sha256_text(report)
        commands = []

        def run_command(command, **kwargs):
            commands.append(command)
            if "--result-file" in command:
                Path(command[command.index("--result-file") + 1]).write_text(
                    json.dumps(result), encoding="utf-8"
                )
            return MODULE.subprocess.CompletedProcess(command, 0, "", "")

        raw_arguments = [
            "agent-task",
            self.preflight["pr"]["pr_url"],
            "--repo-root",
            str(repo),
            "--state",
            str(state_path),
        ]
        imported_identity = {"branch": "feature", "head": commit, "status": ""}
        live_after_push = dict(preflight["pr"], head_sha=commit)
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=repo),
            mock.patch.object(
                MODULE, "resolve_target", return_value={"repo_name": "owner/repo", "number": 7}
            ),
            mock.patch.object(MODULE, "agent_task_preflight", return_value=preflight),
            mock.patch.object(MODULE, "discover_cloud_task", return_value=self.root / "cloud_task.py"),
            mock.patch.object(MODULE, "run", side_effect=run_command),
            mock.patch.object(MODULE, "local_identity", return_value=imported_identity),
            mock.patch.object(
                MODULE,
                "fetch_committed_text",
                side_effect=[report, report],
            ),
            mock.patch.object(MODULE, "validate_generated_history"),
            mock.patch.object(MODULE, "refuse_test_suppression"),
            mock.patch.object(MODULE, "require_live_check_snapshot") as check_snapshot,
            mock.patch.object(
                MODULE,
                "metadata_for",
                side_effect=[
                    preflight["pr"],
                    MODULE.WorkflowError("PR head lookup failed"),
                    live_after_push,
                    live_after_push,
                ],
            ),
            mock.patch.object(MODULE, "remote_head", side_effect=[self.head, commit]),
            mock.patch.object(MODULE, "find_push_remote", return_value="origin"),
            mock.patch.object(MODULE, "wait_for_remote_head", return_value=commit),
            mock.patch.object(MODULE, "emit") as emit,
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "lookup failed"):
                MODULE.command_agent_task(
                    MODULE.build_parser().parse_args(raw_arguments)
                )
            MODULE.command_agent_task(
                MODULE.build_parser().parse_args([*raw_arguments, "--resume"])
            )

        pushes = [
            command
            for command in commands
            if command[:4] == ["git", "-C", str(repo), "push"]
        ]
        self.assertEqual(1, len(pushes))
        self.assertEqual(1, check_snapshot.call_count)
        self.assertEqual("published", emit.call_args.args[0]["result"])
        self.assertEqual(
            commit, MODULE.load_state(state_path)["agent_task"]["published_head_sha"]
        )

    def test_managed_iterations_share_the_budget_and_stop_at_the_cap(self):
        repo = self.root / "repo"
        repo.mkdir()
        state_path = self.root / "state.json"
        preflights = []
        reports = []
        results = []
        for iteration in range(3):
            preflight = copy.deepcopy(self.preflight)
            preflight["repository_root"] = str(repo)
            preflight["check_snapshot"]["observed_at"] = (
                f"2026-01-01T00:00:0{iteration}Z"
            )
            log = f"AssertionError: attempt {iteration}\n"
            log_path = self.root / f"failed-{iteration}.log"
            log_path.write_bytes(log.encode("utf-8"))
            preflight["check_snapshot"]["failures"][0]["log"] = log
            preflight["check_snapshot"]["failures"][0]["log_path"] = str(
                log_path
            )
            preflight["check_snapshot"]["failures"][0]["log_sha256"] = (
                MODULE.sha256_text(log)
            )
            preflight["check_snapshot"]["sha256"] = MODULE.check_snapshot_sha256(
                preflight["check_snapshot"]
            )
            report_payload = json.loads(self.report())
            report_payload["pull_request"]["check_snapshot_sha256"] = preflight[
                "check_snapshot"
            ]["sha256"]
            report = json.dumps(report_payload, separators=(",", ":"), sort_keys=True)
            result = self.result()
            result["report"]["sha256"] = MODULE.sha256_text(report)
            preflights.append(preflight)
            reports.append(report)
            results.append(result)
        helper_calls = 0

        def run_helper(command, **kwargs):
            nonlocal helper_calls
            result = results[helper_calls]
            helper_calls += 1
            Path(command[command.index("--result-file") + 1]).write_text(
                json.dumps(result), encoding="utf-8"
            )
            return MODULE.subprocess.CompletedProcess(command, 0, "", "")

        raw_arguments = [
            "agent-task",
            self.preflight["pr"]["pr_url"],
            "--repo-root",
            str(repo),
            "--state",
            str(state_path),
            "--max-iterations",
            "2",
        ]
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=repo),
            mock.patch.object(
                MODULE, "resolve_target", return_value={"repo_name": "owner/repo", "number": 7}
            ),
            mock.patch.object(MODULE, "agent_task_preflight", side_effect=preflights),
            mock.patch.object(MODULE, "discover_cloud_task", return_value=self.root / "cloud_task.py"),
            mock.patch.object(MODULE, "run", side_effect=run_helper),
            mock.patch.object(
                MODULE, "local_identity", return_value=preflights[0]["identity"]
            ),
            mock.patch.object(
                MODULE,
                "fetch_committed_text",
                side_effect=reports,
            ),
            mock.patch.object(MODULE, "validate_generated_history"),
            mock.patch.object(MODULE, "refuse_test_suppression"),
            mock.patch.object(MODULE, "require_live_check_snapshot"),
            mock.patch.object(MODULE, "metadata_for", return_value=preflights[0]["pr"]),
            mock.patch.object(MODULE, "emit") as emit,
        ):
            for _ in range(3):
                MODULE.command_agent_task(
                    MODULE.build_parser().parse_args(raw_arguments)
                )

        self.assertEqual(2, helper_calls)
        self.assertEqual("max_iterations_reached", emit.call_args.args[0]["result"])
        state = MODULE.load_state(state_path)
        self.assertEqual(2, state["iterations"])
        self.assertEqual("max_iterations_reached", state["escalation"]["reason"])

    def test_snapshot_identity_ignores_observation_time(self):
        first = copy.deepcopy(self.preflight["check_snapshot"])
        second = copy.deepcopy(first)
        second["observed_at"] = "2026-01-01T00:05:00Z"
        self.assertEqual(
            MODULE.check_snapshot_sha256(first),
            MODULE.check_snapshot_sha256(second),
        )

    def test_one_task_primitive_refuses_a_partial_check_suite(self):
        repo = self.root / "repo"
        repo.mkdir()
        state_path = self.root / "partial-state.json"
        preflight = copy.deepcopy(self.preflight)
        preflight["repository_root"] = str(repo)
        preflight["check_snapshot"]["decision"]["pending_checks"] = ["check:queued"]
        arguments = MODULE.build_parser().parse_args(
            [
                "agent-task",
                self.preflight["pr"]["pr_url"],
                "--repo-root",
                str(repo),
                "--state",
                str(state_path),
            ]
        )
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=repo),
            mock.patch.object(
                MODULE,
                "resolve_target",
                return_value={"repo_name": "owner/repo", "number": 7},
            ),
            mock.patch.object(
                MODULE, "agent_task_preflight", return_value=preflight
            ),
            mock.patch.object(MODULE, "discover_cloud_task") as discover,
            mock.patch.object(MODULE, "emit") as emit,
        ):
            MODULE.command_agent_task(arguments)

        self.assertEqual(emit.call_args.args[0]["result"], "waiting")
        discover.assert_not_called()

    def test_local_coordinator_waits_for_a_stable_terminal_check_set(self):
        pending = copy.deepcopy(self.preflight)
        pending["check_snapshot"]["decision"]["pending_checks"] = ["check:queued"]
        stable = copy.deepcopy(self.preflight)
        args = SimpleNamespace(
            wait_timeout=10,
            poll_interval=0,
            poll_max_interval=0,
            poll_jitter=0,
            stability_polls=2,
            debounce_seconds=0,
            stack_state=None,
        )
        state_path = self.root.parent / f"{self.root.name}-coordinator.json"
        self.addCleanup(state_path.unlink, missing_ok=True)
        with (
            mock.patch.object(
                MODULE,
                "agent_task_preflight",
                side_effect=[pending, stable, stable],
            ) as preflight,
            mock.patch.object(MODULE.time, "sleep"),
        ):
            result = MODULE.wait_for_stable_ci_preflight(
                args,
                repo_root=self.root,
                target={"repo_name": "owner/repo", "number": 7},
                state_path=state_path,
            )

        self.assertEqual(result["check_snapshot"]["sha256"], stable["check_snapshot"]["sha256"])
        self.assertEqual(preflight.call_count, 3)
        self.assertEqual(
            MODULE.load_state(state_path)["coordinator"]["status"],
            "ready",
        )

    def test_local_coordinator_skips_a_consumed_snapshot_until_it_changes(self):
        consumed = copy.deepcopy(self.preflight)
        fresh = copy.deepcopy(self.preflight)
        fresh["pr"]["head_sha"] = "9" * 40
        fresh["check_snapshot"]["head_sha"] = "9" * 40
        fresh["check_snapshot"]["sha256"] = "a" * 64
        state_path = self.root / "coordinator.json"
        MODULE.save_state(
            state_path,
            {
                "version": MODULE.STATE_VERSION,
                "coordinator": {
                    "processed_snapshots": [
                        {
                            "snapshot_sha256": consumed["check_snapshot"]["sha256"],
                            "head_sha": consumed["pr"]["head_sha"],
                            "task_id": "task-old",
                        }
                    ]
                },
            },
        )
        args = SimpleNamespace(
            wait_timeout=10,
            poll_interval=0,
            poll_max_interval=0,
            poll_jitter=0,
            stability_polls=2,
            debounce_seconds=0,
            stack_state=None,
        )
        with (
            mock.patch.object(
                MODULE,
                "agent_task_preflight",
                side_effect=[consumed, consumed, fresh, fresh],
            ) as preflight,
            mock.patch.object(MODULE.time, "sleep"),
        ):
            result = MODULE.wait_for_stable_ci_preflight(
                args,
                repo_root=self.root,
                target={"repo_name": "owner/repo", "number": 7},
                state_path=state_path,
            )

        self.assertEqual(result["pr"]["head_sha"], "9" * 40)
        self.assertEqual(preflight.call_count, 4)

    def test_local_coordinator_records_a_bounded_wait_timeout(self):
        state_path = self.root / "timeout.json"
        args = SimpleNamespace(
            wait_timeout=0,
            poll_interval=0,
            poll_max_interval=0,
            poll_jitter=0,
            stability_polls=2,
            debounce_seconds=0,
            stack_state=None,
        )
        with (
            mock.patch.object(MODULE, "agent_task_preflight") as preflight,
            self.assertRaisesRegex(MODULE.WorkflowError, "timed out"),
        ):
            MODULE.wait_for_stable_ci_preflight(
                args,
                repo_root=self.root,
                target={"repo_name": "owner/repo", "number": 7},
                state_path=state_path,
            )

        preflight.assert_not_called()
        self.assertEqual(
            MODULE.load_state(state_path)["coordinator"]["status"],
            "blocked",
        )

    def test_check_detail_failure_retains_pinned_identity(self):
        state_path = self.root.parent / f"{self.root.name}-detail.json"
        self.addCleanup(state_path.unlink, missing_ok=True)
        pr = {
            **self.preflight["pr"],
            "upstream_owner": "owner",
            "upstream_repo": "repo",
            "is_fork": False,
        }
        external = {
            "key": "status:zizmor",
            "kind": "status",
            "name": "zizmor",
            "workflow": None,
            "status": None,
            "conclusion": None,
            "state": "FAILURE",
            "class": "failed",
            "url": "https://github.com/owner/repo/runs/104705281292",
            "workflow_run_id": None,
            "started_at": "2026-09-16T07:55:00Z",
            "completed_at": None,
            "description": "zizmor found an issue",
        }
        external_second = {
            **external,
            "key": "status:zizmor-secondary",
            "name": "zizmor-secondary",
        }
        passed = {
            **external,
            "key": "check:CodeQL/analyze",
            "kind": "check_run",
            "name": "analyze",
            "state": None,
            "class": "passed",
            "conclusion": "SUCCESS",
            "url": "https://github.com/owner/repo/actions/runs/1/job/2",
            "description": None,
        }
        repository = {
            "permissions": {
                "admin": False,
                "maintain": False,
                "push": True,
                "triage": True,
                "pull": True,
            },
            "role_name": "write",
        }
        identity = self.preflight["identity"]
        download_attempts = 0

        def fetch_log(_pr, _check, destination, **_kwargs):
            nonlocal download_attempts
            download_attempts += 1
            if download_attempts == 1:
                MODULE.atomic_write_text(destination, "first failure\n")
                return "first failure\n"
            raise MODULE.WorkflowError("check detail lookup failed")

        with (
            mock.patch.object(MODULE, "git", return_value=""),
            mock.patch.object(MODULE, "metadata_for", return_value=pr),
            mock.patch.object(MODULE, "checkout_pr"),
            mock.patch.object(MODULE, "local_identity", return_value=identity),
            mock.patch.object(MODULE, "require_fork_head"),
            mock.patch.object(MODULE, "find_push_remote", return_value="origin"),
            mock.patch.object(
                MODULE, "gh_json", side_effect=[repository, {"login": "viewer"}]
            ),
            mock.patch.object(
                MODULE,
                "fetch_rollup",
                return_value=(self.head, [passed, external, external_second]),
            ),
            mock.patch.object(
                MODULE,
                "decide",
                return_value={
                    "decision": "failures",
                    "reason": "checks_failed",
                    "checks": [external["key"], external_second["key"]],
                    "pending_checks": [],
                    "detail": "zizmor failed",
                },
            ),
            mock.patch.object(MODULE, "baseline_conclusions", return_value={}),
            mock.patch.object(
                MODULE,
                "fetch_failed_check_log",
                side_effect=fetch_log,
            ),
            self.assertRaisesRegex(MODULE.WorkflowError, "check detail lookup failed"),
        ):
            MODULE.agent_task_preflight(
                self.root,
                {"repo_name": "owner/repo", "number": 7},
                state_path=state_path,
            )

        state = MODULE.load_state(state_path)
        self.assertEqual("owner/repo", state["pr"]["repo_name"])
        self.assertEqual(self.head, state["pr"]["head_sha"])
        self.assertEqual(self.base, state["pr"]["base_sha"])
        self.assertEqual(str(self.root), state["repo_root"])
        self.assertEqual(identity, state["preflight_identity"])
        self.assertEqual("preflighting", state["coordinator"]["status"])
        self.assertEqual(
            [],
            list(state_path.parent.glob(f"{state_path.stem}--ci-fix-logs-*")),
        )

    def test_local_coordinator_resumes_waiting_and_dispatches_each_new_snapshot_once(self):
        first = copy.deepcopy(self.preflight)
        second = copy.deepcopy(self.preflight)
        second["pr"]["head_sha"] = "9" * 40
        second["check_snapshot"]["head_sha"] = "9" * 40
        second["check_snapshot"]["sha256"] = "a" * 64
        repo_root = self.root / "repo"
        repo_root.mkdir()
        state_path = self.root / "coordinator.json"
        MODULE.save_state(
            state_path,
            {
                "version": MODULE.STATE_VERSION,
                "created_at": MODULE.utc_now(),
                "iterations": 0,
                "history": [],
            },
        )
        args = MODULE.build_parser().parse_args(
            [
                "loop",
                "owner/repo#7",
                "--repo-root",
                str(repo_root),
                "--state",
                str(state_path),
                "--poll-interval",
                "0",
                "--poll-max-interval",
                "0",
                "--debounce-seconds",
                "0",
            ]
        )
        dispatched = []

        def run_iteration(iteration_args):
            dispatched.append(iteration_args._preflight["check_snapshot"]["sha256"])
            MODULE.emit(
                {
                    "result": "published" if len(dispatched) == 1 else "escalated",
                    "state": str(state_path),
                    "task": {"id": f"task-{len(dispatched)}"},
                }
            )

        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=repo_root),
            mock.patch.object(
                MODULE,
                "resolve_target",
                return_value={"repo_name": "owner/repo", "number": 7},
            ),
            mock.patch.object(
                MODULE,
                "wait_for_stable_ci_preflight",
                side_effect=[first, second],
            ),
            mock.patch.object(
                MODULE, "command_agent_task", side_effect=run_iteration
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            MODULE.command_loop(args)

        self.assertEqual(
            dispatched,
            [first["check_snapshot"]["sha256"], second["check_snapshot"]["sha256"]],
        )
        processed = MODULE.load_state(state_path)["coordinator"]["processed_snapshots"]
        self.assertEqual([entry["task_id"] for entry in processed], ["task-1", "task-2"])

    def test_pipeline_owned_external_failure_records_state_and_dedupes_restart(self):
        repo_root = self.root / "repo"
        repo_root.mkdir()
        state_path = self.root / "coordinator.json"
        preflight = copy.deepcopy(self.preflight)
        preflight["repository_root"] = str(repo_root)
        external_key = "status:zizmor"
        preflight["check_snapshot"].update(
            {
                "rollup": [
                    {
                        "key": "check:CodeQL/analyze",
                        "name": "analyze",
                        "kind": "check_run",
                        "class": "passed",
                        "conclusion": "SUCCESS",
                    },
                    {
                        "key": external_key,
                        "name": "zizmor",
                        "kind": "status",
                        "class": "failed",
                        "state": "FAILURE",
                        "url": "https://zizmor.example/runs/104705281292",
                    },
                ],
                "decision": {
                    "decision": "failures",
                    "reason": "checks_failed",
                    "checks": [external_key],
                    "pending_checks": [],
                    "detail": "zizmor failed",
                },
                "failures": [
                    {
                        "key": external_key,
                        "name": "zizmor",
                        "workflow": None,
                        "url": "https://zizmor.example/runs/104705281292",
                        "conclusion": "FAILURE",
                        "baseline_conclusion": None,
                        "baseline_verdict": "unknown",
                        "log": "",
                        "log_path": str(self.root / "external-failure.log"),
                        "log_sha256": MODULE.sha256_text(""),
                    }
                ],
            }
        )
        (self.root / "external-failure.log").write_bytes(b"")
        preflight["check_snapshot"]["rollup_sha256"] = MODULE.sha256_text(
            json.dumps(
                preflight["check_snapshot"]["rollup"],
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        preflight["check_snapshot"]["sha256"] = MODULE.check_snapshot_sha256(
            preflight["check_snapshot"]
        )
        failed = self.result()
        failed["status"] = "error"
        failed["task"]["state"] = "in_progress"
        failed["application"] = {
            "status": "not_applied",
            "final_local_head": self.head,
        }
        failed["report"]["commit"] = None
        failed["report"]["sha256"] = None
        failed["attestation"]["structural_complete"] = False
        failed["error"] = {"code": "worker_failed", "message": "worker stopped"}
        helper_commands = []

        def run_helper(command, **kwargs):
            helper_commands.append(command)
            Path(command[command.index("--result-file") + 1]).write_text(
                json.dumps(failed), encoding="utf-8"
            )
            return MODULE.subprocess.CompletedProcess(command, 1, "", "")

        args = MODULE.build_parser().parse_args(
            [
                "loop",
                "owner/repo#7",
                "--repo-root",
                str(repo_root),
                "--state",
                str(state_path),
                "--new-invocation",
                "--pipeline-run",
                "bc204b55bc1240b18bc5193123ceb226",
                "--pipeline-iteration",
                "1",
                "--pipeline-max-iterations",
                "2",
            ]
        )
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=repo_root),
            mock.patch.object(
                MODULE,
                "resolve_target",
                return_value={"repo_name": "owner/repo", "number": 7},
            ),
            mock.patch.object(
                MODULE,
                "wait_for_stable_ci_preflight",
                return_value=preflight,
            ),
            mock.patch.object(MODULE, "require_live_check_snapshot"),
            mock.patch.object(
                MODULE,
                "discover_cloud_task",
                return_value=self.root / "cloud_task.py",
            ),
            mock.patch.object(MODULE, "run", side_effect=run_helper),
            mock.patch.object(
                MODULE, "local_identity", return_value=preflight["identity"]
            ),
        ):
            for expected in ("worker stopped", "unfinished Agent Task"):
                with self.assertRaisesRegex(MODULE.WorkflowError, expected):
                    MODULE.command_loop(args)

        self.assertEqual(1, len(helper_commands))
        state = MODULE.load_state(state_path)
        self.assertEqual("pipeline", state["budget_scope"])
        self.assertNotIn("invocation_budget", state)
        self.assertEqual("failed", state["agent_task"]["status"])
        self.assertEqual("blocked", state["coordinator"]["status"])
        self.assertEqual("coordinator_error", state["escalation"]["reason"])
        self.assertEqual(
            [external_key],
            state["agent_task"]["preflight"]["check_snapshot"]["decision"]["checks"],
        )

    def test_pending_rerun_transition_recovers_completed_checks_after_restart(self):
        state_path = self.root / "coordinator.json"
        MODULE.save_state(
            state_path,
            {
                "version": MODULE.STATE_VERSION,
                "created_at": MODULE.utc_now(),
                "iterations": 0,
                "history": [],
            },
        )
        result = {"result": "rerun", "task": {"id": "task-1"}}

        completed = MODULE.prepare_pending_ci_rerun(
            state_path,
            self.preflight,
            result,
            ["check:a", "check:b"],
        )
        MODULE.record_completed_ci_rerun(state_path, "check:a")
        recovered = MODULE.prepare_pending_ci_rerun(
            state_path,
            self.preflight,
            result,
            ["check:a", "check:b"],
        )

        self.assertEqual(completed, set())
        self.assertEqual(recovered, {"check:a"})
        coordinator = MODULE.load_state(state_path)["coordinator"]
        self.assertNotIn("processed_snapshots", coordinator)

    def test_local_coordinator_stops_when_rerun_is_unsupported(self):
        repo_root = self.root / "repo"
        repo_root.mkdir()
        state_path = self.root / "coordinator.json"
        MODULE.save_state(
            state_path,
            {
                "version": MODULE.STATE_VERSION,
                "created_at": MODULE.utc_now(),
                "iterations": 0,
                "history": [],
            },
        )
        args = MODULE.build_parser().parse_args(
            [
                "loop",
                "owner/repo#7",
                "--repo-root",
                str(repo_root),
                "--state",
                str(state_path),
            ]
        )

        def run_iteration(_args):
            MODULE.emit(
                {
                    "result": "rerun",
                    "state": str(state_path),
                    "task": {"id": "task-1"},
                    "action_checks": ["check:a"],
                }
            )

        def reject_rerun(_args):
            MODULE.emit(
                {
                    "result": "no_rerun_support",
                    "state": str(state_path),
                    "check": "check:a",
                }
            )

        output = io.StringIO()
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=repo_root),
            mock.patch.object(
                MODULE,
                "resolve_target",
                return_value={"repo_name": "owner/repo", "number": 7},
            ),
            mock.patch.object(
                MODULE,
                "wait_for_stable_ci_preflight",
                return_value=self.preflight,
            ) as wait,
            mock.patch.object(MODULE, "command_agent_task", side_effect=run_iteration),
            mock.patch.object(MODULE, "command_rerun", side_effect=reject_rerun),
            contextlib.redirect_stdout(output),
        ):
            MODULE.command_loop(args)

        self.assertEqual(json.loads(output.getvalue())["result"], "no_rerun_support")
        self.assertEqual(wait.call_count, 1)
        coordinator = MODULE.load_state(state_path)["coordinator"]
        self.assertNotIn("pending_rerun", coordinator)
        self.assertEqual(
            coordinator["processed_snapshots"][0]["snapshot_sha256"],
            self.preflight["check_snapshot"]["sha256"],
        )

    def test_cleanup_removes_only_state_owned_agent_task_artifacts(self):
        state_path = self.root / "state.json"
        prompt = self.root / "state--run--agent-task-prompt.txt"
        result = self.root / "state--run--agent-task-result.json"
        recovery = self.root / "state--run--agent-task-recovery-1.json"
        for artifact in (prompt, result, recovery):
            artifact.write_text("artifact", encoding="utf-8")
        MODULE.save_state(
            state_path,
            {
                "version": MODULE.STATE_VERSION,
                "agent_task": {
                    "prompt_file": str(prompt),
                    "result_file": str(result),
                    "recovery_results": [str(recovery)],
                },
            },
        )

        with mock.patch.object(MODULE, "emit"):
            MODULE.command_cleanup(SimpleNamespace(state=str(state_path)))

        self.assertFalse(state_path.exists())
        self.assertTrue(all(not artifact.exists() for artifact in (prompt, result, recovery)))


class LocalCiLogTriageTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        self.artifacts = Path(self.temporary.name) / "artifacts"
        self.artifacts.mkdir()
        self.log_path = self.artifacts / "failure.log"
        self.log_text = "FAILED test_widget.py::test_value\nAssertionError: 2 != 3\n"
        self.log_path.write_text(self.log_text, encoding="utf-8", newline="")
        failure = {
            "key": "check:CI/test",
            "kind": "check_run",
            "name": "test",
            "workflow": "CI",
            "url": "https://github.com/owner/repo/actions/runs/1/job/2",
            "conclusion": "FAILURE",
            "baseline_conclusion": "SUCCESS",
            "baseline_verdict": "pr_caused",
            "log_path": str(self.log_path),
            "log_sha256": MODULE.sha256_text(self.log_text),
        }
        snapshot = {
            "head_sha": "1" * 40,
            "base_sha": "2" * 40,
            "observed_at": "2026-01-01T00:00:00Z",
            "rollup": [],
            "rollup_sha256": MODULE.sha256_text("[]"),
            "decision": {
                "decision": "failures",
                "reason": "checks_failed",
                "checks": [failure["key"]],
                "detail": "test failed",
            },
            "failures": [failure],
        }
        snapshot["sha256"] = MODULE.check_snapshot_sha256(snapshot)
        self.preflight = {
            "repository_root": str(self.root),
            "identity": {"branch": "feature", "head": "1" * 40, "status": ""},
            "pr": {
                "repo_name": "owner/repo",
                "number": 7,
                "head_sha": "1" * 40,
                "base_sha": "2" * 40,
                "pr_url": "https://github.com/owner/repo/pull/7",
                "base_branch": "main",
                "head_repository": "owner/repo",
                "head_branch": "feature",
            },
            "check_snapshot": snapshot,
        }
        self.summary = (
            f"{MODULE.triage_identity_line(self.preflight)}\n"
            "The unit-test assertion is the root failure.\n"
        )

    def test_prompt_references_log_file_without_embedding_log_text(self):
        summary_path = self.artifacts / "summary.md"

        prompt = MODULE.build_triage_prompt(
            self.preflight, summary_path=summary_path
        )

        self.assertIn(json.dumps(str(self.log_path)), prompt)
        self.assertIn("rg or grep", prompt)
        self.assertIn("bounded line ranges", prompt)
        self.assertNotIn(self.log_text, prompt)

    def test_materialized_workspace_contains_only_pinned_logs(self):
        state_path = Path(self.temporary.name) / "state.json"
        state_path.write_text('{"coordinator":"private"}\n', encoding="utf-8")

        workspace = MODULE.materialize_local_triage_workspace(
            self.preflight,
            state_path=state_path,
            run_id="run-1",
        )

        moved_log = Path(
            self.preflight["check_snapshot"]["failures"][0]["log_path"]
        )
        self.assertEqual(workspace, moved_log.parent)
        self.assertEqual([moved_log], list(workspace.iterdir()))
        self.assertFalse(self.log_path.exists())
        self.assertNotEqual(state_path.parent, workspace)

    def test_summary_is_bounded_nonempty_and_snapshot_bound(self):
        summary_path = self.artifacts / "summary.md"
        summary_path.write_text(self.summary, encoding="utf-8", newline="")
        self.assertEqual(
            self.summary,
            MODULE.validate_triage_summary(summary_path, self.preflight),
        )

        invalid = {
            "empty": "",
            "stale": "<!-- ci-fix-loop-triage-v1 snapshot=bad head=bad -->\ntext\n",
            "marker only": f"{MODULE.triage_identity_line(self.preflight)}\n",
            "reserved boundary": (
                f"{MODULE.triage_identity_line(self.preflight)}\n"
                f"{MODULE.TRIAGE_SUMMARY_BOUNDARIES[1]}\n"
            ),
            "oversized": (
                f"{MODULE.triage_identity_line(self.preflight)}\n"
                + "x" * MODULE.MAX_TRIAGE_SUMMARY_BYTES
            ),
        }
        for name, content in invalid.items():
            with self.subTest(name=name):
                summary_path.write_text(content, encoding="utf-8", newline="")
                with self.assertRaises(MODULE.WorkflowError):
                    MODULE.validate_triage_summary(summary_path, self.preflight)

    def test_hosted_prompt_receives_summary_unchanged_and_no_raw_log(self):
        prompt = MODULE.build_worker_prompt(
            self.preflight,
            iteration_allowance=1,
            prior_history=[],
            requested_model="gpt-5.6-sol",
            triage_summary=self.summary,
        )

        start = "----- BEGIN LOCAL CI TRIAGE SUMMARY -----\n"
        end = "----- END LOCAL CI TRIAGE SUMMARY -----\n"
        handed_off = prompt.split(start, 1)[1].split(end, 1)[0]
        self.assertEqual(self.summary, handed_off)
        self.assertNotIn(self.log_text, prompt)
        self.assertNotIn(str(self.log_path), prompt)

    def test_local_worker_uses_default_agent_and_verified_artifacts(self):
        prompt_path = self.artifacts / "prompt.txt"
        summary_path = self.artifacts / "summary.md"
        result_path = self.artifacts / "result.json"
        prompt_path.write_text(
            MODULE.build_triage_prompt(
                self.preflight, summary_path=summary_path
            ),
            encoding="utf-8",
            newline="",
        )
        summary_path.write_text(self.summary, encoding="utf-8", newline="")
        identity = self.preflight["identity"]
        github = {"pull_request": "pinned"}
        attestation = {"session_id": "session-1", "events_sha256": "a" * 64}
        completed = MODULE.subprocess.CompletedProcess(["copilot"], 0, "", "")
        with (
            mock.patch.object(MODULE, "run", return_value=completed) as run,
            mock.patch.object(MODULE, "local_identity", return_value=identity),
            mock.patch.object(
                MODULE, "github_triage_fingerprint", return_value=github
            ),
            mock.patch.object(
                MODULE,
                "local_session_model_attestation",
                return_value=attestation,
            ),
        ):
            summary = MODULE.run_local_triage_worker(
                repo_root=self.root,
                target={"repo_name": "owner/repo", "number": 7},
                preflight=self.preflight,
                prompt_path=prompt_path,
                summary_path=summary_path,
                result_path=result_path,
                run_id="run-1",
                session_id="session-1",
                before_source=identity,
                before_github=github,
            )

        self.assertEqual(self.summary, summary)
        command = run.call_args.args[0]
        self.assertEqual("copilot", command[0])
        self.assertNotIn("--agent", command)
        self.assertIn("--no-custom-instructions", command)
        self.assertIn("--no-remote", command)
        self.assertNotIn("--allow-all-paths", command)
        self.assertEqual(self.artifacts, run.call_args.kwargs["cwd"])
        self.assertEqual("", run.call_args.kwargs["env"]["GH_TOKEN"])
        self.assertEqual("", run.call_args.kwargs["env"]["GITHUB_TOKEN"])
        self.assertNotEqual(
            str(Path.home() / ".config" / "gh"),
            run.call_args.kwargs["env"]["GH_CONFIG_DIR"],
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(MODULE.LOCAL_TRIAGE_RESULT_SCHEMA, result["schema"])
        self.assertEqual(MODULE.LOCAL_TRIAGE_MODEL, result["worker"]["model"])

    def test_local_worker_fails_closed_on_source_change(self):
        prompt_path = self.artifacts / "prompt.txt"
        summary_path = self.artifacts / "summary.md"
        result_path = self.artifacts / "result.json"
        prompt_path.write_text("prompt\n", encoding="utf-8", newline="")
        summary_path.write_text(self.summary, encoding="utf-8", newline="")
        completed = MODULE.subprocess.CompletedProcess(["copilot"], 0, "", "")
        with (
            mock.patch.object(MODULE, "run", return_value=completed),
            mock.patch.object(
                MODULE,
                "local_identity",
                return_value={"branch": "feature", "head": "9" * 40, "status": ""},
            ),
            mock.patch.object(
                MODULE,
                "github_triage_fingerprint",
                return_value={"pull_request": "pinned"},
            ),
            self.assertRaisesRegex(MODULE.WorkflowError, "repository source"),
        ):
            MODULE.run_local_triage_worker(
                repo_root=self.root,
                target={"repo_name": "owner/repo", "number": 7},
                preflight=self.preflight,
                prompt_path=prompt_path,
                summary_path=summary_path,
                result_path=result_path,
                run_id="run-1",
                session_id="session-1",
                before_source=self.preflight["identity"],
                before_github={"pull_request": "pinned"},
            )

    def test_local_worker_never_traverses_replaced_gh_config_directory(self):
        prompt_path = self.artifacts / "prompt.txt"
        summary_path = self.artifacts / "summary.md"
        result_path = self.artifacts / "result.json"
        prompt_path.write_text("prompt\n", encoding="utf-8", newline="")
        preserved_entry = None

        def replace_config(_command, **kwargs):
            nonlocal preserved_entry
            config_dir = Path(kwargs["env"]["GH_CONFIG_DIR"])
            preserved_entry = config_dir / "keep.txt"
            preserved_entry.write_text("keep\n", encoding="utf-8")
            return MODULE.subprocess.CompletedProcess(["copilot"], 0, "", "")

        with (
            mock.patch.object(MODULE, "run", side_effect=replace_config),
            mock.patch.object(
                Path,
                "is_junction",
                autospec=True,
                side_effect=lambda path: path.name.endswith("--gh-config"),
            ),
            self.assertRaisesRegex(
                MODULE.WorkflowError,
                "replaced its GitHub configuration directory",
            ),
        ):
            MODULE.run_local_triage_worker(
                repo_root=self.root,
                target={"repo_name": "owner/repo", "number": 7},
                preflight=self.preflight,
                prompt_path=prompt_path,
                summary_path=summary_path,
                result_path=result_path,
                run_id="run-1",
                session_id="session-1",
                before_source=self.preflight["identity"],
                before_github={"pull_request": "pinned"},
            )

        self.assertIsNotNone(preserved_entry)
        self.assertTrue(preserved_entry.is_file())

    def test_local_session_attests_model_effort_and_assistant_event(self):
        copilot_home = self.artifacts / "copilot-home"
        events_path = (
            copilot_home / "session-state" / "session-1" / "events.jsonl"
        )
        events_path.parent.mkdir(parents=True)
        events_path.write_text(
            "\n".join(
                json.dumps(event)
                for event in (
                    {
                        "type": "session.start",
                        "data": {
                            "sessionId": "session-1",
                            "selectedModel": MODULE.LOCAL_TRIAGE_MODEL,
                            "reasoningEffort": (
                                MODULE.LOCAL_TRIAGE_REASONING_EFFORT
                            ),
                        },
                    },
                    {
                        "type": "assistant.message",
                        "data": {
                            "model": MODULE.LOCAL_TRIAGE_MODEL,
                            "content": "complete",
                        },
                    },
                )
            )
            + "\n",
            encoding="utf-8",
            newline="",
        )

        with mock.patch.dict(
            os.environ, {"COPILOT_HOME": str(copilot_home)}
        ):
            attestation = MODULE.local_session_model_attestation("session-1")

        self.assertEqual(
            MODULE.LOCAL_TRIAGE_MODEL, attestation["startup_model"]
        )
        self.assertEqual(
            MODULE.LOCAL_TRIAGE_REASONING_EFFORT,
            attestation["startup_reasoning_effort"],
        )
        self.assertEqual(1, attestation["assistant_message_count"])

    def test_local_session_rejects_model_changes(self):
        copilot_home = self.artifacts / "changed-model-home"
        events_path = (
            copilot_home / "session-state" / "session-2" / "events.jsonl"
        )
        events_path.parent.mkdir(parents=True)
        events_path.write_text(
            "\n".join(
                json.dumps(event)
                for event in (
                    {
                        "type": "session.start",
                        "data": {
                            "sessionId": "session-2",
                            "selectedModel": MODULE.LOCAL_TRIAGE_MODEL,
                            "reasoningEffort": (
                                MODULE.LOCAL_TRIAGE_REASONING_EFFORT
                            ),
                        },
                    },
                    {
                        "type": "session.model_change",
                        "data": {
                            "newModel": "claude-sonnet-5",
                            "reasoningEffort": "high",
                        },
                    },
                    {
                        "type": "assistant.message",
                        "data": {
                            "model": "claude-sonnet-5",
                            "content": "complete",
                        },
                    },
                )
            )
            + "\n",
            encoding="utf-8",
            newline="",
        )

        with (
            mock.patch.dict(
                os.environ, {"COPILOT_HOME": str(copilot_home)}
            ),
            self.assertRaisesRegex(
                MODULE.WorkflowError, "model attestation mismatch"
            ),
        ):
            MODULE.local_session_model_attestation("session-2")


class EscalationCatalogTest(unittest.TestCase):
    def test_every_reason_carries_a_concrete_next_action(self):
        for reason in MODULE.ESCALATION_REASONS:
            self.assertIn(reason, MODULE.ESCALATION_ACTIONS)
            self.assertTrue(MODULE.ESCALATION_ACTIONS[reason].strip())

    def test_the_iteration_cap_and_rerun_cap_match_the_design(self):
        self.assertEqual(5, MODULE.DEFAULT_MAX_ITERATIONS)
        self.assertEqual(1, MODULE.MAX_RERUNS_PER_CHECK)

    def test_verdicts_are_exactly_the_three_the_loop_understands(self):
        self.assertEqual(("pr_caused", "pre_existing", "flake"), MODULE.VERDICTS)


class ParseTargetTest(unittest.TestCase):
    def test_accepts_a_pull_request_url(self):
        target = MODULE.parse_target("https://github.com/owner/repo/pull/7")
        self.assertEqual("owner", target["owner"])
        self.assertEqual("repo", target["repo"])
        self.assertEqual(7, target["number"])
        self.assertEqual("owner/repo", target["repo_name"])

    def test_accepts_a_url_with_a_fragment(self):
        target = MODULE.parse_target(
            "https://github.com/owner/repo/pull/7#issuecomment-1"
        )
        self.assertEqual(7, target["number"])

    def test_accepts_owner_repo_number(self):
        target = MODULE.parse_target("owner/repo#42")
        self.assertEqual("https://github.com/owner/repo/pull/42", target["pr_url"])

    def test_rejects_a_bare_number(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.parse_target("42")

    def test_rejects_an_issue_url(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.parse_target("https://github.com/owner/repo/issues/7")


def ci_gh_metadata(**overrides):
    payload = {
        "number": 7,
        "title": "Add a thing",
        "url": "https://github.com/owner/repo/pull/7",
        "state": "OPEN",
        "isDraft": False,
        "headRefName": "feature",
        "headRefOid": "head1",
        "headRepositoryOwner": {"login": "fork"},
        "headRepository": {"name": "repo"},
        "baseRefName": "main",
        "baseRefOid": "frozen",
        "commits": [{"oid": "head1", "messageHeadline": "Add a thing"}],
    }
    payload.update(overrides)
    return payload


class PullRequestMetadataTest(unittest.TestCase):
    def test_base_sha_is_the_live_base_branch_tip_not_the_frozen_base_ref_oid(self):
        target = MODULE.parse_target("owner/repo#7")
        with mock.patch.object(
            MODULE, "gh_json", return_value=ci_gh_metadata()
        ), mock.patch.object(
            MODULE, "base_ref_tip", return_value="live-tip"
        ) as tip:
            metadata = MODULE.metadata_for(target)
        # The base commit is what baseline_conclusions attributes against, so it
        # must be the branch's live tip, never GitHub's frozen baseRefOid.
        self.assertEqual("live-tip", metadata["base_sha"])
        self.assertEqual("main", metadata["base_branch"])
        tip.assert_called_once_with("owner/repo", "main")

    def test_a_missing_base_branch_is_rejected(self):
        target = MODULE.parse_target("owner/repo#7")
        with mock.patch.object(
            MODULE, "gh_json", return_value=ci_gh_metadata(baseRefName=None)
        ), mock.patch.object(MODULE, "base_ref_tip", return_value="live-tip"):
            with self.assertRaisesRegex(MODULE.WorkflowError, "no base branch"):
                MODULE.metadata_for(target)


class BaseRefTipTest(unittest.TestCase):
    def test_returns_the_live_tip_from_the_branch_ref(self):
        response = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"object": {"sha": "live-tip", "type": "commit"}}),
            stderr="",
        )
        with mock.patch.object(MODULE, "run", return_value=response) as run:
            self.assertEqual("live-tip", MODULE.base_ref_tip("owner/repo", "main"))
        self.assertEqual(
            ["gh", "api", "repos/owner/repo/git/ref/heads/main"],
            run.call_args.args[0],
        )

    def test_a_deleted_base_branch_raises_rather_than_falling_back(self):
        response = SimpleNamespace(
            returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)"
        )
        with mock.patch.object(MODULE, "run", return_value=response):
            with self.assertRaisesRegex(MODULE.WorkflowError, "may have been deleted"):
                MODULE.base_ref_tip("owner/repo", "gone")


class AuthoritativeDiffTest(unittest.TestCase):
    def setUp(self):
        self.root = Path("repo")
        self.pr = {
            "pr_url": "https://github.com/owner/repo/pull/7",
            "repo_name": "owner/repo",
            "base_branch": "main",
            "base_sha": "base1",
            "head_sha": "head1",
        }

    def response(self, returncode=0, stdout="", stderr=""):
        return SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr
        )

    def test_uses_the_github_rendered_diff_when_available(self):
        with mock.patch.object(
            MODULE, "run", return_value=self.response(stdout=DIFF)
        ) as run:
            result, source = MODULE.fetch_authoritative_diff(self.root, self.pr)

        self.assertEqual(DIFF, result)
        self.assertEqual("github", source)
        run.assert_called_once_with(
            [
                "gh",
                "pr",
                "diff",
                self.pr["pr_url"],
                "--repo",
                self.pr["repo_name"],
            ],
            check=False,
        )

    def test_uses_a_local_merge_base_diff_when_github_rejects_its_size(self):
        responses = [
            self.response(
                returncode=1,
                stderr="HTTP 406: PullRequest.diff too_large",
            ),
            self.response(),
            self.response(stdout="false\n"),
            self.response(stdout=DIFF),
        ]
        with mock.patch.object(MODULE, "run", side_effect=responses) as run:
            result, source = MODULE.fetch_authoritative_diff(self.root, self.pr)

        self.assertEqual(DIFF, result)
        self.assertEqual("local_merge_base", source)
        self.assertEqual(
            [
                "git",
                "-c",
                "diff.noprefix=false",
                "-c",
                "diff.algorithm=myers",
                "-c",
                "diff.context=3",
                "-c",
                "diff.renames=true",
                "-C",
                str(self.root),
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--no-relative",
                "--find-renames",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                "-U3",
                "base1...head1",
                "--",
            ],
            run.call_args_list[-1].args[0],
        )

    def test_fetches_the_base_branch_when_the_pinned_commit_is_missing(self):
        responses = [
            self.response(
                returncode=1,
                stderr="PullRequest.diff too_large (HTTP 406)",
            ),
            self.response(returncode=1),
            self.response(stdout="origin\n"),
            self.response(stdout="git@github.com:owner/repo.git\n"),
            self.response(),
            self.response(),
            self.response(stdout="false\n"),
            self.response(stdout=DIFF),
        ]
        with mock.patch.object(MODULE, "run", side_effect=responses) as run:
            result, source = MODULE.fetch_authoritative_diff(self.root, self.pr)

        self.assertEqual(DIFF, result)
        self.assertEqual("local_merge_base", source)
        self.assertEqual(
            [
                "git",
                "-C",
                str(self.root),
                "fetch",
                "--no-tags",
                "origin",
                "refs/heads/main",
            ],
            run.call_args_list[4].args[0],
        )
        self.assertEqual(
            {"GIT_TERMINAL_PROMPT": "0"},
            run.call_args_list[4].kwargs["env"],
        )

    def test_preserves_non_size_diff_failures(self):
        with mock.patch.object(
            MODULE,
            "run",
            return_value=self.response(
                returncode=1, stderr="HTTP 403: Resource not accessible"
            ),
        ) as run:
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "Resource not accessible"
            ):
                MODULE.fetch_authoritative_diff(self.root, self.pr)

        self.assertEqual(1, run.call_count)

    def test_does_not_fall_back_for_another_too_large_resource(self):
        with mock.patch.object(
            MODULE,
            "run",
            return_value=self.response(
                returncode=1, stderr="HTTP 406: CheckRun.output too_large"
            ),
        ) as run:
            with self.assertRaisesRegex(MODULE.WorkflowError, "CheckRun.output"):
                MODULE.fetch_authoritative_diff(self.root, self.pr)

        self.assertEqual(1, run.call_count)

    def test_reads_every_local_changed_path_from_nul_delimited_output(self):
        with mock.patch.object(
            MODULE,
            "run",
            return_value=self.response(stdout="z.py\0dir/a\nb.py\0z.py\0"),
        ) as run:
            paths = MODULE.changed_files_from_local_diff(self.root, self.pr)

        self.assertEqual(["dir/a\nb.py", "z.py"], paths)
        self.assertIn("-z", run.call_args.args[0])
        self.assertIn("base1...head1", run.call_args.args[0])


class PathHelperTest(unittest.TestCase):
    def test_state_path_uses_the_orchestrator_naming(self):
        target = MODULE.parse_target("owner/repo#7")
        path = MODULE.default_state_path(target)
        self.assertEqual("owner--repo--7.json", path.name)
        self.assertEqual("ci-fix-loop", path.parent.name)
        self.assertEqual("run", path.parent.parent.name)

    def test_stack_member_state_is_scoped_to_the_coordinator_run(self):
        coordinator = Path("stacks/owner--repo--stack-3--run-1.json")
        path = MODULE.stack_member_state_path(coordinator, 7)
        self.assertEqual("owner--repo--stack-3--run-1--pr-7.json", path.name)
        self.assertEqual(coordinator.parent, path.parent)

    def test_stack_propagation_state_is_scoped_to_the_coordinator_run(self):
        coordinator = Path("stacks/owner--repo--stack-3--run-1.json")
        path = MODULE.stack_propagation_state_path(coordinator, 5, "abc123")
        self.assertEqual(
            "owner--repo--stack-3--run-1--propagate-pr-5-abc123.json",
            path.name,
        )
        self.assertEqual(coordinator.parent, path.parent)

    def test_side_files_hang_off_the_state_path(self):
        path = Path("/tmp/state.json")
        self.assertEqual("state.json.diff", MODULE.diff_path_for(path).name)
        self.assertEqual(
            "state.json.preflight.json", MODULE.preflight_path_for(path).name
        )
        self.assertEqual("state.json.checks.json", MODULE.checks_path_for(path).name)
        self.assertEqual("state.json.status.json", MODULE.status_path_for(path).name)

    def test_normalizes_git_bash_paths_only_on_windows(self):
        self.assertEqual(
            "C:/Users/x/.copilot",
            MODULE.normalize_cli_path("/c/Users/x/.copilot", windows=True),
        )
        self.assertEqual(
            "/c/Users/x/.copilot",
            MODULE.normalize_cli_path("/c/Users/x/.copilot", windows=False),
        )

    def test_reads_a_github_repository_from_any_remote_form(self):
        self.assertEqual(
            "owner/repo",
            MODULE.github_repo_from_remote("https://github.com/owner/repo.git"),
        )
        self.assertEqual(
            "owner/repo", MODULE.github_repo_from_remote("git@github.com:owner/repo")
        )
        self.assertIsNone(MODULE.github_repo_from_remote("https://example.com/o/r"))


class ClassificationTest(unittest.TestCase):
    def test_maps_completed_check_run_conclusions(self):
        self.assertEqual("passed", MODULE.classify_check_run("COMPLETED", "SUCCESS"))
        self.assertEqual("neutral", MODULE.classify_check_run("COMPLETED", "SKIPPED"))
        self.assertEqual("failed", MODULE.classify_check_run("COMPLETED", "FAILURE"))
        self.assertEqual("failed", MODULE.classify_check_run("COMPLETED", "TIMED_OUT"))
        self.assertEqual("failed", MODULE.classify_check_run("COMPLETED", "CANCELLED"))
        self.assertEqual("stale", MODULE.classify_check_run("COMPLETED", "STALE"))

    def test_treats_action_required_as_blocked_on_an_approval(self):
        self.assertEqual(
            "approval_blocked", MODULE.classify_check_run("COMPLETED", "ACTION_REQUIRED")
        )
        self.assertEqual("approval_blocked", MODULE.classify_check_run("WAITING", ""))

    def test_maps_incomplete_check_run_statuses(self):
        self.assertEqual("not_started", MODULE.classify_check_run("QUEUED", ""))
        self.assertEqual("running", MODULE.classify_check_run("IN_PROGRESS", ""))

    def test_maps_status_contexts(self):
        self.assertEqual("passed", MODULE.classify_status_context("SUCCESS"))
        self.assertEqual("running", MODULE.classify_status_context("PENDING"))
        self.assertEqual("not_started", MODULE.classify_status_context("EXPECTED"))
        self.assertEqual("failed", MODULE.classify_status_context("ERROR"))

    def test_an_unrecognized_state_is_unknown_rather_than_passing(self):
        self.assertEqual("unknown", MODULE.classify_check_run("COMPLETED", "WAT"))
        self.assertEqual("unknown", MODULE.classify_check_run("WAT", ""))
        self.assertEqual("unknown", MODULE.classify_status_context("WAT"))


class NormalizeRollupTest(unittest.TestCase):
    def test_an_absent_rollup_is_an_empty_list(self):
        self.assertEqual([], MODULE.normalize_rollup(None))

    def test_normalizes_a_check_run(self):
        checks = MODULE.normalize_rollup(
            [
                {
                    "__typename": "CheckRun",
                    "name": "build",
                    "workflowName": "CI",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                    "detailsUrl": "https://github.com/o/r/actions/runs/1/job/2",
                }
            ]
        )
        self.assertEqual(1, len(checks))
        self.assertEqual("check:CI/build", checks[0]["key"])
        self.assertEqual("failed", checks[0]["class"])
        self.assertEqual("check_run", checks[0]["kind"])
        self.assertEqual(1, checks[0]["workflow_run_id"])

    def test_normalizes_a_status_context(self):
        checks = MODULE.normalize_rollup(
            [
                {
                    "__typename": "StatusContext",
                    "context": "ci/external",
                    "state": "FAILURE",
                    "targetUrl": "https://ci.example.com/1",
                }
            ]
        )
        self.assertEqual("status:ci/external", checks[0]["key"])
        self.assertEqual("failed", checks[0]["class"])
        self.assertEqual("status", checks[0]["kind"])
        self.assertIsNone(checks[0]["workflow_run_id"])

    def test_a_check_run_without_a_workflow_keeps_a_bare_key(self):
        checks = MODULE.normalize_rollup(
            [{"__typename": "CheckRun", "name": "build", "status": "IN_PROGRESS"}]
        )
        self.assertEqual("check:build", checks[0]["key"])

    def test_duplicate_keys_are_suffixed_rather_than_dropped(self):
        checks = MODULE.normalize_rollup(
            [
                {"__typename": "CheckRun", "name": "test", "status": "COMPLETED",
                 "conclusion": "SUCCESS"},
                {"__typename": "CheckRun", "name": "test", "status": "COMPLETED",
                 "conclusion": "FAILURE"},
            ]
        )
        self.assertEqual(["check:test", "check:test#2"], [c["key"] for c in checks])
        self.assertEqual(["passed", "failed"], [c["class"] for c in checks])

    def test_infers_the_entry_type_when_typename_is_absent(self):
        checks = MODULE.normalize_rollup(
            [
                {"context": "legacy", "state": "SUCCESS"},
                {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ]
        )
        self.assertEqual(["status:legacy", "check:build"], [c["key"] for c in checks])

    def test_rejects_an_entry_with_no_recognizable_shape(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.normalize_rollup([{"nothing": True}])

    def test_rejects_a_rollup_that_is_not_a_list(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.normalize_rollup({"nodes": []})

    def test_rejects_a_named_check_with_an_empty_name(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.normalize_rollup([{"__typename": "CheckRun", "name": "  "}])


class CheckTrackingTest(unittest.TestCase):
    def test_stamps_the_first_sighting_of_each_check(self):
        tracking = MODULE.update_check_tracking(
            None, [check("check:a", klass="not_started")], NOW
        )
        self.assertEqual(stamp(), tracking["check:a"]["first_seen_at"])
        self.assertEqual(stamp(), tracking["check:a"]["not_started_since"])

    def test_keeps_the_not_started_clock_while_a_check_stays_queued(self):
        earlier = {
            "check:a": {
                "first_seen_at": stamp(30),
                "last_class": "not_started",
                "last_seen_at": stamp(30),
                "not_started_since": stamp(30),
            }
        }
        tracking = MODULE.update_check_tracking(
            earlier, [check("check:a", klass="not_started")], NOW
        )
        self.assertEqual(stamp(30), tracking["check:a"]["not_started_since"])
        self.assertEqual(1800.0, MODULE.not_started_seconds(tracking, "check:a", NOW))

    def test_a_requeued_check_gets_a_fresh_clock(self):
        earlier = {
            "check:a": {
                "first_seen_at": stamp(30),
                "last_class": "running",
                "last_seen_at": stamp(5),
                "not_started_since": stamp(30),
            }
        }
        tracking = MODULE.update_check_tracking(
            earlier, [check("check:a", klass="not_started")], NOW
        )
        self.assertEqual(stamp(), tracking["check:a"]["not_started_since"])
        self.assertEqual(0.0, MODULE.not_started_seconds(tracking, "check:a", NOW))

    def test_an_executing_workflow_sibling_refreshes_the_queue_clock(self):
        earlier = {
            "check:a": {
                "first_seen_at": stamp(30),
                "last_class": "not_started",
                "last_seen_at": stamp(30),
                "not_started_since": stamp(30),
            }
        }
        tracking = MODULE.update_check_tracking(
            earlier,
            [
                check("check:a", klass="not_started", workflow_run_id=123),
                check("check:b", klass="running", workflow_run_id=123),
            ],
            NOW,
        )
        self.assertEqual(stamp(), tracking["check:a"]["not_started_since"])
        self.assertEqual(0.0, MODULE.not_started_seconds(tracking, "check:a", NOW))

    def test_an_executing_different_workflow_does_not_refresh_the_queue_clock(self):
        earlier = {
            "check:a": {
                "first_seen_at": stamp(30),
                "last_class": "not_started",
                "last_seen_at": stamp(30),
                "not_started_since": stamp(30),
            }
        }
        tracking = MODULE.update_check_tracking(
            earlier,
            [
                check("check:a", klass="not_started", workflow_run_id=123),
                check("check:b", klass="running", workflow_run_id=456),
            ],
            NOW,
        )
        self.assertEqual(stamp(30), tracking["check:a"]["not_started_since"])

    def test_a_check_that_left_the_queue_carries_no_clock(self):
        tracking = MODULE.update_check_tracking(
            None, [check("check:a", klass="running")], NOW
        )
        self.assertNotIn("not_started_since", tracking["check:a"])
        self.assertEqual(0.0, MODULE.not_started_seconds(tracking, "check:a", NOW))

    def test_forgets_a_check_that_left_the_rollup(self):
        earlier = {"check:gone": {"first_seen_at": stamp(30)}}
        tracking = MODULE.update_check_tracking(earlier, [check("check:a")], NOW)
        self.assertEqual(["check:a"], list(tracking))


class DecideTest(unittest.TestCase):
    def decide(self, checks, **overrides):
        arguments = {"now": NOW, "tracking": {}, "deadline_expired": False}
        arguments.update(overrides)
        return MODULE.decide(checks, **arguments)

    def test_all_passing_checks_are_green(self):
        decision = self.decide(
            [check("check:a", klass="passed"), check("check:b", klass="neutral")]
        )
        self.assertEqual("green", decision["decision"])
        self.assertEqual("all_checks_passed", decision["reason"])

    def test_a_failure_reports_failures(self):
        decision = self.decide(
            [check("check:a", klass="failed"), check("check:b", klass="passed")]
        )
        self.assertEqual("failures", decision["decision"])
        self.assertEqual(["check:a"], decision["checks"])

    def test_an_empty_rollup_is_never_green(self):
        decision = self.decide([])
        self.assertEqual("no_checks", decision["decision"])
        self.assertEqual("no_applicable_checks", decision["reason"])

    def test_running_checks_wait(self):
        decision = self.decide([check("check:a", klass="running")])
        self.assertEqual("waiting", decision["decision"])

    def test_a_running_check_remains_nonterminal_when_the_poll_slice_ends(self):
        decision = self.decide(
            [check("check:a", klass="running")], deadline_expired=True
        )
        self.assertEqual("waiting", decision["decision"])
        self.assertEqual("still_running", decision["reason"])

    def test_a_concrete_failure_precedes_other_check_states(self):
        decision = self.decide(
            [
                check("check:a", klass="approval_blocked"),
                check("check:b", klass="running"),
                check("check:c", klass="failed"),
                check("check:d", klass="unknown"),
            ]
        )
        self.assertEqual("failures", decision["decision"])
        self.assertEqual(["check:c"], decision["checks"])
        self.assertEqual(["check:b"], decision["pending_checks"])

    def test_an_empty_rollup_with_blocked_runs_escalates_for_approval(self):
        decision = self.decide(
            [], approval_runs=[{"id": 1, "name": "CI"}]
        )
        self.assertEqual("escalate", decision["decision"])
        self.assertEqual("approval_required", decision["reason"])
        self.assertIn("CI", decision["detail"])

    def test_an_unknown_state_escalates_rather_than_waiting(self):
        decision = self.decide(
            [check("check:a", klass="unknown"), check("check:b", klass="running")]
        )
        self.assertEqual("escalate", decision["decision"])
        self.assertEqual("unknown_check_state", decision["reason"])

    def test_a_stale_check_escalates_rather_than_waiting(self):
        decision = self.decide(
            [check("check:a", klass="stale"), check("check:b", klass="running")]
        )
        self.assertEqual("escalate", decision["decision"])
        self.assertEqual("stale_checks", decision["reason"])

    def test_a_queued_check_waits_inside_the_grace_period(self):
        tracking = {"check:a": {"not_started_since": stamp(5)}}
        decision = self.decide(
            [check("check:a", klass="not_started")], tracking=tracking
        )
        self.assertEqual("waiting", decision["decision"])

    def test_a_check_that_never_starts_escalates(self):
        tracking = {"check:a": {"not_started_since": stamp(30)}}
        decision = self.decide(
            [check("check:a", klass="not_started")], tracking=tracking
        )
        self.assertEqual("escalate", decision["decision"])
        self.assertEqual("checks_never_started", decision["reason"])
        self.assertEqual(["check:a"], decision["checks"])

    def test_a_failure_precedes_a_never_started_check(self):
        tracking = {"check:a": {"not_started_since": stamp(30)}}
        decision = self.decide(
            [check("check:a", klass="not_started"), check("check:b", klass="failed")],
            tracking=tracking,
        )
        self.assertEqual("failures", decision["decision"])
        self.assertEqual(["check:b"], decision["checks"])
        self.assertEqual(["check:a"], decision["pending_checks"])

    def test_a_running_check_is_preserved_beside_the_failure_decision(self):
        decision = self.decide(
            [check("check:a", klass="running"), check("check:b", klass="failed")]
        )
        self.assertEqual("failures", decision["decision"])
        self.assertEqual(["check:b"], decision["checks"])
        self.assertEqual(["check:a"], decision["pending_checks"])

    def test_an_aggregate_failure_is_not_an_independent_root_cause(self):
        decision = self.decide(
            [
                check("check:a", name="build"),
                check(
                    "status:required-status-check",
                    name="required-status-check",
                    kind="status",
                ),
                check("check:b", klass="running"),
            ]
        )
        self.assertEqual(["check:a"], decision["checks"])
        self.assertEqual(
            ["status:required-status-check"], decision["aggregate_checks"]
        )
        self.assertEqual(["check:b"], decision["pending_checks"])

    def test_the_grace_period_is_configurable(self):
        tracking = {"check:a": {"not_started_since": stamp(5)}}
        decision = self.decide(
            [check("check:a", klass="not_started")],
            tracking=tracking,
            not_started_grace=60,
        )
        self.assertEqual("checks_never_started", decision["reason"])


class BaselineAttributionTest(unittest.TestCase):
    def test_a_base_failure_reads_as_pre_existing(self):
        for conclusion in MODULE.FAILED_BASELINE_CONCLUSIONS:
            self.assertEqual("pre_existing", MODULE.baseline_verdict(conclusion))

    def test_a_base_success_reads_as_caused_by_the_pull_request(self):
        self.assertEqual("pr_caused", MODULE.baseline_verdict("SUCCESS"))

    def test_anything_else_reads_as_unknown(self):
        self.assertEqual("unknown", MODULE.baseline_verdict("QUEUED"))
        self.assertEqual("unknown", MODULE.baseline_verdict(None))
        self.assertEqual("unknown", MODULE.baseline_verdict(""))

    def test_a_base_failure_leaves_only_the_pre_existing_verdict_open(self):
        self.assertEqual(("pre_existing",), MODULE.allowed_verdicts("pre_existing"))

    def test_a_base_success_rules_out_calling_the_failure_pre_existing(self):
        self.assertEqual(("pr_caused", "flake"), MODULE.allowed_verdicts("pr_caused"))

    def test_no_base_evidence_leaves_every_verdict_open(self):
        self.assertEqual(MODULE.VERDICTS, MODULE.allowed_verdicts("unknown"))

    def test_attributes_only_the_failing_checks(self):
        attributions = MODULE.attribute_failures(
            [check("check:a", klass="failed"), check("check:b", klass="passed")],
            {"a": "FAILURE"},
        )
        self.assertEqual(["check:a"], list(attributions))
        self.assertEqual("pre_existing", attributions["check:a"]["verdict"])
        self.assertEqual("baseline", attributions["check:a"]["source"])

    def test_a_failure_the_base_never_ran_is_left_unattributed(self):
        attributions = MODULE.attribute_failures([check("check:a")], {})
        self.assertEqual("unknown", attributions["check:a"]["verdict"])
        self.assertEqual("unattributed", attributions["check:a"]["source"])

    def test_keeps_a_model_verdict_the_base_evidence_still_allows(self):
        previous = {
            "check:a": {
                "verdict": "flake",
                "source": "model",
                "rationale": "the runner vanished",
            }
        }
        attributions = MODULE.attribute_failures(
            [check("check:a")], {"a": "SUCCESS"}, previous
        )
        self.assertEqual("flake", attributions["check:a"]["verdict"])
        self.assertEqual("model", attributions["check:a"]["source"])
        self.assertEqual("the runner vanished", attributions["check:a"]["rationale"])

    def test_drops_a_model_verdict_the_base_evidence_now_contradicts(self):
        previous = {
            "check:a": {"verdict": "pr_caused", "source": "model", "rationale": "guess"}
        }
        attributions = MODULE.attribute_failures(
            [check("check:a")], {"a": "FAILURE"}, previous
        )
        self.assertEqual("pre_existing", attributions["check:a"]["verdict"])
        self.assertEqual("baseline", attributions["check:a"]["source"])

    def test_ignores_a_stored_baseline_verdict_that_was_never_a_model_choice(self):
        previous = {"check:a": {"verdict": "pr_caused", "source": "baseline"}}
        attributions = MODULE.attribute_failures(
            [check("check:a")], {"a": "FAILURE"}, previous
        )
        self.assertEqual("pre_existing", attributions["check:a"]["verdict"])


class NextActionTest(unittest.TestCase):
    def action(self, checks, attributions, **overrides):
        state = {
            "reruns": overrides.get("reruns", {}),
            "run": {"attributions": attributions, "batches": overrides.get("batches", [])},
        }
        decision = {
            "decision": "failures",
            "reason": "checks_failed",
            "checks": checks,
            "detail": "",
        }
        return MODULE.next_action(state, decision)

    def test_passes_a_non_failure_decision_straight_through(self):
        decision = {
            "decision": "green",
            "reason": "all_checks_passed",
            "checks": [],
            "detail": "fine",
        }
        action = MODULE.next_action({"run": {}}, decision)
        self.assertEqual("green", action["action"])
        self.assertEqual("fine", action["detail"])

    def test_asks_for_a_verdict_before_touching_anything(self):
        action = self.action(
            ["check:a"], {"check:a": attribution("check:a", "unknown")}
        )
        self.assertEqual("attribute", action["action"])
        self.assertEqual(["check:a"], action["checks"])

    def test_reruns_a_flake_that_has_not_been_rerun(self):
        action = self.action(
            ["check:a"], {"check:a": attribution("check:a", "flake")}
        )
        self.assertEqual("rerun", action["action"])
        self.assertEqual("suspected_flake", action["reason"])

    def test_escalates_a_flake_that_failed_after_its_one_rerun(self):
        action = self.action(
            ["check:a"],
            {"check:a": attribution("check:a", "flake")},
            reruns={"check:a": {"count": 1}},
        )
        self.assertEqual("escalate", action["action"])
        self.assertEqual("flake_failed_twice", action["reason"])

    def test_fixes_a_failure_the_pull_request_caused(self):
        action = self.action(
            ["check:a"], {"check:a": attribution("check:a", "pr_caused")}
        )
        self.assertEqual("fix", action["action"])

    def test_never_fixes_a_failure_the_base_branch_already_has(self):
        action = self.action(
            ["check:a"], {"check:a": attribution("check:a", "pre_existing")}
        )
        self.assertEqual("escalate", action["action"])
        self.assertEqual("pre_existing_failures", action["reason"])

    def test_a_fixable_failure_comes_before_a_pre_existing_escalation(self):
        action = self.action(
            ["check:a", "check:b"],
            {
                "check:a": attribution("check:a", "pre_existing"),
                "check:b": attribution("check:b", "pr_caused"),
            },
        )
        self.assertEqual("fix", action["action"])
        self.assertEqual(["check:b"], action["checks"])

    def test_a_fixable_failure_comes_before_unattributed_diagnostics(self):
        action = self.action(
            ["check:a", "check:b"],
            {
                "check:a": attribution("check:a", "unknown"),
                "check:b": attribution("check:b", "pr_caused"),
            },
        )
        self.assertEqual("fix", action["action"])
        self.assertEqual(["check:b"], action["checks"])

    def test_pre_existing_failure_waits_for_pending_checks(self):
        state = {
            "reruns": {},
            "run": {
                "attributions": {
                    "check:a": attribution("check:a", "pre_existing")
                },
                "batches": [],
            },
        }
        decision = {
            "decision": "failures",
            "reason": "checks_failed",
            "checks": ["check:a"],
            "pending_checks": ["check:b"],
            "detail": "",
        }
        action = MODULE.next_action(state, decision)
        self.assertEqual("waiting", action["action"])
        self.assertEqual(["check:b"], action["checks"])

    def test_pre_existing_failure_does_not_hide_an_overdue_check(self):
        state = {
            "reruns": {},
            "run": {
                "attributions": {
                    "check:a": attribution("check:a", "pre_existing")
                },
                "batches": [],
            },
        }
        decision = {
            "decision": "failures",
            "reason": "checks_failed",
            "checks": ["check:a"],
            "pending_checks": ["check:b"],
            "overdue_checks": ["check:b"],
            "detail": "",
        }
        action = MODULE.next_action(state, decision)
        self.assertEqual("escalate", action["action"])
        self.assertEqual("checks_never_started", action["reason"])

    def test_escalates_a_failure_that_survived_its_recorded_fix(self):
        action = self.action(
            ["check:a"],
            {"check:a": attribution("check:a", "pr_caused")},
            batches=[{"id": "b1", "status": "recorded", "check_keys": ["check:a"]}],
        )
        self.assertEqual("escalate", action["action"])
        self.assertEqual("unfixable_failure", action["reason"])

    def test_attribution_follows_an_already_attributed_fix(self):
        action = self.action(
            ["check:a", "check:b"],
            {
                "check:a": attribution("check:a", "unknown"),
                "check:b": attribution("check:b", "pr_caused"),
            },
        )
        self.assertEqual("fix", action["action"])
        self.assertEqual(["check:b"], action["checks"])


class RunReferenceTest(unittest.TestCase):
    def test_reads_a_run_and_job_from_an_actions_url(self):
        reference = MODULE.parse_run_reference(
            "https://github.com/o/r/actions/runs/1234/job/5678"
        )
        self.assertEqual({"run_id": 1234, "job_id": 5678}, reference)

    def test_reads_a_run_from_a_url_without_a_job(self):
        reference = MODULE.parse_run_reference(
            "https://github.com/o/r/actions/runs/1234"
        )
        self.assertEqual({"run_id": 1234}, reference)

    def test_reads_a_legacy_job_url(self):
        reference = MODULE.parse_run_reference("https://github.com/o/r/runs/99")
        self.assertEqual({"job_id": 99}, reference)

    def test_legacy_job_lookup_requires_check_run_workflow_provenance(self):
        url = "https://github.com/o/r/runs/99"
        self.assertEqual(
            {"job_id": 99},
            MODULE.check_run_reference(
                {"kind": "check_run", "workflow": "CI", "url": url}
            ),
        )
        for check in (
            {"workflow": "CI", "url": url},
            {"kind": "check_run", "workflow": None, "url": url},
            {"kind": "check_run", "workflow": "", "url": url},
            {"kind": "status", "workflow": "CI", "url": url},
        ):
            with self.subTest(check=check):
                self.assertIsNone(MODULE.check_run_reference(check))

    def test_an_external_url_has_no_run(self):
        self.assertIsNone(MODULE.parse_run_reference("https://ci.example.com/build/1"))
        self.assertIsNone(MODULE.parse_run_reference(None))
        self.assertIsNone(MODULE.parse_run_reference(""))

    def test_resolves_a_run_from_a_job_identifier(self):
        pr = {"upstream_owner": "o", "upstream_repo": "r"}
        with mock.patch.object(MODULE, "gh_json", return_value={"run_id": 7}) as api:
            self.assertEqual(7, MODULE.resolve_run_id(pr, {"job_id": 99}))
        self.assertIn("actions/jobs/99", api.call_args[0][0][1])

    def test_a_run_identifier_needs_no_lookup(self):
        with mock.patch.object(MODULE, "gh_json") as api:
            self.assertEqual(3, MODULE.resolve_run_id({}, {"run_id": 3, "job_id": 9}))
        api.assert_not_called()


class ApprovalRunTest(unittest.TestCase):
    def test_finds_runs_waiting_on_an_approval(self):
        blocked = MODULE.approval_blocked_runs(
            {
                "workflow_runs": [
                    {"id": 1, "name": "CI", "status": "waiting"},
                    {"id": 2, "name": "Lint", "status": "completed",
                     "conclusion": "action_required"},
                    {"id": 3, "name": "Done", "status": "completed",
                     "conclusion": "success"},
                ]
            }
        )
        self.assertEqual([1, 2], [entry["id"] for entry in blocked])

    def test_an_unexpected_payload_finds_nothing(self):
        self.assertEqual([], MODULE.approval_blocked_runs(None))
        self.assertEqual([], MODULE.approval_blocked_runs({"workflow_runs": None}))


class StateFileTest(unittest.TestCase):
    def test_round_trips_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_state(Path(directory))
            state = MODULE.load_state(path)
            state["marker"] = True
            MODULE.save_state(path, state)
            self.assertTrue(MODULE.load_state(path)["marker"])
            self.assertIn("updated_at", MODULE.load_state(path))

    def test_rejects_an_unsupported_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"version": 99}), encoding="utf-8")
            with self.assertRaises(MODULE.WorkflowError):
                MODULE.load_state(path)

    def test_rejects_a_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(MODULE.WorkflowError):
                MODULE.load_state(Path(directory) / "nope.json")

    def test_refuses_to_work_on_a_published_iteration(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.active_run({"run": {"status": "published"}})

    def test_refuses_to_work_without_an_iteration(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.active_run({})


class ArchiveRunTest(unittest.TestCase):
    def test_archives_settled_batches_and_verdicts(self):
        state = {
            "history": [],
            "run": {
                "iteration": 1,
                "head_sha": "head1",
                "batches": [
                    {"id": "b1", "status": "recorded", "label": "fix",
                     "check_keys": ["check:a"], "check_names": ["a"],
                     "commit": "c1", "summary": "done"},
                    {"id": "b2", "status": "planned", "check_keys": ["check:b"]},
                ],
                "attributions": {
                    "check:c": attribution("check:c", "pre_existing"),
                    "check:d": attribution("check:d", "unknown"),
                },
            },
        }
        MODULE.archive_run(state)
        identifiers = [entry["id"] for entry in state["history"]]
        self.assertIn("1:b1", identifiers)
        self.assertNotIn("1:b2", identifiers)
        self.assertIn("1:verdict:check:c", identifiers)
        self.assertNotIn("1:verdict:check:d", identifiers)
        self.assertEqual(
            "addressed",
            next(e for e in state["history"] if e["id"] == "1:b1")["outcome"],
        )

    def test_archiving_twice_records_nothing_twice(self):
        state = {
            "history": [],
            "run": {
                "iteration": 1,
                "head_sha": "head1",
                "batches": [
                    {"id": "b1", "status": "recorded", "check_keys": [], "commit": None,
                     "rationale": "no code change"}
                ],
                "attributions": {},
            },
        }
        MODULE.archive_run(state)
        MODULE.archive_run(state)
        self.assertEqual(1, len(state["history"]))
        self.assertEqual("recorded", state["history"][0]["outcome"])


class SummaryHelperTest(unittest.TestCase):
    def test_counts_checks_by_class(self):
        counts = MODULE.class_counts(
            [check("check:a", klass="failed"), check("check:b", klass="passed")]
        )
        self.assertEqual(1, counts["failed"])
        self.assertEqual(1, counts["passed"])
        self.assertEqual(0, counts["unknown"])

    def test_counts_batches_by_status(self):
        self.assertEqual(
            {"planned": 1, "recorded": 2},
            MODULE.count_by_status(
                [{"status": "planned"}, {"status": "recorded"}, {"status": "recorded"}]
            ),
        )

    def test_describes_checks_by_their_human_name(self):
        checks = [check("check:CI/build", name="build")]
        self.assertEqual("build", MODULE.describe_checks(checks, ["check:CI/build"]))
        self.assertEqual("", MODULE.describe_checks(checks, ["check:missing"]))

    def test_counts_recorded_batches_as_handled(self):
        state = {
            "run": {
                "batches": [
                    {"status": "recorded", "check_keys": ["check:a"]},
                    {"status": "planned", "check_keys": ["check:b"]},
                ]
            }
        }
        self.assertEqual({"check:a"}, MODULE.handled_checks(state))

    def test_counts_reruns_per_check(self):
        self.assertEqual(0, MODULE.rerun_count({}, "check:a"))
        self.assertEqual(
            2, MODULE.rerun_count({"reruns": {"check:a": {"count": 2}}}, "check:a")
        )


class AttributeCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_managed_history_rejects_test_suppression_before_live_checks(self):
        source = SCRIPT.read_text(encoding="utf-8")
        coordinator = source.index("def command_agent_task")
        history = source.index("validate_generated_history(", coordinator)
        suppression = source.index("refuse_test_suppression(", history)
        live = source.index("live = metadata_for(target)", suppression)
        self.assertLess(history, suppression)
        self.assertLess(suppression, live)

    def state_with(self, baseline, conclusion=None):
        return write_state(
            self.root,
            run={
                "checks": [check("check:a", name="build")],
                "attributions": {
                    "check:a": attribution(
                        "check:a",
                        baseline,
                        baseline=baseline,
                        conclusion=conclusion,
                    )
                },
            },
        )

    def test_records_a_model_verdict_with_its_rationale(self):
        path = self.state_with("unknown")
        payload = call(
            "attribute",
            "--state",
            str(path),
            "--check",
            "check:a",
            "--verdict",
            "pr_caused",
            "--rationale",
            "the error names app.py, which this PR changed",
        )
        self.assertEqual("attributed", payload["result"])
        entry = MODULE.load_state(path)["run"]["attributions"]["check:a"]
        self.assertEqual("pr_caused", entry["verdict"])
        self.assertEqual("model", entry["source"])
        self.assertIn("app.py", entry["rationale"])

    def test_refuses_to_blame_the_pull_request_for_a_base_failure(self):
        path = self.state_with("pre_existing", "FAILURE")
        with self.assertRaises(MODULE.WorkflowError) as error:
            call(
                "attribute",
                "--state",
                str(path),
                "--check",
                "check:a",
                "--verdict",
                "pr_caused",
                "--rationale",
                "looks related",
            )
        self.assertIn("does not allow the verdict", str(error.exception))
        self.assertEqual(
            "pre_existing",
            MODULE.load_state(path)["run"]["attributions"]["check:a"]["verdict"],
        )

    def test_refuses_to_call_a_check_pre_existing_when_the_base_passed(self):
        path = self.state_with("pr_caused", "SUCCESS")
        with self.assertRaises(MODULE.WorkflowError):
            call(
                "attribute",
                "--state",
                str(path),
                "--check",
                "check:a",
                "--verdict",
                "pre_existing",
                "--rationale",
                "not my fault",
            )

    def test_allows_calling_a_check_a_flake_when_the_base_passed(self):
        path = self.state_with("pr_caused", "SUCCESS")
        payload = call(
            "attribute",
            "--state",
            str(path),
            "--check",
            "check:a",
            "--verdict",
            "flake",
            "--rationale",
            "the runner lost the network",
        )
        self.assertEqual("flake", payload["verdict"])

    def test_rejects_an_unknown_check(self):
        path = self.state_with("unknown")
        with self.assertRaises(MODULE.WorkflowError):
            call(
                "attribute",
                "--state",
                str(path),
                "--check",
                "check:missing",
                "--verdict",
                "flake",
                "--rationale",
                "x",
            )

    def test_reads_a_rationale_from_a_file(self):
        path = self.state_with("unknown")
        rationale = self.root / "rationale.txt"
        rationale.write_text("multi\nline (with parens)\n", encoding="utf-8")
        payload = call(
            "attribute",
            "--state",
            str(path),
            "--check",
            "check:a",
            "--verdict",
            "pre_existing",
            "--rationale-file",
            str(rationale),
        )
        self.assertIn("(with parens)", payload["rationale"])

    def test_rejects_an_empty_rationale(self):
        path = self.state_with("unknown")
        with self.assertRaises(MODULE.WorkflowError):
            call(
                "attribute",
                "--state",
                str(path),
                "--check",
                "check:a",
                "--verdict",
                "flake",
                "--rationale",
                "   ",
            )


class RerunCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patcher = mock.patch.object(MODULE, "require_tools")
        patcher.start()
        self.addCleanup(patcher.stop)

    def state_with(self, verdict="flake", url=None, reruns=None):
        return write_state(
            self.root,
            reruns=reruns or {},
            run={
                "checks": [
                    check(
                        "check:a",
                        name="build",
                        url=url or "https://github.com/o/r/actions/runs/5/job/6",
                    )
                ],
                "attributions": {"check:a": attribution("check:a", verdict)},
            },
        )

    def test_requests_one_rerun_and_records_it(self):
        path = self.state_with()
        with mock.patch.object(MODULE, "rerun_failed_jobs") as request:
            payload = call("rerun", "--state", str(path), "--check", "check:a")
        request.assert_called_once()
        self.assertEqual("rerun_requested", payload["result"])
        self.assertEqual(5, payload["run_id"])
        self.assertEqual(1, payload["reruns"])
        self.assertEqual(1, MODULE.load_state(path)["reruns"]["check:a"]["count"])

    def test_permission_denial_uses_the_empty_commit_fallback(self):
        path = self.state_with()

        def fallback(*_arguments, **_options):
            MODULE.emit({"result": "empty_commit_published"})

        with (
            mock.patch.object(
                MODULE,
                "rerun_failed_jobs",
                side_effect=MODULE.RerunPermissionDenied(
                    "must have write access to the repository"
                ),
            ),
            mock.patch.object(
                MODULE, "publish_empty_rerun_commit", side_effect=fallback
            ) as publish,
        ):
            payload = call("rerun", "--state", str(path), "--check", "check:a")

        self.assertEqual("empty_commit_published", payload["result"])
        publish.assert_called_once()

    def test_transient_rerun_failure_stays_retryable_without_an_empty_commit(self):
        path = self.state_with()
        with (
            mock.patch.object(
                MODULE,
                "rerun_failed_jobs",
                side_effect=MODULE.WorkflowError("GitHub timed out"),
            ),
            mock.patch.object(MODULE, "publish_empty_rerun_commit") as publish,
        ):
            with self.assertRaises(MODULE.WorkflowError):
                call("rerun", "--state", str(path), "--check", "check:a")

        publish.assert_not_called()
        self.assertEqual({}, MODULE.load_state(path)["reruns"])

    def test_refuses_a_second_rerun_of_the_same_check(self):
        path = self.state_with(reruns={"check:a": {"count": 1}})
        with mock.patch.object(MODULE, "rerun_failed_jobs") as request:
            with self.assertRaises(MODULE.WorkflowError) as error:
                call("rerun", "--state", str(path), "--check", "check:a")
        request.assert_not_called()
        self.assertIn("flake_failed_twice", str(error.exception))

    def test_refuses_to_rerun_a_check_that_is_not_a_flake(self):
        path = self.state_with(verdict="pr_caused")
        with mock.patch.object(MODULE, "rerun_failed_jobs") as request:
            with self.assertRaises(MODULE.WorkflowError):
                call("rerun", "--state", str(path), "--check", "check:a")
        request.assert_not_called()

    def test_escalates_a_check_with_no_actions_run_behind_it(self):
        path = self.state_with(url="https://ci.example.com/build/1")
        with mock.patch.object(MODULE, "rerun_failed_jobs") as request:
            payload = call("rerun", "--state", str(path), "--check", "check:a")
        request.assert_not_called()
        self.assertEqual("no_rerun_support", payload["result"])
        escalation = MODULE.load_state(path)["escalation"]
        self.assertEqual("no_rerun_support", escalation["reason"])
        self.assertTrue(escalation["next_action"])

    def test_rejects_a_check_outside_this_iteration(self):
        path = write_state(
            self.root,
            run={"attributions": {"check:a": attribution("check:a", "flake")}},
        )
        with self.assertRaises(MODULE.WorkflowError):
            call("rerun", "--state", str(path), "--check", "check:a")

    def test_stamps_the_watermark_before_it_asks_github_to_run_again(self):
        path = self.state_with()
        observed = {}

        def request(pr, run_id):
            observed["reruns"] = dict(MODULE.load_state(path).get("reruns") or {})
            observed["at"] = MODULE.utc_now()

        with mock.patch.object(MODULE, "rerun_failed_jobs", request):
            call("rerun", "--state", str(path), "--check", "check:a")

        # The stored watermark must predate the request, so a run that starts
        # and finishes immediately still counts as newer than the request.
        self.assertEqual({}, observed["reruns"])
        requested_at = MODULE.load_state(path)["reruns"]["check:a"]["requested_at"]
        self.assertLessEqual(
            MODULE.parse_timestamp(requested_at), MODULE.parse_timestamp(observed["at"])
        )

    def test_records_the_head_the_rerun_belongs_to(self):
        path = self.state_with()
        with mock.patch.object(MODULE, "rerun_failed_jobs"):
            call("rerun", "--state", str(path), "--check", "check:a")
        self.assertEqual("head1", MODULE.load_state(path)["reruns"]["check:a"]["head_sha"])

    def test_only_explicit_permission_errors_are_permission_denials(self):
        permission = SimpleNamespace(
            returncode=1,
            stderr="HTTP 403: must have write access to the repository",
            stdout="",
        )
        transient = SimpleNamespace(
            returncode=1,
            stderr="HTTP 503: service unavailable",
            stdout="",
        )
        with mock.patch.object(MODULE, "run", return_value=permission):
            with self.assertRaises(MODULE.RerunPermissionDenied):
                MODULE.rerun_failed_jobs(
                    {"upstream_owner": "owner", "upstream_repo": "repo"}, 5
                )
        with mock.patch.object(MODULE, "run", return_value=transient):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.rerun_failed_jobs(
                    {"upstream_owner": "owner", "upstream_repo": "repo"}, 5
                )
        self.assertNotIsInstance(error.exception, MODULE.RerunPermissionDenied)

    def test_empty_commit_fallback_refuses_a_dirty_worktree(self):
        path = self.state_with()
        state = MODULE.load_state(path)
        with mock.patch.object(
            MODULE, "git", return_value=" M app.py"
        ), self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.publish_empty_rerun_commit(
                path,
                state,
                "check:a",
                state["run"]["attributions"]["check:a"],
                run_id=5,
                permission_detail="must have write access",
            )
        self.assertIn("worktree is not clean", str(error.exception))
        self.assertEqual({}, MODULE.load_state(path)["reruns"])

    def test_empty_commit_fallback_refuses_a_moved_head(self):
        path = self.state_with()
        state = MODULE.load_state(path)

        def fake_git(_root, *arguments):
            if arguments[0] == "status":
                return ""
            if arguments[:2] == ("rev-parse", "HEAD"):
                return "head1"
            if arguments[:2] == ("branch", "--show-current"):
                return ""
            raise AssertionError(arguments)

        with (
            mock.patch.object(MODULE, "git", side_effect=fake_git),
            mock.patch.object(MODULE, "metadata_for", return_value={"head_sha": "head9"}),
            self.assertRaises(MODULE.WorkflowError) as error,
        ):
            MODULE.publish_empty_rerun_commit(
                path,
                state,
                "check:a",
                state["run"]["attributions"]["check:a"],
                run_id=5,
                permission_detail="must have write access",
            )
        self.assertIn("PR head moved", str(error.exception))
        self.assertEqual({}, MODULE.load_state(path)["reruns"])

    def test_empty_commit_fallback_refuses_the_base_branch(self):
        path = self.state_with()
        state = MODULE.load_state(path)
        state["pr"]["head_branch"] = "main"

        def fake_git(_root, *arguments):
            if arguments[0] == "status":
                return ""
            if arguments[:2] == ("rev-parse", "HEAD"):
                return "head1"
            raise AssertionError(arguments)

        with (
            mock.patch.object(MODULE, "git", side_effect=fake_git),
            mock.patch.object(MODULE, "metadata_for", return_value={"head_sha": "head1"}),
            self.assertRaises(MODULE.WorkflowError) as error,
        ):
            MODULE.publish_empty_rerun_commit(
                path,
                state,
                "check:a",
                state["run"]["attributions"]["check:a"],
                run_id=5,
                permission_detail="must have write access",
            )
        self.assertIn("not safely writable", str(error.exception))

    def test_empty_commit_fallback_requires_one_parent_and_an_identical_tree(self):
        cases = (
            ("head2 head1 other", "tree1", "tree1"),
            ("head2 head1", "tree2", "tree1"),
        )
        for parents, commit_tree, pinned_tree in cases:
            with self.subTest(parents=parents, commit_tree=commit_tree):
                trees = iter([commit_tree, pinned_tree])

                def fake_git(_root, *arguments):
                    if arguments[0] == "rev-list":
                        return parents
                    if arguments[0] == "rev-parse":
                        return next(trees)
                    raise AssertionError(arguments)

                with (
                    mock.patch.object(MODULE, "git", side_effect=fake_git),
                    self.assertRaises(MODULE.WorkflowError),
                ):
                    MODULE.require_empty_child(self.root, "head2", "head1")

    def test_empty_commit_fallback_records_an_accepted_pipeline_push(self):
        path = self.state_with()
        state = MODULE.load_state(path)
        state["pipeline_budget"] = {"run": "stack-run", "iteration": 1}
        MODULE.save_state(path, state)
        committed = False
        commands = []

        def fake_git(_root, *arguments):
            if arguments[0] == "status":
                return ""
            if arguments[:2] == ("rev-parse", "HEAD"):
                return "head2" if committed else "head1"
            if arguments[0] == "rev-list":
                return "head2 head1"
            if arguments[0] == "rev-parse" and str(arguments[1]).endswith("^{tree}"):
                return "tree1"
            if arguments[:2] == ("branch", "--show-current"):
                return ""
            raise AssertionError(arguments)

        def fake_run(command, **_options):
            nonlocal committed
            commands.append(command)
            if "commit" in command:
                committed = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        metadata = [{"head_sha": "head1"}, {"head_sha": "head2"}]
        with (
            mock.patch.object(MODULE, "git", side_effect=fake_git),
            mock.patch.object(MODULE, "metadata_for", side_effect=metadata),
            mock.patch.object(MODULE, "find_push_remote", return_value="origin"),
            mock.patch.object(MODULE, "remote_head", return_value="head1"),
            mock.patch.object(MODULE, "wait_for_remote_head", return_value="head2"),
            mock.patch.object(MODULE, "run", side_effect=fake_run),
        ):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                MODULE.publish_empty_rerun_commit(
                    path,
                    state,
                    "check:a",
                    state["run"]["attributions"]["check:a"],
                    run_id=5,
                    permission_detail="must have write access",
                )
            payload = json.loads(stream.getvalue())

        self.assertEqual("empty_commit_published", payload["result"])
        commit_command = next(command for command in commands if "commit" in command)
        self.assertIn("--no-verify", commit_command)
        self.assertTrue(any("push" in command for command in commands))
        saved = MODULE.load_state(path)
        checkpoint = saved["accepted_pushes"][0]
        self.assertEqual("ci_rerun", checkpoint["kind"])
        self.assertEqual("head1", checkpoint["previous_head_sha"])
        self.assertEqual("head2", checkpoint["head_sha"])
        self.assertEqual(["head2"], checkpoint["commits"])
        self.assertEqual("stack-run", checkpoint["pipeline_run"])
        self.assertEqual("published", saved["run"]["status"])

    def test_interrupted_empty_commit_fallback_is_finalized_without_duplication(self):
        path = self.state_with(
            reruns={
                "check:a": {
                    "count": 1,
                    "name": "build",
                    "run_id": 5,
                    "head_sha": "head1",
                    "requested_at": stamp(),
                    "method": "empty_commit",
                    "status": "pushed",
                    "commit_sha": "head2",
                    "permission_detail": "must have write access",
                }
            }
        )
        state = MODULE.load_state(path)

        def fake_git(_root, *arguments):
            if arguments[0] == "status":
                return ""
            if arguments[:2] == ("rev-parse", "HEAD"):
                return "head2"
            if arguments[:2] == ("branch", "--show-current"):
                return ""
            if arguments[0] == "rev-list":
                return "head2 head1"
            if arguments[0] == "rev-parse" and str(arguments[1]).endswith("^{tree}"):
                return "tree1"
            raise AssertionError(arguments)

        with (
            mock.patch.object(MODULE, "git", side_effect=fake_git),
            mock.patch.object(MODULE, "metadata_for", return_value={"head_sha": "head2"}),
            mock.patch.object(MODULE, "find_push_remote", return_value="origin"),
            mock.patch.object(MODULE, "remote_head", return_value="head2"),
            mock.patch.object(MODULE, "wait_for_remote_head", return_value="head2"),
            mock.patch.object(MODULE, "run") as run,
        ):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                MODULE.publish_empty_rerun_commit(
                    path,
                    state,
                    "check:a",
                    state["run"]["attributions"]["check:a"],
                    run_id=5,
                    permission_detail="must have write access",
                )

        run.assert_not_called()
        saved = MODULE.load_state(path)
        self.assertEqual("published", saved["reruns"]["check:a"]["status"])
        self.assertEqual(1, len(saved["accepted_pushes"]))

class AutoRetryCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patcher = mock.patch.object(MODULE, "require_tools")
        patcher.start()
        self.addCleanup(patcher.stop)

    def state_with(self, auto_retries=None, url=None):
        return write_state(
            self.root,
            auto_retries=auto_retries or {},
            run={
                "checks": [
                    check(
                        "check:a",
                        name="Build pull request",
                        url=url or "https://github.com/o/r/actions/runs/5/job/6",
                    )
                ],
            },
        )

    def test_records_and_reports_the_new_workflow_attempt(self):
        path = self.state_with(
            auto_retries={
                "check:a": {
                    "run_id": 5,
                    "head_sha": "head1",
                    "attempt": 1,
                }
            }
        )
        with mock.patch.object(
            MODULE,
            "fetch_workflow_run",
            return_value={
                "id": 5,
                "run_attempt": 2,
                "status": "in_progress",
                "conclusion": None,
            },
        ):
            payload = call(
                "wait-for-auto-retry",
                "--state",
                str(path),
                "--check",
                "check:a",
                "--timeout",
                "0",
            )
        self.assertEqual("retry_started", payload["result"])
        self.assertEqual(2, payload["run_attempt"])
        self.assertEqual(
            2, MODULE.load_state(path)["auto_retries"]["check:a"]["attempt"]
        )

    def test_records_a_retry_that_has_not_started(self):
        path = self.state_with()
        with mock.patch.object(
            MODULE,
            "fetch_workflow_run",
            return_value={
                "id": 5,
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "failure",
            },
        ):
            payload = call(
                "wait-for-auto-retry",
                "--state",
                str(path),
                "--check",
                "check:a",
                "--timeout",
                "0",
            )
        self.assertEqual("retry_not_detected", payload["result"])
        self.assertEqual(
            "not_detected",
            MODULE.load_state(path)["auto_retries"]["check:a"]["status"],
        )

    def test_reports_when_a_failure_has_no_actions_run(self):
        path = self.state_with(url="https://ci.example.com/build/1")
        payload = call(
            "wait-for-auto-retry",
            "--state",
            str(path),
            "--check",
            "check:a",
        )
        self.assertEqual("retry_not_detected", payload["result"])
        self.assertEqual("no_rerun_support", payload["reason"])


class RerunWatermarkTest(unittest.TestCase):
    def entry(self, minutes_ago=5, head_sha="head1"):
        return {
            "count": 1,
            "name": "build",
            "run_id": 5,
            "head_sha": head_sha,
            "requested_at": stamp(minutes_ago),
        }

    def test_holds_back_a_failure_recorded_before_the_rerun_was_asked_for(self):
        stale = check("check:a", completed_at=stamp(10))
        applied = MODULE.apply_rerun_watermark(
            [stale], {"check:a": self.entry()}, "head1"
        )
        self.assertEqual("running", applied[0]["class"])
        self.assertTrue(applied[0]["awaiting_rerun"])

    def test_credits_a_failure_that_landed_after_the_rerun_was_asked_for(self):
        fresh = check("check:a", completed_at=stamp(1))
        applied = MODULE.apply_rerun_watermark(
            [fresh], {"check:a": self.entry()}, "head1"
        )
        self.assertEqual("failed", applied[0]["class"])
        self.assertNotIn("awaiting_rerun", applied[0])

    def test_waits_when_a_failure_carries_no_completion_time(self):
        applied = MODULE.apply_rerun_watermark(
            [check("check:a")], {"check:a": self.entry()}, "head1"
        )
        self.assertEqual("running", applied[0]["class"])

    def test_ignores_a_rerun_recorded_for_a_different_head(self):
        stale = check("check:a", completed_at=stamp(10))
        applied = MODULE.apply_rerun_watermark(
            [stale], {"check:a": self.entry(head_sha="head9")}, "head1"
        )
        self.assertEqual("failed", applied[0]["class"])

    def test_leaves_every_other_check_alone(self):
        checks = [
            check("check:a", klass="passed", completed_at=stamp(10)),
            check("check:b", completed_at=stamp(10)),
        ]
        applied = MODULE.apply_rerun_watermark(
            checks, {"check:a": self.entry()}, "head1"
        )
        self.assertEqual(["passed", "failed"], [item["class"] for item in applied])

    def test_does_nothing_without_a_recorded_rerun(self):
        checks = [check("check:a", completed_at=stamp(10))]
        for reruns in (None, {}, "nonsense"):
            with self.subTest(reruns=reruns):
                self.assertEqual(
                    ["failed"],
                    [
                        item["class"]
                        for item in MODULE.apply_rerun_watermark(
                            checks, reruns, "head1"
                        )
                    ],
                )

    def test_never_reports_a_flake_as_failing_twice_on_the_old_result(self):
        state = {
            "reruns": {"check:a": {"count": 1}},
            "run": {
                "attributions": {"check:a": attribution("check:a", "flake")},
            },
        }
        stale = check("check:a", completed_at=stamp(10))
        applied = MODULE.apply_rerun_watermark(
            [stale], {"check:a": self.entry()}, "head1"
        )
        decision = MODULE.decide(
            applied,
            now=NOW,
            tracking={},
            not_started_grace=MODULE.DEFAULT_NOT_STARTED_GRACE,
            deadline_expired=False,
            approval_runs=[],
        )
        self.assertEqual("waiting", decision["decision"])
        self.assertEqual(
            "waiting", MODULE.next_action(state, decision)["action"]
        )


class PlanCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def state_with(self, verdict):
        return write_state(
            self.root,
            run={
                "checks": [check("check:a", name="build")],
                "attributions": {"check:a": attribution("check:a", verdict)},
            },
        )

    def test_stores_a_batch_for_a_failure_the_pull_request_caused(self):
        path = self.state_with("pr_caused")
        payload = call(
            "plan",
            "--state",
            str(path),
            "--batch",
            "b1",
            "--checks",
            "check:a",
            "--label",
            "fix the import",
            "--paths",
            "app.py",
            "--validation",
            "python -m pytest",
        )
        self.assertEqual("planned", payload["result"])
        self.assertEqual(["a"], payload["batch"]["check_names"])
        self.assertEqual("planned", payload["batch"]["status"])

    def test_refuses_a_pre_existing_failure(self):
        path = self.state_with("pre_existing")
        with self.assertRaises(MODULE.WorkflowError) as error:
            call(
                "plan",
                "--state",
                str(path),
                "--batch",
                "b1",
                "--checks",
                "check:a",
                "--label",
                "fix",
            )
        self.assertIn("pr_caused", str(error.exception))
        self.assertEqual([], MODULE.load_state(path)["run"]["batches"])

    def test_refuses_a_flake(self):
        path = self.state_with("flake")
        with self.assertRaises(MODULE.WorkflowError):
            call(
                "plan", "--state", str(path), "--batch", "b1", "--checks", "check:a",
                "--label", "fix",
            )

    def test_refuses_an_unattributed_failure(self):
        path = self.state_with("unknown")
        with self.assertRaises(MODULE.WorkflowError):
            call(
                "plan", "--state", str(path), "--batch", "b1", "--checks", "check:a",
                "--label", "fix",
            )

    def test_refuses_a_check_outside_this_iteration(self):
        path = self.state_with("pr_caused")
        with self.assertRaises(MODULE.WorkflowError):
            call(
                "plan", "--state", str(path), "--batch", "b1", "--checks", "check:z",
                "--label", "fix",
            )

    def test_replanning_a_batch_replaces_it(self):
        path = self.state_with("pr_caused")
        for label in ("first", "second"):
            call(
                "plan", "--state", str(path), "--batch", "b1", "--checks", "check:a",
                "--label", label,
            )
        batches = MODULE.load_state(path)["run"]["batches"]
        self.assertEqual(1, len(batches))
        self.assertEqual("second", batches[0]["label"])


class RecordAndSkipCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = write_state(
            self.root,
            run={
                "checks": [check("check:a", name="build")],
                "attributions": {"check:a": attribution("check:a", "pr_caused")},
                "batches": [
                    {
                        "id": "b1",
                        "label": "fix",
                        "check_keys": ["check:a"],
                        "check_names": ["build"],
                        "paths": ["app.py"],
                        "validation": None,
                        "status": "planned",
                        "commit": None,
                        "summary": None,
                        "rationale": None,
                    }
                ],
            },
        )

    def test_records_a_commit(self):
        with mock.patch.object(MODULE, "git", return_value="abc123"):
            payload = call(
                "record", "--state", str(self.path), "--batch", "b1",
                "--summary", "fixed the import", "--commit", "HEAD",
            )
        self.assertEqual("abc123", payload["commit"])
        batch = MODULE.load_state(self.path)["run"]["batches"][0]
        self.assertEqual("recorded", batch["status"])

    def test_records_a_no_code_outcome(self):
        payload = call(
            "record", "--state", str(self.path), "--batch", "b1",
            "--summary", "nothing to change", "--rationale", "the fix landed already",
        )
        self.assertIsNone(payload["commit"])
        self.assertEqual("the fix landed already", payload["rationale"])

    def test_requires_a_commit_or_a_rationale(self):
        with self.assertRaises(MODULE.WorkflowError):
            call("record", "--state", str(self.path), "--batch", "b1",
                 "--summary", "nothing")

    def test_rejects_an_unplanned_batch(self):
        with self.assertRaises(MODULE.WorkflowError):
            call("record", "--state", str(self.path), "--batch", "nope",
                 "--summary", "x", "--rationale", "y")

    def test_skipping_a_batch_records_an_escalation(self):
        payload = call(
            "skip", "--state", str(self.path), "--batch", "b1",
            "--rationale", "the failure needs a dependency this loop cannot add",
        )
        self.assertEqual("skipped", payload["result"])
        escalation = MODULE.load_state(self.path)["escalation"]
        self.assertEqual("unfixable_failure", escalation["reason"])
        self.assertEqual(["check:a"], escalation["checks"])

    def test_refuses_a_commit_that_deletes_a_test_file(self):
        """The refusal has to sit on the command, not only in the helper."""
        def fake_git(repo_root, *arguments):
            if arguments[0] == "rev-parse":
                return "abc123"
            if "--name-status" in arguments:
                return "D\ttests/test_widget.py"
            return ""

        with mock.patch.object(MODULE, "git", fake_git):
            with self.assertRaises(MODULE.WorkflowError) as error:
                call(
                    "record", "--state", str(self.path), "--batch", "b1",
                    "--summary", "made the build pass", "--commit", "HEAD",
                )
        self.assertIn("stopping a test from running", str(error.exception))
        batch = MODULE.load_state(self.path)["run"]["batches"][0]
        self.assertEqual("planned", batch["status"])

    def test_refuses_a_commit_that_disables_a_running_test(self):
        def fake_git(repo_root, *arguments):
            if arguments[0] == "rev-parse":
                return "abc123"
            if "--name-status" in arguments:
                return "M\ttests/test_widget.py"
            return "+++ b/tests/test_widget.py\n+@pytest.mark.skip(reason='ci')"

        with mock.patch.object(MODULE, "git", fake_git):
            with self.assertRaises(MODULE.WorkflowError) as error:
                call(
                    "record", "--state", str(self.path), "--batch", "b1",
                    "--summary", "made the build pass", "--commit", "HEAD",
                )
        self.assertIn("@pytest.mark.skip", str(error.exception))


class EscalateCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_records_the_reason_and_the_next_action(self):
        path = write_state(self.root)
        payload = call(
            "escalate", "--state", str(path), "--reason", "pre_existing_failures",
            "--checks", "check:a", "--detail", "build already fails on main",
        )
        self.assertEqual("escalated", payload["result"])
        self.assertEqual(
            MODULE.ESCALATION_ACTIONS["pre_existing_failures"], payload["next_action"]
        )
        self.assertEqual("head1", payload["head_sha"])
        self.assertEqual(
            "pre_existing_failures", MODULE.load_state(path)["escalation"]["reason"]
        )

    def test_rejects_a_reason_outside_the_catalog(self):
        path = write_state(self.root)
        with self.assertRaises(SystemExit):
            call("escalate", "--state", str(path), "--reason", "because",
                 "--detail", "x")

    def test_rejects_an_empty_detail(self):
        path = write_state(self.root)
        with self.assertRaises(MODULE.WorkflowError):
            call("escalate", "--state", str(path), "--reason", "timeout",
                 "--detail", "  ")


class ResolveCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patcher = mock.patch.object(MODULE, "require_tools")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_records_a_green_outcome_at_the_pinned_head(self):
        path = write_state(self.root)
        rollup = ("head1", [check("check:a", klass="passed")])
        with mock.patch.object(MODULE, "fetch_rollup", return_value=rollup):
            payload = call("resolve", "--state", str(path), "--outcome", "green")
        self.assertEqual("green", payload["outcome"])
        self.assertEqual("head1", payload["clean_at_head_sha"])
        self.assertIsNone(payload["skip_note"])
        state = MODULE.load_state(path)
        self.assertEqual("head1", state["clean_at_head_sha"])
        self.assertIsNone(state["escalation"])

    def test_records_a_no_checks_skip_with_a_visible_note(self):
        path = write_state(self.root)
        with mock.patch.object(MODULE, "fetch_rollup", return_value=("head1", [])):
            with mock.patch.object(MODULE, "fetch_workflow_runs", return_value={}):
                payload = call(
                    "resolve", "--state", str(path), "--outcome", "no_checks"
                )
        self.assertEqual("no_checks", payload["outcome"])
        self.assertIn("no applicable checks", payload["skip_note"])
        self.assertIn("owner/repo#7", payload["skip_note"])

    def test_refuses_an_outcome_the_live_checks_contradict(self):
        path = write_state(self.root)
        rollup = ("head1", [check("check:a", klass="failed")])
        with mock.patch.object(MODULE, "fetch_rollup", return_value=rollup):
            with self.assertRaises(MODULE.WorkflowError) as error:
                call("resolve", "--state", str(path), "--outcome", "green")
        self.assertIn("'failures'", str(error.exception))

    def test_refuses_to_call_an_empty_rollup_green(self):
        path = write_state(self.root)
        with mock.patch.object(MODULE, "fetch_rollup", return_value=("head1", [])):
            with mock.patch.object(MODULE, "fetch_workflow_runs", return_value={}):
                with self.assertRaises(MODULE.WorkflowError):
                    call("resolve", "--state", str(path), "--outcome", "green")

    def test_refuses_when_the_head_moved(self):
        path = write_state(self.root)
        rollup = ("head2", [check("check:a", klass="passed")])
        with mock.patch.object(MODULE, "fetch_rollup", return_value=rollup):
            with self.assertRaises(MODULE.WorkflowError) as error:
                call("resolve", "--state", str(path), "--outcome", "green")
        self.assertIn("head changed", str(error.exception))


class ChecksCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patcher = mock.patch.object(MODULE, "require_tools")
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, path, rollup, baseline=None, runs=None, *arguments):
        with mock.patch.object(MODULE, "fetch_rollup", return_value=rollup):
            with mock.patch.object(
                MODULE, "baseline_conclusions", return_value=baseline or {}
            ):
                with mock.patch.object(
                    MODULE, "fetch_workflow_runs", return_value=runs or {}
                ):
                    return call("checks", "--state", str(path), *arguments)

    def test_reports_green(self):
        path = write_state(self.root)
        with mock.patch.object(MODULE, "save_state", wraps=MODULE.save_state) as save:
            payload = self.read(path, ("head1", [check("check:a", klass="passed")]))
        self.assertEqual("green", payload["result"])
        self.assertEqual(1, payload["counts"]["passed"])
        self.assertTrue(Path(payload["checks_path"]).is_file())
        self.assertEqual(1, save.call_count)
        saved = save.call_args.args[1]
        self.assertEqual("green", saved["outcome"])
        self.assertEqual("head1", saved["clean_at_head_sha"])
        self.assertEqual("green", saved["run"]["outcome"])

    def test_reports_a_repository_with_no_checks(self):
        path = write_state(self.root)
        with mock.patch.object(MODULE, "save_state", wraps=MODULE.save_state) as save:
            payload = self.read(path, ("head1", []))
        self.assertEqual("no_checks", payload["result"])
        self.assertEqual("no_applicable_checks", payload["reason"])
        self.assertEqual(1, save.call_count)
        self.assertEqual("no_checks", save.call_args.args[1]["outcome"])
        state = MODULE.load_state(path)
        self.assertEqual("no_checks", state["outcome"])
        self.assertEqual("head1", state["clean_at_head_sha"])
        self.assertIn("no applicable checks", state["skip_note"])

    def test_asks_for_a_verdict_when_the_base_evidence_is_silent(self):
        path = write_state(self.root)
        payload = self.read(path, ("head1", [check("check:a", name="build")]))
        self.assertEqual("attribute", payload["result"])
        self.assertEqual(["check:a"], payload["action_checks"])
        self.assertEqual("build", payload["failing"][0]["name"])

    def test_reports_a_concrete_failure_while_other_checks_are_pending(self):
        path = write_state(self.root)
        payload = self.read(
            path,
            (
                "head1",
                [
                    check("check:a", name="build"),
                    check("check:b", klass="running"),
                    check(
                        "status:required-status-check",
                        name="required-status-check",
                        kind="status",
                    ),
                ],
            ),
        )
        self.assertEqual("attribute", payload["result"])
        self.assertEqual(["check:a"], payload["action_checks"])
        self.assertEqual(["check:b"], payload["pending_checks"])
        self.assertEqual(
            ["status:required-status-check"], payload["aggregate_checks"]
        )
        saved = MODULE.load_state(path)["run"]["decision"]
        self.assertEqual(["check:b"], saved["pending_checks"])

    def test_escalates_without_editing_when_the_base_already_fails(self):
        path = write_state(self.root)
        payload = self.read(
            path, ("head1", [check("check:a", name="build")]), {"build": "FAILURE"}
        )
        self.assertEqual("escalate", payload["result"])
        self.assertEqual("pre_existing_failures", payload["reason"])
        self.assertTrue(payload["next_action"])
        self.assertEqual(
            "pre_existing_failures", MODULE.load_state(path)["escalation"]["reason"]
        )

    def test_asks_for_a_fix_when_the_base_passed(self):
        path = write_state(self.root)
        payload = self.read(
            path, ("head1", [check("check:a", name="build")]), {"build": "SUCCESS"}
        )
        self.assertEqual("fix", payload["result"])
        self.assertEqual("pr_caused", payload["failing"][0]["verdict"])

    def test_escalates_when_the_head_moved_under_the_iteration(self):
        path = write_state(self.root)
        payload = self.read(path, ("head9", [check("check:a", klass="passed")]))
        self.assertEqual("escalate", payload["result"])
        self.assertEqual("head_changed", payload["reason"])

    def test_reports_waiting_without_the_wait_flag(self):
        path = write_state(self.root)
        payload = self.read(path, ("head1", [check("check:a", klass="running")]))
        self.assertEqual("waiting", payload["result"])
        self.assertIsNone(MODULE.load_state(path)["escalation"])

    def test_wait_returns_still_running_after_the_default_five_minute_slice(self):
        self.assertEqual(300, MODULE.DEFAULT_POLL_TIMEOUT)
        path = write_state(self.root)
        rollup = ("head1", [check("check:a", klass="running")])
        with (
            mock.patch.object(MODULE, "fetch_rollup", return_value=rollup),
            mock.patch.object(MODULE, "baseline_conclusions", return_value={}),
            mock.patch.object(MODULE, "time") as clock,
        ):
            clock.monotonic.side_effect = [0.0, 0.0, 300.0]
            clock.sleep.return_value = None
            payload = call("checks", "--state", str(path), "--wait")
        self.assertEqual("waiting", payload["result"])
        self.assertEqual("still_running", payload["reason"])
        self.assertIsNone(MODULE.load_state(path)["escalation"])

    def test_wait_keeps_polling_after_a_pre_existing_failure(self):
        path = write_state(self.root)
        rollup = (
            "head1",
            [
                check("check:a", name="build"),
                check("check:b", klass="running"),
            ],
        )
        with (
            mock.patch.object(MODULE, "fetch_rollup", return_value=rollup) as fetch,
            mock.patch.object(
                MODULE, "baseline_conclusions", return_value={"build": "FAILURE"}
            ),
            mock.patch.object(MODULE, "time") as clock,
        ):
            clock.monotonic.side_effect = [0.0, 0.0, 300.0]
            clock.sleep.return_value = None
            payload = call("checks", "--state", str(path), "--wait")
        self.assertEqual("waiting", payload["result"])
        self.assertEqual(2, fetch.call_count)

    def test_escalates_an_approval_blocked_fork_run(self):
        path = write_state(self.root)
        payload = self.read(
            path,
            ("head1", []),
            None,
            {"workflow_runs": [{"id": 3, "name": "CI", "status": "waiting"}]},
        )
        self.assertEqual("escalate", payload["result"])
        self.assertEqual("approval_required", payload["reason"])

    def test_stores_the_snapshot_and_the_tracking_clock(self):
        path = write_state(self.root)
        self.read(path, ("head1", [check("check:a", klass="not_started")]))
        run_state = MODULE.load_state(path)["run"]
        self.assertEqual(1, len(run_state["checks"]))
        self.assertIn("not_started_since", run_state["tracking"]["check:a"])
        self.assertEqual("waiting", run_state["decision"]["decision"])

    def test_spends_an_iteration_only_on_a_run_with_work_to_do(self):
        for rollup, baseline, expected in (
            ([check("check:a", klass="passed")], None, 0),
            ([], None, 0),
            ([check("check:a", klass="running")], None, 0),
            ([check("check:a", name="build")], {"build": "FAILURE"}, 0),
            ([check("check:a", name="build")], None, 1),
            ([check("check:a", name="build")], {"build": "SUCCESS"}, 1),
        ):
            with self.subTest(expected=expected):
                path = write_state(self.root, iterations=0)
                self.read(path, ("head1", rollup), baseline)
                self.assertEqual(expected, MODULE.load_state(path)["iterations"])

    def test_charges_one_iteration_however_often_it_reads_the_checks(self):
        path = write_state(self.root, iterations=0)
        for _ in range(3):
            self.read(
                path, ("head1", [check("check:a", name="build")]), {"build": "SUCCESS"}
            )
        self.assertEqual(1, MODULE.load_state(path)["iterations"])

    def rerun_state(self, requested_minutes_ago=5):
        return write_state(
            self.root,
            reruns={
                "check:a": {
                    "count": 1,
                    "name": "build",
                    "run_id": 5,
                    "head_sha": "head1",
                    "requested_at": stamp(requested_minutes_ago),
                }
            },
            run={
                "attributions": {
                    "check:a": attribution("check:a", "flake", source="model")
                }
            },
        )

    def test_waits_rather_than_credit_the_failure_its_rerun_replaces(self):
        path = self.rerun_state()
        payload = self.read(
            path,
            ("head1", [check("check:a", name="build", completed_at=stamp(10))]),
        )
        self.assertEqual("waiting", payload["result"])
        self.assertIsNone(MODULE.load_state(path)["escalation"])

    def test_escalates_once_the_rerun_itself_fails(self):
        path = self.rerun_state()
        payload = self.read(
            path,
            ("head1", [check("check:a", name="build", completed_at=stamp(1))]),
        )
        self.assertEqual("escalate", payload["result"])
        self.assertEqual("flake_failed_twice", payload["reason"])


class PublishCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patcher = mock.patch.object(MODULE, "require_tools")
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_git(self, status="", rev_list="", show=""):
        def call_git(repo_root, *arguments):
            if arguments[0] == "status":
                return status
            if arguments[0] == "rev-list":
                return rev_list
            if arguments[0] == "rev-parse":
                return "local1"
            if arguments[0] == "show":
                return show
            raise AssertionError(f"unexpected git call: {arguments}")

        return call_git

    def test_refuses_a_dirty_worktree(self):
        path = write_state(self.root)
        with mock.patch.object(MODULE, "git", self.fake_git(status=" M app.py")):
            with self.assertRaises(MODULE.WorkflowError) as error:
                call("publish", "--state", str(path))
        self.assertIn("worktree is not clean", str(error.exception))

    def test_refuses_a_batch_that_is_still_planned(self):
        path = write_state(
            self.root, run={"batches": [{"id": "b1", "status": "planned"}]}
        )
        with mock.patch.object(MODULE, "git", self.fake_git()):
            with self.assertRaises(MODULE.WorkflowError) as error:
                call("publish", "--state", str(path))
        self.assertIn("neither recorded nor skipped", str(error.exception))

    def test_refuses_to_publish_partial_work_after_a_skip(self):
        path = write_state(
            self.root, run={"batches": [{"id": "b1", "status": "skipped"}]}
        )
        with mock.patch.object(MODULE, "git", self.fake_git()):
            with self.assertRaises(MODULE.WorkflowError) as error:
                call("publish", "--state", str(path))
        self.assertIn("without publishing partial work", str(error.exception))

    def test_refuses_a_local_commit_no_batch_recorded(self):
        path = write_state(
            self.root,
            run={
                "batches": [
                    {"id": "b1", "status": "recorded", "commit": None,
                     "summary": "no code change", "rationale": "none"}
                ]
            },
        )
        with mock.patch.object(MODULE, "git", self.fake_git(rev_list="sneaky1")):
            with self.assertRaises(MODULE.WorkflowError) as error:
                call("publish", "--state", str(path))
        self.assertIn("unrecorded ['sneaky1']", str(error.exception))

    def test_reports_nothing_to_publish_when_no_commit_was_made(self):
        path = write_state(
            self.root,
            run={
                "batches": [
                    {"id": "b1", "status": "recorded", "commit": None,
                     "summary": "no code change", "rationale": "none"}
                ]
            },
        )
        with mock.patch.object(MODULE, "git", self.fake_git()):
            payload = call("publish", "--state", str(path))
        self.assertEqual("nothing_to_publish", payload["result"])

    def test_pushes_and_verifies_the_new_head(self):
        path = write_state(
            self.root,
            run={
                "batches": [
                    {"id": "b1", "status": "recorded", "commit": "local1",
                     "summary": "fixed the import", "rationale": None}
                ]
            },
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(MODULE, "git", self.fake_git(rev_list="local1"))
            )
            stack.enter_context(
                mock.patch.object(MODULE, "find_push_remote", return_value="origin")
            )
            stack.enter_context(
                mock.patch.object(MODULE, "remote_head", side_effect=["head1", "local1"])
            )
            stack.enter_context(
                mock.patch.object(MODULE, "wait_for_remote_head", return_value="local1")
            )
            stack.enter_context(
                mock.patch.object(
                    MODULE, "metadata_for", return_value={"head_sha": "local1"}
                )
            )
            push = stack.enter_context(mock.patch.object(MODULE, "run"))
            payload = call("publish", "--state", str(path))
        push.assert_called_once()
        self.assertEqual("published", payload["result"])
        self.assertEqual(["local1"], payload["commits"])
        state = MODULE.load_state(path)
        self.assertEqual("published", state["run"]["status"])
        self.assertEqual({}, state["reruns"])
        checkpoint = payload["accepted_push"]
        self.assertEqual("head1", checkpoint["previous_head_sha"])
        self.assertEqual("local1", checkpoint["head_sha"])
        self.assertEqual(["local1"], checkpoint["commits"])
        self.assertEqual([checkpoint], state["accepted_pushes"])

    def test_status_exposes_accepted_pushes_to_orchestrators(self):
        checkpoint = {
            "id": "push-1",
            "accepted_at": "2026-01-01T00:00:00Z",
            "previous_head_sha": "head1",
            "head_sha": "head2",
            "commits": ["head2"],
            "pipeline_run": "stack-run",
            "pipeline_iteration": 1,
        }
        path = write_state(self.root, accepted_pushes=[checkpoint])

        payload = call("status", "--state", str(path))

        self.assertEqual([checkpoint], payload["accepted_pushes"])

    def test_status_distinguishes_diagnostics_from_waiting(self):
        path = write_state(
            self.root,
            run={
                "decision": {
                    "decision": "failures",
                    "action": "attribute",
                    "reason": "unattributed_failures",
                    "checks": ["check:a"],
                    "action_checks": ["check:a"],
                    "pending_checks": ["check:b"],
                    "observed_at": "2026-01-01T00:00:00Z",
                }
            },
        )
        payload = call("status", "--state", str(path))
        self.assertEqual("diagnosing", payload["progress"]["phase"])
        self.assertEqual(["check:a"], payload["progress"]["action_checks"])
        self.assertEqual(["check:b"], payload["progress"]["pending_checks"])

    def test_records_the_local_validation_behind_the_push(self):
        """The state has to say what ran, or a live run proves nothing.

        This loop pays for a wrong guess in whole CI cycles, so the record of
        what it ran before spending one is the point of the requirement.
        """
        path = write_state(
            self.root,
            run={
                "batches": [
                    {"id": "b1", "status": "recorded", "commit": "local1",
                     "summary": "fixed the import", "rationale": None}
                ]
            },
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(MODULE, "git", self.fake_git(rev_list="local1"))
            )
            stack.enter_context(
                mock.patch.object(MODULE, "find_push_remote", return_value="origin")
            )
            stack.enter_context(
                mock.patch.object(MODULE, "remote_head", side_effect=["head1", "local1"])
            )
            stack.enter_context(
                mock.patch.object(MODULE, "wait_for_remote_head", return_value="local1")
            )
            stack.enter_context(
                mock.patch.object(
                    MODULE, "metadata_for", return_value={"head_sha": "local1"}
                )
            )
            stack.enter_context(mock.patch.object(MODULE, "run"))
            payload = call(
                "publish",
                "--state",
                str(path),
                "--validated",
                "the failing check",
                "--rewrote",
                "the fixing form",
            )
        state = MODULE.load_state(path)
        self.assertEqual(
            [
                {
                    "head_sha": "local1",
                    "status": "passed",
                    "commands": ["the failing check", "the fixing form"],
                    "rewrote": ["the fixing form"],
                }
            ],
            state["local_validation"],
        )
        self.assertEqual(state["local_validation"][-1], payload["local_validation"])

    def test_publishes_a_fix_that_could_not_be_reproduced_locally(self):
        """A check that only runs in CI must not hold a fix back.

        Refusing to push there would turn an ordinary repository into an
        escalation, which costs more than the failure this record watches for.
        """
        path = write_state(
            self.root,
            run={
                "batches": [
                    {"id": "b1", "status": "recorded", "commit": "local1",
                     "summary": "fixed the import", "rationale": None}
                ]
            },
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(MODULE, "git", self.fake_git(rev_list="local1"))
            )
            stack.enter_context(
                mock.patch.object(MODULE, "find_push_remote", return_value="origin")
            )
            stack.enter_context(
                mock.patch.object(MODULE, "remote_head", side_effect=["head1", "local1"])
            )
            stack.enter_context(
                mock.patch.object(MODULE, "wait_for_remote_head", return_value="local1")
            )
            stack.enter_context(
                mock.patch.object(
                    MODULE, "metadata_for", return_value={"head_sha": "local1"}
                )
            )
            push = stack.enter_context(mock.patch.object(MODULE, "run"))
            payload = call(
                "publish",
                "--state",
                str(path),
                "--not-validated",
                "the check needs a container this workspace has no access to",
            )
        push.assert_called_once()
        self.assertEqual("published", payload["result"])
        state = MODULE.load_state(path)
        self.assertEqual("skipped", state["local_validation"][-1]["status"])
        self.assertEqual(
            "the check needs a container this workspace has no access to",
            state["local_validation"][-1]["reason"],
        )

    def test_refuses_when_the_pull_request_head_does_not_catch_up(self):
        path = write_state(
            self.root,
            run={
                "batches": [
                    {"id": "b1", "status": "recorded", "commit": "local1",
                     "summary": "fixed", "rationale": None}
                ]
            },
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(MODULE, "git", self.fake_git(rev_list="local1"))
            )
            stack.enter_context(
                mock.patch.object(MODULE, "find_push_remote", return_value="origin")
            )
            stack.enter_context(
                mock.patch.object(MODULE, "remote_head", return_value="head1")
            )
            stack.enter_context(
                mock.patch.object(MODULE, "wait_for_remote_head", return_value="local1")
            )
            stack.enter_context(
                mock.patch.object(
                    MODULE, "metadata_for", return_value={"head_sha": "head1"}
                )
            )
            stack.enter_context(mock.patch.object(MODULE, "run"))
            stack.enter_context(mock.patch.object(MODULE.time, "sleep"))
            with self.assertRaises(MODULE.WorkflowError) as error:
                call("publish", "--state", str(path))
        self.assertIn("PR head mismatch", str(error.exception))

    def test_refuses_to_push_a_commit_amended_to_suppress_a_test(self):
        """`record` already passed. An amend after it would reach GitHub unseen.

        This is the last gate before anything leaves the machine, so it reads the
        commits it is about to push rather than trusting what was recorded.
        """
        path = write_state(
            self.root,
            run={
                "batches": [
                    {"id": "b1", "status": "recorded", "commit": "local1",
                     "summary": "fixed the import", "rationale": None}
                ]
            },
        )
        git = self.fake_git(rev_list="local1", show="D\tsrc/test/java/FooTest.java")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(MODULE, "git", git))
            push = stack.enter_context(mock.patch.object(MODULE, "run"))
            with self.assertRaises(MODULE.WorkflowError) as error:
                call("publish", "--state", str(path))
        self.assertIn("stopping a test from running", str(error.exception))
        self.assertIn("FooTest.java", str(error.exception))
        push.assert_not_called()
        self.assertNotEqual("published", MODULE.load_state(path)["run"]["status"])


class StatusCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_reports_a_machine_readable_snapshot(self):
        path = write_state(
            self.root,
            outcome="green",
            clean_at_head_sha="head1",
            run={
                "checks": [check("check:a", klass="passed")],
                "attributions": {"check:a": attribution("check:a", "pr_caused")},
                "decision": {"decision": "green", "action": "green",
                             "reason": "all_checks_passed"},
                "batches": [{"id": "b1", "status": "recorded"}],
            },
        )
        payload = call("status", "--state", str(path))
        self.assertEqual("ready", payload["result"])
        self.assertEqual("green", payload["outcome"])
        self.assertEqual("head1", payload["clean_at_head_sha"])
        self.assertEqual("green", payload["run"]["decision"])
        self.assertEqual({"recorded": 1}, payload["run"]["batch_statuses"])
        self.assertEqual({"check:a": "pr_caused"}, payload["verdicts"])
        self.assertEqual(1, payload["counts"]["passed"])
        self.assertTrue(Path(payload["status_path"]).is_file())

    def test_reports_a_pre_identity_blocked_envelope(self):
        path = self.root / "blocked.json"
        MODULE.save_state(
            path,
            {
                "version": 1,
                "created_at": "2026-09-16T07:55:40Z",
                "iterations": 0,
                "history": [],
                "reruns": {},
                "coordinator": {
                    "status": "blocked",
                    "detail": (
                        "gh api repos/owner/repo/actions/jobs/104705281292 "
                        "failed (1): gh: Not Found (HTTP 404)"
                    ),
                    "observed_at": "2026-09-16T07:55:40Z",
                },
                "escalation": {
                    "reason": "coordinator_error",
                    "detail": (
                        "gh api repos/owner/repo/actions/jobs/104705281292 "
                        "failed (1): gh: Not Found (HTTP 404)"
                    ),
                    "checks": [],
                    "head_sha": None,
                    "next_action": MODULE.ESCALATION_ACTIONS["coordinator_error"],
                    "recorded_at": "2026-09-16T07:55:40Z",
                },
            },
        )

        payload = call("status", "--state", str(path))

        self.assertEqual("ready", payload["result"])
        self.assertIsNone(payload["pr"])
        self.assertEqual("blocked", payload["coordinator"]["status"])
        self.assertEqual("escalated", payload["stage_outcome"])

    def test_rejects_an_incomplete_state_that_is_not_blocked(self):
        path = self.root / "incomplete.json"
        MODULE.save_state(
            path,
            {
                "version": 1,
                "created_at": "2026-09-16T07:55:40Z",
                "iterations": 0,
            },
        )

        with self.assertRaisesRegex(
            MODULE.WorkflowError, "not a valid pre-identity blocked envelope"
        ):
            MODULE.command_status(SimpleNamespace(current=False, state=str(path)))

    def test_status_reports_when_the_helper_last_wrote_its_state(self):
        """The only signal a reader has for telling working from wedged.

        Every write stamps it, so a stamp minutes old and a stamp an hour old
        are different answers to the question a person actually asks.
        """
        path = write_state(self.root, updated_at="2026-02-03T04:05:06Z")
        payload = call("status", "--state", str(path))
        self.assertEqual("2026-02-03T04:05:06Z", payload["last_helper_activity"])
        snapshot = json.loads(
            Path(payload["status_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual("2026-02-03T04:05:06Z", snapshot["last_helper_activity"])

    def test_reports_an_escalation(self):
        path = write_state(
            self.root,
            escalation={
                "reason": "pre_existing_failures",
                "detail": "build already fails on main",
                "checks": ["check:a"],
                "next_action": MODULE.ESCALATION_ACTIONS["pre_existing_failures"],
                "head_sha": "head1",
                "recorded_at": "2026-01-01T00:00:00Z",
            },
        )
        payload = call("status", "--state", str(path))
        self.assertEqual("pre_existing_failures", payload["escalation"]["reason"])
        self.assertTrue(payload["escalation"]["next_action"])

    def test_reports_no_state_for_a_pull_request_the_loop_never_touched(self):
        target = MODULE.parse_target("owner/repo#404")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(MODULE, "require_tools"))
            stack.enter_context(
                mock.patch.object(MODULE, "resolve_repo_root", return_value=self.root)
            )
            stack.enter_context(
                mock.patch.object(MODULE, "current_pr_target", return_value=target)
            )
            stack.enter_context(
                mock.patch.object(
                    MODULE,
                    "default_state_path",
                    return_value=self.root / "missing.json",
                )
            )
            payload = call("status", "--current", "--repo-root", str(self.root))
        self.assertEqual("no_state", payload["result"])
        self.assertIsNone(payload["escalation"])

    def test_omits_the_stage_outcome_when_no_run_happened(self):
        """A missing state file is not a run that ended, so it names no ending.

        Emitting `no_progress` here would tell any reader that the stage ran and
        accomplished nothing, which is false both for a stage that was never
        launched and for one that cleared and then cleaned up after itself.
        """
        target = MODULE.parse_target("owner/repo#404")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(MODULE, "require_tools"))
            stack.enter_context(
                mock.patch.object(MODULE, "resolve_repo_root", return_value=self.root)
            )
            stack.enter_context(
                mock.patch.object(MODULE, "current_pr_target", return_value=target)
            )
            stack.enter_context(
                mock.patch.object(
                    MODULE,
                    "default_state_path",
                    return_value=self.root / "missing.json",
                )
            )
            payload = call("status", "--current", "--repo-root", str(self.root))
        self.assertEqual("no_state", payload["result"])
        self.assertNotIn("stage_outcome", payload)
        self.assertNotIn(
            "no_progress", json.dumps(payload), "no payload field may claim a run ended"
        )

    def test_requires_a_state_or_the_current_flag(self):
        with self.assertRaises(SystemExit):
            run_arguments("status")

    def test_names_the_ending_in_the_vocabulary_an_orchestrator_records(self):
        for overrides, expected in (
            ({"outcome": "green"}, "cleared"),
            ({"outcome": "no_checks", "skip_note": "no applicable checks"}, "skipped"),
            ({"escalation": {"reason": "timeout"}}, "escalated"),
            ({"outcome": "green", "escalation": {"reason": "timeout"}}, "escalated"),
            ({"escalation": {"reason": "max_iterations_reached"}}, "carried"),
        ):
            with self.subTest(expected=expected):
                path = write_state(self.root, **overrides)
                payload = call("status", "--state", str(path))
                self.assertEqual(expected, payload["stage_outcome"])

    def test_omits_the_stage_outcome_while_a_run_has_decided_nothing(self):
        """State exists from preflight on, so its bare presence names no ending.

        A run killed before it decided anything leaves the same state a run still
        in flight leaves. Reporting `no_progress` for either would assert that a
        run completed and achieved nothing, and two of those in a row escalate the
        whole pipeline, so a crash could escalate a healthy pull request.
        """
        for overrides in ({}, {"outcome": None}, {"clean_at_head_sha": "head1"}):
            with self.subTest(overrides=overrides):
                path = write_state(self.root, **overrides)
                payload = call("status", "--state", str(path))
                self.assertNotIn("stage_outcome", payload)
                self.assertNotIn(
                    "no_progress",
                    json.dumps(payload),
                    "no payload field may claim a run ended",
                )

    def test_reports_the_skip_note_a_reader_cannot_mistake_for_a_pass(self):
        path = write_state(
            self.root,
            outcome="no_checks",
            skip_note=(
                "CI Fix Loop skipped owner/repo#7: the pull request head reports no "
                "applicable checks, so this repository ran no CI on it."
            ),
        )
        payload = call("status", "--state", str(path))
        self.assertEqual("skipped", payload["stage_outcome"])
        self.assertIn("no applicable checks", payload["skip_note"])
        self.assertIsNone(payload["clean_at_head_sha"])


class StageOutcomeTest(unittest.TestCase):
    def test_a_run_that_did_nothing_is_never_reported_as_clear(self):
        self.assertIsNone(MODULE.stage_outcome({}))
        self.assertIsNone(MODULE.stage_outcome({"clean_at_head_sha": "head1"}))

    def test_never_manufactures_an_ending_the_state_cannot_support(self):
        """`no_progress` is the agent's claim to make, never the helper's.

        Only a live agent can report that a run ran to completion and achieved
        nothing. The helper reads state that a killed run leaves looking exactly
        like a run still in flight, so it withholds the field instead.
        """
        for state in ({}, {"outcome": None}, {"run": {"status": "active"}}):
            with self.subTest(state=state):
                self.assertIsNone(MODULE.stage_outcome(state))
                self.assertEqual({}, MODULE.stage_outcome_fields(state))

    def test_carries_the_field_only_for_an_ending_it_can_name(self):
        self.assertEqual(
            {"stage_outcome": "cleared"},
            MODULE.stage_outcome_fields({"outcome": "green"}),
        )

    def test_an_escalation_outranks_a_recorded_clearance(self):
        state = {"outcome": "green", "escalation": {"reason": "head_changed"}}
        self.assertEqual("escalated", MODULE.stage_outcome(state))

    def test_a_spent_iteration_cap_is_carried(self):
        self.assertEqual(
            "carried",
            MODULE.stage_outcome(
                {"escalation": {"reason": "max_iterations_reached"}}
            ),
        )

    def test_a_clearance_always_travels_with_the_head_it_was_measured_at(self):
        """The orchestrator refuses a clearance whose marker names another head.

        That guard reads one payload, so the marker has to be in the same payload
        as the word. A `cleared` with no `clean_at_head_sha` beside it would be
        rejected as a mismatch and read as a stage that answered nothing.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = write_state(
                Path(directory),
                outcome="green",
                clean_at_head_sha="head1",
                run={"head_sha": "head1", "status": "resolved"},
            )
            payload = call("status", "--state", str(path))
        self.assertEqual("cleared", payload["stage_outcome"])
        self.assertEqual("head1", payload["clean_at_head_sha"])
        self.assertEqual("head1", payload["run"]["head_sha"])


class ChargeIterationTest(unittest.TestCase):
    def test_spends_one_iteration_for_a_run_however_often_it_is_called(self):
        state = {"iterations": 2}
        run_state = {"head_sha": "head1"}
        self.assertTrue(MODULE.charge_iteration(state, run_state))
        self.assertFalse(MODULE.charge_iteration(state, run_state))
        self.assertFalse(MODULE.charge_iteration(state, run_state))
        self.assertEqual(3, state["iterations"])
        self.assertTrue(run_state["charged"])

    def test_each_run_spends_its_own_iteration(self):
        state = {"iterations": 0}
        for head in ("head1", "head2", "head3"):
            MODULE.charge_iteration(state, {"head_sha": head})
        self.assertEqual(3, state["iterations"])

    def test_a_fresh_run_at_an_unchanged_head_costs_nothing(self):
        """The budget bounds fix attempts, and an attempt is what moves the head.

        A relaunch that re-derives the same analysis at the head already charged
        is one logical attempt read twice, so charging it again would spend a
        fifth of the budget on nothing.
        """
        state = {"iterations": 0}

        self.assertTrue(MODULE.charge_iteration(state, {"head_sha": "head1"}))
        for _ in range(4):
            self.assertFalse(MODULE.charge_iteration(state, {"head_sha": "head1"}))

        self.assertEqual(1, state["iterations"])
        self.assertEqual("head1", state["charged_head_sha"])

    def test_a_moved_head_is_a_new_attempt_and_charges_again(self):
        state = {"iterations": 0}

        MODULE.charge_iteration(state, {"head_sha": "head1"})
        MODULE.charge_iteration(state, {"head_sha": "head2"})

        self.assertEqual(2, state["iterations"])
        self.assertEqual("head2", state["charged_head_sha"])

    def test_a_run_with_no_head_is_charged_rather_than_deduped_on_a_guess(self):
        state = {"iterations": 0}

        for _ in range(3):
            self.assertTrue(MODULE.charge_iteration(state, {}))

        self.assertEqual(3, state["iterations"])
        self.assertNotIn("charged_head_sha", state)

    def test_scoped_runs_charge_and_dedupe_independently(self):
        state = {
            "iterations": 0,
            "budget_charges": {
                "pipeline-position": 0,
                "pipeline-run": 0,
                "user-invocation": 0,
                "user-run": 0,
            },
        }
        pipeline = {
            "head_sha": "head1",
            "iteration": 1,
            "budget_head_key": "pipeline-position",
            "budget_charge_key": "pipeline-position",
            "budget_run_charge_key": "pipeline-run",
        }
        invocation = {
            "head_sha": "head1",
            "iteration": 2,
            "budget_head_key": "user-invocation",
            "budget_charge_key": "user-invocation",
            "budget_run_charge_key": "user-run",
        }

        self.assertTrue(MODULE.charge_iteration(state, pipeline))
        self.assertTrue(MODULE.charge_iteration(state, invocation))
        self.assertFalse(
            MODULE.charge_iteration(
                state,
                {
                    **pipeline,
                    "charged": False,
                },
            )
        )

        self.assertEqual(2, state["iterations"])
        self.assertEqual(1, state["budget_charges"]["pipeline-position"])
        self.assertEqual(1, state["budget_charges"]["pipeline-run"])
        self.assertEqual(1, state["budget_charges"]["user-invocation"])
        self.assertEqual(1, state["budget_charges"]["user-run"])


class AcceptedPushCheckpointTest(unittest.TestCase):
    def test_a_standalone_push_does_not_inherit_saved_pipeline_position(self):
        state = {
            "budget_scope": "invocation",
            "pipeline_budget": {
                "run": "old-pipeline",
                "iteration": 2,
            },
        }

        checkpoint = MODULE.accepted_push_checkpoint(
            state,
            previous_head="head1",
            head_sha="head2",
            commits=["head2"],
        )

        self.assertIsNone(checkpoint["pipeline_run"])
        self.assertIsNone(checkpoint["pipeline_iteration"])


class TestSuppressionTest(unittest.TestCase):
    def test_recognizes_a_test_path_by_directory_or_by_file_name(self):
        for path in (
            "src/test/java/com/example/FooTest.java",
            "tests/test_widget.py",
            "app/__tests__/widget.test.tsx",
            "pkg/thing_test.go",
            "spec/models/user_spec.rb",
            "lib/WidgetTests.cs",
            "TESTS/Upper_Test.py",
            "src\\test\\java\\FooTest.java",
        ):
            with self.subTest(path=path):
                self.assertTrue(MODULE.is_test_path(path))

    def test_leaves_production_code_alone(self):
        for path in (
            "src/main/java/com/example/Widget.java",
            "app/widget.ts",
            "docs/testing.md",
            "src/latest/thing.py",
            "",
            None,
            42,
        ):
            with self.subTest(path=path):
                self.assertFalse(MODULE.is_test_path(path))

    def test_names_every_way_a_line_stops_a_test_running(self):
        cases = {
            "@pytest.mark.skip(reason='broken')": "@pytest.mark.skip",
            "    @pytest.mark.xfail": "@pytest.mark.skip",
            "@unittest.skipIf(sys.platform == 'win32', 'nope')": "@unittest.skip",
            "        pytest.skip('flaky')": "pytest.skip()",
            "        self.skipTest('flaky')": "self.skipTest()",
            "  @Disabled(\"fails on CI\")": "@Disabled",
            "  @Ignore": "@Ignore",
            "  @Test(enabled = false)": "@Test(enabled = false)",
            "  xit('adds two numbers', () => {": "xit()",
            "  it.skip('adds two numbers', () => {": ".skip()",
            "  test.todo('adds two numbers')": ".todo()",
            "\tt.Skip(\"broken\")": "t.Skip()",
            "#[ignore]": "#[ignore]",
            "[Ignore(\"broken\")]": "[Ignore]",
            "  Skip = \"broken on arm\"": 'Skip = "..."',
        }
        for line, marker in cases.items():
            with self.subTest(line=line):
                self.assertIn(marker, MODULE.suppression_markers(line))

    def test_prose_about_a_skip_is_not_a_skip(self):
        """A pattern that fired on prose would refuse an honest commit.

        The refusal has no override, so a false positive stops the loop dead.
        These lines all mention skipping without doing any.
        """
        for line in (
            "# this test used to be skipped, and is not any more",
            "    assert result.skip is False",
            "// Ignore the ordering here; the assertion below is what matters.",
            "        self.assertEqual(expected, disabled_reason)",
            "  @Test(expected = IllegalStateException.class)",
            "  boolean enabled = false;",
            None,
            17,
        ):
            with self.subTest(line=line):
                self.assertEqual([], MODULE.suppression_markers(line))


class CommitSuppressionTest(unittest.TestCase):
    """Read real commits, because the scan parses real `git show` output."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        MODULE.git(self.root, "init", "--quiet", ".")
        MODULE.git(self.root, "config", "user.email", "loop@example.invalid")
        MODULE.git(self.root, "config", "user.name", "Loop")
        MODULE.git(self.root, "config", "commit.gpgsign", "false")

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")

    def commit(self, message):
        MODULE.git(self.root, "add", "--all")
        MODULE.git(self.root, "commit", "--quiet", "--message", message)
        return MODULE.git(self.root, "rev-parse", "HEAD")

    def test_reports_a_deleted_test_file(self):
        self.write("tests/test_widget.py", "def test_widget():\n    assert True\n")
        self.write("app.py", "value = 1\n")
        self.commit("first")
        (self.root / "tests" / "test_widget.py").unlink()
        head = self.commit("drop the test")
        findings = MODULE.commit_suppressions(self.root, head)
        self.assertEqual(
            [{"kind": "deleted_test_file", "path": "tests/test_widget.py", "marker": None}],
            findings,
        )

    def test_ignores_a_deleted_source_file(self):
        self.write("app.py", "value = 1\n")
        self.write("helper.py", "value = 2\n")
        self.commit("first")
        (self.root / "helper.py").unlink()
        head = self.commit("drop the helper")
        self.assertEqual([], MODULE.commit_suppressions(self.root, head))

    def test_reports_a_skip_added_to_a_test_that_was_running(self):
        self.write(
            "tests/test_widget.py",
            "def test_widget():\n    assert compute() == 2\n",
        )
        self.commit("first")
        self.write(
            "tests/test_widget.py",
            "import pytest\n\n\n"
            "@pytest.mark.skip(reason='fails on CI')\n"
            "def test_widget():\n    assert compute() == 2\n",
        )
        head = self.commit("silence the test")
        findings = MODULE.commit_suppressions(self.root, head)
        self.assertEqual(1, len(findings))
        self.assertEqual("added_suppression", findings[0]["kind"])
        self.assertEqual("tests/test_widget.py", findings[0]["path"])
        self.assertEqual("@pytest.mark.skip", findings[0]["marker"])

    def test_ignores_a_skip_that_the_commit_removed(self):
        """Re-enabling a test is the opposite of suppressing one."""
        self.write(
            "tests/test_widget.py",
            "import pytest\n\n\n@pytest.mark.skip\ndef test_widget():\n    pass\n",
        )
        self.commit("first")
        self.write(
            "tests/test_widget.py",
            "import pytest\n\n\ndef test_widget():\n    pass\n",
        )
        head = self.commit("re-enable the test")
        self.assertEqual([], MODULE.commit_suppressions(self.root, head))

    def test_ignores_an_annotation_outside_a_test_file(self):
        self.write("app.py", "value = 1\n")
        self.commit("first")
        self.write("app.py", "value = 1\n# @Disabled\n")
        head = self.commit("comment")
        self.assertEqual([], MODULE.commit_suppressions(self.root, head))

    def test_a_new_test_file_that_is_born_skipped_is_reported(self):
        """Adding a test already disabled is coverage that never runs."""
        self.write("app.py", "value = 1\n")
        self.commit("first")
        self.write(
            "tests/test_new.py",
            "import pytest\n\n\n@pytest.mark.skip\ndef test_new():\n    pass\n",
        )
        head = self.commit("add a disabled test")
        markers = [item["marker"] for item in MODULE.commit_suppressions(self.root, head)]
        self.assertEqual(["@pytest.mark.skip"], markers)

    def test_refusal_names_the_commit_and_the_finding(self):
        self.write("tests/test_widget.py", "def test_widget():\n    pass\n")
        self.commit("first")
        (self.root / "tests" / "test_widget.py").unlink()
        head = self.commit("drop the test")
        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.refuse_test_suppression(self.root, [head])
        message = str(error.exception)
        self.assertIn("stopping a test from running", message)
        self.assertIn("tests/test_widget.py", message)
        self.assertIn(head, message)
        self.assertIn("unfixable_failure", message)

    def test_an_honest_fix_passes(self):
        self.write("app.py", "def compute():\n    return 1\n")
        self.write("tests/test_widget.py", "def test_widget():\n    assert True\n")
        self.commit("first")
        self.write("app.py", "def compute():\n    return 2\n")
        head = self.commit("fix the arithmetic")
        MODULE.refuse_test_suppression(self.root, [head])


class PipelineBudgetTest(unittest.TestCase):
    """A stage budget belongs to an outer loop's iteration, not to a launch."""

    RECORDED = {"run": "run-a", "iteration": 2, "baseline": 3, "run_baseline": 1}

    def scope(self, state, **pipeline):
        return MODULE.pipeline_scope(state, SimpleNamespace(**pipeline))

    def test_the_run_token_alone_decides_whether_the_budget_is_scoped(self):
        """Enumerate every subset of the three arguments rather than assert it in prose.

        The two halves are not symmetric for a reader. An iteration with no run
        asks which run it belongs to and nothing can answer it. A run with no
        iteration still answers what the token is for, whether this loop has seen
        the run before, so it scopes on equality alone. Only the outer cap is
        optional in the other sense: leaving it out falls back rather than lifting
        the ceiling.
        """
        parts = {
            "run": {"pipeline_run": "run-a"},
            "iteration": {"pipeline_iteration": 2},
            "cap": {"pipeline_max_iterations": 3},
        }
        scoped_by_names = {
            (): False,
            ("run",): True,
            ("iteration",): False,
            ("cap",): False,
            ("run", "cap"): True,
            ("iteration", "cap"): False,
            ("run", "iteration"): True,
            ("run", "iteration", "cap"): True,
        }
        for names, scoped in scoped_by_names.items():
            with self.subTest(names=names):
                pipeline = {}
                for name in names:
                    pipeline.update(parts[name])
                scope = self.scope({"iterations": 9}, **pipeline)
                self.assertEqual(scoped, scope is not None)
                self.assertEqual(
                    scoped,
                    MODULE.absolute_iteration_cap(
                        scope, 5, pipeline.get("pipeline_max_iterations")
                    )
                    is not None,
                )

    def test_a_standalone_run_never_resets_anything(self):
        """Absent, empty, and unusable run tokens must never read as a new run."""
        for pipeline in (
            {},
            {"pipeline_run": None, "pipeline_iteration": None},
            {"pipeline_run": "", "pipeline_iteration": 2},
            {"pipeline_run": 7, "pipeline_iteration": 2},
            {"pipeline_iteration": 2},
            {"pipeline_iteration": 2, "pipeline_max_iterations": 3},
        ):
            with self.subTest(pipeline=pipeline):
                state = {"iterations": 4}
                self.assertIsNone(self.scope(state, **pipeline))
                self.assertEqual(4, state["iterations"])
                self.assertNotIn("pipeline_budget", state)

    def test_a_new_pipeline_run_clears_both_budgets(self):
        state = {"iterations": 5, "pipeline_budget": dict(self.RECORDED)}

        scope = self.scope(state, pipeline_run="run-b", pipeline_iteration=1)

        self.assertEqual(
            {"run": "run-b", "iteration": 1, "baseline": 5, "run_baseline": 5}, scope
        )
        self.assertEqual((0, 0), MODULE.budget_spent(state, scope))

    def test_the_pipeline_advancing_clears_only_the_per_iteration_budget(self):
        """The whole-run ceiling must survive an advance, or it bounds nothing."""
        state = {"iterations": 9, "pipeline_budget": dict(self.RECORDED)}

        scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=3)

        self.assertEqual(
            {"run": "run-a", "iteration": 3, "baseline": 9, "run_baseline": 1}, scope
        )
        self.assertEqual((0, 8), MODULE.budget_spent(state, scope))

    def test_a_relaunch_inside_one_iteration_buys_nothing(self):
        state = {"iterations": 9, "pipeline_budget": dict(self.RECORDED)}

        scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=2)

        self.assertEqual(self.RECORDED, scope)
        self.assertEqual((6, 8), MODULE.budget_spent(state, scope))

    def test_replaying_an_earlier_iteration_buys_nothing(self):
        """Strictly greater, so a repeat and a replay both buy nothing."""
        state = {"iterations": 9, "pipeline_budget": dict(self.RECORDED)}

        scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=1)

        self.assertEqual(self.RECORDED, scope)

    def test_a_second_run_resets_even_though_it_counts_from_one_again(self):
        """A pipeline numbers its iterations from one, so this must not be ordered.

        Comparing iterations across runs would leave a pull request that reached
        iteration three permanently unable to reset, and the ceiling would then
        refuse every future run on it. A deadlock outlasts the false start it
        would have prevented, so run identity is compared for equality instead.
        """
        state = {
            "iterations": 9,
            "pipeline_budget": {
                "run": "run-a",
                "iteration": 6,
                "baseline": 7,
                "run_baseline": 2,
            },
        }

        scope = self.scope(state, pipeline_run="run-b", pipeline_iteration=1)

        self.assertEqual(
            {"run": "run-b", "iteration": 1, "baseline": 9, "run_baseline": 9}, scope
        )

    def test_a_reset_never_rewrites_the_durable_count_itself(self):
        """Both budgets are baselines, so the per-PR iteration numbering stays monotone.

        Zeroing the count instead would restart the numbering, and a run id built
        from it would collide with one already folded into history, where a
        duplicate is dropped rather than recorded.
        """
        state = {"iterations": 9, "pipeline_budget": dict(self.RECORDED)}

        self.scope(state, pipeline_run="run-b", pipeline_iteration=1)

        self.assertEqual(9, state["iterations"])

    def test_an_iteration_with_no_run_is_ignored_rather_than_half_applied(self):
        """A run token must come from the caller, never from what this loop recorded.

        Reading it back out of an earlier budget, a head it pushed, or an
        escalation it wrote would be this loop naming its own position.
        """
        states = (
            {},
            {"iterations": 4},
            {"iterations": 4, "pipeline_budget": dict(self.RECORDED)},
            {"iterations": 4, "pr": {"head_sha": "aaaa"}, "history": [{"id": "one"}]},
            {"iterations": 4, "escalation": {"reason": "max_iterations_reached"}},
            {"iterations": 4, "clean_at_head_sha": "aaaa"},
        )
        for state in states:
            with self.subTest(state=state):
                self.assertIsNone(
                    self.scope(dict(state), pipeline_iteration=2, pipeline_max_iterations=3)
                )

    def test_a_lone_run_token_resets_once_and_is_inert_on_every_relaunch(self):
        """This is what makes the degraded case coarser rather than launch-scoped.

        The caller mints one token per run and repeats it on every relaunch inside
        that run, so equality alone still tells a first sighting from a repeat. The
        budget therefore refreshes once when the run arrives and never again while
        it lasts, which is the stricter direction, not the unbounded one.
        """
        state = {"iterations": 5}

        first = self.scope(state, pipeline_run="run-a")
        self.assertEqual(5, first["baseline"])
        self.assertEqual(5, first["run_baseline"])

        state["pipeline_budget"] = first
        for spent in (5, 7, 40):
            with self.subTest(spent=spent):
                state["iterations"] = spent
                relaunch = self.scope(state, pipeline_run="run-a")
                self.assertEqual(5, relaunch["baseline"])
                self.assertEqual(5, relaunch["run_baseline"])

    def test_an_unusable_iteration_degrades_rather_than_refusing_the_pull_request(self):
        """Ignoring the run outright is the permanent refusal this contract removes.

        The durable count only ever climbs, so a position this loop discarded would
        leave a pull request that already reached the cap refusing every later run
        for the rest of its life. The usable half is used instead.
        """
        for iteration in (None, 0, -1, True, "2", 1.5):
            with self.subTest(iteration=iteration):
                scope = self.scope(
                    {"iterations": 5}, pipeline_run="run-a", pipeline_iteration=iteration
                )
                self.assertIsNotNone(scope)
                self.assertIsNone(
                    MODULE.exhausted_budget({"iterations": 5}, scope, 5, 10)
                )

    def test_only_the_position_the_caller_passes_can_reset_the_budget(self):
        """Enumerate the inputs to a reset instead of claiming the property in prose.

        A repeat of one position stays inert no matter what this loop did in
        between: a new head, a commit it pushed, an escalation it recorded, a
        clearance, or more iterations it spent. Every one of those varies here
        while the caller's values stay the same, and neither baseline moves.
        """
        observable = (
            {},
            {"pr": {"head_sha": "new-head"}},
            {"pr": {"head_sha": "another-head"}, "run": {"status": "published"}},
            {"escalation": {"reason": "max_iterations_reached"}},
            {"history": [{"id": "one"}, {"id": "two"}]},
            {"clean_at_head_sha": "new-head"},
            {"reruns": {"1": 2}},
        )
        for spent in (0, 3, 5, 40):
            for extra in observable:
                with self.subTest(spent=spent, extra=extra):
                    state = {
                        "iterations": spent,
                        "pipeline_budget": dict(self.RECORDED),
                        **extra,
                    }
                    scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=2)
                    self.assertEqual(self.RECORDED, scope)

    def test_the_run_is_opaque_and_only_ever_compared_for_equality(self):
        """Tokens that would sort or parse are still just tokens."""
        state = {
            "iterations": 4,
            "pipeline_budget": {
                "run": "2026-05-01/7",
                "iteration": 3,
                "baseline": 2,
                "run_baseline": 0,
            },
        }

        same = self.scope(state, pipeline_run="2026-05-01/7", pipeline_iteration=3)
        self.assertEqual(2, same["baseline"])
        for other in ("2026-05-01/8", "2026-04-01/7", "7", "run", " 2026-05-01/7"):
            with self.subTest(other=other):
                scope = self.scope(state, pipeline_run=other, pipeline_iteration=3)
                self.assertEqual(4, scope["baseline"])
                self.assertEqual(4, scope["run_baseline"])

    def test_an_omitted_outer_cap_falls_back_rather_than_disabling_the_ceiling(self):
        """Only the outer cap is optional, and omitting it must not remove the bound."""
        scope = {"run": "run-a", "iteration": 1, "baseline": 0, "run_baseline": 0}
        for value in (None, 0, -1, True, "3"):
            with self.subTest(value=value):
                self.assertEqual(
                    5 * MODULE.DEFAULT_PIPELINE_MAX_ITERATIONS,
                    MODULE.absolute_iteration_cap(scope, 5, value),
                )

    def test_the_ceiling_is_derived_from_the_callers_own_cap(self):
        scope = {"run": "run-a", "iteration": 1, "baseline": 0, "run_baseline": 0}
        self.assertEqual(15, MODULE.absolute_iteration_cap(scope, 5, 3))
        self.assertEqual(20, MODULE.absolute_iteration_cap(scope, 10, 2))

    def test_there_is_no_ceiling_without_a_pipeline(self):
        self.assertIsNone(MODULE.absolute_iteration_cap(None, 5, 3))

    def test_names_which_budget_ran_out(self):
        scope = {"run": "run-a", "iteration": 4, "baseline": 10, "run_baseline": 0}
        self.assertIsNone(MODULE.exhausted_budget({"iterations": 14}, scope, 5, 20))
        self.assertEqual(
            "iteration", MODULE.exhausted_budget({"iterations": 15}, scope, 5, 20)
        )
        self.assertEqual(
            "absolute", MODULE.exhausted_budget({"iterations": 10}, scope, 5, 10)
        )
        self.assertEqual(
            "iteration", MODULE.exhausted_budget({"iterations": 5}, None, 5, None)
        )

    def test_a_standalone_run_keeps_the_flat_per_pull_request_cap(self):
        """No arguments means the behavior this loop has always had."""
        for spent, expected in ((0, None), (4, None), (5, "iteration"), (9, "iteration")):
            with self.subTest(spent=spent):
                self.assertEqual(
                    expected,
                    MODULE.exhausted_budget({"iterations": spent}, None, 5, None),
                )

    def test_a_scoped_run_spends_against_its_baseline_and_not_the_lifetime_count(self):
        """A spent brake must not read as a permanent refusal.

        Ninety iterations over the pull request's life say nothing about the run
        that just started, which has spent none of its own budget.
        """
        scope = {"run": "run-a", "iteration": 1, "baseline": 90, "run_baseline": 90}
        self.assertIsNone(MODULE.exhausted_budget({"iterations": 90}, scope, 5, 10))
        self.assertIsNone(MODULE.exhausted_budget({"iterations": 94}, scope, 5, 10))
        self.assertEqual(
            "iteration", MODULE.exhausted_budget({"iterations": 95}, scope, 5, 10)
        )

    def test_the_running_total_survives_a_pipeline_iteration(self):
        """The ceiling only bounds anything if the per-iteration reset spares it."""
        state = {"iterations": 0}
        head = 0
        for iteration in (1, 2):
            scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=iteration)
            state["pipeline_budget"] = scope
            state.pop("charged_head_sha", None)
            for _ in range(5):
                head += 1
                MODULE.charge_iteration(state, {"head_sha": f"head{head}"})
        self.assertEqual(10, state["iterations"])
        self.assertEqual((5, 10), MODULE.budget_spent(state, scope))
        self.assertEqual("absolute", MODULE.exhausted_budget(state, scope, 5, 10))

    def test_preflight_takes_the_position_and_defaults_it_to_absent(self):
        parser = MODULE.build_parser()

        bare = parser.parse_args(["preflight"])
        self.assertIsNone(bare.pipeline_run)
        self.assertIsNone(bare.pipeline_iteration)
        self.assertIsNone(bare.pipeline_max_iterations)

        given = parser.parse_args(
            [
                "preflight",
                "--pipeline-run",
                "run-a",
                "--pipeline-iteration",
                "2",
                "--pipeline-max-iterations",
                "3",
            ]
        )
        self.assertEqual("run-a", given.pipeline_run)
        self.assertEqual(2, given.pipeline_iteration)
        self.assertEqual(3, given.pipeline_max_iterations)


class InvocationBudgetTest(unittest.TestCase):
    def scope(self, state, *arguments):
        return MODULE.invocation_scope(state, run_arguments("preflight", *arguments))

    def test_a_new_invocation_preserves_the_lifetime_count_and_starts_at_zero(self):
        state = {"iterations": 5}

        with mock.patch.object(MODULE.uuid, "uuid4") as token:
            token.return_value.hex = "new-run"
            scope = self.scope(state, "--new-invocation")

        self.assertEqual(
            {
                "run": "new-run",
                "iteration": None,
                "baseline": 5,
                "run_baseline": 5,
            },
            scope,
        )
        self.assertEqual(5, state["iterations"])
        self.assertEqual((0, 0), MODULE.budget_spent(state, scope))

    def test_the_returned_token_reuses_only_its_active_invocation_budget(self):
        state = {
            "iterations": 7,
            "invocation_budget": {
                "run": "active-run",
                "iteration": None,
                "baseline": 5,
                "run_baseline": 5,
            },
        }

        scope = self.scope(state, "--invocation-run", "active-run")

        self.assertEqual((2, 2), MODULE.budget_spent(state, scope))

    def test_an_unknown_invocation_token_is_rejected(self):
        state = {
            "iterations": 7,
            "invocation_budget": {
                "run": "active-run",
                "iteration": None,
                "baseline": 5,
                "run_baseline": 5,
            },
        }

        with self.assertRaisesRegex(MODULE.WorkflowError, "--new-invocation"):
            self.scope(state, "--invocation-run", "other-run")


class BudgetAdvancedTest(unittest.TestCase):
    """The per-head charge lives exactly as long as the budget it protects."""

    RECORDED = {"run": "run-a", "iteration": 2, "baseline": 3, "run_baseline": 1}

    def test_a_new_run_or_a_later_iteration_both_count_as_an_advance(self):
        for scope in (
            {"run": "run-b", "iteration": 1},
            {"run": "run-a", "iteration": 3},
            {"run": "run-a", "iteration": 99},
        ):
            with self.subTest(scope=scope):
                self.assertTrue(MODULE.budget_advanced(self.RECORDED, scope))

    def test_a_repeat_a_replay_and_a_standalone_run_are_not_an_advance(self):
        for recorded, scope in (
            (self.RECORDED, {"run": "run-a", "iteration": 2}),
            (self.RECORDED, {"run": "run-a", "iteration": 1}),
            (self.RECORDED, {"run": "run-a", "iteration": None}),
            (self.RECORDED, None),
            (None, None),
        ):
            with self.subTest(recorded=recorded, scope=scope):
                self.assertFalse(MODULE.budget_advanced(recorded, scope))

    def test_a_run_scoped_budget_advances_the_first_time_it_learns_an_iteration(self):
        recorded = {"run": "run-a", "iteration": None, "baseline": 5, "run_baseline": 5}
        self.assertTrue(MODULE.budget_advanced(recorded, {"run": "run-a", "iteration": 1}))

    def test_nothing_recorded_yet_reads_as_an_advance(self):
        self.assertTrue(MODULE.budget_advanced(None, {"run": "run-a", "iteration": 1}))
        self.assertTrue(MODULE.budget_advanced("junk", {"run": "run-a", "iteration": 1}))


class CleanupCommandTest(unittest.TestCase):
    def test_deletes_the_state_and_every_side_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_state(root)
            for side in (
                MODULE.preflight_path_for(path),
                MODULE.checks_path_for(path),
                MODULE.status_path_for(path),
            ):
                side.write_text("{}", encoding="utf-8")
            payload = call("cleanup", "--state", str(path))
            self.assertEqual("cleaned_up", payload["result"])
            self.assertFalse(path.exists())
            self.assertFalse(MODULE.diff_path_for(path).exists())
            self.assertFalse(MODULE.preflight_path_for(path).exists())
            self.assertFalse(MODULE.checks_path_for(path).exists())
            self.assertFalse(MODULE.status_path_for(path).exists())

    def test_deletes_artifacts_from_replaced_managed_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "state.json"
            archived_prompt = root / "state--old--local-triage-prompt.txt"
            archived_prompt.write_text("prompt", encoding="utf-8")
            log_directory = root / "state--ci-fix-logs-old"
            log_directory.mkdir()
            archived_log = log_directory / "failure.log"
            archived_log.write_text("failed", encoding="utf-8")
            MODULE.save_state(
                path,
                {
                    "version": MODULE.STATE_VERSION,
                    "agent_task": {"status": "completed"},
                    "managed_task_history": [
                        {
                            "triage_prompt_file": str(archived_prompt),
                            "preflight": {
                                "check_snapshot": {
                                    "failures": [
                                        {"log_path": str(archived_log)}
                                    ]
                                }
                            },
                        }
                    ],
                },
            )

            payload = call("cleanup", "--state", str(path))

            self.assertEqual("cleaned_up", payload["result"])
            self.assertFalse(archived_prompt.exists())
            self.assertFalse(archived_log.exists())
            self.assertFalse(log_directory.exists())


class PreflightCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.metadata = {
            "number": 7,
            "title": "Add a thing",
            "pr_url": "https://github.com/owner/repo/pull/7",
            "repo_name": "owner/repo",
            "upstream_owner": "owner",
            "upstream_repo": "repo",
            "head_owner": "fork",
            "head_repo": "repo",
            "head_branch": "feature",
            "head_sha": "head1",
            "base_branch": "main",
            "base_sha": "base1",
            "is_fork": True,
            "is_draft": True,
            "commits": [{"sha": "c1", "message": "Add a thing"}],
        }

    def preflight(self, stack, *, status="", head="head1", state_path=None, pipeline=()):
        def call_git(repo_root, *arguments):
            if arguments[0] == "status":
                return status
            if arguments[0] == "rev-parse":
                return head
            if arguments[0] == "branch":
                return "feature"
            raise AssertionError(f"unexpected git call: {arguments}")

        stack.enter_context(mock.patch.object(MODULE, "require_tools"))
        stack.enter_context(
            mock.patch.object(MODULE, "resolve_repo_root", return_value=self.root)
        )
        stack.enter_context(mock.patch.object(MODULE, "git", call_git))
        stack.enter_context(
            mock.patch.object(MODULE, "metadata_for", return_value=self.metadata)
        )
        stack.enter_context(mock.patch.object(MODULE, "checkout_pr", return_value=True))
        stack.enter_context(
            mock.patch.object(
                MODULE,
                "fetch_authoritative_diff",
                return_value=(DIFF, "github"),
            )
        )
        stack.enter_context(
            mock.patch.object(MODULE, "changed_files_for", return_value=["app.py"])
        )
        stack.enter_context(
            mock.patch.object(MODULE, "commit_provenance", return_value=[])
        )
        return call(
            "preflight",
            "owner/repo#7",
            "--repo-root",
            str(self.root),
            "--state",
            str(state_path or self.root / "state.json"),
            *pipeline,
        )

    def test_pins_the_head_and_the_diff(self):
        with contextlib.ExitStack() as stack:
            payload = self.preflight(stack)
        self.assertEqual("ready", payload["result"])
        self.assertEqual("head1", payload["head_sha"])
        self.assertEqual("base1", payload["base_sha"])
        self.assertEqual(1, payload["iteration"])
        self.assertEqual(5, payload["max_iterations"])
        self.assertEqual(DIFF, Path(payload["diff_path"]).read_text(encoding="utf-8"))
        self.assertTrue(Path(payload["preflight_path"]).is_file())
        self.assertEqual("github", payload["diff_source"])
        preflight = json.loads(
            Path(payload["preflight_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual("github", preflight["diff_source"])
        self.assertEqual(
            "github", MODULE.load_state(Path(payload["state"]))["run"]["diff_source"]
        )

    def test_pipeline_position_supersedes_redundant_new_invocation(self):
        path = self.root / "state.json"
        with contextlib.ExitStack() as stack:
            payload = self.preflight(
                stack,
                state_path=path,
                pipeline=[
                    "--new-invocation",
                    "--pipeline-run",
                    "bc204b55bc1240b18bc5193123ceb226",
                    "--pipeline-iteration",
                    "1",
                    "--pipeline-max-iterations",
                    "2",
                ],
            )

        state = MODULE.load_state(path)
        self.assertEqual("ready", payload["result"])
        self.assertEqual("pipeline", payload["budget_scope"])
        self.assertEqual(
            "bc204b55bc1240b18bc5193123ceb226",
            state["pipeline_budget"]["run"],
        )
        self.assertNotIn("invocation_budget", state)

    def test_refuses_a_dirty_worktree(self):
        with contextlib.ExitStack() as stack:
            with self.assertRaises(MODULE.WorkflowError) as error:
                self.preflight(stack, status=" M app.py")
        self.assertIn("worktree is not clean", str(error.exception))

    def test_refuses_a_local_head_that_is_not_the_pull_request_head(self):
        with contextlib.ExitStack() as stack:
            with self.assertRaises(MODULE.WorkflowError) as error:
                self.preflight(stack, head="other1")
        self.assertIn("HEAD mismatch", str(error.exception))

    def test_reading_the_checks_again_spends_no_iteration(self):
        path = self.root / "state.json"
        for _ in range(3):
            with contextlib.ExitStack() as stack:
                payload = self.preflight(stack, state_path=path)
            self.assertEqual(1, payload["iteration"])
            self.assertEqual("ready", payload["result"])
        self.assertEqual(0, MODULE.load_state(path)["iterations"])

    def test_forgets_the_outcome_the_previous_run_recorded(self):
        path = write_state(
            self.root, outcome="green", clean_at_head_sha="head1", iterations=1
        )
        with contextlib.ExitStack() as stack:
            payload = self.preflight(stack, state_path=path)
        self.assertEqual("ready", payload["result"])
        state = MODULE.load_state(path)
        self.assertIsNone(state["outcome"])
        self.assertIsNone(state["clean_at_head_sha"])
        self.assertIsNone(MODULE.stage_outcome(state))

    def test_stops_at_the_iteration_cap(self):
        path = self.root / "state.json"
        for index in range(MODULE.DEFAULT_MAX_ITERATIONS):
            # A charge is per head, so each attempt has to land on its own head
            # the way a real fix does.
            head = f"head{index + 1}"
            self.metadata["head_sha"] = head
            with contextlib.ExitStack() as stack:
                self.preflight(stack, head=head, state_path=path)
            state = MODULE.load_state(path)
            MODULE.charge_iteration(state, state["run"])
            MODULE.save_state(path, state)
        self.metadata["head_sha"] = "head-last"
        with contextlib.ExitStack() as stack:
            payload = self.preflight(stack, head="head-last", state_path=path)
        self.assertEqual("max_iterations_reached", payload["result"])
        escalation = MODULE.load_state(path)["escalation"]
        self.assertEqual("max_iterations_reached", escalation["reason"])
        self.assertTrue(escalation["next_action"])

    def test_a_second_user_invocation_gets_five_fresh_attempts(self):
        path = self.root / "state.json"
        invocation_run = None
        for index in range(MODULE.DEFAULT_MAX_ITERATIONS):
            head = f"old-head-{index + 1}"
            self.metadata["head_sha"] = head
            arguments = (
                ["--new-invocation"]
                if invocation_run is None
                else ["--invocation-run", invocation_run]
            )
            with contextlib.ExitStack() as stack:
                payload = self.preflight(
                    stack, head=head, state_path=path, pipeline=arguments
                )
            invocation_run = payload["invocation_run"]
            state = MODULE.load_state(path)
            MODULE.charge_iteration(state, state["run"])
            MODULE.save_state(path, state)

        self.metadata["head_sha"] = "new-head-1"
        with contextlib.ExitStack() as stack:
            fresh = self.preflight(
                stack,
                head="new-head-1",
                state_path=path,
                pipeline=["--new-invocation"],
            )

        self.assertEqual("ready", fresh["result"])
        self.assertEqual(6, fresh["iteration"])
        self.assertEqual(0, fresh["completed_iterations"])
        self.assertEqual("reused", fresh["state_origin"])
        self.assertEqual("fresh", fresh["budget_origin"])
        self.assertEqual("invocation", fresh["budget_scope"])
        self.assertNotEqual(invocation_run, fresh["invocation_run"])

        invocation_run = fresh["invocation_run"]
        for index in range(MODULE.DEFAULT_MAX_ITERATIONS):
            head = f"new-head-{index + 1}"
            self.metadata["head_sha"] = head
            with contextlib.ExitStack() as stack:
                attempt = self.preflight(
                    stack,
                    head=head,
                    state_path=path,
                    pipeline=["--invocation-run", invocation_run],
                )
            self.assertEqual("ready", attempt["result"])
            self.assertEqual(index, attempt["completed_iterations"])
            self.assertEqual("reused", attempt["budget_origin"])
            state = MODULE.load_state(path)
            MODULE.charge_iteration(state, state["run"])
            MODULE.save_state(path, state)

        with contextlib.ExitStack() as stack:
            resumed_fifth = self.preflight(
                stack,
                head="new-head-5",
                state_path=path,
                pipeline=["--invocation-run", invocation_run],
            )
        self.assertEqual("ready", resumed_fifth["result"])
        self.assertEqual(10, resumed_fifth["iteration"])
        self.assertEqual(5, resumed_fifth["completed_iterations"])

        self.metadata["head_sha"] = "new-head-6"
        with contextlib.ExitStack() as stack:
            exhausted = self.preflight(
                stack,
                head="new-head-6",
                state_path=path,
                pipeline=["--invocation-run", invocation_run],
            )
        self.assertEqual("max_iterations_reached", exhausted["result"])
        self.assertEqual(5, exhausted["completed_iterations"])
        self.assertEqual(10, MODULE.load_state(path)["iterations"])

    def test_migrates_a_legacy_pipeline_before_a_standalone_run_can_charge(self):
        path = write_state(
            self.root,
            iterations=4,
            charged_head_sha="head1",
            pipeline_budget={
                "run": "pipeline-a",
                "iteration": 1,
                "baseline": 2,
                "run_baseline": 2,
            },
            run={"iteration": 4},
        )
        self.metadata["head_sha"] = "standalone-head"
        with contextlib.ExitStack() as stack:
            standalone = self.preflight(
                stack,
                head="standalone-head",
                state_path=path,
                pipeline=["--new-invocation"],
            )
        state = MODULE.load_state(path)
        MODULE.charge_iteration(state, state["run"])
        MODULE.save_state(path, state)

        self.metadata["head_sha"] = "pipeline-head"
        with contextlib.ExitStack() as stack:
            pipeline = self.preflight(
                stack,
                head="pipeline-head",
                state_path=path,
                pipeline=[
                    "--pipeline-run",
                    "pipeline-a",
                    "--pipeline-iteration",
                    "1",
                ],
            )

        self.assertEqual(2, pipeline["completed_iterations"])
        self.assertEqual("ready", pipeline["result"])

    def test_migrates_a_legacy_charged_head_into_its_pipeline_scope(self):
        path = write_state(
            self.root,
            iterations=4,
            charged_head_sha="head1",
            pipeline_budget={
                "run": "pipeline-a",
                "iteration": 1,
                "baseline": 0,
                "run_baseline": 0,
            },
            run={"iteration": 4},
        )
        with contextlib.ExitStack() as stack:
            resumed = self.preflight(
                stack,
                state_path=path,
                pipeline=[
                    "--pipeline-run",
                    "pipeline-a",
                    "--pipeline-iteration",
                    "1",
                ],
            )

        state = MODULE.load_state(path)
        self.assertEqual(4, resumed["iteration"])
        self.assertEqual("ready", resumed["result"])
        self.assertFalse(MODULE.charge_iteration(state, state["run"]))
        self.assertEqual(4, state["iterations"])

        MODULE.save_state(path, state)
        with contextlib.ExitStack() as stack:
            fresh = self.preflight(
                stack,
                state_path=path,
                pipeline=["--new-invocation"],
            )
        state = MODULE.load_state(path)
        self.assertEqual(5, fresh["iteration"])
        self.assertTrue(MODULE.charge_iteration(state, state["run"]))
        self.assertEqual(5, state["iterations"])

    def test_a_legacy_lifetime_head_cannot_leak_into_a_fresh_invocation(self):
        path = write_state(
            self.root,
            iterations=5,
            charged_head_sha="head1",
            run={"iteration": 5},
        )
        with contextlib.ExitStack() as stack:
            fresh = self.preflight(
                stack,
                state_path=path,
                pipeline=["--new-invocation"],
            )

        state = MODULE.load_state(path)
        self.assertEqual(6, fresh["iteration"])
        self.assertTrue(MODULE.charge_iteration(state, state["run"]))
        self.assertEqual(6, state["iterations"])
        self.assertEqual("head1", state["budget_charged_heads"]["lifetime"]["head_sha"])

    def test_a_stale_legacy_pipeline_budget_does_not_hide_a_lifetime_charge(self):
        path = write_state(
            self.root,
            iterations=5,
            charged_head_sha="head1",
            pipeline_budget={
                "run": "old-pipeline",
                "iteration": 1,
                "baseline": 0,
                "run_baseline": 0,
            },
            run={"iteration": 5},
        )

        with contextlib.ExitStack() as stack:
            resumed = self.preflight(stack, state_path=path)

        self.assertEqual("ready", resumed["result"])
        self.assertEqual(5, resumed["iteration"])
        state = MODULE.load_state(path)
        self.assertFalse(MODULE.charge_iteration(state, state["run"]))
        self.assertEqual(5, state["iterations"])

    def test_a_second_preflight_at_a_charged_head_costs_nothing_and_keeps_its_number(self):
        """One logical attempt, read twice, must be billed once and numbered once.

        Advancing the number without charging would let the label outrun the
        budget, and a third read would then mint ids that collide with the second
        read's archived entries, which `archive_run` drops rather than records.
        """
        path = self.root / "state.json"
        with contextlib.ExitStack() as stack:
            first = self.preflight(stack, state_path=path)
        state = MODULE.load_state(path)
        MODULE.charge_iteration(state, state["run"])
        MODULE.save_state(path, state)
        self.assertEqual(1, first["iteration"])
        self.assertEqual(1, state["iterations"])

        for _ in range(3):
            with contextlib.ExitStack() as stack:
                again = self.preflight(stack, state_path=path)
            self.assertEqual("ready", again["result"])
            self.assertEqual(1, again["iteration"])
            self.assertEqual(1, MODULE.load_state(path)["iterations"])

    def test_a_preflight_after_the_head_moved_is_a_new_attempt(self):
        path = self.root / "state.json"
        with contextlib.ExitStack() as stack:
            self.preflight(stack, state_path=path)
        state = MODULE.load_state(path)
        MODULE.charge_iteration(state, state["run"])
        MODULE.save_state(path, state)

        self.metadata["head_sha"] = "head2"
        with contextlib.ExitStack() as stack:
            moved = self.preflight(stack, head="head2", state_path=path)

        self.assertEqual(2, moved["iteration"])

    def test_a_pipeline_iteration_frees_the_head_it_already_charged(self):
        """The per-head charge protects one budget and must not outlive it."""
        path = self.root / "state.json"
        with contextlib.ExitStack() as stack:
            self.preflight(
                stack,
                state_path=path,
                pipeline=["--pipeline-run", "run-a", "--pipeline-iteration", "1"],
            )
        state = MODULE.load_state(path)
        MODULE.charge_iteration(state, state["run"])
        MODULE.save_state(path, state)
        charged_heads = MODULE.load_state(path)["budget_charged_heads"]
        self.assertEqual(
            ["head1"],
            [entry["head_sha"] for entry in charged_heads.values()],
        )

        with contextlib.ExitStack() as stack:
            advanced = self.preflight(
                stack,
                state_path=path,
                pipeline=["--pipeline-run", "run-a", "--pipeline-iteration", "2"],
            )

        self.assertEqual(1, len(MODULE.load_state(path)["budget_charged_heads"]))
        self.assertEqual(2, advanced["iteration"])
        complete = json.loads(
            Path(advanced["preflight_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(0, complete["completed_iterations"])

    def test_standalone_and_pipeline_budgets_do_not_spend_each_other(self):
        path = self.root / "state.json"
        pipeline = [
            "--pipeline-run",
            "pipeline-a",
            "--pipeline-iteration",
            "1",
            "--pipeline-max-iterations",
            "2",
        ]
        with contextlib.ExitStack() as stack:
            first_pipeline = self.preflight(
                stack, state_path=path, pipeline=pipeline
            )
        state = MODULE.load_state(path)
        MODULE.charge_iteration(state, state["run"])
        MODULE.save_state(path, state)

        with contextlib.ExitStack() as stack:
            standalone = self.preflight(
                stack, state_path=path, pipeline=["--new-invocation"]
            )
        state = MODULE.load_state(path)
        MODULE.charge_iteration(state, state["run"])
        MODULE.save_state(path, state)

        with contextlib.ExitStack() as stack:
            resumed_pipeline = self.preflight(
                stack, state_path=path, pipeline=pipeline
            )

        self.assertEqual(1, first_pipeline["iteration"])
        self.assertEqual(2, standalone["iteration"])
        self.assertEqual(1, resumed_pipeline["iteration"])
        self.assertEqual(1, resumed_pipeline["completed_iterations"])
        self.assertEqual("reused", resumed_pipeline["budget_origin"])

    def test_a_pipeline_iteration_never_rewrites_the_durable_count(self):
        """Zeroing it would restart the numbering and collide with archived ids.

        `archive_run` keys history on the iteration number, and it drops a
        duplicate rather than recording it, so a budget that rewrote the count
        would silently lose the second attempt's verdicts.
        """
        path = self.root / "state.json"
        for index, head in enumerate(("head1", "head2")):
            self.metadata["head_sha"] = head
            with contextlib.ExitStack() as stack:
                self.preflight(
                    stack,
                    head=head,
                    state_path=path,
                    pipeline=[
                        "--pipeline-run",
                        "run-a",
                        "--pipeline-iteration",
                        str(index + 1),
                    ],
                )
            state = MODULE.load_state(path)
            MODULE.charge_iteration(state, state["run"])
            state["run"]["attributions"] = {
                "check:a": {"verdict": "pr_caused", "name": "a"}
            }
            MODULE.save_state(path, state)

        self.metadata["head_sha"] = "head3"
        with contextlib.ExitStack() as stack:
            self.preflight(
                stack,
                head="head3",
                state_path=path,
                pipeline=["--pipeline-run", "run-a", "--pipeline-iteration", "2"],
            )

        state = MODULE.load_state(path)
        self.assertEqual(2, state["iterations"])
        self.assertEqual(
            ["1:verdict:check:a", "2:verdict:check:a"],
            sorted(entry["id"] for entry in state["history"]),
        )

    def test_a_new_head_forgets_the_reruns_of_the_old_one(self):
        path = write_state(self.root, reruns={"check:a": {"count": 1}})
        self.metadata["head_sha"] = "head2"
        with contextlib.ExitStack() as stack:
            self.preflight(stack, head="head2", state_path=path)
        self.assertEqual({}, MODULE.load_state(path)["reruns"])

    def test_keeps_the_reruns_of_the_same_head(self):
        path = write_state(self.root, reruns={"check:a": {"count": 1}})
        with contextlib.ExitStack() as stack:
            self.preflight(stack, state_path=path)
        self.assertEqual(1, MODULE.load_state(path)["reruns"]["check:a"]["count"])


class MainTest(unittest.TestCase):
    def test_reports_a_workflow_error_as_json_and_a_failure_code(self):
        stream = io.StringIO()
        with mock.patch.object(
            MODULE.sys, "argv", ["ci_fix_loop.py", "cleanup", "--state", "missing.json"]
        ):
            with contextlib.redirect_stdout(stream):
                code = MODULE.main()
        self.assertEqual(1, code)
        payload = json.loads(stream.getvalue())
        self.assertEqual("error", payload["result"])
        self.assertIn("state file does not exist", payload["error"])

    def test_reports_success_with_a_zero_code(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_state(Path(directory))
            stream = io.StringIO()
            with mock.patch.object(
                MODULE.sys, "argv", ["ci_fix_loop.py", "cleanup", "--state", str(path)]
            ):
                with contextlib.redirect_stdout(stream):
                    code = MODULE.main()
            self.assertEqual(0, code)
            self.assertEqual("cleaned_up", json.loads(stream.getvalue())["result"])


class NativeStackParsingTest(unittest.TestCase):
    def test_member_state_is_required(self):
        raw = {
            "number": 77,
            "size": 1,
            "baseRefName": "main",
            "entries": {
                "nodes": [
                    {
                        "position": 0,
                        "pullRequest": {
                            "number": 7,
                            "title": "PR 7",
                            "headRefName": "feature",
                            "baseRefName": "main",
                            "headRefOid": "head1",
                            "isDraft": False,
                        },
                    }
                ]
            },
        }

        with self.assertRaisesRegex(
            MODULE.WorkflowError, "missing a required field"
        ):
            MODULE.parse_native_stack(raw)


class NativeStackCoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.stack_state = self.root / "stack.json"
        self.resolver = self.root / "pr_conflict_resolver.py"
        self.resolver.write_text("# test", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def start(self, stack):
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "resolve_repo_root", return_value=self.root
        ), mock.patch.object(
            MODULE, "read_native_stack", return_value=stack
        ), mock.patch.object(
            MODULE, "conflict_resolver_script", return_value=self.resolver
        ):
            return call(
                "stack-start",
                "owner/repo#7",
                "--repo-root",
                str(self.root),
                "--state",
                str(self.stack_state),
            )

    def member_state(self, number, head, run_id, *, outcome="green"):
        directory = self.root / f"member-{number}"
        directory.mkdir()
        checkpoint = {
            "id": f"push-{number}",
            "head_sha": head,
            "pipeline_run": run_id,
            "pipeline_iteration": 1,
            "commits": [f"commit-{number}"],
        }
        return write_state(
            directory,
            pr={
                "number": number,
                "title": f"PR {number}",
                "pr_url": f"https://github.com/owner/repo/pull/{number}",
                "repo_name": "owner/repo",
                "head_sha": head,
            },
            outcome=outcome,
            clean_at_head_sha=head,
            budget_scope="pipeline",
            pipeline_budget={"run": run_id, "iteration": 1},
            accepted_pushes=[checkpoint],
            run={
                "budget_scope": "pipeline",
                "stack_guard": {
                    "state": str(self.stack_state),
                    "run_id": run_id,
                    "member": number,
                    "member_head_sha": head,
                },
            },
        )

    def pending_member_state(self, run_id, *, kind="fix", check_key=None):
        directory = self.root / f"pending-{kind}"
        directory.mkdir()
        pending = {
            "id": f"pending-{kind}",
            "previous_head_sha": "lower1",
            "head_sha": "lower2",
            "commits": ["fix"],
            "kind": kind,
            "pipeline_run": run_id,
            "member": 5,
            "validation": {"status": "passed", "commands": ["test"]},
            "resume": {
                "command": "rerun" if kind == "ci_rerun" else "publish",
            },
        }
        if check_key:
            pending["check_key"] = check_key
            pending["resume"]["check"] = check_key
            pending["resume"]["name"] = "build"
            pending["resume"]["run_id"] = 123
        return write_state(
            directory,
            pr={
                "number": 5,
                "title": "PR 5",
                "pr_url": "https://github.com/owner/repo/pull/5",
                "repo_name": "owner/repo",
                "head_sha": "lower1",
            },
            budget_scope="pipeline",
            pipeline_budget={"run": run_id, "iteration": 1},
            run={
                "budget_scope": "pipeline",
                "stack_guard": {
                    "state": str(self.stack_state),
                    "run_id": run_id,
                    "member": 5,
                    "member_head_sha": "lower1",
                },
            },
            pending_stack_push=pending,
        )

    def unfinished_rerun_member_state(self, run_id, status):
        directory = self.root / f"unfinished-rerun-{status}"
        directory.mkdir()
        return write_state(
            directory,
            pr={
                "number": 5,
                "title": "PR 5",
                "pr_url": "https://github.com/owner/repo/pull/5",
                "repo_name": "owner/repo",
                "head_sha": "lower1",
            },
            budget_scope="pipeline",
            pipeline_budget={"run": run_id, "iteration": 1},
            run={
                "budget_scope": "pipeline",
                "stack_guard": {
                    "state": str(self.stack_state),
                    "run_id": run_id,
                    "member": 5,
                    "member_head_sha": "lower1",
                },
            },
            reruns={
                "build (linux)": {
                    "count": 1,
                    "name": "build",
                    "run_id": 123,
                    "head_sha": "lower1",
                    "method": "empty_commit",
                    "status": status,
                    "commit_sha": "lower2" if status != "creating" else None,
                }
            },
        )

    def next(self, stack, *, contains=True):
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=stack
        ), mock.patch.object(MODULE, "commit_contains", return_value=contains):
            return call("stack-next", "--state", str(self.stack_state))

    def record(self, stack, member_state):
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=stack
        ):
            return call(
                "stack-record",
                "--state",
                str(self.stack_state),
                "--member-state",
                str(member_state),
            )

    def test_non_stack_target_keeps_the_single_pr_path(self):
        result = self.start(None)
        self.assertEqual("single", result["result"])
        self.assertEqual("https://github.com/owner/repo/pull/7", result["target"])
        self.assertFalse(self.stack_state.exists())

    def test_orchestrated_invocation_cannot_start_a_recursive_stack_run(self):
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "resolve_repo_root", return_value=self.root
        ), mock.patch.object(MODULE, "read_native_stack") as read_stack:
            result = call(
                "stack-start",
                "owner/repo#7",
                "--repo-root",
                str(self.root),
                "--pipeline-run",
                "outer-run",
            )
        self.assertEqual("single", result["result"])
        self.assertEqual("orchestrated_invocation", result["reason"])
        read_stack.assert_not_called()

    def test_missing_resolver_stops_before_stack_state_is_created(self):
        missing = self.root / "missing-resolver.py"
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "resolve_repo_root", return_value=self.root
        ), mock.patch.object(
            MODULE, "read_native_stack", return_value=native_stack()
        ), mock.patch.object(
            MODULE, "conflict_resolver_script", return_value=missing
        ):
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "before any repair starts"
            ):
                call(
                    "stack-start",
                    "owner/repo#7",
                    "--repo-root",
                    str(self.root),
                    "--state",
                    str(self.stack_state),
                )
        self.assertFalse(self.stack_state.exists())

    def test_merged_lower_members_are_omitted_from_the_active_stack(self):
        stack = native_stack(
            branches={
                5: ("lower", "main"),
                7: ("middle", "main"),
                9: ("upper", "middle"),
            }
        )
        stack["members"][0]["state"] = "MERGED"

        started = self.start(stack)

        self.assertEqual("stack", started["result"])
        self.assertEqual([7, 9], started["members"])
        self.assertEqual(
            [{"number": 5, "state": "MERGED"}], started["inactive_members"]
        )
        self.assertEqual(
            [7, 9],
            [
                member["number"]
                for member in MODULE.load_stack_state(self.stack_state)["members"]
            ],
        )
        first = self.next(stack)
        self.assertEqual("run_member", first["result"])
        self.assertEqual(7, first["member"])

    def test_inactive_member_that_leaves_a_stack_gap_is_rejected(self):
        stack = native_stack()
        stack["members"][0]["state"] = "CLOSED"

        with self.assertRaisesRegex(
            MODULE.WorkflowError,
            "not linear at pull request #7 after omitting inactive pull request #5",
        ):
            self.start(stack)

        self.assertFalse(self.stack_state.exists())

    def test_one_open_member_uses_the_single_pull_request_path(self):
        stack = native_stack()
        stack["members"][0]["state"] = "MERGED"
        stack["members"][2]["state"] = "CLOSED"

        result = self.start(stack)

        self.assertEqual("single", result["result"])
        self.assertEqual("no_open_stack_peers", result["reason"])
        self.assertEqual(
            [
                {"number": 5, "state": "MERGED"},
                {"number": 9, "state": "CLOSED"},
            ],
            result["inactive_members"],
        )
        self.assertFalse(self.stack_state.exists())

    def test_selected_inactive_member_is_rejected(self):
        stack = native_stack()
        stack["members"][1]["state"] = "MERGED"

        with self.assertRaisesRegex(
            MODULE.WorkflowError, "pull request #7 is MERGED, not open"
        ):
            self.start(stack)

        self.assertFalse(self.stack_state.exists())

    def test_middle_target_starts_at_the_bottom_and_continues_upward(self):
        stack = native_stack()
        started = self.start(stack)
        self.assertEqual("stack", started["result"])
        self.assertEqual([5, 7, 9], started["members"])
        first = self.next(stack)
        self.assertEqual("run_member", first["result"])
        self.assertEqual(5, first["member"])
        lower_state = self.member_state(5, "lower1", started["run_id"])
        recorded = self.record(stack, lower_state)
        self.assertEqual("recorded", recorded["result"])
        second = self.next(stack)
        self.assertEqual(7, second["member"])
        self.assertEqual(started["run_id"], second["pipeline_run"])

    def test_child_waits_for_propagation_when_parent_head_is_not_contained(self):
        stack = native_stack()
        started = self.start(stack)
        lower_state = self.member_state(5, "lower1", started["run_id"])
        self.record(stack, lower_state)
        action = self.next(stack, contains=False)
        self.assertEqual("propagate", action["result"])
        self.assertEqual(5, action["fixed_pr"])
        self.assertEqual("lower1", action["expected_head"])
        self.assertEqual(7, action["next_member"])

    def test_cleared_member_movement_retires_it_and_its_descendants(self):
        stack = native_stack()
        started = self.start(stack)
        lower_state = self.member_state(5, "lower1", started["run_id"])
        self.record(stack, lower_state)
        self.next(stack)
        middle_state = self.member_state(7, "middle1", started["run_id"])
        self.record(stack, middle_state)
        self.next(stack)

        pending = {
            "id": "pending-upper",
            "previous_head_sha": "upper1",
            "head_sha": "upper2",
            "commits": ["upper-fix"],
            "kind": "fix",
            "pipeline_run": started["run_id"],
            "member": 9,
            "validation": {"status": "passed", "commands": ["test"]},
            "resume": {"command": "publish"},
        }
        pending_directory = self.root / "pending-upper"
        pending_directory.mkdir()
        pending_state = write_state(
            pending_directory,
            pr={
                "number": 9,
                "title": "PR 9",
                "pr_url": "https://github.com/owner/repo/pull/9",
                "repo_name": "owner/repo",
                "head_sha": "upper1",
            },
            run={
                "budget_scope": "pipeline",
                "stack_guard": {
                    "state": str(self.stack_state),
                    "run_id": started["run_id"],
                    "member": 9,
                    "member_head_sha": "upper1",
                },
            },
            pending_stack_push=pending,
            reruns={
                "build (linux)": {
                    "count": 1,
                    "name": "build",
                    "run_id": 123,
                    "head_sha": "upper1",
                    "method": "empty_commit",
                    "status": "prepared",
                    "commit_sha": "upper2",
                }
            },
        )
        coordinator_member_state = MODULE.stack_member_state_path(
            self.stack_state, 9
        )
        coordinator_member_state.write_text(
            pending_state.read_text(encoding="utf-8"), encoding="utf-8"
        )
        coordinator = MODULE.load_stack_state(self.stack_state)
        coordinator["propagations"] = [{"fixed_pr": 5, "head_sha": "lower1"}]
        coordinator["propagation_attempts"] = ["5:lower1"]
        coordinator["propagation_guards"] = [
            {
                "fixed_pr": 5,
                "fixed_head_sha": "lower1",
                "member": 7,
                "member_head_sha": "middle1",
            }
        ]
        coordinator["pending_format"] = {
            "fixed_pr": 7,
            "expected_head": "middle1",
            "formatting_member": 9,
        }
        MODULE.save_state(self.stack_state, coordinator)

        moved = native_stack(heads={5: "lower1", 7: "middle2", 9: "upper2"})
        retired_result = self.next(moved, contains=False)
        self.assertEqual("propagate", retired_result["result"])
        self.assertEqual(5, retired_result["fixed_pr"])
        self.assertEqual(7, retired_result["next_member"])
        self.assertEqual(7, retired_result["retired_attempt"]["member"])

        result = self.next(moved)
        self.assertEqual("run_member", result["result"])
        self.assertEqual(7, result["member"])
        self.assertEqual("middle2", result["head_sha"])
        self.assertEqual(2, result["member_attempt"])
        self.assertNotIn("retired_attempt", result)

        recovered = MODULE.load_stack_state(self.stack_state)
        self.assertEqual("active", recovered["status"])
        self.assertEqual(1, recovered["cursor"])
        self.assertEqual("clear", recovered["members"][0]["ci_status"])
        self.assertEqual("lower1", recovered["members"][0]["clean_at_head_sha"])
        self.assertEqual(["push-5"], [
            checkpoint["id"]
            for checkpoint in recovered["members"][0]["accepted_pushes"]
        ])
        self.assertEqual("active", recovered["members"][1]["ci_status"])
        self.assertIsNone(recovered["members"][1]["clean_at_head_sha"])
        self.assertEqual("pending", recovered["members"][2]["ci_status"])
        self.assertEqual(2, recovered["members"][2]["attempt"])
        self.assertEqual(
            [{"fixed_pr": 5, "head_sha": "lower1"}],
            recovered["propagations"],
        )
        self.assertEqual(["5:lower1"], recovered["propagation_attempts"])
        self.assertIsNone(recovered["pending_format"])
        self.assertEqual(1, len(recovered["retired_attempts"]))
        retired = recovered["retired_attempts"][0]
        self.assertEqual("middle1", retired["previous_head_sha"])
        self.assertEqual("middle2", retired["current_head_sha"])
        self.assertEqual([7, 9], retired["affected_members"])
        self.assertEqual(9, retired["retired_work"][0]["member"])
        self.assertEqual(
            "pending-upper",
            retired["retired_work"][0]["pending_stack_push"]["id"],
        )

        recovered_member = MODULE.load_state(coordinator_member_state)
        self.assertNotIn("pending_stack_push", recovered_member)
        self.assertEqual(1, len(recovered_member["retired_stack_work"]))
        self.assertEqual(
            "retired", recovered_member["reruns"]["build (linux)"]["status"]
        )

        repeated = self.next(moved)
        self.assertEqual("run_member", repeated["result"])
        self.assertNotIn("retired_attempt", repeated)
        self.assertEqual(
            1,
            len(MODULE.load_stack_state(self.stack_state)["retired_attempts"]),
        )

    def test_record_reports_retirement_instead_of_recording_stale_state(self):
        stack = native_stack()
        started = self.start(stack)
        lower_state = self.member_state(5, "lower1", started["run_id"])
        self.record(stack, lower_state)
        moved = native_stack(heads={5: "lower2", 7: "middle1", 9: "upper1"})
        middle_state = self.member_state(7, "middle1", started["run_id"])

        result = self.record(moved, middle_state)

        self.assertEqual("retired_attempt", result["result"])
        self.assertEqual(5, result["retired_attempt"]["member"])
        self.assertEqual("stack-next", result["next"])

    def test_propagate_reports_retirement_before_using_a_stale_checkpoint(self):
        stack = native_stack()
        started = self.start(stack)
        lower_state = self.member_state(5, "lower1", started["run_id"])
        self.record(stack, lower_state)
        moved = native_stack(heads={5: "lower2", 7: "middle1", 9: "upper1"})

        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=moved
        ):
            result = call(
                "stack-propagate",
                "--state",
                str(self.stack_state),
                "--fixed-pr",
                "5",
                "--expected-head",
                "lower1",
            )

        self.assertEqual("retired_attempt", result["result"])
        self.assertEqual(5, result["retired_attempt"]["member"])
        self.assertEqual("stack-next", result["next"])

    def test_completed_stack_reopens_when_a_cleared_member_moves(self):
        stack = native_stack()
        started = self.start(stack)
        for number, head in ((5, "lower1"), (7, "middle1"), (9, "upper1")):
            self.next(stack)
            member_state = self.member_state(number, head, started["run_id"])
            self.record(stack, member_state)
        self.assertEqual("complete", self.next(stack)["result"])

        moved = native_stack(heads={5: "lower1", 7: "middle2", 9: "upper2"})
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=moved
        ):
            status = call("stack-status", "--state", str(self.stack_state))
        self.assertEqual("retired_attempt", status["result"])
        self.assertEqual("active", status["status"])
        self.assertEqual("stack-next", status["next"])

        result = self.next(moved)

        self.assertEqual("run_member", result["result"])
        self.assertEqual(7, result["member"])
        self.assertNotIn("retired_attempt", result)

    def test_member_guard_rechecks_parent_after_the_child_is_released(self):
        stack = native_stack()
        started = self.start(stack)
        lower_state = self.member_state(5, "lower1", started["run_id"])
        self.record(stack, lower_state)
        self.next(stack)
        moved = native_stack(heads={5: "lower2", 7: "middle1", 9: "upper1"})
        with mock.patch.object(
            MODULE, "read_native_stack", return_value=moved
        ), self.assertRaisesRegex(
            MODULE.WorkflowError, "moved from lower1 to lower2"
        ):
            MODULE.verify_stack_member_guard(
                self.stack_state, MODULE.parse_target("owner/repo#7"), "middle1"
            )

    def test_unexplained_active_member_movement_stops_before_more_work(self):
        stack = native_stack()
        self.start(stack)
        self.next(stack)
        moved = native_stack(heads={5: "lower2", 7: "middle1", 9: "upper1"})
        missing_state = self.root / "missing-member-state.json"
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=moved
        ), mock.patch.object(
            MODULE, "stack_member_state_path", return_value=missing_state
        ):
            result = call("stack-next", "--state", str(self.stack_state))
        self.assertEqual("stopped", result["result"])
        self.assertEqual("active_member_head_changed", result["reason"])

    def test_record_requires_a_clear_marker_at_the_live_head(self):
        stack = native_stack()
        started = self.start(stack)
        stale = self.member_state(5, "lower1", started["run_id"])
        state = MODULE.load_state(stale)
        state["clean_at_head_sha"] = "old-lower"
        MODULE.save_state(stale, state)
        result = self.record(stack, stale)
        self.assertEqual("stopped", result["result"])
        self.assertEqual("member_not_clear", result["reason"])
        self.assertEqual(5, result["blocked_member"])

    def test_record_preserves_head_change_escalation_from_dispatched_head(self):
        stack = native_stack()
        started = self.start(stack)
        self.next(stack)
        escalated = self.member_state(5, "lower1", started["run_id"])
        state = MODULE.load_state(escalated)
        state["outcome"] = None
        state["clean_at_head_sha"] = None
        state["escalation"] = {
            "reason": "head_changed",
            "detail": (
                "the PR head moved from lower1 to lower2 while this iteration "
                "was reading its checks"
            ),
        }
        MODULE.save_state(escalated, state)
        moved = native_stack(heads={5: "lower2", 7: "middle1", 9: "upper1"})

        result = self.record(moved, escalated)

        self.assertEqual("stopped", result["result"])
        self.assertEqual("member_not_clear", result["reason"])
        saved = MODULE.load_stack_state(self.stack_state)
        member = saved["members"][0]
        self.assertEqual("lower2", member["head_sha"])
        self.assertEqual("lower1", member["dispatched_head_sha"])
        self.assertEqual("blocked", member["ci_status"])
        self.assertEqual("escalated", member["stage_outcome"])

    def test_record_rejects_result_for_undispatched_moved_head(self):
        stack = native_stack()
        started = self.start(stack)
        self.next(stack)
        moved = native_stack(heads={5: "lower2", 7: "middle1", 9: "upper1"})
        undispatched = self.member_state(5, "lower2", started["run_id"])

        with self.assertRaisesRegex(MODULE.WorkflowError, "was not produced"):
            self.record(moved, undispatched)

    def test_record_rejects_a_clear_marker_from_another_stack_run(self):
        stack = native_stack()
        self.start(stack)
        other = self.member_state(5, "lower1", "other-run")
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=stack
        ), self.assertRaisesRegex(MODULE.WorkflowError, "was not produced"):
            call(
                "stack-record",
                "--state",
                str(self.stack_state),
                "--member-state",
                str(other),
            )

    def test_stale_pipeline_budget_cannot_authorize_a_standalone_result(self):
        stack = native_stack()
        started = self.start(stack)
        standalone = self.member_state(5, "lower1", started["run_id"])
        state = MODULE.load_state(standalone)
        state["budget_scope"] = "invocation"
        state["run"]["budget_scope"] = "invocation"
        state["run"]["stack_guard"] = None
        MODULE.save_state(standalone, state)
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=stack
        ), self.assertRaisesRegex(MODULE.WorkflowError, "was not produced"):
            call(
                "stack-record",
                "--state",
                str(self.stack_state),
                "--member-state",
                str(standalone),
            )

    def test_interrupted_member_surfaces_its_unpropagated_push(self):
        stack = native_stack()
        started = self.start(stack)
        self.next(stack)
        updated = native_stack(heads={5: "lower2", 7: "middle1", 9: "upper1"})
        directory = self.root / "resume-member"
        directory.mkdir()
        member_state = write_state(
            directory,
            pr={
                "number": 5,
                "title": "PR 5",
                "pr_url": "https://github.com/owner/repo/pull/5",
                "repo_name": "owner/repo",
                "head_sha": "lower2",
            },
            pipeline_budget={"run": started["run_id"], "iteration": 1},
            accepted_pushes=[
                {
                    "id": "pending-push",
                    "head_sha": "lower2",
                    "pipeline_run": started["run_id"],
                    "pipeline_iteration": 1,
                    "commits": ["fix"],
                }
            ],
        )
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=updated
        ), mock.patch.object(
            MODULE, "stack_member_state_path", return_value=member_state
        ):
            action = call("stack-next", "--state", str(self.stack_state))
        self.assertEqual("propagate", action["result"])
        self.assertEqual("accepted_push_not_propagated", action["reason"])
        self.assertEqual("pending-push", action["checkpoint_id"])
        self.assertEqual("lower2", action["expected_head"])

    def test_interrupted_member_surfaces_its_managed_task_recovery(self):
        stack = native_stack()
        started = self.start(stack)
        self.next(stack)
        directory = self.root / "resume-agent-task"
        directory.mkdir()
        member_state = write_state(
            directory,
            pr={
                "number": 5,
                "title": "PR 5",
                "pr_url": "https://github.com/owner/repo/pull/5",
                "repo_name": "owner/repo",
                "head_sha": "lower1",
            },
            run={
                "stack_guard": {
                    "state": str(self.stack_state),
                    "run_id": started["run_id"],
                    "member": 5,
                    "member_head_sha": "lower1",
                }
            },
            agent_task={
                "status": "failed",
                "recovery_command": "python ci_fix_loop.py agent-task --resume",
            },
        )
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "read_native_stack", return_value=stack),
            mock.patch.object(
                MODULE, "stack_member_state_path", return_value=member_state
            ),
        ):
            action = call("stack-next", "--state", str(self.stack_state))

        self.assertEqual("resume_agent-task", action["result"])
        self.assertEqual("agent_task_failed", action["reason"])
        self.assertEqual(str(member_state), action["member_state"])

    def test_landed_pending_push_is_finalized_and_propagated_after_a_crash(self):
        stack = native_stack()
        started = self.start(stack)
        self.next(stack)
        updated = native_stack(heads={5: "lower2", 7: "middle1", 9: "upper1"})
        member_state = self.pending_member_state(started["run_id"])
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=updated
        ), mock.patch.object(
            MODULE, "stack_member_state_path", return_value=member_state
        ):
            action = call("stack-next", "--state", str(self.stack_state))
        self.assertEqual("propagate", action["result"])
        self.assertEqual("pending-fix", action["checkpoint_id"])
        recovered = MODULE.load_state(member_state)
        self.assertNotIn("pending_stack_push", recovered)
        self.assertEqual(
            ["pending-fix"],
            [checkpoint["id"] for checkpoint in recovered["accepted_pushes"]],
        )
        retried = call("publish", "--state", str(member_state))
        self.assertEqual("published", retried["result"])
        self.assertTrue(retried["recovered"])
        self.assertEqual("pending-fix", retried["accepted_push"]["id"])

    def test_unlanded_pending_fix_resumes_the_prepared_publish(self):
        stack = native_stack()
        started = self.start(stack)
        self.next(stack)
        member_state = self.pending_member_state(started["run_id"])
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=stack
        ), mock.patch.object(
            MODULE, "stack_member_state_path", return_value=member_state
        ):
            action = call("stack-next", "--state", str(self.stack_state))
        self.assertEqual("resume_publish", action["result"])
        self.assertEqual(5, action["member"])
        self.assertEqual(str(member_state), action["member_state"])
        pending = MODULE.load_state(member_state)["pending_stack_push"]
        self.assertEqual("pending-fix", pending["id"])
        self.assertEqual("lower1", pending["previous_head_sha"])
        self.assertEqual("lower2", pending["head_sha"])
        self.assertEqual({"command": "publish"}, pending["resume"])

    def test_unlanded_empty_rerun_resumes_the_prepared_check(self):
        stack = native_stack()
        started = self.start(stack)
        self.next(stack)
        member_state = self.pending_member_state(
            started["run_id"], kind="ci_rerun", check_key="build (linux)"
        )
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=stack
        ), mock.patch.object(
            MODULE, "stack_member_state_path", return_value=member_state
        ):
            action = call("stack-next", "--state", str(self.stack_state))
        self.assertEqual("resume_rerun", action["result"])
        self.assertEqual(5, action["member"])
        self.assertEqual(str(member_state), action["member_state"])
        self.assertEqual("build (linux)", action["check"])
        pending = MODULE.load_state(member_state)["pending_stack_push"]
        self.assertEqual("pending-ci_rerun", pending["id"])
        self.assertEqual("lower1", pending["previous_head_sha"])
        self.assertEqual("lower2", pending["head_sha"])
        self.assertEqual(
            {
                "command": "rerun",
                "check": "build (linux)",
                "name": "build",
                "run_id": 123,
            },
            pending["resume"],
        )

    def test_landed_empty_rerun_can_repeat_after_finalization(self):
        stack = native_stack()
        started = self.start(stack)
        self.next(stack)
        updated = native_stack(heads={5: "lower2", 7: "middle1", 9: "upper1"})
        member_state = self.pending_member_state(
            started["run_id"], kind="ci_rerun", check_key="build (linux)"
        )
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=updated
        ), mock.patch.object(
            MODULE, "stack_member_state_path", return_value=member_state
        ):
            action = call("stack-next", "--state", str(self.stack_state))
        self.assertEqual("propagate", action["result"])

        retried = call(
            "rerun",
            "--state",
            str(member_state),
            "--check",
            "build (linux)",
        )
        self.assertEqual("empty_commit_published", retried["result"])
        self.assertTrue(retried["recovered"])
        self.assertEqual("build", retried["name"])
        self.assertEqual(123, retried["run_id"])
        self.assertEqual("pending-ci_rerun", retried["accepted_push"]["id"])

    def test_unfinished_empty_rerun_without_a_push_intent_is_resumed(self):
        stack = native_stack()
        started = self.start(stack)
        self.next(stack)
        for status in ("creating", "prepared"):
            with self.subTest(status=status):
                member_state = self.unfinished_rerun_member_state(
                    started["run_id"], status
                )
                with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
                    MODULE, "read_native_stack", return_value=stack
                ), mock.patch.object(
                    MODULE, "stack_member_state_path", return_value=member_state
                ):
                    action = call("stack-next", "--state", str(self.stack_state))
                self.assertEqual("resume_rerun", action["result"])
                self.assertEqual("build (linux)", action["check"])
                self.assertEqual(f"empty_commit_{status}", action["reason"])

    def test_propagation_refreshes_every_rewritten_descendant(self):
        stack = native_stack()
        self.start(stack)
        updated = native_stack(heads={5: "lower1", 7: "middle2", 9: "upper2"})
        script = self.root / "pr_conflict_resolver.py"
        script.write_text("# test", encoding="utf-8")
        process = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "result": "published",
                    "members_published": [
                        {"number": 7, "head_sha": "middle2"},
                        {"number": 9, "head_sha": "upper2"},
                    ],
                }
            ),
            stderr="",
        )
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", side_effect=[stack, updated]
        ), mock.patch.object(
            MODULE, "conflict_resolver_script", return_value=script
        ), mock.patch.object(
            MODULE, "commit_contains", return_value=True
        ), mock.patch.object(MODULE, "run", return_value=process) as run:
            result = call(
                "stack-propagate",
                "--state",
                str(self.stack_state),
                "--fixed-pr",
                "5",
                "--expected-head",
                "lower1",
            )
        self.assertEqual("propagated", result["result"])
        saved = MODULE.load_stack_state(self.stack_state)
        self.assertEqual(
            ["lower1", "middle2", "upper2"],
            [member["head_sha"] for member in saved["members"]],
        )
        self.assertEqual(
            [
                {
                    "fixed_pr": 5,
                    "fixed_head_sha": "lower1",
                    "member": 7,
                    "member_head_sha": "middle2",
                },
                {
                    "fixed_pr": 5,
                    "fixed_head_sha": "lower1",
                    "member": 9,
                    "member_head_sha": "upper2",
                },
            ],
            saved["propagation_guards"],
        )
        command = run.call_args.args[0]
        self.assertIn("descendant-propagate", command)
        self.assertIn("--stack-number", command)
        self.assertIn("77", command)
        self.assertIn("--expected-head", command)
        self.assertIn("lower1", command)
        state_index = command.index("--state")
        self.assertEqual(
            str(MODULE.stack_propagation_state_path(self.stack_state, 5, "lower1")),
            command[state_index + 1],
        )

    def test_resolved_propagation_refuses_changed_descendants(self):
        stack = native_stack()
        self.start(stack)
        resolver_state = MODULE.stack_propagation_state_path(
            self.stack_state, 5, "lower1"
        )
        MODULE.save_state(
            resolver_state,
            {
                "status": "resolved",
                "members_before": [dict(member) for member in stack["members"][1:]],
            },
        )
        moved = native_stack(
            heads={5: "lower1", 7: "middle-external", 9: "upper1"}
        )
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=moved
        ), mock.patch.object(
            MODULE, "conflict_resolver_script", return_value=self.resolver
        ), mock.patch.object(MODULE, "run") as run:
            result = call(
                "stack-propagate",
                "--state",
                str(self.stack_state),
                "--fixed-pr",
                "5",
                "--expected-head",
                "lower1",
            )
        run.assert_not_called()
        self.assertEqual("stopped", result["result"])
        self.assertEqual("propagation_snapshot_changed", result["reason"])

    def test_conflicted_propagation_waits_for_conflict_resolution(self):
        stack = native_stack()
        self.start(stack)
        script = self.root / "pr_conflict_resolver.py"
        script.write_text("# test", encoding="utf-8")
        resolver_state = MODULE.stack_propagation_state_path(
            self.stack_state, 5, "lower1"
        )
        MODULE.save_state(
            resolver_state,
            {
                "status": "conflicted",
                "detail": "app.py conflicts",
                "cascade": {
                    "current_index": 0,
                    "plan": [{"number": 7}],
                },
            },
        )
        process = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"result": "conflicted", "detail": "app.py conflicts"}),
            stderr="",
        )
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=stack
        ), mock.patch.object(
            MODULE, "conflict_resolver_script", return_value=script
        ), mock.patch.object(MODULE, "run", return_value=process):
            result = call(
                "stack-propagate",
                "--state",
                str(self.stack_state),
                "--fixed-pr",
                "5",
                "--expected-head",
                "lower1",
            )
        self.assertEqual("resolve_conflict", result["result"])
        self.assertEqual(7, result["blocked_member"])
        self.assertEqual(
            str(resolver_state),
            result["resolver_state"],
        )
        self.assertEqual("app.py conflicts", result["detail"])
        self.assertEqual("active", MODULE.load_stack_state(self.stack_state)["status"])
        self.assertEqual(
            7,
            MODULE.load_stack_state(self.stack_state)["pending_conflict"][
                "blocked_member"
            ],
        )

        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=stack
        ):
            next_action = call("stack-next", "--state", str(self.stack_state))
        self.assertEqual("resolve_conflict", next_action["result"])
        self.assertEqual("conflict-resolver", next_action["next"])

    def test_formatting_checkpoint_keeps_propagation_resumable(self):
        stack = native_stack()
        self.start(stack)
        script = self.root / "pr_conflict_resolver.py"
        script.write_text("# test", encoding="utf-8")
        process = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "result": "formatting_required",
                    "detail": "format PR #7 before continuing",
                    "formatting_member": {
                        "number": 7,
                        "branch": "middle",
                        "index": 0,
                    },
                }
            ),
            stderr="",
        )
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=stack
        ), mock.patch.object(
            MODULE, "conflict_resolver_script", return_value=script
        ), mock.patch.object(MODULE, "run", return_value=process):
            result = call(
                "stack-propagate",
                "--state",
                str(self.stack_state),
                "--fixed-pr",
                "5",
                "--expected-head",
                "lower1",
            )
        self.assertEqual("format", result["result"])
        self.assertEqual(5, result["fixed_pr"])
        self.assertEqual(7, result["formatting_member"]["number"])
        saved = MODULE.load_stack_state(self.stack_state)
        self.assertEqual("active", saved["status"])
        self.assertEqual(5, saved["pending_format"]["fixed_pr"])
        self.assertEqual(
            str(MODULE.stack_propagation_state_path(self.stack_state, 5, "lower1")),
            saved["pending_format"]["resolver_state"],
        )

    def test_stack_next_returns_the_pending_formatting_action(self):
        stack = native_stack()
        self.start(stack)
        state = MODULE.load_stack_state(self.stack_state)
        state["pending_format"] = {
            "fixed_pr": 5,
            "expected_head": "lower1",
            "resolver_state": str(
                MODULE.stack_propagation_state_path(self.stack_state, 5, "lower1")
            ),
            "formatting_member": {"number": 7, "branch": "middle", "index": 0},
        }
        MODULE.save_state(self.stack_state, state)
        result = self.next(stack)
        self.assertEqual("format", result["result"])
        self.assertEqual("stack-format", result["next"])
        self.assertEqual(7, result["formatting_member"]["number"])

    def test_stack_format_resumes_propagation_after_the_last_checkpoint(self):
        stack = native_stack()
        self.start(stack)
        resolver_state = MODULE.stack_propagation_state_path(
            self.stack_state, 5, "lower1"
        )
        MODULE.save_state(
            resolver_state,
            {"status": "formatting", "operation": "descendant_propagation"},
        )
        state = MODULE.load_stack_state(self.stack_state)
        state["pending_format"] = {
            "fixed_pr": 5,
            "expected_head": "lower1",
            "resolver_state": str(resolver_state),
            "formatting_member": {"number": 7, "branch": "middle", "index": 0},
        }
        MODULE.save_state(self.stack_state, state)
        process = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "result": "resolved",
                    "state": str(resolver_state),
                    "next": "descendant-propagate",
                }
            ),
            stderr="",
        )
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=stack
        ), mock.patch.object(
            MODULE, "conflict_resolver_script", return_value=self.resolver
        ), mock.patch.object(MODULE, "run", return_value=process) as run:
            result = call(
                "stack-format",
                "--state",
                str(self.stack_state),
                "--no-format",
            )
        self.assertEqual("formatted", result["result"])
        self.assertEqual("stack-next", result["next"])
        self.assertNotIn("pending_format", MODULE.load_stack_state(self.stack_state))
        command = run.call_args.args[0]
        self.assertIn("stack-format", command)
        self.assertIn("--no-format", command)
        self.assertIn(str(resolver_state), command)

    def test_stack_format_keeps_a_later_checkpoint_active(self):
        stack = native_stack()
        self.start(stack)
        resolver_state = MODULE.stack_propagation_state_path(
            self.stack_state, 5, "lower1"
        )
        MODULE.save_state(
            resolver_state,
            {"status": "formatting", "operation": "descendant_propagation"},
        )
        state = MODULE.load_stack_state(self.stack_state)
        state["pending_format"] = {
            "fixed_pr": 5,
            "expected_head": "lower1",
            "resolver_state": str(resolver_state),
            "formatting_member": {"number": 7, "branch": "middle", "index": 0},
        }
        MODULE.save_state(self.stack_state, state)
        process = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "result": "formatting_required",
                    "formatting_member": {
                        "number": 9,
                        "branch": "upper",
                        "index": 1,
                    },
                    "next": "stack-format",
                }
            ),
            stderr="",
        )
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=stack
        ), mock.patch.object(
            MODULE, "conflict_resolver_script", return_value=self.resolver
        ), mock.patch.object(MODULE, "run", return_value=process):
            result = call(
                "stack-format",
                "--state",
                str(self.stack_state),
                "--no-format",
            )
        self.assertEqual("format", result["result"])
        self.assertEqual(9, result["formatting_member"]["number"])
        self.assertEqual("active", MODULE.load_stack_state(self.stack_state)["status"])

    def test_stack_format_routes_a_later_conflict_to_the_resolver(self):
        stack = native_stack()
        self.start(stack)
        resolver_state = MODULE.stack_propagation_state_path(
            self.stack_state, 5, "lower1"
        )
        MODULE.save_state(
            resolver_state,
            {
                "status": "conflicted",
                "detail": "app.py conflicts after formatting",
                "cascade": {
                    "current_index": 0,
                    "plan": [{"number": 7}],
                },
            },
        )
        state = MODULE.load_stack_state(self.stack_state)
        state["pending_format"] = {
            "fixed_pr": 5,
            "expected_head": "lower1",
            "resolver_state": str(resolver_state),
            "formatting_member": {"number": 7, "branch": "middle", "index": 0},
        }
        MODULE.save_state(self.stack_state, state)
        process = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "result": "conflicted",
                    "detail": "app.py conflicts after formatting",
                }
            ),
            stderr="",
        )
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=stack
        ), mock.patch.object(
            MODULE, "conflict_resolver_script", return_value=self.resolver
        ), mock.patch.object(MODULE, "run", return_value=process):
            result = call(
                "stack-format",
                "--state",
                str(self.stack_state),
                "--no-format",
            )
        self.assertEqual("resolve_conflict", result["result"])
        self.assertEqual(7, result["blocked_member"])
        self.assertEqual("conflict-resolver", result["next"])
        saved = MODULE.load_stack_state(self.stack_state)
        self.assertEqual("active", saved["status"])
        self.assertNotIn("pending_format", saved)
        self.assertEqual(7, saved["pending_conflict"]["blocked_member"])
        self.assertEqual(
            "app.py conflicts after formatting",
            saved["pending_conflict"]["detail"],
        )

    def test_stack_format_has_no_local_formatter_execution_path(self):
        parser = MODULE.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "stack-format",
                    "--state",
                    str(self.stack_state),
                    "--format-command",
                    "gradlew.bat",
                    "spotlessApply",
                ]
            )

    def test_success_without_containment_stops_instead_of_retrying_forever(self):
        stack = native_stack()
        self.start(stack)
        process = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"result": "published", "members_published": [{"number": 7}]}
            ),
            stderr="",
        )
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "read_native_stack", return_value=stack
        ), mock.patch.object(
            MODULE, "conflict_resolver_script", return_value=self.resolver
        ), mock.patch.object(
            MODULE, "commit_contains", return_value=False
        ), mock.patch.object(
            MODULE, "PROPAGATION_CONTAINMENT_RETRY_DELAYS", ()
        ), mock.patch.object(MODULE, "run", return_value=process):
            result = call(
                "stack-propagate",
                "--state",
                str(self.stack_state),
                "--fixed-pr",
                "5",
                "--expected-head",
                "lower1",
            )
        self.assertEqual("stopped", result["result"])
        self.assertEqual("propagation_did_not_contain", result["reason"])

    def test_topology_change_is_recorded_as_a_safe_stop(self):
        stack = native_stack()
        self.start(stack)
        changed = native_stack(
            branches={
                5: ("lower", "main"),
                7: ("middle", "main"),
                9: ("upper", "middle"),
            }
        )
        result = self.next(changed)
        self.assertEqual("stopped", result["result"])
        self.assertEqual("topology_changed", result["reason"])

    def test_an_active_member_closing_stops_the_run(self):
        stack = native_stack()
        self.start(stack)
        stack["members"][0]["state"] = "MERGED"

        result = self.next(stack)

        self.assertEqual("stopped", result["result"])
        self.assertEqual("topology_changed", result["reason"])
        self.assertIn("member #5 is no longer open", result["detail"])

    def test_completed_stack_with_no_check_member_is_not_green(self):
        stack = native_stack()
        self.start(stack)
        state = MODULE.load_stack_state(self.stack_state)
        state["cursor"] = len(state["members"])
        for member in state["members"]:
            member["ci_status"] = "clear"
            member["clean_at_head_sha"] = member["head_sha"]
            member["stage_outcome"] = (
                "skipped" if member["number"] == 7 else "cleared"
            )
        MODULE.save_state(self.stack_state, state)
        result = self.next(stack)
        self.assertEqual("complete", result["result"])
        self.assertEqual("skipped", result["outcome"])
        self.assertEqual([7], result["skipped_members"])

    def test_stopping_a_completed_stack_clears_its_outcome(self):
        stack = native_stack()
        self.start(stack)
        state = MODULE.load_stack_state(self.stack_state)
        state["status"] = "complete"
        state["outcome"] = "green"
        MODULE.save_state(self.stack_state, state)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            MODULE.stack_stop(
                self.stack_state,
                state,
                "topology_changed",
                "the stack changed",
            )
        saved = MODULE.load_stack_state(self.stack_state)
        self.assertEqual("stopped", saved["status"])
        self.assertIsNone(saved["outcome"])

    def test_parser_exposes_all_stack_coordination_commands(self):
        parser = MODULE.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        self.assertTrue(
            {
                "stack-start",
                "stack-next",
                "stack-record",
                "stack-propagate",
                "stack-format",
                "stack-status",
                "stack-cleanup",
            }.issubset(subparsers.choices)
        )

    def test_parser_requires_an_explicit_no_format_declaration(self):
        args = run_arguments(
            "stack-format",
            "--state",
            "state.json",
            "--no-format",
        )
        self.assertTrue(args.no_format)


class LocalValidationRecordTest(unittest.TestCase):
    """The record is what makes the push requirement falsifiable.

    Reading a stage's own state afterwards has to say whether it validated,
    skipped, or claimed nothing at all, because inferring that from the checks
    that fail later is exactly the guessing this replaced.
    """

    def entry(self, head="head1", **overrides):
        args = SimpleNamespace(validated=None, rewrote=None, not_validated=None)
        for key, value in overrides.items():
            setattr(args, key, value)
        return MODULE.local_validation_entry(args, head)

    def test_records_the_commands_that_ran_and_the_head_they_covered(self):
        entry = self.entry(validated=["check one", "check two"])
        self.assertEqual("passed", entry["status"])
        self.assertEqual(["check one", "check two"], entry["commands"])
        self.assertEqual([], entry["rewrote"])
        self.assertEqual("head1", entry["head_sha"])

    def test_separates_the_commands_that_rewrote_files(self):
        """A command that ran clean and one that changed files differ.

        Only the second has anything that must reach the commits being pushed.
        """
        entry = self.entry(validated=["check one"], rewrote=["check one"])
        self.assertEqual(["check one"], entry["rewrote"])
        self.assertEqual(["check one"], entry["commands"])

    def test_a_rewriting_command_counts_as_one_that_ran(self):
        """Naming a command as rewriting implies it ran.

        Folding that in keeps a malformed claim from reaching the state as a
        contradiction, and keeps it from becoming a reason to refuse.
        """
        entry = self.entry(rewrote=["check one"])
        self.assertEqual("passed", entry["status"])
        self.assertEqual(["check one"], entry["commands"])
        self.assertEqual(["check one"], entry["rewrote"])

    def test_records_the_reason_when_nothing_covering_ran(self):
        entry = self.entry(not_validated="no narrow command exists here")
        self.assertEqual("skipped", entry["status"])
        self.assertEqual("no narrow command exists here", entry["reason"])
        self.assertNotIn("commands", entry)

    def test_records_that_the_publication_claimed_nothing(self):
        """This is the value that shows the requirement being ignored.

        A run that says neither thing must be distinguishable from one that
        deliberately skipped, or a live run proves nothing either way.
        """
        self.assertEqual("unreported", self.entry()["status"])

    def test_blank_claims_are_treated_as_no_claim(self):
        entry = self.entry(validated=["  "], not_validated="   ")
        self.assertEqual("unreported", entry["status"])


class DetachedHeadTargetTest(unittest.TestCase):
    """A refusal that names no correction is a dead end for its caller.

    The resolver is right to refuse, because a commit can belong to more than
    one pull request and no tie-break belongs here. What it owes the caller is
    the one thing that gets them past it.
    """

    def test_the_refusal_names_the_correction_and_not_only_the_fault(self):
        with mock.patch.object(MODULE, "git", return_value=""):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.current_pr_target(Path("repo"))
        message = str(error.exception)
        self.assertIn("detached HEAD", message)
        self.assertIn(
            "pass the pull request explicitly as a URL or owner/repo#number",
            message,
        )


class PreflightHelpTest(unittest.TestCase):
    """`--help` is read by a caller building a call, not one recovering from it.

    An agent constructing a `preflight` invocation reads this line first. A hint
    that still promises the checked-out branch's pull request sends it to a
    resolver a detached worktree cannot satisfy, and the refusal's correction
    then arrives only after the launch it wasted.
    """

    def test_the_target_help_repeats_the_agent_file_hint(self):
        """Deriving the clause keeps one sentence across both surfaces.

        The agent file's own guard fixes what that clause says; this one stops
        the two from drifting apart.
        """
        hint = re.search(
            r'^argument-hint: "(.+)"$', AGENT.read_text(encoding="utf-8"), re.M
        )
        self.assertIsNotNone(hint)
        clause = hint.group(1).split("; ", 1)[1]
        subparsers = next(
            action
            for action in MODULE.build_parser()._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        target = next(
            action
            for action in subparsers.choices["preflight"]._actions
            if action.dest == "target"
        )
        self.assertTrue(
            target.help.endswith(f"; {clause}"),
            f"preflight target help {target.help!r} does not end with {clause!r}",
        )


if __name__ == "__main__":
    unittest.main()
