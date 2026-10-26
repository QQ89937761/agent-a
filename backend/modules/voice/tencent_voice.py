"""腾讯云语音:一句话识别(ASR)+ 语音合成(TTS)。

用腾讯云 TC3-HMAC-SHA256 v3 签名直接调 HTTP 接口,不依赖官方 SDK,
只用标准库 + httpx。这样悬浮球(Electron)里也能语音——因为走的是腾讯云,
不是 Electron 用不了的浏览器语音。

- ASR: SentenceRecognition(一句话识别),吃 base64 音频(WAV/MP3,16k 单声道)
- TTS: TextToVoice(基础语音合成),返回 base64 的 MP3

注意:音频会上传到腾讯云做识别(非本地)。

TTS returns decoded MP3 bytes.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone

import httpx

from backend.core.config import VoiceConfig

# 基础版单次合成上限(字符)。超过就按句子边界分段,逐段合成后拼接,避免静默丢内容。
TENCENT_TTS_MAX_CHARS = 150
_SENTENCE_BOUNDARIES = "。！？；!?;\n"


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def split_tts_text(text: str, limit: int = TENCENT_TTS_MAX_CHARS) -> list[str]:
    """按句子边界把文本切成不超过 limit 的段,段之间不丢字。

    优先在句末标点(。！？；!?;\\n)处断开;一个超长句内部没有标点时,才在
    limit 处硬切。返回的每段都 <= limit(单字长句除外,但那说明输入本身超限)。
    """
    value = (text or "").strip()
    if not value:
        return []
    if len(value) <= limit:
        return [value]

    segments: list[str] = []
    remaining = value
    while len(remaining) > limit:
        head = remaining[:limit]
        cut = limit
        # 在 limit 之内找最后一个句末标点,优先在它后面断开
        last_boundary = -1
        for boundary in _SENTENCE_BOUNDARIES:
            position = head.rfind(boundary)
            if position > last_boundary:
                last_boundary = position
        if last_boundary >= 0:
            cut = last_boundary + 1  # 标点留在当前段的末尾
        segments.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        segments.append(remaining)
    return segments


class TencentVoice:
    def __init__(self, cfg: VoiceConfig, *, timeout: float = 15.0, retry_delay: float = 0.75):
        self.cfg = cfg
        self._timeout = timeout
        self._retry_delay = retry_delay

    def _headers(self, host: str, service: str, action: str, version: str, payload: str) -> dict:
        """构造 TC3-HMAC-SHA256 v3 签名头。"""
        secret_id = self.cfg.secret_id
        secret_key = self.cfg.secret_key
        algorithm = "TC3-HMAC-SHA256"
        timestamp = int(time.time())
        date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")

        # 1) 规范请求串
        ct = "application/json; charset=utf-8"
        canonical_headers = f"content-type:{ct}\nhost:{host}\nx-tc-action:{action.lower()}\n"
        signed_headers = "content-type;host;x-tc-action"
        hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        canonical_request = f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"

        # 2) 待签名字符串
        credential_scope = f"{date}/{service}/tc3_request"
        hashed_canonical = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        string_to_sign = f"{algorithm}\n{timestamp}\n{credential_scope}\n{hashed_canonical}"

        # 3) 计算签名
        secret_date = _sign(("TC3" + secret_key).encode("utf-8"), date)
        secret_service = _sign(secret_date, service)
        secret_signing = _sign(secret_service, "tc3_request")
        signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        # 4) 授权头
        authorization = (
            f"{algorithm} Credential={secret_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return {
            "Authorization": authorization,
            "Content-Type": ct,
            "Host": host,
            "X-TC-Action": action,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": version,
            "X-TC-Region": self.cfg.region,
        }

    async def _post(self, host: str, headers: dict, payload: str) -> httpx.Response:
        """POST 一次,网络异常和 5xx 各重试一次;4xx 是参数错,不重试。

        trust_env=False:腾讯云是用户明确配置的语音端点,不走系统代理更稳。
        """
        last_error: BaseException | None = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
                    response = await client.post(f"https://{host}", headers=headers, content=payload)
            except httpx.HTTPError as exc:  # 网络抖动:连接失败/超时/读超时
                last_error = exc
            else:
                if response.status_code >= 500:
                    last_error = RuntimeError(f"腾讯云返回 HTTP {response.status_code}")
                else:
                    return response
            if attempt == 0:
                await asyncio.sleep(self._retry_delay)
        raise RuntimeError(f"腾讯云请求失败(已重试一次): {last_error}")

    async def asr(self, audio_b64: str, fmt: str = "wav") -> str:
        """一句话识别:传 base64 音频,返回识别文字。"""
        host = "asr.tencentcloudapi.com"
        payload = json.dumps(
            {
                "EngSerViceType": "16k_zh",
                "SourceType": 1,  # 1=音频数据(base64)
                "VoiceFormat": fmt,
                "Data": audio_b64,
                "DataLen": len(base64.b64decode(audio_b64)),
            }
        )
        headers = self._headers(host, "asr", "SentenceRecognition", "2019-06-14", payload)
        r = await self._post(host, headers, payload)
        data = r.json().get("Response", {})
        if "Error" in data:
            raise RuntimeError(f"腾讯云ASR错误: {data['Error'].get('Message')}")
        return data.get("Result", "")

    async def tts(self, text: str, voice: int | None = None) -> bytes:
        """语音合成:传文字,返回 MP3 的 bytes。voice 可临时指定音色(试听用)。

        超过单次上限的文本会按句子边界分段、逐段合成再按字节拼接,
        避免"只念前 150 字"的静默丢弃。
        """
        segments = split_tts_text(text)
        if not segments:
            return b""
        chunks: list[bytes] = []
        for segment in segments:
            chunks.append(await self._tts_one(segment, voice=voice))
        return b"".join(chunks)

    async def _tts_one(self, text: str, voice: int | None = None) -> bytes:
        """合成单段(已保证不超过上限),返回解码后的 MP3 bytes。"""
        host = "tts.tencentcloudapi.com"
        payload = json.dumps(
            {
                "Text": text,
                "SessionId": uuid.uuid4().hex,  # 并发下也不会撞 ID
                "VoiceType": voice if voice is not None else self.cfg.tts_voice,
                "Speed": self.cfg.tts_speed,
                "Codec": "mp3",
            }
        )
        headers = self._headers(host, "tts", "TextToVoice", "2019-08-23", payload)
        r = await self._post(host, headers, payload)
        data = r.json().get("Response", {})
        if "Error" in data:
            raise RuntimeError(f"腾讯云TTS错误: {data['Error'].get('Message')}")
        audio_b64 = data.get("Audio", "")
        try:
            return base64.b64decode(audio_b64, validate=True)
        except Exception as exc:
            raise RuntimeError("腾讯云TTS返回的音频不是有效的 base64") from exc
