"""Behavioral regression tests for guarded multi-application desktop plans."""
import asyncio
import copy
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.core.agent import Agent, _looks_dangerous, _looks_financial, _looks_remote_confirmation, _is_read_only_operation, _is_spinning
from backend.core.config import AgentConfig
from backend.core.tools import ToolRegistry
from backend.modules.computer import desktop_runtime as runtime


class DesktopRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = ToolRegistry()
        self.info = {"handle": 10, "title": "Test app", "process_id": 123, "class_name": "MainWindow",
                     "rect": {"x": -100, "y": 20, "width": 800, "height": 600}, "minimized": False}
        self.present = True
        self.active = 10
        self.controls = [{"name": "Editor", "control_type": "EditControl", "automation_id": "edit", "focused": True}]
        self.calls = []
        self.captures = 0
        self.queries = 0
        self.hook = None
        self.inspection_hook = None
        self.permission = patch.object(runtime.system_control, "check_foreground_permission")
        self.permission.start()
        self.addCleanup(self.permission.stop)

        def resolve(_window, handle):
            return object() if self.present and handle in (None, 10) else None

        async def inspect(_handle):
            self.queries += 1
            if self.inspection_hook:
                await self.inspection_hook()
            return {"ok": True, "status": "ok", "controls": copy.deepcopy(self.controls)}

        async def capture(_info):
            self.captures += 1
            return {"_observation_image": "image", "path": "test.jpg", "coordinate_mapping": {"origin": {"x": -100, "y": 20}}}

        self.inspect = inspect
        runtime.register_tools(self.registry, resolve=resolve, inventory=lambda: [copy.deepcopy(self.info)],
                               inspect=inspect, capture=capture, foreground=lambda: self.active,
                               identity=lambda _: copy.deepcopy(self.info))
        for name in ("click_element", "type_text", "press_hotkey"):
            async def action(_name=name, **args):
                self.calls.append((_name, args))
                if self.hook:
                    return await self.hook(_name, args)
                return {"ok": True}
            action.__module__ = "backend.modules.computer.computer_module"
            self.registry.tool(name, name, {}, side_effect=True)(action)

    async def observe(self, **kwargs):
        return await self.registry.call("desktop_observe", {"handle": 10, **kwargs})

    def action(self, name="type_text", **args):
        if not args:
            args = {"text": "hello"} if name == "type_text" else {"keys": ["ctrl", "a"]}
        return {"tool": name, "arguments": args, "expect": {"automation_id": "edit", "control_type": "EditControl"}}

    async def execute(self, actions, observed=None):
        observed = observed or await self.observe()
        return await self.registry.call("desktop_execute", {"observation_id": observed["observation_id"], "actions": actions})

    async def test_missing_target_returns_desktop_once_without_execution_token(self):
        self.present = False
        result = await self.registry.call("desktop_observe", {"window": "Any desktop app"})
        self.assertFalse(result["ok"])
        self.assertEqual("window_missing_or_ambiguous", result["status"])
        self.assertTrue(result["_observation_image"])
        self.assertNotIn("observation_id", result)
        self.assertEqual(1, self.captures)
        self.assertEqual(0, self.queries)
        self.assertEqual([], self.calls)
        result = await self.observe(include_image=False)
        self.assertNotIn("_observation_image", result)
        self.assertEqual(1, self.captures)

    async def test_missing_target_capture_failure_has_no_token_or_side_effects(self):
        self.registry.unregister("desktop_observe")
        self.registry.unregister("desktop_execute")
        capture = AsyncMock(side_effect=OSError("capture unavailable"))
        runtime.register_tools(self.registry, resolve=lambda *_: None, inventory=lambda: [],
                               inspect=self.inspect, capture=capture, foreground=lambda: self.active)
        result = await self.observe()
        self.assertEqual("window_missing_or_ambiguous", result["status"])
        self.assertEqual("capture_failed", result["image_status"])
        self.assertEqual("OSError", result["image_error"])
        self.assertNotIn("observation_id", result)
        self.assertEqual([], self.calls)
        with patch.object(runtime, "interrupted", return_value=True):
            self.assertEqual("interrupted", (await self.observe())["status"])
        self.assertEqual(1, capture.await_count)

    async def test_activation_exception_returns_context_without_token(self):
        with patch.object(runtime.system_control, "acquire_control", new=AsyncMock(return_value=object())), \
             patch.object(runtime.system_control, "wait_for_user_idle", new=AsyncMock(return_value=True)), \
             patch.object(runtime.system_control, "activate_foreground"), \
             patch.object(runtime.system_control, "run_controlled", new=AsyncMock(side_effect=RuntimeError("focus failed"))), \
             patch.object(runtime.system_control, "release_control"):
            result = await self.observe(activate=True)
        self.assertEqual("input_target_not_focused", result["status"])
        self.assertEqual(10, result["window"]["handle"])
        self.assertTrue(result["windows"])
        self.assertNotIn("observation_id", result)

    async def test_missing_target_merges_hidden_candidates_without_duplicates(self):
        self.registry.unregister("desktop_observe")
        self.registry.unregister("desktop_execute")
        hidden = {"handle": 99, "title": "Hidden", "process_id": 321, "hidden": True}
        runtime.register_tools(self.registry, resolve=lambda *_: None, inventory=lambda: [self.info],
                               candidates=lambda *_: [self.info, hidden, hidden],
                               inspect=self.inspect, capture=AsyncMock(return_value={}), foreground=lambda: self.active)
        result = await self.observe(include_image=False)
        self.assertEqual([10, 99], [item["handle"] for item in result["windows"]])
        self.assertNotIn("observation_id", result)

    async def test_plan_executes_with_one_final_observation_and_preserves_mapping(self):
        observed = await self.observe()
        result = await self.execute([self.action("press_hotkey"), self.action()], observed)
        self.assertTrue(result["ok"])
        self.assertEqual(2, result["completed"])
        self.assertEqual(2, len(self.calls))
        self.assertEqual(2, self.captures)  # initial and final, not one per primitive
        self.assertEqual({"x": -100, "y": 20}, result["observation"]["coordinate_mapping"]["origin"])
        self.assertEqual("image", result["_observation_image"])
        self.assertTrue(all(args["handle"] == 10 for _, args in self.calls))
        self.assertNotIn("hello", json.dumps(result))
        self.assertTrue(all("action_ms" in step for step in result["steps"]))

    async def test_changed_window_focus_geometry_pid_and_modal_stop_before_input(self):
        for change in (lambda: setattr(self, "active", 11), lambda: self.info.update(process_id=999),
                       lambda: self.info["rect"].update(x=70), lambda: self.info.update(title="Save as"),
                       lambda: self.info.update(minimized=True), lambda: setattr(self, "present", False)):
            with self.subTest(change=change):
                self.active = 10
                self.present = True
                self.info.update(process_id=123, title="Test app", minimized=False)
                self.info["rect"]["x"] = -100
                observed = await self.observe()
                change()
                result = await self.execute([self.action()], observed)
                self.assertFalse(result["ok"])
                self.assertEqual(0, result["completed"])
                self.assertEqual([], self.calls)

    async def test_ambiguous_missing_and_nonfocused_controls_do_not_input(self):
        observed = await self.observe()
        for controls, status in ((self.controls * 2, "target_ambiguous"), ([], "target_missing"),
                                 ([dict(self.controls[0], focused=False)], "input_focus_unconfirmed")):
            with self.subTest(status=status):
                self.controls = controls
                result = await self.execute([self.action()], observed)
                self.assertEqual(status, result["status"])
                self.assertEqual([], self.calls)
                observed = await self.observe()

    async def test_full_plan_is_validated_before_side_effects(self):
        for item in ({"tool": "run_command", "arguments": {"command": "echo bad"}, "expect": {"name": "Editor"}},
                     self.action("type_text", text="hello\n"), self.action("type_text", text="hello\t"),
                     self.action("type_text", text="a" * 1001), self.action("press_hotkey", keys=["enter"]),
                     {"tool": "click_element", "arguments": {"target_desc": "Send"}, "expect": {"name": "Send"}},
                     {"tool": "type_text", "arguments": {"text": "hello", "handle": 999}, "expect": {"name": "Editor"}}):
            with self.assertRaises(ValueError):
                await self.execute([self.action(), item])
        self.assertEqual([], self.calls)
        with self.assertRaises(ValueError):
            runtime.validate_plan([self.action()] * 6)

    async def test_observation_is_single_use_and_expired_observation_cannot_execute(self):
        observed = await self.observe()
        self.assertTrue((await self.execute([self.action()], observed))["ok"])
        again = await self.execute([self.action()], observed)
        self.assertEqual("observation_expired", again["status"])
        observed = await self.observe()
        with patch.object(runtime.time, "monotonic", return_value=runtime.time.monotonic() + 61):
            self.assertEqual("observation_expired", (await self.execute([self.action()], observed))["status"])
        self.assertEqual(1, len(self.calls))

    async def test_partial_failure_does_not_replay_or_continue(self):
        async def fail(_name, _args):
            return {"ok": False, "status": "sent_unverified"} if len(self.calls) == 2 else {"ok": True}
        self.hook = fail
        result = await self.execute([self.action()] * 3)
        self.assertFalse(result["ok"])
        self.assertEqual(1, result["completed"])
        self.assertEqual(2, len(self.calls))

    async def test_action_exception_records_uncertainty_and_stops(self):
        async def fail(_name, _args):
            raise RuntimeError("may have happened")
        self.hook = fail
        result = await self.execute([self.action()] * 2)
        self.assertEqual("action_unconfirmed", result["status"])
        self.assertEqual(1, len(self.calls))
        self.assertEqual(0, result["completed"])

    async def test_interjection_stops_at_step_boundary(self):
        token = runtime.interrupt_check.set(lambda: bool(self.calls))
        try:
            result = await self.execute([self.action()] * 3)
        finally:
            runtime.interrupt_check.reset(token)
        self.assertEqual("interrupted", result["status"])
        self.assertEqual(1, result["completed"])
        self.assertEqual(1, self.captures)

    async def test_escape_permission_revocation_prevents_next_step(self):
        async def revoke(_name, _args):
            self.permission.stop()
            self.permission = patch.object(runtime.system_control, "check_foreground_permission", side_effect=RuntimeError("Esc revoked"))
            self.permission.start()
            self.addCleanup(self.permission.stop)
            return {"ok": True}
        self.hook = revoke
        result = await self.execute([self.action()] * 2)
        self.assertEqual("interrupted", result["status"])
        self.assertEqual(1, result["completed"])
        self.assertEqual(1, len(self.calls))

    async def test_cancel_during_wait_has_no_later_actions(self):
        entered = asyncio.Event()
        async def wait():
            entered.set()
            await asyncio.Event().wait()
        observed = await self.observe()
        self.inspection_hook = wait
        task = asyncio.create_task(self.execute([self.action()], observed))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([], self.calls)

    async def test_waits_for_condition_instead_of_fixed_delay_and_rechecks_focus(self):
        self.controls = []
        observed = await self.observe()
        async def appear():
            self.controls = [{"automation_id": "edit", "control_type": "EditControl", "focused": True}]
        self.inspection_hook = appear
        result = await self.execute([{**self.action(), "wait_ms": 1000}], observed)
        self.assertTrue(result["ok"])
        self.assertEqual(1, len(self.calls))
        observed = await self.observe()
        async def lose_focus():
            self.active = 11
        self.inspection_hook = lose_focus
        result = await self.execute([self.action()], observed)
        self.assertFalse(result["ok"])
        self.assertEqual(1, len(self.calls))

    async def test_empty_tree_is_visual_route_and_missing_window_is_not_guessed(self):
        self.controls = []
        observed = await self.observe()
        self.assertEqual("visual", observed["route"])
        self.present = False
        result = await self.observe()
        self.assertFalse(result["ok"])
        self.assertNotIn("observation_id", result)

    async def test_default_observe_never_acquires_control(self):
        with patch.object(runtime.system_control, "acquire_control", new=AsyncMock()) as acquire:
            self.assertTrue((await self.observe())["ok"])
        acquire.assert_not_awaited()

    async def test_activate_observe_denied_control_never_issues_token(self):
        with patch.object(runtime.system_control, "acquire_control", new=AsyncMock(return_value=None)):
            result = await self.observe(activate=True)
        self.assertEqual("control_busy", result["status"])
        self.assertNotIn("observation_id", result)
        self.assertEqual(0, self.queries)
        self.assertEqual(0, self.captures)

    async def test_activate_observe_focuses_before_inspecting_and_issues_token(self):
        focus = AsyncMock()
        with (patch.object(runtime.system_control, "acquire_control", new=AsyncMock(return_value=object())),
              patch.object(runtime.system_control, "wait_for_user_idle", new=AsyncMock(return_value=True)),
              patch.object(runtime.system_control, "activate_foreground"),
              patch.object(runtime.system_control, "run_controlled", new=focus),
              patch.object(runtime.system_control, "release_control") as release):
            result = await self.observe(activate=True)
        self.assertTrue(result["ok"], result)
        focus.assert_awaited_once()
        release.assert_called_once()
        self.assertEqual(10, result["window"]["handle"])

    async def test_click_binds_exact_control_and_never_falls_back_to_coordinates(self):
        self.controls = [{"name": "Open", "control_type": "ButtonControl", "automation_id": "open"}]
        action = {"tool": "click_element", "arguments": {"target_desc": "other"}, "expect": {"automation_id": "open"}}
        result = await self.execute([action])
        self.assertTrue(result["ok"])
        args = self.calls[0][1]
        self.assertEqual("Open", args["target_desc"])
        self.assertIs(args["allow_visual_fallback"], False)

    async def test_replaced_tool_cannot_run_through_plan(self):
        self.registry.get("type_text").handler.__module__ = "third_party_plugin"
        result = await self.execute([self.action()])
        self.assertEqual("tool_unavailable", result["status"])
        self.assertEqual([], self.calls)

    def test_nested_action_risks_are_not_hidden_by_plan_name(self):
        for text, check in (("pay invoice", _looks_financial), ("Remove-Item file", _looks_dangerous),
                            ("change password", _looks_remote_confirmation)):
            self.assertTrue(check("desktop_execute", {"actions": [self.action(text=text)]}))

    async def test_truncated_tree_and_query_error_preserve_partial_progress(self):
        observed = await self.observe()
        original = self.inspect
        async def truncated(handle):
            return {**await original(handle), "truncated": True}
        # Re-register with a provider that fails after one committed primitive.
        self.registry.unregister("desktop_execute")
        self.registry.unregister("desktop_observe")
        runtime.register_tools(self.registry, resolve=lambda *_: object(), inventory=lambda: [],
                               identity=lambda _: copy.deepcopy(self.info), foreground=lambda: self.active,
                               inspect=truncated, capture=AsyncMock(return_value={}))
        observed = await self.observe()
        result = await self.execute([self.action()], observed)
        self.assertEqual("controls_incomplete", result["status"])
        self.assertEqual([], self.calls)
        async def fail_query():
            if self.calls:
                raise RuntimeError("provider failed")
        self.inspection_hook = fail_query
        self.registry.unregister("desktop_execute")
        self.registry.unregister("desktop_observe")
        runtime.register_tools(self.registry, resolve=lambda *_: object(), inventory=lambda: [],
                               identity=lambda _: copy.deepcopy(self.info), foreground=lambda: self.active,
                               inspect=original, capture=AsyncMock(return_value={}))
        result = await self.execute([self.action()] * 2)
        self.assertEqual("controls_incomplete", result["status"])
        self.assertEqual(1, result["completed"])
        self.assertEqual(1, len(self.calls))

    async def test_actual_critical_label_cannot_hide_behind_automation_id(self):
        self.controls = [{"name": "Send", "automation_id": "button1", "control_type": "ButtonControl"}]
        result = await self.execute([{"tool": "click_element", "arguments": {"target_desc": "button1"},
                                      "expect": {"automation_id": "button1"}}])
        self.assertEqual("critical_action_requires_single_tool", result["status"])
        self.assertEqual([], self.calls)
        with self.assertRaises(ValueError):
            runtime.validate_plan([self.action("press_hotkey", keys=["ctrl", "v"])])

    async def test_real_agent_applies_interjection_between_plan_actions(self):
        observed = await self.observe(include_image=False)
        pending, messages = [], []
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(_name, _args):
            started.set()
            await release.wait()
            return {"ok": True}
        self.hook = slow
        plan = {"observation_id": observed["observation_id"], "actions": [self.action()] * 3}
        class LLM:
            async def chat_stream_tools(inner, batch, **kwargs):
                messages.append(copy.deepcopy(batch))
                if len(messages) == 1:
                    yield "done", {"role": "assistant", "content": "", "tool_calls": [{
                        "id": "plan", "type": "function", "function": {
                            "name": "desktop_execute", "arguments": json.dumps(plan)}}]}
                else:
                    yield "done", {"role": "assistant", "content": "已按新要求暂停后续输入"}
        def take():
            items = pending[:]
            pending.clear()
            return items
        with tempfile.TemporaryDirectory() as tmp:
            agent = Agent(LLM(), self.registry, AgentConfig(), interjection_fn=take, result_dir=Path(tmp))
            async def collect():
                return [event async for event in agent.run("填写输入框")]
            task = asyncio.create_task(collect())
            try:
                await asyncio.wait_for(started.wait(), 3)
                pending.append("停止输入，先看另一个窗口")
                release.set()
                events = await asyncio.wait_for(task, 3)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(1, len(self.calls))
        self.assertTrue(any(e.type == "interjection_applied" for e in events))
        self.assertIn("停止输入，先看另一个窗口", str(messages[-1]))
        tool_result = next(m for m in messages[-1] if m.get("role") == "tool")
        result = json.loads(tool_result["content"])
        self.assertEqual("interrupted", result["status"])
        self.assertEqual(1, result["completed"])
        self.assertIsNone(runtime.interrupt_check.get())

    async def test_agent_denied_nested_risk_never_dispatches_plan(self):
        for text, mode, reason in (("pay invoice", "full", "financial"),
                                   ("Remove-Item file", "ask", "risk"),
                                   ("change password", "remote", "password")):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                observed = await self.observe(include_image=False)
                plan = {"observation_id": observed["observation_id"], "actions": [self.action(text=text)]}
                class LLM:
                    turn = 0
                    async def chat_stream_tools(inner, messages, **kwargs):
                        inner.turn += 1
                        if inner.turn == 1:
                            yield "done", {"role": "assistant", "content": "", "tool_calls": [{
                                "id": "plan", "type": "function", "function": {
                                    "name": "desktop_execute", "arguments": json.dumps(plan)}}]}
                        else:
                            yield "done", {"role": "assistant", "content": "已取消"}
                confirm = AsyncMock(return_value=False)
                agent = Agent(LLM(), self.registry, AgentConfig(confirm_before_action=True),
                              confirm_fn=confirm, result_dir=Path(tmp))
                events = [e async for e in agent.run("编辑输入框", approval_mode=mode)]
                self.assertEqual([], self.calls)
                confirm.assert_awaited_once()
                self.assertEqual(reason, next(e.data["reason"] for e in events if e.type == "confirm_needed"))

    def test_observe_read_only_exemption_requires_literal_false_activation(self):
        for args in ({"window": "微信"}, {"window": "微信", "activate": False}):
            self.assertTrue(_is_read_only_operation("desktop_observe", args))
        for activate in (True, None, 0, 1, "false", "true", [], {}):
            with self.subTest(activate=activate):
                self.assertFalse(_is_read_only_operation("desktop_observe", {"activate": activate}))

    async def test_financial_view_is_read_only_but_following_action_still_requires_confirmation(self):
        for name in (None, "click_element", "desktop_execute", "desktop_observe"):
            with self.subTest(action=name), tempfile.TemporaryDirectory() as tmp:
                observe_args = {"window": "微信", "handle": 10, "include_image": False}
                class LLM:
                    turn = 0
                    async def chat_stream_tools(inner, messages, **kwargs):
                        inner.turn += 1
                        calls = [("desktop_observe", observe_args)]
                        if name is not None:
                            calls.append((name, {**observe_args, "activate": True}
                                          if name == "desktop_observe" else
                                          {"target_desc": "Next"} if name == "click_element" else
                                          {"observation_id": "unused", "actions": [self.action()]}))
                        if inner.turn == 1:
                            yield "done", {"role": "assistant", "content": "", "tool_calls": [
                                {"id": str(i), "type": "function", "function": {
                                    "name": tool_name, "arguments": json.dumps(args)}}
                                for i, (tool_name, args) in enumerate(calls)]}
                        else:
                            yield "done", {"role": "assistant", "content": "已查看界面"}
                confirm = AsyncMock(return_value=False)
                events = [event async for event in Agent(
                    LLM(), self.registry, AgentConfig(confirm_before_action=True),
                    confirm_fn=confirm, result_dir=Path(tmp),
                ).run("帮我看下微信付款界面", approval_mode="risk")]
                observed = [e for e in events if e.type == "tool_result" and e.data["name"] == "desktop_observe"]
                self.assertTrue(observed[0].data["ok"])
                confirmations = [e for e in events if e.type == "confirm_needed"]
                if name is None:
                    confirm.assert_not_awaited()
                    self.assertEqual([], confirmations)
                else:
                    confirm.assert_awaited_once()
                    self.assertEqual(name, confirmations[0].data["name"])
                    self.assertEqual("financial", confirmations[0].data["reason"])
                self.assertEqual([], self.calls)

    async def test_repeated_control_only_observations_are_observed_for_spin_detection(self):
        # Stable observations isolate the classification from changing tokens/timings.
        self.registry.get("desktop_observe").handler = AsyncMock(return_value={
            "ok": True, "window": self.info, "controls": self.controls})
        for activate in (False, True):
            with self.subTest(activate=activate), tempfile.TemporaryDirectory() as tmp:
                histories = []
                class LLM:
                    turn = 0
                    async def chat_stream_tools(inner, messages, **kwargs):
                        inner.turn += 1
                        if inner.turn <= 4:
                            yield "done", {"role": "assistant", "content": "", "tool_calls": [{
                                "id": str(inner.turn), "type": "function", "function": {
                                    "name": "desktop_observe", "arguments": json.dumps({
                                        "handle": 10, "include_image": False, "activate": activate})}}]}
                        else:
                            yield "done", {"role": "assistant", "content": "停止重复观察"}
                def check(recent, window=3):
                    histories.append(list(recent))
                    return _is_spinning(recent, window)
                with patch("backend.core.agent._is_spinning", side_effect=check):
                    events = [event async for event in Agent(
                        LLM(), self.registry, AgentConfig(), result_dir=Path(tmp),
                    ).run("查看指定窗口控件", approval_mode="full")]
                repeated = [history for history in histories if len(history) >= 3]
                self.assertTrue(repeated)
                self.assertTrue(any(_is_spinning(history) for history in repeated))
                self.assertTrue(all(item[3] == "observed" for history in repeated for item in history))
                self.assertFalse(any(e.type == "error" for e in events))

    async def test_observation_does_not_bypass_greeting_screenshot_guard(self):
        class LLM:
            turn = 0
            async def chat_stream_tools(inner, messages, **kwargs):
                inner.turn += 1
                if inner.turn == 1:
                    yield "done", {"role": "assistant", "content": "", "tool_calls": [{
                        "id": "peek", "type": "function", "function": {
                            "name": "desktop_observe", "arguments": "{}"}}]}
                else:
                    yield "done", {"role": "assistant", "content": "需要我做什么？"}
        with tempfile.TemporaryDirectory() as tmp:
            events = [e async for e in Agent(LLM(), self.registry, AgentConfig(), result_dir=Path(tmp)).run("你好")]
        self.assertEqual(0, self.captures)
        self.assertEqual(0, self.queries)
        self.assertTrue(any(e.type == "tool_result" and not e.data["ok"] for e in events))

    async def test_control_only_observation_stays_control_only_after_actions(self):
        observed = await self.observe(include_image=False)
        result = await self.execute([self.action()], observed)
        self.assertTrue(result["ok"])
        self.assertEqual(0, self.captures)
        self.assertNotIn("_observation_image", result)

    async def test_after_condition_stops_without_replaying_completed_input(self):
        result = await self.execute([{**self.action(), "after": {"expect": {"name": "Never appears"}}}, self.action()])
        self.assertEqual("postcondition_unconfirmed", result["status"])
        self.assertEqual(1, result["completed"])
        self.assertEqual(0, result["verified"])
        self.assertEqual(1, len(self.calls))

    async def test_after_focus_verified_and_partial_completion_remain_distinct(self):
        action = {**self.action(), "after": {"expect": {"automation_id": "edit"}, "focused": True}}
        result = await self.execute([action])
        self.assertTrue(result["ok"])
        self.assertEqual(1, result["verified"])
        with self.assertRaises(ValueError):
            await self.execute([self.action(), {**action, "after": {"expect": {"control_type": "EditControl"}}}])
        self.assertEqual(1, len(self.calls))