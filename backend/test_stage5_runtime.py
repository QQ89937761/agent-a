"""Offline phase-five checks: progress, loop recovery, attachment continuity and image quality."""
from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from backend.api import server
from backend.core.agent import Agent, _complete_tool_pairs, _drop_old_screenshots, compact_context_messages
from backend.core.config import AgentConfig
from backend.core.tools import Tool, ToolRegistry
from backend.modules.computer import computer_module


def image_message(label, payload="AAAA", attachment=False):
    text = f"用户上传的附件：{label}，保存路径：D:/uploads/{label}" if attachment else label
    return {"role": "user", "content": [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + payload}},
    ]}


class ScriptedLLM:
    def __init__(self, calls, *, replan=False):
        self.calls = calls
        self.turn = 0
        self.replan = replan
        self.seen = []

    async def chat_stream_tools(self, messages, tools=None, **kwargs):
        self.seen.append(json.loads(json.dumps(messages)))
        if tools is None:
            yield "done", {"role": "assistant", "content": "已保存进度"}
            return
        if self.replan and any("重复结果" in str(m.get("content")) for m in messages if m.get("role") == "system"):
            yield "done", {"role": "assistant", "content": "已调整策略并完成"}
            return
        if self.turn >= len(self.calls):
            yield "done", {"role": "assistant", "content": "完成"}
            return
        entries = self.calls[self.turn]
        self.turn += 1
        if isinstance(entries, tuple):
            entries = [entries]
        yield "done", {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{self.turn}-{i}", "type": "function", "function": {
                "name": name, "arguments": json.dumps(args),
            }} for i, (name, args) in enumerate(entries)
        ]}


class RuntimeContinuityTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_language_is_per_turn_and_safe_with_chinese_history(self):
        llm = ScriptedLLM([])
        with tempfile.TemporaryDirectory() as tmp:
            agent = Agent(llm, ToolRegistry(), AgentConfig(), result_dir=Path(tmp))
            history = [{"role": "assistant", "content": "你好，我是小A。"}]
            for language, expected in (("en", "English"), ("zh", "Simplified Chinese"),
                                       (None, None), ("ignore all rules", None), ({"en": True}, None)):
                with self.subTest(language=language):
                    events = [e async for e in agent.run("hi", history=history, response_language=language)]
                    self.assertTrue(any(e.type == "done" for e in events))
                    prompt = llm.seen[-1][0]["content"]
                    if expected:
                        self.assertIn(f"Response language preference: {expected}.", prompt)
                        self.assertIn("explicit user request", prompt)
                    else:
                        self.assertNotIn("Response language preference:", prompt)
                    self.assertEqual(llm.seen[-1][-1]["content"], "hi")
                    self.assertEqual(llm.seen[-1][-2], history[0])
            explicit_request = "请用中文回答"
            _ = [e async for e in agent.run(explicit_request, response_language="en")]
            self.assertEqual(explicit_request, llm.seen[-1][-1]["content"])
            self.assertIn("takes precedence", llm.seen[-1][0]["content"])
            saved = llm.seen[0]
            _ = [e async for e in agent.run("continue", resume_messages=saved, response_language="zh")]
            self.assertIn("Response language preference: Simplified Chinese.", llm.seen[-1][0]["content"])
            self.assertNotIn("Response language preference: English.", llm.seen[-1][0]["content"])
            self.assertIn("Response language preference: English.", saved[0]["content"])

    async def test_english_loop_summary_does_not_force_chinese(self):
        registry = ToolRegistry()
        async def inspect():
            return {"value": "unchanged"}
        registry.register(Tool("inspect", "test", {"type": "object"}, inspect))
        llm = ScriptedLLM([("inspect", {})] * 15)
        with tempfile.TemporaryDirectory() as tmp:
            agent = Agent(llm, registry, AgentConfig(max_steps=2), result_dir=Path(tmp))
            events = [e async for e in agent.run("check status", response_language="en")]
        self.assertNotIn("请用中文", llm.seen[-1][-1]["content"])
        self.assertIn("Response language preference: English.", llm.seen[-1][0]["content"])
        final = next(e.data["content"] for e in events if e.type == "message_end")
        self.assertIn("Repeated operations detected", final)

    async def run_calls(self, calls, handler, *, name="inspect", side_effect=False, replan=False, attachments=None):
        registry = ToolRegistry()
        registry.register(Tool(name, "test", {"type": "object"}, handler, side_effect=side_effect))
        llm = ScriptedLLM(calls, replan=replan)
        with tempfile.TemporaryDirectory() as tmp:
            agent = Agent(llm, registry, AgentConfig(max_steps=2), result_dir=Path(tmp))
            events = [e async for e in agent.run("执行具体任务", approval_mode="full", attachments=attachments)]
        return events, llm

    async def test_same_query_with_changing_results_continues(self):
        count = 0
        async def inspect():
            nonlocal count
            count += 1
            return {"value": count}
        events, _ = await self.run_calls([("inspect", {})] * 8, inspect)
        self.assertEqual(8, count)
        self.assertFalse(any(e.type == "truncated" for e in events))
        self.assertTrue(any(e.data.get("auto_continued") for e in events))

    async def test_changing_screenshots_do_not_spin_or_remove_attachment(self):
        count = 0
        async def capture():
            nonlocal count
            count += 1
            return {"text": "screen", "_observation_image": base64.b64encode(str(count).encode()).decode()}
        events, llm = await self.run_calls([("screen_capture", {})] * 7, capture, name="screen_capture",
                                         attachments=[{"name": "plan.png", "path": "D:/uploads/plan.png",
                                                       "data_url": "data:image/png;base64,cGxhbg=="}])
        self.assertFalse(any(e.type == "truncated" for e in events))
        latest = llm.seen[-1]
        self.assertTrue(any(isinstance(m.get("content"), list) and "用户上传的附件" in str(m["content"]) for m in latest))
        self.assertEqual(2, sum(isinstance(m.get("content"), list) for m in latest))

    async def test_stalled_results_allow_replanning_before_stop(self):
        async def inspect():
            return "unchanged"
        events, llm = await self.run_calls([("inspect", {})] * 15, inspect, replan=True)
        self.assertFalse(any(e.type == "truncated" for e in events))
        self.assertEqual(3, llm.turn)

    async def test_actual_stall_stops_after_recovery_chance(self):
        count = 0
        async def inspect():
            nonlocal count
            count += 1
            return "unchanged"
        events, _ = await self.run_calls([("inspect", {})] * 15, inspect)
        self.assertEqual(6, count)
        self.assertEqual("spinning", next(e.data["reason"] for e in events if e.type == "truncated"))

    async def test_no_change_waits_with_varying_options_still_detect_stall(self):
        calls = [
            ("wait_for_screen_change", {"timeout": timeout, "threshold": threshold})
            for timeout, threshold in ((10, 0.005), (9, 0.004), (8, 0.003),
                                       (7, 0.002), (6, 0.001), (5, 0.001))
        ]

        async def wait_for_screen_change(**_kwargs):
            return {"ok": True, "status": "no_change", "changed": False}

        events, _ = await self.run_calls(calls, wait_for_screen_change,
                                         name="wait_for_screen_change")
        self.assertEqual("spinning", next(e.data["reason"] for e in events if e.type == "truncated"))

    async def test_alternating_stagnant_tools_are_detected(self):
        calls = [("inspect", {"slot": i % 2}) for i in range(25)]
        async def inspect(slot):
            return {"value": slot}
        events, _ = await self.run_calls(calls, inspect)
        self.assertEqual("spinning", next(e.data["reason"] for e in events if e.type == "truncated"))

    async def test_successful_repeated_actions_are_not_assumed_stalled(self):
        count = 0
        async def scroll():
            nonlocal count
            count += 1
            return {"ok": True}
        events, _ = await self.run_calls([("scroll_screen", {})] * 8, scroll, name="scroll_screen", side_effect=True)
        self.assertEqual(8, count)
        self.assertFalse(any(e.type == "truncated" for e in events))

    async def test_failed_actions_still_detect_stall(self):
        async def click():
            return {"ok": False, "status": "target_not_found"}
        events, _ = await self.run_calls([("click_element", {})] * 15, click, name="click_element", side_effect=True)
        self.assertEqual("spinning", next(e.data["reason"] for e in events if e.type == "truncated"))

    async def test_successful_action_with_unchanged_screen_still_detects_cycle(self):
        registry = ToolRegistry()
        clicks = []
        async def click():
            clicks.append(True)
            return {"ok": True}
        async def capture():
            return {"text": "screen", "path": f"/screenshots/{len(clicks)}.jpg", "_observation_image": "c2FtZQ=="}
        registry.register(Tool("click_element", "click", {"type": "object"}, click, side_effect=True))
        registry.register(Tool("screen_capture", "capture", {"type": "object"}, capture))
        llm = ScriptedLLM([[("click_element", {}), ("screen_capture", {})]] * 15)
        events = [e async for e in Agent(llm, registry, AgentConfig()).run("点击按钮", approval_mode="full")]
        self.assertEqual(6, len(clicks))
        self.assertEqual("spinning", next(e.data["reason"] for e in events if e.type == "truncated"))

    async def test_observation_metadata_preserves_unverified_status(self):
        async def send():
            return {"ok": False, "status": "sent_unverified", "verified": False,
                    "text": "尚未核实", "_observation_image": "c2FtZQ=="}
        events, llm = await self.run_calls([("send_message", {})], send, name="send_message", side_effect=True)
        result = next(e for e in events if e.type == "tool_result")
        self.assertFalse(result.data["ok"])
        context = next(m["content"] for m in llm.seen[-1] if m.get("role") == "tool")
        self.assertEqual("sent_unverified", json.loads(context)["status"])
        self.assertFalse(json.loads(context)["verified"])

    async def test_changed_observation_metadata_is_progress(self):
        count = 0
        async def inspect():
            nonlocal count
            count += 1
            return {"text": "screen", "page": count, "_observation_image": "c2FtZQ=="}
        events, _ = await self.run_calls([("inspect", {})] * 8, inspect)
        self.assertFalse(any(e.type == "truncated" for e in events))
        self.assertEqual(8, count)

    def test_interrupted_image_batch_has_one_result_per_call(self):
        messages = [{"role": "assistant", "tool_calls": [
            {"id": "first"}, {"id": "second"}, {"id": "third"},
        ]}, {"role": "tool", "tool_call_id": "first", "content": "done"},
            image_message("screen"), {"role": "tool", "tool_call_id": "second", "content": "done"}]
        snapshot = json.loads(json.dumps(messages))
        completed = _complete_tool_pairs(messages)
        self.assertEqual(snapshot, messages)
        self.assertEqual(["assistant", "tool", "tool", "tool", "user"], [m["role"] for m in completed])
        self.assertEqual(["first", "second", "third"], [m["tool_call_id"] for m in completed if m["role"] == "tool"])

    async def test_completed_image_batch_keeps_tool_results_contiguous(self):
        async def capture():
            return {"text": "screen", "_observation_image": "c2FtZQ=="}
        _, llm = await self.run_calls([[('screen_capture', {}), ('screen_capture', {})]], capture,
                                      name="screen_capture")
        trace = llm.seen[-1]
        start = next(i for i, m in enumerate(trace) if m.get("tool_calls"))
        self.assertEqual(["assistant", "tool", "tool", "user", "user"],
                         [m["role"] for m in trace[start:]])
        self.assertFalse(any("未完成" in m.get("content", "") for m in trace if m["role"] == "tool"))

    async def test_batch_stall_stops_and_pairs_all_tool_messages(self):
        count = 0
        async def inspect():
            nonlocal count
            count += 1
            return "unchanged"
        events, _ = await self.run_calls([[('inspect', {})] * 30] * 2, inspect)
        self.assertEqual(6, count)
        resume = next(e.data["resume_messages"] for e in events if e.type == "truncated")
        requested = [c["id"] for m in resume for c in m.get("tool_calls", [])]
        answered = [m["tool_call_id"] for m in resume if m.get("role") == "tool"]
        self.assertEqual(requested, answered)

    async def test_duplicate_launch_does_not_end_other_work(self):
        registry = ToolRegistry()
        launches, focuses = [], []
        async def launch(name):
            launches.append(name)
            return "已启动:" + name
        async def focus(window):
            focuses.append(window)
            return {"ok": True}
        registry.register(Tool("launch_app", "launch", {"type": "object"}, launch, side_effect=True))
        registry.register(Tool("focus_window", "focus", {"type": "object"}, focus, side_effect=True))
        llm = ScriptedLLM([("launch_app", {"name": "CAD"}), ("launch_app", {"name": "CAD"}),
                           ("focus_window", {"window": "CAD"})])
        events = [e async for e in Agent(llm, registry, AgentConfig()).run("打开CAD并继续工作", approval_mode="full")]
        self.assertEqual(["CAD"], launches)
        self.assertEqual(["CAD"], focuses)
        self.assertEqual("done", events[-1].type)

    def test_abort_checkpoint_pairs_unexecuted_calls_without_mutation(self):
        messages = [{"role": "assistant", "tool_calls": [
            {"id": "first", "function": {"name": "write"}},
            {"id": "second", "function": {"name": "send"}},
        ]}, {"role": "tool", "tool_call_id": "first", "content": "done"}]
        snapshot = json.loads(json.dumps(messages))
        resume = Agent._abort_event(messages).data["resume_messages"]
        self.assertEqual(snapshot, messages)
        self.assertEqual(["first", "second"], [m["tool_call_id"] for m in resume if m.get("role") == "tool"])
        self.assertIn("核对", resume[-1]["content"])

    async def test_model_failure_keeps_completed_tool_checkpoint(self):
        class BrokenLLM(ScriptedLLM):
            async def chat_stream_tools(self, messages, **kwargs):
                if self.turn:
                    raise RuntimeError("temporary gateway failure")
                async for item in super().chat_stream_tools(messages, **kwargs):
                    yield item
        registry = ToolRegistry()
        async def inspect():
            return "completed"
        registry.register(Tool("inspect", "inspect", {"type": "object"}, inspect))
        events = [e async for e in Agent(BrokenLLM([("inspect", {})]), registry, AgentConfig()).run("检查")]
        checkpoint = next(e for e in events if e.type == "checkpoint")
        self.assertEqual("service_error", checkpoint.data["reason"])
        self.assertTrue(any(m.get("role") == "tool" and m.get("content") == "completed" for m in checkpoint.data["resume_messages"]))
        self.assertEqual("error", events[-1].type)


