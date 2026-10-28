"""百度智能云短语音识别与在线合成（凭据仅通过 HTTPS 请求体发送）。"""
from __future__ import annotations

import base64
import hashlib
import io
import time
from urllib.parse import quote

import httpx

from backend.core.config import VoiceConfig
from backend.modules.voice.tencent_voice import split_tts_text


class BaiduVoice:
    # 只缓存当前账号，切换账号后覆盖；不把 token 写入配置或日志。
    _cached_token: tuple[str, str, float] = ("", "", 0)

    def __init__(self, cfg: VoiceConfig, *, timeout: float = 30.0):
        self.cfg = cfg
        self.timeout = timeout

    async def _post(self, client, url, **kwargs):
        try:
            response = await client.post(url, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPError:
            raise RuntimeError("百度语音连接失败，请检查网络或稍后重试") from None

    async def _token(self, client, *, refresh=False):
        if not self.cfg.baidu_api_key or not self.cfg.baidu_secret_key:
            raise ValueError("请先配置百度智能云 API Key 和 Secret Key")
        identity = hashlib.sha256((self.cfg.baidu_api_key + "\0" + self.cfg.baidu_secret_key).encode()).hexdigest()
        key, token, deadline = type(self)._cached_token
        if not refresh and key == identity and time.monotonic() < deadline:
            return token
        response = await self._post(client, "https://aip.baidubce.com/oauth/2.0/token", data={
            "grant_type": "client_credentials", "client_id": self.cfg.baidu_api_key,
            "client_secret": self.cfg.baidu_secret_key,
        })
        data = response.json()
        if not data.get("access_token"):
            raise RuntimeError("百度语音鉴权失败，请检查密钥和语音服务权限")
        token = data["access_token"]
        type(self)._cached_token = (identity, token, time.monotonic() + max(0, int(data.get("expires_in", 0)) - 60))
        return token

    async def tts(self, text: str, voice=None) -> bytes:
        parts = split_tts_text(text, limit=150)
        if not parts:
            raise ValueError("合成文字不能为空")
        selected = self.cfg.baidu_tts_voice if voice is None else int(voice)
        if not 0 <= selected <= 999999 or not 0 <= self.cfg.baidu_tts_speed <= 15:
            raise ValueError("百度音色或语速不正确")
        audio = []
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            for part in parts:
                for attempt in range(2):
                    token = await self._token(client, refresh=bool(attempt))
                    response = await self._post(client, "https://tsn.baidu.com/text2audio", data={
                        "tex": quote(part, safe=""), "tok": token, "cuid": "xiaoa-speech", "ctp": 1,
                        "lan": "zh", "spd": self.cfg.baidu_tts_speed, "pit": 5, "vol": 5,
                        "per": selected, "aue": 3,
                    })
                    if response.content.startswith(b"ID3") or response.content[:1] == b"\xff":
                        audio.append(response.content)
                        break
                    try:
                        code = int(response.json().get("err_no", -1))
                    except (ValueError, TypeError):
                        code = -1
                    if code in (110, 111, 502) and not attempt:
                        continue
                    raise RuntimeError(f"百度语音合成失败（错误码 {code}），请检查服务权限、额度和音色权限")
        return b"".join(audio)

    @staticmethod
    def _pcm(audio: bytes, fmt: str) -> bytes:
        if not audio or len(audio) > 16 * 1024 * 1024:
            raise ValueError("识别音频为空或过大")
        if fmt.lower() == "pcm":
            pcm = audio
        else:
            import numpy as np
            import soundfile as sf
            try:
                with sf.SoundFile(io.BytesIO(audio)) as source:
                    rate = source.samplerate
                    if rate <= 0 or source.frames > rate * 60:
                        raise ValueError("百度短语音识别每段最多 60 秒")
                    samples = source.read(dtype="float32", always_2d=True).mean(axis=1)
                if rate != 16000:
                    samples = np.interp(np.arange(int(len(samples) * 16000 / rate)) * rate / 16000,
                                        np.arange(len(samples)), samples)
                pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
            except (sf.LibsndfileError, RuntimeError):
                raise ValueError("无法解码语音，请发送 WAV、MP3 或 Ogg Opus 音频") from None
        if not pcm or len(pcm) % 2 or len(pcm) > 16000 * 2 * 60:
            raise ValueError("百度短语音识别需要有效的 16k 单声道音频，每段最多 60 秒")
        return pcm

    async def asr(self, audio_b64: str, fmt="wav") -> str:
        pcm = self._pcm(base64.b64decode(audio_b64, validate=True), fmt)
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            for attempt in range(2):
                token = await self._token(client, refresh=bool(attempt))
                response = await self._post(client, "https://vop.baidu.com/server_api", json={
                    "format": "pcm", "rate": 16000, "channel": 1, "cuid": "xiaoa-speech",
                    "token": token, "dev_pid": 1537, "speech": base64.b64encode(pcm).decode(), "len": len(pcm),
                })
                data = response.json()
                code = int(data.get("err_no", -1))
                if code == 0:
                    return "".join(data.get("result") or []).strip()
                if code in (110, 111, 3302) and not attempt:
                    continue
                raise RuntimeError(f"百度语音识别失败（错误码 {code}），请检查服务权限、额度和音频")
