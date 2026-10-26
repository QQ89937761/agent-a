"""不联网的关键回归检查。"""
import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

from backend.core.agent import (
    Agent, TOOL_RESULT_REF_TTL_SECONDS, cleanup_result_refs, compact_context_messages,
    compact_for_limit, drop_oldest_round, estimate_messages_tokens, estimate_text_tokens,
    _looks_dangerous, _looks_financial, _looks_remote_confirmation,
)
from backend.core.bridge import HubBridge
from backend.core.config import AgentConfig, load_config
from backend.api import server
from backend.core.tools import Tool, ToolRegistry
from backend.modules.computer.user_tool_module import UserToolManager
from backend.modules.memory.memory_module import _JsonBackend
from backend.modules.memory.embedder import FastEmbedder
from backend.modules.memory.vector_memory import VectorMemory


class FakeWebSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = None

    async def receive_text(self):
        if not self.messages:
            raise RuntimeError("disconnect")
        return self.messages.pop(0)

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=1000):
        self.closed = code


class FakeEmbedder:
    dim = 3

    def embed(self, texts):
        return [[float(len(text)), 1.0, 0.5] for text in texts]


class MemoryTests(unittest.TestCase):
    def test_operational_facts_are_queued_and_immediately_recalled(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-memory-facts-") as tmp:
            memory = VectorMemory(None, None, Path(tmp), batch_turns=5)
            turns = [
                "我的云服务器是 203.0.113.10",
                "记住服务器密码在设置里",
                "云服务器地址 203.0.113.10 用户名 root 密码 xxx",
                "帮我登录云端看看 ZHXA 还活着吗",
                "8443 端口放行了",
            ]
            for text in turns:
                self.assertTrue(memory._worth_extracting([{"role": "user", "content": text}]))
            asyncio.run(memory.remember([{"role": "user", "content": turns[2]}]))
            recalled = asyncio.run(memory.recall("203.0.113.10"))
            self.assertTrue(any("203.0.113.10" in item for item in recalled))
            self.assertFalse(memory._worth_extracting([{"role": "user", "content": "好的"}]))

    def test_fastembed_uses_bounded_batches(self):
        calls = []

        class FakeModel:
            def embed(self, texts, batch_size):
                calls.append(batch_size)
                return [[1.0, 2.0] for _ in texts]

        embedder = FastEmbedder.__new__(FastEmbedder)
        embedder._model = FakeModel()
        self.assertEqual([[1.0, 2.0]], embedder.embed(["测试"]))
        self.assertEqual([8], calls)

    def test_json_fallback_does_not_return_unrelated_memory(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-memory-") as tmp:
            backend = _JsonBackend(Path(tmp) / "memory.json")
            backend.remember([{"role": "user", "content": "我喜欢黑咖啡"}])
            self.assertEqual([], backend.recall("火箭进度"))

    def test_vector_model_loads_later_and_backfills_existing_memory(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-vector-backfill-") as tmp:
            memory = VectorMemory(None, None, Path(tmp))
            memory.add("关键词记忆先立即可用")
            self.assertEqual(1, memory.set_embedder(FakeEmbedder(), "fake-v1"))
            self.assertEqual(0, memory.set_embedder(FakeEmbedder(), "fake-v1"))
            self.assertEqual(1, memory.set_embedder(FakeEmbedder(), "fake-v2"))

    def test_project_memory_is_incremental_and_source_aware(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-project-memory-") as tmp:
            root = Path(tmp) / "project"
            data = Path(tmp) / "memory"
            root.mkdir()
            note = root / "architecture.md"
            note.write_text("项目记忆采用 SQLite，并保留原文件路径。", encoding="utf-8")
            memory = VectorMemory(None, None, data, project_roots=())

            first = memory.index_project(root)
            self.assertEqual(1, first["indexed"])
            nested = root / "nested"
            nested.mkdir()
            with self.assertRaisesRegex(ValueError, "重叠"):
                memory.add_project_root(nested)
            results = memory.search("项目的记忆架构", namespace="project")
            self.assertTrue(results)
            self.assertEqual(str(note.resolve()), results[0]["source_path"])
            self.assertTrue(results[0]["source_ref"].startswith("L1"))

            second = memory.index_project(root)
            self.assertEqual(1, second["unchanged"])
            note.write_text("项目工具支持自测、重载和版本回滚。", encoding="utf-8")
            third = memory.index_project(root)
            self.assertEqual(1, third["indexed"])
            self.assertFalse(memory.search("SQLite 原文件路径", namespace="project"))
            self.assertTrue(memory.search("工具版本回滚", namespace="project"))
            status = memory.project_status()
            self.assertEqual(1, status[0]["file_count"])
            self.assertGreater(status[0]["chunk_count"], 0)

            memory_id = memory.add("发布版本是 1.0")
            self.assertTrue(memory.update(memory_id, "发布版本是 2.0"))
            personal = memory.list_memories("personal")
            self.assertEqual("发布版本是 2.0", personal[0]["text"])
            self.assertNotIn("发布版本是 1.0", [item["text"] for item in personal])

            snapshot = memory.export_snapshot()
            snapshot_path = Path(tmp) / "snapshot.sqlite3"
            snapshot_path.write_bytes(snapshot)
            conn = sqlite3.connect(snapshot_path)
            try:
                self.assertGreater(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 0)
            finally:
                conn.close()
            self.assertTrue(memory.remove_project(root))
            self.assertFalse(memory.project_status())
            self.assertFalse(memory.search("工具版本回滚", namespace="project"))


class MemoryAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_flush_keeps_embedding_and_database_work_off_event_loop(self):
        started = threading.Event()
        release = threading.Event()
        embed_threads = []
        connect_threads = []

        class BlockingEmbedder:
            def embed(self, texts):
                embed_threads.append(threading.get_ident())
                if len(embed_threads) == 1:
                    started.set()
                    release.wait(2)
                return [[1.0, 0.5] for _ in texts]

        class LLM:
            async def chat(self, messages, temperature=0.1):
                return {"content": "我喜欢绿色咖啡杯"}

        with tempfile.TemporaryDirectory(prefix="xiaoa-flush-thread-") as tmp:
            memory = VectorMemory(BlockingEmbedder(), LLM(), Path(tmp))
            original_connect = memory._connect

            def tracked_connect():
                connect_threads.append(threading.get_ident())
                return original_connect()

            memory._connect = tracked_connect
            memory._pending.append([{
                "role": "user", "content": "我最近明确说过自己喜欢绿色咖啡杯，请记住。",
            }])
            event_loop_thread = threading.get_ident()
            flush_task = asyncio.create_task(memory.flush())
            began = time.perf_counter()
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                self.assertLess(time.perf_counter() - began, 1.0)
                self.assertFalse(flush_task.done())
            finally:
                release.set()
            self.assertEqual(["我喜欢绿色咖啡杯"], await flush_task)

        self.assertEqual(2, len(embed_threads))
        self.assertTrue(all(thread != event_loop_thread for thread in embed_threads))
        self.assertEqual(2, len(connect_threads))
        self.assertTrue(all(thread != event_loop_thread for thread in connect_threads))


@unittest.skipUnless(os.name == "nt", "用户工具使用 Windows PowerShell")
class UserToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_run_upgrade_and_rollback(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-user-tool-") as tmp:
            registry = ToolRegistry()
            manager = UserToolManager(Path(tmp), registry)
            schema = {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            }
            script_v1 = (
                "$p = $env:XIAOA_TOOL_ARGS | ConvertFrom-Json\n"
                "Write-Output ('v1 hello ' + $p.name)\n"
            )
            created = await asyncio.to_thread(
                manager.create, name="hello_user", description="向用户问好",
                parameters=schema, script=script_v1, test_args={"name": "test"},
                side_effect=False, timeout=20,
            )
            self.assertEqual(1, created["version"])
            self.assertEqual("v1 hello Codex", await registry.call("hello_user", {"name": "Codex"}))
            with self.assertRaisesRegex(ValueError, "缺少必填参数"):
                await registry.call("hello_user", {})
            with self.assertRaisesRegex(ValueError, "类型应为 string"):
                await registry.call("hello_user", {"name": 123})

            script_v2 = script_v1.replace("v1 hello", "v2 hello")
            upgraded = await asyncio.to_thread(
                manager.create, name="hello_user", description="向用户问好",
                parameters=schema, script=script_v2, test_args={"name": "test"},
                side_effect=False, timeout=20,
            )
            self.assertEqual(2, upgraded["version"])
            self.assertEqual("v2 hello Codex", await registry.call("hello_user", {"name": "Codex"}))

            rolled_back = await asyncio.to_thread(manager.rollback, "hello_user")
            self.assertEqual(1, rolled_back["version"])
            self.assertEqual("v1 hello Codex", await registry.call("hello_user", {"name": "Codex"}))


class DynamicToolAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_tool_is_not_executed_and_model_is_told_to_reselect(self):
        class UnknownThenDoneLLM:
            def __init__(self):
                self.turn = 0
                self.messages = []

            async def chat_stream_tools(self, messages, **_kwargs):
                self.turn += 1
                self.messages.append(messages)
                if self.turn == 1:
                    yield "done", {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "unknown", "type": "function",
                        "function": {"name": "triple_click_placeholder", "arguments": "{}"},
                    }]}
                else:
                    yield "done", {"role": "assistant", "content": "三击工具不可用，未执行操作。"}

        llm = UnknownThenDoneLLM()
        events = [event async for event in Agent(
            llm, ToolRegistry(), AgentConfig(max_steps=3),
        ).run("三击")]

        result = next(event for event in events if event.type == "tool_result")
        self.assertFalse(result.data["ok"])
        self.assertIn("没有执行", result.data["result"])
        self.assertIn("已注册工具", result.data["result"])
        self.assertEqual("done", events[-1].type)
        self.assertTrue(any(
            message.get("role") == "tool" and "没有执行" in message.get("content", "")
            for message in llm.messages[1]
        ))

    async def test_agent_reports_context_spin_and_final_step_limits_separately(self):
        class SummaryLLM:
            async def chat_stream_tools(self, _messages, tools=None, **_kwargs):
                yield "done", {"role": "assistant", "content": "已总结"}

        context_events = [event async for event in Agent(
            SummaryLLM(), ToolRegistry(), AgentConfig(max_steps=10, context_window=4096),
        ).run("继续", resume_messages=[
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "不可压缩的原话" + "x" * 20000},
        ])]
        context_stop = next(event for event in context_events if event.type == "truncated")
        # 光 user 原话就超线,压缩/丢轮都帮不上 → 应报 context_exhausted(开新对话),而非 context_limit(继续干)
        self.assertEqual("context_exhausted", context_stop.data["reason"])
        self.assertTrue(any(
            "这轮对话太长" in event.data.get("content", "")
            for event in context_events if event.type == "message_end"
        ))

        registry = ToolRegistry()
        called = []

        async def inspect(value: int):
            called.append(value)
            return "ok"

        registry.register(Tool(
            "inspect", "检查", {
                "type": "object", "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            }, inspect,
        ))

        class RepeatingLLM:
            def __init__(self, repeat: bool):
                self.turn = 0
                self.repeat = repeat

            async def chat_stream_tools(self, _messages, tools=None, **_kwargs):
                if tools is None:
                    yield "done", {"role": "assistant", "content": "已总结"}
                    return
                self.turn += 1
                value = 1 if self.repeat else self.turn
                yield "done", {"role": "assistant", "content": "", "tool_calls": [{
                    "id": f"call-{self.turn}", "type": "function",
                    "function": {"name": "inspect", "arguments": json.dumps({"value": value})},
                }]}

        spin_events = [event async for event in Agent(
            RepeatingLLM(True), registry, AgentConfig(max_steps=20),
        ).run("重复检查")]
        spin_stop = next(event for event in spin_events if event.type == "truncated")
        self.assertEqual("spinning", spin_stop.data["reason"])
        self.assertEqual([1] * 6, called)  # Replanning chance before the stall stop.

        called.clear()
        with mock.patch("backend.core.agent._HARD_STEP_LIMIT", 6):
            step_events = [event async for event in Agent(
                RepeatingLLM(False), registry, AgentConfig(max_steps=2),
            ).run("多步检查")]
        step_stop = next(event for event in step_events if event.type == "truncated")
        self.assertEqual("step_limit", step_stop.data["reason"])
        # 40.3:有进展就自动续跑,撞的是绝对安全上限 max(_HARD_STEP_LIMIT, 10×max_steps)=20
        self.assertEqual(list(range(1, 21)), called)
        self.assertEqual(200, AgentConfig().max_steps)

    async def test_large_tool_result_is_offloaded_deduplicated_and_retrievable(self):
        full_result = "早期事实：项目代号是海棠。\n" + "x" * 20000 + "\n末尾完成"
        calls = []
        registry = ToolRegistry()

        async def read_file(path: str):
            calls.append(path)
            return full_result

        registry.register(Tool(
            "fs_read_text", "读取文件", {
                "type": "object", "properties": {"path": {"type": "string"}},
                "required": ["path"],
            }, read_file,
        ))

        class RefLLM:
            def __init__(self):
                self.turn = 0
                self.tool_messages = []

            async def chat_stream_tools(self, messages, **_kwargs):
                self.turn += 1
                self.tool_messages = [
                    message["content"] for message in messages if message.get("role") == "tool"
                ]
                if self.turn <= 2:
                    message = {"role": "assistant", "content": "", "tool_calls": [{
                        "id": f"read-{self.turn}", "type": "function",
                        "function": {"name": "fs_read_text", "arguments": '{"path":"notes.txt"}'},
                    }]}
                elif self.turn == 3:
                    ref = self.tool_messages[-1].split("ref:", 1)[1].split("]", 1)[0]
                    message = {"role": "assistant", "content": "", "tool_calls": [{
                        "id": "ref-read", "type": "function",
                        "function": {"name": "agent_read_result_ref", "arguments": json.dumps({"ref": ref})},
                    }]}
                else:
                    answer = "找回成功" if "项目代号是海棠" in self.tool_messages[-1] else "找回失败"
                    message = {"role": "assistant", "content": answer}
                yield "done", message

        with tempfile.TemporaryDirectory(prefix="xiaoa-result-ref-") as tmp:
            llm = RefLLM()
            events = [event async for event in Agent(
                llm, registry, AgentConfig(max_steps=10), result_dir=Path(tmp),
            ).run("连续读取两次，再按引用检查全文")]
            stored = list(Path(tmp).glob("*.txt"))

        self.assertEqual(["notes.txt", "notes.txt"], calls)
        self.assertEqual(1, len(stored))
        self.assertIn("[结果已存至 ref:", llm.tool_messages[0])
        self.assertIn("[与前次结果相同，见 ref:", llm.tool_messages[1])
        self.assertEqual(full_result, llm.tool_messages[2])
        self.assertTrue(any(
            event.type == "message" and event.data.get("content") == "找回成功"
            for event in events
        ))

    def test_expired_result_refs_are_cleaned(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-result-cleanup-") as tmp:
            root = Path(tmp)
            old = root / "0123456789ab.txt"
            fresh = root / "abcdef012345.txt"
            old.write_text("old", encoding="utf-8")
            fresh.write_text("fresh", encoding="utf-8")
            now = time.time()
            os.utime(old, (now - TOOL_RESULT_REF_TTL_SECONDS - 1,) * 2)
            self.assertEqual(1, cleanup_result_refs(root, now=now))
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())

    def test_token_estimate_and_compaction_preserve_instructions_and_recent_rounds(self):
        self.assertEqual(10, estimate_text_tokens("abcdefghijklmnopqrst" * 2))
        self.assertEqual(4, estimate_text_tokens("上下文窗口测试一下"))
        messages = [
            {"role": "system", "content": "系统规则不能丢"},
            {"role": "user", "content": "早期原话：暗号是什么？"},
            {"role": "assistant", "tool_calls": [{"id": "old"}]},
            {"role": "tool", "tool_call_id": "old", "content": "暗号是珊瑚红。" + "x" * 5000},
        ]
        for index in range(3):
            messages.extend([
                {"role": "user", "content": f"最近原话 {index}"},
                {"role": "assistant", "content": f"最近回答 {index}"},
            ])
        before = estimate_messages_tokens(messages)
        compacted, count = compact_context_messages(messages)
        self.assertEqual(1, count)
        self.assertLess(estimate_messages_tokens(compacted), before)
        self.assertEqual("系统规则不能丢", compacted[0]["content"])
        self.assertEqual(
            [message["content"] for message in messages if message["role"] == "user"],
            [message["content"] for message in compacted if message["role"] == "user"],
        )
        self.assertIn("暗号是珊瑚红", compacted[3]["content"])
        self.assertEqual(messages[-6:], compacted[-6:])

    async def test_agent_can_use_early_fact_after_watermark_compaction(self):
        class ContextAwareLLM:
            async def chat_stream_tools(self, messages, **_kwargs):
                payload = json.dumps(messages, ensure_ascii=False)
                answer = "珊瑚红" if "暗号是珊瑚红" in payload else "不知道"
                yield "done", {"role": "assistant", "content": answer}

        resume = [
            {"role": "system", "content": "系统规则"},
            {"role": "user", "content": "记住暗号"},
            {"role": "assistant", "tool_calls": [{"id": "old"}]},
            {"role": "tool", "tool_call_id": "old", "content": "暗号是珊瑚红。" + "x" * 20000},
        ]
        for index in range(3):
            resume.extend([
                {"role": "user", "content": f"最近问题 {index}"},
                {"role": "assistant", "content": f"最近回答 {index}"},
            ])
        events = [event async for event in Agent(
            ContextAwareLLM(), ToolRegistry(), AgentConfig(context_window=4096),
        ).run("请说出暗号", resume_messages=resume)]
        self.assertTrue(any(event.type == "context_compacted" for event in events))
        self.assertTrue(any(
            event.type == "message" and event.data.get("content") == "珊瑚红"
            for event in events
        ))

    def test_hub_runtime_context_distinguishes_server_and_worker_tools(self):
        self.assertIn("直接操作这台云服务器", server._HUB_RUNTIME_CONTEXT)
        self.assertIn("code_run", server._HUB_RUNTIME_CONTEXT)
        self.assertIn("只有用户 Windows 电脑", server._HUB_RUNTIME_CONTEXT)

    async def test_runtime_context_identifies_cloud_hub(self):
        class ContextLLM:
            def __init__(self):
                self.system_prompt = ""

            async def chat_stream_tools(self, messages, **_kwargs):
                self.system_prompt = messages[0]["content"]
                yield "done", {"role": "assistant", "content": "知道了"}

        llm = ContextLLM()
        events = [event async for event in Agent(
            llm, ToolRegistry(), AgentConfig(),
            runtime_context="你现在运行在云服务器上的 hub。",
        ).run("你在哪里运行？")]
        self.assertIn("云服务器上的 hub", llm.system_prompt)
        self.assertEqual("done", events[-1].type)

    async def test_launch_retries_when_previous_instance_disappeared(self):
        registry = ToolRegistry()
        launches = []

        async def launch_app(name: str):
            launches.append(name)
            return f"已启动:{name}"

        async def list_windows(title: str = "", limit: int = 5):
            return {"ok": True, "windows": []}

        registry.register(Tool(
            name="launch_app", description="打开软件",
            parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            handler=launch_app, side_effect=True,
        ))
        registry.register(Tool(
            name="list_windows", description="列出窗口",
            parameters={"type": "object", "properties": {}}, handler=list_windows,
        ))

        class RelaunchLLM:
            def __init__(self):
                self.turn = 0

            async def chat_stream_tools(self, messages, tools=None, **kwargs):
                self.turn += 1
                if self.turn <= 2:
                    yield "done", {"role": "assistant", "content": None, "tool_calls": [{
                        "id": f"launch-{self.turn}", "type": "function",
                        "function": {"name": "launch_app", "arguments": '{"name":"记事本"}'},
                    }]}
                else:
                    yield "done", {"role": "assistant", "content": "已恢复 CAD"}

        events = [event async for event in Agent(
            RelaunchLLM(), registry, AgentConfig(max_steps=5),
        ).run("打开 CAD")]
        self.assertEqual(["记事本", "记事本"], launches)
        self.assertEqual("done", events[-1].type)

    async def test_structured_screenshot_reaches_agent_event(self):
        registry = ToolRegistry()
        async def screen_capture():
            return {"text": "已截屏", "_observation_image": "aGVsbG8="}
        registry.register(Tool(
            name="screen_capture", description="截屏",
            parameters={"type": "object", "properties": {}},
            handler=screen_capture,
        ))

        class ScreenshotLLM:
            def __init__(self):
                self.turn = 0

            async def chat_stream_tools(self, *_args, **_kwargs):
                self.turn += 1
                if self.turn == 1:
                    yield "done", {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "shot", "type": "function",
                        "function": {"name": "screen_capture", "arguments": "{}"},
                    }]}
                else:
                    yield "done", {"role": "assistant", "content": "截图已生成"}

        events = [event async for event in Agent(
            ScreenshotLLM(), registry, AgentConfig(max_steps=5),
        ).run("截屏")]
        self.assertEqual("aGVsbG8=", next(
            event.data["image"] for event in events if event.type == "screenshot"
        ))

    async def test_screenshot_path_can_be_passed_to_image_sender(self):
        registry = ToolRegistry()
        screenshot_path = r"C:\\Temp\\xiaoa-screen-test.jpg"
        sent_paths = []

        async def screen_capture():
            return {
                "text": "已截屏",
                "_observation_image": "aGVsbG8=",
                "path": screenshot_path,
            }

        async def send_image_to_chat(path):
            sent_paths.append(path)
            return "已发送"

        registry.register(Tool(
            name="screen_capture", description="截屏",
            parameters={"type": "object", "properties": {}},
            handler=screen_capture,
        ))
        registry.register(Tool(
            name="send_image_to_chat", description="发送本机图片",
            parameters={"type": "object", "properties": {
                "path": {"type": "string"},
            }, "required": ["path"]},
            handler=send_image_to_chat,
        ))

        class ScreenshotSenderLLM:
            def __init__(self):
                self.turn = 0
                self.messages = []

            async def chat_stream_tools(self, messages, **_kwargs):
                self.turn += 1
                self.messages.append(messages)
                if self.turn == 1:
                    yield "done", {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "shot", "type": "function",
                        "function": {"name": "screen_capture", "arguments": "{}"},
                    }]}
                elif self.turn == 2:
                    yield "done", {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "send", "type": "function",
                        "function": {"name": "send_image_to_chat", "arguments": json.dumps({
                            "path": screenshot_path,
                        })},
                    }]}
                else:
                    yield "done", {"role": "assistant", "content": "图片已发送"}

        llm = ScreenshotSenderLLM()
        events = [event async for event in Agent(
            llm, registry, AgentConfig(max_steps=5),
        ).run("截屏并发给我")]
        self.assertEqual([screenshot_path], sent_paths)
        self.assertTrue(any(
            isinstance(part, dict) and part.get("type") == "image_url"
            for batch in llm.messages
            for message in batch
            for part in (message.get("content") if isinstance(message.get("content"), list) else [])
        ))
        self.assertEqual("done", events[-1].type)

    async def test_empty_model_reply_is_reported_as_an_error(self):
        class EmptyLLM:
            async def chat_stream_tools(self, *_args, **_kwargs):
                yield "done", {"role": "assistant", "content": ""}

        events = [event async for event in Agent(
            EmptyLLM(), ToolRegistry(), AgentConfig(max_steps=5),
        ).run("测试空回复")]

        self.assertEqual("error", events[-1].type)
        self.assertIn("空回复", events[-1].data["msg"])

    async def test_duplicate_launch_is_suppressed_and_eventual_stall_stops(self):
        registry = ToolRegistry()
        launches = []

        async def launch_app(name: str):
            launches.append(name)
            return f"已启动:{name}"

        registry.register(Tool(
            name="launch_app", description="打开软件",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            handler=launch_app, side_effect=True,
        ))

        class RepeatingLLM:
            def __init__(self):
                self.turn = 0

            async def chat_stream_tools(self, messages, tools=None, **kwargs):
                self.turn += 1
                yield "done", {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": f"launch-{self.turn}", "type": "function",
                        "function": {"name": "launch_app", "arguments": '{"name":"CAD"}'},
                    }],
                }

        llm = RepeatingLLM()
        events = [event async for event in Agent(
            llm, registry, AgentConfig(max_steps=40),
        ).run("打开CAD")]

        self.assertEqual(["CAD"], launches)
        self.assertEqual(8, llm.turn)  # One launch, six suppressed repeats, then summary.
        self.assertTrue(any(
            event.type == "tool_result" and event.data.get("duplicate_suppressed")
            for event in events
        ))
        self.assertEqual("done", events[-1].type)

    async def test_new_tool_is_visible_in_the_same_agent_run(self):
        registry = ToolRegistry()

        @registry.tool(
            name="create_dynamic",
            description="创建动态工具",
            parameters={"type": "object", "properties": {}},
        )
        async def create_dynamic():
            if registry.get("dynamic_hello") is None:
                async def dynamic_hello(name: str):
                    return f"hello {name}"

                registry.register(Tool(
                    name="dynamic_hello",
                    description="动态问好",
                    parameters={
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                    handler=dynamic_hello,
                ))
            return "created"

        class DynamicLLM:
            def __init__(self):
                self.turn = 0

            async def chat_stream_tools(self, messages, tools=None, **kwargs):
                names = {tool["function"]["name"] for tool in tools or []}
                self.turn += 1
                if self.turn == 1:
                    message = {
                        "role": "assistant", "content": None,
                        "tool_calls": [{
                            "id": "create", "type": "function",
                            "function": {"name": "create_dynamic", "arguments": "{}"},
                        }],
                    }
                elif self.turn == 2:
                    if "dynamic_hello" not in names:
                        raise AssertionError("新工具未在下一步暴露给模型")
                    message = {
                        "role": "assistant", "content": None,
                        "tool_calls": [{
                            "id": "use", "type": "function",
                            "function": {
                                "name": "dynamic_hello", "arguments": '{"name":"Codex"}',
                            },
                        }],
                    }
                else:
                    message = {"role": "assistant", "content": "动态工具已调用。"}
                    yield "delta", message["content"]
                yield "done", message

        events = []
        agent = Agent(DynamicLLM(), registry, AgentConfig(max_steps=5))
        async for event in agent.run("创建并立即调用工具"):
            events.append(event)
        results = [event.data.get("result") for event in events if event.type == "tool_result"]
        self.assertIn("hello Codex", results)

    async def test_interjection_waits_for_current_tool_and_skips_remaining_batch(self):
        registry = ToolRegistry()
        interjections: list[str] = []
        calls: list[str] = []

        @registry.tool(name="first_tool", description="第一步", parameters={"type": "object"})
        async def first_tool():
            calls.append("first")
            interjections.append("等等，先做 X")
            return "第一步已完成"

        @registry.tool(name="second_tool", description="第二步", parameters={"type": "object"})
        async def second_tool():
            calls.append("second")
            return "不该执行"

        class InterjectionLLM:
            def __init__(self):
                self.turn = 0
                self.second_messages = None

            async def chat_stream_tools(self, messages, **_kwargs):
                self.turn += 1
                if self.turn == 1:
                    message = {
                        "role": "assistant", "content": None,
                        "tool_calls": [
                            {"id": "first", "type": "function", "function": {
                                "name": "first_tool", "arguments": "{}",
                            }},
                            {"id": "second", "type": "function", "function": {
                                "name": "second_tool", "arguments": "{}",
                            }},
                        ],
                    }
                else:
                    self.second_messages = list(messages)
                    message = {"role": "assistant", "content": "已按新要求转向。"}
                yield "done", message

        llm = InterjectionLLM()

        def take_interjections():
            items = list(interjections)
            interjections.clear()
            return items

        events = [event async for event in Agent(
            llm, registry, AgentConfig(max_steps=5), interjection_fn=take_interjections,
        ).run("先做两步")]

        self.assertEqual(["first"], calls)
        self.assertEqual("已按新要求转向。", next(
            event.data["content"] for event in events if event.type == "message"
        ))
        messages = llm.second_messages
        self.assertIsNotNone(messages)
        assistant_index = next(i for i, item in enumerate(messages) if item.get("tool_calls"))
        injected_index = next(
            i for i, item in enumerate(messages)
            if item.get("role") == "user" and "[用户中途插话]" in str(item.get("content"))
        )
        tool_messages = messages[assistant_index + 1:injected_index]
        self.assertEqual(["first", "second"], [item.get("tool_call_id") for item in tool_messages])
        self.assertIn("本次尚未执行", tool_messages[1]["content"])


class PolicyTests(unittest.TestCase):
    def test_dangerous_commands_still_require_confirmation(self):
        self.assertTrue(_looks_dangerous("run_command", {"command": "shutdown /s"}))
        self.assertTrue(_looks_dangerous("run_command", {"command": "Remove-Item C:/work/a"}))
        self.assertFalse(_looks_dangerous("run_command", {"command": "python build.py"}))
        self.assertTrue(_looks_financial("pay_invoice", {"amount": 100}))
        self.assertTrue(_looks_financial("click_element", {"target_desc": "确认支付按钮"}))
        self.assertFalse(_looks_financial("fs_read_text", {"path": "D:/普通记录.txt"}))

    def test_remote_confirmation_is_limited_to_requested_categories(self):
        direct_commands = (
            "find /var/lib/docker -name '*.db' 2>/dev/null",
            "docker ps -a --format 'table {{.Names}}\\t{{.Image}}' 2>/dev/null",
            "docker exec grok2api sh -c 'ls -la /app/data; command -v sqlite3 || true; "
            "cat /etc/os-release 2>/dev/null | head -5; ls /usr/bin | head -50'",
            "cat /etc/os-release 2>/dev/null", "ps aux 2>/dev/null | head",
            "free -h 2>/dev/null", "git log --oneline 2>/dev/null",
            "cmd &> /dev/null", "cmd 2>>err.log",
            "ps aux | grep dd", "find /tmp -name dd", "ps aux | grep rm",
            "ps aux | grep remove", "npm run build", "python -m black --format .",
            "ruff format --check .", "rg 'format C:' docs", "rg 'sed -i' .",
            "git grep 'cp -f'", "grep payment app.log", "apt-get install -y chromium",
            "python3 - <<'PY'\ncount = 1\nprint(count > 0)\nPY",
            "python3 - <<'PY'\nimport sqlite3\n"
            "for table in ['provider_accounts', 'account_credentials']:\n"
            "    print(table, 'count', 1)\nPY",
            "for k, v in row.items():\n"
            "    if k.lower() in ('password', 'token', 'cookie', 'credential'):\n"
            "        row[k] = '***'\nprint('sample:', row)",
        )
        for command in direct_commands:
            with self.subTest(direct=command):
                self.assertFalse(_looks_remote_confirmation("code_run", {"command": command}))
        for command in (
            "rm -f old.log", "git remote remove origin", "apt-get uninstall demo",
            "echo hi > /tmp/a.txt", "printf x >> out.log", "cat a.txt > b.txt",
            "cmd 2>&1 | tee log", "grep remove docs; rm -f old.log",
            "passwd root", "select password from users", "支付 100 元",
        ):
            with self.subTest(command=command):
                self.assertTrue(_looks_remote_confirmation("code_run", {"command": command}))


class InputOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_mode_runs_read_only_code_without_confirmation(self):
        registry = ToolRegistry()
        called = []

        async def code_run(command: str):
            called.append(command)
            return "done"

        registry.register(Tool(
            name="code_run", description="执行命令",
            parameters={"type": "object", "properties": {"command": {"type": "string"}}},
            handler=code_run, side_effect=True, danger=True,
        ))

        class ReadOnlyLLM:
            def __init__(self):
                self.turn = 0

            async def chat_stream_tools(self, messages, tools=None, **kwargs):
                self.turn += 1
                if self.turn == 1:
                    yield "done", {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "read", "type": "function",
                        "function": {
                            "name": "code_run",
                            "arguments": '{"command":"ps aux"}',
                        },
                    }]}
                else:
                    yield "done", {"role": "assistant", "content": "完成"}

        confirmations = []

        async def confirm(name, args):
            confirmations.append((name, args))
            return False

        events = [event async for event in Agent(
            ReadOnlyLLM(), registry, AgentConfig(confirm_before_action=True), confirm_fn=confirm,
        ).run("查看进程", approval_mode="remote")]
        self.assertEqual(["ps aux"], called)
        self.assertEqual([], confirmations)
        self.assertFalse(any(event.type == "confirm_needed" for event in events))

    async def test_skill_prompt_is_injected_and_tool_install_always_confirms(self):
        registry = ToolRegistry()
        installed = []

        async def extension_install(url: str, kind: str):
            installed.append((url, kind))
            return "installed"

        registry.register(Tool(
            name="extension_install", description="安装扩展",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"}, "kind": {"type": "string"},
                },
                "required": ["url", "kind"],
            },
            handler=extension_install, side_effect=True,
        ))

        class InstallLLM:
            def __init__(self):
                self.turn = 0
                self.system_prompt = ""

            async def chat_stream_tools(self, messages, tools=None, **kwargs):
                self.system_prompt = messages[0]["content"]
                self.turn += 1
                if self.turn == 1:
                    yield "done", {
                        "role": "assistant", "content": None,
                        "tool_calls": [{
                            "id": "install", "type": "function",
                            "function": {
                                "name": "extension_install",
                                "arguments": '{"url":"https://github.com/o/r","kind":"tool"}',
                            },
                        }],
                    }
                else:
                    yield "done", {"role": "assistant", "content": "已取消"}

        confirmations = []

        async def confirm_fn(name, args):
            confirmations.append((name, args))
            return False

        llm = InstallLLM()
        agent = Agent(
            llm, registry, AgentConfig(confirm_before_action=False),
            confirm_fn=confirm_fn,
            skill_prompt_fn=lambda _: "已启用技能：\n测试技能规则",
        )
        events = [event async for event in agent.run("安装这个工具", approval_mode="full")]

        self.assertIn("测试技能规则", llm.system_prompt)
        self.assertEqual([], installed)
        self.assertEqual("external_code", next(
            event.data["reason"] for event in events if event.type == "confirm_needed"
        ))
        self.assertEqual("extension_install", confirmations[0][0])

    async def test_financial_action_requires_confirmation_in_full_access(self):
        registry = ToolRegistry()
        called = []

        async def perform_action(step: int):
            called.append(step)
            return "done"

        registry.register(Tool(
            name="perform_action", description="执行普通界面动作",
            parameters={
                "type": "object", "properties": {"step": {"type": "integer"}},
                "required": ["step"],
            },
            handler=perform_action, side_effect=True,
        ))

        class PaymentLLM:
            def __init__(self):
                self.turn = 0

            async def chat_stream_tools(self, messages, tools=None, **kwargs):
                self.turn += 1
                if self.turn == 1:
                    yield "done", {
                        "role": "assistant", "content": None,
                        "tool_calls": [{
                            "id": "pay", "type": "function",
                            "function": {"name": "perform_action", "arguments": '{"step":1}'},
                        }],
                    }
                else:
                    yield "done", {"role": "assistant", "content": "已取消支付"}

        confirmations = []

        async def confirm_fn(name, args):
            confirmations.append((name, args))
            return False

        agent = Agent(
            PaymentLLM(), registry, AgentConfig(confirm_before_action=False),
            confirm_fn=confirm_fn,
        )
        events = [event async for event in agent.run("支付100元", approval_mode="full")]

        self.assertEqual([], called)
        self.assertEqual([("perform_action", {"step": 1})], confirmations)
        financial_events = [e for e in events if e.type == "confirm_needed"]
        self.assertEqual("financial", financial_events[0].data["reason"])

    async def test_safe_auto_skips_ordinary_risk_without_keyword_privacy_confirmation(self):
        registry = ToolRegistry()
        called = []

        async def perform_action(command: str):
            called.append(command)
            return "done"

        registry.register(Tool(
            name="perform_action", description="执行普通动作",
            parameters={
                "type": "object", "properties": {"command": {"type": "string"}},
                "required": ["command"],
            }, handler=perform_action, side_effect=True,
        ))

        class SafeAutoLLM:
            def __init__(self):
                self.turn = 0

            async def chat_stream_tools(self, messages, tools=None, **kwargs):
                self.turn += 1
                calls = [
                    '{"command":"删除临时文件"}',
                    '{"command":"读取密码"}',
                ]
                if self.turn <= len(calls):
                    yield "done", {"role": "assistant", "content": None, "tool_calls": [{
                        "id": f"call-{self.turn}", "type": "function",
                        "function": {"name": "perform_action", "arguments": calls[self.turn - 1]},
                    }]}
                else:
                    yield "done", {"role": "assistant", "content": "已完成"}

        confirmations = []

        async def confirm_fn(name, args):
            confirmations.append((name, args))
            return False

        events = [event async for event in Agent(
            SafeAutoLLM(), registry, AgentConfig(confirm_before_action=True),
            confirm_fn=confirm_fn,
        ).run("执行普通操作", approval_mode="safe")]

        self.assertEqual(["删除临时文件", "读取密码"], called)
        self.assertEqual([], confirmations)
        self.assertFalse(any(event.type == "confirm_needed" for event in events))

    async def test_checkpoint_restore_requires_confirmation_after_preview(self):
        registry = ToolRegistry()
        restored = []

        async def restore(confirm_token: str):
            restored.append(confirm_token)
            return "restored"

        registry.register(Tool(
            name="code_checkpoint_restore", description="恢复代码检查点",
            parameters={
                "type": "object",
                "properties": {"confirm_token": {"type": "string"}},
                "required": ["confirm_token"],
            },
            handler=restore, side_effect=True,
        ))

        class RestoreLLM:
            def __init__(self):
                self.turn = 0

            async def chat_stream_tools(self, messages, tools=None, **kwargs):
                self.turn += 1
                if self.turn == 1:
                    yield "done", {
                        "role": "assistant", "content": None,
                        "tool_calls": [{
                            "id": "restore", "type": "function",
                            "function": {
                                "name": "code_checkpoint_restore",
                                "arguments": '{"confirm_token":"preview-token"}',
                            },
                        }],
                    }
                else:
                    yield "done", {"role": "assistant", "content": "已取消恢复"}

        confirmations = []

        async def confirm_fn(name, args):
            confirmations.append((name, args))
            return False

        agent = Agent(
            RestoreLLM(), registry, AgentConfig(confirm_before_action=True),
            confirm_fn=confirm_fn,
        )
        events = [event async for event in agent.run("恢复刚才的检查点", approval_mode="risk")]

        self.assertEqual([], restored)
        self.assertEqual("code_checkpoint_restore", confirmations[0][0])
        self.assertEqual("risk", next(
            event.data["reason"] for event in events if event.type == "confirm_needed"
        ))

    async def test_selected_model_attachments_and_full_access_reach_agent(self):
        registry = ToolRegistry()
        called = []

        async def dangerous(command: str):
            called.append(command)
            return "ok"

        registry.register(Tool(
            name="dangerous",
            description="测试危险操作",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            handler=dangerous,
        ))

        class CaptureLLM:
            def __init__(self):
                self.turn = 0
                self.models = []
                self.first_user_content = None

            async def chat_stream_tools(self, messages, tools=None, model=None, **kwargs):
                self.models.append(model)
                if self.first_user_content is None:
                    self.first_user_content = messages[-1]["content"]
                self.turn += 1
                if self.turn == 1:
                    yield "done", {
                        "role": "assistant", "content": None,
                        "tool_calls": [{
                            "id": "danger", "type": "function",
                            "function": {
                                "name": "dangerous",
                                "arguments": '{"command":"shutdown /s"}',
                            },
                        }],
                    }
                else:
                    yield "done", {"role": "assistant", "content": "完成"}

        confirm_calls = []

        async def confirm_fn(name, args):
            confirm_calls.append((name, args))
            return False

        default_llm = CaptureLLM()
        llm = CaptureLLM()
        agent = Agent(
            default_llm, registry, AgentConfig(confirm_before_action=True), confirm_fn=confirm_fn,
        )
        events = [event async for event in agent.run(
            "看这张图",
            model="model-b",
            llm=llm,
            approval_mode="full",
            attachments=[{
                "name": "sample.png",
                "path": "C:/uploads/sample.png",
                "data_url": "data:image/png;base64,AA==",
            }],
        )]

        self.assertEqual(["shutdown /s"], called)
        self.assertFalse(confirm_calls)
        self.assertFalse(default_llm.models)
        self.assertEqual(["model-b", "model-b"], llm.models)
        self.assertIsInstance(llm.first_user_content, list)
        self.assertTrue(any(part.get("type") == "image_url" for part in llm.first_user_content))
        self.assertEqual("done", events[-1].type)

    def test_configured_models_include_default_without_duplicates(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-model-config-") as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                '[llm]\nchat_model="model-a"\nmodels=["model-b","model-a","model-b"]\n'
                '[[llm.providers]]\nid="vendor-b"\nname="厂商 B"\n'
                'base_url="https://vendor-b.example/v1"\napi_key="key-b"\nmodels=["model-c"]\n',
                encoding="utf-8",
            )
            cfg = load_config(path)
            self.assertEqual(["model-a", "model-b"], cfg.llm.models)
            self.assertEqual("vendor-b", cfg.llm.providers[0].id)
            self.assertEqual(["model-c"], cfg.llm.providers[0].models)


