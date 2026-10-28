"""Focused checks for settings sections, tool catalog, and audit integration."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any, get_type_hints

from fastapi import HTTPException, WebSocketDisconnect

from backend import __version__
from backend.api import server
from backend.core.connection_string import make_connection_string
from backend.core.config import Config, load_config
from backend.core import isolation_workspace
from backend.core.tools import Tool, ToolRegistry
from backend.modules.computer.operation_audit import OperationAuditLog


def _isolate_desktop_history(test):
    # Persisted delivery deduplication must never inspect or mutate the developer's real chats.
    root = Path(test.enterContext(tempfile.TemporaryDirectory(prefix="xiaoa-server-test-")))
    history = []
    test.enterContext(patch.object(server, "DESKTOP_HISTORY", history))
    test.enterContext(patch.object(server, "DESKTOP_CONVERSATIONS", {
        "default": server._new_conversation("default", messages=history),
    }))
    test.enterContext(patch.object(server, "_CONVERSATIONS_PATH", root / "conversations.json"))
    test.enterContext(patch.object(server, "_ATTACHMENT_DIR", root / "uploads"))
    # Ordinary conversation tests use an initialized empty catalog, never the
    # developer's real cloud management configuration or route cache.
    test.enterContext(patch.object(server, "_SHARED_ROUTE_HOME", server.app_home()))
    test.enterContext(patch.object(server, "_SHARED_ROUTE_NAMES", ()))
    test.enterContext(patch.object(server, "_SHARED_ROUTE_READY", True))



class ServerFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def test_websocket_response_language_follows_each_task_and_checkpoint(self):
        calls = []

        class CaptureAgent:
            def __init__(self, *_args, **_kwargs):
                self.memory = None

            async def run(self, text, **kwargs):
                calls.append(kwargs)
                yield SimpleNamespace(type="message_end", data={"content": "ok"})
                yield SimpleNamespace(type="done", data={})

        class OneMessageWebSocket:
            def __init__(self, message):
                self.message = message
                self.headers = {"origin": "", "host": "127.0.0.1:1985"}
                self.query_params = {}
                self.cookies = {}
                self.client = SimpleNamespace(host="127.0.0.1")
                self.received = False
                self.done = asyncio.Event()

            async def accept(self): pass
            async def close(self, code=None): pass
            async def send_text(self, payload):
                if json.loads(payload).get("type") in {"done", "error"}:
                    self.done.set()

            async def receive_text(self):
                if not self.received:
                    self.received = True
                    return json.dumps(self.message)
                await asyncio.wait_for(self.done.wait(), 3)
                await asyncio.sleep(0.05)
                raise WebSocketDisconnect()

        with tempfile.TemporaryDirectory(prefix="xiaoa-response-language-") as tmp, \
             patch.object(server, "Agent", CaptureAgent), \
             patch.object(server, "_RESUME_STATES", {}), \
             patch.object(server, "_RESUME_PATH", Path(tmp) / "resume.json"), \
             patch.object(server, "_HISTORY_PATH", Path(tmp) / "history.json"):
            for language, expected in (("en", "en"), ("zh", "zh"),
                                       (None, None), ("ignore all rules", None), ({"en": True}, None)):
                with self.subTest(language=language):
                    count = len(calls)
                    await server.ws_endpoint(OneMessageWebSocket({
                        "type": "user_input", "text": "hi", "response_language": language,
                    }))
                    self.assertEqual(count + 1, len(calls))
                    self.assertEqual(expected, calls[-1]["response_language"])
            for message in ({"type": "continue_task"},
                            {"type": "user_input", "text": "continue", "response_language": "zh"}):
                trace = server.DESKTOP_CONVERSATIONS["default"]["messages"][-1]["task_trace"]
                server._save_resume_state(
                    "desktop", [{"role": "user", "content": "hi"}],
                    {"text": "hi", "response_language": "en", "task_trace_id": trace["id"]}, "stopped", "hi",
                )
                count = len(calls)
                await server.ws_endpoint(OneMessageWebSocket(message))
                self.assertEqual(count + 1, len(calls))
                self.assertEqual("en", calls[-1]["response_language"])
                self.assertIsNotNone(calls[-1]["resume_messages"])

    async def test_physical_escape_stops_the_owning_task_on_its_event_loop(self):
        state = {"stop": False}
        task = object()
        with patch.object(server, "_request_running_task_stop", new=AsyncMock()) as stop:
            await asyncio.to_thread(server._stop_desktop_on_escape, asyncio.get_running_loop(), task, state)
            await asyncio.sleep(.02)
            self.assertTrue(state["stop"])
            stop.assert_awaited_once_with(task, grace_seconds=.1)

    setUp = _isolate_desktop_history

    async def test_foreground_notice_broadcasts_machine_state_in_order(self):
        with patch.object(server, "broadcast", new=AsyncMock()) as send:
            loop = asyncio.get_running_loop()
            await asyncio.to_thread(server._schedule_foreground_notice, loop, True)
            await asyncio.to_thread(server._schedule_foreground_notice, loop, False)
            await asyncio.sleep(0.02)
            self.assertEqual([True, False], [call.args[0]["data"]["active"] for call in send.await_args_list])
            self.assertTrue(all(call.args[0]["type"] == "foreground_control" for call in send.await_args_list))

    async def test_cloud_worker_desktop_control_also_notifies_local_frontend(self):
        async def execute(_tool, _args):
            token = await server.system_control.acquire_control(0.2)
            server.system_control.release_control(token)
            return {"ok": True}
        with patch.object(server.REGISTRY, "call", side_effect=execute), \
             patch.object(server.system_control, "ensure_listener"), \
             patch.object(server, "broadcast", new=AsyncMock()) as send:
            self.assertTrue((await server._run_worker_tool("focus_window", {}))["ok"])
            await asyncio.sleep(0.02)
            self.assertEqual([True, False], [call.args[0]["data"]["active"] for call in send.await_args_list])

    def test_worker_tool_type_hints_resolve(self):
        self.assertIs(Any, get_type_hints(server._run_worker_tool)["return"])

    def test_chat_file_path_replaces_stale_windows_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            desktop = home / "Desktop"
            desktop.mkdir()
            expected = desktop / "2609024.pdf"
            expected.write_bytes(b"pdf")
            with patch.object(server.Path, "home", return_value=home), \
                 patch.object(server.Path, "cwd", return_value=home):
                resolved = server._resolve_chat_file_path(
                    r"C:\Users\Administrator\Desktop\2609024.pdf"
                )
        self.assertEqual(expected.resolve(), resolved)

    def test_chat_file_path_supports_controlled_fuzzy_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            desktop = home / "Desktop"
            desktop.mkdir()
            expected = desktop / "报告-最终版.pdf"
            expected.write_bytes(b"pdf")
            with patch.object(server.Path, "home", return_value=home), \
                 patch.object(server.Path, "cwd", return_value=home):
                resolved = server._resolve_chat_file_path("桌面 报告最终")
        self.assertEqual(expected.resolve(), resolved)

    def test_chat_file_path_rejects_ambiguous_fuzzy_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            desktop = home / "Desktop"
            desktop.mkdir()
            (desktop / "报告-最终版.pdf").write_bytes(b"one")
            (desktop / "报告-最终版.docx").write_bytes(b"two")
            with patch.object(server.Path, "home", return_value=home), \
                 patch.object(server.Path, "cwd", return_value=home):
                with self.assertRaisesRegex(ValueError, "找到多个匹配文件，请回复要发送的文件名或完整路径") as error:
                    server._resolve_chat_file_path("桌面 报告最终")
        self.assertIn("报告-最终版.pdf", str(error.exception))
        self.assertIn("报告-最终版.docx", str(error.exception))
        self.assertIn("Desktop", str(error.exception))

    def test_chat_file_path_requires_confirmation_for_any_two_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            desktop = home / "Desktop"
            desktop.mkdir()
            (desktop / "报告最终.pdf").write_bytes(b"exact-name-match")
            (desktop / "报告-最终版.docx").write_bytes(b"normalized-match")
            with patch.object(server.Path, "home", return_value=home), \
                 patch.object(server.Path, "cwd", return_value=home):
                with self.assertRaisesRegex(ValueError, "找到多个匹配文件") as error:
                    server._resolve_chat_file_path("桌面 报告最终")
        self.assertIn("报告最终.pdf", str(error.exception))
        self.assertIn("报告-最终版.docx", str(error.exception))

    def test_chat_file_path_accepts_quoted_image_name_without_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            desktop = home / "Desktop"
            desktop.mkdir()
            expected = desktop / "07马.png"
            expected.write_bytes(b"png")
            with patch.object(server.Path, "home", return_value=home), \
                 patch.object(server.Path, "cwd", return_value=home):
                resolved = server._resolve_chat_file_path("桌面 “07马” 的图片")
        self.assertEqual(expected.resolve(), resolved)

    async def test_local_chat_can_attach_file_and_image_to_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_file = root / "报告.pdf"
            source_image = root / "图.png"
            source_file.write_bytes(b"pdf")
            source_image.write_bytes(b"image")
            registry = ToolRegistry()
            reply_attachments = []
            with patch.object(server, "_ATTACHMENT_DIR", root / "uploads"), \
                 patch.object(server, "CURRENT_CHANNEL") as current_channel, \
                 patch.object(server, "_DESKTOP_REPLY_ATTACHMENTS") as desktop_attachments:
                current_channel.get.return_value = None
                desktop_attachments.get.return_value = reply_attachments
                server.register_channel_tools(registry)

                file_result = await registry.get("send_file_to_chat").handler(path=str(source_file))
                image_result = await registry.get("send_image_to_chat").handler(path=str(source_image))

            self.assertIn("附加到当前小A回复", file_result)
            self.assertIn("附加到当前小A回复", image_result)
            self.assertEqual(["报告.pdf", "图.png"], [item["name"] for item in reply_attachments])
            self.assertTrue(all(Path(item["path"]).is_file() for item in reply_attachments))
            self.assertEqual("application/pdf", reply_attachments[0]["type"])
            self.assertEqual("image/png", reply_attachments[1]["type"])

    async def test_weixin_chat_media_tools_send_to_active_conversation(self):
        from backend.modules.chat import weixin_clawbot

        with tempfile.TemporaryDirectory() as tmp:
            source_file = Path(tmp) / "报告.pdf"
            source_image = Path(tmp) / "图.png"
            source_file.write_bytes(b"pdf")
            source_image.write_bytes(b"image")
            registry = ToolRegistry()
            with patch.object(server, "_CHANNEL_CONTEXT_TOKEN") as context_token, \
                 patch.object(server, "CURRENT_CHANNEL") as current_channel, \
                 patch.object(weixin_clawbot, "send_file", new=AsyncMock()) as send_file, \
                 patch.object(weixin_clawbot, "send_image", new=AsyncMock()) as send_image:
                context_token.get.return_value = "ctx-1"
                current_channel.get.return_value = ("weixin", "user-1")
                server.register_channel_tools(registry)
                file_result = await registry.get("send_file_to_chat").handler(path=str(source_file))
                image_result = await registry.get("send_image_to_chat").handler(path=str(source_image))

            self.assertIn("已把文件发到微信", file_result)
            self.assertIn("已把图片发到微信", image_result)
            send_file.assert_awaited_once_with("user-1", str(source_file.resolve()), "ctx-1")
            send_image.assert_awaited_once_with("user-1", source_image.read_bytes(), "ctx-1")

    async def test_worker_media_return_is_bounded_and_validates_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_file = root / "report.pdf"
            source_image = root / "image.png"
            source_file.write_bytes(b"pdf-data")
            source_image.write_bytes(b"\x89PNG\r\n\x1a\nimage-data")
            registry = ToolRegistry()
            server.register_channel_tools(registry)

            file_result = await registry.get("send_file_to_chat").handler(
                path=str(source_file), _return_to_hub=True,
            )
            image_result = await registry.get("send_image_to_chat").handler(
                path=str(source_image), _return_to_hub=True,
            )
            self.assertTrue(file_result["_xiaoa_hub_media"])
            self.assertEqual("file", file_result["kind"])
            self.assertEqual(b"pdf-data", base64.b64decode(file_result["data"]))
            self.assertEqual("image/png", image_result["mime"])
            self.assertEqual(b"\x89PNG\r\n\x1a\nimage-data", base64.b64decode(image_result["data"]))

            with self.assertRaisesRegex(ValueError, "不是有效的图片"):
                server._worker_media_payload(str(source_file), "image")

            too_large = root / "large.bin"
            too_large.write_bytes(b"x" * (server._MAX_CHAT_MEDIA_TRANSFER_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "超过 20 MB"):
                server._worker_media_payload(str(too_large), "file")

    async def test_hub_delivers_worker_file_using_weixin_context_and_cleans_temp(self):
        from backend.modules.chat import weixin_clawbot

        with patch.object(server, "_CHANNEL_CONTEXT_TOKEN") as context_token, \
             patch.object(weixin_clawbot, "send_file", new=AsyncMock()) as send_file:
            context_token.get.return_value = "ctx-remote"

            async def inspect_file(_user_id, path, token, *, file_name=None):
                self.assertEqual("ctx-remote", token)
                self.assertEqual(b"document", Path(path).read_bytes())
                self.assertEqual("report.pdf", file_name)

            send_file.side_effect = inspect_file
            result = await server._deliver_worker_media({
                "_xiaoa_hub_media": True,
                "kind": "file",
                "name": "report.pdf",
                "mime": "application/pdf",
                "data": base64.b64encode(b"document").decode("ascii"),
            }, ("weixin", "user-1"), None)

        self.assertIn("已把文件发到weixin", result)
        send_file.assert_awaited_once()
        self.assertFalse(Path(send_file.await_args.args[1]).exists())

    async def test_logout_weixin_clears_local_and_cloud_binding(self):
        from backend.modules.chat import weixin_clawbot

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data" / "cloud-ssh").mkdir(parents=True)
            (root / "data" / "cloud-ssh" / "target.json").write_text(
                json.dumps({"host": "203.0.113.10", "username": "root"}), encoding="utf-8"
            )
            local = {"connected": False, "ilink_user_id": "", "allowed_user_ids": []}
            with patch.object(server, "app_home", return_value=root), \
                 patch.object(weixin_clawbot, "logout", new=AsyncMock(return_value=local)), \
                 patch("backend.core.cloud_deploy.update_weixin", new=MagicMock()) as update, \
                 patch("backend.core.cloud_deploy.get_weixin_status", new=MagicMock(return_value={
                     "connected": False, "account_id": "", "allowed_user_ids": [],
                 })):
                result = await server._logout_weixin()

        self.assertTrue(result["ok"])
        self.assertTrue(result["cloud"]["configured"])
        update.assert_called_once_with(root, {"logout": True})

    async def test_cloud_weixin_login_keeps_credentials_until_remote_write_succeeds(self):
        from backend.modules.chat import weixin_clawbot

        credentials = {"bot_token": "secret-token", "ilink_bot_id": "bot-id",
                       "baseurl": "https://ilink.example", "ilink_user_id": "user-id"}
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        with patch.object(weixin_clawbot, "cloud_login_status", AsyncMock(return_value={
                 "connected": True, "login_status": "connected", "_credentials": credentials,
             })), \
             patch("backend.core.cloud_deploy.get_weixin_status", return_value={"allowed_user_ids": ["existing-user"]}), \
             patch("backend.core.cloud_deploy.update_weixin", side_effect=RuntimeError("ssh unavailable")) as update, \
             patch.object(weixin_clawbot, "complete_cloud_login") as complete:
            result = await server.api_cloud_weixin_login_status(request)

        self.assertEqual("sync_pending", result["login_status"])
        self.assertEqual("secret-token", credentials["bot_token"])
        self.assertEqual(
            {**credentials, "allowed_user_ids": ["existing-user", "user-id"]},
            update.call_args.args[1],
        )
        complete.assert_not_called()

    def test_hub_does_not_import_local_cloud_ssh_tools(self):
        source = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn("if not CFG.role.is_hub:", source)
        self.assertIn("from backend.modules.cloud import cloud_module", source)

    def test_event_redaction_masks_known_secret_without_changing_other_text(self):
        old_key = server.CFG.llm.api_key
        try:
            server.CFG.llm.api_key = "test-password-not-a-real-secret"
            secret = server.CFG.llm.api_key
            cases = [
                "sshpass -p test-password-not-a-real-secret ssh root@host",
                'PASSWORD = "test-password-not-a-real-secret"',
                "test-password-not-a-real-secret",
                {"nested": ["test-password-not-a-real-secret", {"api_key": "test-password-not-a-real-secret"}]},
                "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc12345.xyz67890",
                "sk-proj-abcdefghijklmnop123456",
                "普通中文句子，不含密钥",
                r"D:\reports\monthly.xlsx",
                {"secret": secret, "path": "report.xlsx"},
                ["sshpass", "-p", secret, "--", "report.xlsx"],
            ]
            original = cases[-1].copy()
            redacted = [server._redact_event_data(item) for item in cases]
            for index in (0, 1, 2, 3, 4, 5, 8, 9):
                self.assertNotIn(secret, str(redacted[index]))
            self.assertEqual(cases[6], redacted[6])
            self.assertEqual(cases[7], redacted[7])
            self.assertEqual(original, cases[-1])
            self.assertIn("sshpass", redacted[0])
        finally:
            server.CFG.llm.api_key = old_key

    def test_event_redaction_preserves_media_fields_but_redacts_text(self):
        import base64
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (255, 0, 0)).save(buf, format="PNG")
        png_b64 = base64.b64encode(buf.getvalue()).decode()

        # ① screenshot 事件的 image 字段一字不改,且能解码回原图
        result = server._redact_event_data({"image": png_b64})
        self.assertEqual(png_b64, result["image"])
        self.assertEqual(buf.getvalue(), base64.b64decode(result["image"]))

        old_pw = server.CFG.auth.password
        try:
            server.CFG.auth.password = "testpw-2025-xyz"
            secret = server.CFG.auth.password

            # ② tool_result 的 _observation_image 保留、text 里的口令遮蔽
            result = server._redact_event_data({
                "_observation_image": png_b64,
                "text": f"sshpass -p {secret} ssh root@host",
            })
            self.assertEqual(png_b64, result["_observation_image"])
            self.assertNotIn(secret, result["text"])
            self.assertIn("***", result["text"])

            # ③ attachments[].data(data URL)保留、text 遮蔽
            data_url = "data:image/png;base64," + png_b64
            result = server._redact_event_data([
                {"name": "s.png", "data": data_url, "text": f"登录口令 {secret}"},
            ])
            self.assertEqual(data_url, result[0]["data"])
            self.assertNotIn(secret, result[0]["text"])
        finally:
            server.CFG.auth.password = old_pw

        # 落盘路(skip_media=False)仍把媒体遮蔽掉,不保留 base64
        result = server._redact_event_data({"image": png_b64}, skip_media=False)
        self.assertNotIn(png_b64, str(result["image"]))

    async def test_context_window_is_saved_and_loaded(self):
        class FakeClient:
            def __init__(self, _cfg):
                pass

            async def aclose(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.toml"
            cfg = Config()
            with patch.object(server, "CFG", cfg), \
                 patch.object(server, "LLM", FakeClient(cfg.llm)), \
                 patch.object(server, "LLMClient", FakeClient), \
                 patch.object(server, "config_path", return_value=target):
                result = await server.api_save_settings_section(
                    "models", server.SettingsRequest(context_window=65536)
                )

            self.assertTrue(result["ok"])
            self.assertEqual(65536, cfg.agent.context_window)
            self.assertEqual(65536, load_config(target).agent.context_window)

    async def test_cloud_browser_models_override_deploy_defaults_after_restart(self):
        from backend.core.config import apply_env_overrides, LLMProviderConfig, provider_runtime_env
        deployed = LLMProviderConfig(id="backup", name="Old backup", base_url="https://old.invalid/v1",
                                     models=["old-backup"], api_key="managed-backup")
        env = {
            **provider_runtime_env([deployed]), "AGENT_ROLE": "hub",
            "AGENT_LLM_BASE_URL": "https://old.invalid/v1", "AGENT_CHAT_MODEL": "old",
            "AGENT_MODEL_OPTIONS": "old", "AGENT_LLM_PROTOCOL": "openai",
            "AGENT_LLM_API_KEY": "managed-primary", "AGENT_LLM_PROVIDER_NAME": "Old",
            "AGENT_CONTEXT_WINDOW": "128000", "AGENT_LLM_CONTEXT_WINDOWS": '{"old":128000}',
            "AGENT_WEB_PASSWORD": "managed-web-password",
        }
        providers = [
            {"id": "default", "name": "New", "base_url": "https://new.invalid/v1",
             "protocol": "responses", "models": ["new"], "context_windows": {"new": 65536}},
            {"id": "backup", "name": "New backup", "base_url": "https://backup.invalid/v1",
             "models": ["new-backup"]},
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True):
            path = Path(tmp) / "config.toml"
            cfg = apply_env_overrides(Config())
            with patch.object(server, "CFG", cfg), patch.object(server, "config_path", return_value=path), \
                 patch.object(server, "LLM", object()), patch.object(server, "runtime_llm", return_value=object()), \
                 patch.object(server, "_retire_llm", new=AsyncMock()), patch.object(server, "broadcast", new=AsyncMock()), \
                 patch.object(server.isolation_workspace, "cleanup"):
                result = await server.api_save_settings_section("models", server.SettingsRequest(
                    providers=providers, context_window=65536))
                self.assertTrue(result["ok"], result)
                for section, values in (("voice", {"tts_voice": 1002}),
                                        ("memory", {"memory_backup_interval_minutes": 45}),
                                        ("system", {"isolation_max_mb": 2048})):
                    saved = await server.api_save_settings_section(section, server.SettingsRequest(**values))
                    self.assertTrue(saved["ok"], saved)
            restored = apply_env_overrides(load_config(path))
            self.assertEqual(1002, restored.voice.tts_voice)
            self.assertEqual(45, restored.memory.backup_interval_minutes)
            self.assertEqual(2048, restored.agent.isolation_max_mb)
            self.assertTrue(restored.llm.settings_saved)
            self.assertEqual("new", restored.llm.chat_model)
            self.assertEqual(["new"], restored.llm.models)
            self.assertEqual("New", restored.llm.provider_name)
            self.assertEqual("responses", restored.llm.protocol)
            self.assertEqual("https://new.invalid/v1", restored.llm.base_url)
            self.assertEqual({"new": 65536}, restored.llm.context_windows)
            self.assertEqual(65536, restored.agent.context_window)
            self.assertEqual(["new-backup"], restored.llm.providers[0].models)
            self.assertEqual("managed-primary", restored.llm.api_key)
            self.assertEqual("managed-backup", restored.llm.providers[0].api_key)
            self.assertEqual("managed-web-password", restored.auth.password)
            self.assertNotIn("managed-", path.read_text(encoding="utf-8"))

    async def test_failed_settings_write_restores_config_without_live_side_effects(self):
        import copy
        import errno
        for section, values in (
            ("models", {"chat_model": "new-model"}),
            ("voice", {"tts_voice": 1002}),
            ("system", {"web_password": "new-password"}),
        ):
            with self.subTest(section=section), tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
                path = Path(tmp) / "config.toml"
                cfg = Config()
                cfg.role.mode = "hub"
                cfg.auth.password = "old-password"
                previous = copy.deepcopy(cfg)
                live_llm_config = cfg.llm
                live_voice_config = cfg.voice
                path.write_text('[llm]\nchat_model = "old"\n', encoding="utf-8")
                original_file = path.read_bytes()
                sessions = {"existing": 9999999999}
                old_client = object()
                with patch.object(server, "CFG", cfg), patch.object(server, "config_path", return_value=path), \
                     patch.object(server, "LLM", old_client), patch.object(server, "_SESSIONS", sessions), \
                     patch.object(server, "runtime_llm") as create, patch.object(server, "_retire_llm", new=AsyncMock()) as retire, \
                     patch.object(server, "broadcast", new=AsyncMock()) as notify, \
                     patch.object(server.os, "replace", side_effect=OSError(errno.EBUSY, "Device or resource busy")):
                    result = await server.api_save_settings_section(section, server.SettingsRequest(**values))
                    self.assertFalse(result["ok"], result)
                    self.assertEqual(previous, cfg)
                    self.assertIs(old_client, server.LLM)
                    self.assertIs(live_llm_config, cfg.llm)
                    self.assertIs(live_voice_config, cfg.voice)
                    self.assertEqual(previous.llm, live_llm_config)
                    self.assertEqual({"existing": 9999999999}, sessions)
                    create.assert_not_called()
                    retire.assert_not_awaited()
                    notify.assert_not_awaited()
                self.assertEqual(original_file, path.read_bytes())
                self.assertEqual([path], list(Path(tmp).iterdir()), "failed writes must clean temporary files")

    async def test_default_provider_first_model_becomes_chat_default(self):
        class FakeClient:
            def __init__(self, _cfg):
                pass

            async def aclose(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.toml"
            cfg = Config()
            cfg.llm.chat_model = "old-model"
            cfg.llm.vision_model = "old-model"
            request = server.SettingsRequest(providers=[server.ProviderSettingsRequest(
                id="default", name="默认接口", base_url="https://model.example/v1",
                models=["first-model", "old-model"],
            )])
            with patch.object(server, "CFG", cfg), \
                 patch.object(server, "LLM", FakeClient(cfg.llm)), \
                 patch.object(server, "LLMClient", FakeClient), \
                 patch.object(server, "config_path", return_value=target):
                result = await server.api_save_settings(request)

            self.assertTrue(result["ok"])
            self.assertEqual("first-model", cfg.llm.chat_model)
            self.assertEqual("first-model", cfg.llm.vision_model)
            self.assertEqual(["first-model", "old-model"], cfg.llm.models)

    async def test_open_at_login_is_saved_and_loaded(self):
        class FakeClient:
            def __init__(self, _cfg):
                pass

            async def aclose(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.toml"
            cfg = Config()
            with patch.object(server, "CFG", cfg), \
                 patch.object(server, "LLM", FakeClient(cfg.llm)), \
                 patch.object(server, "LLMClient", FakeClient), \
                 patch.object(server, "config_path", return_value=target):
                result = await server.api_save_settings(
                    server.SettingsRequest(open_at_login=True)
                )

            self.assertTrue(result["ok"])
            self.assertTrue(cfg.agent.open_at_login)
            self.assertTrue(load_config(target).agent.open_at_login)

    async def test_isolation_limit_is_saved_and_clamped(self):
        class FakeClient:
            def __init__(self, _cfg):
                pass

            async def aclose(self):
                pass

        with tempfile.TemporaryDirectory(dir=str(Path.cwd())) as tmp:
            target = Path(tmp) / "config.toml"
            cfg = Config()
            with patch.object(server, "CFG", cfg), \
                 patch.object(server, "LLM", FakeClient(cfg.llm)), \
                 patch.object(server, "LLMClient", FakeClient), \
                 patch.object(server, "config_path", return_value=target), \
                 patch.object(server.isolation_workspace, "cleanup"):
                result = await server.api_save_settings(
                    server.SettingsRequest(isolation_max_mb=32)
                )

            self.assertTrue(result["ok"])
            self.assertEqual(128, cfg.agent.isolation_max_mb)
            self.assertEqual(128, load_config(target).agent.isolation_max_mb)
            self.assertEqual(10240, isolation_workspace.normalize_max_mb(20000))

    def test_isolation_cleanup_removes_old_files_first(self):
        with tempfile.TemporaryDirectory(dir=str(Path.cwd())) as tmp:
            root = Path(tmp) / "isolation"
            (root / "workspace").mkdir(parents=True)
            (root / "tmp").mkdir()
            old_file = root / "tmp" / "old.bin"
            new_file = root / "tmp" / "new.bin"
            old_file.write_bytes(b"o" * (768 * 1024))
            new_file.write_bytes(b"n" * (768 * 1024))
            now = time.time()
            os.utime(old_file, (now - 60, now - 60))
            os.utime(new_file, (now, now))

            with patch.object(isolation_workspace, "root", return_value=root), \
                 patch.object(isolation_workspace, "normalize_max_mb", return_value=1):
                result = isolation_workspace.cleanup(1)

            self.assertFalse(old_file.exists())
            self.assertTrue(new_file.exists())
            self.assertLessEqual(result["used_bytes"], 1024 * 1024)

    def test_parent_watch_is_disabled_without_electron_parent(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(server._parent_process_alive())

    def test_parent_watch_detects_missing_electron_process(self):
        with patch.dict(os.environ, {"XIAOA_PARENT_PID": "4294967294"}), \
             patch("backend.api.server.os.kill", side_effect=ProcessLookupError):
            self.assertFalse(server._parent_process_alive())

    def test_parent_watch_rejects_reused_pid_by_creation_time(self):
        with patch.dict(os.environ, {
            "XIAOA_PARENT_PID": "1234", "XIAOA_PARENT_START_TIME": "1000",
        }), patch("backend.api.server.os.kill"), \
             patch("backend.api.server._process_creation_time_ms", return_value=3000):
            self.assertFalse(server._parent_process_alive())

    def test_parent_watch_accepts_matching_process_creation_time(self):
        with patch.dict(os.environ, {
            "XIAOA_PARENT_PID": "1234", "XIAOA_PARENT_START_TIME": "1000",
        }), patch("backend.api.server.os.kill"), \
             patch("backend.api.server._process_creation_time_ms", return_value=1500):
            self.assertTrue(server._parent_process_alive())

    def test_windows_probe_does_not_call_os_kill(self):
        if os.name != "nt":
            return
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        try:
            creation = server._process_creation_time_ms(child.pid)
            self.assertIsNotNone(creation)
            with patch.dict(os.environ, {
                "XIAOA_PARENT_PID": str(child.pid), "XIAOA_PARENT_START_TIME": str(creation),
            }), patch.object(server.os, "kill", side_effect=AssertionError("probe must not kill")):
                self.assertTrue(server._parent_process_alive())
        finally:
            child.terminate()
            child.wait(timeout=5)

    async def test_parent_watch_terminates_orphan_backend(self):
        fake_uvicorn = SimpleNamespace(should_exit=False)
        with patch.object(server, "_parent_process_alive", return_value=False), \
             patch("backend.api.server.asyncio.sleep", new=AsyncMock()), \
             patch.object(server, "_UVICORN_SERVER", fake_uvicorn), \
             patch("backend.api.server.os.kill") as kill:
            await server._parent_process_watch()
        if os.name == "nt":
            self.assertTrue(fake_uvicorn.should_exit)
            kill.assert_not_called()
        else:
            kill.assert_called_once_with(os.getpid(), server.signal.SIGTERM)

    async def test_cloud_deploy_endpoint_is_local_and_does_not_store_password(self):
        local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        remote = SimpleNamespace(client=SimpleNamespace(host="203.0.113.9"))
        request = server.CloudDeployRequest(
            host="203.0.113.10", username="root", password="root-secret",
        )
        with self.assertRaises(HTTPException) as denied:
            await server.api_cloud_deploy(request, remote)
        self.assertEqual(403, denied.exception.status_code)

        started = asyncio.Event()

        async def fake_run(_job_id, item):
            item.password = ""
            started.set()

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(server, "app_home", return_value=Path(tmp)), \
             patch.object(server, "_CLOUD_DEPLOY_JOBS", {}), \
             patch.object(server, "_run_cloud_deploy", new=fake_run):
            result = await server.api_cloud_deploy(request, local)
            await asyncio.wait_for(started.wait(), timeout=1)
        self.assertTrue(result["ok"])
        self.assertEqual("", request.password)
        self.assertNotIn("root-secret", str(server._CLOUD_DEPLOY_JOBS))

    async def test_worker_bridge_stop_does_not_start_replacement(self):
        old_bridge = MagicMock()
        old_task = asyncio.create_task(asyncio.sleep(10))
        with patch.object(server, "WORKER_BRIDGE", old_bridge), \
             patch.object(server, "_WORKER_BRIDGE_TASK", old_task), \
             patch.object(server, "WorkerBridge") as bridge_type:
            await server._replace_worker_bridge(start=False)
        old_bridge.stop.assert_called_once_with()
        self.assertTrue(old_task.cancelled())
        bridge_type.assert_not_called()

    def test_cloud_model_context_environment_round_trips(self):
        from backend.core.config import apply_env_overrides
        from deploy.update_hub_env import update_environment
        values = {
            "AGENT_LLM_PROVIDER_NAME": "私有接口",
            "AGENT_LLM_CONTEXT_WINDOWS": json.dumps({"model-one": 340992}),
            "AGENT_CONTEXT_WINDOW": "340992",
        }
        updated = update_environment("AGENT_ROLE=hub\n", values)
        self.assertIn("AGENT_LLM_CONTEXT_WINDOWS=", updated)
        with patch.dict(os.environ, values):
            cfg = apply_env_overrides(Config())
        self.assertEqual(340992, cfg.agent.context_window)
        self.assertEqual({"model-one": 340992}, cfg.llm.context_windows)
        self.assertEqual("私有接口", cfg.llm.provider_name)
        for raw in ('{"model-one": 1}', '[]', '{broken'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                update_environment("", {"AGENT_LLM_CONTEXT_WINDOWS": raw})

    def test_hub_runtime_management_requires_session_and_same_origin(self):
        cfg = Config()
        cfg.auth.password = "test-password"
        sessions = {"valid": time.time() + 60, "expired": time.time() - 60}
        with patch.object(server, "CFG", cfg), patch.object(server, "_SESSIONS", sessions):
            for role in ("hub", "worker", "standalone"):
                cfg.role.mode = role
                for token in ("", "invalid", "expired", "valid"):
                    for origin in ("", "https://hub.example", "https://evil.example"):
                        with self.subTest(role=role, token=token, origin=origin):
                            request = SimpleNamespace(
                                client=SimpleNamespace(host="203.0.113.10"),
                                cookies={"xa_session": token}, headers={"origin": origin},
                                base_url="https://hub.example/",
                            )
                            self.assertEqual(
                                role == "hub" and token == "valid" and origin != "https://evil.example",
                                server._can_manage_runtime(request),
                            )
            local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
            self.assertTrue(server._can_manage_runtime(local))

    async def test_hub_web_can_read_weixin_and_start_qr_without_local_bypass(self):
        import httpx
        from backend.modules.chat import weixin_clawbot
        cfg = Config()
        cfg.role.mode = "hub"
        cfg.auth.password = "test-password"
        manager = MagicMock()
        manager.libraries.return_value = []
        account = {"account_id": "bot-one", "connected": True}
        with patch.object(server, "CFG", cfg), \
             patch.object(server, "_SESSIONS", {"valid": time.time() + 60}), \
             patch.object(server, "_weixin_memories", return_value=manager), \
             patch.object(weixin_clawbot, "status", return_value={"accounts": [account]}), \
             patch.object(weixin_clawbot, "_accounts", return_value={}), \
             patch("backend.modules.memory.shared_memory.get_shared_settings", return_value={}), \
             patch.object(weixin_clawbot, "begin_login", new=AsyncMock(return_value={
                 "image": "data:image/png;base64,qr", "login_id": "login-one",
             })) as begin:
            transport = httpx.ASGITransport(app=server.app, client=("203.0.113.10", 40000))
            async with httpx.AsyncClient(transport=transport, base_url="https://hub.example") as client:
                self.assertEqual(401, (await client.get("/api/weixin/status")).status_code)
                client.cookies.set("xa_session", "valid")
                response = await client.get("/api/weixin/status")
                self.assertEqual(200, response.status_code)
                self.assertEqual([account], response.json()["accounts"])
                response = await client.post("/api/weixin/login", headers={"origin": "https://evil.example"})
                self.assertEqual(403, response.status_code)
                begin.assert_not_awaited()
                response = await client.post("/api/weixin/login", headers={"origin": "https://hub.example"})
                self.assertTrue(response.json()["ok"])
                self.assertEqual("login-one", response.json()["login_id"])
                begin.assert_awaited_once()
                # Login does not grant the desktop-only emergency unlock or deployment permissions.
                self.assertEqual(403, (await client.post("/api/channel/unlock")).status_code)

    async def test_remote_settings_are_covered_by_web_password_guard(self):
        cfg = Config()
        cfg.auth.password = "web-secret"
        request = SimpleNamespace(
            client=SimpleNamespace(host="203.0.113.9"),
            url=SimpleNamespace(path="/api/settings"), cookies={}, headers={},
        )
        with patch.object(server, "CFG", cfg), patch.object(server, "_SESSIONS", {}):
            response = await server.auth_guard(request, AsyncMock())
        self.assertEqual(401, response.status_code)

    async def test_no_password_closes_remote_web_even_with_old_session(self):
        import httpx
        cfg = Config()
        cfg.role.mode = "worker"
        cfg.role.hub_token = "machine-secret"
        transport = httpx.ASGITransport(app=server.app, client=("203.0.113.10", 40000))
        with patch.object(server, "CFG", cfg), \
             patch.object(server, "_SESSIONS", {"old": time.time() + 60}):
            async with httpx.AsyncClient(transport=transport, base_url="https://hub.example",
                                         cookies={"xa_session": "old"}) as client:
                for path in ("/", "/settings", "/login", "/static/app.js", "/api/settings"):
                    with self.subTest(path=path):
                        response = await client.get(path)
                        self.assertEqual(403, response.status_code)
                        self.assertIn("远程网页访问已关闭", response.text)
                response = await client.post("/api/login", json={"password": "anything"})
                self.assertEqual(403, response.status_code)
                self.assertFalse(response.json()["ok"])
                self.assertNotIn("set-cookie", response.headers)
                response = await client.get("/health")
                self.assertEqual({"ok": True, "role": "worker"}, response.json())
                response = await server.api_login(server.LoginRequest(), SimpleNamespace(
                    client=SimpleNamespace(host="203.0.113.10")))
                self.assertEqual(403, response.status_code)

    async def test_no_password_preserves_local_ui_and_machine_token_routes(self):
        cfg = Config()
        cfg.role.hub_token = "machine-secret"
        with patch.object(server, "CFG", cfg):
            for host, path, headers, allowed in (
                ("127.0.0.1", "/settings", {}, True),
                ("::1", "/api/settings", {}, True),
                ("203.0.113.10", "/api/weixin/memory/shared", {"x-hub-token": "machine-secret"}, True),
                ("203.0.113.10", "/api/weixin/memory/shared", {"x-hub-token": "wrong"}, False),
                ("203.0.113.10", "/api/settings", {"x-hub-token": "machine-secret"}, False),
            ):
                with self.subTest(host=host, path=path, headers=headers):
                    next_handler = AsyncMock(return_value="allowed")
                    request = SimpleNamespace(client=SimpleNamespace(host=host),
                        url=SimpleNamespace(path=path), headers=headers, cookies={})
                    result = await server.auth_guard(request, next_handler)
                    self.assertEqual(allowed, next_handler.await_count == 1)
                    if not allowed:
                        self.assertEqual(403, result.status_code)
            result = await server.api_login(server.LoginRequest(), SimpleNamespace(
                client=SimpleNamespace(host="127.0.0.1")))
            self.assertTrue(result["ok"])

    async def test_remote_chat_websocket_requires_password_and_session(self):
        for password, token in (("", ""), ("", "old"), ("set", ""), ("set", "unknown")):
            with self.subTest(password=password, token=token):
                cfg = Config()
                cfg.auth.password = password
                ws = SimpleNamespace(headers={"origin": "https://hub.example", "host": "hub.example"},
                    query_params={}, cookies={"xa_session": token},
                    client=SimpleNamespace(host="203.0.113.10"), close=AsyncMock(), accept=AsyncMock())
                with patch.object(server, "CFG", cfg), \
                     patch.object(server, "_SESSIONS", {"old": time.time() + 60}):
                    await server.ws_endpoint(ws)
                ws.close.assert_awaited_once_with(code=4001)
                ws.accept.assert_not_awaited()

    async def test_changing_password_disconnects_remote_chat_only(self):
        cfg = Config()
        cfg.auth.password = "old"
        remote = MagicMock(client=SimpleNamespace(host="203.0.113.10"), close=AsyncMock())
        local = MagicMock(client=SimpleNamespace(host="127.0.0.1"), close=AsyncMock())
        with patch.dict(os.environ, {"AGENT_WEB_PASSWORD": ""}), \
             patch.object(server, "CFG", cfg), \
             patch.object(server, "_SESSIONS", {"old": time.time() + 60}), \
             patch.object(server, "ACTIVE_WS", {local, remote}), \
             patch.object(server, "LLM", object()), \
             patch.object(server, "runtime_llm", return_value=object()), \
             patch.object(server, "_retire_llm", new=AsyncMock()), \
             patch.object(server, "_rewrite_config"), \
             patch.object(isolation_workspace, "cleanup"):
            for password in ("new", "-"):
                result = await server.api_save_settings(server.SettingsRequest(web_password=password))
                self.assertTrue(result["ok"], result)
                self.assertFalse(server._SESSIONS)
        self.assertEqual(2, remote.close.await_count)
        local.close.assert_not_awaited()

    async def test_worker_password_save_syncs_explicit_values_and_reports_failure(self):
        cfg = Config()
        cfg.role.mode = "worker"
        cfg.auth.password = "short-pass"
        with patch.dict(os.environ, {"AGENT_WEB_PASSWORD": ""}), \
             patch.object(server, "CFG", cfg), \
             patch.object(server, "ACTIVE_WS", set()), \
             patch.object(server, "LLM", object()), \
             patch.object(server, "runtime_llm", return_value=object()), \
             patch.object(server, "_retire_llm", new=AsyncMock()), \
             patch.object(server, "_rewrite_config"), \
             patch.object(isolation_workspace, "cleanup"), \
             patch("backend.core.cloud_deploy.sync_web_password") as sync:
            result = await server.api_save_settings(server.SettingsRequest(web_password="short-pass"))
            self.assertTrue(result["cloud_synced"], result)
            self.assertEqual("short-pass", sync.call_args.args[1])
            sync.reset_mock()
            result = await server.api_save_settings(server.SettingsRequest(web_password=""))
            sync.assert_not_called()
            self.assertEqual("short-pass", cfg.auth.password)
            result = await server.api_save_settings(server.SettingsRequest(web_password="-"))
            self.assertTrue(result["cloud_synced"], result)
            self.assertEqual("", sync.call_args.args[1])
            sync.side_effect = RuntimeError("private server details")
            result = await server.api_save_settings(server.SettingsRequest(web_password="new-short"))
            self.assertTrue(result["ok"])
            self.assertIn("是否生效尚未确认", result["cloud_warning"])
            self.assertNotIn("private server details", str(result))
            sync.reset_mock()
            with patch.dict(os.environ, {"AGENT_WEB_PASSWORD": "managed-password"}):
                cfg.auth.password = "managed-password"
                result = await server.api_save_settings(server.SettingsRequest(web_password="attempt"))
                sync.assert_not_called()
                self.assertIn("ignored_env", result)

    async def test_cloud_web_address_uses_saved_server_and_actual_port(self):
        cfg = Config()
        cfg.role.mode = "worker"
        cfg.role.hub_token = "machine-secret"
        with patch.object(server, "CFG", cfg):
            for url, expected in (
                ("wss://203.0.113.10:8457/worker", "https://203.0.113.10:8457"),
                ("wss://198.51.100.20:9443/worker", "https://198.51.100.20:9443"),
                ("wss://[2001:db8::1]:8448/worker", "https://[2001:db8::1]:8448"),
                ("wss://hub.example/worker", "https://hub.example:443"),
                ("", ""), ("ws://hub.example/worker", ""),
                ("wss://user:secret@hub.example/worker", ""),
                ("wss://hub.example:0/worker", ""),
                ("wss://hub.example:99999/worker", ""),
                ("wss://hub.example:bad/worker", ""),
                ("wss://hub.example/worker?token=secret", ""),
            ):
                with self.subTest(url=url):
                    cfg.role.hub_url = url
                    self.assertEqual(expected, server._cloud_web_url())
            cfg.role.hub_url = "wss://203.0.113.10:8457/worker"
            settings = await server.api_get_settings()
            self.assertEqual("https://203.0.113.10:8457", settings["role"]["web_url"])
            cfg.role.hub_token = ""
            self.assertEqual("", server._cloud_web_url())
            cfg.role.mode = "standalone"
            self.assertEqual("", server._cloud_web_url())
            cfg.role.mode = "hub"
            cfg.server.port = 8459
            with patch.dict(os.environ, {"AGENT_PUBLIC_HOST": "198.51.100.22"}):
                self.assertEqual("https://198.51.100.22:8459", server._cloud_web_url())
            with patch.dict(os.environ, {"AGENT_PUBLIC_HOST": "2001:db8::2"}):
                self.assertEqual("https://[2001:db8::2]:8459", server._cloud_web_url())
            with patch.dict(os.environ, {"AGENT_PUBLIC_HOST": ""}):
                self.assertEqual("", server._cloud_web_url())

    async def test_public_web_and_worker_share_ca_endpoint(self):
        cfg = Config()
        cfg.role.mode = "worker"
        cfg.role.hub_token = "machine-secret"
        cfg.role.hub_url = "wss://203.0.113.10:1115/worker"
        with tempfile.TemporaryDirectory() as temp, patch.object(server, "CFG", cfg), \
             patch.object(server, "app_home", return_value=Path(temp)):
            self.assertEqual("https://203.0.113.10:1115", server._cloud_web_url())
            server._validate_worker_connection("worker", cfg.role.hub_url, cfg.role.hub_token, "test")
            cfg.role.mode = "hub"
            cfg.server.port = 1004
            (Path(temp) / "data").mkdir()
            (Path(temp) / "data/public-tls.json").write_text(json.dumps({"host": "203.0.113.10", "web_port": 1115}))
            with patch.dict(os.environ, {"AGENT_PUBLIC_HOST": "203.0.113.10"}):
                self.assertEqual("https://203.0.113.10:1115", server._cloud_web_url())

    async def test_cloud_feishu_update_is_separate_from_deployment(self):
        local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        remote = SimpleNamespace(client=SimpleNamespace(host="203.0.113.9"))
        request = server.CloudFeishuRequest(allowed_open_ids="ou_one, ou_two")
        with self.assertRaises(HTTPException):
            await server.api_cloud_feishu(request, remote)
        with patch("backend.core.cloud_deploy.update_feishu") as update:
            result = await server.api_cloud_feishu(request, local)
        self.assertTrue(result["ok"])
        self.assertEqual(["ou_one", "ou_two"], update.call_args.args[1])

    async def test_cloud_feishu_status_is_local_only_and_secret_safe(self):
        local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        remote = SimpleNamespace(client=SimpleNamespace(host="203.0.113.9"))
        expected = {
            "connected": True, "app_id": "cli_cloud", "allowed_open_ids": ["ou_cloud"],
            "app_secret_configured": True,
        }
        with self.assertRaises(HTTPException):
            await server.api_get_cloud_feishu(remote)
        with patch("backend.core.cloud_deploy.get_feishu_status", return_value=expected):
            result = await server.api_get_cloud_feishu(local)
        self.assertEqual(expected, result)
        self.assertNotIn("app_secret", result)

    async def test_feishu_logout_clears_local_and_cloud_bindings(self):
        local = Config()
        local.feishu.app_id = "cli_local"
        local.feishu.app_secret = "secret_local"
        local.feishu.allowed_open_ids = ["ou_local"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "data" / "cloud-ssh" / "target.json"
            target.parent.mkdir(parents=True)
            target.write_text("{}", encoding="utf-8")
            with patch.object(server, "CFG", local), \
                 patch.object(server, "app_home", return_value=root), \
                 patch.object(server, "_rewrite_config") as rewrite, \
                 patch("backend.modules.chat.feishu_module.stop_listener") as stop, \
                 patch("backend.modules.chat.feishu_module.reset_client") as reset, \
                 patch("backend.modules.chat.feishu_module.clear_last_open_id") as clear, \
                 patch("backend.core.cloud_deploy.update_feishu") as update, \
                 patch("backend.core.cloud_deploy.get_feishu_status", return_value={
                     "connected": True, "app_id": "", "app_secret_configured": False,
                     "allowed_open_ids": [],
                 }):
                result = await server._logout_all_feishu()
        self.assertTrue(result["ok"])
        self.assertTrue(result["local"]["ok"])
        self.assertTrue(result["cloud"]["ok"])
        self.assertEqual("", local.feishu.app_id)
        self.assertEqual("", local.feishu.app_secret)
        self.assertEqual([], local.feishu.allowed_open_ids)
        stop.assert_called_once()
        reset.assert_called_once_with()
        clear.assert_called_once_with()
        rewrite.assert_called_once_with()
        update.assert_called_once_with(root, [], logout=True)

    async def test_feishu_logout_reports_cloud_failure(self):
        local = Config()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "data" / "cloud-ssh" / "target.json"
            target.parent.mkdir(parents=True)
            target.write_text("{}", encoding="utf-8")
            with patch.object(server, "CFG", local), \
                 patch.object(server, "app_home", return_value=root), \
                 patch.object(server, "_rewrite_config"), \
                 patch("backend.modules.chat.feishu_module.stop_listener"), \
                 patch("backend.modules.chat.feishu_module.reset_client"), \
                 patch("backend.modules.chat.feishu_module.clear_last_open_id"), \
                 patch("backend.core.cloud_deploy.update_feishu", side_effect=RuntimeError("offline")):
                result = await server._logout_all_feishu()
        self.assertFalse(result["ok"])
        self.assertTrue(result["local"]["ok"])
        self.assertFalse(result["cloud"]["ok"])
        self.assertIn("offline", result["cloud"]["error"])

    async def test_feishu_recent_endpoint_uses_current_configuration_and_reports_failure(self):
        with patch("backend.modules.chat.feishu_module.last_open_id", return_value="ou_local") as recent:
            self.assertEqual({"last_open_id": "ou_local"}, await server.api_feishu_recent())
            recent.assert_called_once_with(server.CFG)
        with patch("backend.modules.chat.feishu_module.last_open_id", side_effect=RuntimeError("private")):
            with self.assertRaises(HTTPException) as raised:
                await server.api_feishu_recent()
        self.assertEqual(503, raised.exception.status_code)
        self.assertNotIn("private", raised.exception.detail)

    async def test_cloud_feishu_requires_app_id_and_secret_together(self):
        local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        with patch("backend.core.cloud_deploy.update_feishu") as update:
            result = await server.api_cloud_feishu(
                server.CloudFeishuRequest(allowed_open_ids="ou_one", app_id="cli_new"), local,
            )
        self.assertFalse(result["ok"])
        self.assertIn("同时填写", result["error"])
        update.assert_not_called()

    async def test_extension_http_requires_local_explicitly_confirmed_install(self):
        local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        remote = SimpleNamespace(client=SimpleNamespace(host="203.0.113.10"))
        manager = SimpleNamespace(
            inspect=lambda url, kind: {"ok": True, "name": "demo", "warning": "不可信"},
            install=lambda url, kind: {"ok": True, "name": "demo"},
        )
        request = server.ExtensionInstallRequest(
            url="https://github.com/acme/demo", kind="skill",
        )
        with patch.object(server, "EXTENSION_MANAGER", manager):
            inspected = await server.api_extension_inspect(request, local)
            self.assertEqual("demo", inspected["name"])
            with self.assertRaises(HTTPException) as missing_confirmation:
                await server.api_extension_install(request, local)
            self.assertEqual(400, missing_confirmation.exception.status_code)
            with self.assertRaises(HTTPException) as remote_install:
                await server.api_extension_install(
                    request.model_copy(update={"confirm_install": True}), remote,
                )
            self.assertEqual(403, remote_install.exception.status_code)
            installed = await server.api_extension_install(
                request.model_copy(update={"confirm_install": True}), local,
            )
            self.assertEqual("demo", installed["name"])

    async def test_worker_connection_settings_validate_and_save(self):
        for invalid in (
            "ws://xiaoa.example.com/worker",
            "wss://xiaoa.example.com/wrong",
        ):
            with self.subTest(url=invalid), self.assertRaises(ValueError):
                server._validate_worker_connection("worker", invalid, "token", "office-pc")

        cfg = Config()
        cfg.role.worker_id = "old-pc"
        request = server.SettingsRequest(
            role_mode="worker",
            hub_url="wss://xiaoa.example.com/worker",
            hub_token="new-hub-token",
            worker_id="office-pc",
        )
        empty_role_env = {
            "AGENT_ROLE": "", "AGENT_HUB_URL": "", "AGENT_HUB_TOKEN": "",
            "AGENT_WORKER_ID": "",
        }
        with tempfile.TemporaryDirectory(prefix="xiaoa-worker-settings-") as tmp, \
             patch.dict(os.environ, empty_role_env, clear=False), \
             patch.object(server, "CFG", cfg), \
             patch.object(server, "LLM", object()), \
             patch.object(server, "LLMClient", return_value=object()), \
             patch.object(server, "_retire_llm", new=AsyncMock()), \
             patch.object(server, "config_path", return_value=Path(tmp) / "config.toml"):
            result = await server.api_save_settings_section("connections", request)
            persisted = load_config(Path(tmp) / "config.toml")
        self.assertTrue(result["ok"])
        self.assertTrue(result["restart_required"])
        self.assertEqual("worker", cfg.role.mode)
        self.assertEqual("wss://xiaoa.example.com/worker", cfg.role.hub_url)
        self.assertEqual("new-hub-token", cfg.role.hub_token)
        self.assertEqual("office-pc", cfg.role.worker_id)
        self.assertEqual("worker", persisted.role.mode)
        self.assertEqual("wss://xiaoa.example.com/worker", persisted.role.hub_url)
        self.assertEqual("new-hub-token", persisted.role.hub_token)

    async def test_connection_string_settings_parse_and_save(self):
        fingerprint = "a" * 64
        token = "token-" + "x" * 31
        value = make_connection_string("203.0.113.10", 1115, fingerprint, token)
        cfg = Config()
        with tempfile.TemporaryDirectory(prefix="xiaoa-connection-settings-") as tmp, \
             patch.dict(os.environ, {"AGENT_HUB_TOKEN": ""}, clear=False), \
             patch.object(server, "CFG", cfg), \
             patch.object(server, "LLM", object()), \
             patch.object(server, "LLMClient", return_value=object()), \
             patch.object(server, "_retire_llm", new=AsyncMock()), \
             patch.object(server, "config_path", return_value=Path(tmp) / "config.toml"):
            result = await server.api_save_settings_section(
                "connections", server.SettingsRequest(
                    connection_string=value, worker_id="new-pc",
                ),
            )
            persisted = load_config(Path(tmp) / "config.toml")
        self.assertTrue(result["ok"])
        self.assertTrue(result["restart_required"])
        self.assertEqual("worker", cfg.role.mode)
        self.assertEqual("wss://203.0.113.10:1115/worker", cfg.role.hub_url)
        self.assertEqual(fingerprint, cfg.role.tls_fingerprint)
        self.assertEqual(token, persisted.role.hub_token)

    async def test_hub_probe_does_not_replace_online_worker(self):
        class FakeWebSocket:
            def __init__(self):
                self.sent = []
                self.closed = False

            async def receive_text(self):
                return json.dumps({
                    "type": "hello", "token": "secret", "worker_id": "probe",
                    "probe": True,
                })

            async def send_text(self, value):
                self.sent.append(json.loads(value))

            async def close(self, code=1000):
                self.closed = True

        bridge = server.HubBridge("secret")
        existing = object()
        bridge._ws = existing
        ws = FakeWebSocket()
        await bridge.handle_worker(ws)
        self.assertEqual(existing, bridge._ws)
        self.assertEqual([{"type": "welcome"}], ws.sent)
        self.assertTrue(ws.closed)

    async def test_local_health_reports_backend_version(self):
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        with patch.dict(os.environ, {"XIAOA_PARENT_PID": "1234"}):
            result = await server.health(request)
        self.assertEqual(__version__, result["version"])
        self.assertEqual("1234", result["frontend_pid"])

    async def test_remote_health_requires_valid_session_for_model_list(self):
        cfg = Config()
        cfg.auth.password = "test-web-password"
        cfg.llm.chat_model = "test-chat-model"
        cfg.llm.models = ["test-chat-model", "test-second-model"]
        sessions = {"valid": time.time() + 60, "expired": time.time() - 60}
        with patch.object(server, "CFG", cfg), patch.object(server, "_SESSIONS", sessions):
            for token in ("", "unknown", "expired", "valid"):
                with self.subTest(token=token):
                    request = SimpleNamespace(
                        client=SimpleNamespace(host="203.0.113.10"),
                        cookies={"xa_session": token},
                    )
                    result = await server.health(request)
                    if token == "valid":
                        self.assertEqual(cfg.llm.chat_model, result["chat_model"])
                        self.assertEqual(cfg.llm.models, result["models"])
                        self.assertEqual(server._model_options(), result["model_options"])
                        self.assertEqual(cfg.voice.tts_enabled, result["voice"])
                    else:
                        self.assertEqual({"ok": True, "role": cfg.role.mode}, result)

    def test_backend_version_matches_frontend_package(self):
        package_path = Path(__file__).resolve().parents[1] / "frontend" / "package.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        self.assertEqual(package["version"], __version__)

    def test_display_history_is_decoupled_from_model_context(self):
        history = [{"role": "user", "content": str(i)} for i in range(30)]
        recent = server._recent_history(history, 20)
        self.assertEqual(20, len(recent))
        self.assertEqual("10", recent[0]["content"])
        self.assertEqual(30, len(history))

    async def test_cancel_running_task_waits_for_cancellation(self):
        started = asyncio.Event()
        finished = asyncio.Event()

        async def worker():
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                finished.set()

        task = asyncio.create_task(worker())
        await started.wait()
        self.assertTrue(await server._cancel_running_task(task))
        self.assertTrue(task.cancelled())
        self.assertTrue(finished.is_set())

    async def test_cancel_chat_save_waits_for_file_write(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        with tempfile.TemporaryDirectory(prefix="xiaoa-chat-save-") as tmp:
            path = Path(tmp) / "history.json"

            def save():
                started.set()
                try:
                    if not release.wait(5):
                        raise TimeoutError("write was not released")
                    path.write_text("[]", encoding="utf-8")
                    return True
                finally:
                    finished.set()

            task = asyncio.create_task(server._save_chat_in_thread(save))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertFalse(finished.is_set())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(finished.is_set())
            self.assertEqual("[]", path.read_text(encoding="utf-8"))

    async def test_cancel_to_thread_wait_returns_in_under_three_seconds(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_worker():
            started.set()
            try:
                release.wait(10)
            finally:
                finished.set()

        task = asyncio.create_task(asyncio.to_thread(blocking_worker))
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        try:
            began = time.perf_counter()
            self.assertTrue(await server._cancel_running_task(task))
            self.assertLess(time.perf_counter() - began, 3.0)
            self.assertFalse(finished.is_set())
            self.assertTrue(task.cancelled())
        finally:
            release.set()
            await asyncio.to_thread(finished.wait, 2)

    async def test_websocket_stop_does_not_wait_for_blocking_tool_thread(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_work():
            started.set()
            try:
                release.wait(10)
            finally:
                finished.set()
            return "finished"

        registry = ToolRegistry()

        async def blocking_tool():
            return await asyncio.to_thread(blocking_work)

        registry.register(Tool(
            "blocking_tool", "platform-independent blocking test tool",
            {"type": "object", "properties": {}}, blocking_tool,
        ))

        class ToolAgent:
            runs = 0

            def __init__(self, _llm, tools, *_args, **_kwargs):
                self.memory = None
                self.tools = tools

            async def run(self, *_args, **_kwargs):
                ToolAgent.runs += 1
                if ToolAgent.runs == 1:
                    await self.tools.call("blocking_tool", {})
                yield SimpleNamespace(type="message_end", data={"content": "next message handled"})
                yield SimpleNamespace(type="done", data={})

        class FakeWebSocket:
            def __init__(self):
                self.headers = {"origin": "", "host": "127.0.0.1:1985"}
                self.query_params = {}
                self.cookies = {}
                self.client = SimpleNamespace(host="127.0.0.1")
                self.sent: list[dict] = []
                self.receive_count = 0
                self.stopped = asyncio.Event()
                self.done = asyncio.Event()
                self.stop_requested_at = 0.0
                self.stop_received_at = 0.0

            async def accept(self):
                pass

            async def close(self, code=None):
                del code

            async def send_text(self, payload: str):
                item = json.loads(payload)
                self.sent.append(item)
                if item.get("type") == "stopped":
                    self.stop_received_at = time.perf_counter()
                    self.stopped.set()
                elif item.get("type") == "done":
                    self.done.set()

            async def receive_text(self):
                self.receive_count += 1
                if self.receive_count == 1:
                    return json.dumps({
                        "type": "user_input", "client_id": "stop-command",
                        "text": "run blocked command",
                    })
                if self.receive_count == 2:
                    if not await asyncio.to_thread(started.wait, 2):
                        raise AssertionError("blocking tool worker did not start")
                    self.stop_requested_at = time.perf_counter()
                    return json.dumps({"type": "stop"})
                if self.receive_count == 3:
                    await asyncio.wait_for(self.stopped.wait(), 3)
                    return json.dumps({
                        "type": "user_input", "client_id": "after-stop",
                        "text": "next message",
                    })
                await asyncio.wait_for(self.done.wait(), 3)
                raise WebSocketDisconnect()

        ws = FakeWebSocket()
        old_history = server.DESKTOP_HISTORY
        old_path = server._HISTORY_PATH
        old_active = server._ACTIVE_CLIENT_INPUTS.copy()
        old_completed = server._COMPLETED_CLIENT_INPUTS.copy()
        old_resume = server._RESUME_STATES.copy()
        old_resume_path = server._RESUME_PATH
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoa-stop-ws-") as tmp, \
                 patch.object(server, "Agent", ToolAgent), \
                 patch.object(server, "REGISTRY", registry):
                server.DESKTOP_HISTORY = []
                server._HISTORY_PATH = Path(tmp) / "history.json"
                server._ACTIVE_CLIENT_INPUTS.clear()
                server._COMPLETED_CLIENT_INPUTS.clear()
                server._RESUME_STATES.clear()
                server._RESUME_PATH = Path(tmp) / "resume_states.json"
                await server.ws_endpoint(ws)

            self.assertLess(ws.stop_received_at - ws.stop_requested_at, 3.0)
            self.assertFalse(finished.is_set())
            self.assertEqual(2, ToolAgent.runs)
            self.assertTrue(any(
                item.get("type") == "done" and
                item.get("data", {}).get("client_id") == "after-stop"
                for item in ws.sent
            ))
        finally:
            release.set()
            await asyncio.to_thread(finished.wait, 2)
            server.DESKTOP_HISTORY = old_history
            server._HISTORY_PATH = old_path
            server._ACTIVE_CLIENT_INPUTS.clear()
            server._ACTIVE_CLIENT_INPUTS.update(old_active)
            server._COMPLETED_CLIENT_INPUTS.clear()
            server._COMPLETED_CLIENT_INPUTS.update(old_completed)
            server._RESUME_STATES.clear()
            server._RESUME_STATES.update(old_resume)
            server._RESUME_PATH = old_resume_path

    async def test_websocket_stop_checkpoint_resumes_with_user_correction(self):
        started = asyncio.Event()
        resumed = asyncio.Event()

        class CheckpointAgent:
            resume_messages = None
            correction = ""

            def __init__(self, *_args, **kwargs):
                self.memory = None
                self.abort_fn = kwargs["abort_fn"]

            async def run(self, text, **kwargs):
                if kwargs.get("resume_messages") is None:
                    started.set()
                    while not self.abort_fn():
                        await asyncio.sleep(0.01)
                    yield SimpleNamespace(type="aborted", data={
                        "where": "agent.abort", "msg": "stopped",
                        "resume_messages": [
                            {"role": "system", "content": "rules"},
                            {"role": "user", "content": text},
                        ],
                    })
                    yield SimpleNamespace(type="done", data={"aborted": True})
                    return
                CheckpointAgent.resume_messages = kwargs["resume_messages"]
                CheckpointAgent.correction = text
                yield SimpleNamespace(type="message_end", data={"content": "已按纠正继续"})
                yield SimpleNamespace(type="done", data={})
                resumed.set()

        class FakeWebSocket:
            def __init__(self):
                self.headers = {"origin": "", "host": "127.0.0.1:1985"}
                self.query_params = {}
                self.cookies = {}
                self.client = SimpleNamespace(host="127.0.0.1")
                self.sent = []
                self.receive_count = 0
                self.stopped = asyncio.Event()

            async def accept(self):
                pass

            async def close(self, code=None):
                del code

            async def send_text(self, payload):
                item = json.loads(payload)
                self.sent.append(item)
                if item.get("type") == "stopped":
                    self.stopped.set()

            async def receive_text(self):
                self.receive_count += 1
                if self.receive_count == 1:
                    return json.dumps({
                        "type": "user_input", "client_id": "original-task", "text": "先做旧方案",
                    })
                if self.receive_count == 2:
                    await asyncio.wait_for(started.wait(), 2)
                    return json.dumps({"type": "stop"})
                if self.receive_count == 3:
                    await asyncio.wait_for(self.stopped.wait(), 2)
                    return json.dumps({
                        "type": "user_input", "client_id": "correction-task",
                        "text": "不对，先做新方案",
                    }, ensure_ascii=False)
                await asyncio.wait_for(resumed.wait(), 2)
                raise WebSocketDisconnect()

        ws = FakeWebSocket()
        old_history = server.DESKTOP_HISTORY
        old_path = server._HISTORY_PATH
        old_active = server._ACTIVE_CLIENT_INPUTS.copy()
        old_completed = server._COMPLETED_CLIENT_INPUTS.copy()
        old_resume = server._RESUME_STATES.copy()
        old_resume_path = server._RESUME_PATH
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoa-resume-ws-") as tmp, \
                 patch.object(server, "Agent", CheckpointAgent):
                server.DESKTOP_HISTORY = []
                server._HISTORY_PATH = Path(tmp) / "history.json"
                server._ACTIVE_CLIENT_INPUTS.clear()
                server._COMPLETED_CLIENT_INPUTS.clear()
                server._RESUME_STATES.clear()
                server._RESUME_PATH = Path(tmp) / "resume_states.json"
                await server.ws_endpoint(ws)
            self.assertEqual("不对，先做新方案", CheckpointAgent.correction)
            self.assertEqual("先做旧方案", CheckpointAgent.resume_messages[-1]["content"])
            self.assertTrue(any(
                item.get("type") == "message_end"
                and item.get("data", {}).get("content") == "已按纠正继续"
                for item in ws.sent
            ))
            self.assertTrue(server._is_continue_request("继续"))
            self.assertTrue(server._is_continue_request("go on"))
        finally:
            server.DESKTOP_HISTORY = old_history
            server._HISTORY_PATH = old_path
            server._ACTIVE_CLIENT_INPUTS.clear()
            server._ACTIVE_CLIENT_INPUTS.update(old_active)
            server._COMPLETED_CLIENT_INPUTS.clear()
            server._COMPLETED_CLIENT_INPUTS.update(old_completed)
            server._RESUME_STATES.clear()
            server._RESUME_STATES.update(old_resume)
            server._RESUME_PATH = old_resume_path

    def test_resume_states_are_durable_and_not_count_bounded(self):
        old_states = server._RESUME_STATES.copy()
        old_path = server._RESUME_PATH
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoa-resume-expiry-") as tmp:
                server._RESUME_PATH = Path(tmp) / "resume_states.json"
                server._RESUME_STATES.clear()
                saved = server._save_resume_state(
                    "old", [{"role": "user", "content": "old"}], {}, "stopped",
                )
                saved["updated_at"] = 10
                refreshed = server._resume_state("old")
                self.assertEqual([{"role": "user", "content": "old"}], refreshed["messages"])
                for index in range(35):
                    server._save_resume_state(str(index), [{"role": "user", "content": str(index)}], {}, "stopped")
                self.assertEqual(36, len(server._RESUME_STATES))
        finally:
            server._RESUME_STATES.clear()
            server._RESUME_STATES.update(old_states)
            server._RESUME_PATH = old_path

    def test_explicit_resume_is_available_without_an_age_window(self):
        state = {
            "messages": [{"role": "user", "content": "整理报表"}],
            "reason": "stopped", "updated_at": 1000,
        }
        self.assertTrue(server._resume_automatically(
            state, "继续", now=1000 + 10 * 365 * 24 * 3600,
        ))
        self.assertTrue(server._resume_automatically(
            state, "接着做", now=1000 + 10 * 365 * 24 * 3600,
        ))
        self.assertTrue(server._resume_automatically(
            state, "补充一个结果", now=1000 + 60,
        ))
        self.assertFalse(server._resume_automatically(
            state, "补充一个结果", now=1000 + 3 * 3600,
        ))
        state["reason"] = "service_error"
        self.assertTrue(server._resume_automatically(state, "继续", now=1000 + 10 * 365 * 24 * 3600))
        state["reason"] = "completed"
        self.assertFalse(server._resume_automatically(state, "继续", now=1000))
    def test_resume_persistence_compacts_and_redacts_without_mutating_memory(self):
        old_states = server._RESUME_STATES.copy()
        old_path = server._RESUME_PATH
        secret = "resume-password-must-stay-in-memory"
        arguments = json.dumps({"password": secret, "path": "/tmp/report"})
        messages = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "第一条原话"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "demo", "arguments": arguments},
            }]},
            {"role": "tool", "tool_call_id": "call-1", "content": "x" * 10_000},
            {"role": "user", "content": "第二条原话"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "第三条原话"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "第四条原话"},
        ]
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoa-resume-disk-") as tmp:
                server._RESUME_PATH = Path(tmp) / "resume_states.json"
                server._RESUME_STATES.clear()
                state = server._save_resume_state(
                    "desktop", messages, {"model": "demo"}, "stopped", "整理报表",
                )
                disk_text = server._RESUME_PATH.read_text(encoding="utf-8")
                disk = json.loads(disk_text)["desktop"]
                disk_arguments = json.loads(
                    disk["messages"][2]["tool_calls"][0]["function"]["arguments"]
                )
                self.assertNotIn(secret, disk_text)
                self.assertIn("***", disk_arguments["password"])
                self.assertIn("早期工具结果已压缩", disk["messages"][3]["content"])
                self.assertEqual(arguments, state["messages"][2]["tool_calls"][0]["function"]["arguments"])
                self.assertEqual(
                    ["第一条原话", "第二条原话", "第三条原话", "第四条原话"],
                    [item["content"] for item in disk["messages"] if item.get("role") == "user"],
                )

                server._RESUME_STATES.clear()
                server._RESUME_STATES.update(server._load_resume_states())
                loaded_arguments = json.loads(
                    server._RESUME_STATES["desktop"]["messages"][2]
                    ["tool_calls"][0]["function"]["arguments"]
                )
                self.assertIn("***", loaded_arguments["password"])

                with patch.object(server.os, "replace", side_effect=OSError("disk busy")):
                    self.assertFalse(server._persist_resume_states())
                self.assertEqual(disk_text, server._RESUME_PATH.read_text(encoding="utf-8"))
                self.assertFalse(server._RESUME_PATH.with_suffix(".tmp").exists())
        finally:
            server._RESUME_STATES.clear()
            server._RESUME_STATES.update(old_states)
            server._RESUME_PATH = old_path

    def test_resume_disk_redacts_temporary_cli_secrets_without_over_redacting(self):
        old_states = server._RESUME_STATES.copy()
        old_path = server._RESUME_PATH
        secrets = ("TempSshpass-11", "TempOption-22", "TempAssignment-33")
        commands = [
            f"sshpass -p {secrets[0]} ssh host docker ps",
            f"client --password={secrets[1]} --file C:/报表/普通.xlsx",
            f'PASSWORD = "{secrets[2]}"\necho 普通中文; docker ps',
        ]
        tool_calls = [{
            "id": f"call-{index}", "type": "function",
            "function": {
                "name": "code_run",
                "arguments": json.dumps({"command": command}, ensure_ascii=False),
            },
        } for index, command in enumerate(commands)]
        messages = [
            {"role": "user", "content": "普通中文和 C:/报表/普通.xlsx 要保留"},
            {"role": "assistant", "content": None, "tool_calls": tool_calls},
        ]
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoa-resume-cli-secret-") as tmp:
                server._RESUME_PATH = Path(tmp) / "resume_states.json"
                server._RESUME_STATES.clear()
                state = server._save_resume_state(
                    "desktop", messages, {"model": "demo"}, "stopped", "普通任务",
                )
                disk_text = server._RESUME_PATH.read_text(encoding="utf-8")
                for secret in secrets:
                    self.assertNotIn(secret, disk_text)
                self.assertIn("***", disk_text)
                self.assertIn("普通中文", disk_text)
                self.assertIn("C:/报表/普通.xlsx", disk_text)
                self.assertIn("docker ps", disk_text)
                self.assertEqual(
                    json.dumps({"command": commands[0]}, ensure_ascii=False),
                    state["messages"][1]["tool_calls"][0]["function"]["arguments"],
                )
        finally:
            server._RESUME_STATES.clear()
            server._RESUME_STATES.update(old_states)
            server._RESUME_PATH = old_path

    def test_resume_disk_redacts_supported_cli_forms_and_omits_unknown_long_arguments(self):
        samples = {
            "sshpass -p one ssh host": "one",
            "tool -p two": "two",
            "tool --password three": "three",
            "tool --password=four": "four",
            "mysql -pfive db": "five",
            "curl -u user:six https://example.com": "six",
            "ssh://user:seven@example.com": "seven",
            'PASSWORD = "eight"': "eight",
            "pass=nine": "nine",
            "passphrase=ten": "ten",
            "credential=eleven": "eleven",
            "auth_token=twelve": "twelve",
        }
        for command, secret in samples.items():
            with self.subTest(command=command):
                redacted = server._redact_resume_value({"command": command})["command"]
                self.assertNotIn(secret, redacted)
                self.assertIn("***", redacted)
        ordinary = [
            "普通中文", "C:/报表/普通.xlsx", "docker ps", "git status",
            "cat /etc/os-release",
        ]
        for command in ordinary:
            with self.subTest(ordinary=command):
                self.assertEqual(
                    command, server._redact_resume_value({"command": command})["command"],
                )
        self.assertEqual(
            server._RESUME_OMITTED,
            server._redact_resume_value({"script": "x" * 5000})["script"],
        )

    def test_resume_disk_redacts_url_credentials_and_pass_assignment(self):
        old_states = server._RESUME_STATES.copy()
        old_path = server._RESUME_PATH
        urls = [
            "ssh://user:UrlPW1@host/path",
            "https://user:UrlPW2@github.com/x/y.git",
            "http://admin:UrlPW3@10.0.0.5/api",
            "ftp://u:UrlPW4@host/f.zip",
            "mysql://root:UrlPW5@db:3306/mydb",
            "redis://default:UrlPW6@cache:6379",
        ]
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoa-resume-url-secret-") as tmp:
                server._RESUME_PATH = Path(tmp) / "resume_states.json"
                server._RESUME_STATES.clear()
                for index, url in enumerate(urls):
                    server._save_resume_state(
                        f"url-{index}", [{"role": "tool", "content": url}],
                        {}, "stopped", "URL 任务",
                    )
                    disk_text = server._RESUME_PATH.read_text(encoding="utf-8")
                    self.assertNotIn(f"UrlPW{index + 1}", disk_text)
                    self.assertIn("***", disk_text)

                content = (
                    "pass=LoosePass77\n测试已 passed\nbypass 检查\n"
                    "https://github.com/x/y\nhttp://127.0.0.1:8080/health"
                )
                state = server._save_resume_state(
                    "pass", [{"role": "tool", "content": content}],
                    {}, "stopped", "普通任务",
                )
                disk_text = server._RESUME_PATH.read_text(encoding="utf-8")
                self.assertNotIn("LoosePass77", disk_text)
                self.assertIn("pass=***", disk_text)
                self.assertIn("测试已 passed", disk_text)
                self.assertIn("bypass 检查", disk_text)
                self.assertIn("https://github.com/x/y", disk_text)
                self.assertIn("http://127.0.0.1:8080/health", disk_text)
                self.assertEqual(content, state["messages"][0]["content"])
        finally:
            server._RESUME_STATES.clear()
            server._RESUME_STATES.update(old_states)
            server._RESUME_PATH = old_path

    async def test_completed_task_clears_old_checkpoint_and_continue_is_a_follow_up(self):
        class CaptureAgent:
            calls = []

            def __init__(self, *_args, **_kwargs):
                self.memory = None

            async def run(self, text, **kwargs):
                CaptureAgent.calls.append((text, kwargs.get("resume_messages")))
                yield SimpleNamespace(type="message_end", data={"content": f"完成:{text}"})
                yield SimpleNamespace(type="done", data={})

        class OneMessageWebSocket:
            def __init__(self, text):
                self.text = text
                self.headers = {"origin": "", "host": "127.0.0.1:1985"}
                self.query_params = {}
                self.cookies = {}
                self.client = SimpleNamespace(host="127.0.0.1")
                self.sent = []
                self.received = False
                self.done = asyncio.Event()
            async def accept(self): pass
            async def close(self, code=None): del code
            async def send_text(self, payload):
                item = json.loads(payload)
                self.sent.append(item)
                if item.get("type") in {"done", "error"}: self.done.set()
            async def receive_text(self):
                if not self.received:
                    self.received = True
                    return json.dumps({"type": "user_input", "text": self.text}, ensure_ascii=False)
                await asyncio.wait_for(self.done.wait(), 2)
                await asyncio.sleep(0.05)
                raise WebSocketDisconnect()

        old_states = server._RESUME_STATES.copy()
        old_history = server.DESKTOP_HISTORY
        old_conversations = server.DESKTOP_CONVERSATIONS
        old_history_path = server._HISTORY_PATH
        old_resume_path = server._RESUME_PATH
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoa-resume-window-ws-") as tmp, \
                 patch.object(server, "Agent", CaptureAgent):
                server._RESUME_PATH = Path(tmp) / "resume_states.json"
                server._HISTORY_PATH = Path(tmp) / "history.json"
                server.DESKTOP_CONVERSATIONS = {"default": server._new_conversation("default")}
                server.DESKTOP_HISTORY = server.DESKTOP_CONVERSATIONS["default"]["messages"]
                server._RESUME_STATES.clear()
                state = server._save_resume_state(
                    "desktop", [{"role": "user", "content": "整理D盘报表"}],
                    {"model": "demo"}, "stopped", "整理D盘报表",
                )
                state["updated_at"] = time.time() - 3 * 3600
                server._persist_resume_states()

                await server.ws_endpoint(OneMessageWebSocket("今天天气怎么样"))
                self.assertIsNone(CaptureAgent.calls[0][1])
                self.assertNotIn("desktop", server._RESUME_STATES)
                self.assertEqual({}, json.loads(server._RESUME_PATH.read_text(encoding="utf-8")))

                continue_ws = OneMessageWebSocket("继续")
                await server.ws_endpoint(continue_ws)
                self.assertEqual(2, len(CaptureAgent.calls))
                self.assertEqual(("继续", None), CaptureAgent.calls[1])
                errors = [item for item in continue_ws.sent if item.get("type") == "error"]
                self.assertEqual([], errors)
        finally:
            server._RESUME_STATES.clear()
            server._RESUME_STATES.update(old_states)
            server.DESKTOP_HISTORY = old_history
            server.DESKTOP_CONVERSATIONS = old_conversations
            server._HISTORY_PATH = old_history_path
            server._RESUME_PATH = old_resume_path

    async def test_old_checkpoint_continue_remains_available(self):
        completed = asyncio.Event()

        class CaptureAgent:
            calls = []

            def __init__(self, *_args, **_kwargs):
                self.memory = None

            async def run(self, text, **kwargs):
                CaptureAgent.calls.append((text, kwargs.get("resume_messages")))
                yield SimpleNamespace(type="message_end", data={"content": "已从旧断点继续"})
                yield SimpleNamespace(type="done", data={})

        class FakeWebSocket:
            def __init__(self):
                self.headers = {"origin": "", "host": "127.0.0.1:1985"}
                self.query_params = {}
                self.cookies = {}
                self.client = SimpleNamespace(host="127.0.0.1")
                self.sent = []
                self.received = False
            async def accept(self): pass
            async def close(self, code=None): del code
            async def send_text(self, payload):
                item = json.loads(payload)
                self.sent.append(item)
                if item.get("type") == "done": completed.set()
            async def receive_text(self):
                if not self.received:
                    self.received = True
                    return json.dumps({"type": "user_input", "text": "继续"}, ensure_ascii=False)
                await asyncio.wait_for(completed.wait(), 2)
                raise WebSocketDisconnect()

        old_states = server._RESUME_STATES.copy()
        old_path = server._RESUME_PATH
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoa-resume-expired-ws-") as tmp, \
                 patch.object(server, "Agent", CaptureAgent):
                server._RESUME_PATH = Path(tmp) / "resume_states.json"
                server._RESUME_STATES.clear()
                state = server._save_resume_state(
                    "desktop", [{"role": "user", "content": "old"}], {}, "stopped", "旧任务",
                )
                state["updated_at"] = 0
                ws = FakeWebSocket()
                await server.ws_endpoint(ws)
                errors = [item for item in ws.sent if item.get("type") == "error"]
                self.assertFalse(errors)
                self.assertTrue(any(item.get("type") == "input_ack" for item in ws.sent))
                self.assertEqual("继续", CaptureAgent.calls[0][0])
                self.assertEqual([{"role": "user", "content": "old"}], CaptureAgent.calls[0][1])
        finally:
            server._RESUME_STATES.clear()
            server._RESUME_STATES.update(old_states)
            server._RESUME_PATH = old_path

    async def test_websocket_stop_checkpoint_survives_reconnect_and_model_switch(self):
        started = asyncio.Event()
        first_stopped = asyncio.Event()
        resumed = asyncio.Event()

        class CheckpointAgent:
            resume_messages = None
            models = []

            def __init__(self, *_args, **kwargs):
                self.memory = None
                self.abort_fn = kwargs["abort_fn"]

            async def run(self, text, **kwargs):
                CheckpointAgent.models.append(kwargs.get("model"))
                if kwargs.get("resume_messages") is None:
                    started.set()
                    while not self.abort_fn():
                        await asyncio.sleep(0.01)
                    yield SimpleNamespace(type="aborted", data={
                        "resume_messages": [{"role": "user", "content": text}],
                    })
                    yield SimpleNamespace(type="done", data={"aborted": True})
                    return
                CheckpointAgent.resume_messages = kwargs["resume_messages"]
                yield SimpleNamespace(type="message_end", data={"content": "已恢复"})
                yield SimpleNamespace(type="done", data={})
                resumed.set()

        class FirstWebSocket:
            def __init__(self):
                self.headers = {"origin": "", "host": "127.0.0.1:1985"}
                self.query_params = {}
                self.cookies = {}
                self.client = SimpleNamespace(host="127.0.0.1")
                self.sent = []
                self.received = 0

            async def accept(self): pass
            async def close(self, code=None): del code
            async def send_text(self, payload):
                item = json.loads(payload)
                self.sent.append(item)
                if item.get("type") == "stopped": first_stopped.set()
            async def receive_text(self):
                self.received += 1
                if self.received == 1:
                    return json.dumps({"type": "user_input", "text": "原任务"})
                if self.received == 2:
                    await asyncio.wait_for(started.wait(), 2)
                    return json.dumps({"type": "stop"})
                await asyncio.wait_for(first_stopped.wait(), 2)
                raise WebSocketDisconnect()

        class SecondWebSocket:
            def __init__(self):
                self.headers = {"origin": "", "host": "127.0.0.1:1985"}
                self.query_params = {}
                self.cookies = {}
                self.client = SimpleNamespace(host="127.0.0.1")
                self.sent = []
                self.received = False
            async def accept(self): pass
            async def close(self, code=None): del code
            async def send_text(self, payload): self.sent.append(json.loads(payload))
            async def receive_text(self):
                if not self.received:
                    self.received = True
                    return json.dumps({
                        "type": "continue_task", "provider_id": "default", "model": "switched-model",
                    })
                await asyncio.wait_for(resumed.wait(), 2)
                raise WebSocketDisconnect()

        old_states = server._RESUME_STATES.copy()
        old_history = server.DESKTOP_HISTORY
        old_conversations = server.DESKTOP_CONVERSATIONS
        old_path = server._HISTORY_PATH
        old_resume_path = server._RESUME_PATH
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoa-resume-reconnect-") as tmp, \
                 patch.object(server, "Agent", CheckpointAgent), \
                 patch.object(server.CFG.llm, "models", [server.CFG.llm.chat_model, "switched-model"]):
                server._RESUME_STATES.clear()
                server.DESKTOP_CONVERSATIONS = {"default": server._new_conversation("default")}
                server.DESKTOP_HISTORY = server.DESKTOP_CONVERSATIONS["default"]["messages"]
                server._HISTORY_PATH = Path(tmp) / "history.json"
                server._RESUME_PATH = Path(tmp) / "resume_states.json"
                await server.ws_endpoint(FirstWebSocket())
                self.assertTrue(server._RESUME_PATH.exists())
                server._RESUME_STATES["desktop"]["updated_at"] = time.time() - 10 * 365 * 24 * 3600
                server._persist_resume_states()
                server._RESUME_STATES.clear()
                server._RESUME_STATES.update(server._load_resume_states())
                server.DESKTOP_CONVERSATIONS["default"]["model_selection"] = {
                    "provider_id": "default", "model": "switched-model",
                }
                second = SecondWebSocket()
                await server.ws_endpoint(second)
                self.assertEqual({}, json.loads(server._RESUME_PATH.read_text(encoding="utf-8")))
            self.assertEqual("原任务", CheckpointAgent.resume_messages[-1]["content"])
            self.assertIn("原任务", CheckpointAgent.resume_messages[0]["content"])
            self.assertEqual([server.CFG.llm.chat_model, "switched-model"], CheckpointAgent.models)
            self.assertTrue(any(item.get("type") == "done" for item in second.sent))
        finally:
            server._RESUME_STATES.clear()
            server._RESUME_STATES.update(old_states)
            server.DESKTOP_HISTORY = old_history
            server.DESKTOP_CONVERSATIONS = old_conversations
            server._HISTORY_PATH = old_path
            server._RESUME_PATH = old_resume_path

    async def test_settings_section_rejects_fields_from_another_page(self):
        request = server.SettingsRequest(web_password="must-not-reach-model-page")
        with self.assertRaises(HTTPException) as raised:
            await server.api_save_settings_section("models", request)
        self.assertEqual(400, raised.exception.status_code)


    async def test_settings_section_broadcasts_live_refresh_after_success_only(self):
        with (
            patch.object(server, "api_save_settings", new=AsyncMock(return_value={"ok": True})),
            patch.object(server, "broadcast", new=AsyncMock()) as broadcast,
        ):
            result = await server.api_save_settings_section(
                "models", server.SettingsRequest(chat_model="new-model"),
            )
        self.assertTrue(result["ok"])
        broadcast.assert_awaited_once_with({
            "type": "settings_changed", "data": {"scope": "models"},
        })

        with (
            patch.object(
                server, "api_save_settings",
                new=AsyncMock(return_value={"ok": False, "error": "failed"}),
            ),
            patch.object(server, "broadcast", new=AsyncMock()) as broadcast,
        ):
            result = await server.api_save_settings_section(
                "voice", server.SettingsRequest(tc_region="ap-guangzhou"),
            )
        self.assertFalse(result["ok"])
        broadcast.assert_not_awaited()

        with (
            patch.object(server, "api_save_settings", new=AsyncMock(return_value={"ok": True})),
            patch.object(server, "broadcast", new=AsyncMock(side_effect=RuntimeError("closed"))),
        ):
            result = await server.api_save_settings_section(
                "models", server.SettingsRequest(chat_model="new-model"),
            )
        self.assertTrue(result["ok"])

    async def test_cloud_model_order_preserves_endpoints_secrets_and_conversation_models(self):
        from backend.core.config import LLMProviderConfig, apply_env_overrides
        cfg = Config()
        cfg.role.mode = "worker"
        cfg.llm.models = ["first", "second"]
        cfg.llm.chat_model = cfg.llm.vision_model = "first"
        cfg.llm.api_key = "local-private-key"
        cfg.llm.providers = [LLMProviderConfig(
            id="backup", base_url="https://backup.invalid/v1", api_key="backup-private-key",
            models=["backup-first", "backup-second"])]
        conversation = server.DESKTOP_CONVERSATIONS["default"]
        conversation["model_selection"] = {"provider_id": "default", "model": "first"}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "AGENT_CHAT_MODEL": "first", "AGENT_MODEL_OPTIONS": "first,second",
        }, clear=True), patch.object(server, "CFG", cfg), \
                patch.object(server, "config_path", return_value=Path(tmp) / "config.toml"), \
                patch.object(server, "broadcast", new=AsyncMock()) as notify:
            # Snapshot is generated on the hub only after a browser save.
            self.assertEqual([], server._cloud_model_order())
            cfg.llm.settings_saved = True
            order = server._cloud_model_order()
            self.assertFalse(any("key" in name for item in order for name in item))
            for item in order:
                item["models"] = list(reversed(item["models"]))
            await server._apply_cloud_model_order(order)
            self.assertEqual(["second", "first"], cfg.llm.models)
            self.assertEqual("second", cfg.llm.chat_model)
            self.assertEqual(["backup-second", "backup-first"], cfg.llm.providers[0].models)
            self.assertEqual("local-private-key", cfg.llm.api_key)
            self.assertEqual("backup-private-key", cfg.llm.providers[0].api_key)
            self.assertEqual("first", conversation["model_selection"]["model"])
            restored = apply_env_overrides(load_config(Path(tmp) / "config.toml"))
            self.assertEqual(["second", "first"], restored.llm.models)
            notify.assert_awaited_once_with({"type": "settings_changed", "data": {"scope": "models"}})
            await server._apply_cloud_model_order(order)
            self.assertEqual(1, notify.await_count)

    async def test_cloud_model_order_rejects_mismatch_and_restores_failed_write(self):
        cfg = Config()
        cfg.role.mode = "worker"
        cfg.llm.models = ["first", "second"]
        cfg.llm.chat_model = cfg.llm.vision_model = "first"
        item = {"id": "default", "base_url": cfg.llm.base_url,
                "protocol": cfg.llm.protocol, "models": ["second", "first"]}
        invalid = [None, [None], [dict(item, models=["second", {}])],
                   [dict(item, models=["second", "second"])], [item, item],
                   [dict(item, base_url="https://other.invalid/v1")],
                   [dict(item, protocol="anthropic")], [dict(item, models=["new", "second"])]]
        with patch.object(server, "CFG", cfg), patch.object(server, "_rewrite_config") as save, \
                patch.object(server, "broadcast", new=AsyncMock()) as notify:
            for payload in invalid:
                await server._apply_cloud_model_order(payload)
                self.assertEqual(["first", "second"], cfg.llm.models)
            cfg.role.mode = "standalone"
            await server._apply_cloud_model_order([item])
            save.assert_not_called()
            notify.assert_not_awaited()
            cfg.role.mode = "worker"
            save.side_effect = OSError("disk full")
            with self.assertRaises(OSError):
                await server._apply_cloud_model_order([item])
            self.assertEqual(["first", "second"], cfg.llm.models)
            self.assertEqual("first", cfg.llm.chat_model)
            self.assertFalse(cfg.llm.settings_saved)
            notify.assert_not_awaited()

    async def test_successful_hub_model_save_pushes_order_to_worker(self):
        cfg = Config()
        cfg.role.mode = "hub"
        bridge = SimpleNamespace(sync_model_order=AsyncMock())
        with patch.object(server, "CFG", cfg), patch.object(server, "HUB_BRIDGE", bridge), \
                patch.object(server, "api_save_settings", new=AsyncMock(return_value={"ok": True})) as save, \
                patch.object(server, "broadcast", new=AsyncMock()):
            await server.api_save_settings_section("models", server.SettingsRequest(chat_model="second"))
            bridge.sync_model_order.assert_awaited_once()
            await server.api_save_settings_section("voice", server.SettingsRequest(tts_voice=1002))
            self.assertEqual(1, bridge.sync_model_order.await_count)
            save.return_value = {"ok": False}
            await server.api_save_settings_section("models", server.SettingsRequest(chat_model="first"))
            self.assertEqual(1, bridge.sync_model_order.await_count)

    async def test_wake_voice_status_and_synthesis_use_saved_voice(self):
        cfg = Config()
        cfg.voice.secret_id = "test-id"
        cfg.voice.secret_key = "test-key"
        cfg.voice.tts_voice = 601000
        cfg.voice.tts_speed = 1
        with patch.object(server, "CFG", cfg):
            status = await server.api_current_voice()
            self.assertEqual({"provider": "tencent", "voice": 601000, "speed": 1, "tts_enabled": True}, status)
            with patch.object(server, "synthesize", new=AsyncMock(return_value=b"audio")) as synth:
                result = await server.api_tts(server.TTSRequest(text="我在", voice=status["voice"]))
            self.assertTrue(result["ok"])
            synth.assert_awaited_once_with(cfg.voice, "我在", voice=601000)
            cfg.voice.secret_key = ""
            self.assertFalse((await server.api_current_voice())["tts_enabled"])

    async def test_cloud_runtime_sync_is_local_and_uses_current_model_and_voice(self):
        local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        remote = SimpleNamespace(client=SimpleNamespace(host="203.0.113.9"))
        cfg = Config()
        cfg.llm.protocol = "anthropic"
        cfg.llm.base_url = "https://model.example/v1"
        cfg.llm.chat_model = "model-new"
        cfg.llm.models = ["model-new", "model-second"]
        cfg.voice.secret_id = "voice-id"
        cfg.voice.secret_key = "voice-key"
        cfg.voice.region = "ap-guangzhou"
        cfg.voice.tts_voice = 601003
        with self.assertRaises(HTTPException):
            await server.api_cloud_runtime(remote)
        with patch.object(server, "CFG", cfg), \
             patch("backend.core.cloud_deploy.sync_runtime_settings") as sync:
            result = await server.api_cloud_runtime(local)
        self.assertTrue(result["ok"])
        values = sync.call_args.args[1]
        self.assertEqual("anthropic", values["AGENT_LLM_PROTOCOL"])
        self.assertEqual("model-new", values["AGENT_CHAT_MODEL"])
        self.assertEqual("model-new,model-second", values["AGENT_MODEL_OPTIONS"])
        self.assertEqual("voice-id", values["AGENT_TENCENT_SECRET_ID"])
        self.assertEqual("601003", values["AGENT_TTS_VOICE"])

    async def test_worker_channel_lock_reads_and_unlocks_cloud_scope(self):
        cfg = Config()
        cfg.role.mode = "worker"
        local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        old_locked = server.CHANNEL_LOCKED["locked"]
        server.CHANNEL_LOCKED["locked"] = True
        try:
            with patch.object(server, "CFG", cfg), \
                 patch("backend.core.cloud_deploy.channel_lock_status", return_value=True) as status:
                result = await server.api_channel_status()
            self.assertEqual({
                "locked": True,
                "local_locked": True,
                "cloud_locked": True,
                "scope": "cloud",
            }, result)
            status.assert_called_once()

            with patch.object(server, "CFG", cfg), \
                 patch("backend.core.cloud_deploy.unlock_channel") as unlock, \
                 patch.object(server, "_save_channel_lock") as save:
                result = await server.api_channel_unlock(local)
            self.assertTrue(result["ok"])
            self.assertEqual("cloud", result["scope"])
            unlock.assert_called_once()
            save.assert_called_once_with(False)
            self.assertFalse(server.CHANNEL_LOCKED["locked"])
        finally:
            server.CHANNEL_LOCKED["locked"] = old_locked

    async def test_tool_catalog_groups_registered_tools(self):
        registry = ToolRegistry()

        async def noop():
            return "ok"

        registry.register(Tool("scroll_screen", "滚动窗口", {"type": "object"}, noop))
        registry.register(Tool("code_verify", "验证代码", {"type": "object"}, noop))
        with patch.object(server, "REGISTRY", registry):
            result = await server.api_tools_catalog()
        self.assertEqual(2, result["total"])
        groups = {group["id"]: group for group in result["groups"]}
        self.assertEqual("scroll_screen", groups["computer"]["tools"][0]["name"])
        self.assertEqual("code_verify", groups["code"]["tools"][0]["name"])

    def test_agent_events_are_persisted_with_approval_result(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-server-audit-") as tmp:
            audit = OperationAuditLog(Path(tmp) / "operations.sqlite3")
            with patch.object(server, "AUDIT_LOG", audit):
                trace = server._AuditTrace("desktop-session", "task-1", "risk")
                trace.consume(SimpleNamespace(type="tool_call", data={
                    "name": "click_coordinate", "arguments": {"x": 10, "y": 20},
                }))
                trace.consume(SimpleNamespace(type="confirm_needed", data={
                    "name": "click_coordinate",
                }))
                trace.consume(SimpleNamespace(type="tool_result", data={
                    "name": "click_coordinate", "result": "用户拒绝了该操作。", "ok": False,
                }))
            item = audit.query()["items"][0]
            self.assertEqual("computer", item["category"])
            self.assertEqual("rejected", item["status"])
            self.assertEqual("denied", item["approval_result"])

    def test_history_save_is_atomic_and_bounded(self):
        old_path = server._HISTORY_PATH
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoa-history-") as tmp:
                server._HISTORY_PATH = Path(tmp) / "chat_history.json"
                history = [{"role": "user", "content": str(i)} for i in range(550)]
                self.assertTrue(server.save_chat_history(history))
                saved = json.loads(server._HISTORY_PATH.read_text(encoding="utf-8"))
                self.assertEqual(500, len(saved))
                self.assertEqual("50", saved[0]["content"])
                self.assertFalse(server._HISTORY_PATH.with_suffix(".tmp").exists())
        finally:
            server._HISTORY_PATH = old_path

    def test_attachment_cleanup_preserves_history_and_removes_old_orphans(self):
        old_dir = server._ATTACHMENT_DIR
        old_history = server.DESKTOP_HISTORY
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoa-cleanup-") as tmp:
                server._ATTACHMENT_DIR = Path(tmp)
                keep = Path(tmp) / "keep.txt"
                old = Path(tmp) / "old.txt"
                keep.write_text("keep", encoding="utf-8")
                old.write_text("old", encoding="utf-8")
                stale = time.time() - server._ATTACHMENT_RETENTION_SECONDS - 1
                os.utime(keep, (stale, stale))
                os.utime(old, (stale, stale))
                server.DESKTOP_HISTORY = [{"attachments": [{"saved_name": keep.name}]}]
                self.assertEqual(1, server._cleanup_attachments())
                self.assertTrue(keep.exists())
                self.assertFalse(old.exists())
        finally:
            server._ATTACHMENT_DIR = old_dir
            server.DESKTOP_HISTORY = old_history

    def test_persisted_screenshot_stores_only_file_metadata(self):
        old_dir = server._ATTACHMENT_DIR
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoa-screenshot-") as tmp:
                server._ATTACHMENT_DIR = Path(tmp)
                encoded = base64.b64encode(b"jpeg-bytes").decode("ascii")
                item = server._save_persisted_screenshot(encoded)
                self.assertIsNotNone(item)
                self.assertEqual("image/jpeg", item["type"])
                self.assertNotIn("data", item)
                saved = Path(tmp) / item["saved_name"]
                self.assertEqual(b"jpeg-bytes", saved.read_bytes())
                self.assertIsNone(server._save_persisted_screenshot("not-base64"))
        finally:
            server._ATTACHMENT_DIR = old_dir

    def test_sessions_and_pending_replies_expire(self):
        old_sessions = server._SESSIONS.copy()
        old_replies = server.PENDING_REPLIES.copy()
        try:
            server._SESSIONS.clear()
            server._SESSIONS["expired"] = 10
            self.assertFalse(server._session_valid("expired"))
            server.PENDING_REPLIES.clear()
            server.PENDING_REPLIES["expired"] = {"_created_at": 10}
            server._prune_pending_replies(now=10 + server._PENDING_REPLY_TTL_SECONDS + 1)
            self.assertNotIn("expired", server.PENDING_REPLIES)
        finally:
            server._SESSIONS.clear()
            server._SESSIONS.update(old_sessions)
            server.PENDING_REPLIES.clear()
            server.PENDING_REPLIES.update(old_replies)

    def test_idle_channel_state_is_pruned(self):
        target = "old-channel-test"
        server.CHANNEL_HISTORY[("feishu", target)] = [{"role": "user", "content": "old"}]
        server.CHANNEL_RESUME[("feishu", target)] = [{"role": "user", "content": "resume"}]
        server.CHANNEL_LOCKS[("feishu", target)] = asyncio.Lock()
        server.CHANNEL_LAST_USED[("feishu", target)] = 10
        server._prune_channel_state(now=10 + server._CHANNEL_STATE_TTL_SECONDS + 1)
        self.assertNotIn(("feishu", target), server.CHANNEL_HISTORY)
        self.assertNotIn(("feishu", target), server.CHANNEL_RESUME)
        self.assertNotIn(("feishu", target), server.CHANNEL_LOCKS)
        self.assertNotIn(("feishu", target), server.CHANNEL_LAST_USED)

    def test_client_input_ids_are_validated_and_completed_ids_expire(self):
        old_completed = server._COMPLETED_CLIENT_INPUTS.copy()
        try:
            server._COMPLETED_CLIENT_INPUTS.clear()
            server._COMPLETED_CLIENT_INPUTS["old-id"] = {"completed_at": 10, "duration_ms": 500}
            server._prune_client_inputs(now=10 + server._CLIENT_INPUT_TTL_SECONDS + 1)
            self.assertNotIn("old-id", server._COMPLETED_CLIENT_INPUTS)
            self.assertEqual("safe-id_1", server._client_input_id("safe-id_1"))
            self.assertEqual("", server._client_input_id("bad id"))
        finally:
            server._COMPLETED_CLIENT_INPUTS.clear()
            server._COMPLETED_CLIENT_INPUTS.update(old_completed)

    async def test_websocket_delivery_ack_duration_and_duplicate_suppression(self):
        class FakeAgent:
            runs = 0
            abort_fns = []

            def __init__(self, *_args, **_kwargs):
                self.memory = None
                self.abort_fns.append(_kwargs.get("abort_fn"))

            async def run(self, *_args, **_kwargs):
                FakeAgent.runs += 1
                yield SimpleNamespace(type="message_end", data={"content": "测试完成"})
                yield SimpleNamespace(type="done", data={})

        class FakeWebSocket:
            def __init__(self, message: dict, terminal_type: str):
                self.headers = {"origin": "", "host": "127.0.0.1:1985"}
                self.query_params = {}
                self.cookies = {}
                self.client = SimpleNamespace(host="127.0.0.1")
                self.message = json.dumps(message, ensure_ascii=False)
                self.terminal_type = terminal_type
                self.sent: list[dict] = []
                self.accepted = False
                self._received = False
                self._terminal = asyncio.Event()

            async def accept(self):
                self.accepted = True

            async def close(self, code=None):
                del code

            async def send_text(self, payload: str):
                item = json.loads(payload)
                self.sent.append(item)
                if item.get("type") == self.terminal_type:
                    self._terminal.set()

            async def receive_text(self):
                if not self._received:
                    self._received = True
                    return self.message
                await asyncio.wait_for(self._terminal.wait(), timeout=2)
                raise WebSocketDisconnect()

        client_id = "delivery-test-id"
        message = {
            "type": "user_input", "client_id": client_id, "text": "测试可靠投递",
            "provider_id": "default", "model": server.CFG.llm.chat_model,
        }
        old_history = server.DESKTOP_HISTORY
        old_path = server._HISTORY_PATH
        old_active = server._ACTIVE_CLIENT_INPUTS.copy()
        old_completed = server._COMPLETED_CLIENT_INPUTS.copy()
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoa-delivery-") as tmp:
                server.DESKTOP_HISTORY = []
                server._HISTORY_PATH = Path(tmp) / "history.json"
                server._ACTIVE_CLIENT_INPUTS.clear()
                server._COMPLETED_CLIENT_INPUTS.clear()
                first = FakeWebSocket(message, "done")
                with patch.object(server, "Agent", FakeAgent):
                    await server.ws_endpoint(first)
                ack = next(item for item in first.sent if item["type"] == "input_ack")
                done = next(item for item in first.sent if item["type"] == "done")
                self.assertTrue(first.accepted)
                self.assertEqual(client_id, ack["data"]["client_id"])
                self.assertEqual(client_id, done["data"]["client_id"])
                self.assertGreaterEqual(done["data"]["duration_ms"], 0)
                self.assertTrue(callable(FakeAgent.abort_fns[0]))
                self.assertFalse(FakeAgent.abort_fns[0]())

                second = FakeWebSocket(message, "input_done")
                with patch.object(server, "Agent", FakeAgent):
                    await server.ws_endpoint(second)
                duplicate = next(item for item in second.sent if item["type"] == "input_done")
                self.assertEqual(client_id, duplicate["data"]["client_id"])
                self.assertEqual(1, FakeAgent.runs)
        finally:
            server.DESKTOP_HISTORY = old_history
            server._HISTORY_PATH = old_path
            server._ACTIVE_CLIENT_INPUTS.clear()
            server._ACTIVE_CLIENT_INPUTS.update(old_active)
            server._COMPLETED_CLIENT_INPUTS.clear()
            server._COMPLETED_CLIENT_INPUTS.update(old_completed)

    async def test_llm_error_completes_delivery_and_clears_busy_state(self):
        class FailingAgent:
            runs = 0

            def __init__(self, *_args, **_kwargs):
                self.memory = None

            async def run(self, *_args, **_kwargs):
                FailingAgent.runs += 1
                yield SimpleNamespace(type="error", data={
                    "where": "memory.recall", "msg": "记忆暂时不可用",
                })
                yield SimpleNamespace(type="checkpoint", data={
                    "resume_messages": [{"role": "user", "content": "测试接口失败"}],
                    "reason": "llm_error",
                })
                yield SimpleNamespace(type="error", data={
                    "where": where, "msg": message_text,
                })

        class FakeWebSocket:
            def __init__(self, message: dict):
                self.headers = {"origin": "", "host": "127.0.0.1:1985"}
                self.query_params = {}
                self.cookies = {}
                self.client = SimpleNamespace(host="127.0.0.1")
                self.message = json.dumps(message, ensure_ascii=False)
                self.sent: list[dict] = []
                self._received = False
                self._terminal = asyncio.Event()

            async def accept(self):
                pass

            async def close(self, code=None):
                del code

            async def send_text(self, payload: str):
                item = json.loads(payload)
                self.sent.append(item)
                if item.get("type") == "input_done" or item.get("data", {}).get("task_finished"):
                    self._terminal.set()

            async def receive_text(self):
                if not self._received:
                    self._received = True
                    return self.message
                await asyncio.wait_for(self._terminal.wait(), timeout=2)
                raise WebSocketDisconnect()

        cases = (
            ("llm.chat", "网关返回 500"),
            ("llm.config", '网关返回 400: {"reason":"INVALID_MODEL_ID"}'),
            ("llm.config", "网关返回 401: 密钥无效"),
            ("llm.config", "当前模型不支持工具调用"),
        )
        for where, message_text in cases:
            with self.subTest(where=where, message=message_text), \
                 tempfile.TemporaryDirectory(prefix="xiaoa-llm-error-") as tmp, \
                 patch.object(server, "DESKTOP_HISTORY", []), \
                 patch.object(server, "DESKTOP_CONVERSATIONS", {"default": server._new_conversation("default")}), \
                 patch.object(server, "_HISTORY_PATH", Path(tmp) / "history.json"), \
                 patch.object(server, "_CONVERSATIONS_PATH", Path(tmp) / "conversations.json"), \
                 patch.object(server, "_RESUME_PATH", Path(tmp) / "resume.json"), \
                 patch.object(server, "_RESUME_STATES", {}), \
                 patch.object(server, "_ACTIVE_CLIENT_INPUTS", {}), \
                 patch.object(server, "_COMPLETED_CLIENT_INPUTS", {}), \
                 patch.object(server, "Agent", FailingAgent):
                client_id = "llm-error-test-id"
                message = {"type": "user_input", "client_id": client_id, "text": "测试接口失败"}
                ws = FakeWebSocket(message)
                await server.ws_endpoint(ws)
                errors = [item["data"] for item in ws.sent if item["type"] == "error"]
                self.assertEqual(2, len(errors))
                self.assertNotIn("task_finished", errors[0])
                self.assertNotIn("client_id", errors[0])
                error = errors[1]
                self.assertEqual(where, error["where"])
                self.assertEqual(client_id, error["client_id"])
                self.assertTrue(error["task_finished"])
                self.assertEqual(where == "llm.chat", bool(error.get("service_failed")))
                self.assertGreaterEqual(error["duration_ms"], 0)
                self.assertIn(client_id, server._COMPLETED_CLIENT_INPUTS)
                self.assertNotIn(client_id, server._ACTIVE_CLIENT_INPUTS)
                self.assertTrue(server._RESUME_STATES["desktop"]["messages"])
                self.assertFalse(any(item["type"] == "done" for item in ws.sent))
                # 重连补发同一 client_id 只回完成确认，不再次执行失败的任务。
                runs = FailingAgent.runs
                replay = FakeWebSocket(message)
                await server.ws_endpoint(replay)
                self.assertEqual(runs, FailingAgent.runs)
                self.assertTrue(any(item["type"] == "input_done" for item in replay.sent))

    async def test_busy_message_interjects_and_cancels_pending_confirmation(self):
        class ConfirmingAgent:
            allowed = None
            inserted = []

            def __init__(self, *_args, confirm_fn=None, interjection_fn=None, **_kwargs):
                self.memory = None
                self.confirm_fn = confirm_fn
                self.interjection_fn = interjection_fn

            async def run(self, *_args, **_kwargs):
                yield SimpleNamespace(type="confirm_needed", data={
                    "name": "code_run", "arguments": {"command": "touch /tmp/a"},
                    "reason": "risk",
                })
                ConfirmingAgent.allowed = await self.confirm_fn("code_run", {})
                ConfirmingAgent.inserted = self.interjection_fn()
                yield SimpleNamespace(type="message", data={"content": "已按新要求继续。"})
                yield SimpleNamespace(type="done", data={})

        class FakeWebSocket:
            def __init__(self):
                self.headers = {"origin": "", "host": "127.0.0.1:1985"}
                self.query_params = {}
                self.cookies = {}
                self.client = SimpleNamespace(host="127.0.0.1")
                self.sent: list[dict] = []
                self.received = 0
                self.confirm_seen = asyncio.Event()
                self.done_seen = asyncio.Event()

            async def accept(self):
                pass

            async def close(self, code=None):
                del code

            async def send_text(self, payload: str):
                item = json.loads(payload)
                self.sent.append(item)
                if item.get("type") == "confirm_needed":
                    self.confirm_seen.set()
                if item.get("type") == "done":
                    self.done_seen.set()

            async def receive_text(self):
                self.received += 1
                if self.received == 1:
                    return json.dumps({
                        "type": "user_input", "client_id": "initial-task",
                        "text": "先执行原任务",
                    })
                if self.received == 2:
                    await asyncio.wait_for(self.confirm_seen.wait(), timeout=2)
                    return json.dumps({
                        "type": "user_input", "client_id": "correction-task",
                        "text": "等等，先做 X",
                    })
                await asyncio.wait_for(self.done_seen.wait(), timeout=2)
                raise WebSocketDisconnect()

        old_history = server.DESKTOP_HISTORY
        old_path = server._HISTORY_PATH
        old_active = server._ACTIVE_CLIENT_INPUTS.copy()
        old_completed = server._COMPLETED_CLIENT_INPUTS.copy()
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoa-interject-") as tmp:
                server.DESKTOP_HISTORY = []
                server._HISTORY_PATH = Path(tmp) / "history.json"
                server._ACTIVE_CLIENT_INPUTS.clear()
                server._COMPLETED_CLIENT_INPUTS.clear()
                ws = FakeWebSocket()
                with patch.object(server, "Agent", ConfirmingAgent):
                    await server.ws_endpoint(ws)
                inserted = next(item for item in ws.sent if item["type"] == "interjected")
                self.assertTrue(inserted["data"]["confirmation_cancelled"])
                self.assertEqual("correction-task", inserted["data"]["client_id"])
                self.assertFalse(ConfirmingAgent.allowed)
                self.assertEqual(["等等，先做 X"], ConfirmingAgent.inserted)
                self.assertFalse(any(item["type"] == "queued" for item in ws.sent))
                self.assertIn("correction-task", server._COMPLETED_CLIENT_INPUTS)
        finally:
            server.DESKTOP_HISTORY = old_history
            server._HISTORY_PATH = old_path
            server._ACTIVE_CLIENT_INPUTS.clear()
            server._ACTIVE_CLIENT_INPUTS.update(old_active)
            server._COMPLETED_CLIENT_INPUTS.clear()
            server._COMPLETED_CLIENT_INPUTS.update(old_completed)


class ContextExhaustedResumeTests(unittest.IsolatedAsyncioTestCase):
    setUp = _isolate_desktop_history

    async def _run_truncated(self, reason):
        done_flag = asyncio.Event()

        class TruncatedAgent:
            def __init__(self, *_args, **kwargs):
                self.memory = None
                self.abort_fn = kwargs.get("abort_fn", lambda: False)

            async def run(self, text, **kwargs):
                del kwargs
                yield SimpleNamespace(type="truncated", data={
                    "reason": reason,
                    "resume_messages": [
                        {"role": "system", "content": "rules"},
                        {"role": "user", "content": text},
                    ],
                    "steps_done": 1,
                })
                yield SimpleNamespace(type="done", data={"truncated": True})
                done_flag.set()

        class FakeWS:
            def __init__(self):
                self.headers = {"origin": "", "host": "127.0.0.1:1985"}
                self.query_params = {}
                self.cookies = {}
                self.client = SimpleNamespace(host="127.0.0.1")
                self.sent = []
                self.receive_count = 0

            async def accept(self):
                pass

            async def close(self, code=None):
                del code

            async def send_text(self, payload):
                self.sent.append(json.loads(payload))

            async def receive_text(self):
                self.receive_count += 1
                if self.receive_count == 1:
                    return json.dumps({"type": "user_input", "client_id": "c1", "text": "测试任务"})
                await asyncio.wait_for(done_flag.wait(), 2)
                raise WebSocketDisconnect()

        ws = FakeWS()
        old_history = server.DESKTOP_HISTORY
        old_hpath = server._HISTORY_PATH
        old_resume = server._RESUME_STATES.copy()
        old_rpath = server._RESUME_PATH
        old_active = server._ACTIVE_CLIENT_INPUTS.copy()
        old_completed = server._COMPLETED_CLIENT_INPUTS.copy()
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoa-context-exhausted-") as tmp, \
                 patch.object(server, "Agent", TruncatedAgent):
                server.DESKTOP_HISTORY = []
                server._HISTORY_PATH = Path(tmp) / "history.json"
                server._RESUME_STATES.clear()
                server._RESUME_PATH = Path(tmp) / "resume_states.json"
                server._ACTIVE_CLIENT_INPUTS.clear()
                server._COMPLETED_CLIENT_INPUTS.clear()
                await server.ws_endpoint(ws)
            return ws.sent, dict(server._RESUME_STATES)
        finally:
            server.DESKTOP_HISTORY = old_history
            server._HISTORY_PATH = old_hpath
            server._RESUME_STATES.clear()
            server._RESUME_STATES.update(old_resume)
            server._RESUME_PATH = old_rpath
            server._ACTIVE_CLIENT_INPUTS.clear()
            server._ACTIVE_CLIENT_INPUTS.update(old_active)
            server._COMPLETED_CLIENT_INPUTS.clear()
            server._COMPLETED_CLIENT_INPUTS.update(old_completed)

    async def test_context_exhausted_leaves_checkpoint(self):
        sent, states = await self._run_truncated("context_exhausted")
        self.assertTrue(states)
        self.assertEqual("context_exhausted", next(iter(states.values()))["reason"])
        self.assertTrue(any(item.get("type") == "truncated" for item in sent))

    async def test_context_limit_leaves_checkpoint(self):
        sent, states = await self._run_truncated("context_limit")
        self.assertTrue(states)  # 留了断点
        self.assertEqual("context_limit", next(iter(states.values()))["reason"])
        self.assertTrue(any(item.get("type") == "truncated" for item in sent))


class VisionModelRoutingTests(unittest.TestCase):
    def test_friendly_gateway_error_for_invalid_model_id(self):
        from backend.core.llm import _friendly_gateway_error

        message = "Invalid model ID. Please select a different model to continue."
        for body in (
            json.dumps({"message": message, "reason": "INVALID_MODEL_ID"}),
            json.dumps({"error": {"message": message, "code": "INVALID_MODEL_ID"}}),
            json.dumps({"message": message, "reason": "INVALID_MODEL_ID"}).encode(),
        ):
            with self.subTest(body=body):
                msg = _friendly_gateway_error(400, body)
                self.assertIn("400", msg)
                self.assertIn("模型 ID 无效或在此接口不可用", msg)
                self.assertIn("从上游探测模型", msg)
                self.assertIn("INVALID_MODEL_ID", msg)
                self.assertIn(message, msg)

    def test_friendly_gateway_error_for_vision_capability(self):
        from backend.core.llm import _friendly_gateway_error

        msg = _friendly_gateway_error(
            400, '{"error":{"code":"MODEL_CAPABILITY_NOT_SUPPORTED","message":"image not supported"}}',
        )
        self.assertIn("不支持看图", msg)
        self.assertIn("切换", msg)

    def test_friendly_gateway_error_for_tool_capability(self):
        from backend.core.llm import _friendly_gateway_error

        msg = _friendly_gateway_error(
            400, '{"error":{"code":"MODEL_CAPABILITY_NOT_SUPPORTED","message":"tool_calls not supported for this model"}}',
        )
        self.assertIn("工具调用", msg)
        self.assertNotIn("看图", msg)

    def test_friendly_gateway_error_unknown_capability_carries_original(self):
        from backend.core.llm import _friendly_gateway_error

        original = "some_custom_capability"
        msg = _friendly_gateway_error(
            400, f'{{"error":{{"code":"MODEL_CAPABILITY_NOT_SUPPORTED","message":"{original}"}}}}',
        )
        self.assertIn("某项能力", msg)
        self.assertIn(original, msg)


class VisionModelSaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_vision_model_follows_first_default_model(self):
        class FakeClient:
            def __init__(self, _cfg):
                pass

            async def aclose(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.toml"
            cfg = Config()
            cfg.llm.vision_model = "deleted-vision-model"
            request = server.SettingsRequest(providers=[server.ProviderSettingsRequest(
                id="default", name="默认接口", base_url="https://model.example/v1",
                models=["first-model"],
            )])
            with patch.object(server, "CFG", cfg), \
                 patch.object(server, "LLM", FakeClient(cfg.llm)), \
                 patch.object(server, "LLMClient", FakeClient), \
                 patch.object(server, "config_path", return_value=target):
                result = await server.api_save_settings(request)

            self.assertTrue(result["ok"])
            self.assertEqual("first-model", cfg.llm.vision_model)


class AttachmentCompressionTests(unittest.TestCase):
    def test_image_attachment_is_compressed_original_kept(self):
        import io as _io
        import random
        from PIL import Image
        from backend.api import server

        img = Image.new("RGB", (1400, 1400))
        px = img.load()
        random.seed(7)
        for y in range(0, 1400, 2):
            for x in range(0, 1400, 2):
                v = random.randint(0, 255)
                px[x, y] = (v, v, v)
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        original = buf.getvalue()
        data_url = "data:image/png;base64," + base64.b64encode(original).decode()
        items = [{"name": "big.png", "data": data_url}]
        with patch.object(server, "_ATTACHMENT_DIR", Path(tempfile.gettempdir()) / "xiaoa-test-att"), \
             patch.object(server, "_cleanup_attachments", lambda: None):
            prepared = server._prepare_attachments(items)
        item = prepared[0]
        self.assertTrue(item["data_url"].startswith("data:image/jpeg"), "要压成 JPEG 再发模型")
        compressed = base64.b64decode(item["data_url"].split(",", 1)[1])
        self.assertLess(len(compressed), len(original), "压缩版应远小于原图")
        self.assertTrue(Path(item["path"]).is_file(), "原图仍要按原样存盘")

    def test_image_budget_caps_to_recent_with_note(self):
        from backend.api import server

        prepared = []
        for i in range(6):
            prepared.append({"name": f"{i}.jpg", "data_url": "data:image/jpeg;base64," + "A" * 2000})
        with patch.object(server.CFG, "agent", SimpleNamespace(context_window=4000)):
            server._cap_attachment_image_budget(prepared)
        kept = sum(1 for item in prepared if item.get("data_url"))
        self.assertEqual(3, kept, "只保留最近的几张(4000 窗口 × 40% / 500 tokens/张)")
        self.assertTrue(any(item.get("note") for item in prepared), "要明确告诉用户裁了几张")

    def test_prepare_attachments_caps_image_budget_end_to_end(self):
        """44.7:兜底真的接进了 _prepare_attachments(不是只测了函数本身)。"""
        from backend.api import server

        items = []
        for i in range(6):
            items.append({"name": f"{i}.png", "data": "data:image/png;base64,AAAA"})
        with patch.object(server, "_compress_attachment_image",
                          return_value="data:image/jpeg;base64," + "A" * 2000), \
             patch.object(server, "_ATTACHMENT_DIR", Path(tempfile.gettempdir()) / "xiaoa-test-att3"), \
             patch.object(server, "_cleanup_attachments", lambda: None), \
             patch.object(server.CFG, "agent", SimpleNamespace(context_window=4000)):
            prepared = server._prepare_attachments(items)
        kept = [item for item in prepared if item.get("data_url")]
        dropped = [item for item in prepared if not item.get("data_url")]
        self.assertEqual(3, len(kept), "只保留最近的 3 张")
        self.assertEqual(3, len(dropped), "裁掉最旧的 3 张")
        self.assertTrue(all(item.get("note") for item in dropped), "裁掉的要有 note 说明")
        self.assertTrue(all(not item.get("data_url") for item in dropped), "裁掉的不带 data_url")


if __name__ == "__main__":
    unittest.main()