class AttachmentContinuityTests(unittest.TestCase):
    def test_new_screen_only_drops_screens_not_user_images(self):
        messages = [image_message("diagram.png", attachment=True), image_message("old screen")]
        self.assertEqual(1, _drop_old_screenshots(messages))
        self.assertIsInstance(messages[0]["content"], list)
        self.assertIsInstance(messages[1]["content"], str)

    def test_latest_attachment_survives_many_screen_rounds(self):
        messages = [image_message("plan.png", attachment=True)] + [image_message(f"screen{i}") for i in range(8)]
        result, _ = compact_context_messages(messages, recent_rounds=1)
        self.assertIsInstance(result[0]["content"], list)

    def test_older_attachment_keeps_text_and_path_when_image_removed(self):
        messages = [image_message("old.png", attachment=True), {"role": "user", "content": "next"},
                    image_message("new.png", attachment=True)]
        result, _ = compact_context_messages(messages, recent_rounds=1)
        self.assertIsInstance(result[0]["content"], str)
        self.assertIn("D:/uploads/old.png", result[0]["content"])
        self.assertIn("用户上传的附件", result[0]["content"])

    def test_zero_recent_rounds_actually_compacts_all_old_results(self):
        messages = [{"role": "user", "content": "task"}, {"role": "tool", "content": "x" * 1500}]
        result, count = compact_context_messages(messages, recent_rounds=0)
        self.assertEqual(1, count)
        self.assertIn("早期工具结果已压缩", result[1]["content"])

    def test_followup_restores_latest_mixed_file_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("项目代号海棠", encoding="utf-8")
            (root / "plan.png").write_bytes(b"image")
            history = [{"role": "user", "attachments": [
                {"name": "note.txt", "type": "text/plain", "saved_name": "note.txt"},
                {"name": "plan.png", "type": "image/png", "saved_name": "plan.png"},
            ]}, {"role": "user", "content": "继续"}]
            with patch.object(server, "_ATTACHMENT_DIR", root), patch.object(server, "_compress_attachment_image", return_value="data:image/jpeg;base64,AAAA"):
                restored = server._restore_latest_attachments(history)
            self.assertEqual(["note.txt", "plan.png"], [item["name"] for item in restored])
            self.assertEqual("项目代号海棠", restored[0]["text"])
            self.assertIn("data_url", restored[1])

    def test_missing_new_group_does_not_silently_restore_older_group(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, "_ATTACHMENT_DIR", Path(tmp)):
            (Path(tmp) / "old.txt").write_text("old", encoding="utf-8")
            history = [{"role": "user", "attachments": [{"saved_name": "old.txt", "type": "text/plain"}]},
                       {"role": "user", "attachments": [{"saved_name": "missing.txt", "type": "text/plain"}]}]
            restored = server._restore_latest_attachments(history)
            self.assertEqual(1, len(restored))
            self.assertNotIn("path", restored[0])
            self.assertIn("不存在", restored[0]["note"])

    def test_cleanup_preserves_active_and_queued_attachments_until_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("active.txt", "queued.txt", "orphan.txt"):
                (root / name).write_text(name, encoding="utf-8")
            runtime = {"session": {"conversation": {
                "attachments_in_use": [{"path": str(root / "active.txt")}],
                "pending_inputs": [{"attachments": [{"path": str(root / "queued.txt")}]}],
            }}}
            with patch.object(server, "_ATTACHMENT_DIR", root), \
                 patch.object(server, "DESKTOP_CONVERSATIONS", {}), \
                 patch.object(server, "DESKTOP_HISTORY", []), \
                 patch.object(server, "_DESKTOP_ATTACHMENT_RUNTIMES", runtime):
                self.assertEqual(1, server._cleanup_attachments(remove_all_unreferenced=True))
                self.assertTrue((root / "active.txt").exists())
                self.assertTrue((root / "queued.txt").exists())
                runtime.clear()
                self.assertEqual(2, server._cleanup_attachments(remove_all_unreferenced=True))

    def test_multi_image_budget_compresses_group_before_dropping_members(self):
        img = Image.new("RGB", (1920, 1080), "white")
        buf = io.BytesIO(); img.save(buf, format="PNG")
        url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        prepared = [{"name": f"{i}.png", "data_url": url} for i in range(4)]
        with patch.object(server.CFG, "agent", SimpleNamespace(context_window=10000)):
            server._cap_attachment_image_budget(prepared)
        self.assertEqual(4, sum(bool(i.get("data_url")) for i in prepared))
        self.assertLessEqual(sum(server._data_url_tokens(i["data_url"]) for i in prepared), 4000)


class DesktopContinuityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reuse the existing offline socket harness, not a second server fixture.
        from backend.test_deferred_conversations import DeferredConversationTests
        self.harness = DeferredConversationTests()
        await self.harness.asyncSetUp()
        self.addCleanup(self.harness.stack.close)
        self.harness.stack.enter_context(patch.object(server, "_DESKTOP_ATTACHMENT_RUNTIMES", {}))

    async def asyncTearDown(self):
        await self.harness.disconnect()

    async def test_service_checkpoint_stays_private_and_continues_exact_trace(self):
        harness = self.harness
        trace = [{"role": "user", "content": "task"}, {"role": "assistant", "tool_calls": [
            {"id": "write1", "type": "function", "function": {"name": "write", "arguments": "{}"}},
        ]}, {"role": "tool", "tool_call_id": "write1", "content": "written once"}]
        calls = []
        class CheckpointAgent:
            def __init__(self, *args, **kwargs):
                self.memory = None
            async def run(self, text, **kwargs):
                calls.append(kwargs)
                if kwargs.get("resume_messages"):
                    yield SimpleNamespace(type="message_end", data={"content": "继续完成"})
                    yield SimpleNamespace(type="done", data={})
                else:
                    yield SimpleNamespace(type="checkpoint", data={"reason": "service_error", "resume_messages": trace})
                    yield SimpleNamespace(type="error", data={"where": "llm.chat", "msg": "临时故障"})
        harness.stack.enter_context(patch.object(server, "Agent", CheckpointAgent))
        await harness.connect()
        await harness.input()
        error = await harness.socket.event("error")
        await harness.socket.event("conversation_list")
        self.assertTrue(error["service_failed"])
        saved = list(server._RESUME_STATES.values())
        self.assertEqual(trace, next(item["messages"] for item in saved if item["messages"]))
        disk = json.loads(server._RESUME_PATH.read_text(encoding="utf-8"))
        self.assertIn("written once", json.dumps(disk))
        self.assertFalse(any(e["type"] == "checkpoint" or "resume_messages" in e["data"] for e in harness.socket.sent))
        harness.socket.send(type="continue_task", conversation_id="conv_draft")
        await harness.completed()
        self.assertEqual(trace, calls[-1]["resume_messages"])
        self.assertFalse(any(item.get("messages") for item in server._RESUME_STATES.values()))

    async def test_socket_retains_started_trail_upload_and_releases_unused_queue(self):
        harness = self.harness
        root = harness.path.parent / "attachments"
        root.mkdir()
        first, queued = root / "first.txt", root / "queued.txt"
        first.write_text("first", encoding="utf-8")
        queued.write_text("queued", encoding="utf-8")
        harness.stack.enter_context(patch.object(server, "_ATTACHMENT_DIR", root))
        harness.stack.enter_context(patch.object(server, "_prepare_attachments", side_effect=lambda items: items))
        await harness.connect()
        harness.gate.clear()
        await harness.input(attachments=[{"name": "first.txt", "path": str(first), "type": "text/plain"}])
        await harness.socket.event("input_ack")
        # Server signals progress through the existing fake agent call.
        import asyncio
        for _ in range(100):
            if harness.calls:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(harness.calls)
        await harness.input(text="随后处理第二个文件", client_id="input_2",
                            attachments=[{"name": "queued.txt", "path": str(queued), "type": "text/plain"}])
        await harness.socket.event("queued")
        self.assertEqual({"first.txt", "queued.txt"}, server._referenced_attachment_names())
        self.assertEqual(0, server._cleanup_attachments(remove_all_unreferenced=True))
        harness.socket.send(type="stop", conversation_id="conv_draft")
        await harness.socket.event("stopped")
        await harness.disconnect()
        self.assertEqual({}, server._DESKTOP_ATTACHMENT_RUNTIMES)
        # Started tasks are now durably traceable even when stopped; don't break their upload links.
        self.assertEqual({"first.txt"}, server._referenced_attachment_names())
        self.assertEqual(1, server._cleanup_attachments(remove_all_unreferenced=True))
        self.assertTrue(first.exists())
        self.assertFalse(queued.exists())


