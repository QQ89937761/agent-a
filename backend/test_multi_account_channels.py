from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend.api import server
from backend.modules.chat import feishu_module, weixin_clawbot


class WeixinMultiAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_sessions_are_independent_and_add_accounts(self):
        qr = MagicMock()
        qr.raise_for_status.return_value = None
        qr.json.return_value = {"qrcode": "qr-1", "qrcode_img_content": "https://qr/1"}
        config = {"accounts": {"bot-a": {
            "account_id": "bot-a", "bot_token": "token-a", "ilink_bot_id": "bot-a",
            "baseurl": "https://a", "ilink_user_id": "user-a", "allowed_user_ids": ["user-a"],
            "connected": True,
        }}}
        with patch.object(weixin_clawbot, "_CONFIG", config), patch.object(weixin_clawbot, "_CLIENT") as client:
            client.post = AsyncMock(return_value=qr)
            first = await weixin_clawbot.begin_login()
            qr.json.return_value = {"qrcode": "qr-2", "qrcode_img_content": "https://qr/2"}
            second = await weixin_clawbot.begin_login()
        self.assertNotEqual(first["login_id"], second["login_id"])
        self.assertIn(first["login_id"], weixin_clawbot._LOGINS)
        self.assertIn(second["login_id"], weixin_clawbot._LOGINS)
        self.assertEqual(["token-a"], client.post.await_args_list[0].kwargs["json"]["local_token_list"])

    async def test_old_state_migrates_with_backup_and_context(self):
        old = {
            "bot_token": "token", "ilink_bot_id": "bot", "baseurl": "https://ilink",
            "ilink_user_id": "account", "allowed_user_ids": ["user"],
            "get_updates_buf": "cursor", "last_user_id": "user",
            "last_context_token": "context", "seen_message_ids": ["m1"], "connected": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "weixin-clawbot.json"
            path.write_text(json.dumps(old), encoding="utf-8")
            with patch.object(weixin_clawbot, "start_listener", new=AsyncMock()), patch.object(weixin_clawbot, "stop_listener", new=AsyncMock()):
                await weixin_clawbot.initialize(path, AsyncMock())
            saved = json.loads(path.read_text(encoding="utf-8"))
            account = saved["accounts"]["bot"]
            self.assertEqual("cursor", account["get_updates_buf"])
            self.assertEqual("context", account["user_contexts"]["user"])
            self.assertTrue(path.with_suffix(path.suffix + ".bak").is_file())


class ChannelIsolationTests(unittest.TestCase):
    def test_weixin_accounts_have_distinct_session_keys(self):
        self.assertNotEqual(server._channel_session_key("weixin", "user", "account-a"), server._channel_session_key("weixin", "user", "account-b"))
        self.assertEqual(server._channel_session_key("feishu", "user"), ("feishu", "user"))

    def test_identities_have_independent_remote_control_permissions(self):
        with patch.object(server.channel_control, "identity_permission", side_effect=[(True, 0), (False, 0), (True, 0), (False, 0)]), \
             patch.object(weixin_clawbot, "_CONFIG", {"accounts": {"account-a": {}, "account-b": {}}}), \
             patch.object(server.CFG.feishu, "allowed_open_ids", ["user-a", "user-b"]):
            wx_token = server.CURRENT_CHANNEL.set(("weixin", "account-a", "user-a"))
            try:
                self.assertEqual((True, 0), server._channel_permission("weixin"))
            finally:
                server.CURRENT_CHANNEL.reset(wx_token)
            wx_token = server.CURRENT_CHANNEL.set(("weixin", "account-b", "user-b"))
            try:
                self.assertEqual((False, 0), server._channel_permission("weixin"))
            finally:
                server.CURRENT_CHANNEL.reset(wx_token)
            user_token = server._CHANNEL_USER_ID.set("user-a")
            try:
                self.assertEqual((True, 0), server._channel_permission("feishu"))
            finally:
                server._CHANNEL_USER_ID.reset(user_token)
            user_token = server._CHANNEL_USER_ID.set("user-b")
            try:
                self.assertEqual((False, 0), server._channel_permission("feishu"))
            finally:
                server._CHANNEL_USER_ID.reset(user_token)

    def test_explicit_identity_never_falls_back_to_global_permission(self):
        with patch.object(server, "_first_channel_identity", return_value=""), \
             patch.object(server.channel_control, "identity_permission", return_value=(False, 0)) as account_permission, \
             patch.object(server.channel_control, "permission", side_effect=AssertionError("global permission used")):
            self.assertEqual((False, 0), server._channel_permission("weixin", "account-a"))
            account_permission.assert_called_once_with("weixin", "account-a", "")

    def test_stopping_one_feishu_identity_does_not_stop_another(self):
        previous = dict(server.CHANNEL_ACTIVE_TASKS)
        try:
            server.CHANNEL_ACTIVE_TASKS.clear()
            task_a = {"remote_control": True, "channel_identity": "user-a"}
            task_b = {"remote_control": True, "channel_identity": "user-b"}
            server.CHANNEL_ACTIVE_TASKS.update({
                ("feishu", "chat-a"): task_a,
                ("feishu", "chat-b"): task_b,
            })
            server._stop_channel_control("feishu", "user-a")
            self.assertTrue(task_a["stop"])
            self.assertTrue(task_a["control_revoked"])
            self.assertNotIn("stop", task_b)
            self.assertNotIn("control_revoked", task_b)
        finally:
            server.CHANNEL_ACTIVE_TASKS.clear()
            server.CHANNEL_ACTIVE_TASKS.update(previous)


class FeishuPendingUsersTests(unittest.TestCase):
    def test_unauthorized_user_is_directed_to_authorization_button(self):
        cfg = SimpleNamespace(feishu=SimpleNamespace(app_id="app", allowed_open_ids=["ou_existing"]))
        data = SimpleNamespace(event=SimpleNamespace(
            message=SimpleNamespace(chat_id="chat", chat_type="p2p"),
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_new")),
        ))
        on_message = MagicMock()
        with patch.object(feishu_module, "_cfg_ref", cfg), patch.object(feishu_module, "_seen_msg_ids", None), patch.object(feishu_module, "_save_pending_open_id") as save_pending, patch.object(feishu_module, "_notify", new=MagicMock()) as notify, patch.object(feishu_module, "_spawn") as spawn:
            feishu_module.handle_event(data, on_message, None)
        save_pending.assert_called_once_with("ou_new")
        notify.assert_called_once()
        text = notify.call_args.args[1]
        self.assertIn("授权此用户", text)
        self.assertIn("管理员", text)
        self.assertNotIn("加入白名单", text)
        self.assertNotIn("ou_new", text)
        on_message.assert_not_called()
        spawn.assert_called_once()

    def test_multiple_pending_users_are_kept(self):
        cfg = SimpleNamespace(feishu=SimpleNamespace(app_id="app", allowed_open_ids=[]))
        with tempfile.TemporaryDirectory() as tmp, patch.object(feishu_module, "app_home", return_value=Path(tmp)), patch.object(feishu_module, "_cfg_ref", cfg):
            feishu_module._save_pending_open_id("ou_one")
            feishu_module._save_pending_open_id("ou_two")
            self.assertEqual({"ou_one", "ou_two"}, set(feishu_module.pending_open_ids(cfg)))


if __name__ == "__main__":
    unittest.main()
