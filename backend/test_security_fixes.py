from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend.api import server
from backend.core.agent import Agent, _looks_private
from backend.core.config import (
    AgentConfig, Config, LLMProviderConfig, PROVIDER_API_KEYS_ENV, SECRET_ENV_FIELDS,
    apply_env_overrides, load_config, plaintext_secret_env_vars,
    provider_api_key_env_overrides, provider_api_key_env_warning,
    subprocess_env_without_secrets,
)
from backend.core.tools import Tool, ToolRegistry
from backend.modules.computer.browser_module import BrowserController
from backend.modules.computer.code_module import _reject_protected
from backend.modules.computer.tool_market_module import ToolMarketManager
from backend.modules.computer.user_tool_module import UserToolManager
from backend.modules.computer import system_control, user_tool_module
from backend.modules.chat import feishu_module
from backend.modules.office import docgen_module, fs_module


class _LLM:
    def __init__(self, name: str = "dangerous_write", arguments: str = "{}"):
        self.turn = 0
        self.name = name
        self.arguments = arguments

    async def chat_stream_tools(self, messages, tools=None, **kwargs):
        self.turn += 1
        if self.turn > 1:
            yield "done", {"role": "assistant", "content": "已取消"}
            return
        yield "done", {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": self.name, "arguments": self.arguments},
            }],
        }


