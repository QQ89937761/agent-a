"""LLM hot reload must not strand long-lived holders or close active clients."""
import asyncio
import ssl
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.api import server
from backend.core.config import LLMConfig
from backend.core.llm import LLMClient, LLMError
from backend.modules.memory.vector_memory import VectorMemory


class _ImmediateLLM:
    def __init__(self, content="新客户端"):
        self.content = content
        self.calls = 0

    async def chat(self, messages, temperature=0.1, model=None):
        self.calls += 1
        return {"content": self.content}


class _WaitingLLM(_ImmediateLLM):
    def __init__(self, content="完成"):
        super().__init__(content)
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.aclose = AsyncMock()

    async def chat(self, messages, temperature=0.4, model=None):
        self.calls += 1
        self.started.set()
        await self.finish.wait()
        return {"content": self.content}


class LLMHotReloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server._LLM_USAGE.clear()
        server._LLM_RETIRE.clear()
        permission = patch.object(server, "_channel_permission", return_value=(True, 0))
        permission.start()
        self.addCleanup(permission.stop)

    async def asyncTearDown(self):
        server._LLM_USAGE.clear()
        server._LLM_RETIRE.clear()

    async def test_memory_extraction_uses_reloaded_llm(self):
        old = _ImmediateLLM("旧客户端")
        replacement = _ImmediateLLM("新客户端")
        current = {"llm": old}
        with tempfile.TemporaryDirectory(prefix="xiaoa-memory-llm-") as tmp:
            memory = VectorMemory(None, lambda: current["llm"], Path(tmp))
            current["llm"] = replacement
            facts = await memory.extract_facts([{"role": "user", "content": "我喜欢绿色"}])

        self.assertEqual(["新客户端"], facts)
        self.assertEqual(0, old.calls)
        self.assertEqual(1, replacement.calls)

    async def test_reply_draft_keeps_selected_llm_alive_during_reload(self):
        old = _WaitingLLM("旧客户端完成")
        replacement = _ImmediateLLM()
        info = {
            "source": "feishu", "sender": "同事", "incoming": "进度？",
            "reply_target": "同事", "is_group": False,
        }
        with patch.object(server, "LLM", old):
            task = asyncio.create_task(server._compose_reply_draft(info))
            await old.started.wait()
            server.LLM = replacement
            await server._retire_llm(old)
            old.aclose.assert_not_awaited()
            old.finish.set()
            self.assertEqual("旧客户端完成", await task)

        old.aclose.assert_awaited_once()
        self.assertEqual(0, replacement.calls)

    async def test_channel_agent_keeps_selected_llm_alive_during_reload(self):
        old = _WaitingLLM()
        replacement = _ImmediateLLM()
        started = asyncio.Event()
        finish = asyncio.Event()
        selected = []
        run_options = []
        target = "llm-reload-test"

        class _Agent:
            def __init__(self, llm, *args, **kwargs):
                selected.append(llm)

            async def run(self, *args, **kwargs):
                run_options.append(kwargs)
                started.set()
                await finish.wait()
                yield SimpleNamespace(type="message", data={"content": "渠道任务完成"})

        audit = SimpleNamespace(consume=lambda event: None)
        server.CHANNEL_LOCKED["locked"] = False
        server.CHANNEL_LOCKS.pop(("feishu", target), None)
        server.CHANNEL_HISTORY.pop(("feishu", target), None)

        with (
            patch.object(server, "LLM", old),
            patch.object(server, "Agent", _Agent),
            patch.object(server, "_AuditTrace", return_value=audit),
            patch.object(server, "_channel_send", new=AsyncMock()),
        ):
            task = asyncio.create_task(server.handle_channel_command("feishu", target, "执行长任务"))
            await asyncio.wait_for(started.wait(), 2)
            server.LLM = replacement
            await server._retire_llm(old)
            old.aclose.assert_not_awaited()
            finish.set()
            await task

        old.aclose.assert_awaited_once()
        self.assertEqual([old], selected)
        self.assertEqual(server.CFG.llm.chat_model, run_options[0]["model"])
        self.assertEqual("high", run_options[0]["reasoning_effort"])
        server.CHANNEL_LOCKS.pop(("feishu", target), None)
        server.CHANNEL_HISTORY.pop(("feishu", target), None)

    async def test_channel_state_isolated_between_channels_with_same_target(self):
        target = "same-channel-target"
        old_locked = server.CHANNEL_LOCKED["locked"]
        server.CHANNEL_LOCKED["locked"] = False

        class _Agent:
            def __init__(self, *_args, **_kwargs):
                pass

            async def run(self, text, **_kwargs):
                yield SimpleNamespace(type="message", data={"content": f"回复:{text}"})

        audit = SimpleNamespace(consume=lambda _event: None)
        try:
            with (
                patch.object(server, "Agent", _Agent),
                patch.object(server, "_AuditTrace", return_value=audit),
                patch.object(server, "_channel_send", new=AsyncMock()),
            ):
                await server.handle_channel_command("feishu", target, "飞书内容")
                await server.handle_channel_command("weixin", target, "微信内容")

            feishu_history = server.CHANNEL_HISTORY[("feishu", target)]
            weixin_history = server.CHANNEL_HISTORY[("weixin", target)]
            self.assertEqual("飞书内容", feishu_history[0]["content"])
            self.assertEqual("微信内容", weixin_history[0]["content"])
            self.assertNotEqual(feishu_history, weixin_history)
        finally:
            for source in ("feishu", "weixin"):
                server.CHANNEL_ACTIVE_TASKS.pop((source, target), None)
                server.CHANNEL_LOCKS.pop((source, target), None)
                server.CHANNEL_HISTORY.pop((source, target), None)
                server.CHANNEL_RESUME.pop((source, target), None)
            server.CHANNEL_LOCKED["locked"] = old_locked

    async def test_channel_busy_message_is_delivered_as_interjection(self):
        target = "channel-interjection-test"
        started = asyncio.Event()
        continue_run = asyncio.Event()
        inserted = []

        class _Agent:
            def __init__(self, _llm, *_args, interjection_fn=None, **_kwargs):
                self.interjection_fn = interjection_fn

            async def run(self, *_args, **_kwargs):
                started.set()
                await continue_run.wait()
                inserted.extend(self.interjection_fn())
                yield SimpleNamespace(type="message", data={"content": "已按补充要求调整。"})

        audit = SimpleNamespace(consume=lambda _event: None)
        old_locked = server.CHANNEL_LOCKED["locked"]
        server.CHANNEL_LOCKED["locked"] = False
        server.CHANNEL_LOCKS.pop(("feishu", target), None)
        server.CHANNEL_HISTORY.pop(("feishu", target), None)
        try:
            with (
                patch.object(server, "LLM", _ImmediateLLM()),
                patch.object(server, "Agent", _Agent),
                patch.object(server, "_AuditTrace", return_value=audit),
                patch.object(server, "_channel_send", new=AsyncMock()),
            ):
                task = asyncio.create_task(server.handle_channel_command(
                    "feishu", target, "开始一个长任务",
                ))
                await asyncio.wait_for(started.wait(), 2)
                await server.handle_channel_command("feishu", target, "等等，先找最近的文件")
                continue_run.set()
                await task

            self.assertEqual(["等等，先找最近的文件"], inserted)
            self.assertTrue(any(
                "[用户中途插话]" in item["content"]
                for item in server.CHANNEL_HISTORY[("feishu", target)]
            ))
        finally:
            server.CHANNEL_ACTIVE_TASKS.pop(("feishu", target), None)
            server.CHANNEL_LOCKS.pop(("feishu", target), None)
            server.CHANNEL_HISTORY.pop(("feishu", target), None)
            server.CHANNEL_RESUME.pop(("feishu", target), None)
            server.CHANNEL_LOCKED["locked"] = old_locked

    async def test_channel_does_not_repeat_progress_messages(self):
        target = "channel-no-progress-spam-test"

        class _Agent:
            def __init__(self, *_args, **_kwargs):
                pass

            async def run(self, *_args, **_kwargs):
                yield SimpleNamespace(type="tool_call", data={"name": "code_run"})
                yield SimpleNamespace(type="screenshot", data={"image": "c2NyZWVuc2hvdA=="})
                yield SimpleNamespace(type="message", data={"content": "任务已完成。"})

        audit = SimpleNamespace(consume=lambda _event: None)
        send = AsyncMock()
        old_locked = server.CHANNEL_LOCKED["locked"]
        server.CHANNEL_LOCKED["locked"] = False
        server.CHANNEL_LOCKS.pop(("weixin", target), None)
        server.CHANNEL_HISTORY.pop(("weixin", target), None)
        try:
            with (
                patch.object(server, "LLM", _ImmediateLLM()),
                patch.object(server, "Agent", _Agent),
                patch.object(server, "_AuditTrace", return_value=audit),
                patch.object(server, "_channel_send", new=send),
            ):
                await server.handle_channel_command("weixin", target, "执行任务")

            replies = [call.args[2] for call in send.await_args_list]
            self.assertEqual(["任务已完成。"], replies)
            self.assertFalse(any(reply.startswith("进度：") for reply in replies))
        finally:
            server.CHANNEL_ACTIVE_TASKS.pop(("weixin", target), None)
            server.CHANNEL_LOCKS.pop(("weixin", target), None)
            server.CHANNEL_HISTORY.pop(("weixin", target), None)
            server.CHANNEL_RESUME.pop(("weixin", target), None)
            server.CHANNEL_LOCKED["locked"] = old_locked

    async def test_channel_stop_request_aborts_and_keeps_resume_checkpoint(self):
        target = "channel-stop-test"
        started = asyncio.Event()
        checkpoint = [{"role": "user", "content": "续做上下文"}]

        class _Agent:
            def __init__(self, _llm, *_args, abort_fn=None, **_kwargs):
                self.abort_fn = abort_fn

            async def run(self, *_args, **_kwargs):
                started.set()
                while not self.abort_fn():
                    await asyncio.sleep(0.005)
                yield SimpleNamespace(type="aborted", data={"resume_messages": checkpoint})
                yield SimpleNamespace(type="done", data={"aborted": True})

        audit = SimpleNamespace(consume=lambda _event: None)
        send = AsyncMock()
        old_locked = server.CHANNEL_LOCKED["locked"]
        server.CHANNEL_LOCKED["locked"] = False
        server.CHANNEL_LOCKS.pop(("feishu", target), None)
        server.CHANNEL_HISTORY.pop(("feishu", target), None)
        server.CHANNEL_RESUME.pop(("feishu", target), None)
        try:
            with (
                patch.object(server, "LLM", _ImmediateLLM()),
                patch.object(server, "Agent", _Agent),
                patch.object(server, "_AuditTrace", return_value=audit),
                patch.object(server, "_channel_send", new=send),
            ):
                task = asyncio.create_task(server.handle_channel_command(
                    "feishu", target, "执行一个任务",
                ))
                await asyncio.wait_for(started.wait(), 2)
                await server.handle_channel_command("feishu", target, "先停下吧")
                await task

            self.assertEqual(checkpoint, server.CHANNEL_RESUME[("feishu", target)])
            self.assertTrue(any(
                "已停止当前任务" in call.args[2]
                for call in send.await_args_list
            ))
            self.assertNotIn(("feishu", target), server.CHANNEL_ACTIVE_TASKS)
        finally:
            server.CHANNEL_ACTIVE_TASKS.pop(("feishu", target), None)
            server.CHANNEL_LOCKS.pop(("feishu", target), None)
            server.CHANNEL_HISTORY.pop(("feishu", target), None)
            server.CHANNEL_RESUME.pop(("feishu", target), None)
            server.CHANNEL_LOCKED["locked"] = old_locked

    async def test_windows_client_uses_verified_system_roots_without_certifi_file(self):
        context = ssl.create_default_context()
        with (
            patch("backend.core.llm.sys.platform", "win32"),
            patch("backend.core.llm.ssl.create_default_context", return_value=context) as create,
            patch("certifi.where", side_effect=FileNotFoundError("removed/cacert.pem")) as where,
        ):
            client = LLMClient(LLMConfig())
            create.assert_not_called()
            self.assertIsNotNone(client._http())
            self.assertIs(client._http(), client._client)
            create.assert_called_once_with()
            where.assert_not_called()
            self.assertTrue(context.check_hostname)
            self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
            await client.aclose()

    async def test_client_defers_tls_setup_and_reports_missing_bundle(self):
        with patch("backend.core.llm.httpx.AsyncClient", side_effect=FileNotFoundError("cacert.pem")) as ctor:
            client = LLMClient(LLMConfig())
            ctor.assert_not_called()
            with self.assertRaisesRegex(LLMError, "TLS 证书文件不可用"):
                client._http()
            await client.aclose()


if __name__ == "__main__":
    unittest.main()
