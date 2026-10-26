"""Task trail bounds, replay, early durability and real WebSocket regression tests."""
import asyncio
import json
import unittest
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from backend.core import task_trace
from backend.api import server
from backend import test_deferred_conversations as fixtures


class TaskTraceTests(unittest.TestCase):
    def test_retry_is_visible_durable_and_does_not_count_as_an_operation(self):
        trace = task_trace.new_trace("retry", "task")
        self.assertTrue(task_trace.consume(trace, "llm_retry", {
            "attempt": 2, "status_code": 200, "elapsed_ms": 1234, "empty_stream": True,
            "text": "网关返回空流，2 秒后重试（2/5）。",
        }))
        self.assertIn("2/5", trace["current"])
        self.assertEqual(0, trace["step_count"])
        self.assertEqual("notice", trace["events"][0]["kind"])
        self.assertEqual(1234, trace["events"][0]["elapsed_ms"])
        task_trace.consume(trace, "llm_timing", {"step": 1, "llm_ms": 4000})
        self.assertNotIn("重试", trace["current"])

    def test_timings_are_bounded_and_tool_time_excludes_approval_wait(self):
        trace = task_trace.new_trace("timing", "task")
        for step in range(200):
            task_trace.consume(trace, "llm_timing", {"step": step + 1, "llm_ms": 12})
        self.assertEqual(2400, trace["llm_ms"])
        self.assertEqual(task_trace.MAX_EVENTS, len(trace["model_timings"]))
        task_trace.consume(trace, "tool_call", {"name": "click"})
        trace["events"][-1]["ts"] -= 30000
        task_trace.consume(trace, "tool_result", {"name": "click", "tool_ms": 7, "ok": False})
        self.assertEqual(7, trace["tool_ms"])
        self.assertEqual(7, trace["events"][-1]["tool_ms"])
        self.assertGreaterEqual(trace["events"][-1]["duration_ms"], 30000)

    def test_completed_reply_is_not_duplicated_in_process(self):
        trace = task_trace.new_trace("turn", "task")
        task_trace.consume(trace, "message_end", {"content": "先查看文件"})
        task_trace.consume(trace, "tool_call", {"name": "fs_read_text", "arguments": {"path": "report.txt"}})
        task_trace.consume(trace, "tool_result", {"name": "fs_read_text", "ok": True})
        task_trace.consume(trace, "message_end", {"content": "已完成"})
        task_trace.finish(trace, "completed", final_reply="已完成")
        self.assertEqual(["message", "tool"], [event["kind"] for event in trace["events"]])
        self.assertEqual("done", trace["events"][1]["status"])
        self.assertEqual(1, trace["step_count"])

    def test_bounds_and_no_raw_success_or_media(self):
        trace = task_trace.new_trace("turn", "task")
        for _ in range(300):
            task_trace.consume(trace, "tool_call", {"name": "read", "arguments": {"path": "x" * 10000}})
            task_trace.consume(trace, "tool_result", {"name": "read", "ok": True, "result": "VERY_LARGE_RAW" * 10000})
            task_trace.consume(trace, "message_end", {"content": "m" * 10000})
        task_trace.consume(trace, "screenshot", {"image": "PRIVATE_IMAGE_PAYLOAD"})
        saved = json.dumps(trace, ensure_ascii=False)
        self.assertGreater(trace["omitted"], 0)
        self.assertLessEqual(len(trace["events"]), task_trace.MAX_EVENTS)
        self.assertLessEqual(len(saved.encode()), task_trace.MAX_BYTES)
        self.assertNotIn("VERY_LARGE_RAW", saved)
        self.assertNotIn("PRIVATE_IMAGE_PAYLOAD", saved)
        self.assertEqual(300, trace["step_count"])

    def test_waiting_stopped_error_and_restart(self):
        trace = task_trace.new_trace("turn", "task")
        task_trace.consume(trace, "tool_call", {"name": "run"})
        task_trace.consume(trace, "confirm_needed", {})
        self.assertEqual("waiting", trace["status"])
        messages = [{"task_trace": trace}]
        task_trace.recover(messages)
        self.assertEqual("interrupted", trace["status"])
        self.assertEqual("interrupted", trace["events"][0]["status"])
        task_trace.consume(trace, "error", {"msg": "服务断开"})
        task_trace.finish(trace, "failed")
        self.assertEqual("服务断开", trace["events"][-1]["text"])

    def test_model_history_has_no_trail_or_pending_placeholder(self):
        history = [{"role": "user", "content": "before"},
                   {"role": "assistant", "content": "reply", "task_trace": {"events": ["PRIVATE"]}},
                   {"role": "assistant", "content": "", "task_trace": {"status": "running"}}]
        result = server._recent_history(history, 2)
        self.assertEqual(["before", "reply"], [item["content"] for item in result])
        self.assertNotIn("task_trace", result[1])
        self.assertEqual([], server._recent_history(history, 0))


    def test_concurrent_checkpoints_never_remove_another_writers_tempfile(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server, "_CONVERSATIONS_PATH", Path(folder) / "conversations.json"):
            history = {"default": server._new_conversation("default", messages=[
                {"role": "assistant", "content": "result", "task_trace": task_trace.new_trace("id", "task")}
            ])}
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: server._save_conversations(history), range(80)))
            self.assertTrue(all(results))
            saved = json.loads(server._CONVERSATIONS_PATH.read_text(encoding="utf-8"))
            self.assertEqual("result", saved["default"]["messages"][0]["content"])

    def test_raw_return_size_utf8_and_task_budget(self):
        trace = task_trace.new_trace("turn", "task")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = task_trace.store_return(trace, "中文" * 100000, root)
            text = (root / raw["saved_name"]).read_text(encoding="utf-8")
            self.assertTrue(raw["truncated"])
            self.assertLessEqual(len(text.encode()), task_trace.MAX_RAW_STEP_BYTES)
            trace["raw_bytes"] = task_trace.MAX_RAW_TASK_BYTES - 4
            raw = task_trace.store_return(trace, "123456", root)
            self.assertEqual("1234", (root / raw["saved_name"]).read_text())
            self.assertTrue(raw["truncated"])
            self.assertIn("error", task_trace.store_return(trace, "not stored", root))

    def test_raw_return_saving_failure_is_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "not-a-directory"
            root.write_text("blocking file")
            raw = task_trace.store_return(task_trace.new_trace("turn", "task"), "result", root)
            self.assertIn("保存失败", raw["error"])
            self.assertNotIn("saved_name", raw)

    def test_raw_return_attachment_reference_survives_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            trace = task_trace.new_trace("turn", "task")
            raw = task_trace.store_return(trace, "result", root)
            trace["events"] = [{"kind": "tool", "raw_return": raw}]
            conversations = {"default": {"messages": [{"task_trace": trace}]}}
            with patch.object(server, "_ATTACHMENT_DIR", root), patch.object(server, "DESKTOP_CONVERSATIONS", conversations), patch.object(server, "DESKTOP_HISTORY", []):
                self.assertEqual(0, server._cleanup_attachments(remove_all_unreferenced=True))
                self.assertIsNotNone(server._saved_attachment_path(raw["saved_name"]))
                trace["events"].clear()
                self.assertEqual(1, server._cleanup_attachments(remove_all_unreferenced=True))
                self.assertIsNone(server._saved_attachment_path("../outside.txt"))

    def test_resume_binding_survives_disk_and_expiry_is_checked(self):
        trace_id = "ab" + "12" * 15
        with tempfile.TemporaryDirectory() as folder, patch.object(server, "_RESUME_PATH", Path(folder) / "resume.json"), patch.object(server, "_RESUME_STATES", {}):
            server._save_resume_state("desktop", [{"role": "user", "content": "task"}], {"task_trace_id": trace_id}, "stopped")
            self.assertEqual(trace_id, server._load_resume_states()["desktop"]["task"]["task_trace_id"])
            trace = task_trace.new_trace(trace_id, "task"); task_trace.finish(trace, "interrupted")
            server._refresh_trace_resume(trace, "desktop")
            self.assertTrue(trace["can_resume"])
            server._RESUME_STATES["desktop"]["updated_at"] = 0
            server._refresh_trace_resume(trace, "desktop")
            self.assertFalse(trace["can_resume"])


    def test_resume_saving_failure_does_not_claim_restart_durability(self):
        trace = task_trace.new_trace("ab" + "12" * 15, "task"); task_trace.finish(trace, "stopped")
        with patch.object(server, "_RESUME_STATES", {}), patch.object(server, "_persist_resume_states", return_value=False):
            server._save_resume_state("desktop", [{"role": "user", "content": "task"}], {"task_trace_id": trace["id"]}, "stopped")
            server._refresh_trace_resume(trace, "desktop")
            self.assertTrue(trace["can_resume"])
            self.assertFalse(trace["resume_persisted"])



class TaskTraceWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_status_is_published_and_persisted_before_final_reply(self):
        self.script([("llm_retry", {"text": "网关返回空流，2 秒后重试（2/5）。",
                                    "attempt": 2, "status_code": 200, "elapsed_ms": 1234, "empty_stream": True}),
                     ("llm_timing", {"step": 1, "llm_ms": 4000}),
                     ("message", {"content": "完成"}), ("done", {})])
        await self.connect(); await self.input(); await self.completed()
        entry = json.loads(self.path.read_text(encoding="utf-8"))["conv_draft"]["messages"][-1]
        self.assertEqual("完成", entry["content"])
        self.assertEqual(0, entry["task_trace"]["step_count"])
        self.assertEqual(4000, entry["task_trace"]["llm_ms"])
        self.assertEqual(2, entry["task_trace"]["events"][0]["attempt"])

    # Reuse the real endpoint fixture without re-running all its inherited tests.
    asyncSetUp = fixtures.DeferredConversationTests.asyncSetUp
    asyncTearDown = fixtures.DeferredConversationTests.asyncTearDown
    connect = fixtures.DeferredConversationTests.connect
    disconnect = fixtures.DeferredConversationTests.disconnect
    input = fixtures.DeferredConversationTests.input
    completed = fixtures.DeferredConversationTests.completed

    def script(self, events, *, wait_after=None):
        owner = self
        class Agent:
            def __init__(self, *args, **kwargs):
                self.memory = None
                self.approval_state = kwargs["approval_state"]
            async def run(self, text, **kwargs):
                owner.calls.append((text, kwargs))
                for index, (kind, data) in enumerate(events):
                    yield SimpleNamespace(type=kind, data=dict(data))
                    if index == wait_after:
                        await owner.gate.wait()
        self.stack.enter_context(patch.object(server, "Agent", Agent))

    async def test_early_save_recover_and_no_input_duplicate(self):
        self.gate.clear()
        self.script([("tool_call", {"name": "fs_read_text", "arguments": {"path": "x"}}),
                     ("tool_result", {"name": "fs_read_text", "ok": True}),
                     ("message_end", {"content": "最终结果"}), ("done", {})], wait_after=0)
        await self.connect()
        await self.input()
        await self.socket.event("tool_call")
        await self.socket.event("task_trace")
        # Wait for the step snapshot (the first trace event is the start snapshot).
        for _ in range(100):
            saved = json.loads(self.path.read_text(encoding="utf-8"))["conv_draft"]["messages"]
            if saved[-1]["task_trace"]["events"]:
                break
            await asyncio.sleep(.01)
        self.assertEqual(2, len(saved))
        self.assertEqual("running", saved[-1]["task_trace"]["status"])
        recovered = server._load_conversations()["conv_draft"]["messages"]
        self.assertEqual("interrupted", recovered[-1]["task_trace"]["status"])
        self.assertEqual([], self.calls[0][1]["history"])
        self.gate.set()
        await self.completed()
        await self.disconnect()
        await self.connect()
        self.socket.send(type="conversation_select", conversation_id="conv_draft")
        history = await self.socket.event("history")
        self.assertEqual(2, len(history["messages"]))
        self.assertEqual("最终结果", history["messages"][-1]["content"])
        self.assertEqual("completed", history["messages"][-1]["task_trace"]["status"])
        self.assertEqual("done", history["messages"][-1]["task_trace"]["events"][0]["status"])

    async def test_intermediate_messages_and_final_result_survive_reopen(self):
        self.script([("message_end", {"content": "先检查目录"}),
                     ("tool_call", {"name": "fs_list_dir"}),
                     ("tool_result", {"name": "fs_list_dir", "ok": True}),
                     ("message_end", {"content": "已经找到，继续处理"}),
                     ("message_end", {"content": "任务完成"}), ("done", {})])
        await self.connect(); await self.input(); await self.completed()
        entry = json.loads(self.path.read_text(encoding="utf-8"))["conv_draft"]["messages"][-1]
        self.assertEqual("任务完成", entry["content"])
        self.assertEqual(["先检查目录", "已经找到，继续处理"],
                         [item["text"] for item in entry["task_trace"]["events"] if item["kind"] == "message"])
        await self.input(client_id="turn2"); await self.completed()
        self.assertEqual(2, len(self.calls[1][1]["history"]))
        self.assertNotIn("task_trace", self.calls[1][1]["history"][-1])

    async def test_failure_does_not_promote_preamble_to_final_reply(self):
        self.script([("message_end", {"content": "我现在开始处理"}),
                     ("error", {"where": "llm.chat", "msg": "网络断开"})])
        await self.connect(); await self.input()
        data = await self.socket.event("error")
        self.assertTrue(data["task_finished"])
        self.assertEqual("failed", data["trace"]["status"])
        self.assertEqual("", data["final_reply"])
        saved = json.loads(self.path.read_text(encoding="utf-8"))["conv_draft"]["messages"][-1]
        self.assertEqual("", saved["content"])
        self.assertEqual("网络断开", saved["task_trace"]["events"][-1]["text"])

    async def test_stopped_retains_steps_without_false_completion(self):
        self.script([("tool_call", {"name": "run_command"}),
                     ("done", {"aborted": True})])
        await self.connect(); await self.input(); await self.completed()
        trace = self.conversations["conv_draft"]["messages"][-1]["task_trace"]
        self.assertEqual("stopped", trace["status"])
        self.assertEqual("stopped", trace["events"][0]["status"])

    async def test_trails_are_redacted_before_persistence(self):
        self.script([("tool_call", {"name": "run_command", "arguments": {"command": "use TEST_SECRET_VALUE"}}),
                     ("tool_result", {"name": "run_command", "ok": False, "result": "TEST_SECRET_VALUE invalid"}),
                     ("message_end", {"content": "处理完毕"}), ("done", {})])
        with patch.object(server, "_known_secret_values", return_value=["TEST_SECRET_VALUE"]):
            await self.connect(); await self.input(); await self.completed()
        self.assertNotIn("TEST_SECRET_VALUE", self.path.read_text(encoding="utf-8"))

    async def test_saving_failure_is_visible_not_silently_reported_durable(self):
        await self.connect()
        with patch.object(server, "_save_conversations", return_value=False):
            await self.input()
            data = await self.socket.event("error")
            self.assertEqual("history.save", data["where"])
            await self.completed()

    async def test_retry_without_checkpoint_never_replays_side_effects(self):
        self.gate.clear()
        self.script([("tool_call", {"name": "write"}),
                     ("tool_result", {"name": "write", "ok": True}),
                     ("done", {})], wait_after=0)
        await self.connect(); await self.input(); await self.socket.event("tool_call")
        await self.disconnect()
        previous = self.conversations["conv_draft"]["messages"][-1]["task_trace"]
        self.assertEqual("interrupted", previous["status"])
        previous_id = previous["id"]
        self.gate.set()
        await self.connect(); await self.input()
        error = await self.socket.event("error")
        self.assertIn("已停止自动重发", error["msg"])
        messages = self.conversations["conv_draft"]["messages"]
        self.assertEqual(2, len(messages))
        self.assertEqual(previous_id, messages[-1]["task_trace"]["id"])
        self.assertEqual(1, len(self.calls))
        self.assertEqual("interrupted", previous["status"])

    async def test_terminal_delivery_failure_does_not_corrupt_completed_work(self):
        await self.connect()
        original_send = self.socket.send_text
        async def fail_done(payload):
            if json.loads(payload)["type"] == "done":
                raise ConnectionError("connection lost during terminal delivery")
            await original_send(payload)
        self.socket.send_text = fail_done
        await self.input()
        await self.socket.event("error")
        await self.socket.event("conversation_list")
        saved = json.loads(self.path.read_text(encoding="utf-8"))["conv_draft"]["messages"][-1]
        self.assertEqual("completed", saved["task_trace"]["status"])
        self.assertEqual("已完成", saved["content"])
        await self.disconnect(); await self.connect(); await self.input()
        await self.socket.event("input_done")
        self.assertEqual(1, len(self.calls))

    async def test_immediate_confirmation_not_lost_during_trail_flush(self):
        owner = self
        class ConfirmAgent:
            def __init__(self, *args, **kwargs):
                self.memory = None
                self.confirm = kwargs["confirm_fn"]
            async def run(self, text, **kwargs):
                yield SimpleNamespace(type="tool_call", data={"name": "run_command"})
                yield SimpleNamespace(type="confirm_needed", data={"name": "run_command"})
                allowed = await self.confirm("run_command", {})
                owner.assertTrue(allowed)
                yield SimpleNamespace(type="tool_result", data={"name": "run_command", "ok": True})
                yield SimpleNamespace(type="message_end", data={"content": "已确认并完成"})
                yield SimpleNamespace(type="done", data={})
        self.stack.enter_context(patch.object(server, "Agent", ConfirmAgent))
        await self.connect(); await self.input(); await self.socket.event("confirm_needed")
        # Immediately respond; don't wait for the following display snapshot.
        self.socket.send(type="confirm", allow=True, conversation_id="conv_draft")
        await self.completed()
        self.assertTrue(any(event["type"] == "task_trace" and
                            event["data"]["trace"]["current"] == "确认已收到，正在执行"
                            for event in self.socket.sent))
        self.assertEqual("completed", self.conversations["conv_draft"]["messages"][-1]["task_trace"]["status"])

    async def test_raw_returns_redacted_and_larger_than_ui_summary(self):
        payload = {"password": "DO_NOT_STORE", "output": "中文结果\n" * 1600,
                   "_display_image": "PRIVATE_MEDIA", "summary": "完成"}
        self.script([("tool_call", {"name": "read"}),
                     ("tool_result", {"name": "read", "ok": True, "result": json.dumps(payload)}),
                     ("tool_call", {"name": "write"}),
                     ("tool_result", {"name": "write", "ok": False, "result": "known_secret bad"}),
                     ("done", {})])
        with patch.object(server, "_known_secret_values", return_value=["known_secret"]):
            await self.connect(); await self.input(); await self.completed()
        trace = self.conversations["conv_draft"]["messages"][-1]["task_trace"]
        read, write = trace["events"]
        raw = (server._ATTACHMENT_DIR / read["raw_return"]["saved_name"]).read_text(encoding="utf-8")
        self.assertGreater(len(raw), 4000)
        self.assertNotIn("DO_NOT_STORE", raw)
        self.assertNotIn("PRIVATE_MEDIA", raw)
        self.assertEqual("中文结果\n" * 1600, json.loads(raw)["output"])
        failure = (server._ATTACHMENT_DIR / write["raw_return"]["saved_name"]).read_text(encoding="utf-8")
        self.assertEqual("*** bad", failure)
        self.assertNotIn("raw_return", self.calls[0][1]["history"])
        self.assertEqual(2, write["step"])

    async def test_continue_reuses_card_and_checkpoint_not_completed_tools(self):
        checkpoint = [{"role": "user", "content": "task"}, {"role": "assistant", "tool_calls": [
            {"id": "one", "type": "function", "function": {"name": "write", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "one", "content": "written once"}]
        calls = self.calls
        class ResumeAgent:
            def __init__(self, *args, **kwargs): self.memory = None
            async def run(self, text, **kwargs):
                calls.append((text, kwargs))
                if kwargs.get("resume_messages"):
                    yield SimpleNamespace(type="message_end", data={"content": "继续完成"})
                    yield SimpleNamespace(type="done", data={})
                else:
                    yield SimpleNamespace(type="tool_call", data={"name": "write"})
                    yield SimpleNamespace(type="tool_result", data={"name": "write", "result": "written once"})
                    yield SimpleNamespace(type="aborted", data={"resume_messages": checkpoint})
                    yield SimpleNamespace(type="done", data={"aborted": True})
        self.stack.enter_context(patch.object(server, "Agent", ResumeAgent))
        await self.connect(); await self.input(); await self.completed()
        messages = self.conversations["conv_draft"]["messages"]
        trace = messages[-1]["task_trace"]; trace_id = trace["id"]
        self.assertTrue(trace["can_resume"])
        self.assertEqual(trace_id, next(state for state in server._load_resume_states().values() if state.get("messages"))["task"]["task_trace_id"])
        await self.disconnect()
        # Simulate reloading persistent checkpoints after an application restart.
        server._RESUME_STATES.clear(); server._RESUME_STATES.update(server._load_resume_states())
        await self.connect()
        self.socket.send(type="continue_task", conversation_id="conv_draft", task_trace_id="old-card")
        error = await self.socket.event("error")
        self.assertIn("不会从头重复", error["msg"])
        self.assertEqual(1, len(calls))
        self.socket.send(type="continue_task", conversation_id="default", task_trace_id=trace_id)
        await self.socket.event("error")
        self.assertEqual(1, len(calls))
        self.socket.send(type="continue_task", conversation_id="conv_draft", task_trace_id=trace_id)
        await self.completed()
        self.assertEqual(checkpoint, calls[-1][1]["resume_messages"])
        self.assertEqual(2, len(messages))
        self.assertEqual(trace_id, messages[-1]["task_trace"]["id"])
        self.assertEqual(1, trace["step_count"])
        self.assertEqual("completed", trace["status"])
        self.assertFalse(trace["can_resume"])
        self.assertEqual("继续完成", messages[-1]["content"])
        self.assertTrue(any(item["kind"] == "notice" for item in trace["events"]))
        self.socket.send(type="continue_task", conversation_id="conv_draft", task_trace_id=trace_id)
        await self.socket.event("error")
        self.assertEqual(2, len(calls))

    async def test_raw_saving_failure_keeps_task_result_but_explains_missing_return(self):
        self.script([("tool_call", {"name": "read"}), ("tool_result", {"name": "read", "result": "result"}),
                     ("message_end", {"content": "完成"}), ("done", {})])
        blocker = self.path.parent / "blocker"; blocker.write_text("not a directory")
        with patch.object(server, "_ATTACHMENT_DIR", blocker):
            await self.connect(); await self.input(); await self.completed()
        message = self.conversations["conv_draft"]["messages"][-1]
        self.assertEqual("完成", message["content"])
        self.assertEqual("completed", message["task_trace"]["status"])
        self.assertIn("保存失败", message["task_trace"]["events"][0]["raw_return"]["error"])

    async def test_persisted_terminal_trace_blocks_replay_after_dedup_cache_loss(self):
        self.script([("tool_call", {"name": "write"}), ("tool_result", {"name": "write", "result": "written"}),
                     ("message_end", {"content": "完成"}), ("done", {})])
        await self.connect(); await self.input(); await self.completed()
        await self.disconnect(); server._COMPLETED_CLIENT_INPUTS.clear()
        await self.connect(); await self.input(); await self.socket.event("input_done")
        self.assertEqual(1, len(self.calls))
        self.assertEqual(2, len(self.conversations["conv_draft"]["messages"]))

    async def test_checkpoint_consumed_between_request_and_start_does_not_restart_task(self):
        self.script([("aborted", {"resume_messages": [{"role": "user", "content": "task"}]}), ("done", {"aborted": True})])
        await self.connect(); await self.input(); await self.completed()
        trace_id = self.conversations["conv_draft"]["messages"][-1]["task_trace"]["id"]
        original = server._resume_state
        reads = 0
        def checkpoint_race(scope):
            nonlocal reads
            reads += 1
            return original(scope) if reads == 1 else server._empty_resume_state()
        with patch.object(server, "_resume_state", side_effect=checkpoint_race):
            self.socket.send(type="continue_task", conversation_id="conv_draft", task_trace_id=trace_id)
            error = await self.socket.event("error")
            await self.socket.event("conversation_list")
        self.assertIn("未重新执行", error["msg"])
        self.assertEqual(1, len(self.calls))
        self.assertEqual(2, len(self.conversations["conv_draft"]["messages"]))
