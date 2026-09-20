import ctypes
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts" / "pipeline_common.py"
SPEC = importlib.util.spec_from_file_location("lifetime_common", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


@unittest.skipUnless(os.name == "nt", "Windows lifetime qualification")
class WindowsLifetimeTest(unittest.TestCase):
    def test_no_job_control(self):
        self.qualify(None)

    def test_permitted_breakaway_survives_private_parent_job(self):
        self.qualify(True)

    def test_denied_breakaway_starts_no_fallback_scheduler(self):
        self.qualify(False)

    def qualify(self, allow_breakaway):
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateEventW.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event_name = "Local\\copilot-lifetime-" + root.name
            event = kernel.CreateEventW(None, True, False, event_name)
            self.assertTrue(event)
            parent = None
            job = None
            child_handles = []
            try:
                bootstrap = root / "bootstrap.py"
                bootstrap.write_text(
                    "import ctypes, importlib.util, json, pathlib, sys, time\n"
                    "from ctypes import wintypes\n"
                    f"spec = importlib.util.spec_from_file_location('common', {str(SCRIPT)!r})\n"
                    "common = importlib.util.module_from_spec(spec)\n"
                    "sys.modules[spec.name] = common\n"
                    "spec.loader.exec_module(common)\n"
                    f"root = pathlib.Path({str(root)!r})\n"
                    "command = [sys.executable, '-c', 'import time; time.sleep(120)']\n"
                    "result = {}\n"
                    "try:\n"
                    "    child = common.start_detached(command, cwd=root, log_path=root/'scheduler.log')\n"
                    "    result['scheduler'] = child.launch_receipt\n"
                    "    (root/'receipt.json').write_text(json.dumps(result), encoding='utf-8')\n"
                    "    worker = common.start_background(command, cwd=root, log_path=root/'worker.log')\n"
                    "    result['worker'] = worker.launch_receipt\n"
                    "except BaseException as error:\n"
                    "    result['error'] = str(error)\n"
                    "(root/'receipt.json').write_text(json.dumps(result), encoding='utf-8')\n"
                    "kernel = ctypes.WinDLL('kernel32', use_last_error=True)\n"
                    "kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]\n"
                    "kernel.OpenEventW.restype = wintypes.HANDLE\n"
                    "kernel.SetEvent.argtypes = [wintypes.HANDLE]\n"
                    "kernel.SetEvent.restype = wintypes.BOOL\n"
                    f"event = kernel.OpenEventW(2, False, {event_name!r})\n"
                    "if not event or not kernel.SetEvent(event): raise ctypes.WinError(ctypes.get_last_error())\n"
                    "time.sleep(120)\n",
                    encoding="utf-8",
                )
                try:
                    parent = subprocess.Popen(
                        [sys.executable, str(bootstrap)], cwd=root,
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_BREAKAWAY_FROM_JOB | 4,
                    )
                except OSError as error:
                    if getattr(error, "winerror", None) == 5:
                        context = MODULE.windows_process_identity(os.getpid())
                        self.skipTest(
                            "host forbids isolated test-parent breakaway; lifetime qualification unavailable; "
                            f"parent_flags=0x09000004; winerror=5; test_host={json.dumps(context, sort_keys=True)}"
                        )
                    raise
                if allow_breakaway is not None:
                    job = MODULE.WindowsKillJob(parent.pid)
                    if allow_breakaway:
                        buffer = ctypes.create_string_buffer(256)
                        size = wintypes.DWORD()
                        self.assertTrue(kernel.QueryInformationJobObject(job._handle, 9, buffer, 256, ctypes.byref(size)))
                        flags = ctypes.cast(ctypes.byref(buffer, 16), ctypes.POINTER(wintypes.DWORD))
                        flags.contents.value |= 0x800
                        self.assertTrue(kernel.SetInformationJobObject(job._handle, 9, buffer, size.value))
                MODULE.resume_windows_process(parent.pid)
                self.assertEqual(0, kernel.WaitForSingleObject(event, 15000), "test parent never reported readiness")
                receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
                if allow_breakaway is False:
                    self.assertIn("breakaway", receipt["error"])
                    self.assertNotIn("scheduler", receipt)
                    self.assertNotIn("worker", receipt)
                    return
                for name in ("scheduler", "worker"):
                    identity = receipt[name]["process_identity"]
                    self.assertEqual(identity["creation_time"], MODULE.windows_process_identity(identity["pid"])["creation_time"])
                    handle = kernel.OpenProcess(0x00100001, False, identity["pid"])
                    self.assertTrue(handle)
                    child_handles.append(handle)
                    self.assertEqual(258, kernel.WaitForSingleObject(handle, 0))
                    self.assertTrue(receipt[name]["breakaway_accepted"])
                    self.assertFalse(receipt[name]["fallback_used"])
                self.assertFalse(receipt["scheduler"]["process_identity"]["in_job"])
                self.assertTrue(receipt["worker"]["scheduler_owned_job"])
                if job is not None:
                    job.close()
                    job = None
                else:
                    parent.terminate()
                parent.wait(timeout=10)
                self.assertEqual(258, kernel.WaitForSingleObject(child_handles[0], 250))
                self.assertEqual(0, kernel.WaitForSingleObject(child_handles[1], 10000))
            finally:
                if not child_handles and (root / "receipt.json").exists():
                    retained = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
                    for name in ("scheduler", "worker"):
                        identity = (retained.get(name) or {}).get("process_identity")
                        if identity:
                            handle = kernel.OpenProcess(0x00100001, False, identity["pid"])
                            if handle:
                                current = MODULE.windows_process_identity(identity["pid"])
                                if current and current["creation_time"] == identity["creation_time"]:
                                    child_handles.append(handle)
                                else:
                                    kernel.CloseHandle(handle)
                if job is not None:
                    job.close()
                if parent is not None:
                    if parent.poll() is None:
                        parent.terminate()
                    parent.wait(timeout=10)
                    if parent.stderr:
                        parent.stderr.close()
                for handle in child_handles:
                    if kernel.WaitForSingleObject(handle, 0) == 258:
                        self.assertTrue(kernel.TerminateProcess(handle, 1))
                    self.assertEqual(0, kernel.WaitForSingleObject(handle, 10000))
                    kernel.CloseHandle(handle)
                kernel.CloseHandle(event)
