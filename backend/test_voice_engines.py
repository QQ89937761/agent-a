"""Tencent Cloud TTS/ASR regression tests."""
from __future__ import annotations

import base64
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.core.config import Config, VoiceConfig
from backend.modules.voice import make_tts_engine, synthesize
from backend.modules.voice.tencent_voice import TENCENT_TTS_MAX_CHARS, TencentVoice, split_tts_text

FAKE_MP3 = b"\xff\xfb\x90\x00" + b"\x00" * 64


def _is_mp3(data: bytes) -> bool:
    return len(data) >= 4 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def _tencent_ok_audio() -> dict:
    return {"Response": {"Audio": base64.b64encode(FAKE_MP3).decode()}}


class TencentCredentialTests(unittest.TestCase):
    def test_labelled_credentials_are_normalized_when_loading_config(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from backend.core.config import load_config

        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[voice]\nsecret_id = "SecretId: AKID-example"\n'
                'secret_key = "SecretKey: example-key"\n',
                encoding="utf-8",
            )
            cfg = load_config(path)
        self.assertEqual(cfg.voice.secret_id, "AKID-example")
        self.assertEqual(cfg.voice.secret_key, "example-key")


class _Response:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.text = body if isinstance(body, str) else ""

    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        raise ValueError("not json")


class _FakeHttp:
    """同时充当 httpx.AsyncClient 的工厂、上下文管理器和客户端。"""

    def __init__(self, responses):
        self.responses = list(responses)  # [(status_code, body), ...]
        self.calls = 0
        self.requests: list[dict] = []
        self.client_kwargs: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.client_kwargs.append(kwargs)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, content=None, files=None, data=None):
        self.requests.append({
            "url": url, "headers": headers or {}, "content": content,
            "files": files, "data": data,
        })
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        status, body = self.responses[index]
        return _Response(status, body)

    async def get(self, url, headers=None, **kwargs):
        self.requests.append({"url": url, "headers": headers or {}, "content": None})
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        status, body = self.responses[index]
        return _Response(status, body)


class VoiceConfigSemanticsTests(unittest.TestCase):

    def test_tencent_config_has_both_tts_and_asr(self):
        cfg = VoiceConfig(secret_id="id", secret_key="key")
        self.assertTrue(cfg.tts_enabled)
        self.assertTrue(cfg.asr_enabled)

    def test_empty_config_has_neither(self):
        cfg = VoiceConfig()
        self.assertFalse(cfg.tts_enabled)
        self.assertFalse(cfg.asr_enabled)



class TencentSplitTests(unittest.TestCase):
    def test_split_keeps_all_content_and_stays_within_limit(self):
        text = "今天天气很好，适合出去散步。" * 25  # 350 字
        segments = split_tts_text(text)
        self.assertGreater(len(segments), 1)
        self.assertEqual("".join(segments), text)
        self.assertTrue(all(len(segment) <= TENCENT_TTS_MAX_CHARS for segment in segments))

    def test_short_text_is_not_split(self):
        self.assertEqual(["你好"], split_tts_text("你好"))


