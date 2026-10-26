"""Tests for independent local/cloud remote-control channel permissions."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.api import server
from backend.core import channel_control


class ChannelControlFileTests(unittest.TestCase):
    def test_missing_file_defaults_channels_and_new_identities_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                {"weixin": False, "feishu": False},
                channel_control.status(Path(tmp)),
            )
            self.assertEqual(
                {"a": False, "b": False},
                channel_control.ensure_identities("weixin", ["a", "b"], "a", Path(tmp)),
            )

    def test_legacy_channel_state_never_grants_account_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            channel_control.update("weixin", True, home)
            self.assertEqual((False, 0), channel_control.identity_permission("weixin", "a", "a", home))
            self.assertEqual(
                {"a": False, "b": False},
                channel_control.ensure_identities("weixin", ["a", "b"], "a", home),
            )
            self.assertEqual((False, 0), channel_control.identity_permission("weixin", "a", "a", home))
            self.assertEqual((False, 0), channel_control.identity_permission("weixin", "b", "a", home))
            channel_control.update_account("weixin", "a", True, home)
            channel_control.ensure_identities("weixin", ["a", "b", "new"], "a", home)
            self.assertEqual((True, 0), channel_control.identity_permission("weixin", "a", "a", home))
            self.assertEqual((False, 0), channel_control.identity_permission("weixin", "new", "a", home))
            channel_control.update_account("weixin", "a", False, home)
            self.assertEqual((False, 1), channel_control.identity_permission("weixin", "a", "a", home))

    def test_channels_are_independent_and_disable_revokes_old_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual({"weixin": False, "feishu": False},
                             channel_control.update("weixin", False, home))
            self.assertEqual({"weixin": False, "feishu": True},
                             channel_control.update("feishu", True, home))
            self.assertEqual((False, 0), channel_control.permission("weixin", home))
            self.assertEqual((True, 0), channel_control.permission("feishu", home))
            channel_control.update("weixin", True, home)
            self.assertEqual((True, 0), channel_control.permission("weixin", home))
            channel_control.update("feishu", False, home)
            self.assertEqual((True, 0), channel_control.permission("weixin", home))
            self.assertEqual((False, 1), channel_control.permission("feishu", home))

    def test_each_feishu_identity_has_an_independent_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            channel_control.ensure_identities("feishu", ["user-a", "user-b"], "user-a", home)
            channel_control.update_account("feishu", "user-a", True, home)
            self.assertEqual((True, 0), channel_control.identity_permission("feishu", "user-a", "user-a", home))
            self.assertEqual((False, 0), channel_control.identity_permission("feishu", "user-b", "user-a", home))
            channel_control.update_account("feishu", "user-b", True, home)
            channel_control.update_account("feishu", "user-a", False, home)
            self.assertEqual((False, 1), channel_control.identity_permission("feishu", "user-a", "user-a", home))
            self.assertEqual((True, 0), channel_control.identity_permission("feishu", "user-b", "user-a", home))

    def test_corrupt_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "channel-control.json"
            path.parent.mkdir()
            path.write_text("not-json", encoding="utf-8")
            self.assertEqual((False, -1), channel_control.permission("weixin", Path(tmp)))
            with self.assertRaises(ValueError):
                channel_control.update("weixin", True, Path(tmp))


class ChannelChatOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.key = ("weixin", "chat-only-test")
        server.CHANNEL_LOCKS.pop(self.key, None)
        server.CHANNEL_HISTORY.pop(self.key, None)
        server.CHANNEL_ACTIVE_TASKS.pop(self.key, None)

    async def asyncTearDown(self):
        server.CHANNEL_LOCKS.pop(self.key, None)
        server.CHANNEL_HISTORY.pop(self.key, None)
        server.CHANNEL_ACTIVE_TASKS.pop(self.key, None)

    async def test_legacy_enabled_channel_with_no_account_grant_uses_chat_only(self):
        self.key = ("weixin", "account-a", "chat-only-test")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            channel_control.update("weixin", True, home)
            llm = type("ChatOnlyLLM", (), {"chat": AsyncMock(return_value={"content": "chat only"})})()
            with (
                patch.object(channel_control, "app_home", return_value=home),
                patch.object(server, "_first_channel_identity", return_value="account-a"),
                patch.object(server, "LLM", llm),
                patch.object(server, "Agent", side_effect=AssertionError("OFF account executed tools")),
                patch.object(server, "_channel_send", new=AsyncMock()),
            ):
                await server.handle_channel_command("weixin", self.key[-1], "screenshot", account_id="account-a")
            self.assertIsNone(llm.chat.await_args.kwargs["tools"])

    async def test_disabled_channel_uses_chat_without_agent_tools(self):
        class ChatOnlyLLM:
            def __init__(self):
                self.kwargs = None

            async def chat(self, messages, **kwargs):
                self.kwargs = kwargs
                return {"content": "这是聊天回复"}

        llm = ChatOnlyLLM()
        sent = AsyncMock()
        with (
            patch.object(server, "LLM", llm),
            patch.object(server.channel_control, "permission", return_value=(False, 4)),
            patch.object(server, "Agent", side_effect=AssertionError("disabled channel created Agent")),
            patch.object(server, "_channel_send", new=sent),
        ):
            await server.handle_channel_command("weixin", self.key[1], "请解释一下")

        self.assertIsNotNone(llm.kwargs)
        self.assertIsNone(llm.kwargs["tools"])
        sent.assert_awaited_once()
        self.assertIn("这是聊天回复", sent.await_args.args[2])


if __name__ == "__main__":
    unittest.main()
