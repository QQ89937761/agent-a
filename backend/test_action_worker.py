"""Native hangs must be killable without leaving a desktop lease behind."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time
import threading
import tempfile
import unittest
from multiprocessing.shared_memory import SharedMemory
from unittest.mock import patch, Mock
from types import SimpleNamespace

from backend.modules.computer import system_control
from backend.modules.computer.action_worker import ActionWorker, describe, _release_inputs, _guard
from backend.core.process import hidden_subprocess_kwargs


class ActionWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def read_worker_line(self, process, diagnostics, timeout):
        try:
            return await asyncio.wait_for(process.stdout.readline(), timeout)
        except TimeoutError:
            diagnostics.seek(0)
            self.fail("desktop worker did not respond:\n" + diagnostics.read().decode("utf-8", errors="replace"))

    async def test_timeout_cancel_and_escape_kill_process_then_allow_new_owner(self):
        for reason in ("timeout", "cancel", "escape"):
            with self.subTest(reason=reason), patch.object(system_control, "ensure_listener"), patch.object(system_control, "stop_listener"):
                notices = []
                context = system_control.set_foreground_notice_callback(notices.append, on_escape=lambda: None)
                worker = ActionWorker()
                process = subprocess.Popen([sys.executable, "-u", "-c",
                    "import sys,time; sys.stdin.readline(); time.sleep(60)"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, **hidden_subprocess_kwargs())
                worker.process = process
                token = await system_control.acquire_control(.2)
                try:
                    with patch("backend.modules.computer.action_worker.describe", return_value={"kind": "test"}), patch(
                            "backend.modules.computer.action_worker.ActionWorker", return_value=worker), patch.object(system_control, "_IS_WINDOWS", True):
                        action = asyncio.create_task(system_control.run_controlled(token, lambda: None, timeout=.2 if reason == "timeout" else 3))
                        while token.worker is None:
                            await asyncio.sleep(.005)
                        if reason == "timeout":
                            with self.assertRaisesRegex(TimeoutError, "desktop_call_timeout"):
                                await action
                        else:
                            await asyncio.sleep(.02)
                            if reason == "escape":
                                system_control._escape_foreground(token)
                            action.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await action
                    self.assertIsNotNone(process.poll(), "old process must be dead before reacquiring")
                    deadline = time.monotonic() + 1
                    while system_control._CONTROL_LOCK.locked() and time.monotonic() < deadline:
                        await asyncio.sleep(.01)
                    self.assertFalse(system_control._CONTROL_LOCK.locked())
                    self.assertFalse(system_control.foreground_control_active())
                    self.assertEqual([True, False], notices)
                    if reason == "timeout":
                        self.assertFalse(token.session.revoked.is_set())
                        recovered = await system_control.acquire_control(.3)
                        self.assertIsNotNone(recovered)
                        self.assertIs(token.session, recovered.session)
                        self.assertEqual(42, await system_control.run_controlled(recovered, lambda: 42))
                        system_control.release_control(recovered, force=True)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "控制权已交还"):
                            await system_control.acquire_control(.1)
                finally:
                    worker.close()
                    system_control.reset_foreground_notice_callback(context)
                replacement = await system_control.acquire_control(.3)
                self.assertIsNotNone(replacement)
                self.assertFalse(system_control.release_control(token), "late cleanup must not release new owner")
                self.assertIs(system_control._CONTROL_OWNER, replacement)
                system_control.release_control(replacement, force=True)

    async def test_tool_error_closes_child_but_preserves_task_and_allows_recovery(self):
        with patch.object(system_control, "ensure_listener"), patch.object(system_control, "stop_listener"):
            context = system_control.set_foreground_notice_callback(None, on_escape=lambda: None)
            worker = ActionWorker()
            worker.process = subprocess.Popen([sys.executable, "-u", "-c",
                "import sys; sys.stdin.readline(); print('{\"error\":\"input_target_missing\"}',flush=True); sys.stdin.read()"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, **hidden_subprocess_kwargs())
            process = worker.process
            token = await system_control.acquire_control(.2)
            token.session.sends["test"] = (time.monotonic(), "unknown")
            try:
                with patch("backend.modules.computer.action_worker.describe", return_value={"kind": "test"}), patch(
                        "backend.modules.computer.action_worker.ActionWorker", return_value=worker), patch.object(system_control, "_IS_WINDOWS", True):
                    with self.assertRaisesRegex(RuntimeError, "input_target_missing"):
                        await system_control.run_controlled(token, lambda: None)
                self.assertIsNotNone(process.poll())
                self.assertFalse(token.session.revoked.is_set())
                replacement = await system_control.acquire_control(.2)
                self.assertIs(token.session, replacement.session)
                self.assertIn("test", replacement.session.sends)
                self.assertEqual(42, await system_control.run_controlled(replacement, lambda: 42))
                self.assertFalse(system_control.release_control(token), "old cleanup must not release recovered lease")
                self.assertIs(replacement, system_control._CONTROL_OWNER)
            finally:
                worker.close()
                system_control.reset_foreground_notice_callback(context)

    async def test_thread_timeout_invalidates_old_input_without_revoking_task(self):
        with patch.object(system_control, "ensure_listener"), patch.object(system_control, "stop_listener"):
            context = system_control.set_foreground_notice_callback(None, on_escape=lambda: None)
            token = await system_control.acquire_control(.2)
            finish = threading.Event()
            entered = threading.Event()
            def blocked():
                entered.set()
                finish.wait(3)
                system_control.check_foreground_permission()
            try:
                with self.assertRaisesRegex(TimeoutError, "desktop_call_timeout"):
                    await system_control.run_controlled(token, blocked, timeout=.1)
                self.assertTrue(entered.is_set())
                self.assertFalse(token.session.revoked.is_set())
                self.assertIsNone(await system_control.acquire_control(.1))
                old_worker = token.worker
                finish.set()
                with self.assertRaisesRegex(RuntimeError, "desktop_action_released"):
                    await old_worker
                replacement = await system_control.acquire_control(.3)
                self.assertIsNotNone(replacement)
                self.assertEqual(42, await system_control.run_controlled(replacement, lambda: 42))
            finally:
                finish.set()
                if token.worker is not None:
                    await asyncio.gather(token.worker, return_exceptions=True)
                system_control.reset_foreground_notice_callback(context)

    async def test_failed_child_cleanup_never_reuses_or_unlocks_cancelled_lease(self):
        with patch.object(system_control, "ensure_listener"), patch.object(system_control, "stop_listener"):
            context = system_control.set_foreground_notice_callback(None, on_escape=lambda: None)
            token = await system_control.acquire_control(.2)
            token.actions = Mock()
            token.actions.close.side_effect = RuntimeError("cleanup failed")
            try:
                with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                    system_control.release_control(token, force=True)
                self.assertFalse(token.session.revoked.is_set())
                self.assertTrue(system_control._CONTROL_LOCK.locked())
                self.assertIsNone(await system_control.acquire_control(.1))
                self.assertIsNone(await asyncio.to_thread(system_control.acquire_control_sync, .1))
                action = Mock()
                with self.assertRaisesRegex(RuntimeError, "token"):
                    await system_control.run_controlled(token, action)
                action.assert_not_called()
            finally:
                token.actions.close.side_effect = None
                system_control.reset_foreground_notice_callback(context)

    async def test_cancelled_thread_revokes_task_until_thread_exits(self):
        with patch.object(system_control, "ensure_listener"), patch.object(system_control, "stop_listener"):
            context = system_control.set_foreground_notice_callback(None, on_escape=lambda: None)
            token = await system_control.acquire_control(.2)
            entered, finish = threading.Event(), threading.Event()
            def blocked():
                entered.set()
                finish.wait(3)
                system_control.check_foreground_permission()
            task = asyncio.create_task(system_control.run_controlled(token, blocked))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(token.session.revoked.is_set())
                self.assertTrue(system_control._CONTROL_LOCK.locked())
                with self.assertRaisesRegex(RuntimeError, "控制权已交还"):
                    await system_control.acquire_control(.1)
            finally:
                finish.set()
                if token.worker is not None:
                    await asyncio.gather(token.worker, return_exceptions=True)
                system_control.reset_foreground_notice_callback(context)

    async def test_idle_child_does_not_allow_unlocking_a_live_thread_fallback(self):
        token = await system_control.acquire_control(.2, foreground=False)
        started, finish = threading.Event(), threading.Event()
        token.actions = ActionWorker()
        def blocked():
            started.set()
            finish.wait(3)
        task = asyncio.create_task(system_control.run_controlled(token, blocked, timeout=2))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            system_control.release_control(token, force=True)
            self.assertIsNone(await system_control.acquire_control(.1, foreground=False))
            finish.set()
            await task
            replacement = await system_control.acquire_control(.5, foreground=False)
            self.assertIsNotNone(replacement)
            system_control.release_control(replacement, force=True)
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_cancel_during_spawn_never_dispatches_late_input(self):
        started, finish = threading.Event(), threading.Event()
        process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, **hidden_subprocess_kwargs())
        worker = ActionWorker()
        def spawn(*args, **kwargs):
            started.set()
            finish.wait(3)
            return process
        try:
            with patch("backend.modules.computer.action_worker.subprocess.Popen", side_effect=spawn):
                task = asyncio.create_task(asyncio.to_thread(worker.execute, {}))
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                worker.close()
                finish.set()
                with self.assertRaisesRegex(RuntimeError, "revoked"):
                    await task
            self.assertIsNotNone(process.poll())
            self.assertIsNone(worker.process)
        finally:
            finish.set()
            if process.poll() is None:
                process.kill()
                process.wait()
            worker.close()

    @unittest.skipUnless(sys.platform == "win32", "Windows desktop entry")
    async def test_real_entry_rejects_code_and_exits_on_parent_eof(self):
        memory = SharedMemory(create=True, size=520)
        root = Path(__file__).resolve().parent.parent
        diagnostics = tempfile.TemporaryFile()
        program = """