class _Response(io.BytesIO):
    headers = {"Content-Length": "0"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class SecurityFixTests(unittest.IsolatedAsyncioTestCase):
    async def test_unicode_password_login_and_secret_comparison(self):
        self.assertTrue(server._matches_secret("中文口令", "中文口令"))
        self.assertFalse(server._matches_secret(None, "configured"))
        self.assertFalse(server._matches_secret("bad", "configured"))
        cfg = Config()
        cfg.auth.password = "中文口令"
        request = SimpleNamespace(url=SimpleNamespace(scheme="http"))
        with patch.object(server, "CFG", cfg), patch.object(server, "_new_session", return_value="test-session"):
            response = await server.api_login(server.LoginRequest(password="中文口令"), request)
            rejected = await server.api_login(server.LoginRequest(password="wrong"), request)
        self.assertEqual(200, response.status_code)
        self.assertEqual(401, rejected.status_code)

    def test_ui_token_filter_redacts_formatted_url_without_losing_other_fields(self):
        import logging
        record = logging.LogRecord("uvicorn.error", 20, "", 0,
                                   '%s - "WebSocket %s" [accepted]',
                                   ("127.0.0.1", "/ws?ui_token=test-secret&other=ok"), None)
        self.assertTrue(server._UITokenLogFilter().filter(record))
        self.assertNotIn("test-secret", record.getMessage())
        self.assertIn("&other=ok", record.getMessage())
        self.assertEqual(2, len(record.args))

    async def test_agent_records_model_and_actual_tool_timings(self):
        registry = ToolRegistry()
        async def query():
            await asyncio.sleep(0.002)
            return {"ok": True}
        registry.register(Tool("query", "read", {"type": "object", "properties": {}}, query))
        events = [event async for event in Agent(_LLM("query"), registry, AgentConfig()).run("查询")]
        timings = [event.data for event in events if event.type == "llm_timing"]
        self.assertEqual([1, 2], [item["step"] for item in timings])
        self.assertTrue(all(item["llm_ms"] >= 0 for item in timings))
        result = next(event.data for event in events if event.type == "tool_result")
        self.assertGreaterEqual(result["tool_ms"], 1)

    async def test_agent_forwards_retry_without_treating_it_as_a_model_message(self):
        class RetryLLM:
            async def chat_stream_tools(self, *_args, **_kwargs):
                yield "retry", {"attempt": 2, "text": "网关返回空流，2 秒后重试（2/5）。"}
                yield "done", {"role": "assistant", "content": "完成"}
        events = [event async for event in Agent(RetryLLM(), ToolRegistry(), AgentConfig()).run("你好")]
        retry = next(event.data for event in events if event.type == "llm_retry")
        self.assertEqual(1, retry["step"])
        self.assertEqual(2, retry["attempt"])
        self.assertFalse(any(event.type == "error" for event in events))
        self.assertTrue(any(event.type == "message" and event.data["content"] == "完成" for event in events))

    def test_load_config_ignores_removed_fields(self):
        """任务40 删了 auto_continue_rounds;旧 config.toml 残留该字段时不能崩(云端 hub 崩过)。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[agent]\nmax_steps = 25\nauto_continue_rounds = 3\n",
                encoding="utf-8",
            )
            cfg = load_config(path)  # 不抛 TypeError 即通过
        self.assertEqual(25, cfg.agent.max_steps)
        self.assertFalse(hasattr(cfg.agent, "auto_continue_rounds"))

    def test_invalid_provider_key_environment_is_ignored_with_safe_warning(self):
        raw = '{"gateway":"super-secret"'
        cfg = Config()
        cfg.llm.providers = [LLMProviderConfig(
            id="gateway", api_key="file-key", base_url="https://example.test/v1", models=["model"],
        )]
        try:
            with patch.dict(os.environ, {PROVIDER_API_KEYS_ENV: raw}, clear=False), \
                 self.assertLogs("backend.core.config", level="WARNING") as logs:
                apply_env_overrides(cfg)
            self.assertEqual("file-key", cfg.llm.providers[0].api_key)
            self.assertIn(PROVIDER_API_KEYS_ENV, logs.output[0])
            self.assertIn('{"provider-id":"key"}', logs.output[0])
            self.assertNotIn("super-secret", logs.output[0])
            self.assertTrue(provider_api_key_env_warning())
        finally:
            with patch.dict(os.environ, {PROVIDER_API_KEYS_ENV: ""}, clear=False):
                provider_api_key_env_overrides()

    def test_llm_protocol_environment_override_is_validated(self):
        with patch.dict(os.environ, {"AGENT_LLM_PROTOCOL": "anthropic"}, clear=False):
            self.assertEqual("anthropic", apply_env_overrides(Config()).llm.protocol)
        with patch.dict(os.environ, {"AGENT_LLM_PROTOCOL": "invalid"}, clear=False):
            self.assertEqual("openai", apply_env_overrides(Config()).llm.protocol)

    def test_password_env_marker_survives_rewrite_and_missing_environment(self):
        env = {name: "" for name in SECRET_ENV_FIELDS}
        env["AGENT_WEB_PASSWORD"] = "managed-password"
        env[PROVIDER_API_KEYS_ENV] = ""
        with tempfile.TemporaryDirectory(prefix="xiaoa-password-marker-") as tmp:
            path = Path(tmp) / "config.toml"
            with patch.dict(os.environ, env, clear=False):
                cfg = apply_env_overrides(Config())
                with patch.object(server, "CFG", cfg), patch.object(server, "config_path", return_value=path):
                    server._rewrite_config()
            persisted = load_config(path)
            self.assertEqual("", persisted.auth.password)
            self.assertTrue(persisted.auth.password_env_managed)

            with patch.dict(os.environ, {"AGENT_WEB_PASSWORD": ""}, clear=False):
                missing_env = apply_env_overrides(load_config(path))
                with patch.object(server, "CFG", missing_env):
                    self.assertIn("AGENT_WEB_PASSWORD", server._password_env_warning())

                missing_env.auth.password = "new-file-password"
                missing_env.auth.password_env_managed = False
                with patch.object(server, "CFG", missing_env), \
                     patch.object(server, "config_path", return_value=path):
                    server._rewrite_config()
            replaced = load_config(path)
            self.assertEqual("new-file-password", replaced.auth.password)
            self.assertFalse(replaced.auth.password_env_managed)

    def test_standalone_non_loopback_binding_requires_password(self):
        self.assertTrue(server._host_is_loopback("localhost"))
        self.assertTrue(server._host_is_loopback("127.0.0.1"))
        self.assertTrue(server._host_is_loopback("::1"))
        self.assertFalse(server._host_is_loopback("0.0.0.0"))
        cfg = Config()
        cfg.server.host = "0.0.0.0"
        with patch.object(server, "CFG", cfg):
            with self.assertRaisesRegex(RuntimeError, "AGENT_WEB_PASSWORD"):
                server._validate_startup_auth()
            cfg.auth.password = "configured"
            server._validate_startup_auth()

    def test_user_tool_environment_keeps_proxy_and_powershell_modules_only(self):
        values = {
            "HTTPS_PROXY": "http://proxy.example:8080",
            "NO_PROXY": "127.0.0.1",
            "PSModulePath": r"C:\Users\test\Documents\WindowsPowerShell\Modules",
            "AGENT_LLM_API_KEY": "must-not-leak",
        }
        with patch.dict(os.environ, values, clear=False):
            env = UserToolManager._minimal_env()
        self.assertEqual(values["HTTPS_PROXY"], env["HTTPS_PROXY"])
        self.assertEqual(values["NO_PROXY"], env["NO_PROXY"])
        self.assertEqual(values["PSModulePath"], env["PSModulePath"])
        self.assertNotIn("AGENT_LLM_API_KEY", env)

    def test_credential_file_reads_are_not_a_confirmation_category(self):
        for path in (r"D:\Agent-XiaoA\config.toml", r"D:\Agent-XiaoA\ui_token.txt"):
            with self.subTest(path=path):
                self.assertFalse(_looks_private("fs_read_text", {"path": path}))
        self.assertTrue(_looks_private("browser_upload", {"path": r"D:\Agent-XiaoA\.env"}))
        self.assertFalse(_looks_private("browser_upload", {"path": r"D:\Agent-XiaoA\statement.pdf"}))

    async def test_config_read_is_allowed_in_full_access_mode(self):
        registry = ToolRegistry()
        called = []

        async def read(path: str):
            called.append(path)
            return "secret"

        registry.register(Tool("fs_read_text", "读取文件", {"type": "object"}, read))
        reject = AsyncMock(return_value=False)
        agent = Agent(
            _LLM("fs_read_text", '{"path":"D:/Agent-XiaoA/config.toml"}'),
            registry, AgentConfig(confirm_before_action=False), confirm_fn=reject,
        )
        events = [event async for event in agent.run("读取配置", approval_mode="full")]
        self.assertEqual([r"D:/Agent-XiaoA/config.toml"], called)
        reject.assert_not_awaited()
        self.assertFalse(any(event.type == "confirm_needed" for event in events))

    async def test_credential_upload_still_requires_confirmation(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "browser_upload", "上传文件", {"type": "object"},
            AsyncMock(return_value="uploaded"), side_effect=True,
        ))
        confirm = AsyncMock(return_value=False)
        events = [event async for event in Agent(
            _LLM("browser_upload", '{"path":"D:/Agent-XiaoA/.env"}'),
            registry, AgentConfig(confirm_before_action=True), confirm_fn=confirm,
        ).run("上传配置", approval_mode="full")]
        confirm.assert_awaited_once()
        self.assertTrue(any(event.type == "confirm_needed" and event.data["reason"] == "privacy" for event in events))

    def test_all_plaintext_secrets_support_environment_overrides(self):
        values = {env_name: f"value-{index}" for index, env_name in enumerate((
            "AGENT_LLM_API_KEY",
            "AGENT_TENCENT_SECRET_ID", "AGENT_TENCENT_SECRET_KEY",
            "AGENT_FEISHU_APP_ID", "AGENT_FEISHU_APP_SECRET",
            "AGENT_WEB_PASSWORD", "AGENT_HUB_TOKEN",
            "AGENT_CLOUD_BACKUP_ACCESS_KEY", "AGENT_CLOUD_BACKUP_SECRET_KEY",
        ))}
        values.update({
            "AGENT_TENCENT_REGION": "ap-guangzhou",
            "AGENT_TTS_VOICE": "601003",
            "AGENT_TTS_SPEED": "-2",
        })
        with patch.dict(os.environ, values, clear=False):
            cfg = apply_env_overrides(Config())
            self.assertEqual(values["AGENT_LLM_API_KEY"], cfg.llm.api_key)
            self.assertEqual(values["AGENT_TENCENT_SECRET_ID"], cfg.voice.secret_id)
            self.assertEqual(values["AGENT_TENCENT_SECRET_KEY"], cfg.voice.secret_key)
            self.assertEqual("ap-guangzhou", cfg.voice.region)
            self.assertEqual(601003, cfg.voice.tts_voice)
            self.assertEqual(-2, cfg.voice.tts_speed)
            self.assertTrue(cfg.voice.enabled)
            self.assertEqual(values["AGENT_FEISHU_APP_ID"], cfg.feishu.app_id)
            self.assertEqual(values["AGENT_FEISHU_APP_SECRET"], cfg.feishu.app_secret)
            self.assertEqual(values["AGENT_WEB_PASSWORD"], cfg.auth.password)
            self.assertEqual(values["AGENT_HUB_TOKEN"], cfg.role.hub_token)
            self.assertEqual(values["AGENT_CLOUD_BACKUP_ACCESS_KEY"], cfg.memory.cloud_backup.access_key)
            self.assertEqual(values["AGENT_CLOUD_BACKUP_SECRET_KEY"], cfg.memory.cloud_backup.secret_key)

    def test_provider_secret_override_and_subprocess_filter(self):
        cfg = Config()
        cfg.llm.providers = [LLMProviderConfig(
            id="backup-gateway", base_url="https://example.test/v1",
            api_key="file-provider-key", models=["model"],
        )]
        managed = {name: "managed-secret" for name in (
            "AGENT_LLM_API_KEY", "AGENT_TENCENT_SECRET_ID", "AGENT_TENCENT_SECRET_KEY",
            "AGENT_FEISHU_APP_ID", "AGENT_FEISHU_APP_SECRET", "AGENT_WEB_PASSWORD",
            "AGENT_HUB_TOKEN", "AGENT_CLOUD_BACKUP_ACCESS_KEY",
            "AGENT_CLOUD_BACKUP_SECRET_KEY",
        )}
        managed[PROVIDER_API_KEYS_ENV] = json.dumps({"backup-gateway": "env-provider-key"})
        with patch.dict(os.environ, {**managed, "XIAOA_TEST_SAFE": "kept"}, clear=False):
            self.assertIn(PROVIDER_API_KEYS_ENV, plaintext_secret_env_vars(cfg))
            apply_env_overrides(cfg)
            filtered = subprocess_env_without_secrets()
        self.assertEqual("env-provider-key", cfg.llm.providers[0].api_key)
        self.assertEqual("kept", filtered["XIAOA_TEST_SAFE"])
        self.assertFalse(set(managed) & set(filtered))

    async def test_run_command_does_not_inherit_managed_secrets(self):
        registry = ToolRegistry()
        with patch.object(system_control, "_IS_WINDOWS", True):
            system_control.register_tools(registry)
        completed = SimpleNamespace(stdout=b"ok", stderr=b"", returncode=0)
        def execute(*args, **kwargs):
            kwargs["stdout"].write(b"ok")
            kwargs["stdout"].flush()
            return completed
        with patch.dict(os.environ, {"AGENT_LLM_API_KEY": "must-not-leak"}, clear=False), \
             patch.object(system_control.subprocess, "run", side_effect=execute) as run:
            result = await registry.call("run_command", {"command": "echo ok"})
        self.assertEqual("ok", result)
        self.assertNotIn("AGENT_LLM_API_KEY", run.call_args.kwargs["env"])

    def test_plaintext_secret_warning_keeps_old_config_compatible(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-legacy-config-") as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[llm]\napi_key = "legacy-config-key"\n', encoding="utf-8")
            with patch.dict(os.environ, {"AGENT_LLM_API_KEY": ""}, clear=False):
                cfg = load_config(path)
                self.assertIn("AGENT_LLM_API_KEY", plaintext_secret_env_vars(cfg))
                self.assertEqual("legacy-config-key", apply_env_overrides(cfg).llm.api_key)
            with patch.dict(os.environ, {"AGENT_LLM_API_KEY": "environment-key"}, clear=False):
                cfg = load_config(path)
                self.assertIn("AGENT_LLM_API_KEY", plaintext_secret_env_vars(cfg))
                self.assertEqual("environment-key", apply_env_overrides(cfg).llm.api_key)

    def test_environment_secrets_are_not_rewritten_to_config(self):
        values = {
            "AGENT_LLM_API_KEY": "llm-key",
            "AGENT_TENCENT_SECRET_ID": "voice-id",
            "AGENT_TENCENT_SECRET_KEY": "voice-key",
            "AGENT_FEISHU_APP_ID": "feishu-id",
            "AGENT_FEISHU_APP_SECRET": "feishu-secret",
            "AGENT_WEB_PASSWORD": "web-password",
            "AGENT_HUB_TOKEN": "hub-token",
            "AGENT_CLOUD_BACKUP_ACCESS_KEY": "backup-access",
            "AGENT_CLOUD_BACKUP_SECRET_KEY": "backup-secret",
            PROVIDER_API_KEYS_ENV: json.dumps({"backup-gateway": "provider-env-key"}),
        }
        with tempfile.TemporaryDirectory(prefix="xiaoa-config-secrets-") as tmp, patch.dict(
            os.environ, values, clear=False,
        ):
            path = Path(tmp) / "config.toml"
            cfg = Config()
            cfg.llm.providers = [LLMProviderConfig(
                id="backup-gateway", base_url="https://example.test/v1",
                api_key="provider-file-key", models=["model"],
            )]
            self.assertIn(PROVIDER_API_KEYS_ENV, plaintext_secret_env_vars(cfg))
            cfg = apply_env_overrides(cfg)
            with (
                patch.object(server, "CFG", cfg),
                patch.object(server, "config_path", return_value=path),
            ):
                server._rewrite_config()
            persisted = load_config(path)
            content = path.read_text(encoding="utf-8")
            self.assertFalse(any(value in content for value in values.values()))
            self.assertEqual("", persisted.llm.api_key)
            self.assertEqual("", persisted.voice.secret_id)
            self.assertEqual("", persisted.voice.secret_key)
            self.assertEqual("", persisted.feishu.app_id)
            self.assertEqual("", persisted.feishu.app_secret)
            self.assertEqual("", persisted.auth.password)
            self.assertEqual("", persisted.role.hub_token)
            self.assertEqual("", persisted.memory.cloud_backup.access_key)
            self.assertEqual("", persisted.memory.cloud_backup.secret_key)
            self.assertEqual("", persisted.llm.providers[0].api_key)

    async def test_environment_secrets_remain_authoritative_after_settings_save(self):
        env = {
            "AGENT_LLM_API_KEY": "env-default-key",
            "AGENT_FEISHU_APP_ID": "env-feishu-id",
            "AGENT_FEISHU_APP_SECRET": "env-feishu-secret",
            "AGENT_WEB_PASSWORD": "env-web-password",
            PROVIDER_API_KEYS_ENV: json.dumps({"backup-gateway": "env-provider-key"}),
        }
        cfg = Config()
        cfg.llm.providers = [LLMProviderConfig(
            id="backup-gateway", name="Backup", base_url="https://example.test/v1",
            api_key="file-provider-key", models=["model"],
        )]
        sessions = {"active": 9999999999.0}
        with patch.dict(os.environ, env, clear=False):
            apply_env_overrides(cfg)
            request = server.SettingsRequest(
                providers=[
                    {"id": "default", "name": "Default", "base_url": "https://default.test/v1",
                     "api_key": "ui-default-key", "models": ["model"]},
                    {"id": "backup-gateway", "name": "Backup", "base_url": "https://example.test/v1",
                     "api_key": "ui-provider-key", "models": ["model"]},
                ],
                feishu_app_id="ui-feishu-id", feishu_app_secret="ui-feishu-secret",
                web_password="-",
            )
            with patch.object(server, "CFG", cfg), \
                 patch.object(server, "LLM", object()), \
                 patch.object(server, "LLMClient", return_value=object()), \
                 patch.object(server, "_retire_llm", new=AsyncMock()), \
                 patch.object(server, "_rewrite_config"), \
                 patch.object(server, "_SESSIONS", sessions):
                result = await server.api_save_settings(request)
        self.assertTrue(result["ok"])
        self.assertEqual("env-default-key", cfg.llm.api_key)
        self.assertEqual("env-provider-key", cfg.llm.providers[0].api_key)
        self.assertEqual("env-feishu-id", cfg.feishu.app_id)
        self.assertEqual("env-feishu-secret", cfg.feishu.app_secret)
        self.assertEqual("env-web-password", cfg.auth.password)
        self.assertEqual({"active": 9999999999.0}, sessions)
        self.assertEqual({
            "llm.default.api_key",
            "llm.providers.backup-gateway.api_key",
            "feishu.app_id",
            "feishu.app_secret",
            "auth.password",
        }, {item["field"] for item in result["ignored_env"]})
        self.assertIn("AGENT_WEB_PASSWORD", result["warning"])

    async def test_settings_expose_environment_managed_fields(self):
        env = {name: "" for name in SECRET_ENV_FIELDS}
        env.update({
            "AGENT_LLM_API_KEY": "default-key",
            "AGENT_TENCENT_SECRET_ID": "voice-id",
            "AGENT_TENCENT_SECRET_KEY": "voice-key",
            "AGENT_FEISHU_APP_ID": "feishu-id",
            "AGENT_FEISHU_APP_SECRET": "feishu-key",
            "AGENT_WEB_PASSWORD": "web-password",
            "AGENT_HUB_TOKEN": "hub-token",
            PROVIDER_API_KEYS_ENV: json.dumps({"backup-gateway": "provider-key"}),
        })
        cfg = Config()
        cfg.llm.providers = [LLMProviderConfig(
            id="backup-gateway", name="Backup", base_url="https://example.test/v1",
            api_key="file-key", models=["model"],
        )]
        with patch.dict(os.environ, env, clear=False):
            apply_env_overrides(cfg)
            with patch.object(server, "CFG", cfg):
                result = await server.api_get_settings()
        self.assertEqual("AGENT_LLM_API_KEY", result["env_managed_fields"]["llm.default.api_key"])
        self.assertEqual(
            PROVIDER_API_KEYS_ENV,
            result["env_managed_fields"]["llm.providers.backup-gateway.api_key"],
        )
        self.assertEqual("AGENT_TENCENT_SECRET_ID", result["env_managed_fields"]["voice.secret_id"])
        self.assertEqual("AGENT_FEISHU_APP_SECRET", result["env_managed_fields"]["feishu.app_secret"])
        self.assertEqual("AGENT_WEB_PASSWORD", result["env_managed_fields"]["auth.password"])
        self.assertEqual("AGENT_HUB_TOKEN", result["env_managed_fields"]["role.hub_token"])

    async def test_danger_flag_requires_confirmation(self):
        registry = ToolRegistry()
        called = []

        async def dangerous_write():
            called.append(True)
            return "written"

        registry.register(Tool(
            "dangerous_write", "覆盖文件", {"type": "object", "properties": {}},
            dangerous_write, side_effect=True, danger=True,
        ))
        confirmations = []

        async def reject(name, args):
            confirmations.append(name)
            return False

        agent = Agent(
            _LLM(), registry, AgentConfig(confirm_before_action=True), confirm_fn=reject,
        )
        events = [event async for event in agent.run("写文件")]
        self.assertEqual(["dangerous_write"], confirmations)
        self.assertEqual([], called)
        self.assertTrue(any(event.type == "confirm_needed" for event in events))

    async def test_remote_abort_stops_before_tool(self):
        registry = ToolRegistry()
        called = []

        async def action():
            called.append(True)
            return "done"

        registry.register(Tool("dangerous_write", "动作", {"type": "object"}, action))
        agent = Agent(_LLM(), registry, AgentConfig(), abort_fn=lambda: True)
        events = [event async for event in agent.run("继续")]
        self.assertEqual([], called)
        aborted = next(event for event in events if event.type == "aborted")
        self.assertEqual("agent.abort", aborted.data["where"])
        self.assertTrue(aborted.data["resume_messages"])

    async def test_abort_after_first_tool_prevents_second_tool(self):
        registry = ToolRegistry()
        stopped = {"value": False}
        called = []

        async def first():
            called.append("first")
            stopped["value"] = True
            return "done"

        async def second():
            called.append("second")
            return "done"

        for name, handler in (("first", first), ("second", second)):
            registry.register(Tool(name, name, {"type": "object"}, handler))

        class TwoToolLLM:
            async def chat_stream_tools(self, messages, tools=None, **kwargs):
                yield "done", {
                    "role": "assistant", "content": None,
                    "tool_calls": [
                        {"id": "call-1", "type": "function", "function": {"name": "first", "arguments": "{}"}},
                        {"id": "call-2", "type": "function", "function": {"name": "second", "arguments": "{}"}},
                    ],
                }

        events = [event async for event in Agent(
            TwoToolLLM(), registry, AgentConfig(), abort_fn=lambda: stopped["value"],
        ).run("依次执行两个工具")]

        self.assertEqual(["first"], called)
        abort = next(event for event in events if event.data.get("where") == "agent.abort")
        self.assertEqual("任务已中止，本轮不会继续执行。", abort.data["msg"])
        results = [m for m in abort.data["resume_messages"] if m.get("role") == "tool"]
        self.assertEqual(["call-1", "call-2"], [m["tool_call_id"] for m in results])
        self.assertEqual("done", results[0]["content"])
        self.assertIn("该步骤未完成或结果未知", results[1]["content"])
        self.assertIn("不要直接重放", results[1]["content"])

    async def test_abort_after_confirmation_saves_checkpoint_without_running_tool(self):
        registry = ToolRegistry()
        stopped = {"value": False}
        called = []

        async def action():
            called.append(True)
            return "done"

        async def confirm(_name, _args):
            stopped["value"] = True
            return True

        registry.register(Tool(
            "dangerous_write", "动作", {"type": "object"}, action, danger=True,
        ))
        events = [event async for event in Agent(
            _LLM(), registry, AgentConfig(), confirm_fn=confirm,
            abort_fn=lambda: stopped["value"],
        ).run("写文件")]
        aborted = next(event for event in events if event.type == "aborted")
        self.assertEqual([], called)
        self.assertTrue(aborted.data["resume_messages"])

    async def test_cancelling_blocked_tool_saves_protocol_complete_checkpoint(self):
        registry = ToolRegistry()
        started = asyncio.Event()

        async def blocked():
            started.set()
            await asyncio.sleep(30)

        registry.register(Tool("blocked", "阻塞", {"type": "object"}, blocked))

        class BlockingLLM:
            async def chat_stream_tools(self, *_args, **_kwargs):
                yield "done", {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": "blocked-call", "type": "function",
                        "function": {"name": "blocked", "arguments": "{}"},
                    }],
                }

        events = []

        async def collect():
            async for event in Agent(BlockingLLM(), registry, AgentConfig()).run("执行阻塞工具"):
                events.append(event)

        task = asyncio.create_task(collect())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await task
        aborted = next(event for event in events if event.type == "aborted")
        last = aborted.data["resume_messages"][-1]
        self.assertEqual("tool", last["role"])
        self.assertIn("执行结果未知", last["content"])

    async def test_extension_inspection_always_requires_confirmation(self):
        registry = ToolRegistry()

        async def inspect(url: str, kind: str):
            return "inspected"

        registry.register(Tool(
            "extension_inspect", "审查扩展", {"type": "object"}, inspect, side_effect=True,
        ))
        confirmations = []

        async def reject(name, args):
            confirmations.append(name)
            return False

        llm = _LLM("extension_inspect", '{"url":"https://github.com/a/b","kind":"skill"}')
        agent = Agent(llm, registry, AgentConfig(confirm_before_action=False), confirm_fn=reject)
        events = [event async for event in agent.run("审查扩展", approval_mode="full")]
        self.assertEqual(["extension_inspect"], confirmations)
        self.assertEqual("external_code", next(
            event.data["reason"] for event in events if event.type == "confirm_needed"
        ))

    async def test_fs_write_rejects_skill_root(self):
        root = Path(__file__).resolve().parents[1]
        registry = ToolRegistry()
        fs_module.register_tools(registry)
        with self.assertRaises(ValueError):
            await registry.call("fs_write_text", {
                "path": str(root / "skills" / "security-test" / "SKILL.md"),
                "content": "---\nname: security-test\ndescription: test\n---\n",
            })

    async def test_fs_read_text_pages_until_complete_and_rejects_invalid_offset(self):
        content = "记忆内容。" * 8400
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.txt"
            path.write_text(content, encoding="utf-8")
            registry = ToolRegistry()
            fs_module.register_tools(registry)

            pages = []
            offset = 0
            while True:
                result = await registry.call("fs_read_text", {"path": str(path), "offset": offset})
                header, page = result.split("\n", 1)
                pages.append(page)
                if "状态=已读完" in header:
                    break
                self.assertIn(f"下一页 offset={offset + len(page)}", header)
                offset += len(page)

            self.assertEqual(content, "".join(pages))
            self.assertEqual(3, len(pages))
            with self.assertRaisesRegex(ValueError, "非负整数"):
                await registry.call("fs_read_text", {"path": str(path), "offset": -1})

    def test_all_self_modification_roots_are_protected(self):
        root = Path(__file__).resolve().parents[1]
        for relative in (
            ".git/config", "backend/security.py", "frontend/src/index.html",
            "tools/x/tool.ps1", "plugins/x/code/main.py", "skills/x/SKILL.md",
            "config.toml", "ui_token.txt",
            "data/backup/backup.zip", "model-cache/model.bin",
            "data/channel_lock.json",
        ):
            with self.subTest(path=relative), self.assertRaises(ValueError):
                _reject_protected(root / relative)

    async def test_irreversible_policy_remote_default_and_full(self):
        async def run(mode, confirm):
            registry = ToolRegistry()
            called = []

            async def command(command: str):
                called.append(command)
                return "done"

            registry.register(Tool(
                "run_command", "执行命令", {"type": "object"}, command, side_effect=True,
            ))
            agent = Agent(
                _LLM("run_command", '{"command":"shutdown /s"}'), registry,
                AgentConfig(confirm_before_action=True), confirm_fn=confirm,
            )
            events = [event async for event in agent.run("关机", approval_mode=mode)]
            return called, events

        remote_confirm = AsyncMock(return_value=True)
        called, events = await run(None, remote_confirm)
        self.assertEqual([], called)
        remote_confirm.assert_not_awaited()
        self.assertTrue(any(event.type == "tool_result" and not event.data["ok"] for event in events))

        default_confirm = AsyncMock(return_value=False)
        called, events = await run("risk", default_confirm)
        self.assertEqual([], called)
        default_confirm.assert_awaited_once()
        self.assertTrue(any(event.type == "confirm_needed" for event in events))

        full_confirm = AsyncMock(return_value=False)
        called, _ = await run("full", full_confirm)
        self.assertEqual(["shutdown /s"], called)
        full_confirm.assert_not_awaited()

    async def test_external_reenable_and_skill_prompt_require_confirmation(self):
        cases = (
            ("extension_enable", '{"kind":"tool","name":"demo","enabled":true}', "external_code"),
            ("extension_rollback", '{"kind":"tool","name":"demo"}', "external_code"),
            ("extension_install", '{"kind":"skill","url":"https://github.com/a/b"}', "untrusted_prompt"),
        )
        for name, arguments, reason in cases:
            with self.subTest(tool=name):
                registry = ToolRegistry()
                called = []

                async def action(**kwargs):
                    called.append(kwargs)
                    return "done"

                registry.register(Tool(name, "测试", {"type": "object"}, action, side_effect=True))
                reject = AsyncMock(return_value=False)
                agent = Agent(
                    _LLM(name, arguments), registry, AgentConfig(confirm_before_action=False),
                    confirm_fn=reject,
                )
                events = [event async for event in agent.run("执行", approval_mode="full")]
                self.assertEqual([], called)
                self.assertEqual(reason, next(
                    event.data["reason"] for event in events if event.type == "confirm_needed"
                ))

    async def test_tool_enable_and_reload_follow_risk_approval_mode(self):
        cases = (
            ("tool_enable", '{"name":"demo","enabled":true}'),
            ("tool_reload", "{}"),
        )
        with tempfile.TemporaryDirectory(prefix="xiaoa-tool-policy-") as tmp:
            registry = ToolRegistry()
            user_tool_module.register_tools(registry, Path(tmp))
            for name, arguments in cases:
                with self.subTest(tool=name):
                    self.assertTrue(registry.get(name).danger)

                    reject_risk = AsyncMock(return_value=False)
                    risk_events = [event async for event in Agent(
                        _LLM(name, arguments), registry,
                        AgentConfig(confirm_before_action=True), confirm_fn=reject_risk,
                    ).run("执行", approval_mode="risk")]
                    reject_risk.assert_awaited_once()
                    self.assertEqual("risk", next(
                        event.data["reason"] for event in risk_events if event.type == "confirm_needed"
                    ))

                    full_confirm = AsyncMock(return_value=False)
                    full_events = [event async for event in Agent(
                        _LLM(name, arguments), registry,
                        AgentConfig(confirm_before_action=True), confirm_fn=full_confirm,
                    ).run("执行", approval_mode="full")]
                    full_confirm.assert_not_awaited()
                    self.assertFalse(any(event.type == "confirm_needed" for event in full_events))

    def test_user_tool_description_discloses_risk_mode_confirmation(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-user-description-") as tmp:
            root = Path(tmp)
            directory = root / "demo_tool"
            directory.mkdir()
            (directory / "tool.json").write_text(json.dumps({
                "name": "demo_tool", "description": "演示工具",
                "parameters": {"type": "object", "properties": {}},
            }), encoding="utf-8")
            (directory / "tool.ps1").write_text("Write-Output ok", encoding="utf-8")
            registry = ToolRegistry()
            manager = UserToolManager(root, registry)
            manager._validate_script = lambda path: None

            self.assertEqual(["demo_tool"], manager.reload()["loaded"])
            description = registry.get("demo_tool").description
            self.assertIn("风险审批模式", description)
            self.assertIn("执行前会请求确认", description)
            self.assertEqual(["demo_tool"], manager.reload()["loaded"])
            self.assertEqual(description, registry.get("demo_tool").description)

    def test_remote_confirmation_summary_masks_and_truncates(self):
        summary = server._channel_argument_summary({
            "path": "D:/bank/statement.pdf",
            "api_key": "secret-value",
            "key": "another-secret",
            "content": "x" * 300,
        })
        self.assertIn("D:/bank/statement.pdf", summary)
        self.assertIn("api_key=***", summary)
        self.assertIn("key=***", summary)
        self.assertNotIn("secret-value", summary)
        self.assertNotIn("another-secret", summary)
        self.assertIn("…", summary)
        self.assertLessEqual(len(summary), 800)
        long_summary = server._channel_argument_summary({f"field_{index}": "value" * 40 for index in range(20)})
        self.assertEqual(800, len(long_summary))
        self.assertTrue(long_summary.endswith("…"))

    def test_channel_lock_is_persisted_atomically(self):
        old_path = server._CHANNEL_LOCK_PATH
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoa-channel-lock-") as tmp:
                server._CHANNEL_LOCK_PATH = Path(tmp) / "channel_lock.json"
                server._save_channel_lock(True)
                self.assertTrue(server._load_channel_lock())
                server._save_channel_lock(False)
                self.assertFalse(server._load_channel_lock())
                self.assertFalse(server._CHANNEL_LOCK_PATH.with_suffix(".tmp").exists())
        finally:
            server._CHANNEL_LOCK_PATH = old_path

    async def test_docgen_rejects_protected_and_existing_files(self):
        root = Path(__file__).resolve().parents[1]
        with self.assertRaises(ValueError):
            docgen_module._resolve_path(str(root / "config.toml"), "unused.docx")
        registry = ToolRegistry()
        docgen_module.register_tools(registry)
        with tempfile.TemporaryDirectory(prefix="xiaoa-docgen-") as tmp:
            existing = Path(tmp) / "existing.docx"
            existing.write_bytes(b"keep")
            with self.assertRaisesRegex(ValueError, "已存在"):
                await registry.call("create_word_doc", {"path": str(existing)})
            self.assertEqual(b"keep", existing.read_bytes())

    async def test_llm_is_closed_only_after_active_task_releases_it(self):
        client = MagicMock()
        client.aclose = AsyncMock()
        server._LLM_USAGE.clear()
        server._LLM_RETIRE.clear()
        server._retain_llm(client)
        await server._retire_llm(client)
        client.aclose.assert_not_awaited()
        await server._release_llm(client)
        client.aclose.assert_awaited_once()

    async def test_feishu_credentials_reset_clients_without_backend_restart(self):
        old_app_id = server.CFG.feishu.app_id
        old_secret = server.CFG.feishu.app_secret
        old_llm = server.LLM
        replacement = MagicMock()
        try:
            with (
                patch.object(server, "LLMClient", return_value=replacement),
                patch.object(server, "_retire_llm", new=AsyncMock()),
                patch.object(server, "_rewrite_config"),
                patch.object(server, "_start_chat_listeners"),
                patch.object(feishu_module, "stop_listener") as stop,
                patch.object(feishu_module, "reset_client") as reset,
            ):
                result = await server.api_save_settings(server.SettingsRequest(
                    feishu_app_id="new-app-id", feishu_app_secret="new-secret",
                ))
            self.assertTrue(result["ok"])
            stop.assert_called_once()
            reset.assert_called_once()
            self.assertEqual("new-app-id", server.CFG.feishu.app_id)
            self.assertEqual("new-secret", server.CFG.feishu.app_secret)
        finally:
            server.CFG.feishu.app_id = old_app_id
            server.CFG.feishu.app_secret = old_secret
            server.LLM = old_llm

    def test_zip_drive_relative_path_is_rejected_before_write(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("demo-main/tool.json", json.dumps({
                "name": "github_tool", "description": "test",
                "parameters": {"type": "object", "properties": {}},
                "side_effect": False, "timeout": 10,
            }))
            bundle.writestr("demo-main/tool.ps1", "Write-Output ok")
            bundle.writestr("demo-main/D:outside.ps1", "bad")

        with tempfile.TemporaryDirectory(prefix="xiaoa-security-") as tmp:
            manager = UserToolManager(Path(tmp) / "tools", ToolRegistry())
            manager._validate_script = lambda path: None

            class Opener:
                def __call__(self, request, timeout=0):
                    response = _Response(archive.getvalue())
                    response.headers = {"Content-Length": str(len(archive.getvalue()))}
                    return response

            with self.assertRaisesRegex(ValueError, "不安全路径"):
                ToolMarketManager(manager, opener=Opener()).inspect("https://github.com/acme/demo")

    def test_multi_root_zip_is_rejected_before_writing(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("first/file.txt", "first")
            bundle.writestr("second/file.txt", "second")
        with tempfile.TemporaryDirectory(prefix="xiaoa-zip-root-") as tmp:
            destination = Path(tmp)
            manager = UserToolManager(destination / "tools", ToolRegistry())
            with self.assertRaisesRegex(ValueError, "只有一个仓库根目录"):
                ToolMarketManager(manager)._extract(archive.getvalue(), destination / "extract")
            self.assertFalse((destination / "extract" / "first" / "file.txt").exists())
            self.assertFalse((destination / "extract" / "second" / "file.txt").exists())

    async def test_explicit_missing_browser_target_is_not_replaced(self):
        class Browser(BrowserController):
            async def ensure_browser(self):
                return {"connected": True}

            async def list_tabs(self):
                return [{"id": "actual", "webSocketDebuggerUrl": "ws://test", "type": "page"}]

        browser = Browser(Path(tempfile.gettempdir()))
        with self.assertRaises(ValueError):
            await browser._target("missing")

    def test_browser_debug_port_is_randomized_by_default(self):
        with (
            patch.dict(os.environ, {"XIAOA_BROWSER_DEBUG_PORT": ""}),
            patch("backend.modules.computer.browser_module._free_loopback_port", return_value=54321),
        ):
            browser = BrowserController(Path(tempfile.gettempdir()))
        self.assertEqual(54321, browser.port)
        self.assertNotEqual(9223, browser.port)

    @unittest.skipUnless(os.name == "nt", "Windows browser process tree cleanup")
    async def test_browser_close_terminates_owned_process_tree(self):
        browser = BrowserController(Path(tempfile.gettempdir()), port=54321)
        process = MagicMock()
        process.pid = 12345
        process.poll.return_value = None
        browser._process = process
        with patch("backend.modules.computer.browser_module.subprocess.run") as taskkill:
            await browser.close()
        self.assertIsNone(browser._process)
        self.assertEqual("taskkill", taskkill.call_args.args[0][0])
        self.assertIn("/T", taskkill.call_args.args[0])

    def test_confirmation_and_private_upload_policies(self):
        self.assertEqual(True, server._channel_reply_is_confirm("确认"))
        self.assertIsNone(server._channel_reply_is_confirm("好"))
        self.assertIsNone(server._channel_reply_is_confirm("y"))
        self.assertFalse(_looks_private("browser_upload", {"path": "D:/bank/statement.pdf"}))
        self.assertTrue(_looks_private("browser_upload", {"path": "D:/secrets/client.pem"}))

    async def test_lock_cancels_pending_confirmation(self):
        target = "security-lock-test"
        old_locked = server.CHANNEL_LOCKED["locked"]
        old_resume = dict(server.CHANNEL_RESUME)
        future = asyncio.get_running_loop().create_future()
        server.CHANNEL_CONFIRM[("feishu", target)] = future
        try:
            with patch.object(server, "_channel_send", new=AsyncMock()):
                await server.handle_channel_command("feishu", target, "锁定")
            self.assertTrue(server.CHANNEL_LOCKED["locked"])
            self.assertFalse(future.result())
        finally:
            server.CHANNEL_CONFIRM.pop(("feishu", target), None)
            server.CHANNEL_LOCKED["locked"] = old_locked
            server.CHANNEL_RESUME.clear()
            server.CHANNEL_RESUME.update(old_resume)

    async def test_remote_confirmation_is_reused_for_same_category_in_one_turn(self):
        target = "single-confirm-turn"
        approvals = []

        class FakeAgent:
            def __init__(self, *_args, confirm_fn=None, **_kwargs):
                self.confirm_fn = confirm_fn

            async def run(self, *_args, **_kwargs):
                approvals.append(await self.confirm_fn(
                    "code_run", {"command": "echo first > /tmp/first.txt"},
                ))
                approvals.append(await self.confirm_fn(
                    "code_run", {"command": "printf second > /tmp/second.txt"},
                ))
                yield SimpleNamespace(type="message", data={"content": "完成"})

        sent = AsyncMock()
        audit = SimpleNamespace(consume=lambda _event: None)
        old_locked = server.CHANNEL_LOCKED["locked"]
        server.CHANNEL_LOCKED["locked"] = False
        server.CHANNEL_LOCKS.pop(("feishu", target), None)
        server.CHANNEL_HISTORY.pop(("feishu", target), None)
        try:
            with (
                patch.object(server, "Agent", FakeAgent),
                patch.object(server, "_channel_permission", return_value=(True, 0)),
                patch.object(server, "_AuditTrace", return_value=audit),
                patch.object(server, "_channel_send", new=sent),
            ):
                task = asyncio.create_task(
                    server.handle_channel_command("feishu", target, "写两个文件"),
                )
                for _ in range(100):
                    if ("feishu", target) in server.CHANNEL_CONFIRM:
                        break
                    await asyncio.sleep(0.01)
                self.assertIn(("feishu", target), server.CHANNEL_CONFIRM)
                await server.handle_channel_command("feishu", target, "确认")
                await asyncio.wait_for(task, 2)
        finally:
            server.CHANNEL_CONFIRM.pop(("feishu", target), None)
            server.CHANNEL_LOCKS.pop(("feishu", target), None)
            server.CHANNEL_HISTORY.pop(("feishu", target), None)
            server.CHANNEL_LOCKED["locked"] = old_locked

        self.assertEqual([True, True], approvals)
        prompts = [
            call.args[2] for call in sent.await_args_list
            if len(call.args) >= 3 and "确认请回" in str(call.args[2])
        ]
        self.assertEqual(1, len(prompts))

    async def test_feishu_sender_authorization_and_image_dispatch(self):
        temporary = tempfile.TemporaryDirectory(prefix="xiaoa-feishu-test-")
        self.addCleanup(temporary.cleanup)
        path_patch = patch.object(feishu_module, "_last_open_id_path", return_value=Path(temporary.name) / "recent.json")
        path_patch.start()
        self.addCleanup(path_patch.stop)
        app_patch = patch.object(feishu_module, "_last_app_id", "")
        app_patch.start()
        self.addCleanup(app_patch.stop)
        old_cfg, old_last, old_seen = feishu_module._cfg_ref, feishu_module._last_open_id, feishu_module._seen_msg_ids
        feishu_module._cfg_ref = SimpleNamespace(feishu=SimpleNamespace(allowed_open_ids=["trusted"]))
        feishu_module._last_open_id = "trusted"
        feishu_module._seen_msg_ids = None
        message = SimpleNamespace(chat_id="chat", message_id="security-feishu", chat_type="p2p")
        sender = SimpleNamespace(sender_id=SimpleNamespace(open_id="attacker"))
        data = SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))

        def close_notification(coro, loop):
            coro.close()

        try:
            with patch.object(feishu_module, "_spawn", side_effect=close_notification):
                feishu_module.handle_event(data, lambda *args: None, None)
            self.assertEqual("trusted", feishu_module.last_open_id())

            feishu_module._seen_msg_ids = None
            message = SimpleNamespace(
                chat_id="chat", message_id="security-feishu-image", chat_type="p2p",
                message_type="image", content=json.dumps({"image_key": "image-key"}),
            )
            sender = SimpleNamespace(sender_id=SimpleNamespace(open_id="trusted"))
            data = SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))
            tasks = []

            def schedule(coro, loop):
                tasks.append(asyncio.create_task(coro))

            with patch.object(feishu_module, "_spawn", side_effect=schedule), \
                    patch.object(feishu_module, "_handle_image", new=AsyncMock()) as handle_image, \
                    patch.object(feishu_module, "_notify", new=AsyncMock()) as notify:
                feishu_module.handle_event(data, lambda *args: None, None)
                await asyncio.gather(*tasks)
            handle_image.assert_awaited_once()
            self.assertEqual("image-key", handle_image.await_args.args[4])
            notify.assert_not_awaited()

            callback = AsyncMock()
            vision = AsyncMock(return_value="一张测试图片")
            png = b"\x89PNG\r\n\x1a\ncontent"
            old_vision = feishu_module._vision_hook
            feishu_module._vision_hook = vision
            try:
                with patch.object(feishu_module, "download_resource", new=AsyncMock(return_value=png)):
                    await feishu_module._handle_image(
                        callback, "trusted", "chat", "message-id", "image-key",
                    )
                vision.assert_awaited_once_with(
                    unittest.mock.ANY, png, "png",
                )
                callback.assert_awaited_once()
                self.assertIn("一张测试图片", callback.await_args.args[1])

                callback.reset_mock()
                vision.reset_mock(return_value=True)
                vision.return_value = "图片中的年份是 2026"
                with patch.object(feishu_module, "download_resource", new=AsyncMock(return_value=png)):
                    await feishu_module._handle_image(
                        callback, "trusted", "chat", "message-id", "image-key", "请找出年份",
                    )
                self.assertIn("请找出年份", vision.await_args.args[0])
                self.assertIn("随图说明:请找出年份", callback.await_args.args[1])

                with patch.object(
                    feishu_module, "download_resource",
                    new=AsyncMock(return_value=b"x" * (feishu_module._MAX_IMAGE_BYTES + 1)),
                ), patch.object(feishu_module, "_notify", new=AsyncMock()) as oversized:
                    await feishu_module._handle_image(
                        callback, "trusted", "chat", "message-id", "image-key",
                    )
                self.assertIn("超过 12MB", oversized.await_args.args[1])
                self.assertEqual(1, vision.await_count)
            finally:
                feishu_module._vision_hook = old_vision
        finally:
            feishu_module._cfg_ref, feishu_module._last_open_id = old_cfg, old_last
            feishu_module._seen_msg_ids = old_seen

    async def test_feishu_recent_record_survives_restart_and_respects_app_and_allowlist(self):
        cfg = SimpleNamespace(feishu=SimpleNamespace(app_id="app-one", allowed_open_ids=["trusted"]))
        message = SimpleNamespace(chat_id="chat", message_id="recent-test", chat_type="p2p",
                                  message_type="text", content='{"text":"hello"}')
        data = SimpleNamespace(event=SimpleNamespace(message=message,
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="trusted"))))
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(feishu_module, "_last_open_id_path", return_value=Path(tmp) / "recent.json"), \
                patch.object(feishu_module, "_cfg_ref", cfg), \
                patch.object(feishu_module, "_last_open_id", ""), \
                patch.object(feishu_module, "_last_app_id", ""), \
                patch.object(feishu_module, "_seen_msg_ids", None), \
                patch.object(feishu_module, "_spawn", side_effect=lambda coro, loop: coro.close()):
            self.assertEqual("", feishu_module.last_open_id(cfg))  # A whitelist is not a received message.
            feishu_module.handle_event(data, lambda *args: None, None)
            self.assertEqual("trusted", feishu_module.last_open_id(cfg))
            feishu_module._last_open_id = ""  # Simulate listener restart.
            self.assertEqual("trusted", feishu_module.last_open_id(cfg))
            saved = json.loads((Path(tmp) / "recent.json").read_text(encoding="utf-8"))
            self.assertEqual({"open_id": "trusted", "app_id": "app-one"}, saved)
            cfg.feishu.allowed_open_ids = []
            self.assertEqual("", feishu_module.last_open_id(cfg))
            cfg.feishu.allowed_open_ids = ["trusted"]
            cfg.feishu.app_id = "app-two"
            self.assertEqual("", feishu_module.last_open_id(cfg))
            cfg.feishu.app_id = "app-one"
            (Path(tmp) / "recent.json").write_text("null", encoding="utf-8")
            self.assertEqual("", feishu_module.last_open_id(cfg))

    async def test_feishu_recent_memory_survives_state_write_failure(self):
        cfg = SimpleNamespace(feishu=SimpleNamespace(app_id="app-one", allowed_open_ids=["trusted"]))
        msg = SimpleNamespace(chat_id="chat", message_id="recent-write-fail", chat_type="p2p",
                              message_type="text", content='{"text":"hello"}')
        event = SimpleNamespace(event=SimpleNamespace(message=msg,
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="trusted"))))
        with patch.object(feishu_module, "_cfg_ref", cfg), \
                patch.object(feishu_module, "_last_open_id", ""), \
                patch.object(feishu_module, "_last_app_id", ""), \
                patch.object(feishu_module, "_seen_msg_ids", None), \
                patch.object(feishu_module, "_save_open_id", side_effect=OSError("read-only")), \
                patch.object(feishu_module, "_spawn", side_effect=lambda coro, loop: coro.close()) as spawned:
            feishu_module.handle_event(event, lambda *args: None, None)
            self.assertEqual("trusted", feishu_module.last_open_id(cfg))
            self.assertEqual(1, spawned.call_count)

    def test_powershell_timeout_terminates_process_tree(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-powershell-") as tmp:
            manager = UserToolManager(Path(tmp) / "tools", ToolRegistry())
            script = Path(tmp) / "tool.ps1"
            script.write_text("Start-Sleep 60", encoding="utf-8")
            process = MagicMock()
            process.poll.return_value = None
            process.stdout = process.stderr = None
            process.communicate.side_effect = [
                subprocess.TimeoutExpired("powershell", 1),
                (b"", b""),
            ]
            with (
                patch.object(user_tool_module.subprocess, "Popen", return_value=process),
                patch.object(user_tool_module, "_terminate_process_tree") as terminate,
            ):
                with self.assertRaises(TimeoutError):
                    manager._run_script(script, {}, 1)
            terminate.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main()
