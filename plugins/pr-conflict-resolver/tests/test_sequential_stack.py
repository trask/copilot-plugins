import copy
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import test_pr_conflict_resolver as existing


MODULE = existing.MODULE
CLOUD = existing.CLOUD_MODULE


class ReplayTaskBaseTest(unittest.TestCase):
    def test_creation_and_collection_share_the_policy_10_task_base(self):
        for strategy, base_key in (
            ("merge", "head_sha"),
            ("rebase", "base_sha"),
        ):
            with self.subTest(strategy=strategy):
                request = existing.ManagedTaskPromptTest().minimal_request()
                request.update(policy=CLOUD.POLICY, strategy=strategy)
                request["request_sha256"] = CLOUD.request_digest(request)
                options = CLOUD.Options(
                    strategy, request["model"], request["pull_request"]["url"],
                    Path("request.json"), Path("prompt.txt"), Path("result.json"),
                    request, "Resolve the frozen conflict.",
                )
                snapshot = CLOUD.LocalSnapshot(
                    Path.cwd(), Path.cwd(), request["repository"], "origin",
                    "", request["pull_request"]["head_sha"], "", None,
                )
                task = existing.MinimalConflictContractTest().task(request)
                expected_base = request["pull_request"][base_key]
                task["artifacts"][0]["data"]["base_ref"] = expected_base
                task["sessions"][0]["base_ref"] = expected_base
                with mock.patch.object(CLOUD, "api_json", return_value=task) as api:
                    created = CLOUD.start_task(mock.sentinel.runner, snapshot, options)
                self.assertEqual(expected_base, api.call_args.args[-1]["base_ref"])
                self.assertEqual(
                    "copilot/generated-task",
                    CLOUD.discover_minimal_artifact_ref(created, request).ref,
                )
                prompt = api.call_args.args[-1]["prompt"]
                self.assertEqual(
                    strategy == "rebase",
                    "The task branch starts at the exact replay base" in prompt,
                )

    def test_controller_keeps_source_identity_separate_from_rebase_task_base(self):
        fixture = existing.ManagedConflictCoordinatorTest()
        request = fixture.request()
        request["strategy"] = "rebase"
        request["request_sha256"] = MODULE.request_digest(request)
        result = fixture.success_result(request)
        result["task"].update(
            base_ref=request["pull_request"]["base_sha"],
            base_sha=request["pull_request"]["base_sha"],
        )
        result["generated"]["artifact"]["attribution"] = {
            "task_id": result["task"]["id"], "creator_id": 218610, "creator_login": "trask",
        }
        MODULE.validate_conflict_result_identity(result, request)
        result["task"].update(
            base_ref=request["pull_request"]["head_sha"],
            base_sha=request["pull_request"]["head_sha"],
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "base does not match"):
            MODULE.validate_conflict_result_identity(result, request)


