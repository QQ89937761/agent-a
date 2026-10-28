"""Interjections interrupt model waits without cancelling real tool operations."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.core.agent import Agent
from backend.core.config import AgentConfig, LLMConfig
from backend.core.llm import LLMClient, ProviderFailoverClient
from backend.core.tools import ToolRegistry
from backend.core import task_trace


class InterjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_stream_is_closed_and_new_instructions_apply_without_old_reply(self):
        for partial in [False, True]:
            with self.subTest(partial=partial), tempfile.TemporaryDirectory() as tmp:
                pending = []
                entered, closed = asyncio.Event(), asyncio.Event()
                snapshots = []

                class LLM:
                    async def chat_stream_tools(self, messages, **kwargs):
                        snapshots.append(list(messages))
                        if len(snapshots) == 1:
                            try:
                                if partial:
                                    yield "delta", "旧回复片段"
                                entered.set()
                                await asyncio.Event().wait()
                            finally:
                                closed.set()
                        else:
                            assert closed.is_set(), "previous request must close before replacement"
                            yield "done", {"role": "assistant", "content": "已重新看图"}

                def take():
                    items = list(pending)
                    pending.clear()
                    return items

                agent = Agent(LLM(), ToolRegistry(), AgentConfig(), interjection_fn=take,
                              result_dir=Path(tmp))
                async def collect():
                    return [event async for event in agent.run("整理原图")]
                task = asyncio.create_task(collect())
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    pending.extend(["图片我换了", "请重新看图，不要沿用旧数据"])
                    events = await asyncio.wait_for(task, 2)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                self.assertEqual(2, len(snapshots))
                inserted = [m["content"] for m in snapshots[1] if "[用户中途插话]" in str(m.get("content"))]
                self.assertEqual(2, len(inserted))
                self.assertIn("图片我换了", inserted[0])
                self.assertIn("请重新看图", inserted[1])
                self.assertTrue(any(e.type == "interjection_applied" for e in events))
                self.assertFalse(any(e.type in {"error", "aborted", "truncated"} for e in events))
                self.assertEqual(1, sum(e.type == "done" for e in events))
                if partial:
                    self.assertTrue(any(m.get("content") == "旧回复片段" for m in snapshots[1]))
                    self.assertTrue(any(e.type == "message_end" for e in events))

    async def test_consecutive_interjections_close_real_http_streams_across_protocols(self):
        for protocol in ["openai", "responses", "anthropic"]:
            for failover in [False, True]:
                with self.subTest(protocol=protocol, failover=failover), tempfile.TemporaryDirectory() as tmp:
                    pending, bodies, closed = [], [], []
                    entered = [asyncio.Event(), asyncio.Event()]
                    frames = {
                        "openai": [
                            'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
                            'data: {"choices":[{"delta":{"content":"finished"}}]}\n\ndata: [DONE]\n\n',
                        ],
                        "responses": [
                            'data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
                            'data: {"type":"response.completed","response":{"status":"completed",'
                            '"output":[{"type":"message","role":"assistant","content":'
                            '[{"type":"output_text","text":"finished"}]}]}}\n\n',
                        ],
                        "anthropic": [
                            'event: content_block_delta\ndata: {"delta":{"type":"text_delta","text":"partial"}}\n\n',
                            'event: content_block_delta\ndata: {"delta":{"type":"text_delta","text":"finished"}}\n\n'
                            'event: message_stop\ndata: {}\n\n',
                        ],
                    }[protocol]

                    class Body(httpx.AsyncByteStream):
                        def __init__(self, index):
                            self.index = index

                        async def __aiter__(self):
                            if self.index < 2:
                                if self.index == 1:
                                    yield frames[0].encode()
                                entered[self.index].set()
                                await asyncio.Event().wait()
                            else:
                                yield frames[1].encode()

                        async def aclose(self):
                            closed.append(self.index)

                    def handler(request):
                        index = len(bodies)
                        self.assertEqual(list(range(index)), closed, "old HTTP body must close before replacement")
                        bodies.append(json.loads(request.content))
                        return httpx.Response(200, stream=Body(index), headers={"content-type": "text/event-stream"})

                    cfg = LLMConfig(protocol=protocol, base_url="https://gateway.test/v1", api_key="test-only-key")
                    client = ProviderFailoverClient(cfg) if failover else LLMClient(cfg)
                    inner = client._providers[0] if failover else client
                    inner.max_attempts = 1
                    inner._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=cfg.base_url)

                    def take():
                        items = list(pending)
                        pending.clear()
                        return items

                    async def collect():
                        return [event async for event in Agent(client, ToolRegistry(), AgentConfig(),
                            interjection_fn=take, result_dir=Path(tmp)).run("initial task")]

                    task = asyncio.create_task(collect())
                    try:
                        for index in range(2):
                            await asyncio.wait_for(entered[index].wait(), 2)
                            pending.append(f"correction {index + 1}")
                        events = await asyncio.wait_for(task, 2)
                    finally:
                        if not task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                        await client.aclose()
                    self.assertEqual([0, 1, 2], closed)
                    self.assertEqual(3, len(bodies))
                    self.assertIn("correction 1", json.dumps(bodies[-1]))
                    self.assertIn("correction 2", json.dumps(bodies[-1]))
                    self.assertEqual(2, sum(e.type == "interjection_applied" for e in events))
                    self.assertEqual(1, sum(e.type == "done" for e in events))
                    self.assertFalse(any(e.type in {"error", "aborted", "truncated"} for e in events))

    async def test_running_write_finishes_once_before_interjection_skips_next_write(self):
        registry = ToolRegistry()
        started, release = asyncio.Event(), asyncio.Event()
        pending, calls, snapshots = [], [], []

        @registry.tool("write_one", "write first", {"type": "object"}, side_effect=True)
        async def first():
            started.set()
            await release.wait()
            calls.append("first")
            return "first saved"

        @registry.tool("write_two", "write second", {"type": "object"}, side_effect=True)
        async def second():
            calls.append("second")
            return "second saved"

        class LLM:
            async def chat_stream_tools(self, messages, **kwargs):
                snapshots.append(list(messages))
                if len(snapshots) == 1:
                    yield "done", {"role": "assistant", "content": "", "tool_calls": [
                        {"id": name, "type": "function", "function": {"name": name, "arguments": "{}"}}
                        for name in ["write_one", "write_two"]
                    ]}
                else:
                    yield "done", {"role": "assistant", "content": "按新要求继续"}

        def take():
            items = list(pending)
            pending.clear()
            return items

        async def collect():
            return [e async for e in Agent(LLM(), registry, AgentConfig(), interjection_fn=take).run("写两次")]
        task = asyncio.create_task(collect())
        try:
            await asyncio.wait_for(started.wait(), 2)
            pending.append("不要写第二次，换图后再处理")
            await asyncio.sleep(0.15)
            self.assertFalse(task.done())
            release.set()
            events = await asyncio.wait_for(task, 2)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(["first"], calls)
        skipped = next(e for e in events if e.type == "tool_result" and e.data.get("skipped"))
        self.assertTrue(skipped.data["ok"])
        self.assertEqual("write_two", skipped.data["name"])
        tool_results = [m for m in snapshots[1] if m.get("role") == "tool"]
        self.assertEqual(["write_one", "write_two"], [m["tool_call_id"] for m in tool_results])
        self.assertEqual("first saved", tool_results[0]["content"])

    async def test_interjection_during_safety_summary_resumes_instead_of_finishing_old_task(self):
        pending, snapshots = [], []
        entered, closed = asyncio.Event(), asyncio.Event()
        registry = ToolRegistry()

        @registry.tool("read_fact", "read", {"type": "object"})
        async def read():
            return "saved progress"

        class LLM:
            async def chat_stream_tools(self, messages, **kwargs):
                snapshots.append(list(messages))
                if len(snapshots) == 1:
                    yield "done", {"role": "assistant", "content": "", "tool_calls": [{
                        "id": "read", "type": "function", "function": {"name": "read_fact", "arguments": "{}"}
                    }]}
                elif len(snapshots) == 2:
                    try:
                        entered.set()
                        await asyncio.Event().wait()
                    finally:
                        closed.set()
                else:
                    assert closed.is_set()
                    yield "done", {"role": "assistant", "content": "已处理更正"}

        def take():
            items = list(pending)
            pending.clear()
            return items

        async def collect():
            return [e async for e in Agent(LLM(), registry, AgentConfig(), interjection_fn=take).run("读取")]
        with patch("backend.core.agent._is_spinning", side_effect=lambda recent, window=3: bool(recent)):
            task = asyncio.create_task(collect())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                pending.append("先处理这条更正")
                events = await asyncio.wait_for(task, 2)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(3, len(snapshots))
        self.assertTrue(any(m.get("content") == "saved progress" for m in snapshots[-1]))
        self.assertFalse(any(e.type in {"truncated", "error"} for e in events))
        self.assertTrue(any(e.type == "message" and e.data["content"] == "已处理更正" for e in events))

    async def test_stop_closes_silent_model_request_without_waiting_for_timeout(self):
        entered, closed = asyncio.Event(), asyncio.Event()
        stopped = False
        class LLM:
            async def chat_stream_tools(self, messages, **kwargs):
                try:
                    entered.set()
                    await asyncio.Event().wait()
                    yield "done", {}
                finally:
                    closed.set()
        async def collect():
            return [e async for e in Agent(LLM(), ToolRegistry(), AgentConfig(), abort_fn=lambda: stopped).run("等待")]
        task = asyncio.create_task(collect())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            stopped = True
            events = await asyncio.wait_for(task, 2)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(closed.is_set())
        self.assertTrue(any(e.type == "aborted" for e in events))
        self.assertFalse(any(e.type == "error" for e in events))

    def test_applied_and_skipped_operations_are_neutral_durable_progress(self):
        trace = task_trace.new_trace("test", "task")
        task_trace.consume(trace, "tool_call", {"name": "write"})
        task_trace.consume(trace, "tool_result", {"name": "write", "ok": True, "skipped": True})
        self.assertEqual("skipped", trace["events"][0]["status"])
        self.assertTrue(task_trace.consume(trace, "interjection_applied", {}))
        self.assertEqual("正在按插话调整任务", trace["current"])
        self.assertEqual("notice", trace["events"][-1]["kind"])
        self.assertNotIn("failed", json.dumps(trace))


if __name__ == "__main__":
    unittest.main()