class AttachmentInputTests(unittest.TestCase):
    def test_attachment_is_sanitized_saved_and_text_is_extracted(self):
        import base64
        from backend.api import server

        old_dir = server._ATTACHMENT_DIR
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoa-attachment-") as tmp:
                server._ATTACHMENT_DIR = Path(tmp)
                payload = base64.b64encode("附件正文".encode("utf-8")).decode("ascii")
                items = server._prepare_attachments([{
                    "name": "../notes.txt",
                    "data": f"data:text/plain;base64,{payload}",
                }])
                saved = Path(items[0]["path"])
                self.assertEqual(Path(tmp).resolve(), saved.parent)
                self.assertTrue(saved.exists())
                self.assertEqual("附件正文", items[0]["text"])
                history_items = server._attachment_history_items(items)
                self.assertEqual("notes.txt", history_items[0]["name"])
                self.assertEqual(saved.name, history_items[0]["saved_name"])
                self.assertEqual(saved, server._saved_attachment_path(saved.name))
                self.assertIsNone(server._saved_attachment_path("../" + saved.name))
        finally:
            server._ATTACHMENT_DIR = old_dir


class ProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_provider_builds_client_with_its_own_endpoint_and_key(self):
        from backend.api import server
        from backend.core.config import LLMProviderConfig

        old_providers = server.CFG.llm.providers
        try:
            server.CFG.llm.providers = [LLMProviderConfig(
                id="vendor-b",
                name="厂商 B",
                base_url="https://vendor-b.example/v1",
                api_key="key-b",
                models=["model-b"],
            )]
            client, owned, models, provider_id = server._llm_for_provider("vendor-b")
            self.assertTrue(owned)
            self.assertEqual("vendor-b", provider_id)
            self.assertEqual(["model-b"], models)
            self.assertEqual("https://vendor-b.example/v1", client.cfg.base_url)
            self.assertEqual("key-b", client.cfg.api_key)
            await client.aclose()
        finally:
            server.CFG.llm.providers = old_providers

    async def test_model_test_reuses_masked_saved_key(self):
        from backend.api import server

        old_client = server.LLMClient
        old_key = server.CFG.llm.api_key

        class FakeClient:
            last_cfg = None

            def __init__(self, cfg):
                FakeClient.last_cfg = cfg

            async def chat(self, *args, **kwargs):
                return {"content": "OK"}

            async def aclose(self):
                pass

        try:
            server.LLMClient = FakeClient
            server.CFG.llm.api_key = "saved-secret-key"
            result = await server.api_test_model(server.ModelTestRequest(
                provider_id="default",
                base_url="https://gateway.example/v1",
                api_key=server._mask(server.CFG.llm.api_key),
                model="model-a",
            ))
            self.assertTrue(result["ok"])
            self.assertEqual("saved-secret-key", FakeClient.last_cfg.api_key)
        finally:
            server.LLMClient = old_client
            server.CFG.llm.api_key = old_key


class LLMRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_reasoning_effort_is_sent_to_compatible_endpoint(self):
        from backend.core.config import LLMConfig
        from backend.core.llm import LLMClient

        captured = {}

        async def handler(request):
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

        client = LLMClient(LLMConfig(
            base_url="https://gateway.example/v1", api_key="key", chat_model="model-a",
        ))
        client._client = httpx.AsyncClient(
            base_url="https://gateway.example/v1", transport=httpx.MockTransport(handler),
        )
        try:
            await client.chat([{"role": "user", "content": "test"}], reasoning_effort="xhigh")
            self.assertEqual("xhigh", captured["reasoning_effort"])
        finally:
            await client.aclose()


class ReplyDraftTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_regenerate_uses_guidance_without_removing_pending_draft(self):
        from backend.api import server

        old_llm = server.LLM
        captured = {}

        class FakeLLM:
            async def chat(self, messages, **kwargs):
                captured["prompt"] = messages[0]["content"]
                return {"content": "按提示重拟的回复"}

        server.LLM = FakeLLM()
        server.PENDING_REPLIES["draft-test"] = {
            "source": "feishu", "reply_target": "测试群",
            "conversation": "测试群", "conversation_id": "57718830000@chatroom",
            "is_group": True, "sender": "朱镇平", "incoming": "请问进度？",
        }
        try:
            result = await server.api_reply_regenerate(server.SendReplyRequest(
                id="draft-test", text="语气正式，说明明天下午回复",
            ))
            self.assertTrue(result["ok"])
            self.assertEqual("按提示重拟的回复", result["draft"])
            self.assertIn("语气正式", captured["prompt"])
            self.assertIn("57718830000@chatroom", captured["prompt"])
            self.assertIn("draft-test", server.PENDING_REPLIES)
        finally:
            server.PENDING_REPLIES.pop("draft-test", None)
            server.LLM = old_llm