class SequentialStackTest(unittest.TestCase):
    lower_message = (
            "Lower one\n\nCo-authored-by: Copilot App "
            "<223556219+Copilot@users.noreply.github.com>"
        )

    @staticmethod
    def git_at(root, *args, input=None, env=None):
        process = subprocess.run(
            ["git", "-C", str(root), *args],
            input=input.encode("utf-8") if isinstance(input, str) else input,
            capture_output=True,
            check=True,
            env=dict(os.environ) if env is None else env,
            **MODULE.windows_no_window_options(),
        )
        return process.stdout.decode("utf-8").strip()

    @classmethod
    def commit_at(cls, directory, root, parent, subject, path, content):
        index = directory / "temporary-index"
        index.unlink(missing_ok=True)
        environment = dict(os.environ)
        environment["GIT_INDEX_FILE"] = str(index)
        cls.git_at(root, "read-tree", parent if parent else "--empty", env=environment)
        blob = cls.git_at(root, "hash-object", "-w", "--stdin", input=content)
        cls.git_at(
            root,
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{blob},{path}",
            env=environment,
        )
        tree = cls.git_at(root, "write-tree", env=environment)
        return cls.git_at(
            root,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit-tree",
            tree,
            *(["-p", parent] if parent else []),
            input=subject + "\n",
        )

    @classmethod
    def setUpClass(cls):
        cls.template = tempfile.TemporaryDirectory()
        cls.template_directory = Path(cls.template.name).resolve()
        cls.template_root = cls.template_directory / "repo"
        cls.template_root.mkdir()
        cls.template_remote = cls.template_directory / "remote.git"
        cls.git_at(cls.template_root, "init", "--quiet")
        cls.git_at(cls.template_root, "init", "--bare", "--quiet", str(cls.template_remote))
        cls.git_at(cls.template_root, "remote", "add", "origin", str(cls.template_remote))
        cls.seed = cls.commit_at(
            cls.template_directory, cls.template_root, None, "Seed", "app.py", "seed\n"
        )
        cls.lower1 = cls.commit_at(
            cls.template_directory,
            cls.template_root,
            cls.seed,
            cls.lower_message,
            "app.py",
            "seed\none\n",
        )
        cls.lower = cls.commit_at(
            cls.template_directory,
            cls.template_root,
            cls.lower1,
            "Lower two",
            "app.py",
            "seed\none\ntwo\n",
        )
        cls.upper = cls.commit_at(
            cls.template_directory,
            cls.template_root,
            cls.lower,
            "Upper",
            "upper.py",
            "upper\n",
        )
        cls.trunk = cls.commit_at(
            cls.template_directory,
            cls.template_root,
            cls.seed,
            "Trunk",
            "app.py",
            "seed\ntrunk\n",
        )
        cls.new_lower1 = cls.commit_at(
            cls.template_directory,
            cls.template_root,
            cls.trunk,
            cls.lower_message,
            "app.py",
            "seed\ntrunk\none\n",
        )
        cls.new_lower2 = cls.commit_at(
            cls.template_directory,
            cls.template_root,
            cls.new_lower1,
            "Lower two",
            "app.py",
            "seed\ntrunk\none\ntwo\n",
        )
        cls.new_lower = cls.commit_at(
            cls.template_directory,
            cls.template_root,
            cls.new_lower2,
            "Focused fix",
            "app.py",
            "seed\ntrunk\none\ntwo\nfix\n",
        )
        cls.new_upper = cls.commit_at(
            cls.template_directory,
            cls.template_root,
            cls.new_lower,
            "Upper",
            "upper.py",
            "upper\n",
        )
        cls.report = cls.commit_at(
            cls.template_directory,
            cls.template_root,
            cls.new_upper,
            "Optional notes",
            CLOUD.OUTPUT_REPORT_PATH,
            "anything\n",
        )
        cls.git_at(cls.template_root, "checkout", "--quiet", "--detach", cls.lower)
        cls.branches = {
            "copilot/lower-task": cls.new_lower,
            "copilot/upper-task": cls.report,
        }
        for branch, tip in cls.branches.items():
            cls.git_at(
                cls.template_root,
                "push",
                "--quiet",
                "origin",
                f"{tip}:refs/heads/{branch}",
            )
        cls.member_identities = {
            sha: MODULE.commit_identity(cls.template_root, sha, linear=True)
            for sha in (cls.lower1, cls.lower, cls.upper)
        }

    @classmethod
    def tearDownClass(cls):
        cls.template.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "repo"
        self.remote = self.directory / "remote.git"
        shutil.copytree(self.template_root, self.root)
        shutil.copytree(self.template_remote, self.remote)
        self.run_git("remote", "set-url", "origin", str(self.remote))
        for name in (
            "seed",
            "lower1",
            "lower",
            "upper",
            "trunk",
            "new_lower1",
            "new_lower2",
            "new_lower",
            "new_upper",
            "report",
        ):
            setattr(self, name, getattr(type(self), name))
        self.branches = dict(type(self).branches)
        request = existing.ManagedTaskPromptTest().minimal_request()
        request.update(
            policy=CLOUD.POLICY,
            strategy="native-stack",
            head_commits=[],
            merge_base=self.seed,
            allowed_paths=["app.py", "upper.py"],
        )
        request["pull_request"].update(
            number=6,
            url="https://github.com/owner/repo/pull/6",
            head_ref="lower",
            head_sha=self.lower,
            base_ref="main",
            base_sha=self.trunk,
        )
        members = []
        for number, head, base, merge_base, commits in (
            (6, self.lower, self.trunk, self.seed, [self.lower1, self.lower]),
            (7, self.upper, self.lower, self.lower, [self.upper]),
        ):
            members.append({
                "pr_number": number,
                "repository": "owner/repo",
                "head_ref": "lower" if number == 6 else "upper",
                "head_sha": head,
                "direct_base_ref": "main" if number == 6 else "lower",
                "direct_base_sha": base,
                "retained_base_sha": self.seed if number == 6 else base,
                "direct_merge_base": merge_base,
                "expected_new_parent": {
                    "role": "trunk" if number == 6 else "member:6",
                    "old_sha": base,
                },
                "old_commits": [
                    copy.deepcopy(type(self).member_identities[sha]) for sha in commits
                ],
                "sync_merges": [],
                "lease_sha": head,
            })
        request["native_stack"] = {
            "trunk": {"ref": "main", "sha": self.trunk},
            "members": members,
            "outside_dependents": [],
        }
        request["request_sha256"] = CLOUD.request_digest(request)
        self.request = request
        self.options = CLOUD.Options(
            "native-stack", request["model"], request["pull_request"]["url"],
            self.directory / "request.json", self.directory / "prompt.txt",
            self.directory / "result.json", request, "Resolve conflicts.",
        )
        self.snapshot = CLOUD.LocalSnapshot(
            self.root, self.directory, "owner/repo", "origin", "", self.lower, "", None,
        )
        self.result = CLOUD.Result(
            schema=CLOUD.RESULT_SCHEMA, policy=CLOUD.POLICY,
            model=request["model"], repository="owner/repo", strategy="native-stack",
            request_id=request["request_id"], request_sha256=request["request_sha256"],
            pull_request=request["pull_request"],
        )
        self.launched = []
        self.tasks = {}
        self.user = {"id": 218610, "login": "trask"}
        for patch in (
            mock.patch.object(CLOUD, "api_json", return_value=self.user),
            mock.patch.object(MODULE, "gh_json", side_effect=self.lifecycle),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def lifecycle(self, arguments):
        endpoint = arguments[-1]
        if endpoint == "user/218610":
            return self.user
        return self.tasks[endpoint.rsplit("/", 1)[-1]]

    def run_git(self, *args, input=None, env=None):
        return self.git_at(self.root, *args, input=input, env=env)

    def commit(self, parent, subject, path, content):
        index = self.directory / "temporary-index"
        index.unlink(missing_ok=True)
        environment = dict(os.environ)
        environment["GIT_INDEX_FILE"] = str(index)
        self.run_git("read-tree", parent if parent else "--empty", env=environment)
        blob = self.run_git("hash-object", "-w", "--stdin", input=content)
        self.run_git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=environment)
        tree = self.run_git("write-tree", env=environment)
        return self.run_git(
            "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit-tree", tree, *(["-p", parent] if parent else []),
            input=subject + "\n",
        )

    def start(self, _runner, _snapshot, options):
        self.launched.append(options)
        branch = "copilot/lower-task" if len(self.launched) == 1 else "copilot/upper-task"
        task = existing.MinimalConflictContractTest().task(options.request)
        task["id"] = f"task-{len(self.launched)}"
        task["head_ref"] = branch
        task["artifacts"][0]["data"]["head_ref"] = branch
        task["artifacts"][0]["data"]["base_ref"] = options.request["pull_request"]["base_sha"]
        task["sessions"][0]["head_ref"] = branch
        task["sessions"][0]["task_id"] = task["id"]
        task["sessions"][0]["base_ref"] = options.request["pull_request"]["base_sha"]
        task["creator"] = {"id": self.user["id"]}
        self.tasks[task["id"]] = copy.deepcopy(task)
        return task

    def execute(self, guard=None):
        with (
            mock.patch.object(CLOUD, "start_task", side_effect=self.start),
            mock.patch.object(CLOUD, "require_target_fresh", side_effect=guard),
            mock.patch.object(CLOUD, "local_snapshot", return_value=self.snapshot),
            mock.patch.object(CLOUD, "already_satisfied", return_value=False),
            mock.patch.object(CLOUD, "fetch_pinned_inputs"),
        ):
            CLOUD.execute(
                self.options, cwd=self.root, runner=subprocess.run,
                sleep=lambda _: None, result=self.result,
            )
        return self.result

    def test_whole_stack_uses_only_authoritative_branches_with_extra_fix_and_report(self):
        result = self.execute()
        self.assertEqual("success", result.status)
        self.assertEqual(2, len(self.launched))
        self.assertEqual(self.trunk, self.launched[0].request["pull_request"]["base_sha"])
        self.assertEqual(self.new_lower, self.launched[1].request["pull_request"]["base_sha"])
        self.assertEqual(self.lower, self.launched[0].request["pull_request"]["head_sha"])
        self.assertEqual(self.upper, self.launched[1].request["pull_request"]["head_sha"])
        self.assertEqual(
            [self.trunk, self.new_lower],
            [item["task"]["base_sha"] for item in result.artifact["members"]],
        )
        self.assertEqual(self.new_lower, result.task_base_sha)
        self.assertEqual([self.new_lower], result.code_refs[0]["fix_commits"])
        self.assertEqual([], result.code_refs[1]["fix_commits"])
        self.assertEqual(self.new_upper, result.code_refs[1]["new_sha"])
        self.assertEqual(self.report, result.artifact["head_sha"])
        self.assertEqual(
            ["copilot/lower-task", "copilot/upper-task"],
            [item["ref"] for item in result.code_refs],
        )
        refs, artifact = MODULE.validate_conflict_result_identity(result.as_dict(), self.request)
        MODULE.verify_quarantined_result(self.root, self.request, refs, artifact)
        with mock.patch.object(MODULE, "find_remote", return_value="origin"):
            command = MODULE.conflict_push_command(self.root, self.request, refs)
        self.assertIn("--atomic", command)
        self.assertIn(f"--force-with-lease=refs/heads/lower:{self.lower}", command)
        self.assertIn(f"--force-with-lease=refs/heads/upper:{self.upper}", command)
        self.assertEqual(
            [f"{self.new_lower}:refs/heads/lower", f"{self.new_upper}:refs/heads/upper"],
            command[-2:],
        )
        self.assertEqual(self.lower, self.run_git("rev-parse", "HEAD"))
        self.assertEqual("", self.run_git("status", "--porcelain"))
        self.assertEqual(
            {"refs/heads/copilot/lower-task", "refs/heads/copilot/upper-task"},
            {
                line.split()[1]
                for line in self.run_git("ls-remote", "--heads", "origin").splitlines()
            },
        )
        for options in self.launched:
            self.assertIn("scoped linear companion fixes", options.prompt)
            self.assertNotIn("within the allowed paths", options.prompt)
            prompt = CLOUD.policy_prompt(options)
            self.assertIn("authoritative Agent Task branch", prompt)
            self.assertNotIn("copilot/conflict-", prompt)
            self.assertNotIn("generated_refs", prompt)
            self.assertIn(
                f"`git rev-parse HEAD` equals `{options.request['pull_request']['base_sha']}`",
                prompt,
            )
            self.assertIn(
                f"`git fetch --no-tags origin {options.request['pull_request']['head_sha']}`",
                prompt,
            )
            self.assertIn("Do not switch branches", prompt)
            self.assertIn("Cherry-pick each `head_commits` SHA", prompt)

    def publication_state(self):
        self.execute()
        MODULE.verify_quarantined_result(
            self.root, self.request, self.result.code_refs, self.result.artifact
        )
        for branch, sha in (("lower", self.lower), ("upper", self.upper)):
            self.run_git("push", "--quiet", "origin", f"{sha}:refs/heads/{branch}")
        return {
            "version": MODULE.STATE_VERSION,
            "agent_task": {
                "preflight": {"repository_root": str(self.root), "request": self.request},
                "code_refs": self.result.code_refs,
                "artifact": self.result.artifact,
                "status": "verified",
            },
        }

    def publication_heads(self, *_):
        return [
            self.run_git("ls-remote", "origin", f"refs/heads/{branch}").split()[0]
            for branch in ("lower", "upper")
        ]

    def test_publication_rejects_concurrent_writer_without_partial_push(self):
        state = self.publication_state()
        original_run = MODULE.run
        raced = self.commit(self.upper, "Concurrent writer", "upper.py", "other\n")

        def run(command, **kwargs):
            if "--atomic" in command:
                self.run_git("push", "--quiet", "origin", f"{raced}:refs/heads/upper")
            return original_run(command, **kwargs)

        with (
            mock.patch.object(MODULE, "require_live_conflict_guards"),
            mock.patch.object(MODULE, "remote_publication_heads", side_effect=self.publication_heads),
            mock.patch.object(MODULE, "find_remote", return_value="origin"),
            mock.patch.object(MODULE, "run", side_effect=run),
            self.assertRaisesRegex(MODULE.WorkflowError, "mixed or unexpected"),
        ):
            MODULE.publish_conflict_result(self.directory / "state.json", state)
        self.assertEqual([self.lower, raced], self.publication_heads())


