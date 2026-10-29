"""Task-scoped ownership, physical Esc revocation and safe worker cleanup."""
import asyncio
import threading
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend.modules.computer import system_control as control
from backend.modules.computer.escape_key import physical_escape


class DesktopSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.notices = []
        self.stop = Mock()
        self.listener = patch.object(control, "ensure_listener")
        self.listener.start()
        self.context = control.set_foreground_notice_callback(self.notices.append, on_escape=self.stop)

    async def asyncTearDown(self):
        control.reset_foreground_notice_callback(self.context)
        self.listener.stop()

    async def test_plan_precondition_crosses_input_thread_and_cannot_leak_after_reset(self):
        owner = await control.acquire_control(.1)
        calls = []
        def reject():
            calls.append("guard")
            raise RuntimeError("changed before input")
        token = control.INPUT_PRECONDITION.set(reject)
        try:
            with self.assertRaisesRegex(RuntimeError, "changed before input"):
                await control.run_controlled(owner, lambda: calls.append("input"))
        finally:
            control.INPUT_PRECONDITION.reset(token)
        await control.run_controlled(owner, lambda: calls.append("ordinary input"))
        self.assertEqual(["guard", "ordinary input"], calls)

    async def test_owned_cad_pid_crosses_tool_threads_but_not_task_sessions(self):
        await asyncio.to_thread(control.mark_owned_application, "autocad", 42)
        self.assertTrue(control.owned_application("autocad"))
        self.assertEqual(42, await asyncio.to_thread(control.owned_application_pid, "autocad"))
        other = control.set_foreground_notice_callback(None, on_escape=lambda: None)
        try:
            self.assertFalse(control.owned_application("autocad"))
        finally:
            control.reset_foreground_notice_callback(other)
        self.assertEqual(42, control.owned_application_pid("autocad"))
        await asyncio.to_thread(control.clear_owned_application, "autocad")
        self.assertFalse(control.owned_application("autocad"))

    async def test_same_task_keeps_control_between_tools_until_finished(self):
        first = await control.acquire_control(.1)
        self.assertEqual(42, await control.run_controlled(first, lambda: 42))
        control.release_control(first)
        await asyncio.sleep(.01)  # screenshot/model wait between desktop actions
        self.assertTrue(control.foreground_control_active())
        self.assertEqual([True], self.notices)
        self.assertIs(first, await control.acquire_control(.1))
        self.assertIs(first, await asyncio.to_thread(control.acquire_control_sync, .1))

        async def other_task():
            token = control.set_foreground_notice_callback(None)
            try:
                return await control.acquire_control(.1)
            finally:
                control.reset_foreground_notice_callback(token)
        self.assertIsNone(await asyncio.create_task(other_task()))

    async def test_background_uia_lease_does_not_show_notice_or_keep_desktop(self):
        owner = await control.acquire_control(.1, foreground=False)
        self.assertEqual(42, await control.run_controlled(owner, lambda: 42))
        self.assertFalse(control.foreground_control_active())
        self.assertEqual([], self.notices)
        control._escape_foreground()
        self.stop.assert_not_called()
        control.release_control(owner)
        self.assertFalse(control._CONTROL_LOCK.locked())
        self.assertEqual([], self.notices)

    async def test_foreground_fallback_promotes_only_once_and_retains_control(self):
        owner = await control.acquire_control(.1, foreground=False)
        self.assertEqual([], self.notices)
        control.activate_foreground(owner)
        control.activate_foreground(owner)
        control.release_control(owner)
        self.assertTrue(control.foreground_control_active())
        self.assertEqual([True], self.notices)
        control._escape_foreground()
        self.assertEqual([True, False], self.notices)
        self.stop.assert_called_once()

    async def test_escape_stops_task_and_prevents_reacquisition(self):
        owner = await control.acquire_control(.1)
        control.release_control(owner)
        control._escape_foreground()
        self.assertEqual([True, False], self.notices)
        self.assertFalse(control.foreground_control_active())
        self.stop.assert_called_once()
        with self.assertRaisesRegex(RuntimeError, "控制权已交还"):
            await control.acquire_control(.1)
        with self.assertRaises(RuntimeError):
            await asyncio.to_thread(control.acquire_control_sync, .1)
        action = Mock()
        with self.assertRaises(RuntimeError):
            await control.run_controlled(owner, action)
        action.assert_not_called()
        control._escape_foreground()
        self.stop.assert_called_once()

    async def test_task_finish_releases_without_timer_and_new_task_can_take_over(self):
        owner = await control.acquire_control(.1)
        control.release_control(owner)
        control.reset_foreground_notice_callback(self.context)
        self.assertEqual([True, False], self.notices)
        self.assertFalse(control.foreground_control_active())
        self.context = control.set_foreground_notice_callback(self.notices.append, on_escape=self.stop)
        self.assertIsNotNone(await control.acquire_control(.1))
        self.assertEqual([True, False, True], self.notices)

    async def test_escape_during_input_keeps_notice_until_input_has_stopped(self):
        owner = await control.acquire_control(.1)
        started = threading.Event()
        finish = threading.Event()
        def input_step():
            started.set()
            finish.wait(2)
            control.check_foreground_permission()
        running = asyncio.create_task(control.run_controlled(owner, input_step))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            control._escape_foreground()
            self.assertEqual([True], self.notices)
            self.assertTrue(control.foreground_control_active())
            finish.set()
            with self.assertRaisesRegex(RuntimeError, "控制权已交还"):
                await running
            self.assertFalse(control.foreground_control_active())
            self.assertEqual([True, False], self.notices)
        finally:
            finish.set()
            await asyncio.gather(running, return_exceptions=True)

    async def test_listener_failure_does_not_take_control(self):
        with patch.object(control, "ensure_listener", side_effect=RuntimeError("Esc unavailable")):
            with self.assertRaises(RuntimeError):
                await control.acquire_control(.1)
        self.assertFalse(control.foreground_control_active())
        self.assertEqual([], self.notices)

    async def test_stopped_drag_and_hotkey_release_their_held_inputs(self):
        module = SimpleNamespace(platformModule=SimpleNamespace(_mouseUp=Mock(), _keyUp=Mock()), position=lambda: (5, 7))
        def action(*args, **kwargs):
            raise RuntimeError("input revoked")
        action.__module__ = "pyautogui"
        with patch.dict(sys.modules, {"pyautogui": module}):
            action.__name__ = "dragTo"
            with self.assertRaises(RuntimeError):
                control._run_controlled_input(action, (20, 30), {"button": "right"})
            module.platformModule._mouseUp.assert_called_once_with(5, 7, "right")
            action.__name__ = "hotkey"
            with self.assertRaises(RuntimeError):
                control._run_controlled_input(action, ("ctrl", "v"), {})
            self.assertEqual([(("v",), {}), (("ctrl",), {})], module.platformModule._keyUp.call_args_list)

    async def test_physical_escape_only(self):
        self.assertTrue(physical_escape(0, 0x100, 0x1B, 0))
        self.assertFalse(physical_escape(0, 0x100, 0x1B, 0x10))  # agent-generated Escape
        self.assertFalse(physical_escape(0, 0x101, 0x1B, 0))  # key-up
        self.assertFalse(physical_escape(-1, 0x100, 0x1B, 0))
        self.assertFalse(physical_escape(0, 0x100, 0x41, 0))
