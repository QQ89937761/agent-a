"""Atomically update the hub model settings without printing secrets."""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path


ALLOWED = {
    "AGENT_LLM_PROVIDER_NAME", "AGENT_LLM_CONTEXT_WINDOWS", "AGENT_CONTEXT_WINDOW",
    "AGENT_LLM_PROTOCOL", "AGENT_LLM_BASE_URL", "AGENT_LLM_API_KEY", "AGENT_CHAT_MODEL", "AGENT_MODEL_OPTIONS",
    "AGENT_TENCENT_SECRET_ID", "AGENT_TENCENT_SECRET_KEY", "AGENT_TENCENT_REGION",
    "AGENT_CLEAR_TENCENT_CREDENTIALS", "AGENT_CLEAR_BAIDU_CREDENTIALS",
    "AGENT_TTS_VOICE", "AGENT_TTS_SPEED",
    "AGENT_SPEECH_PROVIDER", "AGENT_BAIDU_API_KEY", "AGENT_BAIDU_SECRET_KEY",
    "AGENT_BAIDU_TTS_VOICE", "AGENT_BAIDU_TTS_SPEED",
    "AGENT_FEISHU_APP_ID", "AGENT_FEISHU_APP_SECRET",
    "AGENT_SCHEDULE_TASKS",
    "AGENT_LLM_PROVIDERS", "AGENT_LLM_PROVIDER_API_KEYS",
    "AGENT_WEB_PASSWORD",
}


def update_environment(text: str, values: dict[str, str]) -> str:
    if set(values) - ALLOWED:
        raise ValueError("包含不允许修改的环境变量")
    if "AGENT_CONTEXT_WINDOW" in values and not 4096 <= int(values["AGENT_CONTEXT_WINDOW"]) <= 2_000_000:
        raise ValueError("AGENT_CONTEXT_WINDOW 必须为 4096 到 2000000")
    for name, expected in (("AGENT_LLM_CONTEXT_WINDOWS", dict), ("AGENT_LLM_PROVIDERS", list), ("AGENT_LLM_PROVIDER_API_KEYS", dict)):
        if name in values:
            try:
                parsed = json.loads(values[name])
            except (ValueError, TypeError):
                raise ValueError(f"{name} 必须为有效 JSON") from None
            if not isinstance(parsed, expected):
                raise ValueError(f"{name} JSON 类型无效")
            if name == "AGENT_LLM_CONTEXT_WINDOWS" and any(
                    not isinstance(key, str) or type(value) is not int or not 4096 <= value <= 2_000_000
                    for key, value in parsed.items()):
                raise ValueError("AGENT_LLM_CONTEXT_WINDOWS 包含无效窗口大小")
            if name == "AGENT_LLM_PROVIDERS":
                from backend.core.config import _providers_from_env
                _providers_from_env(values[name])
            if name == "AGENT_LLM_PROVIDER_API_KEYS" and any(
                    not isinstance(key, str) or not isinstance(value, str) for key, value in parsed.items()):
                raise ValueError(f"{name} 必须为字符串键值对")
    clear_flags = {
        "AGENT_CLEAR_TENCENT_CREDENTIALS": ("AGENT_TENCENT_SECRET_ID", "AGENT_TENCENT_SECRET_KEY"),
        "AGENT_CLEAR_BAIDU_CREDENTIALS": ("AGENT_BAIDU_API_KEY", "AGENT_BAIDU_SECRET_KEY"),
    }
    cleared = set()
    for flag, names in clear_flags.items():
        if values.get(flag, "") not in {"", "1"}:
            raise ValueError(f"{flag} 的值无效")
        if values.get(flag) == "1":
            cleared.update(names)
    # Remove the deprecated split vision setting so old cloud environments
    # cannot keep selecting a deleted model after a sync.
    legacy_voice = ("AGENT_TTS_PROVIDER=", "AGENT_VOICE_PROVIDER=", "AGENT_GROK_")
    remove_names = {name + "=" for name in cleared}
    lines = [line for line in text.splitlines()
             if not line.startswith(("AGENT_VISION_MODEL=", *legacy_voice, *remove_names))]
    for name, raw_value in values.items():
        if name in clear_flags or name in cleared:
            continue
        value = str(raw_value)
        if any(char in value for char in "\r\n\0"):
            raise ValueError(f"{name} 包含非法字符")
        rendered = f"{name}={shlex.quote(value)}"
        for index, line in enumerate(lines):
            if line.startswith(name + "="):
                lines[index] = rendered
                break
        else:
            lines.append(rendered)
    return "\n".join(lines) + "\n"


def reset_saved_model_settings(path: Path) -> None:
    """An explicit desktop-to-cloud model sync supersedes an earlier browser save."""
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    updated = re.sub(
        r'(?ms)(^\[llm\][^\r\n]*\r?\n(?:(?!^\[).)*?^settings_saved[ \t]*=)[^\r\n]*',
        r'\1 false', text, count=1,
    )
    if updated == text:
        return
    fd, temp_name = tempfile.mkstemp(prefix=".config-sync.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("用法: update_hub_env.py <输入JSON> </etc/xiaoa-hub.env>")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    payload_path, env_path = map(Path, sys.argv[1:])
    values = json.loads(payload_path.read_text(encoding="utf-8"))
    updated = update_environment(env_path.read_text(encoding="utf-8"), values)
    fd, temp_name = tempfile.mkstemp(prefix=".xiaoa-hub.", dir=env_path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(updated)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, env_path)
        if set(values) & {"AGENT_LLM_PROVIDER_NAME", "AGENT_LLM_CONTEXT_WINDOWS", "AGENT_CONTEXT_WINDOW",
                          "AGENT_LLM_PROTOCOL", "AGENT_LLM_BASE_URL", "AGENT_CHAT_MODEL",
                          "AGENT_MODEL_OPTIONS", "AGENT_LLM_PROVIDERS"}:
            reset_saved_model_settings(Path(__file__).resolve().parents[1] / "config.toml")
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
