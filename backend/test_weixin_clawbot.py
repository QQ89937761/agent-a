from __future__ import annotations

import asyncio
import base64
import httpx
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from backend.modules.chat import weixin_clawbot as weixin


class WeixinClawbotTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_remark_is_saved_by_account_id(self):
        config = {"accounts": {"bot-a": {"account_id": "bot-a", "connected": True, "bot_token": "token"}}}
        with patch.object(weixin, "_CONFIG", config), patch.object(weixin, "_save") as save:
            result = weixin.set_account_remark("bot-a", "张三的工作号")
        self.assertEqual("张三的工作号", config["accounts"]["bot-a"]["remark"])
        self.assertEqual("张三的工作号", result["accounts"][0]["remark"])
        save.assert_called_once_with()

    async def test_cloud_remark_survives_reload_and_next_listener_save(self):
        from deploy.update_weixin import read_status, update

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "weixin-clawbot.json"
            accounts = {
                "bot-a": {"bot_token": "token-a", "connected": True, "allowed_user_ids": ["user-a"], "remark": "旧备注"},
                "bot-b": {"bot_token": "token-b", "connected": True, "allowed_user_ids": ["user-b"], "remark": "另一账号"},
            }
            path.write_text(json.dumps({"accounts": accounts}), encoding="utf-8")
            with patch.object(weixin, "_CONFIG", {}), patch.object(weixin, "_STATE_PATH", path), \
                 patch.object(weixin, "_CALLBACK", None), patch.object(weixin, "_ACCOUNTS_CHANGED", None), \
                 patch.object(weixin, "_SEEN", {}), patch.object(weixin, "_ACCOUNT_SEEN", {}), \
                 patch.object(weixin, "stop_listener", new=AsyncMock()), \
                 patch.object(weixin, "start_listener", new=AsyncMock()):
                for remark in ("新备注", ""):
                    update(path, {"account_id": "bot-a", "remark": remark})
                    await weixin.initialize(path, AsyncMock())
                    weixin._CONFIG["accounts"]["bot-a"]["get_updates_buf"] = "next-cursor"
                    weixin._save()
                    refreshed = {item["account_id"]: item for item in read_status(path)["accounts"]}
                    self.assertEqual(remark, refreshed["bot-a"]["remark"])
                    self.assertEqual("另一账号", refreshed["bot-b"]["remark"])
                    saved = json.loads(path.read_text(encoding="utf-8"))["accounts"]
                    self.assertEqual("token-a", saved["bot-a"]["bot_token"])
                    self.assertEqual(["user-a"], saved["bot-a"]["allowed_user_ids"])
                    self.assertEqual("next-cursor", saved["bot-a"]["get_updates_buf"])

    async def test_local_confirmed_login_authorizes_scanned_account_and_keeps_existing_users(self):
        response = MagicMock()
        response.json.return_value = {
            "status": "confirmed", "bot_token": "token", "ilink_bot_id": "bot",
            "baseurl": "https://ilink.example", "ilink_user_id": "scanned-user",
        }
        response.raise_for_status.return_value = None
        config = {"allowed_user_ids": ["existing-user"]}
        with patch.object(weixin, "_LOGINS", {"local": {"qrcode": "qr"}}), \
             patch.object(weixin, "_CONFIG", config), patch.object(weixin, "_CLIENT") as client, \
             patch.object(weixin, "_save") as save, \
             patch.object(weixin, "start_listener", new=AsyncMock()):
            client.get = AsyncMock(return_value=response)
            result = await weixin.login_status()

        self.assertTrue(result["connected"])
        self.assertEqual(["existing-user", "scanned-user"], config["allowed_user_ids"])
        save.assert_called_once_with()

    async def test_cloud_confirmed_credentials_are_cached_until_complete(self):
        response = MagicMock()
        response.json.return_value = {
            "status": "confirmed", "bot_token": "token", "ilink_bot_id": "bot",
            "baseurl": "https://ilink.example", "ilink_user_id": "user",
        }
        response.raise_for_status.return_value = None
        client_get = AsyncMock(return_value=response)
        with patch.object(weixin, "_LOGINS", {"cloud": {"qrcode": "qr", "redirect_host": ""}}), \
             patch.object(weixin, "_CLIENT") as client:
            client.get = client_get
            first = await weixin.cloud_login_status()
            second = await weixin.cloud_login_status()

            self.assertTrue(first["connected"])
            self.assertEqual(first["_credentials"], second["_credentials"])
            client_get.assert_awaited_once()
            weixin.complete_cloud_login()
            self.assertEqual("not_started", (await weixin.cloud_login_status())["login_status"])

    async def test_logout_clears_local_and_cloud_login_sessions(self):
        config = {"connected": True, "bot_token": "secret", "allowed_user_ids": ["u1"]}
        logins = {"local": {"qrcode": "local"}, "cloud": {"qrcode": "cloud"}}
        with patch.object(weixin, "_CONFIG", config), \
             patch.object(weixin, "_LOGINS", logins), \
             patch.object(weixin, "stop_listener", new=AsyncMock()), \
             patch.object(weixin, "_save"):
            result = await weixin.logout()

        self.assertFalse(result["connected"])
        self.assertEqual({}, logins)
        self.assertEqual([], result["allowed_user_ids"])

    async def test_confirmed_login_without_required_credentials_is_rejected(self):
        response = MagicMock()
        response.json.return_value = {"status": "confirmed", "bot_token": "token"}
        response.raise_for_status.return_value = None
        with patch.object(weixin, "_LOGINS", {"cloud": {"qrcode": "qr"}}), \
             patch.object(weixin, "_CLIENT") as client:
            client.get = AsyncMock(return_value=response)
            result = await weixin.cloud_login_status()
        self.assertFalse(result["connected"])
        self.assertEqual("error", result["login_status"])
        self.assertTrue(result["expired"])

    async def test_callback_failure_sends_receipt_then_error_in_context(self):
        payload = {"get_updates_buf": "new", "msgs": [{
            "message_id": "m1", "message_type": 1, "message_state": 0,
            "from_user_id": "u1", "context_token": "ctx",
            "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
        }]}
        config = {"connected": True, "get_updates_buf": "old", "allowed_user_ids": ["u1"]}
        callback_started = asyncio.Event()

        async def callback(*_args):
            callback_started.set()
            raise RuntimeError("callback failed")

        send_message = AsyncMock()
        dispatch_tasks = set()
        seen = {}

        async def post(_path, _body):
            if not hasattr(post, "called"):
                post.called = True
                return payload
            await asyncio.sleep(0)
            raise asyncio.CancelledError

        with patch.object(weixin, "_CONFIG", config), patch.object(weixin, "_SEEN", seen), \
             patch.object(weixin, "_DISPATCH_TASKS", dispatch_tasks), \
             patch.object(weixin, "_CALLBACK", callback), patch.object(weixin, "_save"), \
             patch.object(weixin, "send_message", new=send_message), \
             patch.object(weixin, "_post", new=post):
            with self.assertRaises(asyncio.CancelledError):
                await weixin._poll()
        await asyncio.wait_for(callback_started.wait(), 1)
        if dispatch_tasks:
            await asyncio.gather(*tuple(dispatch_tasks), return_exceptions=True)
        self.assertEqual("new", config["get_updates_buf"])
        self.assertIn("m1", seen)
        self.assertEqual([
            unittest.mock.call(
                "u1",
                "收到，正在处理。",
                "ctx",
            ),
            unittest.mock.call("u1", "处理微信消息失败：callback failed", "ctx"),
        ], send_message.await_args_list)

    async def test_cursor_advances_after_callback_succeeds(self):
        payload = {"get_updates_buf": "new", "msgs": [{
            "message_id": "m1", "message_type": 1, "message_state": 0,
            "from_user_id": "u1", "context_token": "ctx",
            "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
        }]}
        config = {"connected": True, "get_updates_buf": "old", "allowed_user_ids": ["u1"]}
        callback_started = asyncio.Event()

        async def callback(*_args):
            callback_started.set()

        seen = {}
        dispatch_tasks = set()
        send_message = AsyncMock()

        async def post(_path, _body):
            if not hasattr(post, "called"):
                post.called = True
                return payload
            await asyncio.sleep(0)
            raise asyncio.CancelledError

        with patch.object(weixin, "_CONFIG", config), patch.object(weixin, "_SEEN", seen), \
             patch.object(weixin, "_DISPATCH_TASKS", dispatch_tasks), \
             patch.object(weixin, "_CALLBACK", callback), patch.object(weixin, "_save"), \
             patch.object(weixin, "send_message", new=send_message), \
             patch.object(weixin, "_post", new=post):
            with self.assertRaises(asyncio.CancelledError):
                await weixin._poll()
        await asyncio.wait_for(callback_started.wait(), 1)
        if dispatch_tasks:
            await asyncio.gather(*tuple(dispatch_tasks), return_exceptions=True)
        self.assertIn("m1", seen)
        self.assertEqual("new", config["get_updates_buf"])
        send_message.assert_awaited_once_with(
            "u1",
            "收到，正在处理。",
            "ctx",
        )

    async def test_inbound_message_with_state_two_is_not_dropped(self):
        payload = {"get_updates_buf": "new", "msgs": [{
            "message_id": "m1", "message_type": 1, "message_state": 2,
            "from_user_id": "u1", "context_token": "ctx",
            "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
        }]}
        config = {"connected": True, "get_updates_buf": "old", "allowed_user_ids": ["u1"]}
        called = asyncio.Event()
        dispatch_tasks = set()

        async def callback(*_args):
            called.set()

        async def post(_path, _body):
            if not hasattr(post, "called"):
                post.called = True
                return payload
            await asyncio.sleep(0)
            raise asyncio.CancelledError

        with patch.object(weixin, "_CONFIG", config), patch.object(weixin, "_SEEN", {}), \
             patch.object(weixin, "_DISPATCH_TASKS", dispatch_tasks), \
             patch.object(weixin, "_CALLBACK", callback), patch.object(weixin, "_save"), \
             patch.object(weixin, "send_message", new=AsyncMock()), \
             patch.object(weixin, "_post", new=post):
            with self.assertRaises(asyncio.CancelledError):
                await weixin._poll()
        await asyncio.wait_for(called.wait(), 1)
        if dispatch_tasks:
            await asyncio.gather(*tuple(dispatch_tasks), return_exceptions=True)

    def test_qr_and_media_key_normalization(self):
        raw_key = bytes(range(16))
        qr_image = weixin._qr_image_content("https://ilinkai.weixin.qq.com/login?ticket=abc")
        self.assertTrue(qr_image.startswith("data:image/png;base64,"))
        self.assertTrue(
            base64.b64decode(qr_image.split(",", 1)[1]).startswith(b"\x89PNG\r\n\x1a\n")
        )
        png_content = base64.b64encode(b"\x89PNG\r\n\x1a\nimage-data").decode()
        self.assertEqual(
            f"data:image/png;base64,{png_content}", weixin._qr_image_content(png_content)
        )
        self.assertEqual(raw_key, weixin._media_key(raw_key))
        self.assertEqual(raw_key, weixin._media_key(raw_key.hex()))
        self.assertEqual(raw_key, weixin._media_key(base64.b64encode(raw_key).decode()))
        self.assertEqual(
            raw_key,
            weixin._media_key(base64.b64encode(raw_key.hex().encode()).decode()),
        )

    async def test_text_reply_keeps_context_token(self):
        sent = {}

        async def post(_path, body):
            sent.update(body)
            return {}

        with patch.object(weixin, "_post", new=post):
            await weixin.send_message("u1", "hello", "ctx-1")
        self.assertEqual("u1", sent["msg"]["to_user_id"])
        self.assertEqual("ctx-1", sent["msg"]["context_token"])
        self.assertEqual("hello", sent["msg"]["item_list"][0]["text_item"]["text"])

    async def test_image_payload_uses_minimal_media_fields(self):
        sent = {}

        async def upload(_raw, _user_id, _media_type):
            return {
                "encrypt_query_param": "query",
                "aes_key": "key",
                "encrypt_type": 1,
                "cipher_size": 12,
                "raw_size": 12,
            }

        async def post(_path, body):
            sent.update(body)
            return {}

        with patch.object(weixin, "_upload_media", new=upload), \
             patch.object(weixin, "_post", new=post):
            await weixin.send_image("u1", b"image-bytes", "ctx-1")

        image = sent["msg"]["item_list"][0]["image_item"]
        self.assertEqual({"encrypt_query_param", "aes_key", "encrypt_type"},
                         set(image["media"]))
        self.assertNotIn("aeskey", image)
        self.assertEqual(12, image["mid_size"])

    async def test_file_payload_uses_string_length_and_filename(self):
        sent = {}

        async def upload(_raw, _user_id, _media_type):
            return {
                "encrypt_query_param": "query",
                "aes_key": "key",
                "encrypt_type": 1,
                "raw_size": 4,
            }

        async def post(_path, body):
            sent.update(body)
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "报告.pdf"
            path.write_bytes(b"data")
            with patch.object(weixin, "_upload_media", new=upload), \
                 patch.object(weixin, "_post", new=post):
                await weixin.send_file("u1", str(path), "ctx-1")

        file_item = sent["msg"]["item_list"][0]["file_item"]
        self.assertEqual("报告.pdf", file_item["file_name"])
        self.assertEqual("4", file_item["len"])
        self.assertNotIn("md5", file_item)

    async def test_file_payload_keeps_explicit_original_filename(self):
        sent = {}

        async def upload(_raw, _user_id, _media_type):
            return {
                "encrypt_query_param": "query",
                "aes_key": "key",
                "encrypt_type": 1,
                "raw_size": 4,
            }

        async def post(_path, body):
            sent.update(body)
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "xiaoa-chat-abcd.pdf"
            path.write_bytes(b"data")
            with patch.object(weixin, "_upload_media", new=upload), \
                 patch.object(weixin, "_post", new=post):
                await weixin.send_file(
                    "u1", str(path), "ctx-1", file_name="桌面报告.pdf",
                )

        file_item = sent["msg"]["item_list"][0]["file_item"]
        self.assertEqual("桌面报告.pdf", file_item["file_name"])

    async def test_inbound_file_is_saved_and_exposed_to_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "weixin-clawbot.json"
            message = {
                "item_list": [{
                    "type": 4,
                    "file_item": {"file_name": "..\\..\\报告.pdf", "media": {"x": "y"}},
                }],
            }
            with patch.object(weixin, "_STATE_PATH", state_path), \
                 patch.object(weixin, "_download_media", new=AsyncMock(return_value=b"pdf")):
                text = await weixin._message_text(message)

            saved = list((Path(tmp) / "data" / "weixin-inbox").iterdir())
            self.assertEqual(1, len(saved))
            self.assertEqual(b"pdf", saved[0].read_bytes())
            self.assertIn("[微信文件]", text)
            self.assertIn("报告.pdf", text)
            self.assertIn(str(saved[0]), text)

    async def test_download_media_decrypts_nested_media_with_string_item_type(self):
        raw = b"\x89PNG\r\n\x1a\nimage-data"
        key = bytes(range(16))
        response = MagicMock()
        response.status_code = 200
        response.headers = {"content-type": "application/octet-stream"}
        response.content = weixin._aes_cipher(raw, key)
        response.raise_for_status.return_value = None
        with patch.object(weixin, "_CLIENT") as client:
            client.get = AsyncMock(return_value=response)
            decoded = await weixin._download_media({
                "type": "2",
                "image_item": {
                    "aeskey": key.hex(),
                    "media": {"encrypt_query_param": "query"},
                },
            })
        self.assertEqual(raw, decoded)

    async def test_download_timeout_reports_a_clear_media_error(self):
        for timeout in (httpx.ReadTimeout(""), asyncio.TimeoutError()):
            with self.subTest(timeout=type(timeout).__name__), patch.object(weixin, "_CLIENT") as client:
                client.get = AsyncMock(side_effect=timeout)
                with self.assertRaisesRegex(TimeoutError, "微信媒体下载超时"):
                    await weixin._download_media({
                        "type": 2,
                        "image_item": {"media": {"encrypt_query_param": "query"}},
                    })

    async def test_inbound_image_is_saved_and_described(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "weixin-clawbot.json"
            vision = AsyncMock(return_value="图中有一个圆")
            message = {
                "item_list": [{
                    "type": "2",
                    "image_item": {"caption": "请看看", "media": {"x": "y"}},
                }],
            }
            image = b"\x89PNG\r\n\x1a\nimage"
            with patch.object(weixin, "_STATE_PATH", state_path), \
                 patch.object(weixin, "_VISION_HOOK", vision), \
                 patch.object(weixin, "_download_media", new=AsyncMock(return_value=image)):
                text = await weixin._message_text(message)

            saved = list((Path(tmp) / "data" / "weixin-inbox").iterdir())
            self.assertEqual(1, len(saved))
            self.assertEqual(image, saved[0].read_bytes())
            vision.assert_awaited_once()
            self.assertEqual("png", vision.await_args.args[2])
            self.assertIn("[微信图片]", text)
            self.assertIn("图片内容:\n图中有一个圆", text)
            self.assertIn(str(saved[0]), text)

    async def test_vision_timeout_keeps_saved_image_and_continues_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "weixin-clawbot.json"
            vision = AsyncMock(side_effect=asyncio.TimeoutError())
            image = b"\x89PNG\r\n\x1a\nimage"
            message = {"item_list": [{
                "type": 2,
                "image_item": {"caption": "图中是什么", "media": {"x": "y"}},
            }]}
            with patch.object(weixin, "_STATE_PATH", state_path), \
                 patch.object(weixin, "_VISION_HOOK", vision), \
                 patch.object(weixin, "_download_media", new=AsyncMock(return_value=image)):
                text = await weixin._message_text(message)

            saved = list((Path(tmp) / "data" / "weixin-inbox").iterdir())
            self.assertEqual(1, len(saved))
            self.assertEqual(image, saved[0].read_bytes())
            self.assertIn("识图超时", text)
            self.assertIn(str(saved[0]), text)
            self.assertIn("图中是什么", text)

    def test_seen_message_cache_evicts_oldest_without_clearing_everything(self):
        seen = {}
        with patch.object(weixin, "_SEEN", seen), patch.object(weixin, "_CONFIG", {}), \
             patch.object(weixin, "_save"):
            for index in range(4097):
                weixin._remember_message(str(index))
        self.assertEqual(4096, len(seen))
        self.assertNotIn("0", seen)
        self.assertIn("4096", seen)


if __name__ == "__main__":
    unittest.main()
