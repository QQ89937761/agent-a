"""Regression checks for creating desktop conversations on first valid input."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import WebSocketDisconnect
from backend.api import server


class ScriptedWebSocket:
    def __init__(self):
        self.headers = {"origin": "", "host": "127.0.0.1:1985"}
        self.query_params = {}
        self.cookies = {}
        self.client = SimpleNamespace(host="127.0.0.1")
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.sent = []

    async def accept(self): pass
    async def close(self, code=None): pass

    async def receive_text(self):
        message = await self.incoming.get()
        if message is None:
            raise WebSocketDisconnect()
        return json.dumps(message)

    async def send_text(self, payload):
        event = json.loads(payload)
        self.sent.append(event)
        self.outgoing.put_nowait(event)

    def send(self, **message):
        self.incoming.put_nowait(message)

    async def event(self, kind):
        async def receive():
            while True:
                event = await self.outgoing.get()
                if event["type"] == kind:
                    return event["data"]
        return await asyncio.wait_for(receive(), timeout=5)


class DeferredConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.path = root / "conversations.json"
        self.stack.enter_context(patch.object(server, "_ATTACHMENT_DIR", root / "uploads"))
        self.history = [{"role": "user", "content": "旧对话内容"}]
        self.conversations = {
            "default": server._new_conversation("default", "已有任务", self.history),
            "old_empty": server._new_conversation("old_empty"),
        }
        self.calls = []
        self.approval_states = []
        self.gate = asyncio.Event()
        self.gate.set()
        owner = self

        class FakeAgent:
            def __init__(self, *_args, **_kwargs):
                self.memory = None
                self.approval_state = _kwargs["approval_state"]

            async def run(self, text, **kwargs):
                owner.approval_states.append(self.approval_state)
                owner.calls.append((text, kwargs))
                await owner.gate.wait()
                yield SimpleNamespace(type="message_end", data={"content": "已完成"})
                yield SimpleNamespace(type="done", data={})

        for name, value in {
            "DESKTOP_CONVERSATIONS": self.conversations,
            "DESKTOP_HISTORY": self.conversations["default"]["messages"],
            "_CONVERSATIONS_PATH": self.path,
            "_HISTORY_PATH": root / "history.json",
            "_RESUME_PATH": root / "resume.json",
            "_RESUME_STATES": {},
            "_ACTIVE_CLIENT_INPUTS": {},
            "_COMPLETED_CLIENT_INPUTS": {},
            "ACTIVE_WS": set(),
            "MEMORY": None,
            # Ordinary conversations have no project routes in this fixture.
            "_SHARED_ROUTE_HOME": server.app_home(),
            "_SHARED_ROUTE_NAMES": (),
            "_SHARED_ROUTE_READY": True,
            "_desktop_project_request": AsyncMock(return_value={"ok": True, "context": None}),
            "Agent": FakeAgent,
            "_AuditTrace": MagicMock(),
            "_llm_for_provider": lambda _: (None, False, ["demo"], "default"),
            "_retain_llm": lambda _: None,
            "_release_llm": AsyncMock(),
        }.items():
            self.stack.enter_context(patch.object(server, name, value))
        self.socket = None
        self.task = None

    async def asyncTearDown(self):
        await self.disconnect()

    async def connect(self):
        self.socket = ScriptedWebSocket()
        self.task = asyncio.create_task(server.ws_endpoint(self.socket))
        listing = await self.socket.event("conversation_list")
        await self.socket.event("history")
        return listing

    async def disconnect(self):
        if self.task:
            self.socket.incoming.put_nowait(None)
            await asyncio.wait_for(self.task, timeout=5)
            self.task = None

    async def input(self, text="整理本月销售报表", cid="conv_draft", client_id="input_1", **extra):
        self.socket.send(type="user_input", conversation_id=cid, new_conversation=True,
                         client_id=client_id, text=text, **extra)

    async def completed(self):
        await self.socket.event("done")
        return await self.socket.event("conversation_list")

    async def test_repeated_new_clicks_do_not_create_or_save(self):
        listing = await self.connect()
        self.assertEqual(["default"], [item["id"] for item in listing["conversations"]])
        ids = []
        for _ in range(4):
            self.socket.send(type="conversation_create")
            listing = await self.socket.event("conversation_list")
            ids.append(listing["current_id"])
            history = await self.socket.event("history")
            self.assertEqual([], history["messages"])
            self.assertEqual(["default"], [item["id"] for item in listing["conversations"]])
        self.assertEqual(4, len(set(ids)))
        self.assertEqual(2, len(self.conversations))
        self.assertFalse(self.path.exists())
        self.socket.send(type="stop")
        await self.socket.event("stopped")
        self.socket.send(type="continue_task")
        await self.socket.event("error")
        self.assertFalse(self.path.exists())

    async def test_first_message_creates_named_record_before_reply(self):
        await self.connect()
        self.gate.clear()
        await self.input(text="你好，小A，整理本月销售报表")
        listing = await self.socket.event("conversation_list")
        await self.socket.event("input_ack")
        created = self.conversations["conv_draft"]
        self.assertEqual("整理本月销售报表", created["title"])
        self.assertEqual([], created["messages"])
        self.assertTrue(created["started"])
        self.assertEqual("conv_draft", listing["current_id"])
        self.assertIn("conv_draft", [item["id"] for item in listing["conversations"]])
        loaded = server._load_conversations()
        self.assertTrue(loaded["conv_draft"]["started"])
        self.assertEqual(self.history, self.conversations["default"]["messages"])
        self.gate.set()
        await self.completed()
        self.assertEqual(2, len(created["messages"]))

    async def test_attachment_only_creates_one_record(self):
        await self.connect()
        attachment = {"name": "报告.txt", "type": "text/plain", "kind": "text", "text": "报表"}
        with patch.object(server, "_prepare_attachments", return_value=[attachment]):
            await self.input(text="", attachments=[attachment])
            await self.completed()
        self.assertEqual("附件对话", self.conversations["conv_draft"]["title"])
        self.assertEqual(1, len(self.calls))

    async def test_invalid_inputs_never_create_records(self):
        await self.connect()
        for text in ("", "   "):
            await self.input(text=text)
            await self.socket.event("error")
            self.assertNotIn("conv_draft", self.conversations)
        with patch.object(server, "_prepare_attachments", side_effect=ValueError("附件无效")):
            await self.input(attachments=[{"name": "bad"}])
            await self.socket.event("error")
        self.assertFalse(self.path.exists())
        self.assertEqual([], self.calls)

    async def test_continue_in_empty_draft_is_a_normal_follow_up(self):
        await self.connect()
        await self.input(text="继续")
        await self.completed()
        self.assertEqual(1, len(self.calls))
        self.assertEqual("继续", self.calls[0][0])
        self.assertIn("conv_draft", self.conversations)

    async def test_unknown_id_is_not_redirected_to_old_conversation(self):
        await self.connect()
        self.socket.send(type="user_input", conversation_id="missing", text="新任务", client_id="bad")
        error = await self.socket.event("error")
        self.assertIn("对话不存在", error["msg"])
        await self.input(cid="../invalid")
        await self.socket.event("error")
        self.assertEqual([], self.calls)
        self.assertEqual(self.history, self.conversations["default"]["messages"])

    async def test_reconnect_retry_keeps_id_and_does_not_duplicate(self):
        await self.connect()
        await self.input()
        await self.completed()
        await self.disconnect()
        await self.connect()
        await self.input()
        await self.socket.event("input_done")
        self.assertEqual(1, len(self.calls))
        self.assertEqual(3, len(self.conversations))
        self.assertEqual(2, len(self.conversations["conv_draft"]["messages"]))

    async def test_active_retry_and_followup_do_not_rename_or_duplicate(self):
        await self.connect()
        self.gate.clear()
        await self.input()
        await self.socket.event("input_ack")
        await self.input()
        await self.socket.event("input_ack")
        self.gate.set()
        await self.completed()
        self.assertEqual(1, len(self.calls))
        self.socket.send(type="conversation_rename", conversation_id="conv_draft", title="我的报表")
        await self.socket.event("conversation_list")
        await self.input(text="再补充一张表", client_id="input_2")
        await self.completed()
        self.assertEqual("我的报表", self.conversations["conv_draft"]["title"])
        self.assertEqual(4, len(self.conversations["conv_draft"]["messages"]))
        self.socket.send(type="conversation_select", conversation_id="default")
        history = await self.socket.event("history")
        self.assertEqual(self.history, history["messages"])

    async def test_legacy_client_creates_on_first_message(self):
        await self.connect()
        self.socket.send(type="conversation_create")
        listing = await self.socket.event("conversation_list")
        await self.socket.event("history")
        cid = listing["current_id"]
        self.socket.send(type="user_input", text="你好", client_id="legacy")
        await self.completed()
        self.assertEqual("你好", self.conversations[cid]["title"])

    async def test_empty_legacy_rows_do_not_consume_limit(self):
        await self.connect()
        with patch.object(server, "_MAX_CONVERSATIONS", 2):
            await self.input()
            await self.completed()
            await self.input(cid="conv_over_limit", client_id="limit")
            error = await self.socket.event("error")
            self.assertIn("上限", error["msg"])
            self.assertNotIn("conv_over_limit", self.conversations)

    async def test_each_task_gets_fresh_approval_and_new_conversations_are_isolated(self):
        await self.connect()
        await self.input()
        await self.completed()
        state = self.approval_states[-1]
        state.requested = state.allowed = True
        await self.disconnect()
        await self.connect()
        await self.input(client_id="second_turn")
        await self.completed()
        self.assertIsNot(state, self.approval_states[-1])
        self.assertFalse(self.approval_states[-1].requested)
        await self.input(cid="conv_another", client_id="another")
        await self.completed()
        self.assertIsNot(state, self.approval_states[-1])
        self.assertFalse(self.approval_states[-1].requested)

    async def test_deleting_conversation_removes_its_runtime(self):
        await self.connect()
        await self.input()
        await self.completed()
        self.socket.send(type="conversation_delete", conversation_id="conv_draft")
        await self.socket.event("conversation_deleted")
        self.assertNotIn("conv_draft", self.conversations)

    async def test_conversation_model_is_persisted_without_sending_chat(self):
        await self.connect()
        options = [
            {"provider_id": "default", "model": "demo"},
            {"provider_id": "other", "model": "demo"},
        ]
        with patch.object(server, "_model_options", return_value=options):
            self.socket.send(type="conversation_model", conversation_id="default", provider_id="other", model="demo")
            listing = await self.socket.event("conversation_list")
        selection = {"provider_id": "other", "model": "demo"}
        self.assertEqual(selection, self.conversations["default"]["model_selection"])
        self.assertEqual(selection, listing["conversations"][0]["model_selection"])
        self.assertEqual(selection, server._load_conversations()["default"]["model_selection"])
        self.assertNotIn("model_selection", self.conversations["old_empty"])
        self.assertEqual([], self.calls)
        await self.disconnect()
        listing = await self.connect()
        self.assertEqual(selection, listing["conversations"][0]["model_selection"])

    async def test_first_input_persists_the_model_and_leaves_other_chats_unchanged(self):
        await self.connect()
        with patch.object(server, "_model_options", return_value=[{"provider_id": "default", "model": "demo"}]):
            await self.input(model="demo", provider_id="default")
            await self.completed()
        selection = {"provider_id": "default", "model": "demo"}
        self.assertEqual(selection, self.conversations["conv_draft"]["model_selection"])
        self.assertEqual(selection, server._load_conversations()["conv_draft"]["model_selection"])
        self.assertNotIn("model_selection", self.conversations["default"])
        self.assertEqual("demo", self.calls[-1][1]["model"])

    async def test_missing_model_or_provider_never_silently_falls_back(self):
        await self.connect()
        original = {"provider_id": "removed", "model": "demo"}
        self.conversations["default"]["model_selection"] = original.copy()
        with patch.object(server, "_model_options", return_value=[{"provider_id": "default", "model": "demo"}]):
            await self.input(cid="default", model="demo", provider_id="removed")
            error = await self.socket.event("error")
            self.assertEqual("input_1", error["client_id"])
            self.assertIn("未自动替换", error["msg"])
            self.socket.send(type="conversation_model", conversation_id="default", provider_id="default", model="removed")
            error = await self.socket.event("error")
        self.assertEqual(original, self.conversations["default"]["model_selection"])
        self.assertEqual([], self.calls)
        self.assertNotIn("input_1", server._ACTIVE_CLIENT_INPUTS)
        self.socket.send(type="conversation_model", conversation_id="missing", model="demo")
        await self.socket.event("error")
        self.assertNotIn("missing", self.conversations)

    async def test_model_removed_after_input_acceptance_does_not_run_a_fallback(self):
        await self.connect()
        with patch.object(server, "_model_options", return_value=[{"provider_id": "default", "model": "removed"}]):
            await self.input(model="removed", provider_id="default")
            await self.socket.event("input_ack")
            error = await self.socket.event("error")
        self.assertIn("未自动替换", error["msg"])
        self.assertEqual([], self.calls)
        self.assertNotIn("input_1", server._ACTIVE_CLIENT_INPUTS)

    def test_titles_remain_short_and_greetings_have_names(self):
        self.assertEqual("你好", server._auto_conversation_title("你好"))
        self.assertEqual("整理报表", server._auto_conversation_title("您好，小A，整理报表"))
        self.assertEqual("详细介绍一下你的能力", server._auto_conversation_title("小A，详细介绍一下你的能力"))
        self.assertEqual("报告整理", server._auto_conversation_title(" 报告整理 \n"))
        self.assertLessEqual(len(server._auto_conversation_title("整理" * 30)), 19)


if __name__ == "__main__":
    unittest.main()
