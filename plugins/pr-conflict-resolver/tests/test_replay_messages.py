import copy
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import test_pr_conflict_resolver as existing


CLOUD = existing.CLOUD_MODULE
MODULE = existing.MODULE
ATTRIBUTION = {"task_id": "task-1", "creator_id": 218610, "creator_login": "trask"}
LINE = b"Co-authored-by: trask <218610+trask@users.noreply.github.com>"


class ReplayMessageProofTest(unittest.TestCase):
    def verify_both(self, original, generated, *, accepted):
        old = {"sha": "a" * 40, "subject": "Subject", "trailers": ["Signed-off-by: Original"]}
        header = b"tree " + b"b" * 40 + b"\n\n"
        with (
            mock.patch.object(CLOUD, "commit_message_bytes", side_effect=[original, generated]),
            mock.patch.object(CLOUD, "commit_subject", return_value=old["subject"]),
            mock.patch.object(CLOUD, "commit_trailers", return_value=old["trailers"]),
            mock.patch.object(MODULE, "git_bytes", side_effect=[header + original, header + generated]),
            mock.patch.object(MODULE, "conflict_commit_subject", return_value=old["subject"]),
            mock.patch.object(MODULE, "conflict_commit_trailers", return_value=old["trailers"]),
        ):
            if accepted:
                CLOUD.preserved_replay_message(mock.sentinel.runner, Path.cwd(), old, "c" * 40, ATTRIBUTION)
                MODULE.verify_replay_message_bytes(Path.cwd(), old, "c" * 40, ATTRIBUTION)
            else:
                with self.assertRaisesRegex(CLOUD.ConflictError, "message bytes changed"):
                    CLOUD.preserved_replay_message(mock.sentinel.runner, Path.cwd(), old, "c" * 40, ATTRIBUTION)
                with self.assertRaisesRegex(MODULE.WorkflowError, "message bytes changed"):
                    MODULE.verify_replay_message_bytes(Path.cwd(), old, "c" * 40, ATTRIBUTION)

    def test_exact_bytes_and_one_blank_paragraph_appendix(self):
        for original in (
            b"Subject\n\nSigned-off-by: Original\n",
            "Subject\n\nBody caf\u00e9 \U0001f642\n\nSigned-off-by: Original\n".encode("utf-8"),
            b"Subject\r\n\r\nBody\r\n\r\nSigned-off-by: Original\r\n",
            b"Subject\n\nOpaque body \xff\n\nSigned-off-by: Original\n",
        ):
            for suffix in (b"", b"\n" + LINE + b"\n"):
                with self.subTest(original=original, suffix=suffix):
                    self.verify_both(original, original + suffix, accepted=True)

    def test_creator_already_present_accepts_exact_message_not_duplicate(self):
        original = b"Subject\n\n" + LINE + b"\n"
        self.verify_both(original, original, accepted=True)
        self.verify_both(original, original + b"\n" + LINE + b"\n", accepted=False)

    def test_no_message_normalization_or_arbitrary_appendices(self):
        original = b"Subject\n\nBody caf\xc3\xa9\n\nSigned-off-by: Original\n"
        for generated in (
            original.replace(b"Original", b"Changed"),
            original.replace(b"caf\xc3\xa9", b"cafe"),
            original.replace(b"\n", b"\r\n"),
            original.rstrip(b"\n"),
            original + b"\nArbitrary prose\n",
            original + b"\nSigned-off-by: trask\n",
            original + b"\nCo-authored-by: other <123+other@users.noreply.github.com>\n",
            original + b"\n" + LINE + b"\n\n" + LINE + b"\n",
            original + b"\n" + LINE + b"\nExtra prose\n",
            original + b"\n" + LINE + b"\n\n",
            original + LINE + b"\n",
            b"Different subject\n" + original,
            b"Subject\n\nSigned-off-by: Original\n\nBody caf\xc3\xa9\n",
        ):
            with self.subTest(generated=generated):
                self.verify_both(original, generated, accepted=False)
        self.verify_both(original[:-1], original[:-1], accepted=True)
        self.verify_both(original[:-1], original[:-1] + b"\n" + LINE + b"\n", accepted=False)

    def test_raw_reader_is_binary_and_hides_windows_console(self):
        root = Path.cwd()
        message = b"Subject\r\n\r\nOpaque \xff\n"
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, b"tree abc\n\n" + message, b""))
        with (
            mock.patch.object(CLOUD, "stable_process_directory", return_value=root),
            mock.patch.object(CLOUD.os, "name", "nt"),
            mock.patch.object(CLOUD, "_creation_flags", return_value=0x08000000),
        ):
            self.assertEqual(message, CLOUD.commit_message_bytes(runner, root, "a" * 40))
        self.assertEqual(0x08000000, runner.call_args.kwargs["creationflags"])
        self.assertEqual(str(root), runner.call_args.kwargs["cwd"])
        self.assertNotIn("text", runner.call_args.kwargs)
        self.assertEqual(["git", "-C", str(root), "cat-file", "commit", "a" * 40], runner.call_args.args[0])

    def test_raw_reader_reports_read_and_format_failures(self):
        for output in (
            subprocess.CompletedProcess([], 1, b"", b"failure"),
            subprocess.CompletedProcess([], 0, b"malformed", b""),
            subprocess.CompletedProcess([], 0, "decoded text", ""),
        ):
            with self.subTest(output=output), self.assertRaises(CLOUD.ConflictError):
                CLOUD.commit_message_bytes(mock.Mock(return_value=output), Path.cwd(), "a" * 40)
        with self.assertRaisesRegex(CLOUD.ConflictError, "could not read"):
            CLOUD.commit_message_bytes(mock.Mock(side_effect=OSError("unavailable")), Path.cwd(), "a" * 40)

    def test_creator_identity_is_machine_owned_and_closed(self):
        snapshot = mock.Mock(control_root=Path.cwd())
        task = {"id": "task-1", "creator": {"id": 218610}}
        with mock.patch.object(CLOUD, "api_json", return_value={"id": 218610, "login": "trask"}) as api:
            self.assertEqual(ATTRIBUTION, CLOUD.task_attribution(mock.sentinel.runner, snapshot, task))
            self.assertEqual(("GET", "user/218610"), api.call_args.args[-2:])
        for creator in (None, {}, {"id": True}, {"id": "218610"}, {"id": -1}):
            with self.subTest(creator=creator), self.assertRaises(CLOUD.ConflictError):
                CLOUD.task_attribution(mock.sentinel.runner, snapshot, {**task, "creator": creator})
        for user in (None, {"id": True, "login": "trask"}, {"id": 123, "login": "trask"}, {"id": 218610, "login": "trask\n"}):
            with mock.patch.object(CLOUD, "api_json", return_value=user), self.subTest(user=user), self.assertRaises(CLOUD.ConflictError):
                CLOUD.task_attribution(mock.sentinel.runner, snapshot, task)

    def test_controller_rechecks_task_session_and_account(self):
        request = existing.ManagedTaskPromptTest().minimal_request()
        request.update(policy=CLOUD.SEQUENTIAL_POLICY, strategy="rebase")
        task = existing.MinimalConflictContractTest().task(request)
        task["id"] = task["sessions"][0]["task_id"] = "task-1"
        task["creator"] = {"id": 218610}
        task["sessions"][0]["base_ref"] = request["pull_request"]["base_sha"]
        task["artifacts"][0]["data"]["base_ref"] = request["pull_request"]["base_sha"]
        artifact = {"branch": "copilot/generated-task", "attribution": ATTRIBUTION}
        base = request["pull_request"]["base_sha"]
        with mock.patch.object(MODULE, "gh_json", side_effect=[task, {"id": 218610, "login": "trask"}]):
            self.assertEqual(ATTRIBUTION, MODULE.verify_replay_attribution(request, artifact, base))
        for mutate in (
            lambda value: value.update(state="in_progress"),
            lambda value: value.update(id="different"),
            lambda value: value.update(error="failed"),
            lambda value: value["creator"].update(id=123),
            lambda value: value["sessions"][0].update(model="different"),
            lambda value: value["sessions"][0].update(base_ref="0" * 40),
            lambda value: value["sessions"][0].update(head_ref="different"),
            lambda value: value["artifacts"][0]["data"].update(head_ref="different"),
        ):
            changed = copy.deepcopy(task)
            mutate(changed)
            with mock.patch.object(MODULE, "gh_json", return_value=changed), self.subTest(mutation=mutate), self.assertRaises(MODULE.WorkflowError):
                MODULE.verify_replay_attribution(request, artifact, base)
        for user in (None, {"id": 123, "login": "trask"}, {"id": 218610, "login": "different"}):
            with mock.patch.object(MODULE, "gh_json", side_effect=[task, user]), self.subTest(user=user), self.assertRaises(MODULE.WorkflowError):
                MODULE.verify_replay_attribution(request, artifact, base)