class TencentVoiceTests(unittest.IsolatedAsyncioTestCase):
    def _cfg(self) -> VoiceConfig:
        return VoiceConfig(secret_id="id", secret_key="key", region="ap-guangzhou")

    async def test_tts_returns_mp3_bytes(self):
        fake = _FakeHttp([(200, _tencent_ok_audio())])
        with patch("backend.modules.voice.tencent_voice.httpx.AsyncClient", fake):
            audio = await TencentVoice(self._cfg()).tts("你好")
        self.assertIsInstance(audio, bytes)
        self.assertTrue(_is_mp3(audio))

    async def test_long_text_splits_into_multiple_requests_without_loss(self):
        text = "今天天气很好，适合出去散步。" * 25  # 350 字,应切成多段
        fake = _FakeHttp([(200, _tencent_ok_audio())] * 10)
        with patch("backend.modules.voice.tencent_voice.httpx.AsyncClient", fake):
            audio = await TencentVoice(self._cfg()).tts(text)
        self.assertGreaterEqual(fake.calls, 3)
        # 每段都 <= 150 字
        sent = []
        for request in fake.requests:
            body = json.loads(request["content"])
            sent.append(body["Text"])
            self.assertLessEqual(len(body["Text"]), TENCENT_TTS_MAX_CHARS)
        # 段间无内容丢失(拼接等于原文)
        self.assertEqual("".join(sent), text)
        self.assertTrue(_is_mp3(audio))

    async def test_session_ids_differ_between_calls(self):
        fake = _FakeHttp([(200, _tencent_ok_audio())] * 2)
        with patch("backend.modules.voice.tencent_voice.httpx.AsyncClient", fake):
            await TencentVoice(self._cfg()).tts("短句一")
            await TencentVoice(self._cfg()).tts("短句二")
        session_ids = [
            json.loads(request["content"])["SessionId"] for request in fake.requests
        ]
        self.assertEqual(len(session_ids), 2)
        self.assertNotEqual(session_ids[0], session_ids[1])

    async def test_retries_once_on_5xx_then_succeeds(self):
        fake = _FakeHttp([
            (500, {"Response": {"Error": {"Message": "busy"}}}),
            (200, _tencent_ok_audio()),
        ])
        with patch("backend.modules.voice.tencent_voice.httpx.AsyncClient", fake), \
             patch("backend.modules.voice.tencent_voice.asyncio.sleep") as sleeper:
            audio = await TencentVoice(self._cfg()).tts("你好")
        self.assertTrue(_is_mp3(audio))
        self.assertEqual(fake.calls, 2)
        sleeper.assert_called_once()

    async def test_does_not_retry_on_4xx(self):
        fake = _FakeHttp([(400, {"Response": {"Error": {"Message": "bad params"}}})])
        with patch("backend.modules.voice.tencent_voice.httpx.AsyncClient", fake), \
             patch("backend.modules.voice.tencent_voice.asyncio.sleep") as sleeper:
            with self.assertRaises(RuntimeError):
                await TencentVoice(self._cfg()).tts("你好")
        self.assertEqual(fake.calls, 1)
        sleeper.assert_not_called()

    async def test_tencent_client_disables_proxy_env(self):
        fake = _FakeHttp([(200, _tencent_ok_audio())])
        with patch("backend.modules.voice.tencent_voice.httpx.AsyncClient", fake):
            await TencentVoice(self._cfg()).tts("你好")
        self.assertTrue(fake.client_kwargs)
        self.assertFalse(fake.client_kwargs[0].get("trust_env", True))




class DispatchTests(unittest.IsolatedAsyncioTestCase):

    async def test_synthesize_unified_bytes_contract(self):
        tencent = VoiceConfig(secret_id="id", secret_key="key")
        fake = _FakeHttp([(200, _tencent_ok_audio())])
        with patch("backend.modules.voice.tencent_voice.httpx.AsyncClient", fake):
            audio = await synthesize(tencent, "你好")
        self.assertIsInstance(audio, bytes)
        self.assertTrue(_is_mp3(audio))


    async def test_synthesize_passes_segmented_tencent_mp3(self):
        # 400 字分段拼接后第一帧头仍 0xFF 开头,应通过 synthesize 的兜底校验
        text = "今天天气很好，适合出去散步。" * 25
        fake = _FakeHttp([(200, _tencent_ok_audio())] * 10)
        cfg = VoiceConfig(secret_id="id", secret_key="key")
        with patch("backend.modules.voice.tencent_voice.httpx.AsyncClient", fake):
            audio = await synthesize(cfg, text)
        self.assertTrue(_is_mp3(audio))


class ProxyBehaviorTests(unittest.TestCase):
    def test_llm_client_uses_direct_connection(self):
        import inspect
        from backend.core import llm as llm_module
        source = inspect.getsource(llm_module.LLMClient._http)
        self.assertIn("trust_env=False", source.replace(" ", ""))


class ApiTtsErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_tts_returns_ok_false_when_synthesis_fails(self):
        from backend.api import server
        from backend.api.server import TTSRequest

        cfg = Config()
        cfg.voice.secret_id = "id"
        cfg.voice.secret_key = "key"
        with patch.object(server, "CFG", cfg), \
             patch("backend.api.server.synthesize",
                   new=AsyncMock(side_effect=RuntimeError("voice service error"))):
            result = await server.api_tts(TTSRequest(text="你好"))
        self.assertFalse(result["ok"])
        self.assertIn("voice service error", result["error"])












if __name__ == "__main__":
    unittest.main()
