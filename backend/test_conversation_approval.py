"""Conversation-wide authorization and restored image context regressions."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.api import server
from backend.core.agent import Agent, ConversationApproval
from backend.core.config import AgentConfig
from backend.core.tools import Tool, ToolRegistry


class OneCallLLM:
    def __init__(self, name, arguments="{}"):
        self.name = name
        self.arguments = arguments
        self.turn = 0

    async def chat_stream_tools(self, *_args, **_kwargs):
        self.turn += 1
        if self.turn == 1:
            yield "done", {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call-1", "type": "function", "function": {
                    "name": self.name, "arguments": self.arguments,
                },
            }]}
        else:
            yield "done", {"role": "assistant", "content": "完成"}


class ConversationApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = ToolRegistry()
        self.calls = []
        self.state = ConversationApproval()
        self.confirm = AsyncMock(return_value=True)
        for name in ("dangerous_write", "extension_inspect", "read_status"):
            async def action(_name=name):
                self.calls.append(_name)
                return "done"
            self.registry.register(Tool(
                name, name, {"type": "object"}, action,
                side_effect=name != "read_status", danger=name == "dangerous_write",
            ))

    async def run_action(self, name="dangerous_write", *, state=None, confirm=None):
        agent = Agent(
            OneCallLLM(name), self.registry, AgentConfig(confirm_before_action=True),
            confirm_fn=self.confirm if confirm is None else confirm,
            approval_state=self.state if state is None else state,
        )
        return [event async for event in agent.run("执行明确指定的任务", approval_mode="risk")]

    async def test_each_task_gets_its_own_approval(self):
        first = await self.run_action()
        second = await self.run_action("extension_inspect", state=ConversationApproval())
        self.assertEqual(["dangerous_write", "extension_inspect"], self.calls)
        self.assertEqual(2, self.confirm.await_count)
        self.assertEqual(2, sum(e.type == "confirm_needed" for e in first + second))
        self.assertEqual(["task"], next(e for e in first if e.type == "confirm_needed").data["approval_scope"])

    async def test_rejection_does_not_reprompt_or_block_read_only_steps(self):
        self.confirm.return_value = False
        first = await self.run_action()
        second = await self.run_action("extension_inspect", state=ConversationApproval())
        await self.run_action("read_status")
        self.assertEqual(2, self.confirm.await_count)
        self.assertEqual(["read_status"], self.calls)
        self.assertEqual(2, sum(e.type == "confirm_needed" for e in first + second))
        self.assertTrue(any(e.type == "tool_result" and not e.data["ok"] for e in second))

    async def test_new_conversation_requires_its_own_approval(self):
        await self.run_action()
        await self.run_action(state=ConversationApproval())
        self.assertEqual(2, self.confirm.await_count)

    async def test_concurrent_agents_wait_for_the_same_decision(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def pending(*_args):
            started.set()
            await release.wait()
            return True
        confirm = AsyncMock(side_effect=pending)
        first = asyncio.create_task(self.run_action(confirm=confirm))
        await asyncio.wait_for(started.wait(), 2)
        second = asyncio.create_task(self.run_action("extension_inspect", confirm=confirm))
        try:
            await asyncio.sleep(0)
            self.assertEqual([], self.calls)
            confirm.assert_awaited_once()
        finally:
            release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.assertEqual(1, sum(e.type == "confirm_needed" for events in results for e in events))
        self.assertEqual(["dangerous_write", "extension_inspect"], self.calls)

    async def test_cancelled_confirmation_is_fail_closed_without_reprompt(self):
        started = asyncio.Event()
        async def pending(*_args):
            started.set()
            await asyncio.Future()
        task = asyncio.create_task(self.run_action(confirm=AsyncMock(side_effect=pending)))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        events = await self.run_action()
        self.confirm.assert_not_awaited()
        self.assertEqual([], self.calls)
        self.assertFalse(any(e.type == "confirm_needed" for e in events))
        self.assertFalse(self.state.allowed)

    async def test_absent_confirmation_callback_does_not_execute(self):
        agent = Agent(OneCallLLM("dangerous_write"), self.registry,
                      AgentConfig(confirm_before_action=True), approval_state=self.state)
        events = [event async for event in agent.run("执行任务", approval_mode="risk")]
        await self.run_action()
        self.assertEqual([], self.calls)
        self.assertEqual(1, sum(e.type == "confirm_needed" for e in events))
        self.confirm.assert_not_awaited()

    async def test_authorization_does_not_bypass_existing_remote_hard_block(self):
        self.state.requested = self.state.allowed = True
        execute = AsyncMock(return_value="done")
        self.registry.register(Tool("run_command", "命令", {"type": "object"}, execute, side_effect=True))
        agent = Agent(OneCallLLM("run_command", '{"command":"shutdown /s"}'),
                      self.registry, AgentConfig(), approval_state=self.state)
        events = [event async for event in agent.run("关机")]
        execute.assert_not_awaited()
        self.assertTrue(any(e.type == "tool_result" and not e.data["ok"] for e in events))


class RestoredImageTests(unittest.TestCase):
    def test_followup_restores_latest_user_images_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("old.png", "new.png", "assistant.png"):
                (root / name).write_bytes(name.encode())
            def image_message(role, name):
                return {"role": role, "attachments": [{
                    "name": name, "type": "image/png", "saved_name": name,
                }]}
            history = [image_message("user", "old.png"), image_message("user", "new.png"),
                       image_message("assistant", "assistant.png"), {"role": "user", "content": "再看看"}]
            with patch.object(server, "_ATTACHMENT_DIR", root), patch.object(
                server, "_compress_attachment_image", return_value="data:image/jpeg;base64,preview",
            ):
                restored = server._restore_latest_image_attachments(history)
            self.assertEqual(["new.png"], [item["name"] for item in restored])
            self.assertTrue(restored[0]["data_url"].startswith("data:image/"))
            self.assertEqual(str((root / "new.png").resolve()), restored[0]["path"])

    def test_missing_and_path_traversal_attachments_are_not_restored(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, "_ATTACHMENT_DIR", Path(tmp)):
            history = [{"role": "user", "attachments": [
                {"type": "image/png", "saved_name": "missing.png"},
                {"type": "image/png", "saved_name": "../outside.png"},
                {"type": "text/plain", "saved_name": "document.txt"},
            ]}]
            self.assertEqual([], server._restore_latest_image_attachments(history))

    def test_restored_images_use_existing_context_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("one.png", "two.png"):
                (root / name).write_bytes(b"image")
            history = [{"role": "user", "attachments": [
                {"type": "image/png", "saved_name": name} for name in ("one.png", "two.png")
            ]}]
            with patch.object(server, "_ATTACHMENT_DIR", root), patch.object(
                server, "_compress_attachment_image", return_value="data:image/jpeg;base64,preview",
            ), patch.object(server, "_cap_attachment_image_budget") as cap:
                restored = server._restore_latest_image_attachments(history)
            cap.assert_called_once_with(restored)


class ChannelApprovalScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.states = []
        owner = self
        class FakeAgent:
            def __init__(self, *_args, approval_state=None, **_kwargs):
                owner.states.append(approval_state)
                self.memory = None
            async def run(self, *_args, **_kwargs):
                yield SimpleNamespace(type="message", data={"content": "完成"})
        for name, value in {
            "Agent": FakeAgent, "CHANNEL_HISTORY": {},
            "CHANNEL_RESUME": {}, "CHANNEL_LOCKS": {}, "CHANNEL_CONFIRM": {},
            "CHANNEL_ACTIVE_TASKS": {}, "CHANNEL_LAST_USED": {},
            "CHANNEL_LOCKED": {"locked": False},
            "_channel_send": AsyncMock(), "_channel_cloud_host": lambda: "",
            "_channel_permission": lambda *_args: (True, 0),
            "_AuditTrace": lambda *_a, **_k: SimpleNamespace(consume=lambda _event: None),
        }.items():
            self.stack.enter_context(patch.object(server, name, value))

    async def test_remote_tasks_get_fresh_approval_without_cross_scope(self):
        await server.handle_channel_command("feishu", "user-1", "任务一")
        first = self.states[-1]
        first.requested = first.allowed = True
        await server.handle_channel_command("feishu", "user-1", "任务二")
        self.assertIsNot(first, self.states[-1])
        self.assertFalse(self.states[-1].requested)
        await server.handle_channel_command("weixin", "user-1", "任务三")
        self.assertIsNot(first, self.states[-1])
        self.assertFalse(self.states[-1].requested)
        await server.handle_channel_command("feishu", "user-2", "任务四")
        self.assertIsNot(first, self.states[-1])
        self.assertFalse(self.states[-1].requested)

    async def test_remote_lock_still_revokes_running_work(self):
        await server.handle_channel_command("feishu", "user-1", "任务")
        await server.handle_channel_command("feishu", "user-1", "锁定")
        self.assertTrue(server.CHANNEL_LOCKED["locked"])
