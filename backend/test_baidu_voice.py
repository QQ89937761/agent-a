"""百度语音：分发、鉴权、真实请求格式、音频转换和设置持久化。"""
import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, unquote

import httpx
import numpy as np
import soundfile as sf

from backend.core.config import Config, VoiceConfig, apply_env_overrides, load_config
from backend.modules.voice import make_asr_engine, make_tts_engine, synthesize, transcribe
from backend.modules.voice.baidu_voice import BaiduVoice

MP3 = b"ID3" + b"\0" * 30


def cfg():
    return VoiceConfig(provider="baidu", baidu_api_key="example-api", baidu_secret_key="example-secret")


class BaiduVoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        BaiduVoice._cached_token = ("", "", 0)
        self.requests = []
        self.real_client = httpx.AsyncClient

    def client(self, handler):
        def receive(request):
            self.requests.append(request)
            return handler(request)
        return patch("backend.modules.voice.baidu_voice.httpx.AsyncClient",
                     side_effect=lambda **kw: self.real_client(transport=httpx.MockTransport(receive), **kw))

    async def test_dispatch_token_cache_voice_zero_encoding_and_long_text(self):
        def receive(r):
            if r.url.path.endswith("token"):
                body = parse_qs(r.content.decode())
                self.assertEqual(body["client_id"], ["example-api"])
                self.assertFalse(r.url.query)
                return httpx.Response(200, json={"access_token": "example-token", "expires_in": 3600})
            data = parse_qs(r.content.decode())
            self.assertEqual(data["per"], ["0"])
            self.assertEqual(data["aue"], ["3"])
            return httpx.Response(200, content=MP3)
        c = cfg()
        self.assertIsInstance(make_tts_engine(c), BaiduVoice)
        self.assertIsInstance(make_asr_engine(c), BaiduVoice)
        text = "你好+小A & 测试。" * 40
        with self.client(receive):
            audio = await synthesize(c, text, voice=0)
            await synthesize(c, "第二句")
        self.assertTrue(audio.startswith(b"ID3"))
        self.assertEqual(sum(r.url.path.endswith("token") for r in self.requests), 1)
        sent = [unquote(parse_qs(r.content.decode())["tex"][0]) for r in self.requests[1:-1]]
        self.assertEqual("".join(sent), text)
        self.assertTrue(all(len(part) <= 150 for part in sent))

    async def test_token_refresh_on_expiry_or_account_change(self):
        count = 0
        def receive(r):
            nonlocal count
            if r.url.path.endswith("token"):
                return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
            count += 1
            return httpx.Response(200, json={"err_no": 110}) if count == 1 else httpx.Response(200, content=MP3)
        with self.client(receive):
            await synthesize(cfg(), "你好")
            other = cfg(); other.baidu_api_key = "other-example"
            await synthesize(other, "你好")
        self.assertEqual(sum(r.url.path.endswith("token") for r in self.requests), 3)

    async def test_audio_only_ogg_opus_is_converted_to_16k_pcm(self):
        stream = io.BytesIO()
        sf.write(stream, np.zeros((4800, 2)), 48000, format="OGG", subtype="OPUS")
        def receive(r):
            if r.url.path.endswith("token"):
                return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
            data = json.loads(r.content)
            self.assertEqual((data["format"], data["rate"], data["channel"]), ("pcm", 16000, 1))
            self.assertEqual(data["len"], len(base64.b64decode(data["speech"])))
            self.assertEqual(data["len"], 3200)
            return httpx.Response(200, json={"err_no": 0, "result": ["你好小A"]})
        with self.client(receive):
            self.assertEqual(await transcribe(cfg(), stream.getvalue(), "ogg-opus"), "你好小A")

    async def test_errors_never_echo_provider_payload_or_credentials(self):
        for response, expected in [
            (httpx.Response(200, json={"error": "example-secret"}), "鉴权"),
            (httpx.Response(403, text="example-secret"), "连接失败"),
        ]:
            BaiduVoice._cached_token = ("", "", 0)
            with self.client(lambda _: response):
                with self.assertRaisesRegex(RuntimeError, expected) as cm:
                    await synthesize(cfg(), "测试")
            self.assertNotIn("example-secret", str(cm.exception))
        def receive(r):
            return (httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
                    if r.url.path.endswith("token") else
                    httpx.Response(200, json={"err_no": 500, "err_msg": "example-secret"}))
        with self.client(receive):
            with self.assertRaisesRegex(RuntimeError, "500") as cm:
                await synthesize(cfg(), "测试")
        self.assertNotIn("example-secret", str(cm.exception))

    def test_invalid_and_overlong_audio_rejected_before_request(self):
        for audio, fmt in [(b"", "pcm"), (b"x", "pcm"), (b"\0" * (32000 * 60 + 2), "pcm"), (b"bad", "ogg")]:
            with self.assertRaises(ValueError):
                BaiduVoice._pcm(audio, fmt)
        wav = io.BytesIO()
        sf.write(wav, np.zeros(16001), 16000, format="WAV")
        self.assertEqual(len(BaiduVoice._pcm(wav.getvalue(), "wav")), 32002)

    def test_config_environment_and_provider_isolation(self):
        c = cfg(); c.secret_id = "tc-id"; c.secret_key = "tc-key"
        self.assertTrue(c.enabled)
        c.baidu_secret_key = ""
        self.assertFalse(c.enabled)
        c.provider = "tencent"
        self.assertTrue(c.enabled)
        with patch.dict(os.environ, {"AGENT_SPEECH_PROVIDER": "baidu", "AGENT_BAIDU_API_KEY": "example-api",
                                   "AGENT_BAIDU_SECRET_KEY": "example-secret", "AGENT_BAIDU_TTS_VOICE": "0"}, clear=True):
            self.assertTrue(apply_env_overrides(Config()).voice.enabled)
        for value in ("invalid", "-1", "16"):
            with self.subTest(speed=value), patch.dict(os.environ, {"AGENT_BAIDU_TTS_SPEED": value}, clear=True):
                self.assertEqual(apply_env_overrides(Config()).voice.baidu_tts_speed, 5)
        with patch.dict(os.environ, {"AGENT_BAIDU_TTS_VOICE": "-1"}, clear=True):
            self.assertEqual(apply_env_overrides(Config()).voice.baidu_tts_voice, 0)

    async def test_settings_save_masks_roundtrip_and_live_hooks_without_reconnect(self):
        from backend.api import server
        from backend.modules.chat import feishu_module, weixin_clawbot
        c = Config(); c.voice.secret_id = "tc-id"; c.voice.secret_key = "tc-key"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True), \
             patch("backend.core.vault.get", return_value=""), \
             patch.object(server, "CFG", c), patch.object(server, "config_path", return_value=Path(tmp)/"config.toml"), \
             patch.object(server, "LLM", object()), patch.object(server, "runtime_llm", return_value=object()), \
             patch.object(server, "_retire_llm", new=AsyncMock()), patch.object(server, "broadcast", new=AsyncMock()), \
             patch.object(server.isolation_workspace, "cleanup"), \
             patch.object(feishu_module, "set_asr_hook") as feishu_hook, \
             patch.object(weixin_clawbot, "set_asr_hook") as weixin_hook, \
             patch.object(feishu_module, "stop_listener") as stop:
            result = await server.api_save_settings_section("voice", server.SettingsRequest(
                speech_provider="baidu", baidu_api_key="example-api-key", baidu_secret_key="example-secret-key", baidu_tts_voice=0))
            self.assertTrue(result["ok"], result)
            feishu_hook.assert_called_once(); weixin_hook.assert_called_once(); stop.assert_not_called()
            restored = load_config(Path(tmp)/"config.toml")
            self.assertEqual(restored.voice.baidu_api_key, "example-api-key")
            self.assertEqual(restored.voice.secret_id, "tc-id")
            self.assertEqual((await server.api_current_voice())["voice"], 0)
            snapshot = await server.api_get_settings()
            self.assertNotIn("example-api-key", json.dumps(snapshot))
            self.assertNotIn("example-secret-key", json.dumps(snapshot))
            self.assertNotIn("example-secret-key", server._redact_event_data({"message": "example-secret-key"})["message"])
            masked = snapshot["voice"]
            result = await server.api_save_settings_section("voice", server.SettingsRequest(
                speech_provider="tencent", baidu_api_key=masked["baidu_api_key_masked"],
                baidu_secret_key=masked["baidu_secret_key_masked"]))
            self.assertTrue(result["ok"])
            self.assertEqual(c.voice.baidu_secret_key, "example-secret-key")

    async def test_preview_zero_provider_mismatch_and_save_rollback(self):
        from backend.api import server
        from fastapi import HTTPException
        c = Config(); c.voice = cfg()
        with patch.object(server, "CFG", c), patch.object(server, "synthesize", new=AsyncMock(return_value=MP3)) as synth:
            self.assertEqual((await server.api_tts_preview("0", "baidu")).body, MP3)
            self.assertEqual(synth.call_args.kwargs["voice"], 0)
            with self.assertRaises(HTTPException) as cm:
                await server.api_tts_preview("0", "tencent")
            self.assertEqual(cm.exception.status_code, 409)
            with patch.object(server, "_persist_voice", side_effect=OSError("disk unavailable")):
                self.assertFalse((await server.api_set_voice(server.SetVoiceRequest(voice=1)))["ok"])
                self.assertEqual(c.voice.current_voice, 0)
            result = await server.api_save_settings(server.SettingsRequest(speech_provider="unknown"))
            self.assertFalse(result["ok"])
            self.assertEqual(c.voice.provider, "baidu")

    async def test_managed_baidu_credentials_never_written_to_toml(self):
        from backend.api import server
        env = {"AGENT_BAIDU_API_KEY": "managed-example-api", "AGENT_BAIDU_SECRET_KEY": "managed-example-secret"}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True):
            c = apply_env_overrides(Config())
            path = Path(tmp)/"config.toml"
            with patch.object(server, "CFG", c), patch.object(server, "config_path", return_value=path):
                server._rewrite_config()
            self.assertNotIn("managed-example", path.read_text(encoding="utf-8"))
