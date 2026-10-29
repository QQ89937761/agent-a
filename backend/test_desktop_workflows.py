"""Production desktop-handler integration with simulated OS/UIA boundaries.

These tests are not acceptance tests of the named commercial applications.
"""
import json
import asyncio
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import nullcontext

from PIL import Image, ImageGrab

from backend.core.agent import Agent
from backend.core.config import AgentConfig
from backend.modules.computer import computer_module as computer, system_control
from backend.test_computer_control import FakeWindow, fake_window_module
from backend import test_computer_control as fixtures


class DesktopWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.ComputerToolsTests.asyncSetUp(self)
        self.window = FakeWindow("Isolated evaluation", 91, (-100, 20, 800, 600))
        self.window.isActive = True
        self.controls = []
        self.invocations = []
        self.root = SimpleNamespace(Name="Main", ControlTypeName="WindowControl", GetChildren=lambda: self.controls)
        auto = types.ModuleType("uiautomation")
        auto.ControlFromHandle = lambda _: self.root
        def focused(nodes):
            for control in nodes:
                if control.HasKeyboardFocus:
                    return control
                child = focused(control.GetChildren())
                if child is not None:
                    return child
            return None
        auto.GetFocusedControl = lambda: focused(self.controls)
        auto.UIAutomationInitializerInThread = nullcontext
        auto.PatternId = SimpleNamespace(InvokePattern=1, SelectionItemPattern=2, TogglePattern=3, ValuePattern=4)
        async def inspect(handle, **options):
            options.pop("query_timeout", None)
            return computer._uia_snapshot(handle, **options)
        for mock in (patch.dict("sys.modules", {"pygetwindow": fake_window_module([self.window]), "uiautomation": auto}),
                     patch.object(computer, "_foreground_window_handle", return_value=91),
                     patch("backend.modules.computer.uia_query.UIAReader.query", side_effect=inspect),
                     patch.object(computer, "screen_bounds", return_value=(-100, 0, 1500, 1000)),
                     patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (800, 600), "white")),
                     patch.object(computer, "save_screenshot_file", return_value="evaluation.jpg")):
            mock.start()
            self.addCleanup(mock.stop)
        async def run_input(_control, function, /, *args, **kwargs):
            return await asyncio.to_thread(system_control._run_controlled_input, function, args, kwargs)
        mock = patch.object(system_control, "run_controlled", side_effect=run_input)
        mock.start()
        self.addCleanup(mock.stop)
        self.registry = fixtures.ToolRegistry()
        computer.register_tools(self.registry, lambda: self.current_llm)

    def control(self, name, kind, identifier, focused=False, children=None):
        node = SimpleNamespace(Name=name, ControlTypeName=kind, AutomationId=identifier,
                               HasKeyboardFocus=focused, IsOffscreen=False, IsPassword=False, IsEnabled=True,
                               GetChildren=lambda: children or [],
                               BoundingRectangle=SimpleNamespace(left=0, top=20, right=200, bottom=60))
        def focus():
            for other in self.controls:
                other.HasKeyboardFocus = False
            node.HasKeyboardFocus = True
        node.SetFocus = focus
        node.GetPattern = lambda _: SimpleNamespace(Invoke=lambda: self.invocations.append(identifier))
        return node

    async def observe(self, image=False):
        return await self.registry.call("desktop_observe", {"handle": 91, "include_image": image})

    def action(self, tool, args, identifier="draft", kind="EditControl"):
        return {"tool": tool, "arguments": args, "expect": {"automation_id": identifier, "control_type": kind}}

    async def execute(self, actions, observed=None):
        observed = observed or await self.observe()
        return await self.registry.call("desktop_execute", {"observation_id": observed["observation_id"], "actions": actions})

    async def test_missing_app_observes_desktop_with_virtual_origin_and_no_actions(self):
        with patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (1600, 1000), "white")) as grab:
            result = await self.registry.call("desktop_observe", {"window": "Not running app"})
        self.assertEqual((-100, 0, 1500, 1000), grab.call_args.kwargs["bbox"])
        self.assertTrue(grab.call_args.kwargs["all_screens"])
        self.assertEqual({"width": 1600, "height": 1000}, result["coordinate_mapping"]["source_size"])
        self.assertEqual("window_missing_or_ambiguous", result["status"])
        self.assertEqual(-100, result["coordinate_mapping"]["origin"]["x"])
        self.assertEqual(0, result["coordinate_mapping"]["origin"]["y"])
        self.assertTrue(result["_observation_image"])
        self.assertEqual("evaluation.jpg", result["path"])
        self.assertNotIn("observation_id", result)
        self.assertEqual([], self.pg.actions)
        self.assertEqual([], self.invocations)

    async def test_standard_uia_win32_electron_office_and_chat_draft_contracts(self):
        # Five UIA technology profiles, three behaviors, repeated three times.
        profiles = (("system-file", "Search", "EditControl", "search"),
                    ("text-editor", "Document", "DocumentControl", "document"),
                    ("browser-ui", "Search tabs", "EditControl", "tab-search"),
                    ("office-ui", "Name box", "ComboBoxControl", "name-box"),
                    ("chat-draft", "Draft", "EditControl", "message-input"))
        for category, label, kind, identifier in profiles:
            for repeat in range(3):
                with self.subTest(category=category, repeat=repeat):
                    self.controls = [self.control(label, kind, identifier, True),
                                     self.control("Open panel", "ButtonControl", "open")]
                    self.pg.actions.clear()
                    result = await self.execute([
                        self.action("click_element", {"target_desc": "Open panel"}, "open", "ButtonControl"),
                        self.action("type_text", {"text": "evaluation"}, identifier, kind),
                        self.action("press_hotkey", {"keys": ["ctrl", "a"]}, identifier, kind)])
                    self.assertTrue(result["ok"], result.get("status"))
                    self.assertEqual(3, result["completed"])
                    self.assertIn(("write", "evaluation", 0.01), self.pg.actions)
                    # A second matching field must stop before input.
                    self.controls.append(self.control(label, kind, identifier, True))
                    before = list(self.pg.actions)
                    result = await self.execute([self.action("type_text", {"text": "no"}, identifier, kind)])
                    self.assertEqual("target_ambiguous", result["status"])
                    self.assertEqual(before, self.pg.actions)
                    # Losing field focus is never treated as successful typing.
                    self.controls = [self.control(label, kind, identifier, False)]
                    result = await self.execute([self.action("type_text", {"text": "no"}, identifier, kind)])
                    self.assertEqual("input_focus_unconfirmed", result["status"])
                    self.assertEqual(before, self.pg.actions)

    async def test_self_drawn_surface_routes_to_visual_and_keeps_coordinate_mapping(self):
        for repeat in range(3):
            with self.subTest(repeat=repeat):
                self.controls = []
                observed = await self.observe(image=True)
                self.assertEqual("visual", observed["route"])
                self.assertEqual({"x": -100, "y": 20}, observed["coordinate_mapping"]["origin"])
                result = await self.execute([self.action("type_text", {"text": "no"})], observed)
                self.assertFalse(result["ok"])
                self.assertEqual([], self.pg.actions)
                # Existing coordinate conversion remains the single visual path.
                point = computer.observation_to_desktop(100, 50, {
                    "origin": {"x": -100, "y": 20},
                    "source_size": {"width": 1600, "height": 1200},
                    "observation_size": {"width": 800, "height": 600}})
                self.assertEqual((100, 120), point)

    async def test_deep_electron_controls_and_duplicate_window_titles(self):
        edit = self.control("Draft", "EditControl", "draft", True)
        deep = edit
        for index in range(30):
            deep = self.control(str(index), "PaneControl", str(index), children=[deep])
        self.controls = [deep]
        observed = await self.observe()
        self.assertEqual("uia", observed["route"])
        self.assertFalse(observed["truncated"])
        self.assertTrue((await self.execute([self.action("type_text", {"text": "deep"})], observed))["ok"])
        other = FakeWindow(self.window.title, 92)
        with patch.dict("sys.modules", {"pygetwindow": fake_window_module([self.window, other])}):
            result = await self.registry.call("desktop_observe", {"window": self.window.title, "include_image": False})
            self.assertEqual("window_missing_or_ambiguous", result["status"])
            self.assertTrue((await self.observe())["ok"])

    async def test_agent_model_rounds_for_same_three_inputs(self):
        metrics = {}
        self.controls = [self.control("Draft", "EditControl", "draft", True)]
        actions = [self.action("press_hotkey", {"keys": ["ctrl", "a"]}),
                   self.action("type_text", {"text": "draft"}),
                   self.action("press_hotkey", {"keys": ["end"]})]
        for batched in (False, True):
            self.pg.actions.clear()
            requests = []
            registry = self.registry
            class LLM:
                async def chat_stream_tools(inner, messages, **kwargs):
                    requests.append(1)
                    turn = len(requests)
                    name, args = "desktop_observe", {"handle": 91, "include_image": False}
                    if turn == 1:
                        pass
                    elif batched and turn == 2:
                        result = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
                        name, args = "desktop_execute", {"observation_id": result["observation_id"], "actions": actions}
                    elif not batched and turn in (2, 3, 4):
                        action = actions[turn - 2]
                        name, args = action["tool"], dict(action["arguments"], handle=91)
                    elif not batched and turn == 5:
                        pass
                    else:
                        yield "done", {"role": "assistant", "content": "草稿步骤已执行，未发送"}
                        return
                    yield "done", {"role": "assistant", "content": "", "tool_calls": [{
                        "id": str(turn), "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
            with tempfile.TemporaryDirectory() as tmp:
                events = [e async for e in Agent(LLM(), registry, AgentConfig(), result_dir=Path(tmp)).run("在指定窗口编辑草稿")]
            self.assertFalse(any(e.type == "error" for e in events))
            metrics[batched] = (len(requests), sum(e.type == "tool_call" for e in events), list(self.pg.actions))
        self.assertEqual((6, 5), metrics[False][:2])
        self.assertEqual((3, 2), metrics[True][:2])
        self.assertEqual(metrics[False][2], metrics[True][2])

    async def test_focus_changes_while_acquiring_lease_stop_before_physical_input(self):
        self.controls = [self.control("Draft", "EditControl", "draft", True)]
        async def stolen(*args, **kwargs):
            self.window.title = "Unexpected dialog"
            return self.control_token
        with patch.object(system_control, "acquire_control", side_effect=stolen):
            result = await self.execute([self.action("type_text", {"text": "do not type"})])
        self.assertEqual("action_unconfirmed", result["status"])
        self.assertEqual([], self.pg.actions)
        self.assertEqual([], self.window.calls)
        self.assertIsNone(system_control.INPUT_PRECONDITION.get())

    async def test_disabled_and_password_fields_are_not_used_by_plan(self):
        for attribute in ("IsEnabled", "IsPassword"):
            self.controls = [self.control("Draft", "EditControl", "draft", True)]
            setattr(self.controls[0], attribute, attribute == "IsPassword")
            result = await self.execute([self.action("type_text", {"text": "no"})])
            self.assertEqual("input_target_protected_or_disabled", result["status"])
            self.assertEqual([], self.pg.actions)

    async def test_inner_focus_changes_while_waiting_for_lease_do_not_type(self):
        first = self.control("Draft", "EditControl", "draft", True)
        other = self.control("Search", "EditControl", "search", False)
        self.controls = [first, other]
        async def changed(*args, **kwargs):
            first.HasKeyboardFocus = False
            other.HasKeyboardFocus = True
            return self.control_token
        with patch.object(system_control, "acquire_control", side_effect=changed):
            result = await self.execute([self.action("type_text", {"text": "do not type"})])
        self.assertEqual("action_unconfirmed", result["status"])
        self.assertEqual([], self.pg.actions)

    async def test_click_label_changed_after_observation_is_not_invoked(self):
        button = self.control("Open panel", "ButtonControl", "open")
        self.controls = [button]
        async def changed(*args, **kwargs):
            button.Name = "Send"
            return self.control_token
        with patch.object(system_control, "acquire_control", side_effect=changed):
            result = await self.execute([self.action("click_element", {"target_desc": "Open panel"}, "open", "ButtonControl")])
        self.assertEqual("action_unconfirmed", result["status"])
        self.assertEqual([], self.invocations)

    async def test_value_postcondition_uses_actual_field_without_returning_text(self):
        node = self.control("Draft", "EditControl", "draft", True)
        node.GetPattern = lambda _: SimpleNamespace(Value="verified draft")
        self.controls = [node]
        action = self.action("type_text", {"text": "verified draft"})
        result = await self.execute([{**action, "after": {"expect": {"automation_id": "draft"}, "value_equals": "verified draft"}}])
        self.assertTrue(result["ok"])
        self.assertEqual(1, result["verified"])
        self.assertNotIn("verified draft", str(result))
        result = await self.execute([{**action, "after": {"expect": {"automation_id": "draft"}, "value_equals": "wrong"}}, action])
        self.assertEqual("postcondition_unconfirmed", result["status"])
        self.assertEqual(1, result["completed"])

    async def test_long_ascii_is_chunked_fast_without_clipboard_or_implicit_enter(self):
        self.controls = [self.control("Draft", "EditControl", "draft", True)]
        text = "abc123 " * 100
        result = await self.execute([self.action("type_text", {"text": text})])
        self.assertTrue(result["ok"])
        writes = [action for action in self.pg.actions if action[0] == "write"]
        self.assertEqual(text, "".join(action[1] for action in writes))
        self.assertTrue(all(action[2] == 0 and len(action[1]) <= 80 for action in writes))
        self.assertEqual([], self.clipboard)
        self.assertFalse(any(action[0] in {"hotkey", "press"} for action in self.pg.actions))
