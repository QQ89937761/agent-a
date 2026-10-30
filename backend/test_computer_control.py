import asyncio
import base64
import io
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from typing import get_type_hints

from PIL import Image, ImageGrab

from backend.core.agent import Agent, ConversationApproval, _looks_financial
from backend.core.config import AgentConfig
from backend.test_conversation_approval import OneCallLLM
from backend.core.tools import ToolRegistry
from backend.modules.computer import computer_module, system_control


class FakeWindow:
    def __init__(self, title: str, handle: int, rect=(0, 0, 100, 80)):
        self.title = title
        self._hWnd = handle
        self.left, self.top, self.width, self.height = rect
        self.isActive = False
        self.isMinimized = False
        self.calls = []

    def activate(self):
        self.calls.append("activate")

    def restore(self):
        self.calls.append("restore")

    def maximize(self):
        self.calls.append("maximize")


def fake_window_module(windows):
    module = types.ModuleType("pygetwindow")
    module.getAllWindows = lambda: windows
    return module


class FakePyAutoGUI(types.ModuleType):
    def failSafeCheck(self):
        pass

    def __init__(self):
        super().__init__("pyautogui")
        self.FAILSAFE = False
        self.KEYBOARD_KEYS = ["ctrl", "shift", "alt", "enter", "esc", "a", "s", "v", "tab", "f4", "f2", "win", "space", "home", "end", "delete", "backspace", "left", "right", "up", "down"]
        self.actions = []
        self.screenshots = []
        self.screenshot_error = None

    def size(self):
        return SimpleNamespace(width=100, height=100)

    def screenshot(self, region=None):
        self.screenshots.append(region)
        if self.screenshot_error:
            raise self.screenshot_error
        width, height = (region[2], region[3]) if region else (100, 100)
        return Image.new("RGB", (width, height), "white")

    def click(self, x, y, button="left", clicks=1, interval=0.0):
        self.actions.append(("click", x, y, button))

    def doubleClick(self, x, y, button="left"):
        self.actions.append(("double", x, y, button))

    def moveTo(self, x, y, duration=0):
        self.actions.append(("move", x, y, duration))

    def scroll(self, amount):
        self.actions.append(("scroll", amount))

    def dragTo(self, x, y, duration=0, button="left"):
        self.actions.append(("drag", x, y, duration, button))

    def hotkey(self, *keys):
        self.actions.append(("hotkey", *keys))

    def press(self, key):
        self.actions.append(("press", key))

    def write(self, text, interval=0):
        self.actions.append(("write", text, interval))


class FakeLLM:
    def __init__(self, coords=(10, 12)):
        self.coords = coords
        self.calls = 0

    async def vision(self, prompt, image_bytes):
        self.calls += 1
        if '"confirmed"' in prompt:
            return '{"confirmed": true}'
        return f'{{"x": {self.coords[0]}, "y": {self.coords[1]}}}'


class WindowHelpersTests(unittest.TestCase):
    def test_minimized_hidden_window_uses_normal_placement_geometry(self):
        window = FakeWindow("微信", 201, (-25600, -25600, 159, 27))
        windows = {201: window}
        gui = types.ModuleType("win32gui")
        gui.IsWindow = lambda h: True
        gui.IsWindowVisible = lambda h: False
        gui.IsIconic = lambda h: True
        gui.GetWindowText = lambda h: windows[h].title
        gui.GetClassName = lambda h: "WeChatMainWndForPC"
        gui.GetWindowRect = lambda h: (-25600, -25600, -25441, -25573)
        gui.GetWindowPlacement = lambda h: (0, 2, (0, 0, 0, 0), (0, 0), (370, 60, 1136, 686))
        gui.EnumWindows = lambda callback, arg: [callback(h, arg) for h in windows]
        gw = fake_window_module([])
        gw.Window = lambda h: windows[h]
        with patch.dict(sys.modules, {"win32gui": gui}):
            self.assertEqual([201], [item._hWnd for item in system_control.find_hidden_main_windows(gw, "微信")])
            info = system_control.window_info(window)
        self.assertTrue(info["minimized"])
        self.assertEqual({"x": 370, "y": 60, "width": 766, "height": 626}, info["rect"])

    def test_hidden_helper_titles_and_unknown_substrings_are_not_main_windows(self):
        windows = {1: FakeWindow("微信输入法-关于", 1, (0, 0, 800, 600)),
                   2: FakeWindow("微信 Workspace", 2, (0, 0, 800, 600)),
                   3: FakeWindow("微信 settings", 3, (0, 0, 800, 600))}
        gui = types.ModuleType("win32gui")
        gui.IsWindowVisible = lambda _: False
        gui.GetWindowText = lambda h: windows[h].title
        gui.GetClassName = lambda _: "Main"
        gui.GetWindowRect = lambda _: (0, 0, 800, 600)
        gui.EnumWindows = lambda callback, arg: [callback(h, arg) for h in windows]
        gw = fake_window_module([])
        gw.Window = lambda h: windows[h]
        with patch.dict(sys.modules, {"win32gui": gui}), patch.object(
                system_control, "_window_process_name", return_value="wetype_update.exe"):
            self.assertEqual([], system_control.find_hidden_main_windows(gw, "微信"))
            self.assertEqual([], system_control.find_hidden_main_windows(gw, handle=1))
        with patch.dict(sys.modules, {"win32gui": gui}), patch.object(
                system_control, "_window_process_name", return_value="wechat.exe"):
            self.assertIn(windows[2], system_control.find_hidden_main_windows(gw, "微信"))
            self.assertEqual([], system_control.find_hidden_main_windows(gw, "微信", exact=True))

    def test_default_resolver_preserves_z_order_while_unique_rejects_alias_ties(self):
        first, second = FakeWindow("微信", 301), FakeWindow("WeChat", 302)
        gw = fake_window_module([first, second])
        self.assertIs(first, system_control.resolve_window(gw, "微信"))
        self.assertIsNone(system_control.resolve_window(gw, "微信", unique=True))
        gw.getAllWindows = lambda: [first, FakeWindow("微信", 303)]
        self.assertIs(first, system_control.resolve_window(gw, "微信"))
        self.assertIsNone(system_control.resolve_window(gw, "微信", unique=True))

    def test_uia_empty_or_truncated_tree_is_unusable_but_editor_tree_is_not(self):
        root = SimpleNamespace(ControlTypeName="WindowControl")
        editor = SimpleNamespace(ControlTypeName="EditControl")
        for controls, truncated, expected in (([root], False, True), ([root, editor], False, False),
                                               ([editor], True, True)):
            def read(_handle, *, stats):
                stats["truncated"] = truncated
                return controls
            with patch.object(computer_module, "_uia_controls", side_effect=read):
                result = computer_module._uia_select_named_control(88, "target")
            self.assertEqual(expected, result["uia_tree_unusable"])

    def test_restore_precedes_uia_focus_and_activation_error_requires_real_foreground(self):
        window = FakeWindow("Test", 301)
        window.isMinimized = True
        auto = types.ModuleType("uiautomation")
        auto.ControlFromHandle = lambda _: SimpleNamespace(SetFocus=lambda: window.calls.append("uia_focus"))
        gui = types.ModuleType("win32gui")
        gui.IsWindow = lambda _: False
        gui.GetForegroundWindow = lambda: 301
        def activate():
            window.calls.append("activate")
            raise RuntimeError("activate failed")
        window.activate = activate
        with patch.dict(sys.modules, {"win32gui": gui, "uiautomation": auto}):
            self.assertTrue(system_control.focus_window_object(window)["focused"])
            self.assertEqual(["restore", "uia_focus", "activate"], window.calls)
            gui.GetForegroundWindow = lambda: 999
            with self.assertRaises(RuntimeError):
                system_control.focus_window_object(window)

    def test_hidden_main_window_discovery_filters_helpers_and_rejects_duplicates(self):
        windows = {1: FakeWindow("Hidden app", 1, (20, 30, 800, 600)),
                   2: FakeWindow("Hidden app", 2, (20, 30, 800, 600)),
                   3: FakeWindow("Hidden app", 3, (20, 30, 800, 600)),
                   4: FakeWindow("Hidden app", 4, (20, 30, 10, 10))}
        gui = types.ModuleType("win32gui")
        gui.IsWindow = lambda h: h in windows
        gui.IsWindowVisible = lambda h: False
        gui.GetWindowText = lambda h: windows[h].title
        gui.GetClassName = lambda h: "MSCTFIME UI" if h == 3 else "AppMain"
        gui.GetWindowRect = lambda h: (0, 0, windows[h].width, windows[h].height)
        gui.EnumWindows = lambda callback, arg: [callback(h, arg) for h in windows]
        gw = fake_window_module([])
        gw.Window = lambda h: windows[h]
        with patch.dict(sys.modules, {"win32gui": gui}):
            found = system_control.find_hidden_main_windows(gw, "Hidden app")
            self.assertEqual([1, 2], [w._hWnd for w in found])
            self.assertIs(windows[1], system_control.resolve_window(gw, "Hidden app"))
            self.assertIsNone(system_control.resolve_window(gw, "Hidden app", unique=True))
            self.assertIs(windows[1], system_control.resolve_window(gw, handle=1))
            self.assertTrue(system_control.window_info(windows[1])["hidden"])
            visible = FakeWindow("Hidden app", 10)
            gw.getAllWindows = lambda: [visible]
            self.assertIs(visible, system_control.resolve_window(gw, "Hidden app"))
            self.assertEqual([], system_control.find_hidden_main_windows(gw))

    def test_hidden_restore_uses_native_show_without_relaunch(self):
        window = FakeWindow("Hidden app", 1, (0, 0, 800, 600))
        visible = []
        gui = types.ModuleType("win32gui")
        gui.IsWindow = lambda h: True
        gui.IsWindowVisible = lambda h: bool(visible)
        gui.ShowWindow = lambda h, mode: visible.append((h, mode))
        con = types.ModuleType("win32con")
        con.SW_SHOW, con.SW_RESTORE = 5, 9
        auto = types.ModuleType("uiautomation")
        auto.ControlFromHandle = Mock(side_effect=RuntimeError("no UIA"))
        with patch.dict(sys.modules, {"win32gui": gui, "win32con": con, "uiautomation": auto}):
            result = system_control.focus_window_object(window)
        self.assertEqual([(1, 5), (1, 9)], visible)
        self.assertEqual(["activate"], window.calls)
        self.assertFalse(result["window"]["hidden"])

    def test_uia_traversal_budget_rejects_partial_action_candidates(self):
        leaf = SimpleNamespace(GetChildren=lambda: [])
        root = SimpleNamespace(GetChildren=lambda: [leaf])
        auto = types.ModuleType("uiautomation")
        auto.ControlFromHandle = lambda _: root
        for stats in (None, {}):
            with patch.dict(sys.modules, {"uiautomation": auto}), \
                 patch.object(computer_module.time, "monotonic", side_effect=[0, 0, 2]):
                entries = computer_module._uia_control_entries(1, stats=stats, timeout=1)
            if stats is None:
                self.assertEqual([], entries)
            else:
                self.assertTrue(stats["truncated"])
                self.assertEqual([(root, 0)], entries)

    def test_foreground_control_type_hints_resolve(self):
        self.assertIn("return", get_type_hints(computer_module._begin_foreground))
        self.assertIs(system_control.ControlToken, get_type_hints(computer_module._end_foreground)["control"])

    def test_codex_alias_exact_handle_and_bounded_info(self):
        chatgpt = FakeWindow("ChatGPT", 42, (-100, 10, 500, 400))
        other = FakeWindow("ChatGPT Settings", 43)
        gw = fake_window_module([other, chatgpt])

        self.assertIs(system_control.resolve_window(gw, "Codex"), chatgpt)
        self.assertIs(system_control.resolve_window(gw, handle=43), other)
        info = system_control.window_info(chatgpt)
        self.assertEqual(info["handle"], 42)
        self.assertEqual(info["rect"]["x"], -100)

    def test_cad_alias_matches_autocad_window(self):
        autocad = FakeWindow("Autodesk AutoCAD 2014 - Drawing1.dwg", 44)
        self.assertIs(system_control.resolve_window(fake_window_module([autocad]), "CAD"), autocad)

    def test_coordinate_region_and_image_change_validation(self):
        bounds = (-100, 0, 100, 100)
        self.assertEqual(computer_module.validate_point(-50, 99, bounds), (-50, 99))
        self.assertEqual(computer_module.validate_region(-100, 0, 200, 100, bounds), (-100, 0, 200, 100))
        with self.assertRaises(ValueError):
            computer_module.validate_point(100, 1, bounds)
        with self.assertRaises(ValueError):
            computer_module.validate_region(0, 0, 101, 100, bounds)

        white = Image.new("RGB", (20, 20), "white")
        changed = white.copy()
        for x in range(10):
            for y in range(20):
                changed.putpixel((x, y), (0, 0, 0))
        self.assertEqual(computer_module.image_change_ratio(white, white), 0.0)
        self.assertGreater(computer_module.image_change_ratio(white, changed), 0.4)

    def test_uia_snapshot_reports_depth_and_node_caps_as_incomplete(self):
        leaf = SimpleNamespace(Name="leaf", ControlTypeName="ButtonControl", GetChildren=lambda: [])
        root = SimpleNamespace(Name="root", ControlTypeName="WindowControl", GetChildren=lambda: [leaf])
        auto = types.ModuleType("uiautomation")
        auto.ControlFromHandle = lambda _: root
        with patch.dict(sys.modules, {"uiautomation": auto}):
            depth_capped = computer_module._uia_snapshot(9, max_depth=0, limit=20, include_offscreen=False)
            node_capped = computer_module._uia_snapshot(9, max_depth=36, limit=1, include_offscreen=False)
            complete = computer_module._uia_snapshot(9, max_depth=36, limit=2, include_offscreen=False)
        self.assertTrue(depth_capped["truncated"])
        self.assertTrue(node_capped["truncated"])
        self.assertFalse(complete["truncated"])

    def test_send_reservation_expires(self):
        computer_module._RECENT_SENDS.clear()
        self.assertTrue(computer_module.reserve_send("same", ttl=10, now=1))
        self.assertFalse(computer_module.reserve_send("same", ttl=10, now=5))
        self.assertTrue(computer_module.reserve_send("same", ttl=10, now=11))

    def test_uia_background_send_sets_value_and_invokes_button(self):
        value = SimpleNamespace(IsReadOnly=False, Value="")
        value.SetValue = lambda text: setattr(value, "Value", text)
        invoke = SimpleNamespace()
        invoke.Invoke = lambda: setattr(value, "Value", "")
        input_control = SimpleNamespace(GetPattern=lambda pattern: value)
        button = SimpleNamespace(
            ControlTypeName="ButtonControl", Name="发送",
            BoundingRectangle=SimpleNamespace(bottom=100),
            GetPattern=lambda pattern: invoke,
        )
        auto = types.ModuleType("uiautomation")
        auto.PatternId = SimpleNamespace(ValuePattern=1, InvokePattern=2)
        with patch.dict(sys.modules, {"uiautomation": auto}), \
             patch.object(computer_module, "_semantic_text_input", return_value=input_control), \
             patch.object(computer_module, "_uia_controls", return_value=[button]):
            result = computer_module._uia_background_send(8, "你好", "enter")
        self.assertTrue(result["available"])
        self.assertTrue(result["verified"])

    def test_send_button_name_requires_explicit_label(self):
        self.assertTrue(computer_module._is_send_button_name("Send message"))
        self.assertTrue(computer_module._is_send_button_name("发送"))
        self.assertFalse(computer_module._is_send_button_name("正在运行 send_buttons"))

    def test_uia_select_named_control_requires_unique_selected_target(self):
        class _Pattern:
            CurrentIsSelected = False

            def Select(self):
                self.CurrentIsSelected = True

        pattern = _Pattern()
        control = SimpleNamespace(
            Name="6S 临时设施施工专项方案", ControlTypeName="ListItemControl",
            IsOffscreen=False, GetPattern=lambda _pattern_id: pattern,
        )
        auto = types.ModuleType("uiautomation")
        auto.PatternId = SimpleNamespace(SelectionItemPattern="selection")
        with patch.dict(sys.modules, {"uiautomation": auto}), \
             patch.object(computer_module, "_uia_controls", return_value=[control]):
            result = computer_module._uia_select_named_control(88, "6S 临时设施施工专项方案")
        self.assertTrue(result["ok"])
        self.assertEqual("target_selected", result["status"])

    def test_uia_select_named_control_invokes_codex_conversation_button(self):
        invoked = []
        invoke = SimpleNamespace(Invoke=lambda: invoked.append(True))
        control = SimpleNamespace(
            Name="6S 临时设施施工专项方案", ControlTypeName="ButtonControl",
            IsOffscreen=False,
            GetPattern=lambda pattern_id: invoke if pattern_id == "invoke" else None,
        )
        auto = types.ModuleType("uiautomation")
        auto.PatternId = SimpleNamespace(
            SelectionItemPattern="selection",
            InvokePattern="invoke",
            LegacyIAccessiblePattern="legacy",
        )
        with patch.dict(sys.modules, {"uiautomation": auto}), \
             patch.object(computer_module, "_uia_controls", return_value=[control]):
            result = computer_module._uia_select_named_control(88, "6S 临时设施施工专项方案")
        self.assertTrue(result["ok"])
        self.assertEqual("target_selected", result["status"])
        self.assertEqual("invoke", result["action"])
        self.assertEqual([True], invoked)

    def test_uia_select_named_control_accepts_target_inside_long_label(self):
        invoked = []
        control = SimpleNamespace(
            Name="朱镇平 12:24 [图片]", ControlTypeName="ButtonControl",
            IsOffscreen=False,
            GetPattern=lambda pattern_id: SimpleNamespace(Invoke=lambda: invoked.append(True)),
        )
        auto = types.ModuleType("uiautomation")
        auto.PatternId = SimpleNamespace(
            SelectionItemPattern="selection", InvokePattern="invoke",
            LegacyIAccessiblePattern="legacy",
        )
        with patch.dict(sys.modules, {"uiautomation": auto}), \
             patch.object(computer_module, "_uia_controls", return_value=[control]):
            result = computer_module._uia_select_named_control(88, "朱镇平")
        self.assertTrue(result["ok"])
        self.assertTrue(result["requires_visual_confirmation"])
        self.assertEqual([True], invoked)

    def test_uia_select_named_control_ignores_stale_wrapper_for_actionable_button(self):
        invoked = []

        class _StaleWrapper:
            Name = "6S 临时设施施工专项方案"
            ControlTypeName = "ListItemControl"
            IsOffscreen = False

            def GetPattern(self, _pattern_id):
                raise RuntimeError("stale UIA node")

        invoke = SimpleNamespace(Invoke=lambda: invoked.append(True))
        button = SimpleNamespace(
            Name="6S 临时设施施工专项方案", ControlTypeName="ButtonControl",
            IsOffscreen=False,
            GetPattern=lambda pattern_id: invoke if pattern_id == "invoke" else None,
        )
        auto = types.ModuleType("uiautomation")
        auto.PatternId = SimpleNamespace(
            SelectionItemPattern="selection",
            InvokePattern="invoke",
            LegacyIAccessiblePattern="legacy",
        )
        with patch.dict(sys.modules, {"uiautomation": auto}), \
             patch.object(computer_module, "_uia_controls", return_value=[_StaleWrapper(), button]):
            result = computer_module._uia_select_named_control(88, "6S 临时设施施工专项方案")
        self.assertTrue(result["ok"], result)
        self.assertEqual("invoke", result["action"])
        self.assertEqual([True], invoked)

    def test_uia_select_named_control_rejects_ambiguous_target(self):
        controls = [
            SimpleNamespace(Name="6S", ControlTypeName="ListItemControl", IsOffscreen=False),
            SimpleNamespace(Name="6S", ControlTypeName="ListItemControl", IsOffscreen=False),
        ]
        with patch.object(computer_module, "_uia_controls", return_value=controls):
            result = computer_module._uia_select_named_control(88, "6S")
        self.assertFalse(result["ok"])
        self.assertEqual("uia_target_ambiguous", result["status"])

    def test_uia_control_info_is_bounded_and_serializable(self):
        control = SimpleNamespace(
            Name="发送", ControlTypeName="ButtonControl", AutomationId="send-button",
            ClassName="Button", IsOffscreen=False,
            BoundingRectangle=SimpleNamespace(left=10, top=20, right=90, bottom=50),
        )
        result = computer_module._uia_control_info(control, 3)
        self.assertEqual({"x": 10, "y": 20, "width": 80, "height": 30}, result["rect"])
        self.assertEqual("send-button", result["automation_id"])
        self.assertEqual(3, result["depth"])


class ControlLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_uia_initializer_lives_in_operation_thread(self):
        steps = []
        auto = types.ModuleType('uiautomation')
        class Initializer:
            def __enter__(self): steps.append(('init', threading.get_ident()))
            def __exit__(self, *_args): steps.append(('exit', threading.get_ident()))
        auto.UIAutomationInitializerInThread = Initializer
        def _uia_probe():
            steps.append(('call', threading.get_ident()))
            return 'ok'
        _uia_probe.__module__ = computer_module.__name__
        with patch.dict(sys.modules, {'uiautomation': auto}):
            result = await asyncio.to_thread(system_control._run_controlled_input, _uia_probe, (), {})
        self.assertEqual('ok', result)
        self.assertEqual(['init', 'call', 'exit'], [kind for kind, _ in steps])
        self.assertEqual(1, len({thread for _, thread in steps}))
        self.assertNotEqual(threading.get_ident(), steps[0][1])

    async def test_owned_takeover_does_not_wait_for_user_input_and_escape_still_revokes(self):
        notices = []
        escaped = []
        context = system_control.set_foreground_notice_callback(notices.append, on_escape=lambda: escaped.append(True))
        try:
            with patch.object(system_control, 'ensure_listener'), \
                 patch.object(system_control, 'user_idle_seconds', return_value=0) as idle:
                owner = await system_control.acquire_control(0.2, foreground=False)
                self.assertEqual([], notices)
                self.assertTrue(await system_control.wait_for_user_idle())
                self.assertEqual([True], notices)
                idle.assert_not_called()
                system_control._escape_foreground()
                self.assertEqual([True, False], notices)
                self.assertEqual([True], escaped)
                with self.assertRaisesRegex(RuntimeError, '交还用户'):
                    await system_control.wait_for_user_idle()
        finally:
            system_control.reset_foreground_notice_callback(context)

    async def test_notice_uses_acquiring_context_after_callback_reset(self):
        notices = []
        context = system_control.set_foreground_notice_callback(notices.append)
        owner = await system_control.acquire_control(0.2)
        system_control.reset_foreground_notice_callback(context)
        self.assertEqual([True], notices)
        self.assertTrue(system_control.foreground_control_active())
        self.assertTrue(system_control.release_control(owner))
        self.assertEqual([True, False], notices)
        self.assertFalse(system_control.foreground_control_active())

    async def test_sync_worker_notifies_and_bad_callback_does_not_block_release(self):
        notices = []
        context = system_control.set_foreground_notice_callback(notices.append)
        try:
            owner = await asyncio.to_thread(system_control.acquire_control_sync, 0.2)
            await asyncio.to_thread(system_control.release_control, owner)
            self.assertEqual([True, False], notices)
        finally:
            system_control.reset_foreground_notice_callback(context)
        def broken(_active):
            raise RuntimeError("UI unavailable")
        context = system_control.set_foreground_notice_callback(broken)
        try:
            owner = await system_control.acquire_control(0.2)
            self.assertTrue(system_control.release_control(owner))
        finally:
            system_control.reset_foreground_notice_callback(context)
        self.assertFalse(system_control.foreground_control_active())

    async def test_cancelled_worker_keeps_lease_until_thread_finishes(self):
        started = threading.Event()
        finish = threading.Event()
        replacement = None
        notices = []
        context = system_control.set_foreground_notice_callback(notices.append)
        owner = await system_control.acquire_control(0.2)
        system_control.reset_foreground_notice_callback(context)
        self.assertIsNotNone(owner)

        def slow_input():
            started.set()
            finish.wait(2)

        async def run_owner():
            try:
                await system_control.run_controlled(owner, slow_input)
            finally:
                system_control.release_control(owner)

        task = asyncio.create_task(run_owner())
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            with self.assertLogs(system_control._LOGGER, level="WARNING"):
                self.assertFalse(system_control.release_control(system_control.ControlToken()))
            self.assertIsNone(await system_control.acquire_control(0.1))
            self.assertEqual([True], notices, "cancel must not hide while the thread still owns the desktop")

            finish.set()
            replacement = await system_control.acquire_control(1.0)
            self.assertIsNotNone(replacement)
            self.assertEqual([True, False], notices)
        finally:
            finish.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if replacement is not None:
                system_control.release_control(replacement)
            else:
                cleanup = await system_control.acquire_control(1.0)
                if cleanup is not None:
                    system_control.release_control(cleanup)

    async def test_cancelled_type_text_keeps_control_until_input_thread_finishes(self):
        started = threading.Event()
        finish = threading.Event()
        pg = FakePyAutoGUI()
        pyperclip = types.ModuleType("pyperclip")
        pyperclip.copy = lambda _text: (_ for _ in ()).throw(RuntimeError("clipboard unavailable"))

        def blocked_write(text, interval=0):
            pg.actions.append(("write-start", text, interval))
            started.set()
            finish.wait(2)
            pg.actions.append(("write-finish", text, interval))

        pg.write = blocked_write
        registry = ToolRegistry()
        replacement = None
        with patch.dict(sys.modules, {"pyautogui": pg, "pyperclip": pyperclip}), \
             patch.object(computer_module, "_gui_available", return_value=True), \
             patch.object(computer_module, "_foreground_window_title", return_value="普通测试窗口"), \
             patch.object(system_control, "wait_for_user_idle", new=AsyncMock(return_value=True)):
            computer_module.register_tools(registry, FakeLLM())
            task = asyncio.create_task(registry.call("type_text", {"text": "slow input"}))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertIsNone(await system_control.acquire_control(0.1))

                finish.set()
                replacement = await system_control.acquire_control(1.0)
                self.assertIsNotNone(replacement)
                self.assertEqual([
                    ("write-start", "slow input", 0.01),
                    ("write-finish", "slow input", 0.01),
                ], pg.actions)
            finally:
                finish.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if replacement is not None:
                    system_control.release_control(replacement)
                else:
                    cleanup = await system_control.acquire_control(1.0)
                    if cleanup is not None:
                        system_control.release_control(cleanup)

    async def test_begin_foreground_releases_lease_when_idle_check_is_cancelled(self):
        entered = asyncio.Event()
        wait_forever = asyncio.Event()

        async def blocked_idle(*_args):
            entered.set()
            await wait_forever.wait()

        with patch.object(system_control, "wait_for_user_idle", new=blocked_idle):
            task = asyncio.create_task(computer_module._begin_foreground(0.2))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        replacement = await system_control.acquire_control(0.2)
        self.assertIsNotNone(replacement)
        system_control.release_control(replacement)

    async def test_begin_foreground_releases_lease_when_idle_check_fails(self):
        with patch.object(
            system_control, "wait_for_user_idle", new=AsyncMock(side_effect=RuntimeError("failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                await computer_module._begin_foreground(0.2)

        replacement = await system_control.acquire_control(0.2)
        self.assertIsNotNone(replacement)
        system_control.release_control(replacement)


class ComputerToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_isolated_uia_timeout_kills_only_query_process_and_keeps_loop_alive(self):
        process = SimpleNamespace(returncode=None, killed=False)
        process.poll = lambda: 1 if process.killed else None
        process.kill = lambda: setattr(process, 'killed', True)
        process.wait = lambda: 1
        with patch.object(computer_module.subprocess, 'Popen', return_value=process), \
             patch.object(computer_module, '_UIA_QUERY_TIMEOUT', 0):
            result = await computer_module._inspect_uia_isolated(91, max_depth=36, limit=2500, include_offscreen=False)
        self.assertEqual('uia_timeout', result['status'])
        self.assertTrue(result['visual_fallback'])
        self.assertTrue(process.killed)

    async def test_short_observation_timeout_does_not_change_original_tool_budget(self):
        process = SimpleNamespace(returncode=None, killed=False)
        process.poll = lambda: 1 if process.killed else None
        process.kill = lambda: setattr(process, "killed", True)
        process.wait = lambda: 1
        with patch.object(computer_module.subprocess, "Popen", return_value=process):
            result = await computer_module._inspect_uia_isolated(91, query_timeout=0)
        self.assertEqual("uia_timeout", result["status"])
        self.assertTrue(process.killed)
        self.assertEqual(12.0, computer_module._UIA_QUERY_TIMEOUT)

    async def test_isolated_uia_cancel_reaps_query_process(self):
        process = SimpleNamespace(returncode=None, killed=False)
        process.poll = lambda: 1 if process.killed else None
        process.kill = lambda: setattr(process, 'killed', True)
        process.wait = lambda: 1
        started = asyncio.Event()
        async def blocked(_seconds):
            started.set()
            await asyncio.Event().wait()
        with patch.object(computer_module.subprocess, 'Popen', return_value=process), \
             patch.object(computer_module.asyncio, 'sleep', new=blocked):
            task = asyncio.create_task(computer_module._inspect_uia_isolated(91))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.killed)

    async def test_uia_snapshot_properties_stay_in_reading_thread_and_report_actual_limit(self):
        def entries(_handle, **options):
            thread = threading.get_ident()
            class Control:
                def __getattr__(self, name):
                    self_thread = threading.get_ident()
                    if self_thread != thread:
                        raise AssertionError('COM crossed thread')
                    return {'Name': 'test', 'IsOffscreen': False}.get(name, '')
            return [(Control(), 1)] * 600
        with patch.object(computer_module, '_uia_control_entries', side_effect=entries):
            result = await asyncio.to_thread(computer_module._uia_snapshot, 91, max_depth=36,
                                             limit=2500, include_offscreen=False)
        self.assertEqual(600, result['count'])
        self.assertEqual('test', result['controls'][0]['name'])
        self.assertFalse(result['truncated'])

    async def asyncSetUp(self):
        computer_module._RECENT_SENDS.clear()
        self.pg = FakePyAutoGUI()
        self.clipboard = []
        pyperclip = types.ModuleType("pyperclip")
        pyperclip.copy = self.clipboard.append
        self.modules = patch.dict(sys.modules, {"pyautogui": self.pg, "pyperclip": pyperclip})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.gui_patch = patch.object(computer_module, "_gui_available", return_value=True)
        self.gui_patch.start()
        self.addCleanup(self.gui_patch.stop)
        self.foreground_handle_patch = patch.object(
            computer_module, "_foreground_window_handle", return_value=0,
        )
        self.foreground_handle_patch.start()
        self.addCleanup(self.foreground_handle_patch.stop)
        self.bounds_patch = patch.object(computer_module, "screen_bounds", return_value=(0, 0, 100, 100))
        self.bounds_patch.start()
        self.addCleanup(self.bounds_patch.stop)
        self.control_token = object()
        self.acquire_patch = patch.object(
            system_control, "acquire_control", new=AsyncMock(return_value=self.control_token),
        )
        self.acquire_patch.start()
        self.addCleanup(self.acquire_patch.stop)
        self.activate_patch = patch.object(system_control, "activate_foreground")
        self.activate_notice = self.activate_patch.start()
        self.addCleanup(self.activate_patch.stop)
        self.release_patch = patch.object(system_control, "release_control", return_value=True)
        self.release_patch.start()
        self.addCleanup(self.release_patch.stop)
        self.controlled_calls = []

        async def run_controlled(_control, function, /, *args, **kwargs):
            self.controlled_calls.append((function, args, kwargs))
            return function(*args, **kwargs)

        self.controlled_patch = patch.object(system_control, "run_controlled", new=run_controlled)
        self.controlled_patch.start()
        self.addCleanup(self.controlled_patch.stop)
        self.idle_patch = patch.object(system_control, "wait_for_user_idle", new=AsyncMock(return_value=True))
        self.idle_patch.start()
        self.addCleanup(self.idle_patch.stop)
        self.registry = ToolRegistry()
        self.llm = FakeLLM()
        self.current_llm = self.llm
        computer_module.register_tools(self.registry, lambda: self.current_llm)

    async def test_corner_failsafe_is_removed_but_permission_hook_still_blocks(self):
        self.assertFalse(self.pg.FAILSAFE)
        self.pg.failSafeCheck()
        before = list(self.pg.actions)
        with patch.object(system_control, "check_foreground_permission", side_effect=RuntimeError("Esc revoked")):
            with self.assertRaises(RuntimeError):
                self.pg.failSafeCheck()
            with self.assertRaises(RuntimeError):
                system_control._run_controlled_input(self.pg.click, (50, 50), {})
        self.assertEqual(before, self.pg.actions)

    async def test_fuzzy_selected_destination_cannot_send_without_independent_confirmation(self):
        window = FakeWindow("App", 88)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={
                 "ok": True, "requires_visual_confirmation": True}), \
             patch.object(computer_module, "_uia_background_send") as send:
            result = await self.registry.call("send_text_to_window", {
                "window": "App", "destination": "Alice", "text": "hello", "allow_visual_fallback": False})
        self.assertFalse(result["ok"])
        self.assertEqual("uia_target_unconfirmed", result["status"])
        send.assert_not_called()
        self.assertEqual([], self.pg.actions)

    async def test_screenshot_path_variants_preserve_scale_and_crop_mapping(self):
        screenshot = "D:/XiaoA/screenshots/current.jpg".replace("/", chr(92))
        with patch.object(computer_module, "save_screenshot_file", return_value=screenshot), \
             patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (60, 40), "white")), \
             patch.object(computer_module, "compress_screenshot", return_value=(b"image", 30, 20)):
            await self.registry.call("screen_capture_region", {"x": 10, "y": 20, "width": 60, "height": 40})
        variants = (screenshot, screenshot.replace(chr(92), chr(92) * 2),
                    screenshot.replace(chr(92), "/"), screenshot.upper())
        for reference in variants:
            with self.subTest(reference=reference):
                result = await self.registry.call("click_coordinate", {
                    "x": 3, "y": 4, "purpose": "test mapped screenshot", "screenshot_path": reference})
                self.assertEqual((16, 28), (result["x"], result["y"]))
                self.assertEqual(("click", 16, 28, "left"), self.pg.actions[-1])
        for reference in ("D:/Other/screenshots/current.jpg", "D:/XiaoA/screenshots/unknown.jpg"):
            before = len(self.pg.actions)
            with self.subTest(reference=reference), self.assertRaisesRegex(ValueError, "截图引用不可用"):
                await self.registry.call("click_coordinate", {
                    "x": 3, "y": 4, "purpose": "unknown screenshot", "screenshot_path": reference})
            self.assertEqual(before, len(self.pg.actions))

    async def test_optional_action_observations_and_prevalidation(self):
        cases = [("click_coordinate", {"x": 5, "y": 6, "purpose": "test"}),
                 ("scroll_screen", {"amount": -1}),
                 ("type_text", {"text": "test"}),
                 ("press_hotkey", {"keys": ["ctrl", "s"]})]
        with patch.object(computer_module, "save_screenshot_file", return_value="observed.jpg"):
            for name, args in cases:
                await self.registry.call(name, args)
                captures = len(self.pg.screenshots)
                result = await self.registry.call(name, {**args, "observe": "screen"})
                self.assertTrue(result["ok"])
                self.assertTrue(result["_observation_image"])
                self.assertEqual(captures + 1, len(self.pg.screenshots))
                actions = list(self.pg.actions)
                with self.assertRaises(ValueError):
                    await self.registry.call(name, {**args, "observe": "region"})
                self.assertEqual(actions, self.pg.actions)
            with patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (30, 40), "white")):
                result = await self.registry.call("click_coordinate", {
                    "x": 5, "y": 6, "purpose": "test", "observe": "region",
                    "observe_region": {"x": 10, "y": 20, "width": 30, "height": 40}})
            self.assertEqual({"x": 10, "y": 20}, result["coordinate_mapping"]["origin"])
            mapped = await self.registry.call("click_coordinate", {
                "x": 3, "y": 4, "purpose": "mapped", "screenshot_path": result["path"]})
            self.assertEqual((13, 24), (mapped["x"], mapped["y"]))
        self.pg.screenshot_error = RuntimeError("capture failed")
        before = len(self.pg.actions)
        result = await self.registry.call("click_coordinate", {
            "x": 5, "y": 6, "purpose": "test", "observe": "screen"})
        self.assertTrue(result["ok"])
        self.assertIn("capture failed", result["observation_error"])
        self.assertEqual(before + 1, len(self.pg.actions))

    async def test_registration_guard_exercises_every_computer_tool(self):
        self.assertEqual({
            "screen_capture", "screen_capture_region", "inspect_ui_controls", "click_element", "click_coordinate",
            "triple_click", "move_pointer", "scroll_screen", "drag_pointer", "type_text",
            "wait_for_screen_change", "send_text_to_window", "press_hotkey", "desktop_observe", "desktop_execute",
        }, set(self.registry.names()))
        await self.registry.call("move_pointer", {"x": 7, "y": 8})
        self.assertIn(("move", 7, 8, 0.2), self.pg.actions)

    async def test_screen_capture_returns_real_png_without_taking_control(self):
        result = await self.registry.call("screen_capture", {})
        screenshot_path = Path(result["path"])
        try:
            image = Image.open(io.BytesIO(base64.b64decode(result["_observation_image"])))
            with Image.open(screenshot_path) as saved:
                self.assertEqual((100, 100), image.size)
                self.assertEqual((100, 100), saved.size)
            self.assertEqual([None], self.pg.screenshots)
            system_control.acquire_control.assert_not_awaited()
            self.assertEqual([], self.pg.actions)
        finally:
            screenshot_path.unlink(missing_ok=True)

        self.pg.screenshot_error = RuntimeError("capture failed")
        with self.assertRaisesRegex(RuntimeError, "capture failed"):
            await self.registry.call("screen_capture", {})
        self.assertEqual([], self.pg.actions)

    async def test_screen_capture_region_validates_before_capture(self):
        with patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (30, 40), "white")) as grab:
            result = await self.registry.call("screen_capture_region", {
                "x": 10, "y": 20, "width": 30, "height": 40,
            })
        screenshot_path = Path(result["path"])
        try:
            image = Image.open(io.BytesIO(base64.b64decode(result["_observation_image"])))
            with Image.open(screenshot_path) as saved:
                self.assertEqual((30, 40), image.size)
                self.assertEqual((30, 40), saved.size)
            self.assertEqual({"x": 10, "y": 20, "width": 30, "height": 40}, result["region"])
            grab.assert_called_once_with(bbox=(10, 20, 40, 60), all_screens=True)
        finally:
            screenshot_path.unlink(missing_ok=True)

        with self.assertRaisesRegex(ValueError, "超出"):
            await self.registry.call("screen_capture_region", {
                "x": 90, "y": 90, "width": 20, "height": 20,
            })
        self.assertEqual(1, grab.call_count)

    async def test_pointer_and_hotkey_use_controlled_input_and_stop_when_user_active(self):
        moved = await self.registry.call("move_pointer", {"x": 30, "y": 40, "duration": 9})
        hotkey = await self.registry.call("press_hotkey", {"keys": ["ctrl", "shift", "s"]})
        self.assertEqual(5.0, moved["duration"])
        self.assertEqual(["ctrl", "shift", "s"], hotkey["keys"])
        self.assertEqual(["moveTo", "hotkey"], [call[0].__name__ for call in self.controlled_calls])
        self.assertIn(("move", 30, 40, 5.0), self.pg.actions)
        self.assertIn(("hotkey", "ctrl", "shift", "s"), self.pg.actions)

        actions_before = list(self.pg.actions)
        with patch.object(system_control, "wait_for_user_idle", new=AsyncMock(return_value=False)):
            rejected = await self.registry.call("press_hotkey", {"keys": ["ctrl", "s"]})
        self.assertEqual("user_active", rejected["status"])
        self.assertEqual(actions_before, self.pg.actions)

    async def test_type_text_waits_for_exact_window_focus_without_redirecting(self):
        window = FakeWindow("图形另存为", 88)
        with patch.object(computer_module, "_text_input_window", return_value=window), \
             patch.object(computer_module, "_foreground_window_handle", side_effect=[77, 77, 88, 88]), \
             patch.object(computer_module.asyncio, "sleep", new=AsyncMock()) as sleep:
            result = await self.registry.call("type_text", {"text": "drawing.dwg", "handle": 88})
        self.assertTrue(result["ok"])
        sleep.assert_awaited_once_with(0.1)
        self.assertEqual(["activate"], window.calls)
        self.assertEqual([], self.clipboard)
        self.assertIn(("write", "drawing.dwg", 0.01), self.pg.actions)
        self.pg.actions.clear()
        self.clipboard.clear()
        with patch.object(computer_module, "_text_input_window", return_value=window), \
             patch.object(computer_module, "_foreground_window_handle", return_value=77), \
             patch.object(computer_module.asyncio, "sleep", new=AsyncMock()):
            result = await self.registry.call("type_text", {"text": "drawing.dwg", "handle": 88})
        self.assertEqual("input_target_not_focused", result["status"])
        self.assertIn("对话框句柄", result["error"])
        self.assertEqual([], self.clipboard)
        self.assertEqual([], self.pg.actions)

    async def test_save_dialog_closing_after_invoke_does_not_report_click_failure(self):
        window = FakeWindow("图形另存为", 88)
        metadata = system_control.window_info(window)
        closed = False
        invoked = []
        def invoke(*_args):
            nonlocal closed
            closed = True
            invoked.append(True)
            return {"ok": True, "action": "invoke", "name": "保存", "control_type": "ButtonControl"}
        def info(_window):
            if closed:
                raise RuntimeError("1400 - 无效的窗口句柄")
            return metadata
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_click_named_control", side_effect=invoke), \
             patch.object(system_control, "window_info", side_effect=info):
            result = await self.registry.call("click_element", {
                "target_desc": "保存", "handle": 88, "allow_visual_fallback": False})
        self.assertTrue(result["ok"])
        self.assertEqual(metadata, result["window"])
        self.assertEqual([True], invoked)
        self.assertEqual([], self.pg.actions)

    async def test_ascii_commands_use_keyboard_and_explicit_newlines(self):
        await self.registry.call("type_text", {"text": "_.CIRCLE\n0,0\r\n333\n"})
        self.assertEqual([], self.clipboard)
        self.assertEqual([("write", "_.CIRCLE", 0.01), ("press", "enter"),
                          ("write", "0,0", 0.01), ("press", "enter"),
                          ("write", "333", 0.01), ("press", "enter")], self.pg.actions)

    async def test_hotkey_respects_window_and_rejects_invalid_keys(self):
        window = FakeWindow("AutoCAD", 88)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_foreground_window_handle", side_effect=[77, 88, 88]), \
             patch.object(computer_module, "_foreground_window_title", return_value="AutoCAD"):
            result = await self.registry.call("press_hotkey", {"window": "AutoCAD", "keys": ["return"]})
        self.assertTrue(result["ok"])
        self.assertEqual(["activate"], window.calls)
        self.assertEqual(("hotkey", "enter"), self.pg.actions[-1])
        self.pg.actions.clear()
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_foreground_window_handle", return_value=77), \
             patch.object(computer_module, "_text_input_window", return_value=FakeWindow("图形另存为", 77)), \
             patch.object(computer_module.asyncio, "sleep", new=AsyncMock()):
            result = await self.registry.call("press_hotkey", {"handle": 88, "keys": ["escape"]})
        self.assertEqual("input_target_not_focused", result["status"])
        self.assertEqual(77, result["foreground_window"]["handle"])
        self.assertEqual([], self.pg.actions)
        with self.assertRaises(ValueError):
            await self.registry.call("press_hotkey", {"keys": ["misspelled-key"]})
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([])}):
            result = await self.registry.call("press_hotkey", {"handle": 88, "keys": ["enter"]})
        self.assertEqual("input_target_missing", result["status"])
        self.assertEqual([], self.pg.actions)

    async def test_named_combo_focus_uses_edit_child_under_foreground_control(self):
        window = FakeWindow("图形另存为", 88)
        edits = []
        edit = SimpleNamespace(ControlTypeName="EditControl", Name="文件名(N):", AutomationId="1001",
                               IsOffscreen=False, SetFocus=lambda: edits.append("focus"))
        combo = SimpleNamespace(ControlTypeName="ComboBoxControl", Name="文件名(N):", AutomationId="13006",
                                IsOffscreen=False, GetChildren=lambda: [edit])
        edit.GetParentControl = lambda: combo
        combo.GetParentControl = lambda: None
        auto = types.ModuleType("uiautomation")
        auto.ControlsAreSame = lambda left, right: left is right
        for options in ({}, {"automation_id": "13006"}, {"control_type": "ComboBoxControl"},
                        {"control_type": "EditControl"}, {"automation_id": "1001"}):
            with self.subTest(options=options), \
                 patch.dict(sys.modules, {"pygetwindow": fake_window_module([window]), "uiautomation": auto}), \
                 patch.object(computer_module, "_uia_controls", return_value=[combo, edit]), \
                 patch.object(computer_module, "_foreground_window_handle", return_value=88):
                result = await self.registry.call("click_element", {
                    "target_desc": "文件名输入框", "handle": 88, "allow_visual_fallback": False, **options})
            self.assertTrue(result["ok"], result)
            self.assertEqual("focus", result["action"])
            self.assertEqual("foreground_exclusive", result["control_mode"])
        self.assertEqual(["focus"] * 5, edits)
        self.assertEqual([], self.pg.actions)
        with patch.object(computer_module, "_uia_controls", return_value=[combo, edit]):
            result = computer_module._uia_click_named_control(88, "文件名(N):", automation_id="missing", strict=True)
        self.assertEqual("uia_control_not_found", result["status"])

    async def test_uia_mnemonic_labels_and_list_editor_deduplication(self):
        selected = []
        auto = types.ModuleType("uiautomation")
        auto.PatternId = SimpleNamespace(InvokePattern="invoke", SelectionItemPattern="select")
        auto.ControlsAreSame = lambda left, right: left is right
        for label, target in (("保存(S)", "保存按钮"), ("是（Y）", "是"), ("确定(O)", "确定")):
            button = SimpleNamespace(ControlTypeName="ButtonControl", Name=label, AutomationId="1",
                                     IsOffscreen=False, GetPattern=lambda _id: SimpleNamespace(Invoke=lambda: selected.append("invoke")))
            with patch.dict(sys.modules, {"uiautomation": auto}), \
                 patch.object(computer_module, "_uia_controls", return_value=[button]):
                result = computer_module._uia_click_named_control(88, target, strict=True)
            self.assertTrue(result["ok"], result)
        item = SimpleNamespace(ControlTypeName="ListItemControl", Name="Drawing1", IsOffscreen=False,
                               GetParentControl=lambda: None,
                               GetPattern=lambda _id: SimpleNamespace(Invoke=lambda: selected.append("item")))
        edit = SimpleNamespace(ControlTypeName="EditControl", Name="Drawing1", IsOffscreen=False,
                               GetParentControl=lambda: item)
        with patch.dict(sys.modules, {"uiautomation": auto}), \
             patch.object(computer_module, "_uia_controls", return_value=[item, edit]):
            result = computer_module._uia_click_named_control(88, "Drawing1", strict=True)
            self.assertTrue(result["ok"], result)
            unrelated = SimpleNamespace(ControlTypeName="EditControl", Name="Drawing1", IsOffscreen=False,
                                        GetParentControl=lambda: None)
            with patch.object(computer_module, "_uia_controls", return_value=[item, unrelated]):
                result = computer_module._uia_click_named_control(88, "Drawing1", strict=True)
            self.assertEqual("uia_control_ambiguous", result["status"])

    async def test_type_text_uses_clipboard_then_controlled_fallback_and_enforces_limit(self):
        pasted = await self.registry.call("type_text", {"text": "中文输入"})
        self.assertEqual(4, pasted["characters"])
        self.assertEqual(["中文输入"], self.clipboard)
        self.assertEqual(("hotkey", "ctrl", "v"), self.pg.actions[-1])
        self.assertEqual("hotkey", self.controlled_calls[-1][0].__name__)

        pyperclip = sys.modules["pyperclip"]
        with patch.object(pyperclip, "copy", side_effect=RuntimeError("clipboard busy")):
            typed = await self.registry.call("type_text", {"text": "fallback"})
        self.assertEqual(8, typed["characters"])
        self.assertEqual(("write", "fallback", 0.01), self.pg.actions[-1])
        self.assertEqual("write", self.controlled_calls[-1][0].__name__)

        actions_before = list(self.pg.actions)
        with self.assertRaisesRegex(ValueError, "20000"):
            await self.registry.call("type_text", {"text": "x" * 20_001})
        self.assertEqual(actions_before, self.pg.actions)

    async def test_wait_for_screen_change_default_accepts_small_meaningful_change(self):
        with patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (20, 10), "white")), \
             patch.object(
                 computer_module, "wait_for_change", new=AsyncMock(return_value=(True, 0.006007)),
             ) as wait:
            result = await self.registry.call("wait_for_screen_change", {"timeout": 8})
        self.assertTrue(result["ok"])
        self.assertEqual(0.005, result["threshold"])
        self.assertEqual(0.005, wait.await_args.args[4])

    async def test_wait_for_screen_change_validates_region_and_reports_timeout(self):
        with patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (20, 10), "white")) as grab, \
             patch.object(
                 computer_module, "wait_for_change", new=AsyncMock(return_value=(False, 0.004)),
             ) as wait:
            result = await self.registry.call("wait_for_screen_change", {
                "x": 1, "y": 2, "width": 20, "height": 10,
                "timeout": 99, "threshold": 0,
            })
        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        self.assertEqual("no_change", result["status"])
        self.assertIn("不是等待桌面空闲", result["summary"])
        self.assertIn("不要只因超时", result["note"])
        self.assertEqual(30.0, result["timeout"])
        self.assertEqual(0.001, result["threshold"])
        grab.assert_called_once_with(bbox=(1, 2, 21, 12), all_screens=True)
        self.assertEqual((1, 2, 20, 10), wait.await_args.args[2])

        with self.assertRaisesRegex(ValueError, "必须同时提供"):
            await self.registry.call("wait_for_screen_change", {"x": 1, "y": 2})
        self.assertEqual(1, grab.call_count)

    async def test_visual_click_does_not_publish_a_post_action_screenshot(self):
        result = await self.registry.call("click_element", {
            "target_desc": "联系人朱镇平",
        })
        self.assertTrue(result["ok"])
        self.assertNotIn("_observation_image", result)
        self.assertIn("关键提交前再核对", result["text"])
        self.assertIn(("click", 10, 12, "left"), self.pg.actions)

    async def test_click_element_prefers_unique_uia_control_without_vision_or_mouse(self):
        invoked = []

        class _Pattern:
            def Invoke(self):
                invoked.append("invoke")

        class _Control:
            ControlTypeName = "ButtonControl"
            Name = "发送"
            IsOffscreen = False

            def GetPattern(self, pattern_id):
                return _Pattern() if pattern_id == "invoke" else None

        auto = types.ModuleType("uiautomation")
        auto.PatternId = SimpleNamespace(InvokePattern="invoke")
        with patch.object(computer_module, "_foreground_window_handle", return_value=88), \
             patch.object(computer_module, "_uia_controls", return_value=[_Control()]), \
             patch.dict(sys.modules, {"uiautomation": auto}):
            result = await self.registry.call("click_element", {"target_desc": "发送按钮"})

        self.assertTrue(result["ok"])
        self.assertEqual("uia", result["control_mode"])
        self.assertEqual(["invoke"], invoked)
        self.assertEqual(0, self.llm.calls)
        self.assertEqual([], self.pg.actions)

    async def test_click_element_exact_window_uses_automation_id_without_visual_fallback(self):
        window = FakeWindow("ChatGPT", 88)

        class _Control:
            ControlTypeName = "ButtonControl"
            Name = "发送"
            AutomationId = "send-button"
            IsOffscreen = False

            def GetPattern(self, _pattern_id):
                invoke = SimpleNamespace()
                invoke.Invoke = lambda: None
                return invoke

        auto = types.ModuleType("uiautomation")
        auto.PatternId = SimpleNamespace(InvokePattern="invoke")
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window]), "uiautomation": auto}), \
             patch.object(computer_module, "_uia_controls", return_value=[_Control()]):
            result = await self.registry.call("click_element", {
                "target_desc": "发送", "window": "Codex", "handle": 88,
                "automation_id": "send-button",
            })

        self.assertFalse(result["ok"])
        self.assertEqual("message_action_requires_send_tool", result["status"])
        self.assertEqual(0, self.llm.calls)
        self.assertEqual([], self.pg.actions)

    async def test_click_element_exact_window_does_not_fallback_on_ambiguous_uia(self):
        window = FakeWindow("ChatGPT", 89)

        class _Control:
            ControlTypeName = "ButtonControl"
            Name = "打开"
            IsOffscreen = False

            def GetPattern(self, _pattern_id):
                return object()

        auto = types.ModuleType("uiautomation")
        auto.PatternId = SimpleNamespace(InvokePattern="invoke")
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window]), "uiautomation": auto}), \
             patch.object(computer_module, "_uia_controls", return_value=[_Control(), _Control()]):
            result = await self.registry.call("click_element", {
                "target_desc": "打开", "handle": 89,
            })

        self.assertFalse(result["ok"])
        self.assertEqual("uia_control_ambiguous", result["status"])
        self.assertEqual(0, self.llm.calls)
        self.assertEqual([], self.pg.actions)

    async def test_inspect_ui_controls_reads_named_window_without_focus_or_mouse(self):
        window = FakeWindow("ChatGPT", 91)
        control = SimpleNamespace(
            Name="会话", ControlTypeName="ListItemControl", AutomationId="conversation-1",
            ClassName="ListItem", IsOffscreen=False,
            BoundingRectangle=SimpleNamespace(left=1, top=2, right=101, bottom=42),
        )
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_inspect_uia_isolated", new=AsyncMock(return_value={
                 'ok': True, 'count': 1, 'controls': [computer_module._uia_control_info(control, 2)],
             })):
            result = await self.registry.call("inspect_ui_controls", {
                "handle": 91, "max_depth": 4, "limit": 20,
            })

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["count"])
        self.assertEqual("conversation-1", result["controls"][0]["automation_id"])
        self.assertEqual(91, result["window"]["handle"])
        self.assertEqual([], self.pg.actions)

    async def test_ambiguous_foreground_uia_match_does_not_guess_visual_target(self):
        class _Control:
            ControlTypeName = "ButtonControl"
            Name = "打开"
            IsOffscreen = False

            def GetPattern(self, _pattern_id):
                return object()

        auto = types.ModuleType("uiautomation")
        auto.PatternId = SimpleNamespace(InvokePattern="invoke")
        with patch.object(computer_module, "_foreground_window_handle", return_value=88), \
             patch.object(computer_module, "_uia_controls", return_value=[_Control(), _Control()]), \
             patch.dict(sys.modules, {"uiautomation": auto}):
            result = await self.registry.call("click_element", {"target_desc": "打开"})

        self.assertFalse(result["ok"])
        self.assertEqual("uia_control_ambiguous", result["status"])
        self.assertEqual(0, self.llm.calls)
        self.assertEqual([], self.pg.actions)

    async def test_visual_tool_uses_reloaded_llm(self):
        replacement = FakeLLM((20, 22))
        self.current_llm = replacement

        result = await self.registry.call("click_element", {"target_desc": "发送按钮"})

        self.assertTrue(result["ok"])
        self.assertEqual(0, self.llm.calls)
        self.assertEqual(1, replacement.calls)
        self.assertIn(("click", 20, 22, "left"), self.pg.actions)

    async def test_mouse_tools_are_registered_and_bounded(self):
        expected = {
            "screen_capture_region", "click_coordinate", "move_pointer", "scroll_screen",
            "drag_pointer", "wait_for_screen_change", "send_text_to_window",
        }
        self.assertTrue(expected.issubset(set(self.registry.names())))

        clicked = await self.registry.call("click_coordinate", {"x": 20, "y": 30, "purpose": "选中一行"})
        await self.registry.call("scroll_screen", {"amount": -3, "x": 20, "y": 30})
        await self.registry.call("drag_pointer", {
            "start_x": 1, "start_y": 2, "end_x": 50, "end_y": 60, "purpose": "移动卡片",
        })
        self.assertTrue(clicked["ok"])
        self.assertEqual(clicked["control_mode"], "foreground_exclusive")
        self.assertIn(("click", 20, 30, "left"), self.pg.actions)
        self.assertIn(("scroll", -3), self.pg.actions)
        self.assertTrue(any(action[0] == "drag" for action in self.pg.actions))
        with self.assertRaises(ValueError):
            await self.registry.call("click_coordinate", {"x": 101, "y": 1, "purpose": "越界"})

    async def test_triple_click_is_bounded_and_runs_under_control_lock(self):
        result = await self.registry.call("triple_click", {
            "x": 20, "y": 30, "purpose": "select paragraph",
        })
        self.assertTrue(result["ok"])
        self.assertEqual("triple_click", result["action"])
        self.assertIn(("click", 20, 30, "left"), self.pg.actions)
        self.assertEqual(3, self.controlled_calls[-1][2]["clicks"])
        before = len(self.pg.actions)
        with self.assertRaises(ValueError):
            await self.registry.call("triple_click", {"x": 101, "y": 30, "purpose": "outside"})
        self.assertEqual(before, len(self.pg.actions))

    async def test_wait_for_change_detects_new_frame(self):
        white = Image.new("RGB", (10, 10), "white")
        black = Image.new("RGB", (10, 10), "black")
        changed, ratio = await computer_module.wait_for_change(
            lambda region: black, white, None, 0.1, 0.1,
        )
        self.assertTrue(changed)
        self.assertGreater(ratio, 0.9)

    async def test_verified_send_is_not_repeated(self):
        window = FakeWindow("ChatGPT", 88)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(system_control, "focus_window_object", return_value={
                 "focused": True, "semantic": False, "window": system_control.window_info(window),
             }), \
             patch.object(computer_module, "_focus_semantic_text_input", return_value=False), \
             patch.object(computer_module, "wait_for_change", new=AsyncMock(return_value=(True, 0.15))):
            args = {
                "window": "Codex", "destination": "当前会话",
                "text": "写代码方面用不用再加强", "allow_visual_fallback": True,
                "verify_timeout": 0.5,
            }
            with patch.object(computer_module, "_uia_select_named_control", return_value={"ok": True}):
                first = await self.registry.call("send_text_to_window", args)
                second = await self.registry.call("send_text_to_window", args)

        self.assertEqual(first["status"], "sent_verified")
        self.assertEqual(second["status"], "duplicate_suppressed")
        self.assertEqual(self.clipboard, [args["text"]])
        self.assertEqual(sum(1 for action in self.pg.actions if action == ("press", "enter")), 1)
        self.assertTrue(_looks_financial("click_coordinate", {"purpose": "确认付款"}))

    async def test_cancel_before_input_rolls_back_reservation_and_retry_sends(self):
        window = FakeWindow("ChatGPT", 90)
        idle_entered = asyncio.Event()
        idle_blocker = asyncio.Event()
        idle_calls = 0

        async def first_idle_wait_is_cancelled(*_args):
            nonlocal idle_calls
            idle_calls += 1
            if idle_calls == 1:
                idle_entered.set()
                await idle_blocker.wait()
            return True

        args = {
            "window": "Codex", "destination": "当前会话", "text": "取消后重试",
            "allow_visual_fallback": True, "verify_timeout": 0.5,
        }
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": True}), \
             patch.object(computer_module, "_uia_background_send", return_value={
                 "available": False, "partial": False, "reason": "不可用",
             }), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": True}), \
             patch.object(system_control, "wait_for_user_idle", new=first_idle_wait_is_cancelled), \
             patch.object(system_control, "focus_window_object", return_value={
                 "focused": True, "semantic": False, "window": system_control.window_info(window),
             }), \
             patch.object(computer_module, "_focus_semantic_text_input", return_value=True), \
             patch.object(computer_module, "wait_for_change", new=AsyncMock(return_value=(True, 0.2))):
            first = asyncio.create_task(self.registry.call("send_text_to_window", args))
            await idle_entered.wait()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual({}, computer_module._RECENT_SENDS)

            retry = await self.registry.call("send_text_to_window", args)

        self.assertEqual("sent_verified", retry["status"])
        self.assertTrue(retry["ok"])

    async def test_cancel_during_input_marks_previous_send_unconfirmed(self):
        window = FakeWindow("ChatGPT", 91)
        input_started = asyncio.Event()
        input_blocker = asyncio.Event()

        async def blocked_input(_control, function, /, *args, **kwargs):
            if function == self.pg.hotkey and args == ("ctrl", "v"):
                input_started.set()
                await input_blocker.wait()
            return function(*args, **kwargs)

        args = {
            "window": "Codex", "destination": "当前会话", "text": "结果不明",
            "allow_visual_fallback": True, "verify_timeout": 0.5,
        }
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_background_send", return_value={
                 "available": False, "partial": False, "reason": "不可用",
             }), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": True}), \
             patch.object(system_control, "focus_window_object", return_value={
                 "focused": True, "semantic": False, "window": system_control.window_info(window),
             }), \
             patch.object(computer_module, "_focus_semantic_text_input", return_value=True), \
             patch.object(system_control, "run_controlled", new=blocked_input):
            first = asyncio.create_task(self.registry.call("send_text_to_window", args))
            await input_started.wait()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first

            retry = await self.registry.call("send_text_to_window", args)

        self.assertFalse(retry["ok"])
        self.assertEqual("previous_send_unconfirmed", retry["status"])
        self.assertFalse(retry["duplicate_suppressed"])
        self.assertIsNone(retry["sent"])

    async def test_background_send_does_not_focus_or_touch_mouse(self):
        window = FakeWindow("ChatGPT", 89)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_background_send", return_value={
                 "available": True, "sent": True, "verified": True,
             }), \
             patch.object(computer_module, "_uia_select_named_control", return_value={
                 "ok": True,
             }), \
             patch.object(system_control, "focus_window_object") as focus:
            result = await self.registry.call("send_text_to_window", {
                "window": "Codex", "destination": "当前会话",
                "text": "后台发送", "verify_timeout": 0.5,
            })
        self.assertEqual(result["control_mode"], "background")
        self.assertEqual(result["status"], "sent_verified")
        focus.assert_not_called()
        self.activate_notice.assert_not_called()
        self.assertEqual(self.pg.actions, [])

    async def test_triple_click_uses_validated_screen_point_and_control_lock(self):
        result = await self.registry.call("triple_click", {
            "x": 20, "y": 30, "purpose": "select a paragraph",
        })
        self.assertTrue(result["ok"])
        self.assertEqual("triple_click", result["action"])
        self.assertIn(("click", 20, 30, "left"), self.pg.actions)
        self.assertEqual(3, self.controlled_calls[-1][2]["clicks"])
        before = list(self.pg.actions)
        with self.assertRaises(ValueError):
            await self.registry.call("triple_click", {"x": 101, "y": 30, "purpose": "out of bounds"})
        self.assertEqual(before, self.pg.actions)

    async def test_send_refuses_unconfirmed_input_without_visual_fallback(self):
        window = FakeWindow("ChatGPT", 92)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": True}), \
             patch.object(computer_module, "_uia_background_send", return_value={
                 "available": False, "partial": False, "reason": "没有 UIA 输入框",
             }), \
             patch.object(system_control, "focus_window_object") as focus:
            result = await self.registry.call("send_text_to_window", {
                "window": "Codex", "destination": "6S 临时设施施工专项方案", "text": "继续",
                "allow_visual_fallback": False,
            })
        self.assertFalse(result["ok"])
        self.assertEqual("input_not_confirmed", result["status"])
        self.assertTrue(result["retryable"])
        self.assertTrue(result["requires_reinspect"])
        self.assertIn("本次 UI Automation 检查暂未发现", result["error"])
        focus.assert_not_called()
        self.assertEqual([], self.pg.actions)

    async def test_generic_typing_is_blocked_in_message_routing_windows(self):
        with patch.object(computer_module, "_foreground_window_title", return_value="ChatGPT"):
            result = await self.registry.call("type_text", {"text": "continue"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "message_action_requires_send_tool")
        self.assertEqual(self.pg.actions, [])

    async def test_document_named_xiaoa_is_not_treated_as_chat(self):
        window = FakeWindow("小A自我介绍 - WPS Office", 95)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_foreground_window_handle", return_value=95), \
             patch.object(system_control, "_window_process_name", return_value=""):
            self.assertTrue(computer_module.is_document_text_input({"handle": 95}))

    async def test_enter_allowed_in_real_wps_document_named_xiaoa(self):
        window = FakeWindow("小A 自我介绍.docx - WPS Office", 95)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_foreground_window_handle", return_value=95), \
             patch.object(computer_module, "_foreground_window_title", return_value=window.title), \
             patch.object(system_control, "_window_process_name", return_value="wps.exe"):
            result = await self.registry.call("press_hotkey", {"keys": ["enter"], "handle": 95})
        self.assertTrue(result["ok"])
        self.assertIn(("hotkey", "enter"), self.pg.actions)

    async def test_fake_document_title_in_browser_does_not_bypass_chat_guard(self):
        window = FakeWindow("ChatGPT - 小A.docx - WPS Office", 95)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_foreground_window_handle", return_value=95), \
             patch.object(computer_module, "_foreground_window_title", return_value=window.title), \
             patch.object(system_control, "_window_process_name", return_value="chrome.exe"):
            result = await self.registry.call("press_hotkey", {"keys": ["enter"], "handle": 95})
        self.assertEqual("message_action_requires_send_tool", result["status"])
        self.assertEqual([], self.pg.actions)

    async def test_enter_is_blocked_in_message_routing_windows(self):
        with patch.object(computer_module, "_foreground_window_title", return_value="XiaoA"):
            result = await self.registry.call("press_hotkey", {"keys": ["enter"]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "message_action_requires_send_tool")
        self.assertEqual(self.pg.actions, [])

    async def test_codex_send_button_cannot_be_clicked_as_message_shortcut(self):
        with patch.object(computer_module, "_foreground_window_title", return_value="ChatGPT"):
            result = await self.registry.call("click_element", {"target_desc": "send"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "message_action_requires_send_tool")
        self.assertEqual(self.pg.actions, [])

    async def test_codex_requires_destination_before_any_send(self):
        window = FakeWindow("ChatGPT", 93)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_background_send") as background:
            result = await self.registry.call("send_text_to_window", {
                "window": "Codex", "text": "继续",
            })
        self.assertFalse(result["ok"])
        self.assertEqual("destination_required", result["status"])
        background.assert_not_called()

    async def test_named_window_missing_uia_automatically_uses_visual_fallback(self):
        window = FakeWindow("记事本", 94)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_click_named_control", return_value={
                 "ok": False, "status": "uia_control_not_found",
             }), \
             patch.object(system_control, "focus_window_object", return_value={"window": system_control.window_info(window)}), \
             patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (100, 80), "white")):
            result = await self.registry.call("click_element", {"target_desc": "打开", "handle": 94})
        self.assertTrue(result["ok"])
        self.assertEqual("foreground_exclusive", result["control_mode"])
        self.assertIn(("click", 10, 12, "left"), self.pg.actions)

    async def test_named_window_visual_fallback_can_be_disabled(self):
        window = FakeWindow("记事本", 94)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_click_named_control", return_value={
                 "ok": False, "status": "uia_control_not_found",
             }), \
             patch.object(system_control, "focus_window_object") as focus:
            result = await self.registry.call("click_element", {
                "target_desc": "打开", "handle": 94, "allow_visual_fallback": False,
            })
        self.assertFalse(result["ok"])
        focus.assert_not_called()
        self.assertEqual([], self.pg.actions)

    async def test_uia_success_does_not_wait_for_foreground_idle(self):
        window = FakeWindow("记事本", 94)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_click_named_control", return_value={
                 "ok": True, "name": "打开", "action": "invoke", "control_type": "ButtonControl",
             }), \
             patch.object(system_control, "wait_for_user_idle", new=AsyncMock(return_value=False)) as idle:
            result = await self.registry.call("click_element", {"target_desc": "打开", "handle": 94})
        self.assertTrue(result["ok"])
        idle.assert_not_awaited()
        self.activate_notice.assert_not_called()
        self.assertEqual([], self.pg.actions)

    async def test_uncertain_uia_click_never_falls_back(self):
        window = FakeWindow("记事本", 94)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_click_named_control", return_value={
                 "ok": False, "status": "uia_action_unconfirmed",
             }):
            result = await self.registry.call("click_element", {"target_desc": "打开", "handle": 94})
        self.assertEqual("uia_action_unconfirmed", result["status"])
        self.assertEqual([], self.pg.actions)
        self.assertEqual(0, self.llm.calls)

    async def test_send_button_and_keyboard_routes_share_duplicate_protection(self):
        window = FakeWindow("ChatGPT", 95)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": True}), \
             patch.object(computer_module, "_uia_background_send", return_value={
                 "available": True, "sent": True, "verified": True,
             }) as background:
            first = await self.registry.call("press_hotkey", {
                "keys": ["ctrl", "enter"], "window": "Codex", "destination": "项目会话", "text": "继续任务",
            })
            second = await self.registry.call("click_element", {
                "target_desc": "发送", "window": "Codex", "destination": "项目会话", "text": "继续任务",
            })
        self.assertEqual("sent_verified", first["status"])
        self.assertEqual("duplicate_suppressed", second["status"])
        background.assert_called_once()
        self.assertEqual([], self.pg.actions)

    async def test_generic_submit_with_content_still_requires_destination(self):
        window = FakeWindow("ChatGPT", 95)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_background_send") as background:
            for name, args in (("press_hotkey", {"keys": ["enter"]}),
                               ("click_element", {"target_desc": "发送"})):
                result = await self.registry.call(name, {**args, "window": "Codex", "text": "继续"})
                self.assertEqual("destination_required", result["status"])
        background.assert_not_called()
        self.assertEqual([], self.pg.actions)

    async def test_non_send_target_containing_continue_is_not_blocked(self):
        with patch.object(computer_module, "_foreground_window_title", return_value="ChatGPT"), \
             patch.object(computer_module, "_uia_click_named_control", return_value={
                 "ok": True, "name": "继续完善施工方案", "action": "invoke", "control_type": "ButtonControl",
             }):
            result = await self.registry.call("click_element", {"target_desc": "继续完善施工方案"})
        self.assertTrue(result["ok"])

    async def test_confirmed_destination_defaults_to_visual_input_and_button_send(self):
        window = FakeWindow("ChatGPT", 96)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": True}), \
             patch.object(computer_module, "_uia_background_send", return_value={"available": False}), \
             patch.object(computer_module, "_focus_semantic_text_input", return_value=False), \
             patch.object(system_control, "focus_window_object", return_value={"window": system_control.window_info(window)}), \
             patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (100, 80), "white")), \
             patch.object(computer_module, "wait_for_change", new=AsyncMock(return_value=(True, 0.2))):
            result = await self.registry.call("send_text_to_window", {
                "window": "Codex", "destination": "项目会话", "text": "继续任务", "submit_key": "button",
            })
        self.assertTrue(result["ok"])
        self.assertEqual(3, self.llm.calls)
        self.assertTrue(result["_observation_image"])
        self.assertEqual(2, sum(action[0] == "click" for action in self.pg.actions))
        self.assertEqual(["继续任务"], self.clipboard)
        self.assertFalse(any(action[0] == "press" for action in self.pg.actions))

    async def test_visual_destination_must_be_verified_before_input(self):
        window = FakeWindow("ChatGPT", 97)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={
                 "ok": False, "status": "uia_target_not_found",
             }), \
             patch.object(computer_module, "confirm_destination_in_image", new=AsyncMock(return_value=False)) as confirm, \
             patch.object(system_control, "focus_window_object", return_value={"window": system_control.window_info(window)}), \
             patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (100, 80), "white")), \
             patch.object(computer_module, "_uia_background_send") as background:
            result = await self.registry.call("send_text_to_window", {
                "window": "Codex", "destination": "项目会话", "text": "继续任务",
            })
        self.assertEqual("destination_not_confirmed", result["status"])
        self.assertEqual(2, confirm.await_count)  # Current chat, then one check of the unchanged selected view.
        self.assertEqual("foreground_exclusive", result["control_mode"])
        background.assert_not_called()
        self.assertEqual([], self.clipboard)
        self.assertEqual({}, computer_module._RECENT_SENDS)

    async def test_unchanged_unconfirmed_destination_returns_current_mapped_image(self):
        # The shared send route must work identically for any desktop application.
        for title in ("Team chat", "Customer support", "Mail"):
            with self.subTest(app=title):
                window = FakeWindow(title, 297, (10, 20, 90, 80))
                with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
                     patch.object(computer_module, "_uia_select_named_control", return_value={"ok": False, "status": "uia_target_not_found"}), \
                     patch.object(computer_module, "confirm_destination_in_image", new=AsyncMock(return_value=False)) as confirm, \
                     patch.object(computer_module.asyncio, "sleep", new=AsyncMock()), \
                     patch.object(system_control, "focus_window_object", return_value={"window": system_control.window_info(window)}), \
                     patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (90, 80), "white")), \
                     patch.object(computer_module, "_uia_background_send") as background:
                    result = await self.registry.call("send_text_to_window", {
                        "window": title, "destination": "Exact recipient", "text": "hello",
                    })
                self.assertEqual("destination_not_confirmed", result["status"])
                self.assertEqual(2, confirm.await_count, "one initial check and one after selection; unchanged retries must not query vision")
                self.assertFalse(result["sent"])
                self.assertTrue(result["requires_reinspect"])
                self.assertTrue(result["_observation_image"])
                self.assertEqual({"x": 10, "y": 20}, result["coordinate_mapping"]["origin"])
                self.assertTrue(Path(result["path"]).is_file())
                background.assert_not_called()
                self.assertEqual([], self.clipboard)
                self.assertFalse(any(action[0] in {"press", "hotkey", "write"} for action in self.pg.actions))

    async def test_uncertain_outgoing_message_is_not_requeried_or_resent_on_identical_pixels(self):
        window = FakeWindow("Generic messenger", 298)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": True}), \
             patch.object(computer_module, "_uia_background_send", return_value={"available": False}), \
             patch.object(computer_module, "_focus_semantic_text_input", return_value=True), \
             patch.object(system_control, "focus_window_object", return_value={"window": system_control.window_info(window)}), \
             patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (100, 80), "white")), \
             patch.object(computer_module, "wait_for_change", new=AsyncMock(return_value=(True, 0.2))), \
             patch.object(computer_module.asyncio, "sleep", new=AsyncMock()), \
             patch.object(computer_module, "confirm_destination_in_image", new=AsyncMock(return_value=False)) as confirm:
            args = {"window": window.title, "destination": "Exact recipient", "text": "hello"}
            result = await self.registry.call("send_text_to_window", args)
            retry = await self.registry.call("send_text_to_window", {**args, "submit_key": "ctrl+enter"})
        self.assertEqual("sent_unverified", result["status"])
        self.assertTrue(result["sent"])
        self.assertFalse(result["verified"])
        self.assertEqual(1, confirm.await_count)
        self.assertEqual("previous_send_unconfirmed", retry["status"])
        self.assertEqual(["hello"], self.clipboard)
        self.assertEqual(1, sum(action[0] == "press" for action in self.pg.actions))
        self.assertTrue(result["coordinate_mapping"])

    async def test_observation_disk_failure_does_not_turn_verified_send_into_retry(self):
        window = FakeWindow("Generic messenger", 299)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": True}), \
             patch.object(computer_module, "_uia_background_send", return_value={"available": False}), \
             patch.object(computer_module, "_focus_semantic_text_input", return_value=True), \
             patch.object(system_control, "focus_window_object", return_value={"window": system_control.window_info(window)}), \
             patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (100, 80), "white")), \
             patch.object(computer_module, "wait_for_change", new=AsyncMock(return_value=(True, 0.2))), \
             patch.object(computer_module, "save_screenshot_file", side_effect=OSError("disk full")), \
             patch.object(computer_module, "confirm_destination_in_image", new=AsyncMock(return_value=True)):
            args = {"window": window.title, "destination": "Exact recipient", "text": "hello"}
            result = await self.registry.call("send_text_to_window", args)
            retry = await self.registry.call("send_text_to_window", args)
        self.assertTrue(result["verified"])
        self.assertEqual("sent_verified", result["status"])
        self.assertEqual("duplicate_suppressed", retry["status"])
        self.assertTrue(result["observation_error"])
        self.assertTrue(result["_observation_image"])
        self.assertNotIn("path", result)
        self.assertEqual(["hello"], self.clipboard)
        self.assertEqual(1, sum(action[0] == "press" for action in self.pg.actions))

    async def test_visual_destination_verification_allows_send(self):
        window = FakeWindow("ChatGPT", 97)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={
                 "ok": False, "status": "uia_target_not_found",
             }), \
             patch.object(computer_module, "confirm_destination_in_image", new=AsyncMock(return_value=True)), \
             patch.object(system_control, "focus_window_object", return_value={"window": system_control.window_info(window)}), \
             patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (100, 80), "white")), \
             patch.object(computer_module, "_uia_background_send", return_value={
                 "available": True, "sent": True, "verified": True,
             }):
            result = await self.registry.call("send_text_to_window", {
                "window": "Codex", "destination": "项目会话", "text": "继续任务",
            })
        self.assertEqual("sent_verified", result["status"])
        self.assertEqual([], self.pg.actions, "already confirmed chat must not be reselected")
        self.assertEqual(0, self.llm.calls, "no redundant contact visual-location request")

    async def test_visual_selection_waits_for_chat_title_without_reclicking(self):
        window = FakeWindow("微信", 197)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": False, "status": "uia_target_not_found"}), \
             patch.object(computer_module, "confirm_destination_in_image", new=AsyncMock(side_effect=[False, False, True])) as confirm, \
             patch.object(computer_module.asyncio, "sleep", new=AsyncMock()), \
             patch.object(system_control, "focus_window_object", return_value={"window": system_control.window_info(window)}), \
             patch.object(ImageGrab, "grab", side_effect=[Image.new("RGB", (100, 80), color) for color in ['white', 'gray', 'gray', 'black', 'black']]), \
             patch.object(computer_module, "_uia_background_send", return_value={"available": True, "verified": True}) as background:
            result = await self.registry.call("send_text_to_window", {"window": "微信", "destination": "测试联系人", "text": "测试"})
        self.assertTrue(result["ok"])
        self.assertEqual(3, confirm.await_count)
        self.assertEqual(1, sum(action[0] == "click" for action in self.pg.actions))
        background.assert_called_once()

    async def test_minimized_window_can_send_through_background_uia(self):
        window = FakeWindow("微信", 198, (-32000, -32000, 100, 80))
        window.isMinimized = True
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": True}), \
             patch.object(computer_module, "_uia_background_send", return_value={"available": True, "verified": True}), \
             patch.object(system_control, "focus_window_object") as focus:
            result = await self.registry.call("send_text_to_window", {"window": "微信", "destination": "测试联系人", "text": "测试"})
        self.assertTrue(result["ok"])
        self.assertEqual("background", result["control_mode"])
        focus.assert_not_called()
        self.assertEqual([], self.pg.actions)

    async def test_restored_window_uses_new_bounds_for_visual_confirmation(self):
        window = FakeWindow("微信", 199, (-32000, -32000, 100, 80))
        def restore(target, **kwargs):
            target.left, target.top = 0, 10
            return {"window": system_control.window_info(target)}
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": False, "status": "uia_target_not_found"}), \
             patch.object(computer_module, "confirm_destination_in_image", new=AsyncMock(return_value=True)), \
             patch.object(system_control, "focus_window_object", side_effect=restore), \
             patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (100, 80), "white")) as grab, \
             patch.object(computer_module, "_uia_background_send", return_value={"available": True, "verified": True}):
            result = await self.registry.call("send_text_to_window", {"window": "微信", "destination": "测试联系人", "text": "测试"})
        self.assertTrue(result["ok"])
        self.assertEqual((0, 10, 100, 90), grab.call_args.kwargs["bbox"])

    async def test_late_message_bubble_reobserves_without_resending(self):
        window = FakeWindow("微信", 200)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": True}), \
             patch.object(computer_module, "_uia_background_send", return_value={"available": False}), \
             patch.object(computer_module, "_focus_semantic_text_input", return_value=True), \
             patch.object(system_control, "focus_window_object", return_value={"window": system_control.window_info(window)}), \
             patch.object(ImageGrab, "grab", side_effect=[Image.new("RGB", (100, 80), color) for color in ['white', 'gray', 'black']]), \
             patch.object(computer_module, "wait_for_change", new=AsyncMock(return_value=(True, 0.2))), \
             patch.object(computer_module.asyncio, "sleep", new=AsyncMock()), \
             patch.object(computer_module, "confirm_destination_in_image", new=AsyncMock(side_effect=[False, True])) as confirm:
            args = {"window": "微信", "destination": "测试联系人", "text": "测试"}
            result = await self.registry.call("send_text_to_window", args)
            retry = await self.registry.call("send_text_to_window", {**args, "submit_key": "ctrl+enter"})
        self.assertEqual("sent_verified", result["status"])
        self.assertEqual("duplicate_suppressed", retry["status"])
        self.assertEqual(2, confirm.await_count)
        self.assertEqual(["测试"], self.clipboard)
        self.assertEqual(1, sum(action[0] == "press" for action in self.pg.actions))

    async def test_slow_vision_still_observes_later_bubble_without_resending(self):
        window = FakeWindow("微信", 203)
        clock = [100.0]
        async def confirm(*args, **kwargs):
            clock[0] += 20.0  # A successful but slow vision request, not a UI wait.
            return clock[0] >= 140.0
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "time", SimpleNamespace(monotonic=lambda: clock[0])), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": True}), \
             patch.object(computer_module, "_uia_background_send", return_value={"available": False}), \
             patch.object(computer_module, "_focus_semantic_text_input", return_value=True), \
             patch.object(system_control, "focus_window_object", return_value={"window": system_control.window_info(window)}), \
             patch.object(ImageGrab, "grab", side_effect=[Image.new("RGB", (100, 80), color) for color in ['white', 'gray', 'black']]) as grab, \
             patch.object(computer_module, "wait_for_change", new=AsyncMock(return_value=(True, 0.2))), \
             patch.object(computer_module.asyncio, "sleep", new=AsyncMock()), \
             patch.object(computer_module, "confirm_destination_in_image", new=AsyncMock(side_effect=confirm)) as verify:
            result = await self.registry.call("send_text_to_window", {
                "window": "微信", "destination": "测试联系人", "text": "测试",
            })
        self.assertEqual("sent_verified", result["status"])
        self.assertEqual(2, verify.await_count)
        self.assertGreaterEqual(grab.call_count, 3)  # Before sending + two fresh observations.
        self.assertEqual(["测试"], self.clipboard)
        self.assertEqual(1, sum(action[0] == "press" for action in self.pg.actions))

    async def test_vision_gateway_error_is_not_reported_as_missing_contact(self):
        window = FakeWindow("微信", 201)
        self.current_llm = SimpleNamespace(vision=AsyncMock(side_effect=RuntimeError("mock gateway failure")))
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": False, "status": "uia_target_not_found"}), \
             patch.object(system_control, "focus_window_object", return_value={"window": system_control.window_info(window)}), \
             patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (100, 80), "white")), \
             patch.object(computer_module, "_uia_background_send") as background:
            result = await self.registry.call("send_text_to_window", {"window": "微信", "destination": "测试联系人", "text": "测试"})
        self.assertFalse(result["sent"])
        self.assertEqual("RuntimeError", result["verification_error"])
        self.assertEqual("mock gateway failure", result["verification_detail"])
        self.assertIn("看图核验请求异常", result["verification_reason"])
        self.current_llm.vision.assert_awaited_once()
        background.assert_not_called()
        self.assertEqual([], self.clipboard)

    async def test_ambiguous_or_uncertain_destination_never_uses_visual_guess(self):
        window = FakeWindow("ChatGPT", 98)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_background_send") as background:
            for status in ("uia_target_ambiguous", "uia_target_unconfirmed"):
                with patch.object(computer_module, "_uia_select_named_control", return_value={"ok": False, "status": status}):
                    result = await self.registry.call("send_text_to_window", {
                        "window": "Codex", "destination": "项目会话", "text": "继续任务",
                    })
                self.assertEqual(status, result["status"])
        background.assert_not_called()
        self.assertEqual([], self.pg.actions)
        self.assertEqual(0, self.llm.calls)

    async def test_background_partial_send_cannot_retry_by_changing_submit_method(self):
        window = FakeWindow("ChatGPT", 99)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": True}), \
             patch.object(computer_module, "_uia_background_send", return_value={
                 "available": False, "partial": True,
             }) as background:
            args = {"window": "Codex", "destination": "项目会话", "text": "继续任务"}
            first = await self.registry.call("send_text_to_window", {**args, "submit_key": "enter"})
            second = await self.registry.call("send_text_to_window", {**args, "submit_key": "button"})
        self.assertEqual("background_partial", first["status"])
        self.assertEqual("previous_send_unconfirmed", second["status"])
        background.assert_called_once()
        self.assertEqual([], self.pg.actions)

    async def test_visual_coords_scale_back_to_real_window_with_offset(self):
        window = FakeWindow("记事本", 94, (100, 50, 1920, 1080))
        self.current_llm = FakeLLM((640, 360))
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "screen_bounds", return_value=(0, 0, 2200, 1200)), \
             patch.object(computer_module, "_uia_click_named_control", return_value={"ok": False, "status": "uia_control_not_found"}), \
             patch.object(system_control, "focus_window_object", return_value={"window": system_control.window_info(window)}), \
             patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (1920, 1080), "white")), \
             patch.object(computer_module, "_SCREENSHOT_ESCALATION", ((1280, 60),)):
            result = await self.registry.call("click_element", {"target_desc": "打开", "handle": 94})
        self.assertTrue(result["ok"])
        self.assertIn(("click", 1060, 590, "left"), self.pg.actions)

    async def test_visual_coords_outside_target_window_never_click(self):
        window = FakeWindow("记事本", 94)
        self.current_llm = FakeLLM((100, 12))
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_click_named_control", return_value={"ok": False, "status": "uia_control_not_found"}), \
             patch.object(system_control, "focus_window_object", return_value={"window": system_control.window_info(window)}), \
             patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (100, 80), "white")):
            result = await self.registry.call("click_element", {"target_desc": "打开", "handle": 94})
        self.assertEqual("visual_coordinate_invalid", result["status"])
        self.assertIn("_observation_image", result)
        self.assertEqual({"width": 100, "height": 80}, result["coordinate_mapping"]["observation_size"])
        self.assertEqual([], self.pg.actions)

    async def test_changed_screen_without_sent_message_does_not_verify_or_repeat(self):
        window = FakeWindow("ChatGPT", 96)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": True}), \
             patch.object(computer_module, "_uia_background_send", return_value={"available": False}), \
             patch.object(computer_module, "_focus_semantic_text_input", return_value=True), \
             patch.object(system_control, "focus_window_object", return_value={"window": system_control.window_info(window)}), \
             patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (100, 80), "white")), \
             patch.object(computer_module, "wait_for_change", new=AsyncMock(return_value=(True, 0.2))), \
             patch.object(computer_module, "confirm_destination_in_image", new=AsyncMock(return_value=False)):
            args = {"window": "Codex", "destination": "项目会话", "text": "继续任务"}
            first = await self.registry.call("send_text_to_window", args)
            retry = await self.registry.call("send_text_to_window", {**args, "submit_key": "ctrl+enter"})
        self.assertFalse(first["ok"])
        self.assertTrue(first["screen_changed"])
        self.assertEqual("sent_unverified", first["status"])
        self.assertEqual("previous_send_unconfirmed", retry["status"])
        self.assertEqual(["继续任务"], self.clipboard)

    async def test_gateway_error_after_submission_keeps_send_uncertain_and_blocked(self):
        window = FakeWindow("微信", 202)
        self.current_llm = SimpleNamespace(vision=AsyncMock(side_effect=RuntimeError("mock gateway failure")))
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": True}), \
             patch.object(computer_module, "_uia_background_send", return_value={"available": False}), \
             patch.object(computer_module, "_focus_semantic_text_input", return_value=True), \
             patch.object(system_control, "focus_window_object", return_value={"window": system_control.window_info(window)}), \
             patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (100, 80), "white")), \
             patch.object(computer_module, "wait_for_change", new=AsyncMock(return_value=(True, 0.2))):
            args = {"window": "微信", "destination": "测试联系人", "text": "测试"}
            result = await self.registry.call("send_text_to_window", args)
            retry = await self.registry.call("send_text_to_window", {**args, "submit_key": "ctrl+enter"})
        self.assertEqual("sent_unverified", result["status"])
        self.assertTrue(result["sent"])
        self.assertFalse(result["verified"])
        self.assertEqual("mock gateway failure", result["verification_detail"])
        self.assertEqual("previous_send_unconfirmed", retry["status"])
        self.current_llm.vision.assert_awaited_once()
        self.assertEqual(["测试"], self.clipboard)
        self.assertEqual(1, sum(action[0] == "press" for action in self.pg.actions))

    async def test_missing_button_after_paste_preserves_draft_without_retry(self):
        window = FakeWindow("ChatGPT", 96)
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": True}), \
             patch.object(computer_module, "_uia_background_send", return_value={"available": False}), \
             patch.object(computer_module, "_focus_semantic_text_input", return_value=True), \
             patch.object(computer_module, "locate_in_image", new=AsyncMock(return_value=None)), \
             patch.object(system_control, "focus_window_object", return_value={"window": system_control.window_info(window)}), \
             patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (100, 80), "white")):
            args = {"window": "Codex", "destination": "项目会话", "text": "继续任务", "submit_key": "button"}
            result = await self.registry.call("send_text_to_window", args)
            retry = await self.registry.call("send_text_to_window", args)
        self.assertEqual("send_button_not_found", result["status"])
        self.assertFalse(result["sent"])
        self.assertTrue(result["drafted"])
        self.assertEqual("previous_send_unconfirmed", retry["status"])
        self.assertEqual(["继续任务"], self.clipboard)
        self.assertFalse(any(action[0] in {"click", "press"} for action in self.pg.actions))

    async def test_visual_failure_is_shared_across_click_send_and_hotkey_without_repeat(self):
        # Any application's title uses the same visual locator and error recovery.
        window = FakeWindow("Generic desktop app", 96, (100, 80, 957, 782))
        self.current_llm = FakeLLM((690, 850))
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "screen_bounds", return_value=(0, 0, 1920, 1080)), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": True}), \
             patch.object(computer_module, "_uia_background_send", return_value={"available": False}), \
             patch.object(computer_module, "_uia_click_named_control", return_value={"ok": False, "status": "uia_control_not_found"}), \
             patch.object(computer_module, "_focus_semantic_text_input", return_value=False), \
             patch.object(system_control, "focus_window_object"), \
             patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (957, 782), "white")), \
             patch.object(computer_module, "save_screenshot_file", return_value="recovery.jpg"):
            args = {"window": window.title, "destination": "Target", "text": "draft"}
            first = await self.registry.call("send_text_to_window", args)
            second = await self.registry.call("press_hotkey", {"keys": ["enter"], **args})
            clicked = await self.registry.call("click_element", {"handle": 96, "target_desc": "another button"})
            self.assertEqual("visual_coordinate_invalid", first["status"])
            self.assertTrue(second["same_observation"])
            self.assertFalse(clicked["ok"])
            self.assertEqual(2, self.current_llm.calls)  # send aliases share the failed input query.
            recovered = await self.registry.call("click_coordinate", {
                "x": 200, "y": 300, "purpose": "inspect control", "screenshot_path": first["path"]})
            self.assertEqual((300, 380), (recovered["x"], recovered["y"]))
            self.assertEqual([("click", 300, 380, "left")], self.pg.actions)
            self.assertEqual([], self.clipboard)

    async def test_visual_failure_expires_and_changed_image_or_model_is_requeried(self):
        window = FakeWindow("Generic app", 94)
        first_model = FakeLLM((100, 12))
        first_model.cfg = SimpleNamespace(chat_model="model-a")
        self.current_llm = first_model
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_click_named_control", return_value={"ok": False, "status": "uia_control_not_found"}), \
             patch.object(system_control, "focus_window_object"), \
             patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (100, 80), "white")) as grab, \
             patch.object(computer_module, "save_screenshot_file", return_value="recovery.jpg"), \
             patch.object(computer_module, "time", SimpleNamespace(monotonic=Mock(return_value=100))) as clock:
            for _ in range(2):
                result = await self.registry.call("click_element", {"target_desc": "Open", "handle": 94})
            self.assertTrue(result["same_observation"])
            self.assertEqual(1, first_model.calls)
            grab.return_value = Image.new("RGB", (100, 80), "black")
            await self.registry.call("click_element", {"target_desc": "Open", "handle": 94})
            self.assertEqual(2, first_model.calls)
            clock.monotonic.return_value = 111
            await self.registry.call("click_element", {"target_desc": "Open", "handle": 94})
            self.assertEqual(3, first_model.calls)
            first_model.cfg.chat_model = "model-b"
            await self.registry.call("click_element", {"target_desc": "Open", "handle": 94})
            self.assertEqual(4, first_model.calls)
            self.current_llm = FakeLLM((10, 12))
            result = await self.registry.call("click_element", {"target_desc": "Open", "handle": 94})
            self.assertTrue(result["ok"])

    async def test_visual_failure_and_success_share_scaled_negative_monitor_mapping(self):
        window = FakeWindow("Generic app", 94, (-1000, -120, 600, 400))
        self.current_llm = FakeLLM((300, 20))  # Outside the 300x200 observation.
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "screen_bounds", return_value=(-1200, -200, 1920, 1080)), \
             patch.object(computer_module, "_uia_click_named_control", return_value={"ok": False, "status": "uia_control_not_found"}), \
             patch.object(system_control, "focus_window_object"), \
             patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (600, 400), "white")), \
             patch.object(computer_module, "compress_screenshot", return_value=(b"scaled-image", 300, 200)), \
             patch.object(computer_module, "save_screenshot_file", return_value="recovery.jpg"):
            failed = await self.registry.call("click_element", {"target_desc": "Open", "handle": 94})
            self.assertEqual({"x": -1000, "y": -120}, failed["coordinate_mapping"]["origin"])
            self.assertEqual([], self.pg.actions)
            recovered = await self.registry.call("click_coordinate", {
                "x": 30, "y": 40, "purpose": "inspect control", "screenshot_path": failed["path"]})
            self.assertEqual((-940, -40), (recovered["x"], recovered["y"]))
            self.current_llm = FakeLLM((30, 40))
            for _ in range(2):
                valid = await self.registry.call("click_element", {"target_desc": "Open", "handle": 94})
                self.assertEqual((-940, -40), (valid["x"], valid["y"]))
            self.assertEqual(2, self.current_llm.calls)  # Successful coordinates are never cached.

    async def test_invalid_visual_send_button_preserves_draft_and_never_submits_again(self):
        window = FakeWindow("Generic app", 94)
        self.current_llm = FakeLLM((100, 12))
        with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
             patch.object(computer_module, "_uia_select_named_control", return_value={"ok": True}), \
             patch.object(computer_module, "_uia_background_send", return_value={"available": False}), \
             patch.object(computer_module, "_focus_semantic_text_input", return_value=True), \
             patch.object(system_control, "focus_window_object"), \
             patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (100, 80), "white")):
            args = {"window": window.title, "destination": "Target", "text": "draft", "submit_key": "button"}
            result = await self.registry.call("send_text_to_window", args)
            repeated = await self.registry.call("press_hotkey", {**args, "keys": ["enter"]})
        self.assertEqual("visual_coordinate_invalid", result["status"])
        self.assertTrue(result["drafted"])
        self.assertFalse(result["sent"])
        self.assertTrue(result["_observation_image"])
        self.assertEqual("previous_send_unconfirmed", repeated["status"])
        self.assertEqual(["draft"], self.clipboard)
        self.assertFalse(any(action[0] in {"click", "press"} for action in self.pg.actions))

    async def test_visual_confirmation_rejects_malformed_or_nonboolean_reply(self):
        llm = SimpleNamespace(vision=AsyncMock())
        for reply in ('{"confirmed": "true"}', 'true', '{"x": 10}', '[]', 'not json'):
            llm.vision.return_value = reply
            self.assertFalse(await computer_module.confirm_destination_in_image(llm, b"image", "项目会话"))
        llm.vision.return_value = '```json\n{"confirmed": true}\n```'
        self.assertTrue(await computer_module.confirm_destination_in_image(llm, b"image", "项目会话"))

    async def test_visual_input_point_reuse_is_same_frame_only_and_skips_unusable_uia(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                window = FakeWindow("微信", 401 + changed, (0, 0, 100, 80))
                llm = SimpleNamespace(vision=AsyncMock(return_value='{"confirmed":true,"input_x":12,"input_y":9,"coordinate_space":"pixels"}'))
                frames = [Image.new("RGB", (100, 80), "white"),
                          Image.new("RGB", (100, 80), "black" if changed else "white")]
                self.pg.actions.clear()
                with patch.dict(sys.modules, {"pygetwindow": fake_window_module([window])}), \
                     patch.object(computer_module, "_uia_select_named_control", return_value={
                         "ok": False, "status": "uia_selection_unavailable", "uia_tree_unusable": True}), \
                     patch.object(computer_module, "_uia_background_send") as background, \
                     patch.object(computer_module, "_focus_semantic_text_input") as semantic, \
                     patch.object(system_control, "focus_window_object", return_value={"window": system_control.window_info(window)}), \
                     patch.object(computer_module, "_foreground_window_handle", return_value=window._hWnd), \
                     patch.object(ImageGrab, "grab", side_effect=frames + [frames[-1]] * 4), \
                     patch.object(computer_module, "locate_in_image", new=AsyncMock(return_value=(20, 30))) as locate:
                    previous = self.current_llm
                    self.current_llm = llm
                    try:
                        result = await self.registry.call("send_text_to_window", {
                            "window": "微信", "destination": "target", "text": "draft", "submit_key": "none"})
                    finally:
                        self.current_llm = previous
                self.assertTrue(result["ok"], result)
                background.assert_not_called()
                semantic.assert_not_called()
                self.assertEqual(int(changed), locate.await_count)
                self.assertEqual((20, 30) if changed else (12, 9), self.pg.actions[0][1:3])

    async def test_visual_confirmation_only_exports_valid_input_point_when_confirmed(self):
        llm = SimpleNamespace(vision=AsyncMock(return_value='{"confirmed":true,"input_x":12,"input_y":9,"coordinate_space":"pixels"}'))
        target = {"width": 100, "height": 80}
        self.assertTrue(await computer_module.confirm_destination_in_image(
            llm, b"image", "项目会话", input_desc="消息输入框", input_target=target,
        ))
        self.assertEqual({"width": 100, "height": 80, "x": 12, "y": 9, "coordinate_space": "pixels"}, target)
        llm.vision.return_value = '{"confirmed":false,"input_x":1,"input_y":2,"coordinate_space":"pixels"}'
        target = {"width": 100, "height": 80}
        self.assertFalse(await computer_module.confirm_destination_in_image(
            llm, b"image", "项目会话", input_desc="消息输入框", input_target=target,
        ))
        self.assertNotIn("x", target)
        for fields in ('"input_x":true,"input_y":9,"coordinate_space":"pixels"',
                       '"input_x":100,"input_y":9,"coordinate_space":"pixels"',
                       '"input_x":12,"input_y":9,"coordinate_space":"normalized_1000"'):
            target = {"width": 100, "height": 80}
            llm.vision.return_value = '{"confirmed":true,' + fields + '}'
            self.assertTrue(await computer_module.confirm_destination_in_image(
                llm, b"image", "target", input_desc="editor", input_target=target))
            self.assertNotIn("x", target)

    async def test_rejected_risk_authorization_cannot_be_bypassed_by_submit_route(self):
        state = ConversationApproval()
        confirm = AsyncMock(return_value=False)
        events = []
        for name, extra in (("send_text_to_window", {}), ("click_element", {"target_desc": "发送"}),
                            ("press_hotkey", {"keys": ["enter"]})):
            args = {"window": "Codex", "destination": "项目会话", "text": "确认支付 100 元", **extra}
            agent = Agent(OneCallLLM(name, __import__("json").dumps(args)), self.registry,
                          AgentConfig(confirm_before_action=True), confirm_fn=confirm, approval_state=state)
            events.extend([event async for event in agent.run("发送指定内容", approval_mode="risk")])
        confirm.assert_awaited_once()
        self.assertEqual(1, sum(event.type == "confirm_needed" for event in events))
        self.assertEqual([], self.pg.actions)
        self.assertEqual([], self.clipboard)

    async def test_active_user_cancels_foreground_fallback(self):
        with patch.object(system_control, "wait_for_user_idle", new=AsyncMock(return_value=False)):
            result = await self.registry.call("click_coordinate", {
                "x": 20, "y": 30, "purpose": "选中一行",
            })
        self.assertEqual(result["control_mode"], "needs_foreground")
        self.assertEqual(result["status"], "user_active")
        self.assertEqual(self.pg.actions, [])


class SystemToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_remember_app_persists_only_resolved_target_and_opens_it(self):
        registry = ToolRegistry()
        learned = {"old": "old.exe"}
        with TemporaryDirectory() as tmp:
            target = (Path(tmp) / "CAD.exe").resolve()
            target.touch()
            with patch.object(system_control, "_IS_WINDOWS", True), \
                 patch.object(system_control, "_load_learned", return_value=dict(learned)), \
                 patch.object(system_control, "_save_learned") as save, \
                 patch.object(system_control, "_find_shortcut", return_value=None), \
                 patch.object(system_control.os, "startfile", create=True) as startfile:
                system_control.register_tools(registry)
                result = await registry.call("remember_app", {
                    "name": "My CAD", "target": str(target),
                })
                missing = await registry.call("remember_app", {
                    "name": "Missing", "target": "not-installed-app",
                })
        self.assertIn("记住了", result)
        save.assert_called_once_with({"old": "old.exe", "my cad": str(target)})
        startfile.assert_called_once_with(str(target))
        self.assertIn("没找到", missing)

    async def test_open_url_normalizes_scheme_and_surfaces_browser_failure(self):
        registry = ToolRegistry()
        with patch.object(system_control, "_IS_WINDOWS", True), \
             patch.object(system_control.webbrowser, "open", return_value=True) as open_browser:
            system_control.register_tools(registry)
            bare = await registry.call("open_url", {"url": "example.com/path"})
            secure = await registry.call("open_url", {"url": "https://example.org"})
        self.assertIn("https://example.com/path", bare)
        self.assertIn("https://example.org", secure)
        self.assertEqual([
            (("https://example.com/path",), {}),
            (("https://example.org",), {}),
        ], open_browser.call_args_list)

        with patch.object(system_control.webbrowser, "open", side_effect=RuntimeError("no browser")):
            with self.assertRaisesRegex(RuntimeError, "no browser"):
                await registry.call("open_url", {"url": "https://failed.example"})

    async def test_office_launch_falls_back_only_when_missing(self):
        for name, primary, fallback in (("Word", "winword", "wps"), ("Excel", "excel", "et"), ("PPT", "powerpnt", "wpp")):
            registry = ToolRegistry()
            with patch.object(system_control, "_IS_WINDOWS", True), \
                 patch.object(system_control, "_load_learned", return_value={}), \
                 patch.dict(sys.modules, {"pygetwindow": fake_window_module([])}), \
                 patch.object(system_control.os, "startfile", create=True, side_effect=[FileNotFoundError(), None]) as startfile:
                system_control.register_tools(registry)
                result = await registry.call("launch_app", {"name": name})
            self.assertIn("使用 WPS", result)
            self.assertEqual([primary, fallback], [call.args[0] for call in startfile.call_args_list])
        registry = ToolRegistry()
        with patch.object(system_control, "_IS_WINDOWS", True), \
             patch.object(system_control, "_load_learned", return_value={}), \
             patch.object(system_control, "_find_shortcut", return_value=None), \
             patch.dict(sys.modules, {"pygetwindow": fake_window_module([])}), \
             patch.object(system_control.os, "startfile", create=True, side_effect=PermissionError("denied")) as startfile:
            system_control.register_tools(registry)
            await registry.call("launch_app", {"name": "Word"})
        startfile.assert_called_once_with("winword")

    def test_excel_window_alias_does_not_match_generic_word_wps_window(self):
        queries = system_control.window_queries("excel")
        self.assertIn("wps 表格", queries)
        self.assertNotIn("wps office", queries)
        self.assertFalse(any(query in "小A.docx - WPS Office".lower() for query in queries))

    async def test_launch_app_does_not_restart_an_open_cad_window(self):
        autocad = FakeWindow("Autodesk AutoCAD 2014 - Drawing1.dwg", 10)
        registry = ToolRegistry()
        with patch.object(system_control, "_IS_WINDOWS", True), \
             patch.dict(sys.modules, {"pygetwindow": fake_window_module([autocad])}), \
             patch.object(system_control.os, "startfile", create=True) as startfile:
            system_control.register_tools(registry)
            result = await registry.call("launch_app", {"name": "CAD"})
        self.assertIn("检测到已经打开", result)
        startfile.assert_not_called()
        self.assertFalse(system_control.owned_application("autocad"))

    async def test_launch_app_records_only_a_new_autocad_process(self):
        registry = ToolRegistry()
        with patch.object(system_control, "_IS_WINDOWS", True), \
             patch.dict(sys.modules, {"pygetwindow": fake_window_module([])}), \
             patch.object(system_control.os, "startfile", create=True), \
             patch.object(system_control, "_process_ids_by_name", side_effect=[set(), {4242}]), \
             patch.object(system_control, "_APP_ALIASES", {"cad": r"C:\\AutoCAD\\acad.exe"}):
            system_control.register_tools(registry)
            result = await registry.call("launch_app", {"name": "CAD"})
        self.assertIn("已启动", result)
        self.assertEqual(4242, system_control.owned_application_pid("autocad"))
        system_control.clear_owned_application("autocad")

    async def test_window_list_and_exact_focus(self):
        chatgpt = FakeWindow("ChatGPT", 9)
        gw = fake_window_module([FakeWindow("", 1), chatgpt])
        registry = ToolRegistry()
        with patch.object(system_control, "_IS_WINDOWS", True), patch.dict(sys.modules, {"pygetwindow": gw}):
            system_control.register_tools(registry)
            listed = await registry.call("list_windows", {"title": "Codex"})
            control = object()
            with patch.object(system_control, "acquire_control", new=AsyncMock(return_value=control)), \
                 patch.object(system_control, "release_control", return_value=True) as release, \
                 patch.object(system_control, "wait_for_user_idle", new=AsyncMock(return_value=True)), \
                 patch.object(system_control, "run_controlled", new=AsyncMock(return_value={
                     "focused": True, "semantic": True,
                     "window": system_control.window_info(chatgpt),
                 })) as run_controlled:
                focused = await registry.call("focus_window", {"handle": 9, "exact": True})
            run_controlled.assert_awaited_once_with(
                control, system_control.focus_window_object, chatgpt, maximize=False,
            )
            release.assert_called_once_with(control)
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["windows"][0]["title"], "ChatGPT")
        self.assertTrue(focused["ok"])
        self.assertEqual(focused["control_mode"], "foreground_exclusive")
        self.assertEqual(focused["window"]["handle"], 9)

    async def test_focus_window_keeps_event_loop_responsive(self):
        chatgpt = FakeWindow("ChatGPT", 12)
        started = threading.Event()
        release = threading.Event()

        def blocking_focus(window, *, maximize=False):
            started.set()
            release.wait(2)
            return {
                "focused": True, "semantic": False,
                "window": system_control.window_info(window),
            }

        registry = ToolRegistry()
        with patch.object(system_control, "_IS_WINDOWS", True), \
             patch.dict(sys.modules, {"pygetwindow": fake_window_module([chatgpt])}), \
             patch.object(system_control, "wait_for_user_idle", new=AsyncMock(return_value=True)), \
             patch.object(system_control, "focus_window_object", side_effect=blocking_focus):
            system_control.register_tools(registry)
            task = asyncio.create_task(registry.call("focus_window", {"handle": 12}))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                began = time.perf_counter()
                await asyncio.sleep(0.02)
                self.assertLess(time.perf_counter() - began, 0.5)
            finally:
                release.set()
                result = await asyncio.wait_for(task, 1)

        self.assertTrue(result["ok"])


