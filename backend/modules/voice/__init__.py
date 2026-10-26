"""??? TTS ? ASR ???"""
from __future__ import annotations

from backend.core.config import VoiceConfig
from backend.modules.voice.tencent_voice import TencentVoice


def make_tts_engine(cfg: VoiceConfig):
    return TencentVoice(cfg)


def make_asr_engine(cfg: VoiceConfig):
    return TencentVoice(cfg)


def _is_mp3(data: bytes) -> bool:
    return len(data) >= 4 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


async def synthesize(cfg: VoiceConfig, text: str, voice=None) -> bytes:
    audio = await make_tts_engine(cfg).tts(text, voice=voice)
    if not _is_mp3(audio):
        raise ValueError("\u8bed\u97f3\u670d\u52a1\u6ca1\u6709\u8fd4\u56de\u6709\u6548\u7684 MP3")
    return audio


async def transcribe(cfg: VoiceConfig, audio: bytes, fmt: str = "wav") -> str:
    import base64
    return await make_asr_engine(cfg).asr(base64.b64encode(audio).decode(), fmt=fmt)


__all__ = ["TencentVoice", "make_tts_engine", "make_asr_engine", "synthesize", "transcribe"]