class MemoryBackupTests(unittest.TestCase):
    def test_versioned_backups_are_complete_and_retained(self):
        from backend.api import server

        old_dir = server.CFG.memory.backup_dir
        old_keep = server.CFG.memory.backup_keep_count
        old_interval = server.CFG.memory.backup_interval_minutes
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoa-backup-") as tmp:
                server.CFG.memory.backup_dir = tmp
                server.CFG.memory.backup_keep_count = 2
                server.CFG.memory.backup_interval_minutes = 60
                unrelated = Path(tmp) / "do-not-delete.txt"
                unrelated.write_text("keep", encoding="utf-8")
                for index in range(3):
                    server._write_memory_backup({
                        "memory.sqlite3": f"snapshot-{index}".encode(),
                        "chat_history.json": b"[]",
                    })
                snapshots = sorted(path for path in Path(tmp).iterdir() if path.is_dir())
                self.assertEqual(2, len(snapshots))
                self.assertTrue(all((path / "manifest.json").exists() for path in snapshots))
                self.assertTrue(all((path / "memory.sqlite3").exists() for path in snapshots))
                self.assertEqual("keep", unrelated.read_text(encoding="utf-8"))
                self.assertGreater(server._seconds_until_next_memory_backup(), 3500)
        finally:
            server.CFG.memory.backup_dir = old_dir
            server.CFG.memory.backup_keep_count = old_keep
            server.CFG.memory.backup_interval_minutes = old_interval


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_proxy_replaces_hub_channel_media_tools(self):
        from backend.api import server

        old_registry = server.REGISTRY
        old_bridge = server.HUB_BRIDGE
        old_proxy_tools = server._WORKER_PROXY_TOOLS
        try:
            server.REGISTRY = ToolRegistry()

            async def hub_file_fallback(**_kwargs):
                return "hub-local-file"

            server.REGISTRY.register(Tool(
                name="send_file_to_chat", description="云端回退",
                parameters={"type": "object", "properties": {
                    "path": {"type": "string"},
                }}, handler=hub_file_fallback,
            ))
            bridge = HubBridge("good")

            async def dispatch(tool, args):
                return {"ok": True, "result": f"worker:{tool}:{args['path']}"}

            bridge.dispatch = dispatch
            server.HUB_BRIDGE = bridge
            server._WORKER_PROXY_TOOLS = set()
            server._sync_worker_tools([{
                "schema": {"type": "function", "function": {
                    "name": "send_file_to_chat", "description": "发送本机文件",
                    "parameters": {"type": "object", "properties": {
                        "path": {"type": "string"},
                    }, "required": ["path"]},
                }},
                "side_effect": True,
            }], persist=False)

            result = await server.REGISTRY.call(
                "send_file_to_chat", {"path": r"C:\Users\test\Desktop\2609024.pdf"},
            )
            self.assertEqual(
                r"worker:send_file_to_chat:C:\Users\test\Desktop\2609024.pdf",
                result,
            )
            self.assertIn("send_file_to_chat", server._WORKER_PROXY_TOOLS)
        finally:
            server.REGISTRY = old_registry
            server.HUB_BRIDGE = old_bridge
            server._WORKER_PROXY_TOOLS = old_proxy_tools

    async def test_worker_media_proxy_returns_payload_to_active_weixin_chat(self):
        from backend.api import server

        old_registry = server.REGISTRY
        old_bridge = server.HUB_BRIDGE
        old_proxy_tools = server._WORKER_PROXY_TOOLS
        channel_token = server.CURRENT_CHANNEL.set(("weixin", "user-1"))
        context_token = server._CHANNEL_CONTEXT_TOKEN.set("ctx-1")
        captured = {}
        try:
            server.REGISTRY = ToolRegistry()

            async def dispatch(tool, args):
                captured["tool"] = tool
                captured["args"] = args
                return {"ok": True, "result": {
                    "_xiaoa_hub_media": True, "kind": "image", "name": "circle.png",
                    "mime": "image/png", "data": "iVBORw==",
                }}

            bridge = HubBridge("good")
            bridge.dispatch = dispatch
            server.HUB_BRIDGE = bridge
            server._WORKER_PROXY_TOOLS = set()
            server._sync_worker_tools([{
                "schema": {"type": "function", "function": {
                    "name": "send_image_to_chat", "description": "发送本机图片",
                    "parameters": {"type": "object", "properties": {
                        "path": {"type": "string"},
                    }, "required": ["path"]},
                }},
                "side_effect": True,
            }], persist=False)

            with mock.patch.object(server, "_deliver_worker_media", mock.AsyncMock(
                    return_value="已把图片发到微信:circle.png")) as deliver:
                result = await server.REGISTRY.call(
                    "send_image_to_chat", {"path": r"C:\\tmp\\circle.png"},
                )

            self.assertEqual("已把图片发到微信:circle.png", result)
            self.assertEqual({
                "path": r"C:\\tmp\\circle.png", "_return_to_hub": True,
            }, captured["args"])
            deliver.assert_awaited_once()
            self.assertEqual(("weixin", "user-1"), deliver.await_args.args[1])
            self.assertIsNone(deliver.await_args.args[2])
        finally:
            server.CURRENT_CHANNEL.reset(channel_token)
            server._CHANNEL_CONTEXT_TOKEN.reset(context_token)
            server.REGISTRY = old_registry
            server.HUB_BRIDGE = old_bridge
            server._WORKER_PROXY_TOOLS = old_proxy_tools

    async def test_worker_proxy_does_not_replace_hub_local_tool(self):
        from backend.api import server

        old_registry = server.REGISTRY
        old_bridge = server.HUB_BRIDGE
        old_proxy_tools = server._WORKER_PROXY_TOOLS
        try:
            server.REGISTRY = ToolRegistry()
            async def local_code_run(**_kwargs):
                return "server-local"
            server.REGISTRY.register(Tool(
                name="code_run", description="云服务器命令",
                parameters={"type": "object", "properties": {}}, handler=local_code_run,
            ))
            server.HUB_BRIDGE = HubBridge("good")
            server._WORKER_PROXY_TOOLS = set()
            server._sync_worker_tools([{
                "schema": {"type": "function", "function": {
                    "name": "code_run", "description": "电脑命令",
                    "parameters": {"type": "object", "properties": {}},
                }},
            }], persist=False, available=False)
            self.assertEqual("server-local", await server.REGISTRY.call("code_run", {}))
            self.assertNotIn("code_run", server._WORKER_PROXY_TOOLS)
        finally:
            server.REGISTRY = old_registry
            server.HUB_BRIDGE = old_bridge
            server._WORKER_PROXY_TOOLS = old_proxy_tools

    async def test_worker_proxy_preserves_structured_screenshot_result(self):
        from backend.api import server

        old_registry = server.REGISTRY
        old_bridge = server.HUB_BRIDGE
        old_proxy_tools = server._WORKER_PROXY_TOOLS
        try:
            server.REGISTRY = ToolRegistry()
            bridge = HubBridge("good")
            async def dispatch(_tool, _args):
                return {"ok": True, "result": {"text": "已截屏", "_observation_image": "aGVsbG8="}}
            bridge.dispatch = dispatch
            server.HUB_BRIDGE = bridge
            server._WORKER_PROXY_TOOLS = set()
            server._sync_worker_tools([{
                "schema": {"type": "function", "function": {
                    "name": "screen_capture", "description": "截屏",
                    "parameters": {"type": "object", "properties": {}},
                }},
            }], persist=False)
            result = await server.REGISTRY.call("screen_capture", {})
            self.assertEqual("aGVsbG8=", result["_observation_image"])
        finally:
            server.REGISTRY = old_registry
            server.HUB_BRIDGE = old_bridge
            server._WORKER_PROXY_TOOLS = old_proxy_tools

    async def test_cached_worker_tools_are_not_exposed_while_offline(self):
        from backend.api import server

        old_registry = server.REGISTRY
        old_bridge = server.HUB_BRIDGE
        old_proxy_tools = server._WORKER_PROXY_TOOLS
        try:
            server.REGISTRY = ToolRegistry()
            server.HUB_BRIDGE = HubBridge("good")
            server._WORKER_PROXY_TOOLS = set()
            async def cloud_tool():
                return "cloud-online"
            server.REGISTRY.register(Tool(
                name="code_run", description="云端命令", parameters={"type": "object"},
                handler=cloud_tool,
            ))
            tools = [{
                "schema": {"type": "function", "function": {
                    "name": "list_windows", "description": "列出窗口",
                    "parameters": {"type": "object", "properties": {}},
                }},
            }]
            with tempfile.TemporaryDirectory(prefix="xiaoa-worker-cache-") as tmp:
                cache = Path(tmp) / "worker_tools.json"
                cache.write_text(json.dumps(tools), encoding="utf-8")
                with mock.patch.object(server, "_worker_tools_cache_path", return_value=cache):
                    server._load_cached_worker_tools()
            self.assertIsNone(server.REGISTRY.get("list_windows"))
            self.assertEqual({"code_run"}, set(server.REGISTRY.names()))
            self.assertEqual("cloud-online", await server.REGISTRY.call("code_run", {}))
            self.assertEqual(1, server.HUB_BRIDGE.status()["cached_tool_count"])
        finally:
            server.REGISTRY = old_registry
            server.HUB_BRIDGE = old_bridge
            server._WORKER_PROXY_TOOLS = old_proxy_tools

    async def test_hub_registers_worker_reported_tools_without_local_windows_modules(self):
        from backend.api import server

        old_registry = server.REGISTRY
        old_bridge = server.HUB_BRIDGE
        old_proxy_tools = server._WORKER_PROXY_TOOLS
        try:
            server.REGISTRY = ToolRegistry()
            server.HUB_BRIDGE = HubBridge("good")
            server._WORKER_PROXY_TOOLS = set()
            server._sync_worker_tools([{
                "schema": {
                    "type": "function",
                    "function": {
                        "name": "launch_app",
                        "description": "打开 Windows 软件",
                        "parameters": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"],
                        },
                    },
                },
                "side_effect": True,
                "danger": False,
            }], persist=False)
            self.assertIsNotNone(server.REGISTRY.get("launch_app"))
            result = await server.REGISTRY.call("launch_app", {"name": "calc"})
            self.assertTrue(result["offline"])
            self.assertIn("不在线", result["result"])
            server._sync_worker_tools([], persist=False)
            self.assertIsNone(server.REGISTRY.get("launch_app"))
        finally:
            server.REGISTRY = old_registry
            server.HUB_BRIDGE = old_bridge
            server._WORKER_PROXY_TOOLS = old_proxy_tools

    async def test_rejects_wrong_token(self):
        ws = FakeWebSocket([json.dumps({"type": "hello", "token": "bad"})])
        await HubBridge("good").handle_worker(ws)
        self.assertEqual(4003, ws.closed)
        self.assertEqual("deny", ws.sent[0]["type"])

    async def test_model_order_snapshot_requires_authenticated_capable_worker(self):
        order = [{"id": "default", "models": ["second", "first"]}]
        for token, capable in (("good", True), ("good", False), ("bad", True)):
            with self.subTest(token=token, capable=capable):
                ws = FakeWebSocket([json.dumps({"type": "hello", "token": token,
                    "worker_id": "office-pc", "model_order_sync": capable})])
                bridge = HubBridge("good", get_model_order=lambda: order)
                await bridge.handle_worker(ws)
                if token == "good" and capable:
                    self.assertEqual(order, ws.sent[0]["model_order"])
                else:
                    self.assertNotIn("model_order", ws.sent[0])

    async def test_records_worker_identity(self):
        ws = FakeWebSocket([json.dumps({
            "type": "hello", "token": "good", "worker_id": "office-pc",
        })])
        bridge = HubBridge("good")
        await bridge.handle_worker(ws)
        self.assertEqual("welcome", ws.sent[0]["type"])
        self.assertEqual("office-pc", bridge.status()["worker_id"])

    async def test_receives_worker_tool_capabilities(self):
        received = []
        tool = {
            "schema": {
                "type": "function",
                "function": {
                    "name": "hello_user",
                    "description": "问好",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            "side_effect": False,
            "danger": False,
        }
        ws = FakeWebSocket([
            json.dumps({
                "type": "hello", "token": "good", "worker_id": "office-pc", "tools": [tool],
            }),
            json.dumps({"type": "capabilities", "tools": [tool]}),
        ])
        bridge = HubBridge("good", on_tools=received.extend)
        await bridge.handle_worker(ws)
        self.assertEqual(1, len(received))
        self.assertEqual("hello_user", received[0]["schema"]["function"]["name"])
        self.assertEqual(0, bridge.status()["tool_count"])
        self.assertEqual(1, bridge.status()["cached_tool_count"])

    async def test_disconnect_hides_worker_tools_but_preserves_cached_count(self):
        received = []
        offline = []
        tool = {"schema": {"type": "function", "function": {
            "name": "list_windows", "description": "列出窗口",
            "parameters": {"type": "object", "properties": {}},
        }}}
        ws = FakeWebSocket([json.dumps({
            "type": "hello", "token": "good", "worker_id": "office-pc", "tools": [tool],
        })])
        bridge = HubBridge("good", on_tools=received.extend,
                           on_worker_offline=lambda: offline.append(True))
        await bridge.handle_worker(ws)
        self.assertEqual([tool], received)
        self.assertEqual([True], offline)
        self.assertFalse(bridge.worker_online)
        self.assertEqual(0, bridge.status()["tool_count"])
        self.assertEqual(1, bridge.status()["cached_tool_count"])
        self.assertEqual("", bridge._tools_fingerprint)


class ContextCompactionEscalationTests(unittest.TestCase):
    def _rounds(self, round_count: int, tool_chars: int, user_chars: int = 20):
        messages = [{"role": "system", "content": "系统规则"}]
        for index in range(round_count):
            messages.append({"role": "user", "content": f"第{index}轮问题" + "u" * user_chars})
            messages.append({"role": "assistant", "tool_calls": [{"id": f"t{index}"}]})
            messages.append({"role": "tool", "tool_call_id": f"t{index}",
                             "content": "结果" + "x" * tool_chars})
        return messages

    def _user_contents(self, messages):
        return [m["content"] for m in messages if m["role"] == "user"]

    def test_scenario_b_protected_big_results_fit_via_escalation(self):
        # 3 轮、每轮工具结果 40000 字符,全落在默认保护区 → 旧逻辑压 0 个
        messages = self._rounds(3, 40000)
        self.assertEqual(0, compact_context_messages(messages)[1])  # 默认压 0
        limit = 20000
        result, _, fitted = compact_for_limit(messages, limit)
        self.assertTrue(fitted)
        self.assertLess(estimate_messages_tokens(result), limit)
        self.assertEqual(self._user_contents(messages), self._user_contents(result))

    def test_scenario_c_many_rounds_fit_via_escalation(self):
        # 30 轮、user 1500 字、工具 8000 字:默认压缩不够,逐级加压后能降到线下
        messages = self._rounds(30, 8000, user_chars=1500)
        limit = 20000
        # 默认(保护区 3 轮)单次压缩仍超线
        default_result, _ = compact_context_messages(messages)
        self.assertGreaterEqual(estimate_messages_tokens(default_result), limit)
        result, _, fitted = compact_for_limit(messages, limit)
        self.assertTrue(fitted)
        self.assertLess(estimate_messages_tokens(result), limit)
        self.assertEqual(self._user_contents(messages), self._user_contents(result))

    def test_scenario_a_user_alone_exceeds_limit_is_not_fitted(self):
        # 60 轮、user 2000 字:光 user 原话就超线,压缩/丢轮都帮不上
        messages = self._rounds(60, 100, user_chars=2000)
        user_tokens = sum(estimate_text_tokens(m["content"]) for m in messages if m["role"] == "user")
        limit = int(user_tokens * 0.5)  # 一个连 user 原话都装不下的线
        result, _, fitted = compact_for_limit(messages, limit)
        self.assertFalse(fitted)
        # user 消息一条不少、一字不改
        self.assertEqual(self._user_contents(messages), self._user_contents(result))

    def test_user_messages_preserved_at_every_level(self):
        messages = self._rounds(8, 5000)
        original_users = self._user_contents(messages)
        for recent, head, tail in [(3, 420, 180), (1, 420, 180), (0, 420, 180), (0, 120, 60), (0, 40, 20)]:
            result, _ = compact_context_messages(
                messages, recent_rounds=recent, head_chars=head, tail_chars=tail,
            )
            self.assertEqual(original_users, self._user_contents(result))
        self.assertEqual(original_users, self._user_contents(drop_oldest_round(messages)))

    def test_drop_oldest_round_keeps_tool_call_pairing(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "tool_calls": [{"id": "t1"}, {"id": "t2"}]},
            {"role": "tool", "tool_call_id": "t1", "content": "r1"},
            {"role": "tool", "tool_call_id": "t2", "content": "r2"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "tool_calls": [{"id": "t3"}]},
            {"role": "tool", "tool_call_id": "t3", "content": "r3"},
        ]
        dropped = drop_oldest_round(messages)
        tool_ids = {m["tool_call_id"] for m in dropped if m["role"] == "tool"}
        assistant_ids = {
            call.get("id") for m in dropped if m["role"] == "assistant"
            for call in (m.get("tool_calls") or [])
        }
        self.assertEqual(tool_ids, assistant_ids)  # 无 orphan
        self.assertEqual({"t3"}, tool_ids)
        self.assertEqual(["u1", "u2"], self._user_contents(dropped))


class LLMErrorRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def _error_where(self, llm):
        from backend.core.agent import Agent
        from backend.core.config import AgentConfig
        from backend.core.tools import ToolRegistry

        events = [event async for event in Agent(
            llm, ToolRegistry(), AgentConfig(max_steps=3),
        ).run("测试")]
        for event in events:
            if event.type == "error":
                return event.data.get("where"), event.data.get("msg", "")
        return None, ""

    async def test_invalid_model_400_is_config_error_without_retry_or_model_switch(self):
        from backend.core.config import LLMConfig
        from backend.core.llm import LLMClient

        requests = []

        async def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(400, json={
                "message": "Invalid model ID. Please select a different model to continue.",
                "reason": "INVALID_MODEL_ID",
            })

        client = LLMClient(LLMConfig(
            base_url="https://gateway.example/v1", api_key="test", chat_model="invalid-model",
        ))
        client._client = httpx.AsyncClient(
            base_url="https://gateway.example/v1", transport=httpx.MockTransport(handler),
        )
        try:
            where, msg = await self._error_where(client)
            self.assertEqual("llm.config", where)
            self.assertIn("模型 ID 无效", msg)
            self.assertIn("400", msg)
            self.assertEqual(1, len(requests))
            self.assertEqual("invalid-model", requests[0]["model"])
        finally:
            await client.aclose()

    async def test_config_llm_error_maps_to_config_where(self):
        from backend.core.llm import LLMError

        class ConfigErrorLLM:
            async def chat_stream_tools(self, messages, **_kwargs):
                raise LLMError("当前模型不支持看图（视觉能力），请切换到支持视觉的模型后重试。", kind="config")
                yield

        where, msg = await self._error_where(ConfigErrorLLM())
        self.assertEqual("llm.config", where)
        self.assertIn("不支持看图", msg)

    async def test_service_llm_error_maps_to_chat_where(self):
        from backend.core.llm import LLMError

        class ServiceErrorLLM:
            async def chat_stream_tools(self, messages, **_kwargs):
                raise LLMError("网关返回 500", kind="service")
                yield

        where, _ = await self._error_where(ServiceErrorLLM())
        self.assertEqual("llm.chat", where)

    async def test_empty_reply_maps_to_service_where(self):
        class EmptyReplyLLM:
            async def chat_stream_tools(self, messages, **_kwargs):
                yield "done", {"role": "assistant", "content": ""}

        where, msg = await self._error_where(EmptyReplyLLM())
        self.assertEqual("llm.chat", where)
        self.assertIn("空回复", msg)


class ScreenshotGuardTests(unittest.IsolatedAsyncioTestCase):
    """第一步截屏拦截:只该挡"拿截屏猜意图",不该挡本来就得看画面的活。

    Why: 这道闸门原来把"在 CAD 图上加标注"也拦了,用户直接干不了活。
    """

    async def _first_step_screenshot(self, user_input: str, attachments=None) -> tuple[bool, list]:
        """返回 (是否被拦下, 截屏工具实际被调用的次数记录)。"""
        from backend.core.agent import Agent
        from backend.core.config import AgentConfig
        from backend.core.tools import Tool, ToolRegistry

        shots = []

        async def screen_capture():
            shots.append(True)
            return {"text": "已截屏", "_observation_image": "aGVsbG8="}

        registry = ToolRegistry()
        registry.register(Tool(
            name="screen_capture", description="截屏",
            parameters={"type": "object", "properties": {}},
            handler=screen_capture,
        ))

        class ShotFirstLLM:
            def __init__(self):
                self.turn = 0

            async def chat_stream_tools(self, *_args, **_kwargs):
                self.turn += 1
                if self.turn == 1:
                    yield "done", {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "shot", "type": "function",
                        "function": {"name": "screen_capture", "arguments": "{}"},
                    }]}
                else:
                    yield "done", {"role": "assistant", "content": "好了"}

        events = [event async for event in Agent(
            ShotFirstLLM(), registry, AgentConfig(max_steps=5),
        ).run(user_input, attachments=attachments)]
        blocked = any(
            event.type == "tool_result" and "没有具体任务" in str(event.data.get("result", ""))
            for event in events
        )
        return blocked, shots

    async def test_vague_input_is_still_blocked(self):
        """原有行为不能丢:用户随便说句话,模型上来先截屏猜,照样拦。"""
        blocked, shots = await self._first_step_screenshot("你好")
        self.assertTrue(blocked, "含糊输入下的首步截屏应当仍被拦下")
        self.assertEqual([], shots, "被拦下就不该真去截屏")

    async def test_explicitly_asking_for_screen_passes(self):
        blocked, shots = await self._first_step_screenshot("帮我截个屏看看")
        self.assertFalse(blocked)
        self.assertEqual(1, len(shots))

    async def test_cad_task_is_not_blocked(self):
        """用户的真实原话形态:要在图纸上标注。这必须放行。"""
        for text in (
            "在 CAD 图上给雨棚加尺寸标注",
            "打开 D:\\图纸\\活动板房.dwg 看一下",
            "把这张施工图的图层整理一下",
            "帮我核对一下图框里的标题栏对不对",
        ):
            with self.subTest(text=text):
                blocked, shots = await self._first_step_screenshot(text)
                self.assertFalse(blocked, f"CAD 类任务被误拦:{text}")
                self.assertEqual(1, len(shots))

    async def test_gui_operation_task_is_not_blocked(self):
        for text in (
            "打开 Excel 把 A1 填成 100",
            "点一下右上角的保存按钮",
        ):
            with self.subTest(text=text):
                blocked, _ = await self._first_step_screenshot(text)
                self.assertFalse(blocked, f"界面操作类任务被误拦:{text}")

    async def test_uploaded_image_allows_visual_followup_without_screen_keyword(self):
        blocked, shots = await self._first_step_screenshot("小A", attachments=[{
            "name": "reference.png", "data_url": "data:image/png;base64,aGVsbG8=",
        }])
        self.assertFalse(blocked)
        self.assertEqual(1, len(shots))

    async def test_task_does_not_need_a_software_name_whitelist(self):
        blocked, shots = await self._first_step_screenshot("核对当前界面里的数量是否正确")
        self.assertFalse(blocked)
        self.assertEqual(1, len(shots))

    def test_visual_task_matcher_covers_cad_and_gui(self):
        from backend.core.agent import _is_visual_task

        for text in ("在 dwg 上加标注", "AutoCAD 图层", "打开 word", "点击确定",
                     "看一下图纸的位置对不对", "活动板房平面图"):
            self.assertTrue(_is_visual_task(text), f"应判为视觉类任务:{text}")
        for text in ("你好", "今天天气怎么样", "帮我算一下 3 加 5", "记住我明天要开会"):
            self.assertFalse(_is_visual_task(text), f"不该判为视觉类任务:{text}")