class PipelineConflictEntryTest(unittest.TestCase):
    def arguments(self, root, state):
        return MODULE.build_parser().parse_args([
            "pipeline", "owner/repo#6", "--repo-root", str(root), "--state", str(state),
            "--pipeline-run", "run-1",
        ])

    def test_only_terminal_success_exits_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "repo"
            root.mkdir()
            for outcome, status, expected in (
                ("published", "completed", 0),
                ("mergeable", "completed", 0),
                ("published", "running", 1),
                ("invocation_abandoned", "interrupted", 1),
                ("task_creation_failed", "failed", 1),
            ):
                state = directory / f"{outcome}-{status}.json"

                def execute(args):
                    MODULE.save_state(state, {
                        "version": MODULE.STATE_VERSION,
                        "last_result": outcome, "agent_task": {"status": status},
                    })
                    self.assertEqual(1, args.pipeline_iteration)
                    self.assertEqual(3, args.pipeline_max_iterations)

                with self.subTest(outcome=outcome), mock.patch.object(
                    MODULE, "command_agent_task", side_effect=execute,
                ):
                    self.assertEqual(expected, MODULE.command_pipeline(self.arguments(root, state)))
                self.assertFalse(state.with_name(state.name + ".lock").exists())

    def test_existing_state_or_active_owner_cannot_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "repo"
            root.mkdir()
            state = directory / "state.json"
            MODULE.save_state(state, {
                "version": MODULE.STATE_VERSION, "agent_task": {"status": "interrupted"},
            })
            with mock.patch.object(MODULE, "command_agent_task") as execute:
                with self.assertRaisesRegex(MODULE.WorkflowError, "fresh invocation"):
                    MODULE.command_pipeline(self.arguments(root, state))
                state.unlink()
                state.with_name(state.name + ".lock").write_text("123", encoding="utf-8")
                with self.assertRaisesRegex(MODULE.WorkflowError, "another invocation"):
                    MODULE.command_pipeline(self.arguments(root, state))
                execute.assert_not_called()

    def test_state_created_before_lock_acquisition_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            root = directory / "repo"
            root.mkdir()
            state = directory / "state.json"
            contents = '{"version":1,"agent_task":{"status":"completed"}}'
            actual_open = Path.open

            def open_file(path, *args, **kwargs):
                if path == state.with_name(state.name + ".lock"):
                    state.write_text(contents, encoding="utf-8")
                return actual_open(path, *args, **kwargs)

            with (
                mock.patch.object(Path, "open", open_file),
                mock.patch.object(MODULE, "command_agent_task") as execute,
                self.assertRaisesRegex(MODULE.WorkflowError, "fresh invocation"),
            ):
                MODULE.command_pipeline(self.arguments(root, state))
            execute.assert_not_called()
            self.assertEqual(contents, state.read_text(encoding="utf-8"))

    def test_interrupt_is_terminal_and_retains_owned_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "repo"
            root.mkdir()
            state = directory / "state.json"

            def interrupt(_):
                MODULE.save_state(state, {
                    "version": MODULE.STATE_VERSION,
                    "agent_task": {"status": "running", "task_id": "task-1"},
                })
                raise KeyboardInterrupt

            with (
                mock.patch.object(MODULE, "command_agent_task", side_effect=interrupt),
                mock.patch.object(MODULE, "emit"),
            ):
                self.assertEqual(1, MODULE.command_pipeline(self.arguments(root, state)))
            saved = MODULE.load_state(state)
            self.assertEqual("interrupted", saved["agent_task"]["status"])
            self.assertEqual("task-1", saved["agent_task"]["task_id"])
            with self.assertRaisesRegex(MODULE.WorkflowError, "fresh invocation"):
                MODULE.command_pipeline(self.arguments(root, state))

    def test_normal_active_child_waits_until_completed(self):
        initial = {"id": "task-1", "state": "queued"}
        active = {"id": "task-1", "state": "in_progress"}
        final = {"id": "task-1", "state": "completed"}
        progress = CLOUD.Progress()
        sleep = mock.Mock()
        with mock.patch.object(CLOUD, "get_task", side_effect=[active, active, final]):
            result = CLOUD.monitor_task(
                mock.sentinel.runner, mock.sentinel.snapshot, initial, progress, sleep
            )
        self.assertEqual(final, result)
        self.assertEqual(3, sleep.call_count)
        self.assertEqual("completed", progress.task_state)

    def test_policy_seven_refuses_a_single_task_stack_boundary_guess(self):
        request = existing.ManagedTaskPromptTest().minimal_request()
        request.update(policy=CLOUD.POLICY, strategy="native-stack")
        with self.assertRaisesRegex(CLOUD.ConflictError, "one task cannot identify"):
            CLOUD.prove_generated_minimal(
                mock.sentinel.runner, mock.sentinel.snapshot, request, {},
            )

    def test_pipeline_native_scope_clears_mergeable_and_prepares_complete_conflict(self):
        for mergeable in ("MERGEABLE", "CONFLICTING"):
            metadata = existing.pr_metadata()
            metadata["mergeable"] = mergeable
            with self.subTest(mergeable=mergeable), mock.patch.multiple(
                MODULE,
                require_clean_worktree=mock.DEFAULT,
                require_no_integration_in_progress=mock.DEFAULT,
                live_mergeability=mock.DEFAULT,
                checkout_pr_branch=mock.DEFAULT,
                conflict_preflight_identity=mock.DEFAULT,
                find_remote=mock.DEFAULT,
                fetch_preflight_ref=mock.DEFAULT,
                stack_membership=mock.DEFAULT,
                stack_relations=mock.DEFAULT,
                repository_merge_methods=mock.DEFAULT,
                base_ref_tip=mock.DEFAULT,
                native_stack_member_history=mock.DEFAULT,
                merge_tree_conflicts=mock.DEFAULT,
                ordered_commits=mock.DEFAULT,
                commit_identity=mock.DEFAULT,
                external_stack_dependents=mock.DEFAULT,
                git=mock.DEFAULT,
            ) as calls:
                calls["live_mergeability"].return_value = metadata
                calls["stack_membership"].return_value = existing.native_stack_detection()
                calls["stack_relations"].return_value = existing.NO_RELATIONS
                calls["repository_merge_methods"].return_value = existing.ALL_MERGE_METHODS
                calls["base_ref_tip"].side_effect = lambda _repo, ref: {
                    "main": "base1",
                    "v143": "aaa",
                }[ref]
                calls["native_stack_member_history"].side_effect = (
                    lambda _root, **values: (
                        "base1",
                        [values["head"]],
                        [],
                    )
                )
                calls["merge_tree_conflicts"].return_value = []
                calls["ordered_commits"].return_value = []
                calls["commit_identity"].side_effect = lambda _root, sha, **_options: {
                    "sha": sha,
                    "subject": "Change",
                    "trailers": [],
                    "patch_sha256": "a" * 64,
                    "paths": ["file.txt"],
                }
                calls["external_stack_dependents"].return_value = []
                calls["git"].return_value = "merge-base"
                args = {
                    "requested_strategy": "auto",
                    "whole_stack": False,
                    "iteration_id": "run-1",
                    "iteration_number": 1,
                    "iteration_budget": 3,
                    "model": "gpt-5.6-sol",
                }
                result = MODULE.conflict_preflight(Path("repo"), {}, **args)
                if mergeable == "MERGEABLE":
                    self.assertTrue(result["already_mergeable"])
                else:
                    self.assertFalse(result["already_mergeable"])
                    self.assertEqual("native-stack", result["strategy"])
                    self.assertEqual(
                        [19483, 7],
                        [
                            member["pr_number"]
                            for member in result["request"]["native_stack"]["members"]
                        ],
                    )