class ScreenshotCompressionTests(unittest.TestCase):
    def test_compress_screenshot_reduces_high_entropy_image(self):
        from backend.core.agent import estimate_text_tokens

        # 高熵噪声图(压缩率极差,PNG 会到几 MB 级别)
        noise = Image.effect_noise((1920, 1080), 100).convert("RGB")
        data, width, height = computer_module.compress_screenshot(noise)
        self.assertIsNotNone(data)
        tokens = estimate_text_tokens(base64.b64encode(data).decode())
        # 截图预算约为 96000 上下文窗口的 40%
        self.assertLess(tokens, 40000)

    def test_compress_screenshot_outputs_jpeg_and_downscales(self):
        img = Image.new("RGB", (3840, 2160), (255, 0, 0))
        data, width, height = computer_module.compress_screenshot(img)
        self.assertIsNotNone(data)
        self.assertTrue(data.startswith(b"\xff\xd8"))  # JPEG magic
        self.assertEqual(1920, width)  # Auto detail preserves readable full-HD observations.
        self.assertEqual(1080, height)

    def test_compact_fits_compressed_screenshot(self):
        from backend.core.agent import compact_for_limit

        noise = Image.effect_noise((1920, 1080), 100).convert("RGB")
        data, _, _ = computer_module.compress_screenshot(noise)
        b64 = base64.b64encode(data).decode()
        messages = [{"role": "system", "content": "系统提示" * 200}]
        for index in range(20):
            messages.append({"role": "user", "content": f"第{index}轮用户消息"})
            messages.append({"role": "assistant", "content": f"第{index}轮回复"})
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": "看屏幕"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        })
        _result, _count, fitted = compact_for_limit(messages, 96000)
        self.assertTrue(fitted)

    def test_compact_replaces_old_image_keeps_latest(self):
        from backend.core.agent import compact_context_messages

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [
                {"type": "text", "text": "第一次截图"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
            ]},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [
                {"type": "text", "text": "第二次截图"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,BBBB"}},
            ]},
        ]
        result, compacted = compact_context_messages(messages, recent_rounds=3)
        self.assertEqual(1, compacted)
        self.assertIsInstance(result[1]["content"], str)
        self.assertIn("较早的截图已省略", result[1]["content"])
        self.assertIsInstance(result[3]["content"], list)  # 最新保留
        self.assertEqual("image_url", result[3]["content"][1]["type"])

    def test_compact_replaces_old_user_attachment(self):
        """43.6:用户附件贴的旧图,超出最近 N 轮就换成文字占位(不像工具截图那样保留最新)。"""
        from backend.core.agent import compact_context_messages

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [
                {"type": "text", "text": "帮我看看\n\n用户上传的附件：\n- a.png"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "第二轮"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "第三轮"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [
                {"type": "text", "text": "再看这个\n\n用户上传的附件：\n- b.png"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}},
            ]},
        ]
        result, compacted = compact_context_messages(messages, recent_rounds=3)
        self.assertGreaterEqual(compacted, 1)
        self.assertIn("用户此前附了一张图片", result[1]["content"])
        self.assertIsInstance(result[7]["content"], list, "最近那张用户附件保留")
        self.assertEqual("image_url", result[7]["content"][1]["type"])


if __name__ == "__main__":
    unittest.main()