class StepLimitAutoContinueTests(unittest.IsolatedAsyncioTestCase):
    """40.3:撞步数上限有进展就自动续,只在打转/硬上限/死局/手动停止时才停。"""

    @staticmethod
    def _registry(calls):
        registry = ToolRegistry()

        async def inspect(value):
            calls.append(value)
            return f"值 {value}"

        registry.register(Tool(
            "inspect", "检查", {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            }, inspect,
        ))
        return registry

    @staticmethod
    def _tool_call_llm(values_fn, *, finish_after=None):
        """生成 tool_calls 的假 LLM。values_fn(turn) 返回参数;finish_after 步后直接收尾。"""
        class LLM:
            def __init__(self):
                self.turn = 0

            async def chat_stream_tools(self, messages, **_kwargs):
                if not _kwargs.get("tools"):
                    yield "done", {"role": "assistant", "content": "总结"}
                    return
                self.turn += 1
                if finish_after is not None and self.turn > finish_after:
                    yield "done", {"role": "assistant", "content": "全部做完"}
                else:
                    yield "done", {"role": "assistant", "content": "", "tool_calls": [{
                        "id": f"c{self.turn}", "type": "function",
                        "function": {"name": "inspect",
                                     "arguments": json.dumps({"value": values_fn(self.turn)})},
                    }]}

        return LLM

    async def test_progress_auto_continues_without_truncated(self):
        calls = []
        registry = self._registry(calls)
        llm = self._tool_call_llm(lambda turn: turn, finish_after=5)
        events = [e async for e in Agent(
            llm(), registry, AgentConfig(max_steps=2),
        ).run("多步检查")]
        self.assertFalse(any(e.type == "truncated" for e in events),
                         "有进展自动续跑,不该产生 truncated / 写断点 / 提继续")
        self.assertTrue(any(e.type == "progress" and e.data.get("auto_continued") for e in events),
                        "自动续跑要发 auto_continued 的 progress 事件")
        self.assertTrue(any(e.type == "done" and not e.data.get("truncated") for e in events))
        self.assertEqual([1, 2, 3, 4, 5], calls)

    async def test_spinning_still_stops(self):
        calls = []
        registry = self._registry(calls)
        llm = self._tool_call_llm(lambda turn: 1)  # 永远同一个参数 → 打转
        events = [e async for e in Agent(
            llm(), registry, AgentConfig(max_steps=50),
        ).run("重复检查")]
        stop = next(e for e in events if e.type == "truncated")
        self.assertEqual("spinning", stop.data["reason"], "打转保护不能被自动续跑放开")

    async def test_hard_limit_stops_with_step_limit(self):
        calls = []
        registry = self._registry(calls)
        llm = self._tool_call_llm(lambda turn: turn)  # 一直有进展,永不收尾
        with mock.patch("backend.core.agent._HARD_STEP_LIMIT", 6):
            events = [e async for e in Agent(
                llm(), registry, AgentConfig(max_steps=2),
            ).run("很多步")]
        stop = next(e for e in events if e.type == "truncated")
        self.assertEqual("step_limit", stop.data["reason"])
        # 硬上限 = max(6, 10×2) = 20 步
        self.assertEqual(list(range(1, 21)), calls)

    async def _summary_prompt(self, values_fn, max_steps):
        """跑 agent 撞停后,抓取喂给 LLM 的总结提问内容。"""
        captured = {}
        registry = ToolRegistry()

        async def inspect(value):
            return f"值 {value}"

        registry.register(Tool("inspect", "检查", {
            "type": "object", "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        }, inspect))

        class CapturingLLM:
            def __init__(self):
                self.turn = 0

            async def chat_stream_tools(self, messages, **_kwargs):
                if not _kwargs.get("tools"):
                    captured["prompt"] = messages[-1]["content"]
                    yield "done", {"role": "assistant", "content": "总结"}
                    return
                self.turn += 1
                yield "done", {"role": "assistant", "content": "", "tool_calls": [{
                    "id": f"c{self.turn}", "type": "function",
                    "function": {"name": "inspect",
                                 "arguments": json.dumps({"value": values_fn(self.turn)})},
                }]}

        events = [e async for e in Agent(
            CapturingLLM(), registry, AgentConfig(max_steps=max_steps),
        ).run("任务")]
        self.assertTrue(any(e.type == "truncated" for e in events))
        return captured["prompt"]

    async def test_step_limit_summary_prompt_has_no_continue(self):
        with mock.patch("backend.core.agent._HARD_STEP_LIMIT", 6):
            prompt = await self._summary_prompt(lambda turn: turn, max_steps=2)
        self.assertIn("拆", prompt)
        self.assertNotIn("继续", prompt, "step_limit 的总结提问不该再提'继续'")

    async def test_spinning_summary_prompt_keeps_continue(self):
        prompt = await self._summary_prompt(lambda turn: 1, max_steps=50)
        self.assertIn("继续", prompt, "spinning 是纠正后还能继续,该提'继续'")


class CloseWindowProtectionTests(unittest.TestCase):
    def test_close_window_refuses_self(self):
        """42.8:close_window 绝不允许关小A 自己(xiaoa/electron/python 进程)。"""
        from backend.modules.computer import system_control
        self.assertIsNotNone(system_control._is_protected("小A", ""))
        self.assertIsNotNone(system_control._is_protected("AI 办公助手", ""))
        self.assertIsNotNone(system_control._is_protected("", "electron.exe"))
        self.assertIsNotNone(system_control._is_protected("", "python.exe"))
        self.assertIsNotNone(system_control._is_protected("", "pythonw.exe"))
        self.assertIsNone(system_control._is_protected("AutoCAD 2014", "acad.exe"))
        self.assertIsNone(system_control._is_protected("聊天应用", "chat.exe"))


class ContextExhaustedMessageTests(unittest.TestCase):
    def test_image_heavy_message_mentions_image_not_text(self):
        """43.7:图片占大头时,文案要指对方向(图太大),不是"你说过的话"。"""
        from backend.core.agent import _context_exhausted_message
        big_image = "data:image/png;base64," + "A" * 4000  # ~1000 tokens
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "你好"},
                {"type": "image_url", "image_url": {"url": big_image}},
            ]},
        ]
        msg = _context_exhausted_message(messages)
        self.assertIn("图", msg)
        self.assertIn("MB", msg)
        self.assertNotIn("你说过的话", msg)

    def test_text_heavy_message_keeps_original_wording(self):
        from backend.core.agent import _context_exhausted_message
        messages = [{"role": "user", "content": "这是一段很长的文字。" * 60}]
        msg = _context_exhausted_message(messages)
        self.assertIn("你说过的话", msg)


if __name__ == "__main__":
    unittest.main()
