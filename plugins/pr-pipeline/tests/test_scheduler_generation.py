import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "pipeline_common.py"
SPEC = importlib.util.spec_from_file_location("scheduler_generation_common", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class SchedulerGenerationTest(unittest.TestCase):
    def test_reused_unreadable_and_unrecorded_generations_fail_monitoring(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launch = root / "launch.json"
            for expected, observed, reason in (
                ({"creation_time": "1"}, {"creation_time": "2", "running": True}, "scheduler_generation_changed"),
                ({"creation_time": "1"}, OSError("access denied"), "scheduler_generation_unreadable"),
                (None, {"creation_time": "1", "running": True}, "scheduler_generation_unverified"),
            ):
                with self.subTest(reason=reason):
                    MODULE.write_json_atomically(launch, {"pid": 123, "process_identity": expected})
                    with (
                        mock.patch.object(MODULE, "IS_WINDOWS", True),
                        mock.patch.object(
                            MODULE, "windows_process_identity",
                            side_effect=observed if isinstance(observed, OSError) else None,
                            return_value=observed,
                        ),
                    ):
                        result = MODULE.watch_progress(
                            event_log=root / "events.jsonl", launch_path=launch,
                            observer_path=root / "observer.json", cursor=0, wait_seconds=1,
                        )
                    self.assertTrue(result["finished"])
                    self.assertEqual(reason, result["monitor_failure"])
                    self.assertNotIn("final_event", result)

    def test_terminal_journal_wins_a_race_with_generation_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launch = root / "launch.json"
            MODULE.write_json_atomically(launch, {"pid": 123, "process_identity": {"creation_time": "1"}})
            final = {"terminal": True, "final_event": {"result": "partial"}}
            with (
                mock.patch.object(MODULE, "IS_WINDOWS", True),
                mock.patch.object(MODULE, "windows_process_identity", return_value=None),
                mock.patch.object(MODULE, "read_progress_log", side_effect=[[], [final]]),
            ):
                result = MODULE.watch_progress(
                    event_log=root / "events.jsonl", launch_path=launch,
                    observer_path=root / "observer.json", cursor=0, wait_seconds=1,
                )
            self.assertTrue(result["finished"])
            self.assertNotIn("monitor_failure", result)
            self.assertEqual("partial", result["updates"][0]["final_event"]["result"])