import faulthandler, runpy, sys
class NoImageImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"PIL", "numpy", "cv2", "pyscreeze", "mouseinfo", "pymsgbox"}:
            raise RuntimeError("input worker must not load " + fullname)
sys.meta_path.insert(0, NoImageImports())
faulthandler.enable()
faulthandler.dump_traceback_later(10, repeat=False)
entry = sys.argv.pop(1)
runpy.run_path(entry, run_name="__main__")
"""
        process = await asyncio.create_subprocess_exec(sys.executable, "-u", "-c", program, str(root / "xiaoa_backend.py"),
            "--desktop-worker", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=diagnostics, **hidden_subprocess_kwargs())
        try:
            process.stdin.write((json.dumps({"memory": memory.name}) + "\n").encode())
            process.stdin.write((json.dumps({"action": {"kind": "eval", "name": "eval", "args": [], "kwargs": {}}}) + "\n").encode())
            await process.stdin.drain()
            response = json.loads(await self.read_worker_line(process, diagnostics, 15))
            self.assertEqual({"error": "Unsupported desktop action"}, response)
            # Empty write exercises the real input route without generating desktop input.
            process.stdin.write((json.dumps({"action": {"kind": "input", "name": "typewrite",
                "args": [""], "kwargs": {}}}) + "\n").encode())
            await process.stdin.drain()
            self.assertEqual({"result": None}, json.loads(await self.read_worker_line(process, diagnostics, 5)))
            process.stdin.close()
            await asyncio.wait_for(process.wait(), 3)
            self.assertEqual(0, process.returncode)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            diagnostics.close()
            memory.close()
            memory.unlink()

    @unittest.skipUnless(sys.platform == "win32", "Windows desktop entry")
    async def test_parent_eof_stops_worker_even_during_blocked_native_action(self):
        memory = SharedMemory(create=True, size=520)
        diagnostics = tempfile.TemporaryFile()
        program = ("import faulthandler,time; faulthandler.enable(); "
                   "faulthandler.dump_traceback_later(10, repeat=False); "
                   "from backend.modules.computer import action_worker as w; "
                   "w._execute=lambda *args: (print('entered',flush=True),time.sleep(60)); w.serve()")
        process = await asyncio.create_subprocess_exec(sys.executable, "-u", "-c", program,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=diagnostics, **hidden_subprocess_kwargs())
        try:
            process.stdin.write((json.dumps({"memory": memory.name}) + "\n" +
                                 json.dumps({"action": {}}) + "\n").encode())
            await process.stdin.drain()
            self.assertEqual(b"entered", (await self.read_worker_line(process, diagnostics, 15)).strip())
            process.stdin.close()
            await asyncio.wait_for(process.wait(), 3)
            self.assertEqual(0, process.returncode)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            diagnostics.close()
            memory.close()
            memory.unlink()

    def test_serialized_guard_rechecks_window_and_actual_control(self):
        descriptor = {"window": {"handle": 77, "title": "Window"}, "tool": "click_element",
                      "selected": {"name": "Target", "control_type": "ButtonControl", "automation_id": "id"},
                      "expect": {"automation_id": "id"}}
        with patch.dict(sys.modules, {"pygetwindow": SimpleNamespace(), "uiautomation": SimpleNamespace(),
                                     "win32gui": SimpleNamespace(GetForegroundWindow=lambda: 77)}), patch.object(
                system_control, "resolve_window", return_value=object()), patch(
                "backend.modules.computer.desktop_runtime.window_identity", return_value=descriptor["window"]) as identity:
            guard = _guard(descriptor)
            guard(SimpleNamespace(Name="Target", ControlTypeName="ButtonControl", AutomationId="id", IsEnabled=True, IsPassword=False))
            with self.assertRaisesRegex(RuntimeError, "control_changed"):
                guard(SimpleNamespace(Name="Wrong", ControlTypeName="ButtonControl", AutomationId="id", IsEnabled=True, IsPassword=False))
            identity.return_value = {"handle": 78}
            with self.assertRaisesRegex(RuntimeError, "window_changed"):
                guard()

    def test_cleanup_releases_only_child_recorded_keys_and_buttons(self):
        held = bytearray(520)
        held[65] = held[17] = held[512] = 1
        user32 = SimpleNamespace(keybd_event=Mock(), mouse_event=Mock())
        import ctypes
        with patch.object(ctypes, "windll", SimpleNamespace(user32=user32), create=True):
            _release_inputs(held)
        self.assertEqual([(17, 0, 2, 0), (65, 0, 2, 0)], [call.args for call in user32.keybd_event.call_args_list])
        user32.mouse_event.assert_called_once_with(4, 0, 0, 0, 0)
        self.assertFalse(any(held))

    def test_pyautogui_write_alias_is_also_isolated(self):
        def typewrite():
            pass
        typewrite.__module__ = "pyautogui"
        self.assertEqual("typewrite", describe(typewrite, ("text",), {})["name"])

    def test_worker_protocol_rejects_arbitrary_callable(self):
        self.assertIsNone(describe(eval, ("1",), {}))
        self.assertIsNone(describe(lambda: None, (), {}))