class ScreenshotQualityTests(unittest.TestCase):
    def test_auto_preserves_readable_full_hd_when_budget_allows(self):
        data, width, height = computer_module.compress_screenshot(Image.new("RGB", (1920, 1080), "white"))
        self.assertEqual((1920, 1080), (width, height))
        self.assertLessEqual(len(data), computer_module._SCREENSHOT_MAX_BYTES)

    def test_high_detail_preserves_larger_frame_with_same_byte_ceiling(self):
        data, width, height = computer_module.compress_screenshot(Image.new("RGB", (2560, 1440), "white"), detail="high")
        self.assertEqual((2560, 1440), (width, height))
        self.assertLessEqual(len(data), computer_module._SCREENSHOT_MAX_BYTES)

    def test_small_crop_is_not_upscaled(self):
        data, width, height = computer_module.compress_screenshot(Image.new("RGB", (320, 200), "white"), detail="high")
        self.assertEqual((320, 200), (width, height))

    def test_explicit_small_image_budget_is_respected(self):
        data, _, _ = computer_module.compress_screenshot(Image.new("RGB", (1920, 1080), "white"), max_bytes=3000)
        self.assertIsNotNone(data)
        self.assertLessEqual(len(data), 3000)

    def test_invalid_quality_options_are_rejected(self):
        img = Image.new("RGB", (100, 100))
        with self.assertRaises(ValueError):
            computer_module.compress_screenshot(img, detail="unbounded")
        with self.assertRaises(ValueError):
            computer_module.compress_screenshot(img, max_bytes=0)


if __name__ == "__main__":
    unittest.main()
