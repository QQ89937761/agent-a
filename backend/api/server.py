"""后端服务入口:FastAPI + WebSocket。

前端通过 WebSocket 连上来:
  - 发 {"type":"user_input","text":...} 提交任务
  - 收 AgentEvent 流(thinking/tool_call/tool_result/message/confirm_needed/done/error)
  - 对 confirm_needed 回 {"type":"confirm","allow":true/false}
  - 随时发 {"type":"stop"} 中止当前任务

启动:python -m backend.api.server
"""
from __future__ import annotations

import asyncio
import base64
import copy
import contextvars
import io
import hmac
import ipaddress
import json
import logging
import mimetypes
import math
import os
import re
import shutil
import signal
import socket
import ssl
import sys
import tempfile
import threading as _threading
import time
import urllib.parse
import uuid
from dataclasses import replace as dataclass_replace
from typing import Any, Literal
from pathlib import Path

from contextlib import asynccontextmanager

_IMPORT_STARTED = time.monotonic()

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StrictBool

from backend import __version__
from backend.core.agent import (
    Agent, ConversationApproval, _remote_confirmation_categories, compact_context_messages, compact_for_limit,
)
from backend.core.bridge import (
    HubBridge, WorkerBridge, direct_websocket_options, hub_ssl_context,
    verify_peer_fingerprint,
)
from backend.core.connection_string import masked_preview, parse_connection_string
from backend.core.config import (
    LLMConfig, LLMProviderConfig, PROVIDER_API_KEYS_ENV, SECRET_ENV_FIELDS,
    _normalize_tencent_credential,
    app_home, apply_env_overrides, asset_root, config_path, load_config,
    plaintext_secret_env_vars, provider_api_key_env_overrides,
    provider_api_key_env_warning, provider_runtime_env,
)
from backend.core import channel_control, isolation_workspace, update_manager, task_trace
from backend.core.llm import runtime_llm, LLMClient
from backend.core.tools import Tool, ToolRegistry
from backend.modules.memory.memory_module import MemoryModule
from backend.modules.memory.sync_audit import MemorySyncAudit
from backend.modules.memory import tool_module as memory_tool_module
from backend.modules.office import fs_module, office_module, docgen_module
from backend.modules.cad import cad_module
from backend.modules.computer import (
    browser_module, code_module, computer_module, extension_module, skill_module, system_control,
    user_tool_module,
)
from backend.modules.computer.operation_audit import OperationAuditLog, redact_parameters
from backend.modules.schedule.schedule_module import Scheduler
from backend.modules.voice import synthesize, transcribe

# 全局单例(启动时初始化)
_FILE_CFG = load_config()
if os.name == "nt" and _FILE_CFG.feishu.app_secret and not os.getenv("AGENT_FEISHU_APP_SECRET"):
    try:
        from backend.core import vault
        vault.put("feishu_app_secret", _FILE_CFG.feishu.app_secret)
        config_file = config_path()
        raw_config = config_file.read_text(encoding="utf-8")
        import tempfile as _config_tempfile
        from backend.core.config import scrub_feishu_secret
        scrubbed = scrub_feishu_secret(raw_config)
        if scrubbed != raw_config:
            fd, temp_name = _config_tempfile.mkstemp(prefix=".config-migrate-", dir=config_file.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(scrubbed)
                    handle.flush(); os.fsync(handle.fileno())
                os.replace(temp_name, config_file)
            finally:
                try: os.unlink(temp_name)
                except FileNotFoundError: pass
        _FILE_CFG.feishu.app_secret = ""
    except Exception as exc:
        print(f"[security] 飞书密钥迁移失败，暂保留旧配置: {type(exc).__name__}")
_PLAINTEXT_SECRET_ENV_VARS = plaintext_secret_env_vars(_FILE_CFG)
CFG = apply_env_overrides(_FILE_CFG)
isolation_workspace.configure_process_temp()
LLM = runtime_llm(CFG.llm, hub=CFG.role.is_hub)
_LLM_USAGE: dict[LLMClient, int] = {}
_LLM_RETIRE: set[LLMClient] = set()
REGISTRY = ToolRegistry()
MEMORY: MemoryModule | None = None
_WEIXIN_MEMORIES = None
_WEIXIN_MEMORIES_LOCK = _threading.Lock()
_SHARED_ROUTE_CACHE_FILE = "shared-library-routes.json"
_SHARED_ROUTE_CACHE_LOCK = _threading.RLock()
_SHARED_ROUTE_NAMES: tuple[str, ...] | None = None
_SHARED_ROUTE_HOME: Path | None = None
_SHARED_ROUTE_READY = False
_SHARED_ROUTE_REFRESH_TASK: asyncio.Task | None = None


def _weixin_memories():
    from backend.modules.memory.account_memory import WeixinMemories
    global _WEIXIN_MEMORIES
    home = app_home()
    with _WEIXIN_MEMORIES_LOCK:
        if _WEIXIN_MEMORIES is None or _WEIXIN_MEMORIES.root != home / "data" / "weixin-memory":
            _WEIXIN_MEMORIES = WeixinMemories(home, CFG.memory, lambda: LLM)
        return _WEIXIN_MEMORIES


def _sync_weixin_memory_accounts(accounts):
    if accounts:
        _weixin_memories().register_accounts(accounts)


def _shared_route_cache_path() -> Path:
    return app_home() / "data" / _SHARED_ROUTE_CACHE_FILE


def _shared_route_names(result: object) -> tuple[str, ...]:
    if not isinstance(result, dict):
        return ()
    names: set[str] = set()
    for item in result.get("libraries", ()):
        if not isinstance(item, dict):
            continue
        value = item.get("call_name")
        if isinstance(value, str) and value.strip():
            names.add(value.strip())
        for value in item.get("aliases", ()):
            if isinstance(value, str) and value.strip():
                names.add(value.strip())
    return tuple(sorted(names, key=lambda value: (-len(value), value)))


def _load_shared_route_names() -> tuple[str, ...]:
    global _SHARED_ROUTE_NAMES, _SHARED_ROUTE_HOME, _SHARED_ROUTE_READY
    home = app_home()
    with _SHARED_ROUTE_CACHE_LOCK:
        if _SHARED_ROUTE_HOME != home:
            _SHARED_ROUTE_HOME = home
            _SHARED_ROUTE_NAMES = None
        if _SHARED_ROUTE_NAMES is not None:
            return _SHARED_ROUTE_NAMES
        _SHARED_ROUTE_READY = False
        try:
            value = json.loads(_shared_route_cache_path().read_text(encoding="utf-8"))
            names = value["names"]
            if not isinstance(names, list) or any(not isinstance(name, str) or not name.strip() for name in names):
                raise ValueError("invalid shared route cache")
            _SHARED_ROUTE_NAMES = tuple(names)
            _SHARED_ROUTE_READY = value.get("complete", True) is True
        except (OSError, ValueError, TypeError, KeyError):
            _SHARED_ROUTE_NAMES = ()
        try:
            from backend.modules.memory import shared_libraries
            local = shared_libraries.route_names(_weixin_memories().shared)
        except Exception:
            local = ()
        _SHARED_ROUTE_NAMES = tuple(sorted(set(_SHARED_ROUTE_NAMES) | set(local),
                                           key=lambda value: (-len(value), value)))
        return _SHARED_ROUTE_NAMES


def _save_shared_route_names(result: object, *, complete: bool = True) -> None:
    global _SHARED_ROUTE_NAMES, _SHARED_ROUTE_HOME, _SHARED_ROUTE_READY
    if not isinstance(result, dict) or not result.get("ok") or not isinstance(result.get("libraries"), list):
        return
    with _SHARED_ROUTE_CACHE_LOCK:
        names = _shared_route_names(result)
        # Keep old prefixes as aliases: a renamed/deleted project must still reach
        # the authoritative service and receive its rename/disabled response.
        names = tuple(sorted(set(_load_shared_route_names()) | set(names),
                             key=lambda value: (-len(value), value)))
        ready = complete or _SHARED_ROUTE_READY
        path = _shared_route_cache_path()
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({"names": names, "complete": ready}, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        _SHARED_ROUTE_HOME = app_home()
        _SHARED_ROUTE_NAMES = names
        _SHARED_ROUTE_READY = ready


def _shared_route_matches(text: str) -> bool:
    value = text.strip()
    # A call name is intentionally a plain prefix. Voice and natural-language
    # messages may continue immediately with Chinese text and have no separator.
    return any(value.startswith(name) for name in _load_shared_route_names())


def _refresh_shared_route_names() -> None:
    from backend.core import cloud_deploy
    if not (app_home() / "data/cloud-ssh/target.json").is_file():
        return
    try:
        _save_shared_route_names(cloud_deploy.get_project_libraries(app_home()))
    except Exception as exc:
        logging.getLogger(__name__).debug("shared library route refresh failed: %s", type(exc).__name__)


async def _refresh_shared_route_names_async() -> None:
    await asyncio.to_thread(_refresh_shared_route_names)


def _memory_for_current_channel():
    channel = CURRENT_CHANNEL.get()
    if not channel or channel[0] != "weixin":
        return MEMORY
    from backend.modules.chat import weixin_clawbot
    accounts = weixin_clawbot._accounts()
    identity = _channel_parts(channel)[2] or (next(iter(accounts)) if len(accounts) == 1 else "")
    if not accounts:
        # Legacy identity-less chats have no private store; never expose desktop memory.
        return None
    return _weixin_memories().for_account(identity, accounts)


def _project_memory_for_current_channel():
    channel = CURRENT_CHANNEL.get()
    if channel and channel[0] == "weixin":
        raise PermissionError("微信独立记忆不允许索引本机项目；请在桌面端管理项目")
    return MEMORY


async def _flush_private_memories():
    if _WEIXIN_MEMORIES is not None:
        for memory in _WEIXIN_MEMORIES.active_stores():
            if memory.pending_count:
                await memory.flush()

# hub 侧:管理拨进来的 worker(仅 hub 角色用)
HUB_BRIDGE = HubBridge(
    token=CFG.role.hub_token,
    on_tools=lambda tools: _sync_worker_tools(tools),
    on_worker_offline=lambda: _sync_worker_tools([], persist=False),
    get_model_order=lambda: _cloud_model_order(),
) if CFG.role.is_hub else None
# worker 侧:拨号连 hub 的客户端(仅 worker 角色用)
WORKER_BRIDGE: WorkerBridge | None = None
_WORKER_BRIDGE_TASK: asyncio.Task | None = None
USER_TOOL_MANAGER = None
SKILL_MANAGER = None
EXTENSION_MANAGER = None
CODE_PROCESS_MANAGER = None
BROWSER_CONTROLLER = None
AUDIT_LOG = OperationAuditLog()
SCHEDULER = Scheduler(
    app_home() / "data" / "scheduled_tasks.json",
    location=("cloud" if CFG.role.is_hub else "local"),
)
_SCHEDULE_FIRE_LOCK = asyncio.Lock()
_WORKER_PROXY_TOOLS: set[str] = set()
# These tools operate on the computer that owns the current chat session.  In
# hub mode the worker copy must win over any hub-local fallback; otherwise a
# request to send C:\\Users\\... is incorrectly evaluated on the cloud host.
_WORKER_AUTHORITATIVE_CHANNEL_TOOLS = {
    "send_file_to_chat", "send_image_to_chat",
}
_CHANNEL_CONTEXT_TOKEN: contextvars.ContextVar[str] = contextvars.ContextVar(
    "channel_context_token", default="",
)


def _known_secret_values() -> tuple[str, ...]:
    """Values already held by the app that must never leave in UI events."""
    values = [
        CFG.llm.api_key, CFG.voice.secret_id, CFG.voice.secret_key,
        CFG.feishu.app_secret, CFG.auth.password, CFG.role.hub_token,
        CFG.memory.cloud_backup.access_key, CFG.memory.cloud_backup.secret_key,
        os.getenv("AGENT_LLM_API_KEY", ""), os.getenv("AGENT_TENCENT_SECRET_ID", ""),
        os.getenv("AGENT_TENCENT_SECRET_KEY", ""), os.getenv("AGENT_FEISHU_APP_SECRET", ""),
        os.getenv("AGENT_WEB_PASSWORD", ""), os.getenv("AGENT_HUB_TOKEN", ""),
    ]
    return tuple(dict.fromkeys(
        value for value in (str(item).strip() for item in values)
        if len(value) >= 6 and value.lower() not in {"default", "secret", "password"}
    ))


def _normalize_event_punctuation(ev) -> None:
    """把小A 回复里的半角标点规范成中文全角(模型写中文时常混用)。

    在事件进入历史/前端之前处理,message / message_end / message_delta 都覆盖。
    """
    from backend.core.text_normalize import normalize_cn_punctuation

    data = getattr(ev, "data", None)
    if not isinstance(data, dict):
        return
    for key in ("content", "delta"):
        value = data.get(key)
        if isinstance(value, str) and value:
            data[key] = normalize_cn_punctuation(value)


def _redact_event_data(value, *, skip_media=True, text_limit=4000, omit_media=False):
    """Redact only the event/display copy; tool execution keeps original args.

    skip_media=True(默认,发给前端/远程的事件)时媒体字段(image/_observation_image 等
    base64)整段原样保留,否则 PNG base64 会被高熵 token 正则毁成 ***REDACTED***。
    落盘(断点)路径传 skip_media=False,让媒体也被遮蔽(截图不必落盘)。
    """
    safe = redact_parameters(value, skip_media=skip_media, text_limit=text_limit, omit_media=omit_media)
    secrets = _known_secret_values()

    def replace(item):
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "***")
            return item
        if isinstance(item, dict):
            return {key: replace(val) for key, val in item.items()}
        if isinstance(item, list):
            return [replace(val) for val in item]
        return item

    return replace(safe)
_PARENT_MONITOR_TASK: asyncio.Task | None = None
_UVICORN_SERVER = None
_HUB_RUNTIME_CONTEXT = (
    "你现在运行在云服务器上的 hub，不在用户的 Windows 电脑上。"
    "你可以直接操作这台云服务器：修改代码、运行命令、安装依赖、查询进程、查看磁盘和日志、运行测试、管理长任务；"
    "这些任务请使用 code_* 和 fs_* 工具在云服务器上完成，不要派给 worker。"
    "查询云服务器自身进程或状态时用 code_run（例如 ps aux），不要使用 Windows 专属的 list_windows。"
    "只有用户 Windows 电脑上的软件、窗口、文件或屏幕操作，才必须通过 worker 工具派回去。"
    "如果 worker 离线，这些工具会失败；请明确告诉用户‘你的电脑当前没开或未连接’，"
    "不要假装已经完成。"
)
_LOCAL_CLOUD_RUNTIME_CONTEXT = (
    "你有一台已连接的云服务器。需要操作云服务器时，直接使用 cloud_exec、"
    "cloud_read_file、cloud_upload；不要自己编写 SSH 脚本，也不要索要或传递云端密码。"
    "cloud_exec 的命令在云服务器上执行，查询进程、日志和文件也在那里完成。"
)
_CLOUD_DEPLOY_JOBS: dict[str, dict] = {}
_CLOUD_JOB_LOCK = _threading.RLock()
_UPDATE_STATE_PATH = update_manager.update_state_path(app_home())
_UPDATE_JOB: dict[str, object] = update_manager.load_update_state(_UPDATE_STATE_PATH)
_UPDATE_JOB_LOCK = _threading.RLock()
_UPDATE_CANCEL = _threading.Event()
if _UPDATE_JOB.get("stage") in {"queued", "checking", "downloading", "cancelling"}:
    _UPDATE_JOB.update(stage="error", percent=0, message="上次下载已中断，可重新下载更新", error="")

# ===== 桌面对话历史持久化(重开页面能延续上次任务) =====
# 单用户自用,一份全局历史存到 data/chat_history.json。
_HISTORY_PATH = app_home() / "data" / "chat_history.json"
_CONVERSATIONS_PATH = app_home() / "data" / "conversations.json"
_MAX_SAVED_HISTORY_MESSAGES = 500
_MAX_CONVERSATIONS = 100
_HISTORY_WRITE_LOCK = _threading.Lock()
_CONVERSATIONS_WRITE_LOCK = _threading.Lock()
_RESUME_PATH = app_home() / "data" / "resume_states.json"
_RESUME_WRITE_LOCK = _threading.Lock()
_CHANNEL_LOCK_PATH = app_home() / "data" / "channel_lock.json"
_CHANNEL_LOCK_WRITE_LOCK = _threading.Lock()


def _frontend_file(name: str) -> Path:
    return asset_root() / "frontend" / "src" / name


def load_chat_history() -> list[dict]:
    try:
        if _HISTORY_PATH.exists():
            data = json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)][-_MAX_SAVED_HISTORY_MESSAGES:]
    except Exception as e:
        print(f"[history] 读取失败: {e}")
    return []


def save_chat_history(history: list[dict]) -> bool:
    temp = _HISTORY_PATH.with_suffix(".tmp")
    try:
        payload = json.dumps(history[-_MAX_SAVED_HISTORY_MESSAGES:], ensure_ascii=False)
        with _HISTORY_WRITE_LOCK:
            _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, _HISTORY_PATH)
        return True
    except Exception as e:
        print(f"[history] 保存失败: {e}")
        return False
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _recent_history(history: list[dict], limit: int) -> list[dict]:
    """界面保留长历史，模型只接收最近的短上下文。"""
    limit = max(0, int(limit))
    # UI-only trails/unfinished placeholders must never enlarge or pollute model context.
    visible = [{key: value for key, value in item.items() if key != "task_trace"}
               for item in history if item.get("content")]
    return visible[-limit:] if limit else []


def _new_conversation(conversation_id: str | None = None, title: str = "新对话", messages: list[dict] | None = None) -> dict:
    now = int(time.time() * 1000)
    return {
        "id": conversation_id or f"conv_{uuid.uuid4().hex}",
        "title": (title or "新对话").strip()[:80] or "新对话",
        "created_at": now,
        "updated_at": now,
        "messages": list(messages or [])[-_MAX_SAVED_HISTORY_MESSAGES:],
    }


def _auto_conversation_title(text: str, attachments: list[dict] | None = None) -> str:
    """从首条用户消息生成短标题；空文本附件消息也要有可识别名称。"""
    candidate = re.sub(r"\s+", " ", str(text or "")).strip()
    # Ignore a short greeting so a substantive question/task becomes the conversation title.
    original = candidate
    candidate = re.sub(r"^(?:你好|您好)[，,。.!！?？\s]*(?:小[ AaＡａ])?[，,。.!！?？\s]*", "", candidate, count=1)
    candidate = re.sub(r"^小[ AaＡａ]\s*[，,。.!！?？:：、]*\s*", "", candidate, count=1)
    candidate = candidate or original
    if candidate:
        return candidate[:18].rstrip() + ("…" if len(candidate) > 18 else "")
    return "附件对话" if attachments else "新对话"


def _load_conversations() -> dict[str, dict]:
    try:
        if _CONVERSATIONS_PATH.exists():
            raw = json.loads(_CONVERSATIONS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                loaded = {}
                for key, value in raw.items():
                    if not isinstance(value, dict):
                        continue
                    cid = str(value.get("id") or key).strip()
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", cid):
                        continue
                    messages = value.get("messages") if isinstance(value.get("messages"), list) else []
                    task_trace.recover(messages)
                    item = _new_conversation(cid, str(value.get("title") or "新对话"), messages)
                    item["created_at"] = value.get("created_at", item["created_at"])
                    item["updated_at"] = value.get("updated_at", item["updated_at"])
                    item["started"] = bool(value.get("started") or messages)
                    selection = value.get("model_selection")
                    if isinstance(selection, dict) and isinstance(selection.get("model"), str) and selection["model"].strip():
                        item["model_selection"] = {
                            "provider_id": str(selection.get("provider_id") or "default").strip(),
                            "model": selection["model"].strip(),
                        }
                    loaded[cid] = item
                if loaded:
                    return loaded
    except Exception as exc:
        print(f"[conversations] 读取失败: {exc}")
    # 迁移旧版单一历史，保留原记录，不让升级丢上下文。
    legacy = load_chat_history()
    task_trace.recover(legacy)
    return {"default": _new_conversation("default", "默认对话", legacy)}


def _save_conversations(conversations: dict[str, dict]) -> bool:
    temp = _CONVERSATIONS_PATH.with_suffix(".tmp")
    try:
        with _CONVERSATIONS_WRITE_LOCK:
            payload = json.dumps(conversations, ensure_ascii=False)
            _CONVERSATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, _CONVERSATIONS_PATH)
        return True
    except Exception as exc:
        print(f"[conversations] 保存失败: {exc}")
        return False
    finally:
        # Frequent task checkpoints may come from several conversations at once.
        # Never unlink another writer's in-progress temporary file.
        with _CONVERSATIONS_WRITE_LOCK:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


def _conversation_id(value: object) -> str:
    candidate = str(value or "").strip()
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", candidate) else ""


def _conversation_summary(item: dict) -> dict:
    messages = item.get("messages") if isinstance(item.get("messages"), list) else []
    last = messages[-1] if messages else {}
    return {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or "新对话"),
        "created_at": item.get("created_at", 0),
        "updated_at": item.get("updated_at", 0),
        "message_count": len(messages),
        "model_selection": item.get("model_selection"),
        "preview": str(last.get("content") or "")[:120] if isinstance(last, dict) else "",
    }


DESKTOP_CONVERSATIONS: dict[str, dict] = _load_conversations()
if "default" not in DESKTOP_CONVERSATIONS:
    first_id = next(iter(DESKTOP_CONVERSATIONS), "default")
    if first_id not in DESKTOP_CONVERSATIONS:
        DESKTOP_CONVERSATIONS["default"] = _new_conversation("default", "默认对话")
DESKTOP_HISTORY: list[dict] = DESKTOP_CONVERSATIONS.get("default", next(iter(DESKTOP_CONVERSATIONS.values()))).get("messages", [])
# 兼容旧模块和旧备份格式；新的聊天写入 conversations.json。
BACKUP_STATUS = {
    "last_success": "", "last_error": "", "last_path": "",
    # worker → hub 记忆上传(让云端也有本机积累的记忆)的状态
    "last_upload": "", "last_upload_error": "", "last_upload_result": "",
}
BACKUP_LOCK = asyncio.Lock()
# 记忆同步 loop 的"设置变更"唤醒口:改了备份间隔要立刻生效,不能等旧的 sleep 走完。
_MEMORY_LOOP_WAKE: asyncio.Event | None = None
_MEMORY_LOOP: asyncio.AbstractEventLoop | None = None

# config.toml 全量重写的写锁(线程锁:_rewrite_config 可能被同步路径调用)。
# 防两个设置页同时保存互相覆盖,配合 temp+os.replace 保证要么旧内容要么新内容,
# 不会出现写一半的残缺文件(残缺会导致启动时静默回落默认值、丢掉全部密钥)。
_CONFIG_WRITE_LOCK = _threading.Lock()

_ATTACHMENT_DIR = app_home() / "data" / "uploads"
_MAX_ATTACHMENTS = 6
_MAX_ATTACHMENT_BYTES = 6 * 1024 * 1024
_MAX_ATTACHMENTS_TOTAL = 12 * 1024 * 1024
_INLINE_TEXT_LIMIT = 60_000
_ATTACHMENT_RETENTION_SECONDS = 30 * 24 * 3600
_MAX_UNREFERENCED_ATTACHMENTS = 500
_MAX_PERSISTED_SCREENSHOT_BYTES = 4 * 1024 * 1024
_MAX_CHAT_MEDIA_TRANSFER_BYTES = 20 * 1024 * 1024
_MAX_DESKTOP_REPLY_ATTACHMENT_BYTES = 50 * 1024 * 1024
_DESKTOP_REPLY_ATTACHMENTS: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar(
    "desktop_reply_attachments", default=None,
)


# Live/queued uploads are not in persisted chat history until their turn finishes.
_DESKTOP_ATTACHMENT_RUNTIMES: dict[str, dict[str, dict]] = {}


def _referenced_attachment_names() -> set[str]:
    names: set[str] = set()
    for conversation in DESKTOP_CONVERSATIONS.values():
        for message in conversation.get("messages") or []:
            for event in (message.get("task_trace") or {}).get("events", []):
                name = (event.get("raw_return") or {}).get("saved_name")
                if name:
                    names.add(Path(str(name)).name)
            for item in message.get("attachments") or []:
                name = Path(str(item.get("saved_name") or "")).name
                if name:
                    names.add(name)
    # 兼容旧测试/旧模块临时替换 DESKTOP_HISTORY 的场景。
    for message in DESKTOP_HISTORY:
        for item in message.get("attachments") or []:
            name = Path(str(item.get("saved_name") or "")).name
            if name:
                names.add(name)
    for runtimes in _DESKTOP_ATTACHMENT_RUNTIMES.values():
        for state in runtimes.values():
            groups = [state.get("attachments_in_use") or []]
            groups.extend(task.get("attachments") or [] for task in state.get("pending_inputs") or [])
            for attachments in groups:
                for item in attachments:
                    name = Path(str(item.get("path") or "")).name
                    if name:
                        names.add(name)
    return names


def _cleanup_attachments(*, remove_all_unreferenced: bool = False, now: float | None = None) -> int:
    if not _ATTACHMENT_DIR.is_dir():
        return 0
    current = time.time() if now is None else now
    referenced = _referenced_attachment_names()
    files: list[tuple[float, Path]] = []
    try:
        for path in _ATTACHMENT_DIR.iterdir():
            try:
                if path.is_file():
                    files.append((path.stat().st_mtime, path))
            except OSError:
                pass
    except OSError:
        return 0
    files.sort(key=lambda item: item[0], reverse=True)
    removed = 0
    unreferenced_index = 0
    for modified_at, path in files:
        if path.name in referenced:
            continue
        expired = current - modified_at > _ATTACHMENT_RETENTION_SECONDS
        overflow = unreferenced_index >= _MAX_UNREFERENCED_ATTACHMENTS
        unreferenced_index += 1
        if remove_all_unreferenced or expired or overflow:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def _audit_category(tool_name: str) -> str:
    name = str(tool_name or "")
    if name.startswith("code_"):
        return "code"
    if name.startswith(("fs_", "excel_", "word_", "create_")):
        return "office"
    if name.startswith(("memory_", "project_")):
        return "memory"
    if name.startswith(("tool_", "extension_")):
        return "extensions"
    if name in {"send_file_to_chat", "send_image_to_chat"}:
        return "messages"
    if name in {"screen_capture", "screen_capture_region", "inspect_ui_controls", "click_element", "click_coordinate", "triple_click",
                "move_pointer", "scroll_screen", "drag_pointer", "type_text", "press_hotkey",
                "wait_for_screen_change", "send_text_to_window", "list_windows", "focus_window",
                "launch_app", "remember_app", "open_url"}:
        return "computer"
    if name.startswith("browser_"):
        return "browser"
    if name == "run_command":
        return "system"
    return "other"


class _AuditTrace:
    """Turn Agent events into durable operation records without affecting the task."""

    def __init__(self, session_id: str, task_id: str, approval_mode: str):
        self.session_id = session_id
        self.task_id = task_id
        self.approval_mode = approval_mode
        self.pending: list[dict] = []

    def consume(self, event) -> None:
        data = event.data if isinstance(getattr(event, "data", None), dict) else {}
        if event.type == "tool_call":
            self.pending.append({
                "name": str(data.get("name") or ""),
                "arguments": data.get("arguments") or {},
                "started": time.perf_counter(),
                "approval": "not_required",
            })
            return
        if event.type == "confirm_needed":
            name = str(data.get("name") or "")
            item = next((item for item in reversed(self.pending) if item["name"] == name), None)
            if item:
                item["approval"] = "pending"
            return
        if event.type != "tool_result":
            return
        name = str(data.get("name") or "")
        item_index = next(
            (index for index in range(len(self.pending) - 1, -1, -1)
             if self.pending[index]["name"] == name), None,
        )
        item = self.pending.pop(item_index) if item_index is not None else {
            "name": name, "arguments": {}, "started": time.perf_counter(), "approval": "unknown",
        }
        result = str(data.get("result") or "")
        rejected = "用户拒绝" in result or "操作被拒绝" in result
        ok = bool(data.get("ok", True)) and not rejected
        AUDIT_LOG.record(
            session_id=self.session_id, task_id=self.task_id,
            tool_name=item["name"], category=_audit_category(item["name"]),
            parameters=item["arguments"], approval_mode=self.approval_mode,
            approval_result=("denied" if rejected else item["approval"]),
            status=("rejected" if rejected else ("ok" if ok else "failed")),
            duration_ms=(time.perf_counter() - item["started"]) * 1000,
            error="" if ok else result,
        )


def _extract_attachment_text(path: Path) -> str:
    """Extract a bounded text preview so cloud-hosted uploads remain useful to the model."""
    ext = path.suffix.lower()
    try:
        if ext in {
            ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".xml",
            ".html", ".css", ".js", ".ts", ".py", ".toml", ".yaml", ".yml", ".log",
        }:
            return path.read_text(encoding="utf-8-sig", errors="replace")[:_INLINE_TEXT_LIMIT]
        if ext == ".docx":
            from docx import Document
            doc = Document(str(path))
            parts = [p.text for p in doc.paragraphs if p.text]
            for table in doc.tables:
                parts.extend("\t".join(cell.text for cell in row.cells) for row in table.rows)
            return "\n".join(parts)[:_INLINE_TEXT_LIMIT]
        if ext in {".xlsx", ".xlsm"}:
            from openpyxl import load_workbook
            book = load_workbook(str(path), read_only=True, data_only=True)
            parts: list[str] = []
            for sheet in book.worksheets:
                parts.append(f"[{sheet.title}]")
                for row in sheet.iter_rows(values_only=True):
                    parts.append("\t".join("" if value is None else str(value) for value in row))
                    if sum(len(part) for part in parts) >= _INLINE_TEXT_LIMIT:
                        break
                if sum(len(part) for part in parts) >= _INLINE_TEXT_LIMIT:
                    break
            book.close()
            return "\n".join(parts)[:_INLINE_TEXT_LIMIT]
    except Exception:
        return ""
    return ""


_IMAGE_BUDGET_RATIO = 0.40  # 压缩后所有图片 tokens 之和占上下文窗口的上限


def _compress_attachment_image(payload: bytes, *, max_bytes: int = 120_000) -> str | None:
    """把图片附件压成 JPEG data_url(复用 computer_module.compress_screenshot 的档位阶梯)。

    失败(非图片/格式不支持/Pillow 缺失)返回 None——调用方应保守地不把未压缩大图塞进上下文。
    """
    try:
        from PIL import Image
        from backend.modules.computer.computer_module import compress_screenshot
        img = Image.open(io.BytesIO(payload))
        img.load()
        data, _width, _height = compress_screenshot(img, max_bytes=max_bytes)
        if not data:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(data).decode()
    except Exception:
        return None


def _data_url_tokens(url: str) -> int:
    """估算一个图片 data_url 的 tokens:base64 主要是 ASCII,约 1 token / 4 字符。"""
    return max(1, math.ceil(len(url) / 4))


def _cap_attachment_image_budget(prepared: list[dict], context_window: int | None = None) -> None:
    """43.8 总量兜底:压缩后所有图片 tokens 之和若仍超窗口 40%,只留最近几张,其余换文字占位。

    不许静默丢弃——裁掉的图在 note 里明确告诉用户。
    """
    images = [item for item in prepared if item.get("data_url")]
    if not images:
        return
    window = int(context_window or getattr(getattr(CFG, "agent", None), "context_window", 128000) or 128000)
    budget = int(window * _IMAGE_BUDGET_RATIO)
    tokens = [_data_url_tokens(item["data_url"]) for item in images]
    if sum(tokens) > budget:
        # ponytail: share one image budget before discarding references from a multi-image task.
        per_image_bytes = max(1, (budget // len(images) - 16) * 3)
        for item in images:
            try:
                path = Path(str(item.get("path") or ""))
                payload = path.read_bytes() if path.is_file() else base64.b64decode(item["data_url"].split(",", 1)[1], validate=True)
                smaller = _compress_attachment_image(payload, max_bytes=per_image_bytes)
            except (OSError, ValueError, IndexError):
                smaller = None
            if smaller and _data_url_tokens(smaller) < _data_url_tokens(item["data_url"]):
                item["data_url"] = smaller
        tokens = [_data_url_tokens(item["data_url"]) for item in images]
    # 从最新往旧累计,算出能保留几张(最新优先)
    keep = 0
    running = 0
    for t in reversed(tokens):
        if running + t > budget:
            break
        running += t
        keep += 1
    dropped = len(images) - keep
    if dropped <= 0:
        return
    for item in images[:dropped]:
        item.pop("data_url", None)
        item["image_skipped"] = True
        item["note"] = "这张图因多图合计超出模型窗口预算，本次未发给模型"
    if images:
        images[-1]["note"] = (images[-1].get("note") or "") + f"（多图合计超出预算，只把最后 {keep} 张图发给模型了）"


def _prepare_attachments(raw_items: object, context_window: int | None = None) -> list[dict]:
    if not raw_items:
        return []
    if not isinstance(raw_items, list) or len(raw_items) > _MAX_ATTACHMENTS:
        raise ValueError(f"一次最多上传 {_MAX_ATTACHMENTS} 个附件")

    prepared: list[dict] = []
    total = 0
    _ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_attachments()
    for item in raw_items:
        if not isinstance(item, dict):
            raise ValueError("附件格式不正确")
        name = Path(str(item.get("name") or "attachment")).name
        safe_name = re.sub(r"[^\w. -]", "_", name).strip(" .") or "attachment"
        data_url = str(item.get("data") or "")
        match = re.fullmatch(r"data:([^;,]+);base64,([A-Za-z0-9+/=\r\n]+)", data_url)
        if not match:
            raise ValueError(f"附件 {name} 数据不完整")
        mime = match.group(1).lower()
        try:
            payload = base64.b64decode(match.group(2), validate=True)
        except Exception as exc:
            raise ValueError(f"附件 {name} 无法读取") from exc
        if len(payload) > _MAX_ATTACHMENT_BYTES:
            raise ValueError(f"附件 {name} 超过 6 MB")
        total += len(payload)
        if total > _MAX_ATTACHMENTS_TOTAL:
            raise ValueError("附件总大小不能超过 12 MB")

        path = _ATTACHMENT_DIR / f"{uuid.uuid4().hex[:12]}-{safe_name}"
        path.write_bytes(payload)
        prepared_item = {
            "name": name,
            "type": mime,
            "path": str(path.resolve()),
            "text": _extract_attachment_text(path),
        }
        if mime.startswith("image/"):
            # 43.5:原图已按原样存盘(上面的 path.write_bytes(payload)),这里只把
            # 压缩后的版本放进 data_url(送模型那份),用户点开附件看的仍是原图。
            compressed_url = _compress_attachment_image(payload)
            if compressed_url:
                prepared_item["data_url"] = compressed_url
            else:
                prepared_item["image_skipped"] = True
                prepared_item["note"] = "这张图太大没能压缩，已跳过不发给模型"
        prepared.append(prepared_item)
    _cap_attachment_image_budget(prepared, context_window)
    return prepared


def _attachment_history_items(attachments: list[dict]) -> list[dict]:
    """Keep only the small, safe fields needed to restore attachment UI."""
    items: list[dict] = []
    for attachment in attachments:
        path = Path(str(attachment.get("path") or ""))
        if not path.name:
            continue
        items.append({
            "name": str(attachment.get("name") or "附件"),
            "type": str(attachment.get("type") or "application/octet-stream"),
            "saved_name": path.name,
        })
    return items


def _restore_latest_attachments(history: list[dict], context_window: int | None = None) -> list[dict]:
    """Restore the latest user-uploaded group, including document previews and paths."""
    for message in reversed(history):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        items = message.get("attachments")
        if not isinstance(items, list) or not items:
            continue
        restored: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            path = _saved_attachment_path(str(item.get("saved_name") or ""))
            attachment = {"name": str(item.get("name") or item.get("saved_name") or "附件"),
                          "type": str(item.get("type") or "application/octet-stream"),
                          "note": "沿用本对话最近上传的附件"}
            if path is None:
                attachment["note"] = "该附件不存在或保存路径无效；请重新上传，不要把其他旧附件当作本文件"
                restored.append(attachment)
                continue
            attachment["path"] = str(path.resolve())
            attachment["text"] = _extract_attachment_text(path)
            if attachment["type"].lower().startswith("image/"):
                try:
                    data_url = _compress_attachment_image(path.read_bytes())
                except OSError:
                    data_url = None
                if data_url:
                    attachment["data_url"] = data_url
                else:
                    attachment["note"] += "；图像暂时无法读取，原文件路径仍保留"
            restored.append(attachment)
        if context_window is None:
            _cap_attachment_image_budget(restored)
        else:
            _cap_attachment_image_budget(restored, context_window)
        # A missing newest group must not silently become a different older group.
        return restored
    return []


def _restore_latest_image_attachments(history: list[dict]) -> list[dict]:
    """Compatibility for callers explicitly requesting only available user images."""
    return [item for item in _restore_latest_attachments(history) if item.get("data_url")]



def _save_desktop_reply_attachment(source: Path) -> dict:
    size = source.stat().st_size
    if size > _MAX_DESKTOP_REPLY_ATTACHMENT_BYTES:
        raise ValueError("文件超过 50 MB，暂不能附加到小A回复")
    _ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w. -]", "_", source.name).strip(" .") or "attachment"
    saved = _ATTACHMENT_DIR / f"{uuid.uuid4().hex[:12]}-{name}"
    shutil.copyfile(source, saved)
    return {
        "name": source.name,
        "type": mimetypes.guess_type(source.name)[0] or "application/octet-stream",
        "path": str(saved.resolve()),
    }


async def _deliver_worker_media(
    payload: dict, channel: tuple[str, ...] | None,
    attachments: list[dict] | None,
) -> str:
    """Validate a bounded worker file payload, then deliver it from the hub."""
    try:
        name = Path(str(payload.get("name") or "attachment")).name
        if not name or name in {".", ".."}:
            raise ValueError("本机返回的文件名无效")
        encoded = payload.get("data")
        max_encoded = ((_MAX_CHAT_MEDIA_TRANSFER_BYTES + 2) // 3) * 4
        if not isinstance(encoded, str) or len(encoded) > max_encoded:
            raise ValueError("本机文件超过 20 MB 的云端传输限制")
        raw = base64.b64decode(encoded, validate=True)
        if not raw or len(raw) > _MAX_CHAT_MEDIA_TRANSFER_BYTES:
            raise ValueError("本机文件为空或超过 20 MB 的云端传输限制")
        mime = str(payload.get("mime") or mimetypes.guess_type(name)[0] or "application/octet-stream")
        is_image = payload.get("kind") == "image"
        if is_image and not mime.startswith("image/"):
            raise ValueError(f"{name} 不是图片")

        if channel:
            source, target, account_id = _channel_parts(channel)
            with tempfile.NamedTemporaryFile(prefix="xiaoa-chat-", suffix=Path(name).suffix, delete=False) as temp:
                temp.write(raw)
                temp_path = Path(temp.name)
            try:
                if source == "weixin":
                    from backend.modules.chat import weixin_clawbot
                    if is_image:
                        await weixin_clawbot.send_image(target, raw, _CHANNEL_CONTEXT_TOKEN.get(), **_weixin_send_account(channel))
                    else:
                        await weixin_clawbot.send_file(
                            target, str(temp_path), _CHANNEL_CONTEXT_TOKEN.get(), file_name=name, **_weixin_send_account(channel),
                        )
                elif source == "feishu":
                    from backend.modules.chat import feishu_module
                    if is_image:
                        await feishu_module.send_image(target, raw, **_feishu_send_type())
                    else:
                        await feishu_module.send_file(target, str(temp_path), file_name=name, **_feishu_send_type())
                else:
                    return f"{source} 渠道暂不支持发送文件。"
            finally:
                temp_path.unlink(missing_ok=True)
            return f"已把{'图片' if is_image else '文件'}发到{source}:{name}"

        if attachments is None:
            return "当前任务没有可接收本机附件的聊天上下文。"
        with tempfile.NamedTemporaryFile(prefix="xiaoa-chat-", suffix=Path(name).suffix, delete=False) as temp:
            temp.write(raw)
            temp_path = Path(temp.name)
        try:
            attachment = _save_desktop_reply_attachment(temp_path)
            attachment["name"] = name
            attachment["type"] = mime
            if is_image and not attachment["type"].startswith("image/"):
                Path(attachment["path"]).unlink(missing_ok=True)
                return f"文件不是可识别的图片格式：{name}"
            attachments.append(attachment)
        finally:
            temp_path.unlink(missing_ok=True)
        return f"已把{'图片' if is_image else '文件'}附加到当前小A回复：{name}"
    except (ValueError, OSError) as exc:
        return f"本机文件回传失败：{exc}"


def _worker_media_payload(path: str, kind: str) -> dict:
    """Read one bounded local file for delivery by the hub's chat credentials."""
    resolved_path = _resolve_chat_file_path(path)
    size = resolved_path.stat().st_size
    if size <= 0:
        raise ValueError("文件为空")
    if size > _MAX_CHAT_MEDIA_TRANSFER_BYTES:
        raise ValueError("文件超过 20 MB 的云端传输限制")
    with resolved_path.open("rb") as source:
        raw = source.read(_MAX_CHAT_MEDIA_TRANSFER_BYTES + 1)
    if len(raw) > _MAX_CHAT_MEDIA_TRANSFER_BYTES:
        raise ValueError("文件超过 20 MB 的云端传输限制")

    mime = mimetypes.guess_type(resolved_path.name)[0] or "application/octet-stream"
    if kind == "image":
        signatures = (
            (b"\x89PNG\r\n\x1a\n", "image/png"),
            (b"\xff\xd8\xff", "image/jpeg"),
            (b"GIF87a", "image/gif"),
            (b"GIF89a", "image/gif"),
            (b"BM", "image/bmp"),
        )
        actual_mime = next((value for marker, value in signatures if raw.startswith(marker)), None)
        if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            actual_mime = "image/webp"
        if not actual_mime:
            raise ValueError(f"{resolved_path.name} 不是有效的图片文件")
        mime = actual_mime

    return {
        "_xiaoa_hub_media": True,
        "kind": kind,
        "name": resolved_path.name,
        "mime": mime,
        "data": base64.b64encode(raw).decode("ascii"),
    }


def _save_persisted_screenshot(image_b64: str) -> dict | None:
    """保存本轮最后一张截图，历史只记录文件元数据，不保存 Base64。"""
    raw = str(image_b64 or "").strip()
    if not raw:
        return None
    try:
        payload = base64.b64decode(raw, validate=True)
    except (ValueError, base64.binascii.Error):
        return None
    if not payload or len(payload) > _MAX_PERSISTED_SCREENSHOT_BYTES:
        return None
    _ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
    path = _ATTACHMENT_DIR / f"{uuid.uuid4().hex[:12]}-xiaoa-screen.jpg"
    try:
        path.write_bytes(payload)
    except OSError:
        return None
    return {
        "name": "小A看到的屏幕.jpg",
        "type": "image/jpeg",
        "saved_name": path.name,
    }


def _saved_attachment_path(saved_name: str) -> Path | None:
    """Resolve one upload by basename without allowing directory traversal."""
    clean_name = Path(saved_name).name
    if not clean_name or clean_name != saved_name:
        return None
    path = _ATTACHMENT_DIR / clean_name
    return path if path.is_file() else None


def _hub_http_base() -> str:
    """把 worker 配的 hub WebSocket 地址转成 HTTP 基址。
    ws://1.2.3.4:1985/worker -> http://1.2.3.4:1985
    """
    url = CFG.role.hub_url or ""
    url = url.replace("wss://", "https://").replace("ws://", "http://")
    # 去掉末尾的 /worker 路径
    if "/worker" in url:
        url = url.split("/worker")[0]
    return url.rstrip("/")


def _hub_http_client(timeout: float):
    """Connect directly to the configured hub with the same TLS policy as worker."""
    import httpx

    return httpx.AsyncClient(
        timeout=timeout, trust_env=False, verify=hub_ssl_context(CFG.role.tls_fingerprint),
    )


async def _collect_memory_export_files(*, include_shared: bool = True) -> dict[str, bytes]:
    if MEMORY is None:
        raise RuntimeError("记忆尚未就绪")
    files = {
        "memory.sqlite3": await asyncio.to_thread(MEMORY.export_snapshot),
    }
    for path in (
        CFG.memory_data_dir / "vector_memory.jsonl",
        CFG.memory_data_dir / "simple_memory.json",
        _HISTORY_PATH,
    ):
        if path.exists():
            files[path.name] = path.read_bytes()
    files.update(await asyncio.to_thread(_weixin_memories().export_files, include_shared=include_shared))
    return files


def _write_memory_backup(files: dict[str, bytes], *, projects: bool = False) -> Path:
    """Write one complete snapshot atomically and prune old snapshots."""
    import shutil
    from datetime import datetime

    backup_dir = CFG.memory_backup_dir / "project-libraries" if projects else CFG.memory_backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / stamp
    suffix = 1
    while target.exists():
        target = backup_dir / f"{stamp}-{suffix}"
        suffix += 1
    temp_dir = backup_dir / f".{target.name}-{uuid.uuid4().hex[:8]}.tmp"
    temp_dir.mkdir()
    try:
        for name, content in files.items():
            safe_name = Path(name).name
            if safe_name:
                (temp_dir / safe_name).write_bytes(content)
        (temp_dir / "manifest.json").write_text(json.dumps({
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "role": CFG.role.mode,
            "files": sorted(files),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_dir.replace(target)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    snapshots = sorted(
        path for path in backup_dir.iterdir()
        if path.is_dir() and re.fullmatch(r"\d{8}-\d{6}(?:-\d+)?", path.name)
    )
    keep = max(2, min(int(CFG.memory.backup_keep_count), 365))
    for old in snapshots[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
    return target


def _seconds_until_next_memory_backup() -> float:
    interval = max(5, min(int(CFG.memory.backup_interval_minutes), 7 * 24 * 60)) * 60
    try:
        latest = max(
            path.stat().st_mtime for path in CFG.memory_backup_dir.iterdir()
            if path.is_dir() and re.fullmatch(r"\d{8}-\d{6}(?:-\d+)?", path.name)
        )
    except (FileNotFoundError, ValueError, OSError):
        return 0
    return max(0, interval - (time.time() - latest))


async def _merge_memory_snapshot_into_runtime(files: dict[str, bytes]) -> dict[str, int]:
    """Merge the hub's shared long-term memory into this worker's live memory."""
    snapshot = files.get("memory.sqlite3")
    if MEMORY is None or snapshot is None:
        raise RuntimeError("云端备份缺少可合并的记忆快照")
    result = await asyncio.to_thread(MEMORY.import_snapshot, snapshot)
    # Worker downloads never restore project data/ACLs, including an old hub payload.
    project_free = {name: data for name, data in files.items() if name != "shared-memory.sqlite3" and not name.startswith(("shared-library-", "shared-libraries"))}
    private = await asyncio.to_thread(_weixin_memories().import_files, project_free, authoritative=True)
    return {key: result[key] + private[key] for key in ("added", "skipped")}


_LAST_MEMORY_BACKUP = [None, None]


async def _run_memory_backup_once(*, skip_unchanged: bool = False) -> Path:
    import base64

    async with BACKUP_LOCK:
        if CFG.role.is_hub:
            raise RuntimeError("云端 hub 不能代替本地电脑保存备份")
        if CFG.role.is_worker:
            base = _hub_http_base()
            if not base:
                raise RuntimeError("worker 未配置 hub_url")
            async with _hub_http_client(60) as client:
                response = await client.get(
                    base + "/api/memory/export",
                    headers={"x-hub-token": CFG.role.hub_token},
                )
            if response.status_code != 200:
                raise RuntimeError(f"云端返回 HTTP {response.status_code}")
            encoded = (response.json() or {}).get("files", {})
            files = {
                Path(name).name: base64.b64decode(content, validate=True)
                for name, content in encoded.items()
                if Path(name).name != "shared-memory.sqlite3" and not Path(name).name.startswith(("shared-library-", "shared-libraries"))
            }
            # Worker uses the same downloaded cloud snapshot both for the backup
            # and for its live MEMORY, so remote-channel memories become searchable.
            await _merge_memory_snapshot_into_runtime(files)
        else:
            files = await _collect_memory_export_files()
        if not files:
            raise RuntimeError("没有可备份的记忆文件")
        import hashlib
        digest = hashlib.sha256()
        for name, content in sorted(files.items()):
            digest.update(name.encode("utf-8") + b"\0")
            digest.update(hashlib.sha256(content).digest())
        key = (str(CFG.memory_backup_dir), digest.digest())
        previous_key, previous_path = _LAST_MEMORY_BACKUP
        if skip_unchanged and key == previous_key and previous_path is not None and previous_path.is_dir():
            return previous_path
        target = await asyncio.to_thread(_write_memory_backup, files)
        _LAST_MEMORY_BACKUP[:] = [key, target]
        BACKUP_STATUS.update({
            "last_success": datetime_now_iso(), "last_error": "", "last_path": str(target),
        })
        print(f"[backup] 记忆已备份到本地 {target}({len(files)} 个文件)")
        return target


async def _run_memory_upload_once() -> dict:
    """worker → hub:把本机记忆快照推给云端,让云端也有本机积累的记忆。

    hub 侧按 (namespace, content_hash, source_path, source_ref) 去重合并,不删云端已有的。
    返回云端给的 {"added": n, "skipped": n}。
    """
    audit = _memory_sync_audit()
    request_id = str(uuid.uuid4())
    device_id = audit.device_id
    audit.record(request_id=request_id, device_id=device_id, status="started")
    try:
        return await _upload_memory_snapshot(audit, request_id, device_id)
    except Exception as exc:
        audit.record(
            request_id=request_id, device_id=device_id, status="failed",
            error=redact_parameters(str(exc)),
        )
        raise


async def _upload_memory_snapshot(audit: MemorySyncAudit, request_id: str, device_id: str) -> dict:
    import base64

    if CFG.role.is_hub:
        raise RuntimeError("hub 是记忆的接收方,不需要上传")
    if not CFG.role.is_worker:
        raise RuntimeError("只有 worker 需要上传记忆")
    base = _hub_http_base()
    if not base:
        raise RuntimeError("worker 未配置 hub_url")
    files = await _collect_memory_export_files(include_shared=False)
    if not files:
        raise RuntimeError("没有可上传的记忆文件")
    encoded = {name: base64.b64encode(content).decode() for name, content in files.items()}
    async with _hub_http_client(120) as client:
        response = await client.post(
            base + "/api/memory/import",
            json={"files": encoded, "request_id": request_id, "device_id": device_id},
            headers={"x-hub-token": CFG.role.hub_token},
        )
    if response.status_code != 200:
        raise RuntimeError(f"云端返回 HTTP {response.status_code}")
    body = response.json() or {}
    if not body.get("ok"):
        raise RuntimeError(f"云端导入失败:{body.get('error') or '未知错误'}")
    audit.record(request_id=request_id, device_id=device_id, status="succeeded")
    BACKUP_STATUS.update({
        "last_upload": datetime_now_iso(),
        "last_upload_error": "",
        "last_upload_result": f"新增 {body.get('added', 0)} 条,已存在 {body.get('skipped', 0)} 条",
    })
    print(f"[memory-sync] 本机记忆已推给云端:新增 {body.get('added', 0)} 条,"
          f"已存在 {body.get('skipped', 0)} 条")
    return body


def _wake_memory_loop() -> None:
    """设置变更(备份间隔)时唤醒记忆同步 loop,让它立刻用新间隔,不用等旧的 sleep 走完。"""
    event, loop = _MEMORY_LOOP_WAKE, _MEMORY_LOOP
    if event is None or loop is None:
        return
    try:
        loop.call_soon_threadsafe(event.set)
    except Exception:
        pass


async def _sleep_or_wake(seconds: float) -> bool:
    """睡 seconds,或被设置变更唤醒。返回 True 表示被唤醒(调用方应重算间隔)。"""
    event = _MEMORY_LOOP_WAKE
    if event is None:
        await asyncio.sleep(seconds)
        return False
    try:
        await asyncio.wait_for(event.wait(), timeout=max(0.0, seconds))
        event.clear()
        return True
    except asyncio.TimeoutError:
        return False


def _memory_source_fingerprint(*, include_shared: bool) -> tuple:
    """Cheap change detection, including SQLite WAL and every exported store."""
    private = _weixin_memories()
    paths = {CFG.memory_data_dir / name for name in (
        "memory.sqlite3", "vector_memory.jsonl", "simple_memory.json",
    )}
    paths.add(_HISTORY_PATH)
    paths.add(private.catalog)
    paths.update((private.root / "accounts").glob("*/memory.sqlite3"))
    if include_shared:
        paths.update((private.shared, private.shared.parent / "shared-libraries.sqlite3"))
        paths.update((private.shared.parent / "shared-libraries").glob("*.sqlite3"))
    paths.update(Path(str(path) + suffix) for path in tuple(paths)
                 if path.suffix == ".sqlite3" for suffix in ("-wal", "-shm"))
    state = [("context", CFG.role.mode, str(CFG.memory_backup_dir),
              CFG.role.hub_url, CFG.role.hub_token)]
    for path in sorted(paths):
        try:
            stat = path.stat()
            state.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except FileNotFoundError:
            state.append((str(path), None))
    return tuple(state)


async def _memory_backup_loop() -> None:
    """定时同步记忆。

    worker:先把本机记忆推给云端(云端做真相源),再把云端记忆拉回本地备份。
    standalone:只做本地备份。间隔用设置里的"备份间隔(分钟)",改了立刻生效。
    """
    global _MEMORY_LOOP_WAKE, _MEMORY_LOOP
    _MEMORY_LOOP = asyncio.get_running_loop()
    _MEMORY_LOOP_WAKE = asyncio.Event()
    last_uploaded = last_backed_up = None
    last_backup_path = None
    await _sleep_or_wake(120)  # 启动后先等一会儿,别和启动抢资源
    while True:
        success = False
        try:
            before = await asyncio.to_thread(_memory_source_fingerprint, include_shared=not CFG.role.is_worker)
            if CFG.role.is_worker:
                if before != last_uploaded:
                    await _run_memory_upload_once()
                    after = await asyncio.to_thread(_memory_source_fingerprint, include_shared=False)
                    # A write during export/upload must be included on the next pass.
                    last_uploaded = before if before == after else None
                # Always fetch cloud changes, even if this machine has nothing new.
                await _run_memory_backup_once(skip_unchanged=True)
            elif before != last_backed_up or last_backup_path is None or not last_backup_path.is_dir():
                last_backup_path = await _run_memory_backup_once()
                after = await asyncio.to_thread(_memory_source_fingerprint, include_shared=True)
                last_backed_up = before if before == after else None
            BACKUP_STATUS["last_error"] = ""
            if CFG.role.is_worker:
                BACKUP_STATUS["last_upload_error"] = ""
            success = True
        except Exception as e:
            message = f"{type(e).__name__}: {e}"
            BACKUP_STATUS["last_error"] = message
            if CFG.role.is_worker:
                BACKUP_STATUS["last_upload_error"] = message
            print(f"[memory-sync] 同步出错(下次再试): {message}")
        interval = max(5, min(int(CFG.memory.backup_interval_minutes), 7 * 24 * 60)) * 60
        await _sleep_or_wake(interval if success else min(300, interval))


def datetime_now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def _memory_sync_audit() -> MemorySyncAudit:
    return MemorySyncAudit(CFG.memory_data_dir / "sync_audit.sqlite3")


def _register_operation_tools(reg: ToolRegistry | None = None) -> None:
    """注册当前机器可执行的操作工具；各模块自行跳过不支持的平台能力。"""
    global USER_TOOL_MANAGER, SKILL_MANAGER, EXTENSION_MANAGER, CODE_PROCESS_MANAGER, BROWSER_CONTROLLER
    reg = reg or REGISTRY
    workspace = isolation_workspace.workspace()
    fs_module.set_workspace_root(str(workspace))
    fs_module.register_tools(reg)
    office_module.register_tools(reg)
    # CAD is a physical desktop capability. In hub mode it must come from the
    # connected Windows worker, never from the Linux cloud host itself.
    if not CFG.role.is_hub:
        cad_module.register_tools(reg)
    docgen_module.register_tools(reg)
    computer_module.register_tools(reg, lambda: LLM)
    system_control.register_tools(reg)
    browser = browser_module.BrowserController(app_home() / "data" / "browser-profile")
    browser_module.register_tools(reg, browser)
    code_processes = code_module.register_tools(reg)
    memory_tool_module.register_project_tools(reg, _project_memory_for_current_channel)
    manager = user_tool_module.register_tools(reg, app_home() / "tools")
    skills = skill_module.SkillManager(app_home() / "skills")
    extensions = extension_module.ExtensionManager(manager, skills)
    extension_module.register_tools(reg, extensions)
    register_schedule_tools(reg)
    # The hub runs on Linux without Paramiko; cloud_* tools are local-worker
    # conveniences for managing a remote server and must not be imported there.
    if not CFG.role.is_hub:
        from backend.modules.cloud import cloud_module
        cloud_module.register_tools(reg, app_home())
    if reg is REGISTRY:
        USER_TOOL_MANAGER = manager
        SKILL_MANAGER = skills
        EXTENSION_MANAGER = extensions
        CODE_PROCESS_MANAGER = code_processes
        BROWSER_CONTROLLER = browser


def _active_skill_prompt(user_input: str) -> str:
    return SKILL_MANAGER.build_prompt(user_input) if SKILL_MANAGER is not None else ""


def _worker_tools_cache_path() -> Path:
    return app_home() / "data" / "worker_tools.json"


def _sync_worker_tools(
    capabilities: list[dict], *, persist: bool = True, available: bool = True,
) -> None:
    """Mirror the worker's actual capabilities into the hub as live proxies."""
    if HUB_BRIDGE is None:
        return
    for name in list(_WORKER_PROXY_TOOLS):
        REGISTRY.unregister(name)
    _WORKER_PROXY_TOOLS.clear()
    accepted = []
    for item in capabilities[:200]:
        schema = item.get("schema") if isinstance(item, dict) else None
        function = schema.get("function") if isinstance(schema, dict) else None
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "")
        description = str(function.get("description") or "")
        parameters = function.get("parameters")
        if not name or len(name) > 64 or not isinstance(parameters, dict):
            continue

        existing = REGISTRY.get(name)
        if existing is not None:
            # Most duplicate names intentionally remain cloud-local.  Chat
            # media is different: the path belongs to the worker's desktop,
            # so the worker implementation must be proxied to the hub.
            if name not in _WORKER_AUTHORITATIVE_CHANNEL_TOOLS:
                continue
            REGISTRY.unregister(name)

        def make_proxy(tool_name: str):
            async def proxy(**kwargs):
                is_media_tool = tool_name in _WORKER_AUTHORITATIVE_CHANNEL_TOOLS
                channel = CURRENT_CHANNEL.get() if is_media_tool else None
                attachments = _DESKTOP_REPLY_ATTACHMENTS.get() if is_media_tool else None
                dispatch_args = dict(kwargs)
                if channel or attachments is not None:
                    dispatch_args["_return_to_hub"] = True
                result = await HUB_BRIDGE.dispatch(tool_name, dispatch_args)
                worker_result = result.get("result", "")
                if (result.get("ok") and isinstance(worker_result, dict)
                        and worker_result.get("_xiaoa_hub_media") is True):
                    return await _deliver_worker_media(worker_result, channel, attachments)
                # 离线标记必须进模型上下文；在线截图结果也必须保留完整对象。
                return result if result.get("offline") else result.get("result", "")
            return proxy

        availability_note = "" if available else "（电脑当前离线；开机并连接 worker 后才能使用）"
        REGISTRY.register(Tool(
            name=name, description=description + "(由本地电脑执行)" + availability_note, parameters=parameters,
            handler=make_proxy(name), side_effect=bool(item.get("side_effect", True)),
            danger=bool(item.get("danger", False)),
        ))
        _WORKER_PROXY_TOOLS.add(name)
        accepted.append(item)
    if persist:
        try:
            path = _worker_tools_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(accepted, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, path)
        except Exception as e:
            print(f"[hub] 保存 worker 工具清单失败: {e}")


def _load_cached_worker_tools() -> None:
    """Keep the last worker manifest for status/reconnect, never expose it offline."""
    path = _worker_tools_cache_path()
    try:
        if path.is_file():
            tools = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(tools, list):
                if HUB_BRIDGE is not None:
                    HUB_BRIDGE.set_cached_tool_count(min(len(tools), 200))
                print(f"[hub] 保留上次 worker 工具清单缓存: {min(len(tools), 200)} 个；离线工具不注册")
    except Exception as e:
        print(f"[hub] 读取 worker 工具清单失败: {e}")


def _host_is_loopback(host: str) -> bool:
    value = host.strip().strip("[]")
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _password_env_warning() -> str:
    if (CFG.auth.password_env_managed and not os.getenv("AGENT_WEB_PASSWORD")
            and not CFG.auth.password):
        return (
            "网页密码曾由 AGENT_WEB_PASSWORD 接管，但该环境变量当前不存在，"
            "网页密码已关闭。请恢复该环境变量或在设置页设置新密码。"
        )
    return ""


def _parent_process_alive() -> bool:
    """Check both Electron PID and creation time so a reused PID cannot keep us alive."""
    raw_pid = os.getenv("XIAOA_PARENT_PID", "").strip()
    if not raw_pid:
        return True  # 手动启动后端没有宿主父进程，不应自行退出。
    try:
        pid = int(raw_pid)
        if pid <= 0 or pid == os.getpid():
            return True
        # Windows os.kill(..., 0) maps to TerminateProcess, so use the
        # read-only OpenProcess/GetProcessTimes path there.
        if os.name == "nt":
            actual = _process_creation_time_ms(pid)
            if actual is None:
                return True
        else:
            os.kill(pid, 0)
            actual = None
    except PermissionError:
        return True
    except (ValueError, OverflowError, OSError, ProcessLookupError):
        return False
    expected_raw = os.getenv("XIAOA_PARENT_START_TIME", "").strip()
    if expected_raw:
        try:
            expected = float(expected_raw)
            if actual is None:
                actual = _process_creation_time_ms(pid)
        except (ValueError, OverflowError):
            return True  # 旧启动器/异常值回退到 PID 检查，保持兼容。
        except PermissionError:
            return True  # 无权读取时宁可保留后端，不误杀别的进程。
        except (OSError, ProcessLookupError):
            return False
        if actual is None:
            return True
        return abs(actual - expected) <= 1000
    return True


def _process_creation_time_ms(pid: int) -> float | None:
    """Read a process creation timestamp without adding psutil to the bundle."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 5:
                raise PermissionError(error, "OpenProcess")
            raise ProcessLookupError(error, "OpenProcess")
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            if not ctypes.windll.kernel32.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(exit_time),
                ctypes.byref(kernel_time), ctypes.byref(user_time),
            ):
                raise OSError(ctypes.get_last_error(), "GetProcessTimes")
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return ticks / 10000 - 11644473600000
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
    ticks = int(fields[21])
    hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    boot = next(
        float(line.split()[1]) for line in Path("/proc/stat").read_text(encoding="ascii").splitlines()
        if line.startswith("btime ")
    )
    return (boot + ticks / hz) * 1000


async def _parent_process_watch() -> None:
    """Catch an Electron task-manager kill so the worker cannot stay online."""
    while True:
        await asyncio.sleep(2)
        if not _parent_process_alive():
            print("[shutdown] Electron 宿主已退出，停止孤儿后端")
            if os.name == "nt" and _UVICORN_SERVER is not None:
                _UVICORN_SERVER.should_exit = True
            else:
                os.kill(os.getpid(), signal.SIGTERM)
            return


def _validate_startup_auth() -> None:
    if CFG.role.is_hub and not CFG.auth.enabled:
        raise RuntimeError(
            "hub 模式必须设置网页密码(auth.password 或环境变量 AGENT_WEB_PASSWORD)才能启动,"
            "否则公网上任何人都能操作你的电脑和读取记忆。请在 /etc/xiaoa-hub.env 里配置后重启。"
        )
    if CFG.role.is_standalone and not CFG.auth.enabled and not _host_is_loopback(CFG.server.host):
        raise RuntimeError(
            "standalone 模式监听非回环地址时必须设置网页密码"
            "(auth.password 或环境变量 AGENT_WEB_PASSWORD)；"
            "如仅本机使用，请把 server.host 改为 127.0.0.1。"
        )


async def _run_worker_tool(tool: str, args: dict) -> Any:
    loop = asyncio.get_running_loop()
    worker_task = asyncio.current_task()
    notice_token = system_control.set_foreground_notice_callback(
        lambda active: _schedule_foreground_notice(loop, active),
        on_escape=lambda: loop.call_soon_threadsafe(worker_task.cancel),
    )
    try:
        return await REGISTRY.call(tool, args)
    finally:
        system_control.reset_foreground_notice_callback(notice_token)


async def _replace_worker_bridge(*, start: bool = True) -> None:
    global WORKER_BRIDGE, _WORKER_BRIDGE_TASK
    old_bridge, old_task = WORKER_BRIDGE, _WORKER_BRIDGE_TASK
    WORKER_BRIDGE, _WORKER_BRIDGE_TASK = None, None
    if old_bridge is not None:
        old_bridge.stop()
    if old_task is not None:
        old_task.cancel()
        await asyncio.gather(old_task, return_exceptions=True)
    if not start or not CFG.role.is_worker or not CFG.role.hub_url:
        return
    WORKER_BRIDGE = WorkerBridge(
        CFG.role.hub_url, CFG.role.hub_token, CFG.role.worker_id, _run_worker_tool,
        get_tools=REGISTRY.capabilities, tls_fingerprint=CFG.role.tls_fingerprint,
        on_notice=_on_worker_notice, on_model_order=_apply_cloud_model_order,
    )
    _WORKER_BRIDGE_TASK = asyncio.create_task(WORKER_BRIDGE.run_forever())
    print(f"[startup] worker 模式:开始连接 hub {CFG.role.hub_url}")


class _UITokenLogFilter(logging.Filter):
    def filter(self, record):
        def redact(value):
            return re.sub(r"([?&]ui_token=)[^&\s\"'<>]*", r"\1[REDACTED]", value) if isinstance(value, str) else value
        record.msg = redact(record.msg)
        if isinstance(record.args, dict):
            record.args = {key: redact(value) for key, value in record.args.items()}
        else:
            record.args = tuple(redact(value) for value in record.args)
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    global MEMORY, _PARENT_MONITOR_TASK
    llm_logger = logging.getLogger("backend.core.llm")
    if not llm_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        llm_logger.addHandler(handler)
    llm_logger.setLevel(logging.INFO)
    llm_logger.propagate = False
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, _UITokenLogFilter) for item in logger.filters):
            logger.addFilter(_UITokenLogFilter())
    started = time.monotonic()
    print(f"[startup] server import to lifespan +{(started - _IMPORT_STARTED) * 1000:.0f}ms unix_ms={round(time.time() * 1000)}")
    role = CFG.role.mode
    print(f"[startup] 角色 = {role}")
    pending_plaintext = [name for name in _PLAINTEXT_SECRET_ENV_VARS
                         if not (os.name == "nt" and name == "AGENT_FEISHU_APP_SECRET")]
    if pending_plaintext:
        print(
            "[security] config.toml 仍含明文凭据。旧配置会继续生效；"
            "建议按 docs/密钥迁移.md 迁移到环境变量: "
            + ", ".join(pending_plaintext)
        )
    if _load_channel_lock():
        CHANNEL_LOCKED["locked"] = True
        print("[startup] 远程遥控处于锁定状态，需在本机设置页解除。")

    if warning := _password_env_warning():
        print(f"[security] {warning}")
    _validate_startup_auth()

    # 生成本机 UI 令牌(给 file:// 的 Electron 悬浮球用)。hub 公网模式也生成,但公网
    # 客户端走密码鉴权、不靠这个,所以无副作用。
    _ensure_ui_token()
    # 大目录清理错峰、串行执行，不在启动时让多个扫描线程争用磁盘。
    cleanup_task = asyncio.create_task(
        _startup_cache_cleanup(), name="cache-cleanup-startup",
    )

    if CFG.role.is_hub:
        # 先注册 Linux 云服务器本机能力；同名 Windows 工具不会覆盖它，
        # 其余 Windows 能力再以 worker 实际上报的清单补入。
        _register_operation_tools()
        # Chat media paths belong to the connected worker.  The hub exposes
        # these tools only after the worker manifest arrives, avoiding a cloud
        # fallback that cannot see the local Windows filesystem.
        memory_tool_module.register_memory_tools(REGISTRY, _memory_for_current_channel)
        _load_cached_worker_tools()
    else:
        # standalone / worker:注册全部本机操作工具。
        # worker 的 agent 也跑在本机(用本地 REGISTRY 和本地 MEMORY),memory_* 工具同样要注册。
        _register_operation_tools()
        register_channel_tools(REGISTRY)
        memory_tool_module.register_memory_tools(REGISTRY, _memory_for_current_channel)

    print(f"[startup] tools ready +{(time.monotonic() - started) * 1000:.0f}ms")
    print(f"[startup] 已注册工具: {REGISTRY.names()}")
    if os.getenv("XIAOA_PARENT_PID"):
        _PARENT_MONITOR_TASK = asyncio.create_task(_parent_process_watch())

    # SQLite 记忆在后台初始化；ONNX 向量模型等首次使用记忆时才加载。
    # 数据库就绪前 recall 先返回空，就绪后关键词记忆立即可用。
    def _init_memory():
        global MEMORY
        try:
            memory = MemoryModule(CFG.memory, CFG.memory_data_dir, lambda: LLM)
            MEMORY = memory
            print(f"[startup] 关键词记忆已就绪 +{(time.monotonic() - started) * 1000:.0f}ms")
        except Exception as e:
            print(f"[startup] 记忆初始化失败(先不影响对话): {e}")

    import threading
    threading.Thread(target=_init_memory, daemon=True).start()

    # 放后台线程启动——飞书长连接的建立会阻塞几秒,若放在这里会拖住 uvicorn 启动,
    # 导致前端白屏、连不上(实测就是它,不是记忆)。回调仍用主事件循环(loop 已捕获)。
    _loop = asyncio.get_running_loop()
    threading.Thread(target=lambda: _start_chat_listeners(_loop), daemon=True).start()

    # Load local WeChat state in every process, including workers.  A worker
    # must have its local session available before local scheduled tasks can
    # send reminders to WeChat.
    from backend.modules.chat import weixin_clawbot

    async def _weixin_message(source: str, target: str, text: str, context_token: str, account_id: str = "") -> None:
        await handle_channel_command(source, target, text, context_token=context_token, account_id=account_id)

    # 微信状态文件读取和监听器初始化放到后台，避免首次启动等待渠道网络。
    weixin_init_task = asyncio.create_task(
        weixin_clawbot.initialize(
            app_home() / "data" / "weixin-clawbot.json", _weixin_message,
            _sync_weixin_memory_accounts,
        ),
        name="weixin-clawbot-init",
    )
    # The route list is metadata only. Refresh it in the background so the
    # first ordinary desktop message never waits for SSH.
    global _SHARED_ROUTE_REFRESH_TASK
    _SHARED_ROUTE_REFRESH_TASK = asyncio.create_task(_refresh_shared_route_names_async(), name="shared-route-refresh")

    # worker:拨号连 hub,听 hub 派活并用本机工具执行
    backup_task = None
    if CFG.role.is_worker:
        if not CFG.role.hub_url:
            print("[startup] worker 模式但没配 hub_url,无法连接 hub")
        else:
            await _replace_worker_bridge()
            # 定时把云端记忆拉回本地电脑。
            backup_task = asyncio.create_task(_memory_backup_loop())

    if CFG.role.is_standalone:
        backup_task = asyncio.create_task(_memory_backup_loop())

    # 定时把攒着的记忆提炼落盘(万一被强杀,最多丢这一小段)
    flush_task = asyncio.create_task(_memory_flush_loop())
    # 定时任务(到点提醒/执行)
    schedule_task = asyncio.create_task(_scheduler_loop())
    # 持续回收由 cleanup_task 接续执行，不与启动维护重复扫描。

    print(f"[startup] backend ready +{(time.monotonic() - started) * 1000:.0f}ms")
    yield

    # 关小A 前:把还攒着没提炼的对话补上,别丢
    if MEMORY is not None:
        try:
            n = MEMORY.pending_count
            if n:
                print(f"[memory] 退出前提炼攒着的 {n} 轮对话…")
            await MEMORY.flush()
        except Exception as e:
            print(f"[memory] 退出前 flush 失败: {e}")
    await _flush_private_memories()
    flush_task.cancel()
    schedule_task.cancel()
    if not weixin_init_task.done():
        weixin_init_task.cancel()
    if not cleanup_task.done():
        cleanup_task.cancel()
    if _SHARED_ROUTE_REFRESH_TASK is not None and not _SHARED_ROUTE_REFRESH_TASK.done():
        _SHARED_ROUTE_REFRESH_TASK.cancel()
    await asyncio.gather(schedule_task, return_exceptions=True)
    await asyncio.gather(weixin_init_task, cleanup_task, return_exceptions=True)
    if _SHARED_ROUTE_REFRESH_TASK is not None:
        await asyncio.gather(_SHARED_ROUTE_REFRESH_TASK, return_exceptions=True)
    if _PARENT_MONITOR_TASK is not None:
        _PARENT_MONITOR_TASK.cancel()
        await asyncio.gather(_PARENT_MONITOR_TASK, return_exceptions=True)
        _PARENT_MONITOR_TASK = None
    await _replace_worker_bridge(start=False)
    try:
        from backend.modules.chat import weixin_clawbot
        await weixin_clawbot.stop_listener()
    except Exception:
        pass
    if 'backup_task' in dir() and backup_task:
        backup_task.cancel()
    if CODE_PROCESS_MANAGER is not None:
        await asyncio.to_thread(CODE_PROCESS_MANAGER.stop_all)
    if BROWSER_CONTROLLER is not None:
        await BROWSER_CONTROLLER.close()
    await _close_llm_clients()


def _cleanup_stale_update_packages() -> None:
    with _UPDATE_JOB_LOCK:
        # Never race a download or a pending restart, however old it is.
        if _UPDATE_JOB.get("stage") in {"queued", "checking", "downloading", "verifying", "preparing", "staged", "applying", "restarting", "replacing", "cancelling"}:
            return
        isolation_workspace.cleanup_stale_updates()


async def _startup_cache_cleanup() -> None:
    # UI、渠道和健康检查先可用；每项维护在后台串行执行，避免启动时争抢磁盘。
    await asyncio.sleep(30)
    for name, cleaner in (
        ("isolation", lambda: isolation_workspace.cleanup(CFG.agent.isolation_max_mb)),
        ("attachments", _cleanup_attachments),
        ("old-updates", _cleanup_stale_update_packages),
    ):
        try:
            started = time.monotonic()
            await asyncio.to_thread(cleaner)
            print(f"[startup] {name} cleanup {(time.monotonic() - started) * 1000:.0f}ms")
        except Exception as error:
            print(f"[startup] {name} cleanup failed: {error}")
        await asyncio.sleep(5)

    # 已有项目仍会自动增量更新；没有项目不扫描，也不因维护而加载向量模型。
    memory = MEMORY
    if memory is not None:
        try:
            results = await asyncio.to_thread(memory.refresh_projects)
            for result in results:
                print(f"[memory] 项目增量索引: {result}")
        except Exception as error:
            print(f"[startup] 项目增量索引失败: {error}")

    # 单个维护任务接续周期清理，不与尚未完成的启动扫描争用磁盘。
    await _isolation_cleanup_loop()


async def _memory_flush_loop() -> None:
    """每隔一段时间把攒着的记忆提炼一次。
    这是"被强杀也不至于丢一整天"的兜底——正常退出走 lifespan 的 flush。
    """
    interval = max(int(getattr(CFG.memory, "flush_seconds", 180)), 30)
    while True:
        await asyncio.sleep(interval)
        try:
            if MEMORY is not None and MEMORY.pending_count:
                await MEMORY.flush()
            await _flush_private_memories()
        except Exception as e:
            print(f"[memory] 定时 flush 出错: {e}")


async def _isolation_cleanup_loop() -> None:
    """定期限制过程文件/临时缓存占用,不触碰持久记忆和浏览器资料。"""
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(isolation_workspace.cleanup, CFG.agent.isolation_max_mb)
        except Exception as e:
            print(f"[isolation] 定时清理出错: {e}")


# ============ 定时任务(到点提醒/执行) ============

async def _run_scheduled_agent(task: dict) -> tuple[str, str | None]:
    """到点执行任务:跑完整 agent 链,但确认类操作一律自动拒绝(人不在场)。

    用 approval_mode="scheduled":危险判定照 risk 档算(危险操作会被识别出来),
    但 confirm_fn 直接返回 False。返回 (回复文本, 被拦截原因或 None)。
    """
    blocked_tool: list[str] = []

    async def deny_confirm(name, args):
        del args
        if name not in blocked_tool:
            blocked_tool.append(name)
        return False

    schedule_llm = LLM
    _retain_llm(schedule_llm)
    channel_token = context_token = receive_token = None
    channel_source = channel_identity = ""
    channel_permission = None
    try:
        if task.get("channel_target"):
            source, target = task.get("channel", "feishu"), task["channel_target"]
            account_id = task.get("channel_account_id", "")
            reply_context = ""
            if source == "weixin":
                from backend.modules.chat import weixin_clawbot
                account_id, config = weixin_clawbot._get_account(account_id)
                if target not in config.get("allowed_user_ids", []):
                    return ("微信接收用户已撤销授权", "微信接收用户已撤销授权")
                reply_context = str(config.get("user_contexts", {}).get(target) or (
                    config.get("last_context_token", "") if config.get("last_user_id") == target else ""))
            elif source == "feishu":
                user_id = task.get("channel_user_id") or (target if task.get("channel_receive_id_type", "open_id") == "open_id" else "")
                if user_id not in CFG.feishu.allowed_open_ids:
                    return ("飞书接收用户已撤销授权", "飞书接收用户已撤销授权")
            channel_source, channel_identity = source, account_id if source == "weixin" else user_id
            channel_permission = _channel_permission(channel_source, channel_identity)
            if CHANNEL_LOCKED["locked"] or not channel_permission[0]:
                reason = "该账号远程操控已关闭，定时操作已跳过"
                return reason, reason
            channel_token = CURRENT_CHANNEL.set(_channel_session_key(source, target, account_id))
            context_token = _CHANNEL_CONTEXT_TOKEN.set(reply_context)
            receive_token = _CHANNEL_RECEIVE_ID_TYPE.set(task.get("channel_receive_id_type", "open_id"))
        agent = Agent(
            schedule_llm, REGISTRY, CFG.agent, confirm_fn=deny_confirm, memory=_memory_for_current_channel(),
            skill_prompt_fn=_active_skill_prompt,
            runtime_context=(
                _HUB_RUNTIME_CONTEXT if CFG.role.is_hub
                else _LOCAL_CLOUD_RUNTIME_CONTEXT if CFG.role.is_standalone else ""
            ),
            abort_fn=lambda: bool(channel_source) and (
                CHANNEL_LOCKED["locked"] or
                _channel_permission(channel_source, channel_identity) != channel_permission
            ),
        )
        reply = ""
        async for ev in agent.run(task.get("text") or "", approval_mode="scheduled"):
            _normalize_event_punctuation(ev)
            if ev.type in ("message", "message_end") and ev.data.get("content"):
                reply = ev.data["content"]
            elif ev.type == "error":
                reply = f"执行出错: {ev.data.get('msg', '')[:120]}"
        if channel_source and (CHANNEL_LOCKED["locked"] or
                               _channel_permission(channel_source, channel_identity) != channel_permission):
            reason = "该账号远程操控已撤销，定时操作已停止"
            return reply or reason, reason
        if blocked_tool:
            reason = f"任务里有需要确认的操作（{', '.join(blocked_tool)}），已跳过未执行"
            return (reply or reason, reason)
        return (reply or "已执行", None)
    except Exception as e:
        return (f"执行出错: {e}", None)
    finally:
        if channel_token is not None:
            CURRENT_CHANNEL.reset(channel_token)
        if context_token is not None:
            _CHANNEL_CONTEXT_TOKEN.reset(context_token)
        if receive_token is not None:
            _CHANNEL_RECEIVE_ID_TYPE.reset(receive_token)
        await _release_llm(schedule_llm)


async def _send_feishu_reminder(text: str, target: str = "", receive_id_type: str = "open_id", user_id: str = "") -> str:
    if not CFG.feishu.enabled or not CFG.feishu.allowed_open_ids:
        return "未配置跳过"
    if not target:
        if len(CFG.feishu.allowed_open_ids) != 1:
            return "未发送：请为定时任务指定飞书接收用户"
        target = CFG.feishu.allowed_open_ids[0]
        receive_id_type = "open_id"
    recipient = user_id or (target if receive_id_type == "open_id" else "")
    if recipient not in CFG.feishu.allowed_open_ids:
        return "未发送：飞书接收用户未授权或已撤销授权"
    try:
        from backend.modules.chat import feishu_module
        await feishu_module.send_message(target, text, receive_id_type=receive_id_type)
        return "发送成功"
    except Exception as e:
        print(f"[schedule] 飞书提醒发送失败: {e}")
        return f"发送失败({e})"


async def _send_weixin_reminder(text: str, account_id: str = "", user_id: str = "") -> str:
    try:
        from backend.modules.chat import weixin_clawbot
        kwargs = {"account_id": account_id, "user_id": user_id} if account_id or user_id else {}
        await weixin_clawbot.send_scheduled_message(text, **kwargs)
        return "发送成功"
    except Exception as e:
        print(f"[schedule] 微信提醒发送失败: {e}")
        return f"发送失败({e})"


def _cloud_model_order() -> list[dict]:
    # Explicit desktop-to-cloud sync clears settings_saved; do not replay stale browser defaults.
    if not CFG.llm.settings_saved:
        return []
    return [{key: provider[key] for key in ("id", "base_url", "protocol", "models")}
            for provider in _provider_settings()]


async def _apply_cloud_model_order(providers: list[dict]) -> None:
    """Adopt ordering only for the same local endpoint and unchanged model membership."""
    if not CFG.role.is_worker or not isinstance(providers, list):
        return
    previous = copy.deepcopy(CFG.llm)
    local = {"default": CFG.llm, **{p.id: p for p in CFG.llm.providers}}
    changed = False
    seen = set()
    for item in providers:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return
        identity = item["id"]
        models = item.get("models")
        if (identity in seen or not isinstance(models, list) or not models
                or any(not isinstance(model, str) or not model.strip() for model in models)
                or len(models) != len(set(models))):
            return
        seen.add(identity)
    for item in providers:
        provider = local.get(item["id"])
        models = item["models"]
        if (provider is None or item.get("base_url") != provider.base_url
                or item.get("protocol") != provider.protocol
                or set(models) != set(provider.models)):
            continue
        if models != provider.models:
            provider.models = list(models)
            if item["id"] == "default":
                CFG.llm.chat_model = CFG.llm.vision_model = models[0]
            changed = True
    if not changed:
        return
    CFG.llm.settings_saved = True
    try:
        _rewrite_config()
    except Exception:
        # Existing clients hold these config objects; restore them in place on disk failure.
        for provider in CFG.llm.providers:
            original = next((p for p in previous.providers if p.id == provider.id), None)
            if original is not None:
                provider.models = original.models
        for key in ("models", "chat_model", "vision_model", "settings_saved"):
            setattr(CFG.llm, key, getattr(previous, key))
        raise
    await broadcast({"type": "settings_changed", "data": {"scope": "models"}})


async def _on_worker_notice(payload: dict) -> None:
    """worker 收到 hub 推来的提醒,在本机广播给悬浮球和聊天窗口。"""
    if not isinstance(payload, dict):
        return
    await broadcast({"type": "scheduled_reminder", "data": payload})


def _with_late_notice(text: str, task: dict) -> str:
    """迟到(超过 60 秒)才触发时,在通知文本里补一句延迟说明。"""
    due_time = task.get("_due_time")
    if not due_time:
        return text
    delay = time.time() - float(due_time)
    if delay <= 60:
        return text
    when = time.strftime("%H:%M", time.localtime(float(due_time)))
    return f"{text} (原定 {when}, 延迟 {_delay_text(delay)} 送达)"


def _delay_text(seconds: float) -> str:
    if seconds < 120:
        return f"{int(seconds)} 秒"
    return f"{round(seconds / 60)} 分钟"


async def _deliver_scheduled_notice(task: dict, text: str) -> dict:
    """把定时任务的结果/提醒推给前端(悬浮球 + 聊天窗口)、飞书,以及(云端时)worker。

    三路各自兜底,并返回每路的送达结果,供写进任务的 last_message 留痕。
    """
    results: dict[str, str] = {}
    try:
        await broadcast({
            "type": "scheduled_reminder",
            "data": {
                "id": task.get("id"), "name": task.get("name"),
                "kind": task.get("kind"), "text": text,
            },
        })
        results["broadcast"] = "成功"
    except Exception as e:
        print(f"[schedule] 前端广播失败: {e}")
        results["broadcast"] = f"失败({e})"
    # Both local and cloud tasks notify the selected connected channel; the
    # local broadcast above remains available whenever this app is running.
    channel = task.get("channel") if task.get("channel") in {"feishu", "weixin"} else "feishu"
    notice = f"[\u5b9a\u65f6\u4efb\u52a1] {task.get('name') or ''}\n{text}"
    sender = _send_weixin_reminder if channel == "weixin" else _send_feishu_reminder
    try:
        kwargs = {}
        if task.get("channel_target"):
            if channel == "weixin":
                kwargs = {"account_id": task.get("channel_account_id", ""), "user_id": task["channel_target"]}
            else:
                kwargs = {"target": task["channel_target"], "receive_id_type": task.get("channel_receive_id_type", "open_id"), "user_id": task.get("channel_user_id", "")}
        results[channel] = await sender(notice, **kwargs)
    except Exception as e:
        print(f"[schedule] {channel} \u63d0\u9192\u53d1\u9001\u5931\u8d25: {e}")
        results[channel] = f"\u53d1\u9001\u5931\u8d25({e})"
    try:
        if HUB_BRIDGE is not None:
            pushed = await HUB_BRIDGE.notify({
                "id": task.get("id"), "name": task.get("name"),
                "kind": task.get("kind"), "text": text,
            })
            results["worker"] = "成功" if pushed else "worker 离线"
        else:
            results["worker"] = "本机"
    except Exception as e:
        print(f"[schedule] 推送 worker 失败: {e}")
        results["worker"] = f"失败({e})"
    return results


async def _fire_scheduled_task(task: dict) -> None:
    """触发一个到点任务;串行执行,避免两个定时任务同时改文件。"""
    async with _SCHEDULE_FIRE_LOCK:
        blocked_reason = None
        if task.get("kind") == "execute":
            text, blocked_reason = await _run_scheduled_agent(task)
        else:
            text = task.get("text") or task.get("name") or ""
        text = _with_late_notice(text, task)
        delivery = await _deliver_scheduled_notice(task, text)
        parts = []
        if blocked_reason:
            parts.append(blocked_reason)
        delivery_text = "；".join(f"{key}:{value}" for key, value in delivery.items())
        if delivery_text:
            parts.append(delivery_text)
        SCHEDULER.record_fire_result(
            task["id"], "blocked" if blocked_reason else "ok", "；".join(parts),
        )


async def _scheduler_loop() -> None:
    """定时任务驱动循环:照抄 _memory_flush_loop 的写法。"""
    await asyncio.sleep(5)
    while True:
        try:
            for task in SCHEDULER.tick():
                await _fire_scheduled_task(task)
        except Exception as e:
            print(f"[schedule] 调度循环出错: {e}")
        await asyncio.sleep(15)


def _automatic_schedule_location() -> str:
    """Choose cloud only when a usable worker connection is configured."""
    role = CFG.role
    connected = role.is_worker and bool(role.hub_url and role.hub_token)
    return "cloud" if role.is_hub or connected else "local"


def register_schedule_tools(reg: ToolRegistry | None = None) -> None:
    """让模型能创建/查询/删除定时任务,而不是凭空声称"已经挂上"。"""
    reg = reg or REGISTRY

    async def schedule_add(
        name: str, text: str, schedule: str = "daily", time: str = "09:00",
        days: list[int] | None = None, kind: str = "remind",
        channel: str = "feishu", datetime: str = "", timezone: str = "",
    ) -> str:
        task = SCHEDULER.add_task(**_bind_schedule_destination(dict(
            name=name, text=text, schedule=schedule, time=time,
            days=days or [], kind=kind, location=_automatic_schedule_location(),
            channel=channel, datetime=datetime, timezone=timezone,
        )))
        when = task["datetime"] if task["schedule"] == "once" else task["time"]
        return f"已创建定时任务「{task['name']}」({task['kind']}/{task['schedule']} {when},{task['location']},{task['channel']})"

    async def schedule_list() -> str:
        tasks = [task for task in SCHEDULER.list_tasks() if _owns_channel_schedule(task)]
        if not tasks:
            return "当前没有定时任务。"
        lines = []
        for item in tasks:
            state = "已停用" if not item["enabled"] else ("已完成" if item.get("done") else "启用")
            when = item["datetime"] if item["schedule"] == "once" else item["time"]
            lines.append(
                f"- {item['id']}: {item['name']}({item['kind']}/{item['schedule']} {when},"
                f"{item['location']},{item.get('channel', 'feishu')},{state})"
            )
        return "当前定时任务:\n" + "\n".join(lines)

    async def schedule_remove(task_id: str) -> str:
        task = next((item for item in SCHEDULER.list_tasks() if item["id"] == task_id), None)
        if task is not None and not _owns_channel_schedule(task):
            return "此定时任务属于其他会话，不能删除。"
        return f"已删除定时任务 {task_id}" if SCHEDULER.remove_task(task_id) else f"未找到定时任务 {task_id}"

    reg.register(Tool(
        name="schedule_add",
        description="创建一个定时任务(到点提醒或到点执行)。schedule=daily(每天)/weekly(每周)/once(一次性);"
                    "kind=remind(提醒)/execute(执行指令);time=HH:MM(daily/weekly);days=星期数组(weekly,0=周一);"
                    "datetime=一次性任务的 ISO 时间(once);\u6267\u884c\u4f4d\u7f6e\u6839\u636e\u662f\u5426\u5df2\u914d\u7f6e\u4e91\u7aef\u8fde\u63a5\u81ea\u52a8\u786e\u5b9a\u3002",
        parameters={"type": "object", "properties": {
            "name": {"type": "string", "description": "任务名称"},
            "text": {"type": "string", "description": "提醒内容(kind=remind)或要执行的指令(kind=execute)"},
            "kind": {"type": "string", "enum": ["remind", "execute"]},
            "schedule": {"type": "string", "enum": ["daily", "weekly", "once"]},
            "time": {"type": "string", "description": "触发时间 HH:MM(daily/weekly)"},
            "days": {"type": "array", "items": {"type": "integer"}, "description": "星期(weekly),0=周一..6=周日"},
            "datetime": {"type": "string", "description": "一次性任务的执行时间(once),如 2026-08-25T14:50:00"},
            "channel": {"type": "string", "enum": ["feishu", "weixin"], "description": "\u4e91\u7aef\u901a\u77e5\u6e20\u9053"},
        }, "required": ["name", "text"]},
        handler=schedule_add, side_effect=True,
    ))
    reg.register(Tool(
        name="schedule_list",
        description="列出所有定时任务(用户询问有哪些定时任务时调用)。",
        parameters={"type": "object", "properties": {}},
        handler=schedule_list, side_effect=False,
    ))
    reg.register(Tool(
        name="schedule_remove",
        description="删除一个定时任务。task_id 用 schedule_list 查到的 id。",
        parameters={"type": "object", "properties": {
            "task_id": {"type": "string", "description": "任务 id"},
        }, "required": ["task_id"]},
        handler=schedule_remove, side_effect=True,
    ))


async def _close_llm_clients() -> None:
    """Close every live or retired client during process shutdown."""
    clients = {LLM, *_LLM_RETIRE}
    _LLM_RETIRE.clear()
    _LLM_USAGE.clear()
    results = await asyncio.gather(
        *(client.aclose() for client in clients), return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            print(f"[shutdown] 关闭模型客户端失败: {type(result).__name__}: {result}")


def _start_chat_listeners(loop=None) -> None:
    # 从后台线程调用时,主事件循环由参数传入(线程里没有 running loop)
    if loop is None:
        loop = asyncio.get_running_loop()

    def on_incoming(source: str):
        # 私聊(is_command)=命令小A并把结果发回;群/他人=拟稿让用户确认
        # 返回协程(不自己 run_coroutine_threadsafe):调用方 _call 已经在事件循环里
        # 并会 await 它,这样任务被上层 future 强引用住,不会被 GC 中途回收。
        def cb(sender: str, text: str, target: str, is_command: bool = False, meta: dict | None = None):
            if is_command:
                return handle_channel_command(source, target, text, user_id=sender)
            return draft_and_notify(source, sender, text, target, meta=meta)
        return cb

    # worker 的飞书消息先到 hub；本机不重复监听飞书。
    if not CFG.role.is_worker:
        # Worker Feishu messages arrive through the hub; local WeChat remains enabled.
        try:
            if CFG.feishu.enabled:
                from backend.modules.chat import feishu_module

                # 注入语音转文字能力:飞书收到语音消息时按语音引擎转成文字再执行
                if CFG.voice.asr_enabled:
                    async def _asr(audio: bytes, fmt: str) -> str:
                        return await transcribe(CFG.voice, audio, fmt=fmt)
                    feishu_module.set_asr_hook(_asr)

                async def _vision(prompt: str, image: bytes, image_format: str) -> str:
                    vision_llm = LLM
                    _retain_llm(vision_llm)
                    try:
                        return await asyncio.wait_for(
                            vision_llm.vision(prompt, image, image_format=image_format),
                            timeout=CFG.llm.timeout,
                        )
                    finally:
                        await _release_llm(vision_llm)

                feishu_module.set_vision_hook(_vision)

                print("[chat] " + feishu_module.start_listener(CFG, on_incoming("feishu"), loop))
        except Exception as e:
            import traceback
            print(f"[chat] 飞书监听启动失败: {e}")
            traceback.print_exc()

    # 微信 ClawBot uses the same speech/image adapters as Feishu. The hub
    # receives the message; the worker still executes the actual desktop tool.
    try:
        from backend.modules.chat import weixin_clawbot

        if CFG.voice.asr_enabled:
            async def _weixin_asr(audio: bytes, fmt: str) -> str:
                return await transcribe(CFG.voice, audio, fmt=fmt)
            weixin_clawbot.set_asr_hook(_weixin_asr)

        async def _weixin_vision(prompt: str, image: bytes, image_format: str) -> str:
            vision_llm = LLM
            _retain_llm(vision_llm)
            try:
                return await asyncio.wait_for(
                    vision_llm.vision(prompt, image, image_format=image_format),
                    timeout=CFG.llm.timeout,
                )
            finally:
                await _release_llm(vision_llm)

        weixin_clawbot.set_vision_hook(_weixin_vision)
    except Exception as e:
        print(f"[chat] 微信媒体能力初始化失败: {e}")


app = FastAPI(title="Desktop Agent Backend", lifespan=lifespan)
# 注意:默认不再包含 "null"。null 来源(sandboxed iframe / file://)必须走 UI 令牌那条路,
# 不能靠源白名单放行,否则任意恶意网页都能冒充悬浮球连上本机。
_ALLOWED_ORIGINS = [
    x.strip() for x in os.getenv(
        "AGENT_ALLOWED_ORIGINS", "http://127.0.0.1:1985,http://localhost:1985"
    ).split(",") if x.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============ 网页登录密码 ============
# 本机免密码；远程网页必须设置密码并登录。
_SESSION_TTL_SECONDS = 30 * 24 * 3600
_MAX_SESSIONS = 1000
_SESSIONS: dict[str, float] = {}      # token -> 过期时间
_PUBLIC_PATHS = {"/login", "/api/login", "/health", "/ui-language.js"}

# ============ 本机 UI 一次性令牌 ============
# 问题:Electron 悬浮球用 file:// 加载页面,它连 WebSocket 时浏览器发的 Origin 是
# "null"——和恶意网页塞进 sandboxed iframe 时发的 Origin 一模一样。光看来源分不出
# 谁是自己的悬浮球、谁是坏页面。所以改成"凭证据放行":后端启动时生成一串随机令牌,
# 写到本机数据目录的一个文件里;只有本机进程(Electron 主进程)读得到,再把它带在
# WS 地址上。恶意网页跑在别的进程/别的域,读不到这个文件,也就拿不到令牌 → 连不上。
# 浏览器同源访问(http://127.0.0.1:1985/)走 Host 匹配那条路,不需要令牌,不受影响。
_UI_TOKEN: str = ""


def _ui_token_path():
    return app_home() / "ui_token.txt"


def _ensure_ui_token() -> str:
    """取 UI 令牌:磁盘上已有就复用,没有才新生成并落盘。

    **必须复用磁盘上的令牌**,不能每次进程启动都换新的。原因:开发用 `uvicorn --reload`
    时,改一次代码就重启一个 worker;若每次重启都生成新令牌塞进内存,而前端(Electron)
    仍拿着文件里的旧令牌来连,内存与文件对不上 → WebSocket 一直 403,悬浮球连不上。
    复用磁盘令牌后,重启多少次、写盘偶尔失败都不会让前后端错位。
    """
    global _UI_TOKEN
    if _UI_TOKEN:
        return _UI_TOKEN
    path = _ui_token_path()
    # 先尝试复用磁盘上已有的令牌(跨重启/reload 稳定)
    try:
        if path.is_file():
            existing = path.read_text(encoding="utf-8").strip()
            if len(existing) >= 32:
                _UI_TOKEN = existing
                return _UI_TOKEN
    except OSError:
        pass
    # 没有可用的旧令牌,生成一个并落盘
    import secrets
    _UI_TOKEN = secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_UI_TOKEN, encoding="utf-8")
        try:
            os.chmod(path, 0o600)  # 尽力收紧权限;失败不致命
        except OSError:
            pass
    except OSError as e:
        print(f"[startup] 写 UI 令牌失败(悬浮球可能连不上,浏览器版不受影响): {e}")
    return _UI_TOKEN


def _new_session() -> str:
    import secrets
    _prune_sessions()
    tok = secrets.token_urlsafe(24)
    _SESSIONS[tok] = time.time() + _SESSION_TTL_SECONDS
    return tok


def _prune_sessions(now: float | None = None) -> None:
    current = time.time() if now is None else now
    for token, expires_at in list(_SESSIONS.items()):
        if expires_at <= current:
            _SESSIONS.pop(token, None)
    if len(_SESSIONS) > _MAX_SESSIONS:
        for token, _ in sorted(_SESSIONS.items(), key=lambda item: item[1])[:-_MAX_SESSIONS]:
            _SESSIONS.pop(token, None)


def _session_valid(token: str) -> bool:
    expires_at = _SESSIONS.get(token, 0)
    if expires_at <= time.time():
        _SESSIONS.pop(token, None)
        return False
    return True


def _is_local(client_host: str | None) -> bool:
    """连接是否来自本机。本机访问(悬浮球/本地浏览器)一律免密码——
    人都坐在这台电脑前了,网页密码没有意义;密码只用来挡公网上的陌生人。
    """
    return client_host in ("127.0.0.1", "::1", "localhost")


def _can_manage_runtime(request: Request) -> bool:
    """本机管理，或已登录且同源的 hub 网页管理；不授予 worker/聊天用户管理权。"""
    if _is_local(request.client.host if request.client else None):
        return True
    if not (CFG.role.is_hub and CFG.auth.enabled
            and _session_valid(getattr(request, "cookies", {}).get("xa_session", ""))):
        return False
    origin = request.headers.get("origin", "")
    return not origin or origin == str(request.base_url).rstrip("/")


_REMOTE_WEB_DISABLED = "未设置网页访问密码，远程网页访问已关闭。请先在本机设置密码。"


@app.middleware("http")
async def auth_guard(request, call_next):
    """本机免密码；远程未设密码时关闭网页，设密码后校验会话。"""
    if _is_local(request.client.host if request.client else None):
        return await call_next(request)
    path = request.url.path
    # worker 的机器通信单独校验桥口令，不依赖网页登录。
    if (CFG.role.hub_token and path in ("/api/memory/export", "/api/memory/import", "/api/weixin/memory/shared", "/api/weixin/memory/context")
            and _matches_secret(request.headers.get("x-hub-token"), CFG.role.hub_token)):
        return await call_next(request)
    from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
    if not CFG.auth.enabled and path != "/health":
        if path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": _REMOTE_WEB_DISABLED}, status_code=403)
        return PlainTextResponse(_REMOTE_WEB_DISABLED, status_code=403)
    if path in _PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if _session_valid(request.cookies.get("xa_session", "")):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    return RedirectResponse("/login")


@app.get("/login")
async def login_page():
    from fastapi.responses import FileResponse
    html = _frontend_file("login.html")
    return FileResponse(str(html))


class LoginRequest(BaseModel):
    password: str = ""


def _matches_secret(candidate: str | None, expected: str) -> bool:
    return hmac.compare_digest((candidate or "").encode("utf-8"), expected.encode("utf-8"))


@app.post("/api/login")
async def api_login(req: LoginRequest, request: Request):
    from fastapi.responses import JSONResponse
    if not CFG.auth.enabled:
        if not _is_local(request.client.host if request.client else None):
            return JSONResponse({"ok": False, "error": _REMOTE_WEB_DISABLED}, status_code=403)
        return {"ok": True, "note": "本机无需登录"}
    # 用 compare_digest 防时序侧信道
    if not _matches_secret(req.password, CFG.auth.password):
        return JSONResponse({"ok": False, "error": "密码不对"}, status_code=401)
    tok = _new_session()
    resp = JSONResponse({"ok": True})
    # httponly:JS 读不到,降低被盗风险;samesite=lax 防跨站
    resp.set_cookie(
        "xa_session", tok, httponly=True, samesite="strict",
        secure=request.url.scheme == "https", max_age=30 * 24 * 3600,
    )
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    from fastapi.responses import JSONResponse
    _SESSIONS.pop(request.cookies.get("xa_session", ""), None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("xa_session")
    return resp


@app.get("/api/channel/status")
async def api_channel_status() -> dict:
    """远程遥控是否处于紧急锁定状态。"""
    local_locked = bool(CHANNEL_LOCKED["locked"])
    if CFG.role.is_worker:
        try:
            from backend.core import cloud_deploy as cloud_deployer
            cloud_locked = await asyncio.wait_for(
                asyncio.to_thread(cloud_deployer.channel_lock_status, app_home()),
                timeout=8,
            )
            return {
                "locked": local_locked or cloud_locked,
                "local_locked": local_locked,
                "cloud_locked": bool(cloud_locked),
                "scope": "cloud",
            }
        except Exception as exc:
            return {
                "locked": True if local_locked else None,
                "local_locked": local_locked,
                "cloud_locked": None,
                "scope": "cloud",
                "error": f"{type(exc).__name__}: {exc}",
            }
    return {"locked": local_locked, "local_locked": local_locked, "scope": "local"}


@app.post("/api/channel/unlock")
async def api_channel_unlock(request: Request):
    """解除远程遥控锁定。只能在本机操作——盗号者发消息无法解锁。"""
    from fastapi.responses import JSONResponse
    if not _is_local(request.client.host if request.client else None):
        return JSONResponse(
            {"ok": False, "error": "出于安全,解锁只能在本机操作。"}, status_code=403)
    errors = []
    cloud_unlocked = not CFG.role.is_worker
    local_unlocked = False
    if CFG.role.is_worker:
        try:
            from backend.core import cloud_deploy as cloud_deployer
            await asyncio.to_thread(cloud_deployer.unlock_channel, app_home())
            cloud_unlocked = True
        except Exception as exc:
            errors.append(f"云端：{type(exc).__name__}")
    try:
        await asyncio.to_thread(_save_channel_lock, False)
        local_unlocked = True
    except Exception as exc:
        errors.append(f"本机：{type(exc).__name__}")
    if errors:
        CHANNEL_LOCKED["locked"] = not local_unlocked
        return JSONResponse(
            {
                "ok": False,
                "locked": True,
                "local_unlocked": local_unlocked,
                "cloud_unlocked": cloud_unlocked,
                "error": "；".join(errors) + "，仍保持锁定（仍有锁定状态未解除）",
            },
            status_code=500,
        )
    CHANNEL_LOCKED["locked"] = False
    return {
        "ok": True,
        "locked": False,
        "local_unlocked": local_unlocked,
        "cloud_unlocked": cloud_unlocked,
        "scope": "cloud" if CFG.role.is_worker else "local",
    }


class ChannelControlRequest(BaseModel):
    source: Literal["weixin", "feishu"]
    enabled: StrictBool
    identity: str = Field(default="", max_length=256)


def _stop_channel_control(source: str, identity: str = "") -> None:
    def belongs(key: tuple[str, ...]) -> bool:
        if key[0] != source:
            return False
        if not identity:
            return True
        return (source == "weixin" and len(key) == 3 and key[1] == identity) or (
            source == "feishu" and key[-1] == identity
        )

    for key in tuple(CHANNEL_RESUME):
        if belongs(key):
            CHANNEL_RESUME.pop(key, None)
    for key, active in tuple(CHANNEL_ACTIVE_TASKS.items()):
        matches_identity = not identity or active.get("channel_identity") == identity
        if (belongs(key) or matches_identity) and active.get("remote_control", True):
            active["stop"] = active["control_revoked"] = True
    for key, pending in tuple(CHANNEL_CONFIRM.items()):
        active = CHANNEL_ACTIVE_TASKS.get(key, {})
        if (belongs(key) or (identity and active.get("channel_identity") == identity)) and not pending.done():
            pending.set_result(False)


def _ensure_channel_control_identities() -> dict[str, list[str]]:
    from backend.modules.chat import weixin_clawbot

    weixin_ids = [str(item.get("account_id", "")) for item in weixin_clawbot.status().get("accounts", [])
                  if str(item.get("account_id", ""))]
    feishu_ids = [str(item).strip() for item in CFG.feishu.allowed_open_ids if str(item).strip()]
    if weixin_ids:
        channel_control.ensure_identities("weixin", weixin_ids, weixin_ids[0])
    if feishu_ids:
        channel_control.ensure_identities("feishu", feishu_ids, feishu_ids[0])
    return {"weixin": weixin_ids, "feishu": feishu_ids}


@app.get("/api/channel/control")
async def api_channel_control_status(request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="远程操控开关需要本机或已登录的云端管理员读取")
    try:
        await asyncio.to_thread(_ensure_channel_control_identities)
        local = await asyncio.to_thread(channel_control.snapshot)
        cloud = None
        if CFG.role.is_worker:
            from backend.core import cloud_deploy
            cloud = await asyncio.to_thread(cloud_deploy.channel_control_settings, app_home())
        return {"ok": True, "local": local, "cloud": cloud, "scope": "cloud" if cloud is not None else "local"}
    except Exception as error:
        return {"ok": False, "error": str(error)}


@app.post("/api/channel/control")
async def api_channel_control_set(req: ChannelControlRequest, request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="远程操控开关需要本机或已登录的云端管理员修改")
    local = cloud = None
    identity = req.identity.strip()
    try:
        # 关闭先收回本机权限；云端不可达时也不继续放行本机工具。
        if not req.enabled:
            local = await asyncio.to_thread(
                channel_control.update_account if identity else channel_control.update,
                req.source, identity, False, app_home(),
            ) if identity else await asyncio.to_thread(
                channel_control.update, req.source, False,
            )
            _stop_channel_control(req.source, identity)
        if CFG.role.is_worker:
            from backend.core import cloud_deploy
            cloud = await asyncio.to_thread(
                cloud_deploy.channel_control_settings, app_home(), req.source, req.enabled, identity,
            )
        if req.enabled:
            local = await asyncio.to_thread(
                channel_control.update_account if identity else channel_control.update,
                req.source, identity, True, app_home(),
            ) if identity else await asyncio.to_thread(
                channel_control.update, req.source, True,
            )
        label = "微信" if req.source == "weixin" else "飞书"
        suffix = "该账号" if identity else label
        return {"ok": True, "local": local, "cloud": cloud, "scope": "cloud" if cloud is not None else "local",
                "message": f"{suffix}远程操控已{'开启' if req.enabled else '关闭，仅聊天'}，立即生效"}
    except Exception as error:
        message = str(error)
        if local is not None and not req.enabled:
            message = "本机已关闭；云端未确认关闭，仍可能允许远程操控，请重试。" + message
        elif cloud is not None:
            message = "云端已更新，但本机保存失败；两端状态可能不同，请重试。" + message
        return {"ok": False, "local": local, "cloud": cloud, "error": message}


@app.get("/health")
async def health(request: Request) -> dict:
    # /health 允许匿名探活；完整详情只提供给本机或持有有效登录会话的网页。
    # 远程网页同样依赖模型清单和语音状态来初始化界面。
    local_client = _is_local(request.client.host if request.client else None)
    if not local_client and (not CFG.auth.enabled
            or not _session_valid(request.cookies.get("xa_session", ""))):
        return {"ok": True, "role": CFG.role.mode}
    worker = HUB_BRIDGE.status() if HUB_BRIDGE is not None else None
    local_worker = WORKER_BRIDGE.status() if WORKER_BRIDGE is not None else None
    return {
        "ok": True,
        "version": __version__,
        "frontend_pid": os.getenv("XIAOA_PARENT_PID", ""),
        "role": CFG.role.mode,
        "worker": worker,
        "local_worker": local_worker,
        "tool_count": len(REGISTRY.names()),
        "memory": {
            "ready": MEMORY is not None,
            "vector_ready": bool(MEMORY and MEMORY.vector_ready),
            "backup": {
                "dir": str(CFG.memory_backup_dir),
                **BACKUP_STATUS,
            },
        },
        "chat_model": CFG.llm.chat_model,
        "models": CFG.llm.models,
        "model_options": _model_options(),
        "voice": CFG.voice.tts_enabled,  # 前端据此决定语音走云端还是浏览器
        "asr_enabled": CFG.voice.asr_enabled,
        "open_at_login": CFG.agent.open_at_login,
    }


# ============ 设置(网关/密钥/腾讯云,自助修改) ============

def _mask(s: str) -> str:
    """密钥脱敏:只留首尾几位。"""
    if not s:
        return ""
    if len(s) <= 10:
        return s[:2] + "***"
    return s[:6] + "***" + s[-3:]


def _model_options() -> list[dict]:
    options = [
        {"provider_id": "default", "provider_name": CFG.llm.provider_name, "model": model}
        for model in CFG.llm.models
    ]
    for provider in CFG.llm.providers:
        options.extend({
            "provider_id": provider.id,
            "provider_name": provider.name,
            "model": model,
        } for model in provider.models)
    return options


_MIN_CONTEXT_WINDOW = 4096
_MAX_CONTEXT_WINDOW = 2_000_000


def _context_windows_for_models(models: list[str], configured: dict[str, int] | None = None) -> dict[str, int]:
    fallback = int(getattr(CFG.agent, "context_window", 128000) or 128000)
    configured = configured or {}
    return {model: int(configured.get(model, fallback)) for model in models}


def _context_window_for_model(provider_id: str, model: str) -> int:
    models = CFG.llm.models
    configured = CFG.llm.context_windows
    if provider_id and provider_id != "default":
        provider = next((item for item in CFG.llm.providers if item.id == provider_id), None)
        if provider is not None:
            models = provider.models
            configured = provider.context_windows
    selected = model if model in models else (models[0] if models else "")
    return _context_windows_for_models([selected], configured).get(
        selected, int(getattr(CFG.agent, "context_window", 128000) or 128000),
    )


def _validated_context_windows(raw: dict[str, int] | None, models: list[str], label: str) -> dict[str, int]:
    if raw is None:
        return {}
    result: dict[str, int] = {}
    for model in models:
        if model not in raw:
            continue
        try:
            window = int(raw[model])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}模型 {model} 的上下文窗口必须是整数") from exc
        if not _MIN_CONTEXT_WINDOW <= window <= _MAX_CONTEXT_WINDOW:
            raise ValueError(f"上下文窗口必须在 {_MIN_CONTEXT_WINDOW} 到 {_MAX_CONTEXT_WINDOW} 之间")
        result[model] = window
    return result


def _provider_settings() -> list[dict]:
    providers = [{
        "id": "default",
        "name": CFG.llm.provider_name,
        "base_url": CFG.llm.base_url,
        "protocol": CFG.llm.protocol,
        "api_key_masked": _mask(CFG.llm.api_key),
        "models": list(CFG.llm.models),
        "context_windows": _context_windows_for_models(CFG.llm.models, CFG.llm.context_windows),
        "is_default": True,
    }]
    providers.extend({
        "id": provider.id,
        "name": provider.name,
        "base_url": provider.base_url,
        "protocol": provider.protocol,
        "api_key_masked": _mask(provider.api_key),
        "models": list(provider.models),
        "context_windows": _context_windows_for_models(provider.models, provider.context_windows),
        "is_default": False,
    } for provider in CFG.llm.providers)
    return providers


def _llm_for_provider(provider_id: str) -> tuple[LLMClient, bool, list[str], str]:
    if CFG.role.is_hub or not provider_id or provider_id == "default":
        return LLM, False, CFG.llm.models, "default"
    provider = next((item for item in CFG.llm.providers if item.id == provider_id), None)
    if provider is None:
        return LLM, False, CFG.llm.models, "default"
    provider_cfg = LLMConfig(
        base_url=provider.base_url,
        api_key=provider.api_key,
        protocol=provider.protocol,
        chat_model=provider.models[0],
        vision_model=provider.models[0],
        models=list(provider.models),
        timeout=CFG.llm.timeout,
    )
    return LLMClient(provider_cfg), True, provider.models, provider.id


def _retain_llm(client: LLMClient) -> None:
    _LLM_USAGE[client] = _LLM_USAGE.get(client, 0) + 1


async def _retire_llm(client: LLMClient) -> None:
    _LLM_RETIRE.add(client)
    if _LLM_USAGE.get(client, 0) == 0:
        _LLM_RETIRE.discard(client)
        await client.aclose()


async def _release_llm(client: LLMClient) -> None:
    remaining = _LLM_USAGE.get(client, 0) - 1
    if remaining > 0:
        _LLM_USAGE[client] = remaining
        return
    _LLM_USAGE.pop(client, None)
    if client in _LLM_RETIRE:
        _LLM_RETIRE.discard(client)
        await client.aclose()


def _rewrite_config() -> None:
    """按当前 CFG 全量重写 config.toml(保留结构与注释性分段)。"""
    cfg_path = config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    v = CFG.voice
    q = lambda value: json.dumps(str(value), ensure_ascii=False)
    if os.name == "nt" and not os.getenv("AGENT_FEISHU_APP_SECRET") and CFG.feishu.app_secret:
        from backend.core import vault
        vault.put("feishu_app_secret", CFG.feishu.app_secret)
    feishu_secret_in_vault = os.name == "nt" and not os.getenv("AGENT_FEISHU_APP_SECRET")
    secret = lambda value, env_name: q("" if os.getenv(env_name) else value)
    provider_keys = provider_api_key_env_overrides()
    CFG.auth.password_env_managed = (
        bool(os.getenv("AGENT_WEB_PASSWORD")) or CFG.auth.password_env_managed
    )
    lines = [
        "[llm]",
        f"provider_name = {q(CFG.llm.provider_name)}",
        f"settings_saved = {str(CFG.llm.settings_saved).lower()}",
        f"base_url = {q(CFG.llm.base_url)}",
        f"protocol = {q(CFG.llm.protocol)}",
        f"api_key = {secret(CFG.llm.api_key, 'AGENT_LLM_API_KEY')}",
        f"chat_model = {q(CFG.llm.chat_model)}",
        "models = [" + ", ".join(q(x) for x in CFG.llm.models) + "]",
        "context_windows = {" + ", ".join(
            f"{q(model)} = {int(window)}" for model, window in CFG.llm.context_windows.items()
        ) + "}",
        f"timeout = {CFG.llm.timeout}",
        "",
        "[memory]",
        f"data_dir = {q(CFG.memory.data_dir)}",
        f"backup_dir = {q(CFG.memory.backup_dir)}",
        f"backup_interval_minutes = {CFG.memory.backup_interval_minutes}",
        f"backup_keep_count = {CFG.memory.backup_keep_count}",
        f"vector_backend = {q(CFG.memory.vector_backend)}",
        f"embed_model = {q(CFG.memory.embed_model)}",
        "project_roots = [" + ", ".join(q(x) for x in CFG.memory.project_roots) + "]",
        f"max_project_files = {CFG.memory.max_project_files}",
        f"batch_turns = {CFG.memory.batch_turns}",
        f"flush_seconds = {CFG.memory.flush_seconds}",
        "",
        "[memory.cloud_backup]",
        f"enabled = {'true' if CFG.memory.cloud_backup.enabled else 'false'}",
        f"endpoint = {q(CFG.memory.cloud_backup.endpoint)}",
        f"bucket = {q(CFG.memory.cloud_backup.bucket)}",
        f"access_key = {secret(CFG.memory.cloud_backup.access_key, 'AGENT_CLOUD_BACKUP_ACCESS_KEY')}",
        f"secret_key = {secret(CFG.memory.cloud_backup.secret_key, 'AGENT_CLOUD_BACKUP_SECRET_KEY')}",
        "",
        "[server]",
        f"host = {q(CFG.server.host)}",
        f"port = {CFG.server.port}",
        "",
        "[agent]",
        f"max_steps = {CFG.agent.max_steps}",
        f"confirm_before_action = {'true' if CFG.agent.confirm_before_action else 'false'}",
        f"max_history_messages = {CFG.agent.max_history_messages}",
        f"context_window = {CFG.agent.context_window}",
        f"open_at_login = {'true' if CFG.agent.open_at_login else 'false'}",
        f"isolation_max_mb = {CFG.agent.isolation_max_mb}",
        "",
        "[voice]",
        f"provider = {q(v.provider)}",
        f"secret_id = {secret(v.secret_id, 'AGENT_TENCENT_SECRET_ID')}",
        f"secret_key = {secret(v.secret_key, 'AGENT_TENCENT_SECRET_KEY')}",
        f"region = {q(v.region)}",
        f"tts_voice = {v.tts_voice}",
        f"tts_speed = {v.tts_speed}",
        "",
        "[feishu]",
        f"app_id = {secret(CFG.feishu.app_id, 'AGENT_FEISHU_APP_ID')}",
        f"app_secret = {q('' if feishu_secret_in_vault or os.getenv('AGENT_FEISHU_APP_SECRET') else CFG.feishu.app_secret)}",
        "allowed_open_ids = [" + ", ".join(q(x) for x in CFG.feishu.allowed_open_ids) + "]",
        "remarks = {" + ", ".join(
            f"{q(key)} = {q(value)}" for key, value in getattr(CFG.feishu, "remarks", {}).items()
            if key in CFG.feishu.allowed_open_ids and str(value).strip()
        ) + "}",
        "",
        "[auth]",
        f"password = {q('' if CFG.auth.password_env_managed else CFG.auth.password)}",
        f"password_env_managed = {'true' if CFG.auth.password_env_managed else 'false'}",
        "",
        "[role]",
        f"mode = {q(CFG.role.mode)}",
        f"hub_url = {q(CFG.role.hub_url)}",
        f"hub_token = {secret(CFG.role.hub_token, 'AGENT_HUB_TOKEN')}",
        f"worker_id = {q(CFG.role.worker_id)}",
        f"tls_fingerprint = {q(CFG.role.tls_fingerprint)}",
    ]
    for provider in CFG.llm.providers:
        lines.extend([
            "",
            "[[llm.providers]]",
            f"id = {q(provider.id)}",
            f"name = {q(provider.name)}",
            f"base_url = {q(provider.base_url)}",
            f"protocol = {q(provider.protocol)}",
            f"api_key = {q('' if provider.id in provider_keys else provider.api_key)}",
            "models = [" + ", ".join(q(x) for x in provider.models) + "]",
            "context_windows = {" + ", ".join(
                f"{q(model)} = {int(window)}" for model, window in provider.context_windows.items()
            ) + "}",
        ])
    # 原子写 + 全局写锁:裸 write_text 是"先截断再写",写一半崩掉(断电/被杀)会留下
    # 残缺的 config.toml,而 load_config 读不出来会静默回落默认值 → api_key、hub_token、
    # 网页密码全丢。同文件 _sync_worker_tools 早就用了 temp+os.replace,这里补齐。
    # 锁防两个设置页同时保存互相覆盖(写的是同一个 CFG 快照)。
    import tempfile
    with _CONFIG_WRITE_LOCK:
        data = "\n".join(lines) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            prefix=".config.", suffix=".tmp", dir=str(cfg_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, cfg_path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


@app.get("/settings")
async def settings_page():
    from fastapi.responses import FileResponse
    html = _frontend_file("settings.html")
    return FileResponse(str(html))


async def _isolation_status_after_cleanup() -> dict[str, object]:
    status = isolation_workspace.status(CFG.agent.isolation_max_mb)
    if status["used_bytes"] > status["limit_bytes"]:
        await asyncio.to_thread(isolation_workspace.cleanup, CFG.agent.isolation_max_mb)
        status = isolation_workspace.status(CFG.agent.isolation_max_mb)
    return status


@app.get("/api/isolation/status")
async def api_isolation_status() -> dict[str, object]:
    return await _isolation_status_after_cleanup()


@app.get("/api/settings")
async def api_get_settings() -> dict:
    try:
        from backend.modules.chat import feishu_module
        recent_feishu_open_id = feishu_module.last_open_id(CFG)
    except Exception:
        recent_feishu_open_id = ""
    env_managed_fields = _env_managed_fields()
    security_warnings = [warning for warning in (
        _password_env_warning(), provider_api_key_env_warning(),
    ) if warning]
    return {
        "llm": {
            "base_url": CFG.llm.base_url,
            "protocol": CFG.llm.protocol,
            "api_key_masked": _mask(CFG.llm.api_key),
            "chat_model": CFG.llm.chat_model,
            "vision_model": CFG.llm.chat_model,
            "models": list(CFG.llm.models),
            "providers": _provider_settings(),
        },
        "agent": {"context_window": CFG.agent.context_window},
        "memory_backup": {
            "dir": CFG.memory.backup_dir,
            "resolved_dir": str(CFG.memory_backup_dir),
            "interval_minutes": CFG.memory.backup_interval_minutes,
            "keep_count": CFG.memory.backup_keep_count,
            **BACKUP_STATUS,
        },
        "memory_sync_audit": _memory_sync_audit().summary(),
        "voice": {
            "secret_id_masked": _mask(CFG.voice.secret_id),
            "secret_key_masked": _mask(CFG.voice.secret_key),
            "region": CFG.voice.region,
            "enabled": CFG.voice.enabled,
            "tts_enabled": CFG.voice.tts_enabled,
            "asr_enabled": CFG.voice.asr_enabled,
            "tts_voice": CFG.voice.tts_voice,
        },
        "feishu": {
            "app_id": CFG.feishu.app_id,
            "app_secret_masked": _mask(CFG.feishu.app_secret),
            "allowed_open_ids": list(CFG.feishu.allowed_open_ids),
            "remarks": dict(getattr(CFG.feishu, "remarks", {})),
            "enabled": CFG.feishu.enabled,
            "last_open_id": recent_feishu_open_id,
        },
        "auth": {
            "enabled": CFG.auth.enabled,   # 只告诉前端"有没有设密码",不回传密码本身
        },
        "desktop": {
            "open_at_login": CFG.agent.open_at_login,
            "packaged": bool(getattr(sys, "frozen", False)),
            "startup_auth_warning": bool(
                CFG.role.is_standalone
                and not _host_is_loopback(CFG.server.host)
                and not CFG.auth.enabled
            ),
        },
        "isolation": await _isolation_status_after_cleanup(),
        "role": {
            "mode": CFG.role.mode,
            "web_url": _cloud_web_url(),
            "hub_url": CFG.role.hub_url,
            "hub_token_masked": _mask(CFG.role.hub_token),
            "worker_id": CFG.role.worker_id,
            "tls_fingerprint": CFG.role.tls_fingerprint,
            "connection_configured": bool(CFG.role.hub_url and CFG.role.hub_token),
            "connection_preview": _connection_preview(
                CFG.role.hub_url, CFG.role.tls_fingerprint,
            ),
        },
        "env_managed_fields": env_managed_fields,
        "security": {"warnings": security_warnings},
    }


class ProviderSettingsRequest(BaseModel):
    id: str = ""
    name: str = ""
    protocol: str = "openai"
    base_url: str = ""
    api_key: str = ""
    models: list[str] = Field(default_factory=list)
    context_windows: dict[str, int] = Field(default_factory=dict)


class ModelTestRequest(BaseModel):
    provider_id: str = ""
    protocol: str = "openai"
    base_url: str = ""
    api_key: str = ""
    model: str = ""


class ModelDiscoveryRequest(BaseModel):
    provider_id: str = ""
    protocol: str = "openai"
    base_url: str = ""
    api_key: str = ""


class HubConnectionTestRequest(BaseModel):
    connection_string: str = ""
    hub_url: str = ""
    hub_token: str = ""
    tls_fingerprint: str = ""
    worker_id: str = ""


class SettingsRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""
    chat_model: str = ""
    vision_model: str = ""
    models: str = ""
    providers: list[ProviderSettingsRequest] | None = None
    context_window: int | None = None
    memory_backup_dir: str | None = None
    memory_backup_interval_minutes: int | None = None
    memory_backup_keep_count: int | None = None
    tc_secret_id: str = ""
    tc_secret_key: str = ""
    tc_region: str = ""
    tts_voice: int | None = None
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_allowed_open_ids: str | None = None
    role_mode: str | None = None
    hub_url: str | None = None
    hub_token: str | None = None
    tls_fingerprint: str | None = None
    connection_string: str | None = None
    worker_id: str | None = None
    web_password: str = ""     # 网页访问密码;留空=不改,填 "-" =清空(关闭远程网页访问)
    open_at_login: bool | None = None
    isolation_max_mb: int | None = None


class WeixinConfigRequest(BaseModel):
    allowed_user_ids: list[str] = Field(default_factory=list)


class AccountRemarkRequest(BaseModel):
    remark: str = Field(default="", max_length=80)


_SETTING_SECTION_FIELDS = {
    "models": {
        "base_url", "api_key", "chat_model", "vision_model", "models", "providers",
        "context_window",
    },
    "memory": {
        "memory_backup_dir", "memory_backup_interval_minutes", "memory_backup_keep_count",
    },
    "voice": {
        "tc_secret_id", "tc_secret_key", "tc_region", "tts_voice",
    },
    "connections": {
        "feishu_app_id", "feishu_app_secret", "feishu_allowed_open_ids",
        "role_mode", "hub_url", "hub_token", "tls_fingerprint", "connection_string", "worker_id",
    },
    "system": {"web_password", "open_at_login", "isolation_max_mb"},
}


class ExtensionInstallRequest(BaseModel):
    url: str
    kind: str
    confirm_install: bool = False


class ExtensionEnableRequest(BaseModel):
    kind: str
    name: str
    enabled: bool


class ExtensionRollbackRequest(BaseModel):
    kind: str
    name: str


class MemoryBackupRequest(BaseModel):
    directory: str | None = None
    interval_minutes: int | None = None
    keep_count: int | None = None


def _apply_memory_backup_settings(
    directory: str | None, interval_minutes: int | None, keep_count: int | None,
) -> None:
    if directory is not None:
        CFG.memory.backup_dir = directory.strip()
    if interval_minutes is not None:
        CFG.memory.backup_interval_minutes = max(5, min(interval_minutes, 7 * 24 * 60))
        _wake_memory_loop()  # 间隔改了,唤醒 loop 立刻用新间隔(不用等旧的 sleep 走完)
    if keep_count is not None:
        CFG.memory.backup_keep_count = max(2, min(keep_count, 365))


def _changed(new: str, current: str) -> bool:
    """判断字段是否被真正修改:留空 或 仍是脱敏值 视为未改。"""
    return bool(new) and new != _mask(current)


def _env_managed_fields() -> dict[str, str]:
    fields = {}
    for env_name, path in SECRET_ENV_FIELDS.items():
        if os.getenv(env_name) or (env_name == "AGENT_WEB_PASSWORD" and CFG.role.is_hub and env_name in os.environ):
            field = "llm.default.api_key" if path == ("llm", "api_key") else ".".join(path)
            fields[field] = env_name
    for provider_id in provider_api_key_env_overrides():
        fields[f"llm.providers.{provider_id}.api_key"] = PROVIDER_API_KEYS_ENV
    return fields


def _ignored_env_setting_changes(req: SettingsRequest) -> list[dict[str, str]]:
    managed = _env_managed_fields()
    ignored: list[dict[str, str]] = []

    def add(field: str, changed: bool) -> None:
        if changed and field in managed:
            ignored.append({"field": field, "env": managed[field]})

    if req.providers is not None:
        old_custom = {provider.id: provider for provider in CFG.llm.providers}
        for index, item in enumerate(req.providers):
            provider_id = item.id.strip() or f"provider-{index + 1}"
            if provider_id == "default":
                add("llm.default.api_key", _changed(item.api_key.strip(), CFG.llm.api_key))
            else:
                current = old_custom.get(provider_id)
                add(
                    f"llm.providers.{provider_id}.api_key",
                    _changed(item.api_key.strip(), current.api_key if current else ""),
                )
    add("llm.default.api_key", _changed(req.api_key.strip(), CFG.llm.api_key))
    add("voice.secret_id", _changed(req.tc_secret_id.strip(), CFG.voice.secret_id))
    add("voice.secret_key", _changed(req.tc_secret_key.strip(), CFG.voice.secret_key))
    add(
        "feishu.app_id",
        bool(req.feishu_app_id.strip()) and req.feishu_app_id.strip() != CFG.feishu.app_id,
    )
    add("feishu.app_secret", _changed(req.feishu_app_secret.strip(), CFG.feishu.app_secret))
    if req.hub_token is not None:
        add("role.hub_token", _changed(req.hub_token.strip(), CFG.role.hub_token))
    if req.web_password:
        new_password = "" if req.web_password.strip() == "-" else req.web_password.strip()
        add("auth.password", new_password != CFG.auth.password)
    return ignored


def _connection_preview(hub_url: str, fingerprint: str) -> dict[str, str | int] | None:
    parsed = urllib.parse.urlparse(hub_url)
    if not parsed.hostname:
        return None
    return {
        "host": parsed.hostname,
        "port": parsed.port or 443,
        "fingerprint_prefix": str(fingerprint or "").replace(":", "").lower()[:12],
    }


def _cloud_web_url() -> str:
    """只使用已保存的连接或本服务器部署配置，包含最终监听端口。"""
    if CFG.role.is_worker and CFG.role.hub_token:
        hub_url = CFG.role.hub_url
    elif CFG.role.is_hub and (host := os.getenv("AGENT_PUBLIC_HOST", "").strip()):
        host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        hub_url = f"wss://{host}:{CFG.server.port}/worker"
    else:
        return ""
    try:
        parsed = urllib.parse.urlparse(hub_url)
        if (parsed.scheme != "wss" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path.rstrip("/") != "/worker"
                or parsed.query or parsed.fragment):
            return ""
        port = parsed.port if parsed.port is not None else 443
        if not 1 <= port <= 65535:
            return ""
        host = parsed.hostname
        host = f"[{host}]" if ":" in host else host
        if CFG.role.is_hub:
            try:
                info = json.loads((app_home() / "data/public-tls.json").read_text(encoding="utf-8"))
                if info.get("host") == parsed.hostname and 1024 <= int(info["web_port"]) <= 65535:
                    port = int(info["web_port"])
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass
        return f"https://{host}:{port}"
    except ValueError:
        return ""


def _validate_worker_connection(
    mode: str, hub_url: str, hub_token: str, worker_id: str, tls_fingerprint: str = "",
) -> None:
    if mode not in ("standalone", "worker"):
        raise ValueError("本机模式只能是 standalone 或 worker")
    if mode == "standalone":
        return
    parsed = urllib.parse.urlparse(hub_url)
    if parsed.scheme != "wss" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("云端地址必须是 wss://地址/worker，不能使用明文 ws://")
    tls_fingerprint = tls_fingerprint.replace(":", "").strip().lower()
    if tls_fingerprint and not re.fullmatch(r"[0-9a-f]{64}", tls_fingerprint):
        raise ValueError("证书指纹必须是 64 位 SHA256 十六进制值")
    try:
        ipaddress.ip_address(parsed.hostname)
        is_ip = True
    except ValueError:
        is_ip = False
    try:
        ascii_host = parsed.hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("云端域名格式无效") from exc
    if not is_ip:
        labels = ascii_host.rstrip(".").split(".")
        if len(labels) < 2 or any(
            not label or len(label) > 63 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
            for label in labels
        ):
            raise ValueError("请填写完整域名或 IP 地址")
    if parsed.path.rstrip("/") != "/worker" or parsed.query or parsed.fragment:
        raise ValueError("云端地址路径必须是 /worker，且不能带查询参数或片段")
    if not hub_token:
        raise ValueError("请填写与云端 AGENT_HUB_TOKEN 一致的连接口令")
    if not worker_id or len(worker_id) > 100:
        raise ValueError("机器名称不能为空且最多 100 个字符")


def _exception_chain(exc: BaseException):
    while exc is not None:
        yield exc
        exc = exc.__cause__ or exc.__context__


async def _probe_hub_connection(
    hub_url: str, hub_token: str, worker_id: str, tls_fingerprint: str = "",
) -> dict:
    import websockets

    started = time.perf_counter()
    parsed = urllib.parse.urlparse(hub_url)
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM,
            ),
            timeout=5,
        )
        connect_kwargs = direct_websocket_options(
            websockets.connect, open_timeout=8, close_timeout=2, ping_interval=None,
        )
        connect_kwargs["ssl"] = hub_ssl_context(tls_fingerprint)
        async with websockets.connect(hub_url, **connect_kwargs) as ws:
            if tls_fingerprint:
                verify_peer_fingerprint(ws, tls_fingerprint)
            await ws.send(json.dumps({
                "type": "hello", "token": hub_token, "worker_id": worker_id,
                "tools": [], "probe": True,
            }))
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
            if reply.get("type") == "deny":
                return {"ok": False, "error_code": "auth", "error": "连接口令不正确"}
            if reply.get("type") != "welcome":
                return {"ok": False, "error_code": "server", "error": "服务器响应不是小A hub 协议"}
            return {
                "ok": True,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "message": "云端握手成功",
            }
    except Exception as exc:
        chain = tuple(_exception_chain(exc))
        if any(isinstance(item, socket.gaierror) for item in chain):
            return {"ok": False, "error_code": "dns", "error": "域名无法解析，请检查域名和网络"}
        if any(isinstance(item, ssl.SSLError) and "指纹" in str(item) for item in chain):
            return {"ok": False, "error_code": "fingerprint", "error": "云端证书指纹不匹配，请重新粘贴连接串"}
        if any(isinstance(item, (ssl.SSLCertVerificationError, ssl.SSLError)) for item in chain):
            return {"ok": False, "error_code": "tls", "error": "TLS 证书无效或与地址不匹配"}
        return {
            "ok": False, "error_code": "server",
            "error": "服务器未启动、端口未开放或 /worker 地址不可用",
        }


@app.post("/api/hub/test")
async def api_test_hub_connection(req: HubConnectionTestRequest, request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="云端连接只能在本机测试")
    fingerprint = req.tls_fingerprint.strip()
    if req.connection_string.strip():
        try:
            details = parse_connection_string(req.connection_string)
        except ValueError as exc:
            return {"ok": False, "error_code": "parse", "error": str(exc)}
        hub_url, hub_token, fingerprint = details.hub_url, details.token, details.fingerprint
        preview = masked_preview(details)
    else:
        hub_url = req.hub_url.strip()
        hub_token = req.hub_token.strip()
        preview = _connection_preview(hub_url, fingerprint)
    if not _changed(hub_token, CFG.role.hub_token):
        hub_token = CFG.role.hub_token
    worker_id = req.worker_id.strip() or CFG.role.worker_id
    try:
        _validate_worker_connection("worker", hub_url, hub_token, worker_id, fingerprint)
    except ValueError as exc:
        return {"ok": False, "error_code": "validation", "error": str(exc)}
    result = await _probe_hub_connection(hub_url, hub_token, worker_id, fingerprint)
    if preview:
        result["connection"] = preview
    return result


class ConnectionStringRequest(BaseModel):
    connection_string: str = ""


class CloudDeployRequest(BaseModel):
    host: str = ""
    username: str = "root"
    password: str = ""
    worker_id: str = ""
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_allowed_open_ids: str = ""
    operation: str = "deploy"
    confirmation: str = ""
    purge_data: bool = False
    purge_confirmation: str = ""
    use_release: bool = False  # Accepted for older clients; deployment always uses bundled files.


class CloudFeishuRequest(BaseModel):
    allowed_open_ids: str = ""
    app_id: str = ""
    app_secret: str = ""


def _load_cloud_jobs() -> None:
    from backend.core import cloud_jobs
    with _CLOUD_JOB_LOCK:
        if not _CLOUD_DEPLOY_JOBS:
            _CLOUD_DEPLOY_JOBS.update(cloud_jobs.load(app_home() / "data/cloud-deploy-jobs.json"))
            if _CLOUD_DEPLOY_JOBS:
                try:
                    cloud_jobs.save(app_home() / "data/cloud-deploy-jobs.json", _CLOUD_DEPLOY_JOBS)
                except OSError:
                    pass


def _cloud_job_update(job_id: str, *, stage: str, message: str, **extra: object) -> None:
    from backend.core import cloud_jobs
    with _CLOUD_JOB_LOCK:
        job = _CLOUD_DEPLOY_JOBS.get(job_id)
        if job is None:
            return
        cloud_jobs.update(job, stage, str(_redact_event_data(str(message)))[-2000:], **extra)
        try:
            cloud_jobs.save(app_home() / "data/cloud-deploy-jobs.json", _CLOUD_DEPLOY_JOBS)
        except OSError:
            # A full local disk must not abort a healthy remote transaction mid-flight.
            job["message"] += "（本机记录写入失败，重开程序后请检查云端状态）"


async def _cloud_connect_worker(job_id: str, *, connection: dict | None = None) -> None:
    _cloud_job_update(job_id, stage="worker", message="本地配置已保存，正在连接云端 worker…", cloud_ready=True)
    try:
        await _replace_worker_bridge()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if WORKER_BRIDGE is not None and WORKER_BRIDGE.connected:
                _cloud_job_update(job_id, stage="done", message="云端连接成功，worker 已上线",
                                  ok=True, connection=connection or {}, worker_id=CFG.role.worker_id)
                return
            await asyncio.sleep(0.5)
        raise RuntimeError("worker 暂未上线；请放行显示端口的安全组后点击重试连接，无需重新上传")
    except Exception as exc:
        _cloud_job_update(job_id, stage="partial", message="云端已部署，本机连接未完成：" + str(exc),
                          ok=False, cloud_ready=True, connection=connection or {})


async def _run_cloud_deploy(job_id: str, req: CloudDeployRequest) -> None:
    password = req.password
    req.password = ""
    feishu_secret = req.feishu_app_secret.strip()
    try:
        from backend.core import cloud_deploy as cloud_deployer
        if req.operation == "retry":
            target_path = app_home() / "data/cloud-ssh/target.json"
            target = json.loads(target_path.read_text(encoding="utf-8")) if target_path.is_file() else {
                "host": _CLOUD_DEPLOY_JOBS.get(job_id, {}).get("host"),
                "username": _CLOUD_DEPLOY_JOBS.get(job_id, {}).get("username"),
            }
            if target.get("host") != req.host or target.get("username") != req.username:
                raise ValueError("重试连接仅适用于已部署的服务器")
            status = await asyncio.to_thread(cloud_deployer.maintain, req.host, req.username, password, app_home())
            _cloud_job_update(job_id, stage="health", message="已读取云端状态，正在核验健康检查结果", remote_status=status)
            if not status.get("healthy"):
                raise ValueError("云端健康检查未通过，请先检查状态或修复部署")
            result = await asyncio.to_thread(cloud_deployer.recover_connection, req.host, req.username, password, app_home())
            details = parse_connection_string(result["connection_string"])
            CFG.role.mode = "worker"
            CFG.role.hub_url, CFG.role.hub_token, CFG.role.tls_fingerprint = details.hub_url, details.token, details.fingerprint
            CFG.role.worker_id = CFG.role.worker_id or req.worker_id.strip() or socket.gethostname()
            _rewrite_config()
            await _cloud_connect_worker(job_id, connection=result["connection"])
            return
        if req.operation in {"check", "uninstall"}:
            _cloud_job_update(job_id, stage="ssh", message="正在检查云端状态…" if req.operation == "check" else "正在安全卸载小A云端服务…")
            status = await asyncio.to_thread(cloud_deployer.maintain, req.host, req.username, password, app_home(),
                                             operation=req.operation, purge=req.purge_data)
            if req.operation == "uninstall":
                current = app_home() / "data/cloud-ssh/target.json"
                target = json.loads(current.read_text(encoding="utf-8")) if current.is_file() else {}
                if target.get("host") == req.host and target.get("username") == req.username:
                    await _replace_worker_bridge(start=False)
                    CFG.role.mode = "standalone"
                    CFG.role.hub_url = CFG.role.hub_token = CFG.role.tls_fingerprint = ""
                    _rewrite_config()
            message = ("已卸载，数据已保留" if status.get("data_preserved") else "已卸载，并删除云端小A数据") if req.operation == "uninstall" else (
                "云端运行正常" if status.get("healthy") else "云端未安装或服务异常，请部署/修复")
            _cloud_job_update(job_id, stage="done", message=message, ok=True, remote_status=status)
            return
        result = await asyncio.to_thread(
            cloud_deployer.deploy,
            req.host, req.username, password, app_home(),
            feishu_app_id=req.feishu_app_id,
            feishu_app_secret=feishu_secret,
            feishu_allowed_open_ids=[
                item.strip() for item in req.feishu_allowed_open_ids.split(",") if item.strip()
            ],
            llm_protocol=CFG.llm.protocol,
            llm_base_url=CFG.llm.base_url,
            llm_api_key=CFG.llm.api_key,
            chat_model=CFG.llm.chat_model,
            model_options=CFG.llm.models,
            llm_providers=CFG.llm.providers,
            llm_provider_name=CFG.llm.provider_name,
            llm_context_windows=CFG.llm.context_windows,
            context_window=CFG.agent.context_window,
            voice_secret_id=CFG.voice.secret_id,
            voice_secret_key=CFG.voice.secret_key,
            voice_region=CFG.voice.region,
            tts_voice=CFG.voice.tts_voice,
            tts_speed=CFG.voice.tts_speed,
            web_password=CFG.auth.password,
            operation=req.operation,
            progress=lambda stage, message, **extra: _cloud_job_update(
                job_id, stage=stage, message=message, **extra,
            ),
        )
        _cloud_job_update(job_id, stage="worker", message="云端已健康上线，正在保存本地连接配置",
                          cloud_ready=True, connection=result["connection"],
                          remote_status=result.get("remote_status", {}))
        details = parse_connection_string(result["connection_string"])
        CFG.role.mode = "worker"
        CFG.role.hub_url = details.hub_url
        CFG.role.hub_token = details.token
        CFG.role.tls_fingerprint = details.fingerprint
        CFG.role.worker_id = req.worker_id.strip() or CFG.role.worker_id or socket.gethostname()
        _rewrite_config()
        await _cloud_connect_worker(job_id, connection=result["connection"])
    except Exception as exc:
        ready = bool(_CLOUD_DEPLOY_JOBS.get(job_id, {}).get("cloud_ready"))
        _cloud_job_update(job_id, stage="partial" if ready else "error", message=str(exc), ok=False)
    finally:
        password = ""
        feishu_secret = ""
        req.feishu_app_secret = ""


@app.get("/api/update/release")
async def api_update_release(request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="版本检查只能在本机读取")
    try:
        from backend.core import update_manager
        manifest = await asyncio.to_thread(update_manager.fetch_manifest)
        with _UPDATE_JOB_LOCK:
            job = copy.deepcopy(_UPDATE_JOB)
        if manifest is None:
            return {
                "ok": True, "current_version": __version__, "latest": None,
                "update_available": False, "job": job,
                "message": "尚无公开版本，请稍后再试。",
            }
        return {
            "ok": True,
            "current_version": __version__,
            "latest": manifest,
            "update_available": update_manager.version_key(manifest["version"]) > update_manager.version_key(__version__),
            "job": job,
        }
    except Exception as exc:
        return {"ok": False, "current_version": __version__, "error": str(exc)}




def _update_job_set(**values: object) -> None:
    with _UPDATE_JOB_LOCK:
        _UPDATE_JOB.update(values)
        snapshot = copy.deepcopy(_UPDATE_JOB)
    try:
        update_manager.save_update_state(_UPDATE_STATE_PATH, snapshot)
    except OSError as exc:
        # 状态落盘失败不应中断下载或让更新流程误报失败。
        print(f"[update] 更新状态保存失败: {exc}")


async def _run_portable_update() -> None:
    try:
        from backend.core import update_manager
        _update_job_set(stage="checking", percent=0, message="正在检查 GitHub Releases…", error="")
        manifest = await asyncio.to_thread(update_manager.fetch_manifest)
        if manifest is None:
            raise update_manager.ReleaseError("暂无可用的公开版本")
        if _UPDATE_CANCEL.is_set():
            raise update_manager.UpdateCancelled()
        if update_manager.version_key(manifest["version"]) <= update_manager.version_key(__version__):
            _update_job_set(stage="done", percent=100, message=f"当前已是最新版本 v{__version__}", latest=manifest["version"], update_available=False)
            return
        executable = os.getenv("XIAOA_EXECUTABLE_PATH", "").strip()
        parent_pid = int(os.getenv("XIAOA_PARENT_PID", "0") or 0)
        if not executable or not parent_pid:
            raise update_manager.ReleaseError("开发版不自动替换源码目录，请使用正式版 EXE 更新")
        _update_job_set(stage="downloading", percent=0, message=f"正在下载正式版 v{manifest['version']}…", latest=manifest["version"], update_available=True)
        def progress(percent, received, total):
            if _UPDATE_CANCEL.is_set():
                raise update_manager.UpdateCancelled()
            _update_job_set(percent=int(percent), message=f"正在下载正式版… {int(percent)}%" if total else f"正在下载正式版… {received // 1024 // 1024} MB")
        prepared = await asyncio.to_thread(
            update_manager.prepare_portable_update, manifest,
            executable=executable, parent_pid=parent_pid, backend_pid=os.getpid(), progress=progress,
            state_path=_UPDATE_STATE_PATH,
            log_path=app_home() / "data" / "update.log",
            health_url=f"http://127.0.0.1:{CFG.server.port}/health",
        )
        if _UPDATE_CANCEL.is_set():
            update_manager.cancel_prepared_update(prepared["prepared_config"])
            raise update_manager.UpdateCancelled()
        with _UPDATE_JOB_LOCK:
            if _UPDATE_CANCEL.is_set():
                raise update_manager.UpdateCancelled()
            _update_job_set(stage="staged", percent=100,
                            message=f"v{manifest['version']} 已下载并校验完成，点击立即重启更新。",
                            prepared_config=prepared["prepared_config"], error="")
    except update_manager.UpdateCancelled:
        _update_job_set(stage="cancelled", percent=0, message="已取消更新，当前版本保持不变", error="", prepared_config="")
    except Exception as exc:
        _update_job_set(stage="error", percent=0, message="更新失败", error=str(exc))


@app.post("/api/update/install")
async def api_update_install(request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="更新只能在本机启动")
    with _UPDATE_JOB_LOCK:
        if _UPDATE_JOB.get("stage") in {
            "queued", "checking", "downloading", "cancelling", "staged", "replacing", "restarting", "verifying",
        }:
            return {"ok": False, "error": "已有本地更新正在进行"}
        _UPDATE_CANCEL.clear()
        _UPDATE_JOB.clear()
        _UPDATE_JOB.update({"stage": "queued", "percent": 0, "message": "已开始检查更新", "error": ""})
        snapshot = copy.deepcopy(_UPDATE_JOB)
    try:
        update_manager.save_update_state(_UPDATE_STATE_PATH, snapshot)
    except OSError as exc:
        print(f"[update] 更新状态保存失败: {exc}")
    asyncio.create_task(_run_portable_update())
    return {"ok": True}


@app.post("/api/update/cancel")
async def api_update_cancel(request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="更新只能在本机取消")
    with _UPDATE_JOB_LOCK:
        stage = _UPDATE_JOB.get("stage")
        if stage in {"restarting", "replacing", "verifying"}:
            return {"ok": False, "error": "已开始替换文件，不能中途取消"}
        _UPDATE_CANCEL.set()
        active = stage in {"queued", "checking", "downloading", "cancelling"}
        prepared = str(_UPDATE_JOB.get("prepared_config") or "")
        cleanup_warning = ""
        if not active:
            try:
                update_manager.cancel_prepared_update(prepared)
            except Exception:
                cleanup_warning = "；临时文件未自动清理，不影响继续使用旧版"
        _update_job_set(stage="cancelling" if active else "cancelled", percent=0,
                        message="正在取消下载…" if active else "已取消更新，当前版本保持不变" + cleanup_warning,
                        error="", prepared_config="")
    return {"ok": True}


@app.post("/api/update/restart")
async def api_update_restart(request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="更新只能在本机启动")
    with _UPDATE_JOB_LOCK:
        if _UPDATE_JOB.get("stage") != "staged":
            return {"ok": False, "error": "请先下载并校验更新文件"}
        config = str(_UPDATE_JOB.get("prepared_config") or "")
        if not config:
            return {"ok": False, "error": "旧版残留更新没有有效准备记录，请取消后重新下载"}
        _update_job_set(stage="restarting", message="正在启动独立更新器…", error="")
    try:
        await asyncio.to_thread(update_manager.restart_prepared_update, config,
                              executable=os.getenv("XIAOA_EXECUTABLE_PATH", ""),
                              parent_pid=int(os.getenv("XIAOA_PARENT_PID", "0") or 0),
                              backend_pid=os.getpid())
        return {"ok": True}
    except Exception as exc:
        _update_job_set(stage="staged", message="重启更新失败，可以重试或取消", error=str(exc))
        return {"ok": False, "error": str(exc)}


@app.get("/api/update/job")
async def api_update_job(request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="更新状态只能在本机读取")
    with _UPDATE_JOB_LOCK:
        if _UPDATE_JOB.get("stage") in {"restarting", "replacing", "verifying"}:
            saved = update_manager.load_update_state(_UPDATE_STATE_PATH)
            _UPDATE_JOB.update(saved)
        return copy.deepcopy(_UPDATE_JOB)

@app.post("/api/cloud/deploy")
async def api_cloud_deploy(req: CloudDeployRequest, request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="云端部署只能在本机启动")
    headers = getattr(request, "headers", {})
    origin = headers.get("origin", "")
    if origin and origin not in _ALLOWED_ORIGINS and origin != str(getattr(request, "base_url", "")).rstrip("/"):
        raise HTTPException(status_code=403, detail="云端维护不接受跨站请求")
    if req.operation not in {"deploy", "repair", "check", "uninstall", "retry"}:
        return {"ok": False, "error": "未知云端操作"}
    if req.operation == "uninstall":
        if not req.password.strip() or req.confirmation != "remote_password":
            return {"ok": False, "error": "卸载需要填写远程密码并确认"}
        if req.purge_data and req.purge_confirmation != f"删除数据 {req.host.strip()}":
            return {"ok": False, "error": "彻底删除云端数据需要额外确认"}
    if bool(req.feishu_app_id.strip()) != bool(req.feishu_app_secret.strip()):
        return {"ok": False, "error": "飞书 App ID 和 App Secret 需要同时填写，或都留空"}
    cloud_ids = [item.strip() for item in req.feishu_allowed_open_ids.split(",") if item.strip()]
    if any(not item.startswith("ou_") for item in cloud_ids):
        return {"ok": False, "error": "云端 Open ID 应以 ou_ 开头，请检查后再连接"}
    try:
        from backend.core import cloud_deploy as cloud_deployer

        cloud_deployer.validate_target(req.host, req.username)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    from backend.core import cloud_jobs
    _load_cloud_jobs()
    with _CLOUD_JOB_LOCK:
        if any(job.get("stage") not in cloud_jobs.TERMINAL for job in _CLOUD_DEPLOY_JOBS.values()):
            active = next(j for j in _CLOUD_DEPLOY_JOBS.values() if j.get("stage") not in cloud_jobs.TERMINAL)
            return {"ok": False, "error": "已有云端部署/维护正在进行，请等待完成", "active_job_id": active["job_id"]}
        job_id = uuid.uuid4().hex
        host, username = cloud_deployer.validate_target(req.host, req.username)
        req.host, req.username = host, username
        _CLOUD_DEPLOY_JOBS[job_id] = cloud_jobs.new_job(job_id, req.operation, host, username, __version__)
        # Keep only current and previous task, even after repeated deployments.
        for old in sorted(_CLOUD_DEPLOY_JOBS, key=lambda key: _CLOUD_DEPLOY_JOBS[key]["created_at"])[:-2]:
            del _CLOUD_DEPLOY_JOBS[old]
        try:
            cloud_jobs.save(app_home() / "data/cloud-deploy-jobs.json", _CLOUD_DEPLOY_JOBS)
        except OSError:
            del _CLOUD_DEPLOY_JOBS[job_id]
            req.password = req.feishu_app_secret = ""
            return {"ok": False, "error": "无法保存本机任务记录，请检查磁盘空间/权限；未开始部署"}
    asyncio.create_task(_run_cloud_deploy(job_id, req))
    return {"ok": True, "job_id": job_id}


@app.get("/api/cloud/deploy/latest")
async def api_cloud_deploy_latest(request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="云端部署状态只能在本机读取")
    _load_cloud_jobs()
    with _CLOUD_JOB_LOCK:
        jobs = sorted(_CLOUD_DEPLOY_JOBS.values(), key=lambda j: j["created_at"])
        return copy.deepcopy({"job": jobs[-1] if jobs else None, "previous": jobs[-2] if len(jobs) > 1 else None})


@app.get("/api/cloud/deploy/{job_id}")
async def api_cloud_deploy_status(job_id: str, request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="云端部署状态只能在本机读取")
    _load_cloud_jobs()
    with _CLOUD_JOB_LOCK:
        job = _CLOUD_DEPLOY_JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="部署任务不存在或已过期")
        return copy.deepcopy(job)


async def _logout_all_feishu() -> dict:
    """Clear local and cloud Feishu bindings, including recent-user state."""
    from backend.modules.chat import feishu_module

    local = {"configured": bool(CFG.feishu.app_id or CFG.feishu.app_secret or CFG.feishu.allowed_open_ids), "ok": True}
    try:
        await asyncio.to_thread(feishu_module.stop_listener)
        feishu_module.reset_client()
        feishu_module.clear_last_open_id()
        try:
            from backend.core import vault
            vault.put("feishu_app_secret", "")
        except Exception:
            pass
        CFG.feishu.app_id = ""
        CFG.feishu.app_secret = ""
        CFG.feishu.allowed_open_ids = []
        _rewrite_config()
        local.update({"app_id": "", "app_secret_configured": False, "allowed_open_ids": []})
    except Exception as exc:
        local.update({"ok": False, "error": str(exc)})

    cloud = {"configured": False, "ok": True, "app_id": "", "app_secret_configured": False, "allowed_open_ids": []}
    target_path = app_home() / "data" / "cloud-ssh" / "target.json"
    if target_path.is_file():
        cloud["configured"] = True
        try:
            from backend.core import cloud_deploy as cloud_deployer
            await asyncio.to_thread(cloud_deployer.update_feishu, app_home(), [], logout=True)
            remote = await asyncio.to_thread(cloud_deployer.get_feishu_status, app_home())
            if (remote.get("app_id") or remote.get("app_secret_configured")
                    or remote.get("allowed_open_ids") or remote.get("last_open_id")):
                raise RuntimeError("云端飞书状态复查仍显示已配置")
            cloud.update({
                "app_id": "", "app_secret_configured": False,
                "allowed_open_ids": [], "last_open_id": "",
            })
        except Exception as exc:
            cloud.update({"ok": False, "error": str(exc)})

    ok = bool(local.get("ok")) and bool(cloud.get("ok"))
    return {
        "ok": ok,
        "local": local,
        "cloud": cloud,
        "message": "本机和云端飞书账号已全部解绑" if ok else "飞书解绑未完全成功，请重试",
    }


@app.post("/api/cloud/feishu")
async def api_cloud_feishu(req: CloudFeishuRequest, request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="云端设置只能在本机修改")
    ids = [item.strip() for item in req.allowed_open_ids.split(",") if item.strip()]
    if any(not item.startswith("ou_") for item in ids):
        return {"ok": False, "error": "Open ID 应以 ou_ 开头，请检查后再保存"}
    if bool(req.app_id.strip()) != bool(req.app_secret.strip()):
        return {"ok": False, "error": "更换云端 App ID 时请同时填写对应的 App Secret"}
    app_secret = req.app_secret.strip()
    try:
        from backend.core import cloud_deploy as cloud_deployer

        await asyncio.to_thread(
            cloud_deployer.update_feishu, app_home(), ids,
            app_id=req.app_id, app_secret=app_secret,
        )
        return {"ok": True, "message": "飞书配置已更新，云端服务已重启"}
    except Exception as exc:
        req.app_secret = ""
        return {"ok": False, "error": str(exc)}
    finally:
        req.app_secret = ""
        app_secret = ""


@app.post("/api/cloud/feishu/authorize-recent")
async def api_cloud_feishu_authorize_recent(request: Request, open_id: str = "") -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="云端设置只能在本机修改")
    try:
        from backend.core import cloud_deploy as cloud_deployer
        await asyncio.to_thread(cloud_deployer.update_feishu, app_home(), [], authorize_pending=True,
                                **({"pending_open_id": open_id} if open_id else {}))
        return {"ok": True, "message": "最近私聊用户已加入云端飞书白名单并重启服务"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/cloud/feishu/allowlist")
async def api_cloud_feishu_allowlist(ids: WeixinConfigRequest, request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="云端设置只能在本机修改")
    values = list(dict.fromkeys(x.strip() for x in ids.allowed_user_ids if x.strip()))
    if len(values) > 100 or any(not x.startswith("ou_") or len(x) > 256 for x in values):
        return {"ok": False, "error": "Open ID 格式无效，白名单最多 100 个用户"}
    try:
        from backend.core import cloud_deploy as cloud_deployer
        await asyncio.to_thread(cloud_deployer.update_feishu, app_home(), values)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/cloud/feishu")
async def api_get_cloud_feishu(request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="云端设置只能在本机读取")
    try:
        from backend.core import cloud_deploy as cloud_deployer

        return await asyncio.to_thread(cloud_deployer.get_feishu_status, app_home())
    except Exception as exc:
        return {
            "connected": False, "app_id": "", "allowed_open_ids": [],
            "app_secret_configured": False, "error": str(exc),
        }


@app.delete("/api/feishu/account")
@app.delete("/api/feishu/accounts")
async def api_feishu_logout(request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="飞书全部解绑需在本机小A操作")
    return await _logout_all_feishu()


@app.delete("/api/cloud/feishu/account")
async def api_cloud_feishu_logout(request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="云端设置只能在本机修改")
    return await _logout_all_feishu()


@app.get("/api/feishu/recent")
async def api_feishu_recent() -> dict:
    try:
        from backend.modules.chat import feishu_module
        result = {"last_open_id": feishu_module.last_open_id(CFG)}
        candidates = feishu_module.pending_open_ids(CFG)
        if candidates:
            result.update(pending_available=True, pending_open_ids=candidates)
        return result
    except Exception as exc:
        raise HTTPException(status_code=503, detail="最近私聊记录暂时无法读取") from exc


@app.post("/api/feishu/authorize-recent")
async def api_feishu_authorize_recent(request: Request, open_id: str = "") -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="飞书连接设置需要本机或已登录的云端管理员修改")
    from backend.modules.chat import feishu_module
    candidates = feishu_module.pending_open_ids(CFG)
    candidate = open_id or (candidates[0] if len(candidates) == 1 else "")
    if not candidate or candidate not in candidates:
        return {"ok": False, "error": "没有可授权的最近未授权用户，请让对方重新私聊"}
    CFG.feishu.allowed_open_ids = list(dict.fromkeys(CFG.feishu.allowed_open_ids + [candidate]))
    feishu_module.clear_pending_open_id(candidate)
    _rewrite_config()
    return {"ok": True, "message": "最近私聊用户已加入飞书白名单"}


@app.post("/api/feishu/allowlist")
async def api_feishu_allowlist(ids: WeixinConfigRequest, request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="飞书连接设置需要本机或已登录的云端管理员修改")
    values = list(dict.fromkeys(x.strip() for x in ids.allowed_user_ids if x.strip()))
    if len(values) > 100 or any(not x.startswith("ou_") or len(x) > 256 for x in values):
        return {"ok": False, "error": "Open ID 格式无效，白名单最多 100 个用户"}
    CFG.feishu.allowed_open_ids = values
    CFG.feishu.remarks = {
        key: value for key, value in getattr(CFG.feishu, "remarks", {}).items() if key in values
    }
    _rewrite_config()
    return {"ok": True}


@app.post("/api/feishu/users/{open_id}/remark")
async def api_feishu_user_remark(open_id: str, req: AccountRemarkRequest, request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="飞书用户备注需要本机或已登录的云端管理员修改")
    if open_id not in CFG.feishu.allowed_open_ids:
        return {"ok": False, "error": "飞书用户未在授权名单中"}
    if not hasattr(CFG.feishu, "remarks"):
        CFG.feishu.remarks = {}
    if req.remark.strip():
        CFG.feishu.remarks[open_id] = req.remark.strip()
    else:
        CFG.feishu.remarks.pop(open_id, None)
    _rewrite_config()
    return {"ok": True, "remarks": dict(CFG.feishu.remarks)}


@app.post("/api/cloud/feishu/users/{open_id}/remark")
async def api_cloud_feishu_user_remark(open_id: str, req: AccountRemarkRequest, request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="云端飞书用户备注只能在本机修改")
    try:
        from backend.core import cloud_deploy as cloud_deployer
        await asyncio.to_thread(
            cloud_deployer.update_feishu, app_home(), None,
            remark_open_id=open_id, remark=req.remark, restart=False,
        )
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/weixin/status")
async def api_weixin_status(request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="微信连接设置需要本机或已登录的云端管理员读取")
    from backend.modules.chat import weixin_clawbot
    status = weixin_clawbot.status()
    from backend.modules.memory.shared_memory import get_shared_settings
    status["shared_memory"] = await asyncio.to_thread(get_shared_settings, _weixin_memories().shared)
    accounts = weixin_clawbot._accounts()
    if accounts:
        manager = _weixin_memories()
        await asyncio.to_thread(manager.register_accounts, accounts)
        for item in status.get("accounts", []):
            item["shared_memory_owner"] = manager.is_owner(item["account_id"], accounts)
    status["shared_libraries"] = await asyncio.to_thread(_weixin_memories().libraries)
    return status


@app.post("/api/weixin/login")
async def api_weixin_login(request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="微信扫码登录需要本机或已登录的云端管理员发起")
    from backend.modules.chat import weixin_clawbot
    try:
        return {"ok": True, **await weixin_clawbot.begin_login()}
    except Exception as exc:
        return {"ok": False, "error": f"二维码获取失败：{type(exc).__name__}: {exc}"}


@app.get("/api/weixin/login/status")
async def api_weixin_login_status(request: Request, login_id: str = "") -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="微信登录状态需要本机或已登录的云端管理员读取")
    from backend.modules.chat import weixin_clawbot
    try:
        return await weixin_clawbot.login_status(**({"login_id": login_id} if login_id else {}))
    except Exception as exc:
        return {"connected": False, "error": f"登录状态查询失败：{type(exc).__name__}: {exc}"}


@app.post("/api/weixin/config")
async def api_weixin_config(req: WeixinConfigRequest, request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="微信连接设置需要本机或已登录的云端管理员修改")
    user_ids = [item.strip() for item in req.allowed_user_ids if item.strip()]
    if len(user_ids) > 100 or any(len(item) > 256 for item in user_ids):
        return {"ok": False, "error": "白名单最多 100 个用户 ID，单个 ID 不能超过 256 字符"}
    from backend.modules.chat import weixin_clawbot
    return {"ok": True, **weixin_clawbot.configure(user_ids)}


@app.post("/api/weixin/accounts/{account_id}/remark")
async def api_weixin_account_remark(account_id: str, req: AccountRemarkRequest, request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="微信账号备注需要本机或已登录的云端管理员修改")
    from backend.modules.chat import weixin_clawbot
    try:
        return {"ok": True, **weixin_clawbot.set_account_remark(account_id, req.remark)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/cloud/weixin/accounts/{account_id}/remark")
async def api_cloud_weixin_account_remark(account_id: str, req: AccountRemarkRequest, request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="云端微信账号备注只能在本机修改")
    try:
        from backend.core import cloud_deploy as cloud_deployer
        await asyncio.to_thread(
            cloud_deployer.update_weixin, app_home(),
            {"account_id": account_id, "remark": req.remark}, restart=False,
        )
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _logout_weixin() -> dict:
    """Clear the local and cloud WeChat binding."""
    from backend.modules.chat import weixin_clawbot

    local_ok = True
    try:
        local = await weixin_clawbot.logout()
    except Exception as exc:
        local_ok = False
        local = {"connected": False, "ilink_user_id": "", "allowed_user_ids": [], "error": str(exc)}
    cloud = {"configured": False, "ok": True, "connected": False, "account_id": ""}
    target_path = app_home() / "data" / "cloud-ssh" / "target.json"
    if target_path.is_file():
        cloud["configured"] = True
        try:
            from backend.core import cloud_deploy as cloud_deployer
            await asyncio.to_thread(cloud_deployer.update_weixin, app_home(), {"logout": True})
            remote = await asyncio.to_thread(cloud_deployer.get_weixin_status, app_home())
            if remote.get("connected") or remote.get("account_id") or remote.get("allowed_user_ids"):
                raise RuntimeError("云端微信状态复查仍显示已连接")
            cloud.update({"connected": False, "account_id": "", "allowed_user_ids": []})
        except Exception as exc:
            cloud.update({"ok": False, "error": str(exc)})

    ok = local_ok and bool(cloud.get("ok"))
    return {
        "ok": ok,
        "local": local,
        "cloud": cloud,
        "message": "本机和云端微信已解绑" if ok else "微信解绑未完全成功，请重试",
    }


@app.delete("/api/weixin/account")
async def api_weixin_logout(request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="微信连接设置需要本机或已登录的云端管理员修改")
    return await _logout_weixin()


def _revoke_weixin_account(account_id: str) -> None:
    for key, active in tuple(CHANNEL_ACTIVE_TASKS.items()):
        if key[0] == "weixin" and len(key) == 3 and key[1] == account_id:
            active["stop"] = active["control_revoked"] = True
    for key, pending in tuple(CHANNEL_CONFIRM.items()):
        if key[0] == "weixin" and len(key) == 3 and key[1] == account_id and not pending.done():
            pending.set_result(False)
    for mapping in (CHANNEL_HISTORY, CHANNEL_RESUME, CHANNEL_LAST_USED, CHANNEL_SHARED_CONTEXT):
        for key in tuple(mapping):
            if key[0] == "weixin" and len(key) == 3 and key[1] == account_id:
                mapping.pop(key, None)


class SharedMemoryCommandRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=256)
    user_id: str = Field(default="", max_length=256)
    sender_id: str = Field(min_length=1, max_length=256)
    text: str = Field(max_length=8100)


@app.post("/api/weixin/memory/shared")
async def api_weixin_shared_memory(req: SharedMemoryCommandRequest, request: Request) -> dict:
    # The public login cookie cannot impersonate a worker/account here.
    if not CFG.role.is_hub or not CFG.role.hub_token or not _matches_secret(request.headers.get("x-hub-token"), CFG.role.hub_token):
        raise HTTPException(status_code=403, detail="仅允许已认证的本机 worker 转发微信记忆指令")
    from backend.modules.memory.shared_libraries import handle_command
    manager = _weixin_memories()
    accounts = {req.account_id: {"ilink_user_id": req.user_id}}
    # Worker imports may register identities, but never appoint the administrator.
    identity = manager.identity(req.account_id, accounts[req.account_id])
    with manager._db() as db:
        binding = db.execute("SELECT identity FROM bindings WHERE account=?", (req.account_id,)).fetchone()
    if not binding or binding[0] != identity:
        raise HTTPException(status_code=403, detail="微信账号记忆身份尚未同步，请等待同步后重试")
    actor = "user:" + req.sender_id if req.user_id else identity
    response = await asyncio.to_thread(handle_command, manager.shared, req.text, actor)
    return {"ok": True, "handled": response is not None, "reply": response}


class SharedMemoryContextRequest(SharedMemoryCommandRequest):
    library_id: str = Field(default="", max_length=32)
    operation: Literal["context", "action"] = "context"
    action: Literal["search", "list", "save", "update", "delete"] = "search"
    query: str = Field(default="", max_length=8100)
    content: str = Field(default="", max_length=8000)
    memory_id: int = Field(default=0, ge=0)


@app.post("/api/weixin/memory/context")
async def api_weixin_memory_context(req: SharedMemoryContextRequest, request: Request) -> dict:
    if not CFG.role.is_hub or not CFG.role.hub_token or not _matches_secret(request.headers.get("x-hub-token"), CFG.role.hub_token):
        raise HTTPException(status_code=403, detail="仅允许已认证的 worker 访问项目上下文")
    from backend.modules.memory import shared_libraries
    manager = _weixin_memories()
    identity = manager.identity(req.account_id, {"ilink_user_id": req.user_id})
    with manager._db() as db:
        binding = db.execute("SELECT identity FROM bindings WHERE account=?", (req.account_id,)).fetchone()
    if not binding or binding[0] != identity:
        raise HTTPException(status_code=403, detail="微信记忆身份尚未同步或已变更")
    # Sender identity is supplied by the authenticated channel worker, never the model.
    actor = "user:" + req.sender_id if req.user_id else identity
    try:
        if req.operation == "action":
            reply = await asyncio.to_thread(shared_libraries.manage_context, manager.shared, req.library_id, actor,
                                           req.action, query=req.query, content=req.content, memory_id=req.memory_id)
            return {"ok": True, "reply": reply}
        context = await asyncio.to_thread(shared_libraries.context_for_message, manager.shared, req.text, actor)
        if context is None and req.library_id:
            context = await asyncio.to_thread(shared_libraries.context_for_library, manager.shared, req.library_id, actor, req.text)
        return {"ok": True, "context": context}
    except (ValueError, PermissionError, RuntimeError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


class SharedMemorySettingsRequest(BaseModel):
    call_name: str = Field(min_length=2, max_length=30)


class SharedMemoryDeleteRequest(BaseModel):
    confirmation: str = Field(min_length=2, max_length=30)


def _check_shared_settings_request(request: Request) -> None:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="共享库设置需要本机或已登录的云端管理员")
    origin = request.headers.get("origin", "")
    if origin and origin not in _ALLOWED_ORIGINS and origin != str(getattr(request, "base_url", "")).rstrip("/"):
        raise HTTPException(status_code=403, detail="共享库设置不接受跨站请求")


class SharedLibraryRequest(BaseModel):
    action: str = Field(pattern="^(save|delete)$")
    id: str = Field(default="", max_length=32)
    name: str = Field(default="", max_length=80)
    call_name: str = Field(default="", max_length=30)
    owner_account_id: str = Field(default="", max_length=256)
    reader_account_ids: list[str] = Field(default_factory=list, max_length=100)
    retained_identities: list[str] = Field(default_factory=list, max_length=100)
    confirmation: str = Field(default="", max_length=30)
    context_ttl_minutes: int | None = Field(default=None, ge=5, le=240)


class ProjectLibraryRequest(BaseModel):
    action: Literal["save", "delete"]
    id: str = Field(default="", max_length=32)
    name: str = Field(default="", max_length=80)
    call_name: str = Field(default="", max_length=30)
    owner_identity: str = Field(default="", max_length=1024)
    reader_identities: list[str] = Field(default_factory=list, max_length=200)
    retained_identities: list[str] = Field(default_factory=list, max_length=200)
    confirmation: str = Field(default="", max_length=30)
    context_ttl_minutes: int | None = Field(default=None, ge=5, le=240)


def _project_library_accounts() -> list[dict]:
    from backend.modules.chat import weixin_clawbot
    from backend.modules.memory import shared_libraries
    accounts = weixin_clawbot._accounts()
    manager = _weixin_memories()
    manager.register_accounts(accounts)
    return shared_libraries.authorized_accounts(accounts, {
        "app_id": CFG.feishu.app_id, "allowed_open_ids": CFG.feishu.allowed_open_ids,
        "remarks": CFG.feishu.remarks,
    })


@app.get("/api/memory/libraries")
async def api_project_libraries(request: Request) -> dict:
    _check_shared_settings_request(request)
    from backend.modules.memory import shared_libraries
    accounts = await asyncio.to_thread(_project_library_accounts)
    return {"ok": True, "capability": shared_libraries.CAPABILITY, "accounts": accounts,
            "libraries": await asyncio.to_thread(_weixin_memories().libraries)}


@app.get("/api/cloud/memory/libraries")
async def api_cloud_project_libraries(request: Request) -> dict:
    _check_shared_settings_request(request)
    from backend.core import cloud_deploy
    result = await asyncio.to_thread(cloud_deploy.get_project_libraries, app_home())
    _save_shared_route_names(result)
    return result


@app.post("/api/memory/libraries")
async def api_project_libraries_manage(req: ProjectLibraryRequest, request: Request) -> dict:
    _check_shared_settings_request(request)
    from backend.modules.memory import shared_libraries
    try:
        accounts = await asyncio.to_thread(_project_library_accounts)
        result = await asyncio.to_thread(shared_libraries.manage_accounts, _weixin_memories().shared, accounts, req.model_dump())
        global _SHARED_ROUTE_NAMES
        _SHARED_ROUTE_NAMES = None
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/cloud/memory/libraries")
async def api_cloud_project_libraries_manage(req: ProjectLibraryRequest, request: Request) -> dict:
    _check_shared_settings_request(request)
    from backend.core import cloud_deploy
    async with BACKUP_LOCK:
        await asyncio.to_thread(cloud_deploy.update_weixin, app_home(), {"project_library": req.model_dump()}, restart=False)
    # The write has succeeded: retain its invocation name even if the subsequent
    # metadata refresh loses connectivity. This cache never grants permission.
    if req.action == "save":
        _save_shared_route_names({"ok": True, "libraries": [{"call_name": req.call_name}]}, complete=False)
    await _refresh_shared_route_names_async()
    return {"ok": True}


@app.post("/api/weixin/memory/libraries")
async def api_weixin_libraries(req: SharedLibraryRequest, request: Request) -> dict:
    _check_shared_settings_request(request)
    from backend.modules.chat import weixin_clawbot
    try:
        return await asyncio.to_thread(_weixin_memories().manage_library, weixin_clawbot._accounts(), req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/cloud/weixin/memory/libraries")
async def api_cloud_weixin_libraries(req: SharedLibraryRequest, request: Request) -> dict:
    _check_shared_settings_request(request)
    from backend.core import cloud_deploy
    # Cloud is authoritative: no local shadow grants/data to fall back to.
    async with BACKUP_LOCK:
        await asyncio.to_thread(cloud_deploy.update_weixin, app_home(), {"shared_library": req.model_dump()}, restart=False)
    return {"ok": True}


@app.post("/api/memory/libraries/backup")
@app.post("/api/cloud/memory/libraries/backup")
@app.post("/api/weixin/memory/libraries/backup")
@app.post("/api/cloud/weixin/memory/libraries/backup")
async def api_weixin_libraries_backup(request: Request) -> dict:
    _check_shared_settings_request(request)
    from backend.modules.memory.shared_libraries import export_files
    async with BACKUP_LOCK:
        if request.url.path.startswith("/api/cloud/"):
            import base64
            from backend.core import cloud_deploy
            result = await asyncio.to_thread(cloud_deploy.get_shared_library_backup, app_home())
            if not result.get("ok") or not result.get("files"):
                raise HTTPException(status_code=502, detail="云端项目库备份未成功，未保存不完整备份")
            files = {name: base64.b64decode(content, validate=True) for name, content in result["files"].items()}
            if any(name not in {"shared-memory.sqlite3", "shared-libraries.sqlite3"}
                   and not re.fullmatch(r"shared-library-[0-9a-f]{32}\.sqlite3", name) for name in files):
                raise HTTPException(status_code=502, detail="云端项目库备份文件名无效")
        else:
            files = await asyncio.to_thread(export_files, _weixin_memories().shared)
        target = await asyncio.to_thread(_write_memory_backup, files, projects=True)
    return {"ok": True, "path": str(target), "files": len(files)}


@app.post("/api/weixin/memory/settings")
async def api_weixin_memory_settings(req: SharedMemorySettingsRequest, request: Request) -> dict:
    _check_shared_settings_request(request)
    from backend.modules.memory.shared_libraries import legacy_settings
    try:
        await asyncio.to_thread(legacy_settings, _weixin_memories().shared, name=req.call_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.delete("/api/weixin/memory/shared")
async def api_weixin_memory_delete(req: SharedMemoryDeleteRequest, request: Request) -> dict:
    _check_shared_settings_request(request)
    from backend.modules.memory.shared_libraries import legacy_settings
    try:
        await asyncio.to_thread(legacy_settings, _weixin_memories().shared, confirmation=req.confirmation)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/api/cloud/weixin/memory/settings")
async def api_cloud_weixin_memory_settings(req: SharedMemorySettingsRequest, request: Request) -> dict:
    _check_shared_settings_request(request)
    from backend.modules.memory.shared_memory import validate_call_name
    from backend.core import cloud_deploy
    try:
        name = validate_call_name(req.call_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    async with BACKUP_LOCK:
        await asyncio.to_thread(cloud_deploy.update_weixin, app_home(), {"shared_memory_call_name": name}, restart=False)
    return {"ok": True}


@app.delete("/api/cloud/weixin/memory/shared")
async def api_cloud_weixin_memory_delete(req: SharedMemoryDeleteRequest, request: Request) -> dict:
    _check_shared_settings_request(request)
    from backend.core import cloud_deploy
    async with BACKUP_LOCK:
        await asyncio.to_thread(cloud_deploy.update_weixin, app_home(), {"delete_shared_memory": req.confirmation}, restart=False)
    return {"ok": True}


class SharedMemoryOwnerRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=256)


@app.post("/api/weixin/memory/owner")
async def api_weixin_memory_owner(req: SharedMemoryOwnerRequest, request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="共享库管理员需要本机或已登录的云端管理员设置中更换")
    from backend.modules.chat import weixin_clawbot
    accounts = weixin_clawbot._accounts()
    if req.account_id not in accounts:
        raise HTTPException(status_code=400, detail="指定微信账号尚未连接")
    await asyncio.to_thread(_weixin_memories().transfer_owner, req.account_id, accounts)
    return {"ok": True}


@app.post("/api/cloud/weixin/memory/owner")
async def api_cloud_weixin_memory_owner(req: SharedMemoryOwnerRequest, request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="共享库管理员只能在本机设置中更换")
    from backend.core import cloud_deploy as cloud_deployer
    await asyncio.to_thread(cloud_deployer.update_weixin, app_home(),
                            {"memory_owner_account_id": req.account_id}, restart=False)
    return {"ok": True}


@app.delete("/api/weixin/accounts/{account_id}")
async def api_weixin_remove_account(account_id: str, request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="微信连接需要本机或已登录的云端管理员修改")
    from backend.modules.chat import weixin_clawbot
    try:
        _revoke_weixin_account(account_id)
        return {"ok": True, **await weixin_clawbot.logout(account_id)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.delete("/api/cloud/weixin/accounts/{account_id}")
async def api_cloud_weixin_remove_account(account_id: str, request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="云端设置只能在本机修改")
    try:
        from backend.core import cloud_deploy
        await asyncio.to_thread(cloud_deploy.update_weixin, app_home(), {"logout": True, "account_id": account_id})
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/cloud/weixin")
async def api_get_cloud_weixin(request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="云端设置只能在本机读取")
    try:
        from backend.core import cloud_deploy as cloud_deployer
        return await asyncio.to_thread(cloud_deployer.get_weixin_status, app_home())
    except Exception as exc:
        return {"connected": False, "account_id": "", "allowed_user_ids": [], "error": str(exc)}


@app.post("/api/cloud/weixin/login")
async def api_cloud_weixin_login(request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="微信扫码登录只能在本机发起")
    from backend.modules.chat import weixin_clawbot
    try:
        from backend.core import cloud_deploy as cloud_deployer
        qr_data = await asyncio.to_thread(cloud_deployer.get_weixin_qrcode, app_home())
        return {"ok": True, **await weixin_clawbot.begin_cloud_login(qr_data)}
    except Exception as exc:
        return {"ok": False, "error": f"云端登录二维码获取失败：{type(exc).__name__}: {exc}"}


@app.get("/api/cloud/weixin/login/status")
async def api_cloud_weixin_login_status(request: Request, login_id: str = "") -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="微信登录状态只能在本机读取")
    from backend.modules.chat import weixin_clawbot
    try:
        result = await weixin_clawbot.cloud_login_status(**({"login_id": login_id} if login_id else {}))
        credentials = result.pop("_credentials", None)
        if credentials:
            try:
                from backend.core import cloud_deploy as cloud_deployer
                current = await asyncio.to_thread(cloud_deployer.get_weixin_status, app_home())
                await asyncio.to_thread(
                    cloud_deployer.update_weixin, app_home(),
                    {**credentials, "allowed_user_ids": sorted(set(next((item.get("allowed_user_ids", []) for item in current.get("accounts", []) if item.get("ilink_user_id") == credentials.get("ilink_user_id")), current.get("allowed_user_ids", []) if not current.get("accounts") else [])) | ({credentials.get("ilink_user_id")} if credentials.get("ilink_user_id") else set()))},
                )
                weixin_clawbot.complete_cloud_login(**({"login_id": login_id} if login_id else {}))
                result["message"] = "微信已登录到云端，服务已重启"
            except Exception as exc:
                return {
                    "connected": False, "login_status": "sync_pending",
                    "error": f"微信已确认，但云端同步失败；登录凭据已保留，可再次点击扫码连接云端重试。{exc}",
                }
        return result
    except Exception as exc:
        return {"connected": False, "error": f"云端登录同步失败：{type(exc).__name__}: {exc}"}


@app.post("/api/cloud/weixin/config")
async def api_cloud_weixin_config(req: WeixinConfigRequest, request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="云端设置只能在本机修改")
    user_ids = sorted({item.strip() for item in req.allowed_user_ids if item.strip()})
    if len(user_ids) > 100 or any(len(item) > 256 for item in user_ids):
        return {"ok": False, "error": "白名单最多 100 个用户 ID，单个 ID 不能超过 256 字符"}
    try:
        from backend.core import cloud_deploy as cloud_deployer
        await asyncio.to_thread(
            cloud_deployer.update_weixin, app_home(), {"allowed_user_ids": user_ids},
        )
        return {"ok": True, "message": "云端微信白名单已更新并重启服务"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.delete("/api/cloud/weixin/account")
async def api_cloud_weixin_logout(request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="云端设置只能在本机修改")
    return await _logout_weixin()


@app.post("/api/cloud/runtime")
async def api_cloud_runtime(request: Request) -> dict:
    """同步云端实际需要的模型和腾讯语音配置；本机专属设置不会上传。"""
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="云端设置只能在本机修改")
    values = {
        **provider_runtime_env(CFG.llm.providers),
        "AGENT_LLM_PROVIDER_NAME": CFG.llm.provider_name,
        "AGENT_LLM_CONTEXT_WINDOWS": json.dumps(CFG.llm.context_windows),
        "AGENT_CONTEXT_WINDOW": str(CFG.agent.context_window),
        "AGENT_LLM_PROTOCOL": CFG.llm.protocol,
        "AGENT_LLM_BASE_URL": CFG.llm.base_url,
        "AGENT_LLM_API_KEY": CFG.llm.api_key,
        "AGENT_CHAT_MODEL": CFG.llm.chat_model,
        "AGENT_MODEL_OPTIONS": ",".join(CFG.llm.models),
        # Vision requests intentionally use the same first default model.
        "AGENT_TENCENT_SECRET_ID": CFG.voice.secret_id,
        "AGENT_TENCENT_SECRET_KEY": CFG.voice.secret_key,
        "AGENT_TENCENT_REGION": CFG.voice.region,
        "AGENT_TTS_VOICE": str(CFG.voice.tts_voice),
        "AGENT_TTS_SPEED": str(CFG.voice.tts_speed),
        "AGENT_SCHEDULE_TASKS": SCHEDULER.sync_payload(),
    }
    try:
        from backend.core import cloud_deploy as cloud_deployer
        await asyncio.to_thread(cloud_deployer.sync_runtime_settings, app_home(), values)
        return {"ok": True, "message": "模型和语音设置已同步到云端"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/hub/parse")
async def api_parse_hub_connection(req: ConnectionStringRequest, request: Request) -> dict:
    if not _is_local(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="连接串只能在本机解析")
    try:
        return {"ok": True, "connection": masked_preview(parse_connection_string(req.connection_string))}
    except ValueError as exc:
        return {"ok": False, "error_code": "parse", "error": str(exc)}


@app.post("/api/models/discover")
async def api_discover_models(req: ModelDiscoveryRequest) -> dict:
    base_url = req.base_url.strip()
    if not base_url:
        return {"ok": False, "error": "请先填写 Base URL"}

    if not req.provider_id or req.provider_id == "default":
        stored_key = CFG.llm.api_key
    else:
        provider = next((item for item in CFG.llm.providers if item.id == req.provider_id), None)
        stored_key = provider.api_key if provider else ""
    api_key = req.api_key.strip()
    if not _changed(api_key, stored_key):
        api_key = stored_key

    client = LLMClient(LLMConfig(
        base_url=base_url,
        api_key=api_key,
        protocol=req.protocol if req.protocol in {"openai", "anthropic", "responses"} else "openai",
        chat_model="",
        vision_model="",
        models=[],
        timeout=min(max(CFG.llm.timeout, 1), 15),
    ))
    try:
        models = await asyncio.wait_for(client.list_models(), timeout=20)
        return {"ok": True, "models": models[:500]}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "20 秒内没有响应，请检查地址、API Key 或网络"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        await client.aclose()


@app.post("/api/models/test")
async def api_test_model(req: ModelTestRequest) -> dict:
    base_url = req.base_url.strip()
    model = req.model.strip()
    if not base_url or not model:
        return {"ok": False, "error": "请先填写 Base URL 和模型名称"}

    if not req.provider_id or req.provider_id == "default":
        stored_key = CFG.llm.api_key
    else:
        provider = next((item for item in CFG.llm.providers if item.id == req.provider_id), None)
        stored_key = provider.api_key if provider else ""
    api_key = req.api_key.strip()
    if not _changed(api_key, stored_key):
        api_key = stored_key

    client = LLMClient(LLMConfig(
        base_url=base_url,
        api_key=api_key,
        protocol=req.protocol if req.protocol in {"openai", "anthropic", "responses"} else "openai",
        chat_model=model,
            vision_model=model,
        models=[model],
        timeout=min(max(CFG.llm.timeout, 1), 15),
    ))
    started = time.perf_counter()
    try:
        message = await asyncio.wait_for(client.chat(
            [{"role": "user", "content": "只回复 OK"}], model=model, temperature=0,
        ), timeout=20)
        return {
            "ok": True,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "reply": str(message.get("content") or "").strip()[:120],
        }
    except asyncio.TimeoutError:
        return {"ok": False, "error": "20 秒内没有响应，请检查地址、模型名或网络"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        await client.aclose()


@app.post("/api/settings")
async def api_save_settings(req: SettingsRequest) -> dict:
    global LLM
    previous_config = copy.deepcopy(CFG)
    config_saved = False
    feishu_credentials_changed = False
    old_web_password = CFG.auth.password
    old_role = (
        CFG.role.mode, CFG.role.hub_url, CFG.role.hub_token,
        CFG.role.worker_id, CFG.role.tls_fingerprint,
    )
    ignored_env = _ignored_env_setting_changes(req)
    try:
        if any(char in req.web_password for char in "\r\n\0"):
            raise ValueError("网页密码包含非法字符")
        role_supplied = any(value is not None for value in (
            req.role_mode, req.hub_url, req.hub_token, req.tls_fingerprint,
            req.connection_string, req.worker_id,
        ))
        next_role_mode = (req.role_mode or CFG.role.mode).strip()
        next_hub_url = (req.hub_url if req.hub_url is not None else CFG.role.hub_url).strip()
        next_hub_token = (
            req.hub_token.strip() if req.hub_token is not None else CFG.role.hub_token
        )
        if not _changed(next_hub_token, CFG.role.hub_token):
            next_hub_token = CFG.role.hub_token
        next_worker_id = (
            req.worker_id if req.worker_id is not None else CFG.role.worker_id
        ).strip()
        next_fingerprint = (
            req.tls_fingerprint if req.tls_fingerprint is not None else CFG.role.tls_fingerprint
        ).strip().replace(":", "").lower()
        if req.connection_string and req.connection_string.strip():
            details = parse_connection_string(req.connection_string)
            next_role_mode = "worker"
            next_hub_url = details.hub_url
            next_fingerprint = details.fingerprint
            next_hub_token = details.token
        if role_supplied:
            _validate_worker_connection(
                next_role_mode, next_hub_url, next_hub_token, next_worker_id, next_fingerprint,
            )

        if req.providers is not None:
            old_custom = {provider.id: provider for provider in CFG.llm.providers}
            new_custom: list[LLMProviderConfig] = []
            seen_ids: set[str] = set()
            default_seen = False
            for index, item in enumerate(req.providers):
                if item.protocol not in {"openai", "anthropic", "responses"}:
                    raise ValueError("接口类型只支持 OpenAI Chat Completions、OpenAI Responses 或 Anthropic")
                provider_id = item.id.strip() or f"provider-{index + 1}"
                models = list(dict.fromkeys(model.strip() for model in item.models if model.strip()))
                if not item.base_url.strip() or not models:
                    raise ValueError("每个模型厂商都必须填写 Base URL 和至少一个模型")
                if provider_id == "default":
                    default_seen = True
                    CFG.llm.provider_name = item.name.strip() or "默认接口"
                    CFG.llm.base_url = item.base_url.strip()
                    CFG.llm.protocol = item.protocol
                    if _changed(item.api_key, CFG.llm.api_key):
                        CFG.llm.api_key = item.api_key.strip()
                    CFG.llm.models = models
                    CFG.llm.context_windows = _validated_context_windows(
                        item.context_windows, models, "默认接口"
                    ) or {
                        model: CFG.llm.context_windows[model]
                        for model in models if model in CFG.llm.context_windows
                    }
                    CFG.llm.chat_model = models[0]
                    CFG.llm.vision_model = CFG.llm.chat_model
                    continue
                if provider_id in seen_ids:
                    raise ValueError("模型厂商 ID 重复，请删除后重新添加")
                seen_ids.add(provider_id)
                old_provider = old_custom.get(provider_id)
                api_key = item.api_key.strip()
                if old_provider and not _changed(api_key, old_provider.api_key):
                    api_key = old_provider.api_key
                new_custom.append(LLMProviderConfig(
                    id=provider_id,
                    name=item.name.strip() or f"接口 {index + 1}",
                    base_url=item.base_url.strip(),
                    api_key=api_key,
                    models=models,
                    context_windows=(
                        _validated_context_windows(item.context_windows, models, provider_id)
                        or {model: old_provider.context_windows[model] for model in models
                            if old_provider and model in old_provider.context_windows}
                    ),
                    protocol=item.protocol,
                ))
            if not default_seen:
                raise ValueError("默认接口不能删除")
            CFG.llm.providers = new_custom

        if req.context_window is not None:
            if not _MIN_CONTEXT_WINDOW <= req.context_window <= _MAX_CONTEXT_WINDOW:
                raise ValueError("上下文窗口必须在 4096 到 2000000 之间")
            CFG.agent.context_window = req.context_window

        _apply_memory_backup_settings(
            req.memory_backup_dir,
            req.memory_backup_interval_minutes,
            req.memory_backup_keep_count,
        )

        if req.base_url:
            CFG.llm.base_url = req.base_url.strip()
        if req.chat_model:
            CFG.llm.chat_model = req.chat_model.strip()
        if req.models:
            CFG.llm.models = list(dict.fromkeys(
                model.strip() for model in req.models.split(",") if model.strip()
            ))
        CFG.llm.models = list(dict.fromkeys(
            model for model in [CFG.llm.chat_model, *CFG.llm.models] if model
        ))
        CFG.llm.vision_model = CFG.llm.chat_model
        if _changed(req.api_key, CFG.llm.api_key):
            CFG.llm.api_key = req.api_key.strip()

        if req.tc_region:
            CFG.voice.region = req.tc_region.strip()
        if _changed(req.tc_secret_id, CFG.voice.secret_id):
            CFG.voice.secret_id = _normalize_tencent_credential(req.tc_secret_id, "SecretId")
        if _changed(req.tc_secret_key, CFG.voice.secret_key):
            CFG.voice.secret_key = _normalize_tencent_credential(req.tc_secret_key, "SecretKey")
        # 有密钥就自动启用腾讯云
        if CFG.voice.secret_id and CFG.voice.secret_key and not CFG.voice.provider:
            CFG.voice.provider = "tencent"
        if req.tts_voice is not None:
            CFG.voice.tts_voice = req.tts_voice

        # 飞书
        old_feishu_app_id = CFG.feishu.app_id
        old_feishu_secret = CFG.feishu.app_secret
        if req.feishu_app_id:
            CFG.feishu.app_id = req.feishu_app_id.strip()
        if _changed(req.feishu_app_secret, CFG.feishu.app_secret):
            next_secret = req.feishu_app_secret.strip()
            if os.name == "nt" and not os.getenv("AGENT_FEISHU_APP_SECRET"):
                from backend.core import vault
                vault.put("feishu_app_secret", next_secret)
            CFG.feishu.app_secret = next_secret
        if req.feishu_allowed_open_ids is not None:
            CFG.feishu.allowed_open_ids = [
                x.strip() for x in req.feishu_allowed_open_ids.split(",") if x.strip()
            ]
        if role_supplied:
            CFG.role.mode = next_role_mode
            CFG.role.hub_url = next_hub_url
            CFG.role.hub_token = next_hub_token
            CFG.role.worker_id = next_worker_id
            CFG.role.tls_fingerprint = next_fingerprint

        # 网页访问密码:留空=不改;填 "-" =清空(关闭远程网页访问)
        if req.web_password:
            new_pw = "" if req.web_password.strip() == "-" else req.web_password.strip()
            if new_pw != CFG.auth.password:
                CFG.auth.password = new_pw
            if not os.getenv("AGENT_WEB_PASSWORD"):
                CFG.auth.password_env_managed = False
        if req.open_at_login is not None:
            CFG.agent.open_at_login = req.open_at_login
        if req.isolation_max_mb is not None:
            CFG.agent.isolation_max_mb = isolation_workspace.normalize_max_mb(req.isolation_max_mb)

        # 浏览器保存的模型设置优先于部署默认值；环境变量托管的密钥始终优先。
        # 必须在重建客户端、重启监听器和清会话之前重应用并确认落盘成功。
        if CFG.role.is_hub and any(value is not None and value != "" for value in (
            req.providers, req.base_url, req.chat_model, req.models, req.context_window,
        )):
            CFG.llm.settings_saved = True
        apply_env_overrides(CFG)
        # Persist before replacing clients, disconnecting sessions or restarting listeners.
        _rewrite_config()
        config_saved = True
        feishu_credentials_changed = (
            CFG.feishu.app_id != old_feishu_app_id
            or CFG.feishu.app_secret != old_feishu_secret
        )
        if CFG.auth.password != old_web_password:
            _SESSIONS.clear()  # 最终有效密码改变后，已登录会话全部失效。
            for ws in list(ACTIVE_WS):
                if not _is_local(ws.client.host if ws.client else None):
                    try:
                        await ws.close(code=4001)
                    except Exception:
                        pass

        # 热重载 LLM 客户端(新连接即用新配置)
        old = LLM
        LLM = runtime_llm(CFG.llm, hub=CFG.role.is_hub)
        await _retire_llm(old)

        if feishu_credentials_changed:
            from backend.modules.chat import feishu_module
            await asyncio.to_thread(feishu_module.stop_listener)
            feishu_module.reset_client()
            if CFG.feishu.enabled:
                await asyncio.to_thread(_start_chat_listeners, asyncio.get_running_loop())

        await asyncio.to_thread(isolation_workspace.cleanup, CFG.agent.isolation_max_mb)
        result = {"ok": True}
        if old_role != (
            CFG.role.mode, CFG.role.hub_url, CFG.role.hub_token,
            CFG.role.worker_id, CFG.role.tls_fingerprint,
        ):
            result["restart_required"] = True
        if ignored_env:
            env_names = ", ".join(dict.fromkeys(item["env"] for item in ignored_env))
            result.update({
                "ignored_env": ignored_env,
                "warning": f"部分密钥修改未生效，由环境变量接管：{env_names}",
            })
        if (req.web_password and CFG.role.is_worker
                and not any(item["field"] == "auth.password" for item in ignored_env)):
            try:
                from backend.core import cloud_deploy
                await asyncio.to_thread(cloud_deploy.sync_web_password, app_home(), CFG.auth.password)
                result["cloud_synced"] = True
            except Exception:
                result["cloud_warning"] = "云端网页密码是否生效尚未确认，请检查服务器连接后重新填写密码并保存。"
        return result
    except Exception as e:
        if not config_saved:
            # Existing clients/modules may retain a reference to a config section.
            for section, previous in vars(previous_config).items():
                current = getattr(CFG, section)
                current.__dict__.clear()
                current.__dict__.update(vars(previous))
        return {"ok": False, "error": str(e)}


@app.post("/api/settings/{section}")
async def api_save_settings_section(section: str, req: SettingsRequest) -> dict:
    """Save one settings page without allowing it to overwrite another page."""
    allowed = _SETTING_SECTION_FIELDS.get(section)
    if allowed is None:
        raise HTTPException(status_code=404, detail="设置分区不存在")
    supplied = set(req.model_dump(exclude_unset=True) if hasattr(req, "model_dump")
                   else req.dict(exclude_unset=True))
    unexpected = sorted(supplied - allowed)
    if unexpected:
        raise HTTPException(status_code=400, detail="该页面不能修改: " + ", ".join(unexpected))
    result = await api_save_settings(req)
    if result.get("ok"):
        if section == "models" and CFG.role.is_hub and HUB_BRIDGE is not None:
            try:
                await HUB_BRIDGE.sync_model_order()
            except Exception:
                # Reconnection carries the same saved order in the welcome snapshot.
                pass
        try:
            await broadcast({"type": "settings_changed", "data": {"scope": section}})
        except Exception:
            # Settings are already persisted; live refresh is best-effort only.
            pass
    return result


_TOOL_GROUPS = [
    ("computer", "电脑与窗口", lambda n: n in {
        "screen_capture", "screen_capture_region", "inspect_ui_controls", "click_element", "click_coordinate", "triple_click",
        "move_pointer", "scroll_screen", "drag_pointer", "type_text", "press_hotkey",
        "wait_for_screen_change", "send_text_to_window", "list_windows", "focus_window",
        "launch_app", "remember_app", "open_url",
    }),
    ("browser", "浏览器专用控制", lambda n: n.startswith("browser_")),
    ("code", "代码工作台", lambda n: n.startswith("code_")),
    ("office", "文件与 Office", lambda n: n.startswith(("fs_", "excel_", "word_", "create_"))),
    ("memory", "记忆与项目", lambda n: n.startswith(("memory_", "project_"))),
    ("messages", "消息与发送", lambda n: n in {
        "send_file_to_chat", "send_image_to_chat",
    }),
    ("extensions", "能力与扩展", lambda n: n.startswith(("tool_", "extension_"))),
    ("system", "系统命令", lambda n: n == "run_command"),
]


@app.get("/api/tools/catalog")
async def api_tools_catalog() -> dict:
    groups = {key: {"id": key, "label": label, "tools": []}
              for key, label, _ in _TOOL_GROUPS}
    groups["other"] = {"id": "other", "label": "其他能力", "tools": []}
    for name in REGISTRY.names():
        tool = REGISTRY.get(name)
        if tool is None:
            continue
        group_id = next((key for key, _, matches in _TOOL_GROUPS if matches(name)), "other")
        groups[group_id]["tools"].append({
            "name": name,
            "description": tool.description,
            "side_effect": tool.side_effect,
            "danger": tool.danger,
        })
    items = [group for group in groups.values() if group["tools"]]
    return {"ok": True, "total": sum(len(group["tools"]) for group in items), "groups": items}


@app.get("/api/audit")
async def api_audit(
    page: int = 1, page_size: int = 50, session_id: str = "", task_id: str = "",
    tool_name: str = "", category: str = "", status: str = "",
    created_from: str = "", created_to: str = "",
) -> dict:
    return await asyncio.to_thread(
        AUDIT_LOG.query,
        page=page, page_size=page_size, session_id=session_id, task_id=task_id,
        tool_name=tool_name, category=category, status=status,
        created_from=created_from, created_to=created_to,
    )


@app.get("/api/audit/export")
async def api_audit_export(
    limit: int = 10_000, session_id: str = "", task_id: str = "",
    tool_name: str = "", category: str = "", status: str = "",
    created_from: str = "", created_to: str = "",
) -> dict:
    return await asyncio.to_thread(
        AUDIT_LOG.export,
        limit=limit, session_id=session_id, task_id=task_id, tool_name=tool_name,
        category=category, status=status, created_from=created_from, created_to=created_to,
    )


@app.post("/api/audit/cleanup")
async def api_audit_cleanup(payload: dict | None = None) -> dict:
    payload = payload or {}
    return await asyncio.to_thread(
        AUDIT_LOG.cleanup,
        retention_days=payload.get("retention_days"), max_rows=payload.get("max_rows"),
    )


@app.get("/api/extensions")
async def api_extensions(request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="扩展需要本机或已登录的云端管理员管理")
    if EXTENSION_MANAGER is None:
        return {"tools": [], "skills": [], "plugins": [], "available": False}
    try:
        return {
            **await asyncio.to_thread(EXTENSION_MANAGER.list_extensions),
            "available": True,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/extensions/inspect")
async def api_extension_inspect(req: ExtensionInstallRequest, request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="扩展需要本机或已登录的云端管理员管理")
    if EXTENSION_MANAGER is None:
        return {"ok": False, "error": "当前角色不能在本机检查扩展。"}
    try:
        return await asyncio.to_thread(EXTENSION_MANAGER.inspect, req.url, req.kind)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/extensions/install")
async def api_extension_install(req: ExtensionInstallRequest, request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="扩展需要本机或已登录的云端管理员管理")
    if not req.confirm_install:
        raise HTTPException(status_code=400, detail="安装第三方扩展前必须先审查并明确确认")
    if EXTENSION_MANAGER is None:
        return {"ok": False, "error": "当前角色不能在本机安装扩展。"}
    try:
        return await asyncio.to_thread(EXTENSION_MANAGER.install, req.url, req.kind)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/extensions/enable")
async def api_extension_enable(req: ExtensionEnableRequest, request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="扩展需要本机或已登录的云端管理员管理")
    if EXTENSION_MANAGER is None:
        return {"ok": False, "error": "当前角色不能管理本机扩展。"}
    try:
        return await asyncio.to_thread(
            EXTENSION_MANAGER.enable, req.kind, req.name, req.enabled,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/extensions/rollback")
async def api_extension_rollback(req: ExtensionRollbackRequest, request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="扩展需要本机或已登录的云端管理员管理")
    if EXTENSION_MANAGER is None:
        return {"ok": False, "error": "当前角色不能管理本机扩展。"}
    try:
        return await asyncio.to_thread(EXTENSION_MANAGER.rollback, req.kind, req.name)
    except Exception as e:
        return {"ok": False, "error": str(e)}


class ASRRequest(BaseModel):
    audio: str  # base64
    format: str = "wav"


class TTSRequest(BaseModel):
    text: str
    voice: int | str | None = None


@app.post("/api/asr")
async def api_asr(req: ASRRequest) -> dict:
    """语音识别:前端传 base64 音频,返回文字。"""
    if not CFG.voice.asr_enabled:
        return {"ok": False, "error": "未配置云语音(识别)"}
    try:
        import base64
        text = await transcribe(CFG.voice, base64.b64decode(req.audio), fmt=req.format)
        return {"ok": True, "text": text}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/tts")
async def api_tts(req: TTSRequest) -> dict:
    """语音合成:前端传文字,返回 base64 的 mp3。"""
    if not CFG.voice.tts_enabled:
        return {"ok": False, "error": "未配置云语音(合成)"}
    try:
        audio = await synthesize(CFG.voice, req.text, voice=req.voice)
        return {"ok": True, "audio": base64.b64encode(audio).decode()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/tts/preview")
async def api_tts_preview(voice: str = ""):
    """Return preview audio as a real media response so browsers may play it from one click."""
    from fastapi.responses import Response

    if not CFG.voice.tts_enabled:
        raise HTTPException(status_code=400, detail="未配置云语音")
    try:
        try:
            voice_value = int(voice)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="\u97f3\u8272\u7f16\u53f7\u4e0d\u6b63\u786e")
        if not 1 <= voice_value <= 999999:
            raise HTTPException(status_code=422, detail="\u97f3\u8272\u7f16\u53f7\u4e0d\u6b63\u786e")
        audio = await synthesize(
            CFG.voice, "你好，我是小A。这个 PPT 和 Excel 我来帮你搞定。", voice=voice_value,
        )
        if len(audio) < 4 or audio[0] != 0xFF or audio[1] & 0xE0 != 0xE0:
            raise ValueError("语音服务没有返回有效的 MP3")
        return Response(
            content=audio,
            media_type="audio/mpeg",
            headers={"Cache-Control": "no-store"},
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.get("/voices")
async def voices_page():
    from fastapi.responses import FileResponse
    html = _frontend_file("voices.html")
    return FileResponse(str(html))


class SetVoiceRequest(BaseModel):
    voice: int


def _persist_voice(voice: int) -> None:
    """把选定音色写回 config.toml 的 tts_voice 行,重启后仍生效。"""
    _rewrite_config()


@app.post("/api/set_voice")
async def api_set_voice(req: SetVoiceRequest) -> dict:
    """把某个音色设为小A的默认声音(即时生效 + 写回配置)。"""
    CFG.voice.tts_voice = req.voice
    try:
        _persist_voice(req.voice)
        return {"ok": True, "voice": req.voice}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/current_voice")
async def api_current_voice() -> dict:
    return {
        "voice": CFG.voice.tts_voice,
        "speed": CFG.voice.tts_speed,
        "tts_enabled": CFG.voice.tts_enabled,
    }


class ScheduleTaskRequest(BaseModel):
    id: str = ""
    name: str = ""
    kind: str = "remind"
    schedule: str = "daily"
    time: str = "09:00"
    days: list[int] = Field(default_factory=list)
    datetime: str = ""
    timezone: str = ""
    location: str = "local"
    channel: str = "feishu"
    channel_target: str = Field(default="", max_length=256)
    channel_account_id: str = Field(default="", max_length=256)
    channel_receive_id_type: str = "open_id"
    channel_user_id: str = Field(default="", max_length=256)
    text: str = ""
    enabled: bool = True


class ScheduleEnableRequest(BaseModel):
    id: str = ""
    enabled: bool = True


def _sync_schedule_to_cloud(force: bool = False) -> str | None:
    """任务表里有 location=cloud 的任务时,把 AGENT_SCHEDULE_TASKS 推给云端并重启。

    返回 None 表示无需推送或推送成功;否则返回给人看的警告字符串。
    绝不抛异常——本地任务已经保存成功,推送失败只作为 cloud_warning 返回。
    force=True 用于「删除了最后一个云端任务」的场景:表里可能已无 cloud 任务,
    但仍要推一次把云端残留的那条清掉。
    """
    if CFG.role.is_hub:
        return None  # 已在目标云端持久化，无需加载桌面端 SSH 组件。
    from backend.core import cloud_deploy as cloud_deployer
    from backend.core.config import app_home

    tasks = SCHEDULER.list_tasks()
    has_cloud = any(task.get("location") == "cloud" for task in tasks)
    if not has_cloud and not force:
        return None  # 没有云端任务,无需推送
    target_path = app_home() / "data" / "cloud-ssh" / "target.json"
    if not target_path.is_file():
        return "尚未连接云端，云端任务不会执行（请先完成一次云端部署）" if has_cloud else None
    try:
        cloud_deployer.sync_runtime_settings(
            app_home(), {"AGENT_SCHEDULE_TASKS": SCHEDULER.sync_payload()},
        )
        return None
    except Exception as exc:
        return f"同步到云端失败：{exc}"


@app.get("/api/schedule")
async def api_schedule_list(request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="定时任务需要本机或已登录的云端管理员管理")
    return {"ok": True, "tasks": SCHEDULER.list_tasks()}


@app.get("/api/schedule/recipients")
async def api_schedule_recipients(request: Request, channel: Literal["weixin", "feishu"] = "weixin") -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="定时任务接收用户需要本机或已登录的云端管理员读取")
    try:
        if _automatic_schedule_location() == "cloud" and not CFG.role.is_hub:
            from backend.core import cloud_deploy
            reader = cloud_deploy.get_weixin_status if channel == "weixin" else cloud_deploy.get_feishu_status
            state = await asyncio.to_thread(reader, app_home())
        elif channel == "weixin":
            from backend.modules.chat import weixin_clawbot
            state = weixin_clawbot.status()
        else:
            state = {"allowed_open_ids": CFG.feishu.allowed_open_ids, "remarks": getattr(CFG.feishu, "remarks", {})}
        if channel == "weixin":
            recipients = [{"target": user_id, "account_id": account["account_id"],
                           "receive_id_type": "chat_id", "user_id": user_id, "connected": account["connected"],
                           "number": f"{index + 1}" + (f".{user_index + 1}" if len(account.get("allowed_user_ids", [])) > 1 else ""),
                           "remark": account.get("remark", "")}
                          for index, account in enumerate(state.get("accounts", []))
                          for user_index, user_id in enumerate(account.get("allowed_user_ids", []))]
        else:
            recipients = [{"target": user_id, "account_id": "", "receive_id_type": "open_id", "user_id": user_id, "connected": True,
                           "number": str(index + 1), "remark": state.get("remarks", {}).get(user_id, "")}
                          for index, user_id in enumerate(state.get("allowed_open_ids", []))]
        return {"ok": True, "recipients": recipients}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "recipients": []}


@app.post("/api/schedule")
async def api_schedule_add(req: ScheduleTaskRequest, request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="定时任务需要本机或已登录的云端管理员管理")
    try:
        task_data = req.model_dump(exclude={"id", "location"})
        task_data["location"] = _automatic_schedule_location()
        task = SCHEDULER.add_task(**task_data)
        warning = await asyncio.to_thread(_sync_schedule_to_cloud)
        result = {"ok": True, "task": task}
        if warning:
            result["cloud_warning"] = warning
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.put("/api/schedule/{task_id}")
async def api_schedule_update(task_id: str, req: ScheduleTaskRequest, request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="定时任务需要本机或已登录的云端管理员管理")
    try:
        task_data = req.model_dump(exclude={"id", "location"})
        task_data["location"] = _automatic_schedule_location()
        previous = next((task for task in SCHEDULER.list_tasks() if task["id"] == task_id), {})
        if previous.get("channel") != task_data.get("channel") and "channel_target" not in req.model_fields_set:
            task_data.update(channel_target="", channel_account_id="", channel_receive_id_type="open_id", channel_user_id="")
        for field in ("channel_target", "channel_account_id", "channel_receive_id_type", "channel_user_id"):
            if field not in req.model_fields_set and previous.get("channel") == task_data.get("channel"):
                task_data.pop(field, None)
        task = SCHEDULER.update_task(task_id, **task_data)
        if task is None:
            return {"ok": False, "error": "任务不存在"}
        warning = await asyncio.to_thread(_sync_schedule_to_cloud)
        result = {"ok": True, "task": task}
        if warning:
            result["cloud_warning"] = warning
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.delete("/api/schedule/{task_id}")
async def api_schedule_remove(task_id: str, request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="定时任务需要本机或已登录的云端管理员管理")
    deleted_was_cloud = any(
        task["id"] == task_id and task.get("location") == "cloud"
        for task in SCHEDULER.list_tasks()
    )
    ok = SCHEDULER.remove_task(task_id)
    warning = await asyncio.to_thread(_sync_schedule_to_cloud, deleted_was_cloud) if ok else None
    result = {"ok": ok, "error": "" if ok else "任务不存在"}
    if warning:
        result["cloud_warning"] = warning
    return result


@app.post("/api/schedule/enable")
async def api_schedule_enable(req: ScheduleEnableRequest, request: Request) -> dict:
    if not _can_manage_runtime(request):
        raise HTTPException(status_code=403, detail="定时任务需要本机或已登录的云端管理员管理")
    ok = SCHEDULER.set_enabled(req.id, req.enabled)
    warning = await asyncio.to_thread(_sync_schedule_to_cloud) if ok else None
    result = {"ok": ok, "error": "" if ok else "任务不存在"}
    if warning:
        result["cloud_warning"] = warning
    return result


@app.get("/favicon.ico")
async def favicon():
    """浏览器/Electron 会自动来要网站图标。没有的话日志里会刷红色 404,
    这里返回 204(无内容)让它安静,不是错误。"""
    from fastapi.responses import Response
    return Response(status_code=204)


@app.get("/")
async def index():
    """直接托管前端页面,浏览器打开 http://127.0.0.1:1985/ 即可使用,无需 Electron。"""
    from fastapi.responses import FileResponse

    html = _frontend_file("index.html")
    return FileResponse(str(html))


@app.get("/ui-language.js")
async def ui_language_script():
    return FileResponse(str(_frontend_file("ui-language.js")), media_type="text/javascript")


@app.get("/markdown-table.js")
async def markdown_table_script():
    return FileResponse(str(_frontend_file("markdown-table.js")), media_type="text/javascript")


# 活跃的前端连接,用于服务端主动推送(新消息通知等)
ACTIVE_WS: set[WebSocket] = set()

# 前端断线重连时会按 client_id 补发。进行中表防止同一连接重复入队，完成表让
# “服务端已经做完、回执恰好丢了”的消息不会再次执行。完成记录只保留一天。
_ACTIVE_CLIENT_INPUTS: dict[str, str] = {}
_COMPLETED_CLIENT_INPUTS: dict[str, dict[str, float]] = {}
_CLIENT_INPUT_TTL_SECONDS = 24 * 3600
_MAX_COMPLETED_CLIENT_INPUTS = 1000
_RESUME_STATES: dict[str, dict] = {}
_RESUME_AUTO_WINDOW_SECONDS = 2 * 3600
_RESUME_STATE_TTL_SECONDS = 7 * 24 * 3600
_MAX_RESUME_STATES = 32


def _prune_client_inputs(now: float | None = None) -> None:
    current = time.time() if now is None else now
    for client_id, info in list(_COMPLETED_CLIENT_INPUTS.items()):
        if current - float(info.get("completed_at", current)) > _CLIENT_INPUT_TTL_SECONDS:
            _COMPLETED_CLIENT_INPUTS.pop(client_id, None)
    overflow = len(_COMPLETED_CLIENT_INPUTS) - _MAX_COMPLETED_CLIENT_INPUTS
    if overflow > 0:
        oldest = sorted(
            _COMPLETED_CLIENT_INPUTS,
            key=lambda key: _COMPLETED_CLIENT_INPUTS[key].get("completed_at", 0),
        )[:overflow]
        for client_id in oldest:
            _COMPLETED_CLIENT_INPUTS.pop(client_id, None)


def _client_input_id(value: object) -> str:
    candidate = str(value or "").strip()
    return candidate if re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", candidate) else ""


def _resume_scope(local_client: bool, session_token: str) -> str:
    return "desktop" if local_client else f"web:{session_token}"


def _empty_resume_state() -> dict:
    return {
        "messages": None, "task": None, "reason": None, "summary": "",
        "updated_at": time.time(), "from_disk": False,
    }


_RESUME_OMITTED = "[已省略：可能含凭据]"
_RESUME_LONG_ARGUMENT = 4096
_RESUME_SECRET_KEY = re.compile(
    r"(?:password|passwd|pwd|passphrase|credential|auth[_-]?token|secret|token|"
    r"api[_-]?key|access[_-]?key|authorization|(?:^|[_-])pass(?:$|[_-]))",
    re.IGNORECASE,
)
_RESUME_SECRET_PATTERNS = (
    re.compile(r"(?i)(\bsshpass\b[^\r\n;&|]*?\s-p\s+)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s;&|]+)"),
    re.compile(r"(?i)((?<!\S)-p\s+)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s;&|]+)"),
    re.compile(r"(?i)((?<!\S)--password(?:\s+|=))(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s;&|]+)"),
    re.compile(r"(?i)(\bmysql\b[^\r\n;&|]*?\s-p)([^\s;&|]+)"),
    re.compile(r"(?i)(\bcurl\b[^\r\n;&|]*?\s-u\s+[^\s:;&|]+:)([^\s;&|]+)"),
    re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)([^\s@/]+)(@)"),
    re.compile(
        r"(?i)(\b(?:password|passwd|pwd|pass|passphrase|credential|auth[_-]?token|"
        r"secret|token|api[_-]?key|access[_-]?key)\b\s*=\s*)"
        r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s;&|]+)"
    ),
)


def _redact_resume_text(value: str) -> str:
    redacted = value
    for pattern in _RESUME_SECRET_PATTERNS:
        redacted = pattern.sub(
            lambda match: match.group(1) + "***" + (match.group(3) if match.lastindex == 3 else ""),
            redacted,
        )
    return redacted


def _redact_resume_value(value, field: str = ""):
    if isinstance(value, dict):
        return {
            key: "***" if _RESUME_SECRET_KEY.search(str(key)) else _redact_resume_value(item, str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_resume_value(item, field) for item in value]
    if isinstance(value, str):
        redacted = _redact_resume_text(value)
        if field.lower() in {"command", "script", "content"} and len(value) > _RESUME_LONG_ARGUMENT:
            return redacted if redacted != value else _RESUME_OMITTED
        return redacted
    return value


def _redact_resume_messages(messages: list[dict]) -> list[dict]:
    safe = copy.deepcopy(messages)
    for message in safe if isinstance(safe, list) else []:
        for call in message.get("tool_calls", []) if isinstance(message, dict) else []:
            function = call.get("function") if isinstance(call, dict) else None
            raw = function.get("arguments") if isinstance(function, dict) else None
            if not isinstance(raw, str):
                continue
            try:
                function["arguments"] = json.dumps(
                    _redact_resume_value(json.loads(raw)), ensure_ascii=False,
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                function["arguments"] = (
                    _RESUME_OMITTED if len(raw) > _RESUME_LONG_ARGUMENT
                    else _redact_resume_text(raw)
                )
    # Do not pass the whole message list through the generic event limiter:
    # that limiter is allowed to replace long lists with "[truncated:N]", which
    # makes a resumable chat history invalid. Redact each message independently.
    safe_messages = []
    for message in safe if isinstance(safe, list) else []:
        if not isinstance(message, dict):
            continue
        native = message.pop("_responses_output", None)
        redacted = _redact_event_data(message, skip_media=False)
        redacted = _redact_resume_value(redacted)
        if isinstance(redacted, dict):
            if isinstance(native, list):
                output = []
                for item in native:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "reasoning":
                        # This opaque protocol state cannot be truncated or redacted.
                        # Plaintext reasoning summaries are not needed for replay.
                        output.append({"type": "reasoning", "summary": [], **{
                            key: item[key] for key in ("id", "encrypted_content")
                            if isinstance(item.get(key), str)}})
                    elif item.get("type") in {"message", "function_call"}:
                        output.append(_redact_resume_value(_redact_event_data(item, skip_media=False)))
                redacted["_responses_output"] = output
            safe_messages.append(redacted)
    return safe_messages


def _resume_disk_payload() -> dict[str, dict]:
    payload: dict[str, dict] = {}
    for scope, state in _RESUME_STATES.items():
        messages = state.get("messages")
        if not isinstance(messages, list) or not messages:
            continue
        compacted, _ = compact_context_messages(messages)
        payload[scope] = {
            "messages": _redact_resume_messages(compacted),
            "task": _redact_resume_value(_redact_event_data(state.get("task") or {}, skip_media=False)),
            "reason": str(state.get("reason") or ""),
            "summary": _redact_resume_value(
                _redact_event_data(str(state.get("summary") or ""), skip_media=False), "summary",
            )[:200],
            "updated_at": float(state.get("updated_at", 0)),
        }
        # Internal UUIDs are references, not credentials; generic token redaction must not break them.
        trace_id = str((state.get("task") or {}).get("task_trace_id") or "")
        if re.fullmatch(r"[a-f0-9]{32}", trace_id):
            payload[scope]["task"]["task_trace_id"] = trace_id
    return payload


def _persist_resume_states() -> bool:
    temporary = _RESUME_PATH.with_suffix(".tmp")
    try:
        payload = json.dumps(_resume_disk_payload(), ensure_ascii=False)
        with _RESUME_WRITE_LOCK:
            _RESUME_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, _RESUME_PATH)
        return True
    except Exception as exc:
        print(f"[resume] 保存失败: {exc}")
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _load_resume_states(now: float | None = None) -> dict[str, dict]:
    current = time.time() if now is None else now
    try:
        raw = json.loads(_RESUME_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        loaded: dict[str, dict] = {}
        for scope, state in raw.items():
            if not isinstance(scope, str) or not isinstance(state, dict):
                continue
            messages = state.get("messages")
            try:
                updated_at = float(state.get("updated_at", 0))
            except (TypeError, ValueError):
                continue
            if (
                not isinstance(messages, list) or not messages
                or current - updated_at > _RESUME_STATE_TTL_SECONDS
            ):
                continue
            # Older builds could persist a list-limit placeholder as one item.
            # Drop that malformed item rather than letting resume crash later.
            messages = [message for message in messages if isinstance(message, dict)]
            if not messages:
                continue
            loaded[scope] = {
                "messages": messages,
                "task": state.get("task") if isinstance(state.get("task"), dict) else {},
                "reason": str(state.get("reason") or ""),
                "summary": str(state.get("summary") or "")[:200],
                "updated_at": updated_at,
                "from_disk": True,
            }
        newest = sorted(
            loaded.items(), key=lambda item: item[1]["updated_at"], reverse=True,
        )[:_MAX_RESUME_STATES]
        return dict(newest)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"[resume] 读取失败: {exc}")
        return {}


def _prune_resume_states(now: float | None = None, *, persist: bool = True) -> bool:
    current = time.time() if now is None else now
    changed = False
    for scope, state in list(_RESUME_STATES.items()):
        if current - float(state.get("updated_at", 0)) > _RESUME_STATE_TTL_SECONDS:
            _RESUME_STATES.pop(scope, None)
            changed = True
    overflow = len(_RESUME_STATES) - _MAX_RESUME_STATES
    if overflow > 0:
        oldest = sorted(
            _RESUME_STATES, key=lambda scope: float(_RESUME_STATES[scope].get("updated_at", 0)),
        )[:overflow]
        for scope in oldest:
            _RESUME_STATES.pop(scope, None)
            changed = True
    if changed and persist:
        _persist_resume_states()
    return changed


def _resume_state(scope: str) -> dict:
    _prune_resume_states()
    return _RESUME_STATES.get(scope) or _empty_resume_state()


def _refresh_trace_resume(trace: dict, scope: str) -> None:
    state = _resume_state(scope)
    trace["can_resume"] = bool(
        trace.get("status") in {"stopped", "interrupted", "failed", "limited"}
        and state.get("messages") and state.get("reason") != "context_exhausted"
        and (state.get("task") or {}).get("task_trace_id") == trace.get("id")
    )
    trace["resume_persisted"] = trace["can_resume"] and bool(state.get("persisted") or state.get("from_disk"))


def _save_resume_state(
    scope: str, messages: object, task: dict, reason: str, summary: str = "",
) -> dict:
    state = {
        "messages": messages, "task": task, "reason": reason,
        "summary": summary[:200], "updated_at": time.time(), "from_disk": False,
    }
    _RESUME_STATES[scope] = state
    _prune_resume_states(persist=False)
    state["persisted"] = _persist_resume_states()
    return state


def _delete_resume_state(scope: str) -> None:
    if _RESUME_STATES.pop(scope, None) is not None:
        _persist_resume_states()


def _resume_automatically(state: dict, text: str, now: float | None = None) -> bool:
    if _is_continue_request(text):
        return True
    current = time.time() if now is None else now
    age = current - float(state.get("updated_at", 0))
    return state.get("reason") == "stopped" and age <= _RESUME_AUTO_WINDOW_SECONDS


def _resume_age_text(seconds: float) -> str:
    if seconds >= 24 * 3600:
        return f"约 {max(1, round(seconds / (24 * 3600)))} 天"
    if seconds >= 3600:
        return f"约 {max(1, round(seconds / 3600))} 小时"
    return f"约 {max(1, round(seconds / 60))} 分钟"


_RESUME_STATES.update(_load_resume_states())
if _RESUME_PATH.exists():
    _persist_resume_states()


async def broadcast(payload: dict) -> None:
    """把事件推给所有打开着的前端(消息中心用)。"""
    dead = []
    for w in list(ACTIVE_WS):
        try:
            await w.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception:
            dead.append(w)
    for w in dead:
        ACTIVE_WS.discard(w)


def _schedule_foreground_notice(loop: asyncio.AbstractEventLoop, active: bool) -> None:
    # One desktop lease, one ordered broadcast, including remote-channel tasks.
    payload = {"type": "foreground_control", "data": {"active": bool(active)}}
    try:
        loop.call_soon_threadsafe(lambda: asyncio.create_task(broadcast(payload)))
    except RuntimeError:
        pass  # The event loop has already shut down.


def _stop_desktop_on_escape(loop, task, state) -> None:
    def stop():
        state["stop"] = True
        asyncio.create_task(_request_running_task_stop(task, grace_seconds=0.1))
    loop.call_soon_threadsafe(stop)


@app.websocket("/worker")
async def worker_endpoint(ws: WebSocket) -> None:
    """worker(你电脑)拨号进来的入口。仅 hub 角色启用。"""
    await ws.accept()
    if HUB_BRIDGE is None:
        await ws.close()
        return
    await HUB_BRIDGE.handle_worker(ws)


async def _save_chat_in_thread(save_fn, *args):
    """连接断开也要等已开始的文件写入结束，避免后台写入越过会话清理。"""
    write = asyncio.create_task(asyncio.to_thread(save_fn, *args))
    try:
        return await asyncio.shield(write)
    except asyncio.CancelledError:
        try:
            await write
        finally:
            raise


async def _cancel_running_task(task: asyncio.Task | None) -> bool:
    """Cancel a task and wait until its coroutine can no longer schedule more work."""
    if task is None or task.done():
        return False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    return True


async def _request_running_task_stop(task: asyncio.Task | None, grace_seconds: float = 1.0) -> bool:
    """Let the agent emit a checkpoint first, then cancel if it is blocked."""
    if task is None or task.done():
        return False
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=grace_seconds)
    except asyncio.TimeoutError:
        await _cancel_running_task(task)
    except Exception:
        pass
    return True


def _is_continue_request(text: str) -> bool:
    return text.strip().lower() in {"继续", "接着做", "接着干", "continue", "go on"}


def _take_channel_interjections(active: dict) -> list[str]:
    items = list(active["interjections"])
    active["interjections"].clear()
    return items


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    # HTTP 中间件管不到 WebSocket,这里必须自己校验,否则设了密码也能绕过。
    # 本机连接(悬浮球/本地浏览器)免密码,只挡公网陌生人。
    origin = ws.headers.get("origin", "")
    host = ws.headers.get("host", "")
    local_client = _is_local(ws.client.host if ws.client else None)
    token = ws.query_params.get("ui_token", "")
    # 浏览器从后端托管页发起时,Origin 一定是 //host,直接放行(不需要令牌)。
    browser_same_origin = bool(origin) and origin.endswith("//" + host)
    # 浏览器发 WS 一定带 Origin;空 Origin 只可能是本机非浏览器工具(测试脚本等)。
    empty_origin_local = (not origin) and local_client
    # file:// 或 null:既是 Electron 悬浮球、也是恶意 iframe 的伪装。两者靠令牌区分——
    # 必须本机 + 带正确 UI 令牌。恶意网页读不到本机令牌文件,过不了这关。
    file_like = origin == "null" or origin.startswith("file://")
    token_ok = bool(_UI_TOKEN) and _matches_secret(token, _UI_TOKEN)
    if browser_same_origin or origin in _ALLOWED_ORIGINS or empty_origin_local:
        pass
    elif file_like and local_client and token_ok:
        pass
    else:
        print(f"[ws-gate] 拒绝(4003):没有任何一条放行规则命中")
        await ws.close(code=4003)
        return
    if (not local_client and (not CFG.auth.enabled
            or not _session_valid(ws.cookies.get("xa_session", "")))):
        await ws.close(code=4001)
        return
    await ws.accept()
    ACTIVE_WS.add(ws)
    # The lease may have been acquired by a remote channel before this UI connected.
    await ws.send_text(json.dumps({
        "type": "foreground_control",
        "data": {"active": system_control.foreground_control_active()},
    }, ensure_ascii=False))
    session_id = uuid.uuid4().hex
    resume_scope_base = _resume_scope(local_client, ws.cookies.get("xa_session", ""))
    session_client_ids: set[str] = set()
    runtimes: dict[str, dict] = {}
    selected_conversation_id = "default" if "default" in DESKTOP_CONVERSATIONS else next(iter(DESKTOP_CONVERSATIONS))

    async def send_ws(event_type: str, data: dict | None = None, conversation_id: str = "") -> None:
        payload = dict(data or {})
        if conversation_id:
            payload.setdefault("conversation_id", conversation_id)
        await ws.send_text(json.dumps({"type": event_type, "data": payload}, ensure_ascii=False))

    def runtime_for(conversation_id: str) -> dict | None:
        conversation = DESKTOP_CONVERSATIONS.get(conversation_id)
        if conversation is None:
            return None
        existing = runtimes.get(conversation_id)
        if existing is not None:
            return existing
        state = {
            "conversation_id": conversation_id,
            "conversation": conversation,
            "history": conversation.setdefault("messages", []),
            "interjections": [],
            "interjection_history": [],
            "pending_confirm": None,
            "stop": False,
            "pending_inputs": [],
            "current": None,
            "resume_scope": resume_scope_base if conversation_id == "default" else f"{resume_scope_base}:{conversation_id}",
        }

        async def confirm_fn(name: str, args: dict) -> bool:
            loop = asyncio.get_running_loop()
            # Register the future before sending the modal: early clicks must not be lost
            # while the display trail is being flushed to disk.
            if state["pending_confirm"] is None:
                state["pending_confirm"] = loop.create_future()
            try:
                return await state["pending_confirm"]
            finally:
                state["pending_confirm"] = None

        state["agent"] = Agent(
            LLM, REGISTRY, CFG.agent, confirm_fn=confirm_fn, memory=MEMORY,
            skill_prompt_fn=_active_skill_prompt,
            runtime_context=(
                _HUB_RUNTIME_CONTEXT if CFG.role.is_hub
                else _LOCAL_CLOUD_RUNTIME_CONTEXT if CFG.role.is_standalone
                else ""
            ),
            abort_fn=lambda: state["stop"],
            interjection_fn=lambda: _take_channel_interjections(state),
            approval_state=ConversationApproval(),
        )
        runtimes[conversation_id] = state
        return state

    async def send_conversation_list() -> None:
        items = []
        for item in sorted(DESKTOP_CONVERSATIONS.values(), key=lambda value: value.get("updated_at", 0), reverse=True):
            # 首条输入已接受但回复尚未完成时，messages 仍可能为空。
            if not (item.get("messages") or item.get("started")):
                continue
            summary = _conversation_summary(item)
            state = runtimes.get(summary["id"])
            summary["running"] = bool(state and state.get("current") and not state["current"].done())
            summary["unread"] = bool(state and state.get("unread"))
            items.append(summary)
        await send_ws("conversation_list", {
            "conversations": items, "current_id": selected_conversation_id,
        })

    async def send_conversation_history(conversation_id: str) -> None:
        state = runtime_for(conversation_id)
        if state is not None:
            for message in state["history"]:
                if isinstance(message.get("task_trace"), dict):
                    _refresh_trace_resume(message["task_trace"], state["resume_scope"])
            await send_ws("history", {"messages": state["history"]}, conversation_id)

    await send_conversation_list()
    await send_conversation_history(selected_conversation_id)
    for conversation_id in list(DESKTOP_CONVERSATIONS):
        state = runtime_for(conversation_id)
        resume_state = _resume_state(state["resume_scope"])
        if resume_state.get("messages") and resume_state.get("reason") != "context_exhausted":
            await send_ws("resume_available", {"summary": resume_state.get("summary", ""), "task_trace_id": (resume_state.get("task") or {}).get("task_trace_id")}, conversation_id)

    async def run_task(state: dict, task: dict, resume: bool = False) -> None:
        conversation_id = state["conversation_id"]
        history = state["history"]
        agent = state["agent"]
        resume_scope = state["resume_scope"]
        resume_state = _resume_state(resume_scope)
        if resume and (not resume_state.get("messages") or resume_state.get("reason") == "context_exhausted"
                       or (task.get("task_trace_id") and task["task_trace_id"] !=
                           (resume_state.get("task") or {}).get("task_trace_id"))):
            # Recheck at execution time: another page may already have consumed the checkpoint.
            rejected_id = _client_input_id(task.get("client_id"))
            if rejected_id and _ACTIVE_CLIENT_INPUTS.get(rejected_id) == session_id:
                _ACTIVE_CLIENT_INPUTS.pop(rejected_id, None)
                session_client_ids.discard(rejected_id)
            await send_ws("error", {"where": "task.resume", "client_id": rejected_id,
                "msg": "断点已失效或已被其他页面接续；未重新执行任务。"}, conversation_id)
            return
        state["interjection_history"].clear()
        started_at = time.perf_counter()
        client_id = _client_input_id(task.get("client_id"))
        terminal_delivered = False
        completed_normally = False
        duration_ms = 0
        # 记忆是后台初始化的:连接建立时可能还没就绪(memory=None),
        # 这里每次任务开始时把最新的 MEMORY 补给 agent,就绪后立刻生效,
        # 不用等用户重开页面。
        if agent.memory is None and MEMORY is not None:
            agent.memory = MEMORY
        # 每条用户输入都是独立任务；授权不能从上一条任务带入下一条。
        agent.approval_state = ConversationApproval()
        text = str(task.get("text") or "").strip()
        attachments = task.get("attachments") or []
        task_llm, owns_llm, provider_models, provider_id = _llm_for_provider(
            str(task.get("provider_id") or "default").strip()
        )
        _retain_llm(task_llm)
        requested_model = str(task.get("model") or "").strip()
        if requested_model and (requested_model not in provider_models or
                str(task.get("provider_id") or "default").strip() != provider_id):
            if client_id and _ACTIVE_CLIENT_INPUTS.get(client_id) == session_id:
                _ACTIVE_CLIENT_INPUTS.pop(client_id, None)
                session_client_ids.discard(client_id)
            try:
                await send_ws("error", {"where": "llm.config", "client_id": client_id,
                    "msg": "当前任务的模型或接口已不可用，请手动选择其他模型后发送；未自动替换。"}, conversation_id)
            finally:
                if owns_llm:
                    await _retire_llm(task_llm)
                await _release_llm(task_llm)
            return
        model = requested_model if requested_model in provider_models else provider_models[0]
        context_window = _context_window_for_model(provider_id, model)
        # Each task gets its own AgentConfig so model context settings do not leak across models.
        agent.cfg = dataclass_replace(CFG.agent, context_window=context_window)
        _cap_attachment_image_budget(attachments, context_window)
        requested_approval = str(task.get("approval_mode") or "risk").strip().lower()
        approval_mode = requested_approval if requested_approval in {"risk", "full"} else "risk"
        # 服务端裁决,不盲信客户端:"完全访问"(降低泛化危险确认)只允许本机连接使用——
        # 你人坐在电脑前自己选的照旧生效。远程(hub 网页等非本机)客户端即便发 full 也
        # 压回 risk，不能靠一条消息把确认关掉。资金/隐私/外部代码仍进入
        # 对话级授权判定；只有该使用者在该对话明确批准后才复用。
        if approval_mode == "full" and not local_client:
            approval_mode = "risk"
        reasoning_effort = str(task.get("reasoning_effort") or "high").strip().lower()
        if reasoning_effort not in {"low", "medium", "high", "xhigh"}:
            reasoning_effort = "high"
        last_reply = ""
        last_screenshot = ""
        desktop_reply_attachments: list[dict] = []
        desktop_attachments_token = _DESKTOP_REPLY_ATTACHMENTS.set(desktop_reply_attachments)
        task_id = uuid.uuid4().hex
        # Keep an interrupted delivery's saved trail on reconnect; don't duplicate its user row.
        retry_entry = next((item for item in history
                            if client_id and (item.get("task_trace") or {}).get("client_id") == client_id
                            and (item.get("task_trace") or {}).get("status") == "interrupted"), None)
        resume_entry = next((item for item in history if resume and
                             (item.get("task_trace") or {}).get("id") ==
                             (resume_state.get("task") or {}).get("task_trace_id")), None)
        if resume_entry is not None:
            model_history = _recent_history(history, CFG.agent.max_history_messages)
            assistant_entry = resume_entry
            trace = resume_entry["task_trace"]
            task_id = trace["id"]
            trace["status"] = "running"
            trace["started_at"] = int(time.time() * 1000) - trace.get("duration_ms", 0)
            trace.pop("finished_at", None)
            trace["current"] = "正在从保存断点继续"
            task_trace.consume(trace, "notice", {"text": "从保存断点继续；保留已完成步骤，先核实未完成操作的现状。"})
        elif retry_entry is not None:
            index = history.index(retry_entry)
            begin = index - 1 if index and history[index - 1].get("client_id") == client_id else index
            model_history = _recent_history(history[:begin] + history[index + 1:], CFG.agent.max_history_messages)
            assistant_entry = retry_entry
            trace = retry_entry["task_trace"]
            task_id = trace["id"]
            trace["status"] = "running"
            trace["current"] = "连接已恢复，正在继续处理"
            task_trace.consume(trace, "notice", {"text": "连接已恢复，继续处理本次请求；保留上次已执行的过程。"})
        else:
            model_history = _recent_history(history, CFG.agent.max_history_messages)
            trace = task_trace.new_trace(task_id, text)
            trace["client_id"] = client_id
            history_text = text or "请处理我上传的附件。"
            if attachments:
                history_text += "\n[附件：" + "、".join(str(item.get("name") or "附件") for item in attachments) + "]"
            user_entry = {"role": "user", "content": history_text, "ts": trace["started_at"], "client_id": client_id}
            history_attachments = _attachment_history_items(attachments)
            if history_attachments:
                user_entry["attachments"] = history_attachments
            assistant_entry = {"role": "assistant", "content": "", "ts": trace["started_at"], "task_trace": trace}
            history.extend([user_entry, assistant_entry])
        trace["conversation_id"] = conversation_id
        audit_trace = _AuditTrace(session_id, task_id, approval_mode)
        state["display_trace"] = trace

        persistence_warned = False

        async def persist_trace() -> None:
            nonlocal persistence_warned
            _refresh_trace_resume(trace, resume_scope)
            if len(history) > _MAX_SAVED_HISTORY_MESSAGES:
                del history[:len(history) - _MAX_SAVED_HISTORY_MESSAGES]
            state["conversation"]["updated_at"] = int(time.time() * 1000)
            saved = await _save_chat_in_thread(_save_conversations, DESKTOP_CONVERSATIONS)
            if not saved and not persistence_warned:
                persistence_warned = True
                await send_ws("error", {"where": "history.save", "msg": "执行过程保存失败；本轮结果仍可查看，但重开后过程可能丢失，请检查磁盘空间和目录权限。"}, conversation_id)

        async def publish_trace(reason: str = "") -> None:
            await persist_trace()
            await send_ws("task_trace", {"trace": trace, "reason": reason}, conversation_id)

        # 续做:用上次保存的完整上下文(含工具调用轨迹),不是重新理解一段总结
        rm = resume_state["messages"] if resume else None
        if resume:
            age = max(0.0, time.time() - float(resume_state.get("updated_at", 0)))
            if resume_state.get("from_disk") and age > _RESUME_AUTO_WINDOW_SECONDS and rm:
                summary = str(resume_state.get("summary") or "这个任务")
                rm = [{
                    "role": "system",
                    "content": (
                        f"以下任务“{summary}”于{_resume_age_text(age)}前中断，现在继续；"
                        "开始前请先确认相关文件和进程的当前状态。"
                    ),
                }, *rm]
            _delete_resume_state(resume_scope)  # 取出即清,磁盘同步删除
            resume_state.update({"messages": None, "task": None, "reason": None})
        # 冷启动等待的进度:cad 工具跑在工作线程里,拿不到事件流。这里注入一个回调,
        # 用 run_coroutine_threadsafe 把进度投回主循环、推给前端(用户能看到"已等 N 秒")。
        loop = asyncio.get_running_loop()

        def _cad_progress(message: str) -> None:
            async def _send() -> None:
                try:
                    await send_ws("progress", {"cad_wait": message}, conversation_id)
                except Exception:
                    pass

            try:
                asyncio.run_coroutine_threadsafe(_send(), loop)
            except Exception:
                pass

        cad_module.set_progress_callback(_cad_progress)

        foreground_task = asyncio.current_task()
        foreground_notice_token = system_control.set_foreground_notice_callback(
            lambda active: _schedule_foreground_notice(loop, active),
            on_escape=lambda: _stop_desktop_on_escape(loop, foreground_task, state),
        )
        shared_token = _SHARED_TURN.set(None)
        try:
            await publish_trace("started")
            # Select the shared library before exposing any ordinary memory tools.
            model_history = await _prepare_desktop_shared(state, text, model_history, local_client)
            if _SHARED_TURN.get() or any(row.get("_shared_library") for row in history):
                rm = None  # A raw checkpoint must never carry another library's data or tools.
            agent.tools = _channel_agent_registry(REGISTRY)
            agent.memory = None if _SHARED_TURN.get() else MEMORY
            agent.runtime_context = _with_shared_prompt(
                _HUB_RUNTIME_CONTEXT if CFG.role.is_hub
                else _LOCAL_CLOUD_RUNTIME_CONTEXT if CFG.role.is_standalone else "")
            tag = _shared_history_tag()
            if (retry_entry or resume_entry) and assistant_entry.get("_shared_library") != tag.get("_shared_library"):
                raise RuntimeError("原任务的项目上下文已变更或过期，请重新以调用名称开头发送请求。")
            assistant_entry.update(tag)
            if not retry_entry and not resume_entry:
                user_entry.update(_shared_history_tag())
            # 带上最近的对话历史(滑动窗口,防止上下文过长卡死)。
            # Follow-ups keep the latest upload group even outside the model history window.
            model_attachments = attachments or _restore_latest_attachments(
                [item for item in history if item.get("_shared_library") ==
                 (_SHARED_TURN.get() or {}).get("library_id")], context_window)
            _cap_attachment_image_budget(model_attachments, context_window)
            state["attachments_in_use"] = model_attachments
            async for ev in agent.run(
                text,
                history=model_history,
                resume_messages=rm,
                model=model,
                llm=task_llm,
                approval_mode=approval_mode,
                reasoning_effort=reasoning_effort,
                response_language=task.get("response_language"),
                attachments=model_attachments,
            ):
                _normalize_event_punctuation(ev)
                await asyncio.to_thread(audit_trace.consume, ev)
                # 注意:流式回复走的是 message_end(不是 message),两者都要收,
                # 否则流式下 assistant 的回复会存不进历史,重开就断了上下文。
                if ev.type in ("message", "message_end") and ev.data.get("content"):
                    last_reply = ev.data["content"]
                if ev.type == "screenshot" and ev.data.get("image"):
                    # 只保留本轮最新截图；过程截图仍只在当前会话展示。
                    last_screenshot = str(ev.data["image"])
                if ev.type == "truncated":
                    task_trace.finish(trace, "limited")
                    reason = ev.data.get("reason", "step_limit")
                    if reason != "context_exhausted":
                        # 存下断点(完整上下文留服务端,不发给前端——太大且没必要)。
                        # context_exhausted 是死局(上下文装不下,无法"继续"),不留断点,
                        # 否则每次重连都会推 resume_available 弹一次关不掉的框。
                        resume_state = _save_resume_state(resume_scope, ev.data.get("resume_messages"), {
                            "text": "继续", "provider_id": provider_id,
                            "model": model, "approval_mode": approval_mode,
                            "reasoning_effort": reasoning_effort, "task_trace_id": task_id,
                        }, reason, str(resume_state.get("summary") or text) if resume else text)
                    await publish_trace("truncated")
                    await send_ws("truncated", {"steps_done": ev.data.get("steps_done"), "reason": reason, "task_trace_id": task_id}, conversation_id)
                    continue
                if ev.type in {"aborted", "checkpoint"}:
                    if ev.type == "aborted":
                        task_trace.finish(trace, "stopped")
                    resume_state = _save_resume_state(resume_scope, ev.data.get("resume_messages"), {
                        "text": "继续", "provider_id": provider_id,
                        "model": model, "approval_mode": approval_mode,
                        "reasoning_effort": reasoning_effort, "task_trace_id": task_id,
                    }, ev.data.get("reason", "stopped"), str(resume_state.get("summary") or text) if resume else text)
                    await publish_trace(ev.type)
                    continue
                event_data = _redact_event_data(ev.data)
                if ev.type == "tool_result" and any(
                    item.get("kind") == "tool" and item.get("name") == ev.data.get("name")
                    and item.get("status") in {"running", "waiting", "stopped"} for item in trace["events"]
                ):
                    raw = ev.data.get("result", "")
                    if isinstance(raw, str):
                        try:
                            raw = json.loads(raw)
                        except (ValueError, TypeError):
                            pass
                    safe_raw = _redact_event_data(raw, skip_media=False,
                                                  text_limit=task_trace.MAX_RAW_STEP_BYTES + 1, omit_media=True)
                    raw_text = safe_raw if isinstance(safe_raw, str) else json.dumps(safe_raw, ensure_ascii=False, indent=2)
                    event_data["raw_return"] = await asyncio.to_thread(
                        task_trace.store_return, trace, _redact_resume_text(raw_text), _ATTACHMENT_DIR)
                event_data.setdefault("task_id", task_id)
                trace_changed = task_trace.consume(trace, ev.type, event_data)
                if ev.type == "confirm_needed" and state["pending_confirm"] is None:
                    state["pending_confirm"] = loop.create_future()
                # Agent 将模型调用异常转换成 error 事件后结束生成器。
                # 补回任务编号和终止信息，前端才能结束“工作中”并停止补发。
                if ev.type == "error" and event_data.get("where") in {"llm.chat", "llm.config"}:
                    event_data["task_finished"] = True
                    if event_data["where"] == "llm.chat":
                        event_data["service_failed"] = True
                    event_data["duration_ms"] = max(
                        0, round((time.perf_counter() - started_at) * 1000)
                    )
                    if client_id:
                        event_data["client_id"] = client_id
                if ev.type == "done":
                    completed_normally = not event_data.get("aborted") and not event_data.get("truncated")
                    duration_ms = max(0, round((time.perf_counter() - started_at) * 1000))
                    event_data["duration_ms"] = duration_ms
                    event_data["reply_attachments"] = _attachment_history_items(
                        desktop_reply_attachments,
                    )
                    if client_id:
                        event_data["client_id"] = client_id
                    if not event_data.get("aborted") and not event_data.get("truncated"):
                        _delete_resume_state(resume_scope)
                        resume_state = _empty_resume_state()
                if ev.type == "done" or event_data.get("task_finished"):
                    status = ("failed" if event_data.get("task_finished") else
                              "stopped" if event_data.get("aborted") else
                              "limited" if event_data.get("truncated") else "completed")
                    task_trace.finish(trace, status, final_reply=last_reply if status == "completed" else "")
                    if status == "completed":
                        assistant_entry["content"] = last_reply
                    screenshot_item = _save_persisted_screenshot(last_screenshot) if status == "completed" else None
                    reply_attachments = _attachment_history_items(desktop_reply_attachments)
                    if screenshot_item:
                        reply_attachments.append(screenshot_item)
                    if reply_attachments:
                        assistant_entry["attachments"] = reply_attachments
                        event_data["reply_attachments"] = reply_attachments
                    event_data["trace"] = trace
                    event_data["final_reply"] = assistant_entry["content"]
                    if status == "completed" and completed_normally:
                        await asyncio.to_thread(cad_module._close_task_owned_autocad)
                    await persist_trace()
                await send_ws(ev.type, event_data, conversation_id)
                if trace_changed and ev.type != "done" and not event_data.get("task_finished"):
                    await publish_trace(ev.type)
                if ev.type == "done" or event_data.get("task_finished") or event_data.get("service_failed"):
                    duration_ms = event_data.get("duration_ms", duration_ms)
                    state["unread"] = conversation_id != selected_conversation_id
                    terminal_delivered = True
            for item in state["interjection_history"]:
                history.insert(history.index(assistant_entry), {
                    "role": "user", "content": f"[用户中途插话]\n{item}", "ts": int(time.time() * 1000),
                    **_shared_history_tag(),
                })
            await persist_trace()
            if conversation_id == "default":
                await _save_chat_in_thread(save_chat_history, history)  # 兼容旧版备份
        except Exception as e:
            duration_ms = max(0, round((time.perf_counter() - started_at) * 1000))
            error_data = {
                "msg": str(e), "service_failed": True, "duration_ms": duration_ms,
            }
            if trace["status"] not in {"completed", "stopped", "limited"}:
                task_trace.consume(trace, "error", _redact_event_data(error_data, skip_media=False))
                task_trace.finish(trace, "failed")
            else:
                terminal_delivered = True  # The durable result can be replayed after reconnect.
            error_data["trace"] = trace
            if client_id:
                error_data["client_id"] = client_id
            await persist_trace()
            try:
                await send_ws("error", error_data, conversation_id)
                terminal_delivered = True
            except Exception:
                # 连接本身已经断了：不要记成完成，让前端重连后按原 client_id 补发。
                pass
        finally:
            if trace["status"] in task_trace.ACTIVE_STATUSES:
                task_trace.finish(trace, "stopped" if state["stop"] else "interrupted")
                await persist_trace()
                try:
                    await send_ws("task_trace", {"trace": trace}, conversation_id)
                except Exception:
                    pass
            _SHARED_TURN.reset(shared_token)
            state.pop("display_trace", None)
            system_control.reset_foreground_notice_callback(foreground_notice_token)
            state["attachments_in_use"] = []
            _DESKTOP_REPLY_ATTACHMENTS.reset(desktop_attachments_token)
            cad_module.set_progress_callback(None)  # 本轮结束,清掉进度回调
            if client_id:
                if terminal_delivered:
                    _COMPLETED_CLIENT_INPUTS[client_id] = {
                        "completed_at": time.time(), "duration_ms": float(duration_ms),
                    }
                    _prune_client_inputs()
                if _ACTIVE_CLIENT_INPUTS.get(client_id) == session_id:
                    _ACTIVE_CLIENT_INPUTS.pop(client_id, None)
                session_client_ids.discard(client_id)
            if owns_llm:
                await _retire_llm(task_llm)
            await _release_llm(task_llm)

    max_pending_inputs = 10

    async def run_queue(state: dict, first_task: dict, resume: bool = False) -> None:
        """每个对话各自排队；切换到别的对话不会打断这里。"""
        try:
            await run_task(state, first_task, resume=resume)
            while state["pending_inputs"]:
                nxt = state["pending_inputs"].pop(0)
                if state["stop"]:
                    for pending in state["pending_inputs"]:
                        pending_id = _client_input_id(pending.get("client_id"))
                        if pending_id and _ACTIVE_CLIENT_INPUTS.get(pending_id) == session_id:
                            _ACTIVE_CLIENT_INPUTS.pop(pending_id, None)
                            session_client_ids.discard(pending_id)
                    state["pending_inputs"].clear()
                    break
                await send_ws("dequeued", {"text": nxt.get("text", "")}, state["conversation_id"])
                await run_task(state, nxt)
        finally:
            state["current"] = None
            await send_conversation_list()

    async def delete_conversation(conversation_id: str) -> tuple[bool, str]:
        state = runtimes.get(conversation_id)
        if state and state.get("current") and not state["current"].done():
            state["stop"] = True
            if state.get("pending_confirm") and not state["pending_confirm"].done():
                state["pending_confirm"].set_result(False)
            await _request_running_task_stop(state["current"])
        if conversation_id not in DESKTOP_CONVERSATIONS:
            return False, "对话不存在"
        if len(DESKTOP_CONVERSATIONS) <= 1:
            return False, "至少保留一个对话；请先新建对话后再删除。"
        state = runtimes.pop(conversation_id, None)
        if state:
            for pending in state["pending_inputs"]:
                pending_id = _client_input_id(pending.get("client_id"))
                if pending_id and _ACTIVE_CLIENT_INPUTS.get(pending_id) == session_id:
                    _ACTIVE_CLIENT_INPUTS.pop(pending_id, None)
                    session_client_ids.discard(pending_id)
        _delete_resume_state(resume_scope_base if conversation_id == "default" else f"{resume_scope_base}:{conversation_id}")
        DESKTOP_CONVERSATIONS.pop(conversation_id, None)
        if conversation_id == "default":
            DESKTOP_HISTORY.clear()
            save_chat_history([])
        _save_conversations(DESKTOP_CONVERSATIONS)
        _cleanup_attachments(remove_all_unreferenced=True)
        return True, ""

    current_task_ids: set[str] = set()
    _DESKTOP_ATTACHMENT_RUNTIMES[session_id] = runtimes
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")
            requested_id = _conversation_id(msg.get("conversation_id"))
            if mtype == "conversation_list":
                await send_conversation_list()
                continue
            if mtype == "conversation_create":
                # 兼容旧客户端：新建只选择临时草稿，不创建或保存空对话。
                selected_conversation_id = f"conv_{uuid.uuid4().hex}"
                await send_conversation_list()
                await send_ws("history", {"messages": []}, selected_conversation_id)
                continue
            if mtype == "conversation_select":
                conversation_id = requested_id or selected_conversation_id
                if conversation_id not in DESKTOP_CONVERSATIONS:
                    await send_ws("error", {"msg": "对话不存在。"})
                    continue
                selected_conversation_id = conversation_id
                state = runtime_for(conversation_id)
                state["unread"] = False
                await send_conversation_history(conversation_id)
                await send_conversation_list()
                continue
            if mtype == "conversation_model":
                conversation_id = requested_id or selected_conversation_id
                conversation = DESKTOP_CONVERSATIONS.get(conversation_id)
                provider_id = str(msg.get("provider_id") or "default").strip()
                model = str(msg.get("model") or "").strip()
                if conversation is None or not any(
                    option["provider_id"] == provider_id and option["model"] == model
                    for option in _model_options()
                ):
                    await send_ws("error", {"msg": "模型或接口已不可用，请重新选择。"}, conversation_id)
                    continue
                conversation["model_selection"] = {"provider_id": provider_id, "model": model}
                _save_conversations(DESKTOP_CONVERSATIONS)
                await send_conversation_list()
                continue
            if mtype == "conversation_rename":
                conversation_id = requested_id or selected_conversation_id
                conversation = DESKTOP_CONVERSATIONS.get(conversation_id)
                title = str(msg.get("title") or "").strip()[:80]
                if conversation is None or not title:
                    await send_ws("error", {"msg": "对话名称不能为空。"}, conversation_id)
                    continue
                conversation["title"] = title
                conversation["updated_at"] = int(time.time() * 1000)
                _save_conversations(DESKTOP_CONVERSATIONS)
                await send_conversation_list()
                continue
            if mtype == "conversation_delete":
                conversation_id = requested_id or selected_conversation_id
                ok, error = await delete_conversation(conversation_id)
                if not ok:
                    await send_ws("error", {"msg": error}, conversation_id)
                    continue
                switched_conversation_id = ""
                if selected_conversation_id == conversation_id:
                    selected_conversation_id = next(iter(DESKTOP_CONVERSATIONS))
                    switched_conversation_id = selected_conversation_id
                # 先通知前端删除旧项，再发送新选择项的列表和历史，避免历史事件被旧 ID 丢弃。
                await send_ws("conversation_deleted", {"deleted_id": conversation_id})
                await send_conversation_list()
                if switched_conversation_id:
                    await send_conversation_history(switched_conversation_id)
                continue
            if mtype == "user_input":
                conversation_id = requested_id or selected_conversation_id
                text = str(msg.get("text") or "").strip()
                client_id = _client_input_id(msg.get("client_id"))
                if msg.get("conversation_id") and not requested_id:
                    await send_ws("error", {"msg": "对话标识无效。", "client_id": client_id})
                    continue
                if conversation_id not in DESKTOP_CONVERSATIONS and not (
                    (requested_id and msg.get("new_conversation") is True)
                    or conversation_id == selected_conversation_id
                ):
                    await send_ws("error", {"msg": "对话不存在，请重新选择或新建对话。", "client_id": client_id}, conversation_id)
                    continue
                _prune_client_inputs()
                saved_trace = next((item.get("task_trace") for item in
                    (DESKTOP_CONVERSATIONS.get(conversation_id) or {}).get("messages", [])
                    if client_id and (item.get("task_trace") or {}).get("client_id") == client_id), None)
                if saved_trace and saved_trace.get("status") in {"completed", "failed", "stopped", "limited"}:
                    await send_ws("input_done", {"client_id": client_id,
                        "duration_ms": saved_trace.get("duration_ms", 0)}, conversation_id)
                    continue
                if client_id in _COMPLETED_CLIENT_INPUTS:
                    completed = _COMPLETED_CLIENT_INPUTS[client_id]
                    await send_ws("input_done", {
                        "client_id": client_id, "duration_ms": completed.get("duration_ms", 0),
                    }, conversation_id)
                    continue
                owner = _ACTIVE_CLIENT_INPUTS.get(client_id) if client_id else None
                if owner:
                    if owner == session_id:
                        await send_ws("input_ack", {"client_id": client_id}, conversation_id)
                    continue
                resume_scope = resume_scope_base if conversation_id == "default" else f"{resume_scope_base}:{conversation_id}"
                resume_state = _resume_state(resume_scope)
                interrupted_trace = saved_trace if saved_trace and saved_trace.get("status") == "interrupted" else None
                bound_retry = bool(interrupted_trace and resume_state.get("messages") and
                    (resume_state.get("task") or {}).get("task_trace_id") == interrupted_trace.get("id"))
                if interrupted_trace and interrupted_trace.get("has_tools") and not bound_retry:
                    await send_ws("error", {"client_id": client_id,
                        "msg": "上次任务已有操作记录，但没有有效断点；为避免重复执行，已停止自动重发。请先核实实际结果，再提出后续要求。"}, conversation_id)
                    await send_conversation_history(conversation_id)
                    continue
                resume_from_input = bound_retry or bool(resume_state["messages"] and _resume_automatically(resume_state, text))
                resumed_task = resume_state.get("task") or {}
                attachment_provider_id = (
                    resumed_task.get("provider_id") if resume_from_input
                    else str(msg.get("provider_id") or "default").strip()
                )
                attachment_model = (
                    resumed_task.get("model") if resume_from_input
                    else str(msg.get("model") or "").strip()
                )
                if not resume_from_input and attachment_model and not any(
                    option["provider_id"] == attachment_provider_id and option["model"] == attachment_model
                    for option in _model_options()
                ):
                    await send_ws("error", {"where": "llm.config", "client_id": client_id,
                        "msg": "当前对话的模型或接口已不可用，请手动选择其他模型后发送；未自动替换。"}, conversation_id)
                    continue
                attachment_context_window = _context_window_for_model(
                    str(attachment_provider_id or "default"), str(attachment_model or "")
                )
                try:
                    # Keep the one-argument call compatible with channel/test adapters that wrap this helper.
                    attachments = await asyncio.to_thread(_prepare_attachments, msg.get("attachments"))
                    _cap_attachment_image_budget(attachments, attachment_context_window)
                except ValueError as exc:
                    await send_ws("error", {"msg": str(exc), "client_id": client_id}, conversation_id)
                    continue
                if not text and not attachments:
                    await send_ws("error", {"msg": "消息不能为空。", "client_id": client_id}, conversation_id)
                    continue
                if not resume_state["messages"] and _is_continue_request(text):
                    await send_ws("error", {
                        "msg": "没有可继续的断点；可能已完成或过期，不会从头重复执行。", "client_id": client_id,
                    }, conversation_id)
                    continue
                conversation = DESKTOP_CONVERSATIONS.get(conversation_id)
                if conversation is None:
                    if sum(bool(item.get("messages") or item.get("started")) for item in DESKTOP_CONVERSATIONS.values()) >= _MAX_CONVERSATIONS:
                        await send_ws("error", {"msg": "对话数量已达到上限。", "client_id": client_id}, conversation_id)
                        continue
                    conversation = _new_conversation(conversation_id)
                    DESKTOP_CONVERSATIONS[conversation_id] = conversation
                state = runtime_for(conversation_id)
                state["stop"] = False
                selected_conversation_id = conversation_id
                selection = {
                    "provider_id": str(msg.get("provider_id") or "default").strip(),
                    "model": str(msg.get("model") or "").strip(),
                }
                if selection["model"] and conversation.get("model_selection") != selection:
                    conversation["model_selection"] = selection
                    _save_conversations(DESKTOP_CONVERSATIONS)
                if not (conversation.get("messages") or conversation.get("started")):
                    if conversation.get("title") in {"新对话", "默认对话"}:
                        conversation["title"] = _auto_conversation_title(text, attachments)
                    conversation["started"] = True
                    conversation["updated_at"] = int(time.time() * 1000)
                    _save_conversations(DESKTOP_CONVERSATIONS)
                    await send_conversation_list()
                task = {
                    "conversation_id": conversation_id,
                    "client_id": client_id,
                    "text": text,
                    "attachments": attachments,
                    "provider_id": resumed_task.get("provider_id") if resume_from_input else str(msg.get("provider_id") or "default").strip(),
                    "model": resumed_task.get("model") if resume_from_input else str(msg.get("model") or "").strip(),
                    "approval_mode": resumed_task.get("approval_mode") if resume_from_input else str(msg.get("approval_mode") or "risk"),
                    "reasoning_effort": resumed_task.get("reasoning_effort") if resume_from_input else str(msg.get("reasoning_effort") or "high"),
                    "response_language": resumed_task.get("response_language") if resume_from_input else msg.get("response_language"),
                }
                if task["response_language"] not in ("en", "zh"):
                    task["response_language"] = None
                if client_id:
                    _ACTIVE_CLIENT_INPUTS[client_id] = session_id
                    session_client_ids.add(client_id)
                await send_ws("input_ack", {"client_id": client_id}, conversation_id)
                current = state.get("current")
                if current and not current.done():
                    if text and not attachments:
                        state["interjections"].append(text)
                        state["interjection_history"].append(text)
                        confirmation_cancelled = bool(state["pending_confirm"] and not state["pending_confirm"].done())
                        if confirmation_cancelled:
                            state["pending_confirm"].set_result(False)
                        if client_id:
                            _COMPLETED_CLIENT_INPUTS[client_id] = {"completed_at": time.time(), "duration_ms": 0.0}
                            _ACTIVE_CLIENT_INPUTS.pop(client_id, None)
                            session_client_ids.discard(client_id)
                            _prune_client_inputs()
                        await send_ws("interjected", {
                            "text": text, "client_id": client_id, "confirmation_cancelled": confirmation_cancelled,
                        }, conversation_id)
                        continue
                    if len(state["pending_inputs"]) >= max_pending_inputs:
                        if client_id:
                            _ACTIVE_CLIENT_INPUTS.pop(client_id, None)
                            session_client_ids.discard(client_id)
                        await send_ws("error", {"msg": "排队任务已满，请等前面的任务完成后再发。", "client_id": client_id}, conversation_id)
                        continue
                    state["pending_inputs"].append(task)
                    await send_ws("queued", {
                        "text": text, "ahead": len(state["pending_inputs"]), "client_id": client_id,
                    }, conversation_id)
                    continue
                state["current"] = asyncio.create_task(run_queue(state, task, resume=resume_from_input))
                continue
            if mtype == "continue_task":
                if msg.get("conversation_id") and not requested_id:
                    await send_ws("error", {"where": "task.resume", "msg": "对话标识无效；未继续任何任务。"}, selected_conversation_id)
                    continue
                conversation_id = requested_id or selected_conversation_id
                state = runtime_for(conversation_id)
                if state is None:
                    await send_ws("error", {"where": "task.resume", "msg": "没有可继续的断点。"}, conversation_id)
                    continue
                state["stop"] = False
                resume_state = _resume_state(state["resume_scope"])
                if state.get("current") and not state["current"].done():
                    await send_ws("error", {"where": "task.resume", "msg": "该对话已有任务在执行。"}, conversation_id)
                    continue
                requested_trace = str(msg.get("task_trace_id") or "")
                saved_trace = (resume_state.get("task") or {}).get("task_trace_id")
                if (not resume_state["messages"] or resume_state.get("reason") == "context_exhausted"
                        or (requested_trace and requested_trace != saved_trace)):
                    await send_ws("error", {"where": "task.resume", "msg": "该任务没有有效断点，可能已完成、过期或被后续任务替代；不会从头重复执行。"}, conversation_id)
                    await send_conversation_history(conversation_id)
                    continue
                task = dict(resume_state.get("task") or {"text": "继续", "provider_id": "default", "model": CFG.llm.chat_model, "approval_mode": "risk"})
                task["conversation_id"] = conversation_id
                state["current"] = asyncio.create_task(run_queue(state, task, resume=True))
                continue
            if mtype == "confirm":
                state = runtime_for(requested_id or selected_conversation_id)
                if state and state["pending_confirm"] and not state["pending_confirm"].done():
                    allowed = bool(msg.get("allow"))
                    trace = state.get("display_trace")
                    if trace and trace["status"] == "waiting":
                        trace["status"] = "running"
                        trace["current"] = "确认已收到，正在执行" if allowed else "操作已拒绝，正在整理结果"
                        for event in trace["events"]:
                            if event.get("status") == "waiting":
                                event["status"] = "running" if allowed else "stopped"
                        await _save_chat_in_thread(_save_conversations, DESKTOP_CONVERSATIONS)
                        await send_ws("task_trace", {"trace": trace}, state["conversation_id"])
                    state["pending_confirm"].set_result(allowed)
                continue
            if mtype == "stop":
                conversation_id = requested_id or selected_conversation_id
                state = runtime_for(conversation_id)
                if state is None:
                    await send_ws("stopped", {"summary": ""}, conversation_id)
                    continue
                state["stop"] = True
                for pending in state["pending_inputs"]:
                    pending_id = _client_input_id(pending.get("client_id"))
                    if pending_id and _ACTIVE_CLIENT_INPUTS.get(pending_id) == session_id:
                        _ACTIVE_CLIENT_INPUTS.pop(pending_id, None)
                        session_client_ids.discard(pending_id)
                state["pending_inputs"].clear()
                if state["pending_confirm"] and not state["pending_confirm"].done():
                    state["pending_confirm"].set_result(False)
                await _request_running_task_stop(state.get("current"))
                state["current"] = None
                await send_ws("stopped", {"summary": _resume_state(state["resume_scope"]).get("summary", "")}, conversation_id)
    except WebSocketDisconnect:
        pass
    finally:
        for state in runtimes.values():
            await _cancel_running_task(state.get("current"))
        for client_id in list(session_client_ids):
            if _ACTIVE_CLIENT_INPUTS.get(client_id) == session_id:
                _ACTIVE_CLIENT_INPUTS.pop(client_id, None)
        _DESKTOP_ATTACHMENT_RUNTIMES.pop(session_id, None)
        ACTIVE_WS.discard(ws)


# ============ 消息中心:收到消息→拟稿→通知前端→用户确认发送 ============

# 待发送的草稿暂存(id -> {source, to, draft, ...}),等用户确认
PENDING_REPLIES: dict[str, dict] = {}
_PENDING_REPLY_TTL_SECONDS = 24 * 3600
_MAX_PENDING_REPLIES = 200


def _prune_pending_replies(now: float | None = None) -> None:
    current = time.time() if now is None else now
    for reply_id, info in list(PENDING_REPLIES.items()):
        created_at = float(info.get("_created_at", current) or current)
        if current - created_at > _PENDING_REPLY_TTL_SECONDS:
            PENDING_REPLIES.pop(reply_id, None)
    if len(PENDING_REPLIES) > _MAX_PENDING_REPLIES:
        ordered = sorted(
            PENDING_REPLIES,
            key=lambda reply_id: float(PENDING_REPLIES[reply_id].get("_created_at", current) or current),
        )
        for reply_id in ordered[:-_MAX_PENDING_REPLIES]:
            PENDING_REPLIES.pop(reply_id, None)


def _memory_requested(guidance: str) -> bool:
    """Only explicit memory wording may add personal memory to a private draft."""
    import re
    text = str(guidance or "")
    return bool(re.search(r"(?:查|找|看|回忆|记得).{0,12}记忆|记忆.{0,12}(?:查|找|看|哪个|什么)", text))


async def _compose_reply_draft(info: dict, guidance: str = "") -> str:
    draft_llm = LLM
    _retain_llm(draft_llm)
    try:
        return await _compose_reply_draft_with_llm(draft_llm, info, guidance)
    finally:
        await _release_llm(draft_llm)


async def _compose_reply_draft_with_llm(llm: LLMClient, info: dict, guidance: str = "") -> str:
    source = info["source"]
    sender = info["sender"]
    text = info["incoming"]
    conversation = str(info.get("conversation") or info.get("reply_target") or "").strip()
    conversation_id = str(info.get("conversation_id") or "").strip()
    memory_note = ""
    if info.get("is_group"):
        group_note = "这是工作群消息。只依据当前消息和用户提供的提示拟稿，不读取全局长期记忆。"
    elif _memory_requested(guidance):
        group_note = "这是私聊消息。用户明确要求查个人记忆，必须使用下面的本地记忆检索结果；没有结果就明确说未查到，不要编造。"
        if MEMORY is not None:
            try:
                recalled = await MEMORY.recall(guidance, limit=8)
                memory_note = recalled or "（本地记忆没有匹配结果）"
            except Exception as e:
                memory_note = f"（本地记忆检索失败：{e}）"
        else:
            memory_note = "（本地记忆尚未就绪）"
    else:
        group_note = "不读取全局长期记忆。"
    prompt = (
        "你在帮用户回复飞书消息。"
        f"会话：{conversation or '未知'}；会话稳定标识：{conversation_id or '未知'}；发来消息的人是「{sender}」。\n"
        f"对方原消息：\n{text}\n\n"
        f"当前草稿或用户重拟提示：\n{guidance}\n\n"
        f"{group_note}\n本地记忆检索结果：\n{memory_note}\n"
        f"重拟提示是用户给你的内部操作说明，不是要发给对方的原文；禁止把“查记忆”“你查”等操作指令写进回复。\n"
        f"请以用户的口吻拟一条得体、简洁的中文回复，只输出回复正文，不要解释。"
    )
    image_path = Path(str(info.get("image_path") or ""))
    if image_path.is_file():
        image_format = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png",
                        ".gif": "gif", ".webp": "webp"}.get(image_path.suffix.lower(), "png")
        image_data = base64.b64encode(image_path.read_bytes()).decode()
        content = [
            {"type": "text", "text": prompt + "\n请结合对方发送的图片内容拟回复。"},
            {"type": "image_url", "image_url": {
                "url": f"data:image/{image_format};base64,{image_data}",
            }},
        ]
        msg = await llm.chat(
            [{"role": "user", "content": content}],
            model=CFG.llm.chat_model, temperature=0.4,
        )
    else:
        msg = await llm.chat([{"role": "user", "content": prompt}], temperature=0.4)
    return (msg.get("content") or "").strip()


async def draft_and_notify(source: str, sender: str, text: str, reply_target: str, meta: dict | None = None) -> None:
    """收到一条消息:让 LLM 拟一条回复稿,推给前端让用户确认/编辑/发送。
    source: 'feishu';sender: 谁发来的;text: 对方说的;reply_target: 回复时用的会话标识。
    """
    import uuid
    draft = ""
    info = {"source": source, "reply_target": reply_target, "sender": sender, "incoming": text, **(meta or {})}
    try:
        draft = await _compose_reply_draft(info)
    except Exception as e:
        draft = f"(拟稿失败:{e})"
    rid = uuid.uuid4().hex[:12]
    info["draft"] = draft
    info["_created_at"] = time.time()
    PENDING_REPLIES[rid] = info
    _prune_pending_replies()
    await broadcast({
        "type": "incoming_message",
        "data": {"id": rid, "source": source, "sender": sender,
                 "conversation": info.get("conversation") or reply_target,
                 "conversation_id": info.get("conversation_id", ""),
                 "is_group": bool(info.get("is_group")), "text": text, "draft": draft},
    })


# 远程会话的短期记忆(按会话 target 分别保留)
CHANNEL_HISTORY: dict[tuple[str, ...], list] = {}
CHANNEL_SHARED_CONTEXT: dict[tuple[str, ...], dict] = {}
_SHARED_TURN: contextvars.ContextVar[dict | None] = contextvars.ContextVar("shared_memory_turn", default=None)
# 远程会话的"断点":撞步数上限时存下完整上下文,用户回"继续"就能接着做
CHANNEL_RESUME: dict[tuple[str, ...], list] = {}
# 每个会话一把锁,防止同一会话的两条命令并发跑(会互相踩历史、抢屏幕)
CHANNEL_LOCKS: dict[tuple[str, ...], asyncio.Lock] = {}
CHANNEL_ACTIVE_TASKS: dict[tuple[str, ...], dict] = {}
CHANNEL_LAST_USED: dict[tuple[str, ...], float] = {}
_CHANNEL_STATE_TTL_SECONDS = 7 * 24 * 3600
_MAX_CHANNEL_STATES = 200
# 远程紧急闸:飞书发"锁定"即置 True,此后所有远程命令一律拒绝执行,
# 直到在**本机**(设置/桌面)解锁。手机丢了/号被盗时的止损开关。
CHANNEL_LOCKED = {"locked": False}
def _load_channel_lock() -> bool:
    try:
        data = json.loads(_CHANNEL_LOCK_PATH.read_text(encoding="utf-8"))
        return bool(data.get("locked")) if isinstance(data, dict) else False
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return False


def _save_channel_lock(locked: bool) -> None:
    payload = json.dumps({"locked": bool(locked)}, ensure_ascii=False)
    with _CHANNEL_LOCK_WRITE_LOCK:
        _CHANNEL_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = _CHANNEL_LOCK_PATH.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, _CHANNEL_LOCK_PATH)


# 待确认的远程操作:target -> asyncio.Future[bool]。隐私/装扩展这类需要你点头的,
# 会发一条飞书消息问你,你回"确认/取消"来放行或拒绝(远程没有按钮可点,用文字确认)。
CHANNEL_CONFIRM: dict[tuple[str, ...], "asyncio.Future[bool]"] = {}
# 当前正在处理的远程会话(供"发文件给我"这类工具知道往哪发)
CURRENT_CHANNEL: contextvars.ContextVar[tuple[str, ...] | None] = contextvars.ContextVar(
    "current_channel", default=None)


_CHANNEL_RECEIVE_ID_TYPE: contextvars.ContextVar[str] = contextvars.ContextVar("channel_receive_id_type", default="chat_id")
_CHANNEL_USER_ID: contextvars.ContextVar[str] = contextvars.ContextVar("channel_user_id", default="")


def _channel_parts(key: tuple[str, ...]) -> tuple[str, str, str]:
    return key[0], key[-1], key[1] if len(key) == 3 else ""


def _first_channel_identity(source: str) -> str:
    if source == "weixin":
        try:
            from backend.modules.chat import weixin_clawbot
            return weixin_clawbot.first_account_id()
        except Exception:
            return ""
    return str(next(iter(CFG.feishu.allowed_open_ids), "")) if source == "feishu" else ""


def _channel_identity(source: str) -> str:
    current = CURRENT_CHANNEL.get()
    if source == "weixin":
        identity = _channel_parts(current)[2] if current else ""
        if identity:
            return identity
        try:
            from backend.modules.chat import weixin_clawbot
            accounts = weixin_clawbot._accounts()
            return next(iter(accounts)) if len(accounts) == 1 else ""
        except Exception:
            return ""
    return _CHANNEL_USER_ID.get() if source == "feishu" else ""


def _channel_permission(source: str, identity: str = "") -> tuple[bool, int]:
    first_identity = _first_channel_identity(source)
    identity = identity or _channel_identity(source)
    if not first_identity and not identity:
        # Only identity-less internal callers use the legacy channel switch.
        return channel_control.permission(source)
    return channel_control.identity_permission(source, identity, first_identity)


def _channel_session_key(source: str, target: str, account_id: str = "") -> tuple[str, ...]:
    current = CURRENT_CHANNEL.get()
    if not account_id and current and current[0] == source and current[-1] == target:
        account_id = _channel_parts(current)[2]
    return (source, account_id, target) if source == "weixin" and account_id else (source, target)


def _feishu_send_type() -> dict:
    receive_id_type = _CHANNEL_RECEIVE_ID_TYPE.get()
    return {"receive_id_type": receive_id_type} if receive_id_type != "chat_id" else {}


def _weixin_send_account(key: tuple[str, ...] | None = None) -> dict:
    current = key or CURRENT_CHANNEL.get()
    account_id = _channel_parts(current)[2] if current else ""
    return {"account_id": account_id} if account_id else {}


def _owns_channel_schedule(task: dict) -> bool:
    current = CURRENT_CHANNEL.get()
    if not current:
        return True
    source, target, account_id = _channel_parts(current)
    return (task.get("channel") == source and task.get("channel_target") == target
            and task.get("channel_account_id", "") == account_id)


def _bind_schedule_destination(values: dict) -> dict:
    current = CURRENT_CHANNEL.get()
    if current:
        source, target, account_id = _channel_parts(current)
        values.update(channel=source, channel_target=target, channel_account_id=account_id,
                      channel_receive_id_type="chat_id" if source == "weixin" else "open_id",
                      channel_user_id=_CHANNEL_USER_ID.get())
    return values


def _chat_folder_candidates(home: Path, kind: str) -> list[Path]:
    names = {
        "desktop": ("Desktop", "桌面"),
        "documents": ("Documents", "文档"),
        "downloads": ("Downloads", "下载"),
    }[kind]
    paths: list[Path] = []

    def add(path: Path) -> None:
        if path not in paths:
            paths.append(path)

    for name in names:
        add(home / name)

    # Windows may redirect these folders to OneDrive or another location.
    # Keep the home-folder fallback above so tests and ordinary installations
    # remain deterministic when the shell-folder registry is unavailable.
    user_profile = os.environ.get("USERPROFILE", "")
    if os.name == "nt" and user_profile and home == Path(user_profile).expanduser():
        registry_names = {
            "desktop": "Desktop",
            "documents": "Personal",
            "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
        }
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                value = winreg.QueryValueEx(key, registry_names[kind])[0]
            add(Path(os.path.expandvars(str(value))))
        except (ImportError, OSError):
            pass

        for variable in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
            root = os.environ.get(variable)
            if root:
                add(Path(root) / names[0])
    return paths


def _clean_chat_file_query(value: str) -> str:
    value = str(value or "").strip()
    value = value.strip("\"'“”‘’「」『』")
    value = re.sub(
        r"(?is)^(?:\u8bf7(?:\u5e2e\u6211)?|\u5e2e\u6211|\u9ebb\u70e6(?:\u5e2e\u6211)?|"
        r"\u628a|\u5c06|\u53d1\u9001|\u53d1|\u4e0a\u4f20|\u627e\u5230|\u641c\u7d22|"
        r"\u67e5\u627e|\u7ed9\u6211)\s*",
        "",
        value,
    )
    value = re.sub(r"(?is)\s*(?:的)?(?:图片|照片|文件)\s*$", "", value).strip()
    return value.strip("\"'“”‘’「」『』")


def _resolve_chat_file_path(raw_path: str) -> Path:
    """Resolve a user file on this machine without guessing a different account.

    Natural requests commonly arrive as ``桌面 2609024.pdf`` rather than a
    literal path.  Search only the current user's standard folders and keep
    ambiguous fuzzy matches explicit so a similarly named file is never sent
    by accident.
    """
    text = _clean_chat_file_query(raw_path)
    if not text:
        raise FileNotFoundError("未提供文件路径")

    home = Path.home()
    direct = Path(text).expanduser()
    candidates: dict[Path, int] = {}

    def add(candidate: Path, rank: int = 0) -> None:
        try:
            candidate = candidate.resolve()
        except OSError:
            return
        if candidate.is_file() and rank < candidates.get(candidate, 99):
            candidates[candidate] = rank

    desktop_roots = _chat_folder_candidates(home, "desktop")
    document_roots = _chat_folder_candidates(home, "documents")
    download_roots = _chat_folder_candidates(home, "downloads")
    standard_roots = {
        "桌面": desktop_roots, "desktop": desktop_roots,
        "文档": document_roots, "文档文件夹": document_roots,
        "documents": document_roots, "document": document_roots,
        "下载": download_roots, "downloads": download_roots,
        "download": download_roots,
    }
    search_text = text
    search_roots: list[Path] | None = None
    recent_screen = bool(re.search(
        r"(?is)(?:\u521a\u624d|\u521a\u521a).*(?:\u622a\u5c4f|\u622a\u56fe|screen ?shot)",
        text,
    ))
    prefix = re.match(
        r"(?is)^(桌面|文档文件夹|文档|下载|desktop|documents?|downloads?)"
        r"(?:上|里|中)?(?:的)?\s*[:：]?\s*(.+)$",
        text,
    )
    if prefix:
        search_roots = standard_roots[prefix.group(1).casefold()]
        search_text = _clean_chat_file_query(prefix.group(2))

    add(direct)

    if recent_screen:
        temp_root = Path(tempfile.gettempdir())
        try:
            latest = max(
                temp_root.glob("xiaoa-screen-*.jpg"),
                key=lambda item: item.stat().st_mtime,
            )
        except (OSError, ValueError):
            latest = None
        if latest is not None:
            add(latest, 0)

    # A model may reuse a stale C:\\Users\\<name> path from an earlier machine.
    match = re.match(r"(?i)^[a-z]:[\\/]+Users[\\/]+[^\\/]+(?:[\\/]+(.*))?$", text)
    if match and match.group(1):
        add(home / Path(match.group(1).replace("\\", "/")))

    if not direct.is_absolute():
        roots = [Path.cwd(), *desktop_roots, *document_roots, *download_roots]
        if search_roots is None:
            roots.append(home)
        if search_roots is not None:
            roots = search_roots
            for root in roots:
                add(root / search_text)
        else:
            search_text = text
        for root in roots:
            add(root / search_text)

        # Search only the current user's standard folders. Exact names win;
        # normalized substring matches cover omitted extensions/separators.
        query = search_text.casefold()
        compact_query = re.sub(r"[\s._()\[\]-]+", "", query)
        if query:
            for root in roots:
                try:
                    for match_path in root.rglob("*"):
                        if not match_path.is_file():
                            continue
                        name = match_path.name.casefold()
                        compact_name = re.sub(r"[\s._()\[\]-]+", "", name)
                        stem = Path(name).stem
                        compact_stem = re.sub(r"[\s._()\[\]-]+", "", stem)
                        if name == query:
                            rank = 0
                        elif query in name or query in stem:
                            rank = 1
                        elif compact_query and (
                            compact_query in compact_name or compact_query in compact_stem
                        ):
                            rank = 2
                        else:
                            continue
                        add(match_path, rank)
                except OSError:
                    continue

    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) > 1:
        best = sorted(candidates, key=lambda path: (candidates[path], str(path).casefold()))
        listed = "；".join(
            f"{index}. {item.name}（{item.parent}）"
            for index, item in enumerate(best[:5], 1)
        )
        more = "" if len(best) <= 5 else f"；另有 {len(best) - 5} 个候选"
        raise ValueError(
            "找到多个匹配文件，请回复要发送的文件名或完整路径："
            f"{listed}{more}"
        )
    raise FileNotFoundError(
        f"当前电脑找不到文件：{text}。已按文件名片段检查当前用户目录、桌面、文档、下载和工作目录，"
        "不会猜测其他 Windows 用户名的路径。"
    )


def _prune_channel_state(now: float | None = None) -> None:
    current = time.time() if now is None else now
    ordered = sorted(CHANNEL_LAST_USED, key=CHANNEL_LAST_USED.get)
    for session_key in ordered:
        expired = current - CHANNEL_LAST_USED[session_key] > _CHANNEL_STATE_TTL_SECONDS
        overflow = len(CHANNEL_LAST_USED) > _MAX_CHANNEL_STATES
        lock = CHANNEL_LOCKS.get(session_key)
        if (not (expired or overflow) or (lock is not None and lock.locked())
                or session_key in CHANNEL_CONFIRM or session_key in CHANNEL_ACTIVE_TASKS):
            continue
        CHANNEL_LAST_USED.pop(session_key, None)
        CHANNEL_HISTORY.pop(session_key, None)
        CHANNEL_RESUME.pop(session_key, None)
        CHANNEL_LOCKS.pop(session_key, None)
        CHANNEL_SHARED_CONTEXT.pop(session_key, None)


def register_channel_tools(reg: ToolRegistry) -> None:
    """Register file/image delivery tools for remote channels and desktop chat."""

    async def send_file_to_chat(path: str, _return_to_hub: bool = False) -> str | dict:
        """把文件发到微信/飞书会话，或附加到当前桌面小A回复。"""
        cur = CURRENT_CHANNEL.get()
        try:
            resolved_path = _resolve_chat_file_path(path)
            if _return_to_hub:
                return _worker_media_payload(str(resolved_path), "file")
            if not cur:
                attachments = _DESKTOP_REPLY_ATTACHMENTS.get()
                if attachments is None:
                    return "当前没有可附加文件的桌面聊天任务。"
                attachments.append(_save_desktop_reply_attachment(resolved_path))
                return f"已把文件附加到当前小A回复：{resolved_path.name}"
            source, target, account_id = _channel_parts(cur)
            if source == "feishu":
                from backend.modules.chat import feishu_module
                await feishu_module.send_file(target, str(resolved_path), **_feishu_send_type())
                return f"已把文件发到飞书:{resolved_path}"
            if source == "weixin":
                from backend.modules.chat import weixin_clawbot
                await weixin_clawbot.send_file(
                    target, str(resolved_path), _CHANNEL_CONTEXT_TOKEN.get(), **_weixin_send_account(cur),
                )
                return f"已把文件发到微信:{resolved_path}"
            return f"{source} 渠道暂不支持发送文件。"
        except (FileNotFoundError, ValueError) as e:
            return str(e)
        except Exception as e:
            return f"发送失败:{e}"

    async def send_image_to_chat(path: str, _return_to_hub: bool = False) -> str | dict:
        """把图片发到微信/飞书会话，或附加到当前桌面小A回复。"""
        cur = CURRENT_CHANNEL.get()
        try:
            resolved_path = _resolve_chat_file_path(path)
            if _return_to_hub:
                return _worker_media_payload(str(resolved_path), "image")
            if not cur:
                attachments = _DESKTOP_REPLY_ATTACHMENTS.get()
                if attachments is None:
                    return "当前没有可附加图片的桌面聊天任务。"
                attachment = _save_desktop_reply_attachment(resolved_path)
                if not attachment["type"].startswith("image/"):
                    Path(attachment["path"]).unlink(missing_ok=True)
                    return f"文件不是可识别的图片格式：{resolved_path.name}"
                attachments.append(attachment)
                return f"已把图片附加到当前小A回复：{resolved_path.name}"
            source, target, account_id = _channel_parts(cur)
            if source == "feishu":
                from backend.modules.chat import feishu_module
                with resolved_path.open("rb") as f:
                    await feishu_module.send_image(target, f.read(), **_feishu_send_type())
                return f"已把图片发到飞书:{resolved_path}"
            if source == "weixin":
                from backend.modules.chat import weixin_clawbot
                with resolved_path.open("rb") as f:
                    await weixin_clawbot.send_image(
                        target, f.read(), _CHANNEL_CONTEXT_TOKEN.get(), **_weixin_send_account(cur),
                    )
                return f"已把图片发到微信:{resolved_path}"
            return f"{source} 渠道暂不支持发送图片。"
        except (FileNotFoundError, ValueError) as e:
            return str(e)
        except Exception as e:
            return f"发送失败:{e}"

    reg.register(Tool(
        name="send_file_to_chat",
        description=(
            "把本机文件发到当前微信/飞书会话，或作为附件回复到本地小A聊天。"
            "path 可以是完整路径，也可以直接填写用户原话中的文件描述（例如“桌面上的服务器信息.txt”）；"
            "优先使用完整路径，不确定时先查找本机文件。工具会在实际执行电脑上解析描述；"
            "多个候选时必须先让用户选择，不能一次发送全部候选。"
        ),
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "完整路径或用户原话中的文件描述，不要擅自改写成不存在的路径"}}, "required": ["path"]},
        handler=send_file_to_chat,
        side_effect=True,
    ))
    reg.register(Tool(
        name="send_image_to_chat",
        description=(
            "把本机图片发回当前微信/飞书会话，或附加到本轮桌面小A回复。"
            "path 可以是完整路径，也可以直接填写用户原话中的图片描述（例如“刚才截的屏”或“桌面上的截图.png”）；"
            "优先使用完整路径，不确定时先查找本机图片。多个候选时必须先让用户选择，不能擅自发送多份。"
        ),
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "完整路径或用户原话中的图片描述，不要擅自改写成不存在的路径"}}, "required": ["path"]},
        handler=send_image_to_chat,
        side_effect=True,
    ))


async def _channel_send(source: str, target: str, text: str) -> None:
    """把一段文字发回对应渠道,发送失败只记录不抛。"""
    try:
        if source == "feishu":
            from backend.modules.chat import feishu_module
            await feishu_module.send_message(target, text, **_feishu_send_type())
        elif source == "weixin":
            from backend.modules.chat import weixin_clawbot
            await weixin_clawbot.send_message(
                target, text, _CHANNEL_CONTEXT_TOKEN.get(), **_weixin_send_account(),
            )
        print(f"[chat] {source} 回复已提交发送接口", flush=True)
    except Exception as e:
        print(f"[chat] {source} 回复发送失败: {type(e).__name__}", flush=True)


_SECRET_ARGUMENT_RE = re.compile(r"(?:^key$|token|secret|password|passwd|api[_ -]?key|access[_ -]?key|private[_ -]?key)", re.I)
_IMPORTANT_ARGUMENT_RE = re.compile(r"(?:path|file|url|uri|amount|money|price|command|contact|target|name)", re.I)


def _channel_argument_summary(args: dict[str, object]) -> str:
    """为远程确认保留可判断风险的参数，并遮蔽凭据。"""
    if not isinstance(args, dict):
        return ""
    args = _redact_event_data(args)
    items = sorted(args.items(), key=lambda item: (0 if _IMPORTANT_ARGUMENT_RE.search(str(item[0])) else 1, str(item[0])))
    parts: list[str] = []
    for key, value in items:
        if _SECRET_ARGUMENT_RE.search(str(key)):
            rendered = "***"
        else:
            try:
                rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
            except (TypeError, ValueError):
                rendered = str(value)
            rendered = rendered.replace("\r", " ").replace("\n", " ")
            if len(rendered) > 200:
                rendered = rendered[:199] + "…"
        parts.append(f"{key}={rendered}")
    summary = "; ".join(parts)
    return summary if len(summary) <= 800 else summary[:799] + "…"


def _channel_confirmation_label(categories: set[str]) -> str:
    """把确认原因说具体，避免所有操作都显示成同一条笼统警告。"""
    if "financial" in categories:
        return "涉及资金操作"
    if "password" in categories:
        return "涉及读取或处理凭据"
    if "service_stop" in categories:
        return "将停止、禁用或结束指定服务/进程"
    if "delete" in categories:
        return "将删除指定文件或资源"
    if "uninstall" in categories:
        return "将卸载指定软件或服务"
    if "overwrite" in categories:
        return "将覆盖指定文件或配置"
    return "会对外部系统产生副作用"


def _channel_cloud_host() -> str:
    try:
        record = app_home() / "data" / "cloud-ssh" / "target.json"
        data = json.loads(record.read_text(encoding="utf-8"))
        return str(data.get("host") or "").strip()
    except (OSError, TypeError, ValueError):
        return ""


def _channel_reply_is_confirm(text: str) -> bool | None:
    """把远程回复解读成确认/取消。返回 True=确认,False=取消,None=不是确认语。"""
    t = (text or "").strip().lower()
    if t in ("确认", "confirm"):
        return True
    if t in ("取消", "拒绝", "不", "算了", "no", "n", "别"):
        return False
    return None


def _is_channel_stop_request(text: str) -> bool:
    value = re.sub(r"[\s，。！？!?.,;；：:]+", "", (text or "").strip().lower())
    return value in {
        "停止", "停止任务", "停止当前任务", "取消任务", "中止", "中止任务",
        "暂停", "暂停任务", "停一下", "停一下吧", "先停下", "先停下吧",
        "先暂停", "先暂停一下", "stop", "stoptask", "canceltask", "abort",
    }


def _shared_channel_actor(source: str, target: str, account_id: str) -> tuple[str, dict]:
    from backend.modules.chat import weixin_clawbot, feishu_module
    from backend.modules.memory import shared_libraries
    if source == "weixin":
        accounts = weixin_clawbot._accounts()
        selected = account_id or (next(iter(accounts)) if len(accounts) == 1 else "")
        config = accounts.get(selected)
        if not config or target not in config.get("allowed_user_ids", []):
            raise PermissionError("当前微信账号尚未授权访问项目共享记忆库")
        return ("user:" + target if config.get("ilink_user_id") else "account:" + selected,
                {"account_id": selected, "user_id": config.get("ilink_user_id", ""), "sender_id": target})
    if source == "feishu":
        app_id = feishu_module.event_app_id() or CFG.feishu.app_id
        sender = _CHANNEL_USER_ID.get()
        if not app_id or app_id != CFG.feishu.app_id or sender not in CFG.feishu.allowed_open_ids:
            raise PermissionError("当前飞书发送者尚未授权访问项目共享记忆库")
        return shared_libraries.feishu_identity(app_id, sender), {}
    raise PermissionError("当前渠道不支持项目共享记忆库")


async def _shared_hub_request(values: dict) -> dict:
    if not _hub_http_base():
        raise RuntimeError("未配置云端地址，不能回退读取本机项目副本")
    async with _hub_http_client(30) as client:
        response = await client.post(_hub_http_base() + "/api/weixin/memory/context",
                                     headers={"x-hub-token": CFG.role.hub_token}, json=values)
    if response.status_code != 200:
        raise RuntimeError("云端项目上下文不可用，请检查连接及云端版本；未回退本机")
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("云端未确认项目资料访问")
    return result


def _drop_shared_history(key: tuple[str, ...]) -> None:
    history = CHANNEL_HISTORY.get(key)
    if history is not None:
        history[:] = [item for item in history if not item.get("_shared_library")]
    CHANNEL_RESUME.pop(key, None)


async def _prepare_shared_context(source: str, target: str, text: str, account_id: str = "") -> str:
    from backend.modules.memory import shared_libraries
    if source not in {"weixin", "feishu"}:
        return text
    key = _channel_session_key(source, target, account_id)
    state = CHANNEL_SHARED_CONTEXT.get(key)
    if state and time.monotonic() >= state["expires_at"]:
        CHANNEL_SHARED_CONTEXT.pop(key, None)
        _drop_shared_history(key)
        state = None
    actor, route = "", {}
    try:
        actor, route = _shared_channel_actor(source, target, account_id)
    except PermissionError:
        if state:
            CHANNEL_SHARED_CONTEXT.pop(key, None)
            _drop_shared_history(key)
            raise
    if state and actor != state["actor"]:
        CHANNEL_SHARED_CONTEXT.pop(key, None)
        _drop_shared_history(key)
        state = None
    try:
        if CFG.role.is_worker:
            if source != "weixin":
                raise PermissionError("飞书消息请通过云端机器人处理，不使用本机项目副本")
            if not route:
                return text
            payload = await _shared_hub_request({**route, "text": text, "library_id": state["library_id"] if state else ""})
            if "context" not in payload:
                raise RuntimeError("云端版本不支持项目对话上下文")
            context = payload["context"]
        else:
            path = _weixin_memories().shared
            context = await asyncio.to_thread(shared_libraries.context_for_message, path, text, actor)
            if context is None and state:
                context = await asyncio.to_thread(shared_libraries.context_for_library, path, state["library_id"], actor, text)
    except Exception:
        CHANNEL_SHARED_CONTEXT.pop(key, None)
        _drop_shared_history(key)
        raise
    if context is None:
        return text
    if state and (not context.get("authorized") or context.get("library_id") != state["library_id"]
                  or context.get("revision") != state.get("revision")):
        _drop_shared_history(key)
    if context.get("authorized"):
        # Only an explicit invocation refreshes the fixed duration; a follow-up does not.
        expires = time.monotonic() + context["ttl_minutes"] * 60 if "user_text" in context else state["expires_at"]
        CHANNEL_SHARED_CONTEXT[key] = {"library_id": context["library_id"], "expires_at": expires,
                                       "actor": actor, "revision": context.get("revision")}
    else:
        CHANNEL_SHARED_CONTEXT.pop(key, None)
    _SHARED_TURN.set({**context, "actor": actor, "route": route, "source": source, "target": target, "account_id": account_id})
    return context.get("user_text", text)


async def _desktop_project_request(values: dict) -> dict:
    from backend.core import cloud_deploy
    from backend.modules.memory import shared_libraries
    if (app_home() / "data/cloud-ssh/target.json").is_file():
        return await asyncio.to_thread(cloud_deploy.desktop_project_memory, app_home(), values)
    if CFG.role.is_worker:
        raise RuntimeError("尚未配置云端管理连接，不能使用本机项目副本")
    if not (app_home() / "data/shared-libraries.sqlite3").is_file():
        return {"ok": True, "context": None}
    return await asyncio.to_thread(shared_libraries.desktop_context, _weixin_memories().shared, **values)


async def _prepare_desktop_shared(state: dict, text: str, history: list[dict], local_client: bool) -> list[dict]:
    """Desktop management uses the same catalog and tools as channel conversations."""
    _SHARED_TURN.set(None)
    if not local_client:
        return [row for row in history if not row.get("_shared_library")]
    previous = state.get("shared_context")
    if previous and time.monotonic() >= previous["expires_at"]:
        previous = None
        state.pop("shared_context", None)
    # Ordinary desktop turns must not block on SSH.  A cached call name is only
    # a routing hint; the cloud/local catalog still makes the authorization decision.
    if not previous and not _shared_route_matches(text):
        if (app_home() / "data/cloud-ssh/target.json").is_file() and not _SHARED_ROUTE_READY:
            # Do not stall ordinary tools on SSH, or silently save an unknown
            # project invocation to private memory during the first catalog load.
            _SHARED_TURN.set({"authorized": False, "prompt": (
                "项目共享库调用目录尚未加载成功，可以继续普通对话和任务。"
                "当前不能判断项目入口，不能读取或保存记忆、声称项目资料已入库或改存本机索引。"
                "涉及项目资料时，请提示稍后重试或在设置中刷新项目共享库。"
            )})
            return []
        return [row for row in history if not row.get("_shared_library")]
    try:
        payload = await _desktop_project_request({"text": text, "library_id": previous["library_id"] if previous else ""})
        context = payload["context"]
        if context is None:
            context = {"authorized": False, "prompt": (
                "项目调用入口不存在或已失效，请在设置中核对调用名称。"
                "不能声称已保存项目资料，也不能改存个人记忆或本机项目索引。"
            )}
    except (RuntimeError, OSError):
        # Keep chat available, but fail closed for every memory tool when routing is unknown.
        context = {"authorized": False, "prompt": (
            "项目共享记忆服务暂不可用，当前无法核验项目入口、读取或保存共享资料。"
            "可以继续普通对话，但不能声称已查询、保存或同步项目资料，不能改存普通长期记忆或本机索引。"
            "涉及项目资料时明确告知连接失败，请连接恢复后重新以调用名称开头。"
        )}
    if context and context.get("authorized"):
        expires = time.monotonic() + context["ttl_minutes"] * 60 if "user_text" in context else previous["expires_at"]
        state["shared_context"] = {"library_id": context["library_id"], "expires_at": expires}
        context = {**context, "source": "desktop", "expires_at": expires}
    else:
        state.pop("shared_context", None)
    _SHARED_TURN.set(context)
    tag = context.get("library_id", "denied") if context else None
    if previous and context and context.get("revision") != state.get("shared_revision"):
        history = []
    state["shared_revision"] = context.get("revision") if context else None
    return [row for row in history if row.get("_shared_library") == tag]


def _with_shared_prompt(prompt: str) -> str:
    context = _SHARED_TURN.get()
    return prompt + ("\n\n" + context["prompt"] if context else "")


def _shared_history_tag() -> dict:
    context = _SHARED_TURN.get()
    return {"_shared_library": context.get("library_id", "denied")} if context else {}


async def _shared_memory_action(**values) -> str:
    from backend.modules.memory import shared_libraries
    context = _SHARED_TURN.get()
    if not context or not context.get("authorized"):
        return "当前没有已授权的项目上下文，不能访问项目资料。"
    if context.get("source") == "desktop":
        if time.monotonic() >= context["expires_at"]:
            return "项目对话保持时间已结束，请重新以调用名称开头。"
        result = await _desktop_project_request({"text": "", "library_id": context["library_id"], **values})
        return result["reply"]
    key = _channel_session_key(context["source"], context["target"], context["account_id"])
    state = CHANNEL_SHARED_CONTEXT.get(key)
    if not state or state["library_id"] != context["library_id"] or time.monotonic() >= state["expires_at"]:
        return "项目对话保持时间已结束，请重新以调用名称开头再访问资料。"
    # Recheck authenticated channel and catalog ACL at every tool boundary.
    actor, route = _shared_channel_actor(context["source"], context["target"], context["account_id"])
    if actor != context["actor"]:
        return "当前账号身份已变更，已拒绝操作。"
    if CFG.role.is_worker:
        result = await _shared_hub_request({**route, "text": "", "library_id": context["library_id"],
                                           "operation": "action", **values})
        return result["reply"]
    return await asyncio.to_thread(shared_libraries.manage_context, _weixin_memories().shared,
                                   context["library_id"], actor, **values)


def _channel_agent_registry(base: ToolRegistry) -> ToolRegistry:
    if _SHARED_TURN.get() is None:
        return base
    registry = ToolRegistry()
    for name in base.names():
        # Project discussions must not be copied into personal long-term memory.
        if not name.startswith(("memory_", "project_")):
            registry.register(base.get(name))
    memory_tool_module.register_shared_tools(registry, _SHARED_TURN.get, _shared_memory_action)
    return registry


async def handle_channel_command(
    source: str, target: str, text: str, *, context_token: str = "", account_id: str = "", user_id: str = "",
) -> None:
    shared_token = _SHARED_TURN.set(None)
    token = _CHANNEL_CONTEXT_TOKEN.set(context_token)
    channel_token = CURRENT_CHANNEL.set(_channel_session_key(source, target, account_id))
    user_token = _CHANNEL_USER_ID.set(user_id or (target if source == "weixin" else ""))
    try:
        await _handle_channel_command(source, target, text, account_id)
    finally:
        _SHARED_TURN.reset(shared_token)
        _CHANNEL_CONTEXT_TOKEN.reset(token)
        CURRENT_CHANNEL.reset(channel_token)
        _CHANNEL_USER_ID.reset(user_token)


async def _channel_chat_only(source: str, target: str, text: str, account_id: str = "") -> None:
    session_key = _channel_session_key(source, target, account_id)
    CHANNEL_RESUME.pop(session_key, None)
    lock = CHANNEL_LOCKS.setdefault(session_key, asyncio.Lock())
    if lock.locked():
        await _channel_send(source, target, "正在处理上一条消息或等待旧任务收尾，请稍后再发。")
        return
    async with lock:
        try:
            text = await _prepare_shared_context(source, target, text, account_id)
        except Exception as exc:
            await _channel_send(source, target, _redact_event_data(f"项目资料暂时无法读取：{exc}"))
            return
        history = CHANNEL_HISTORY.setdefault(session_key, [])
        active = {
            "task": asyncio.current_task(), "stop": False, "remote_control": False,
            "channel_identity": _channel_identity(source),
        }
        CHANNEL_ACTIVE_TASKS[session_key] = active
        client = LLM
        _retain_llm(client)
        try:
            messages = [{"role": "system", "content": (
                "你是小A。当前渠道仅允许聊天，远程操控已关闭。可以回答问题、解释、翻译和撰写文字。"
                "不能访问、读取、修改本机或云服务器文件，不能执行命令、操作应用、创建定时任务、安装扩展。"
                "不要声称已经执行任何操作。需要实际操作时，请说明须由用户在本机设置开启本渠道远程操控。"
            )}]
            messages.extend({"role": item["role"], "content": item.get("content", "")}
                            for item in history if item.get("role") in ("user", "assistant"))
            messages.append({"role": "user", "content": text})
            messages, _, fitted = compact_for_limit(messages, max(1, int(CFG.agent.context_window * 0.75)))
            if not fitted:
                reply = "聊天上下文过长，请缩短消息后再试。"
            else:
                memory = await asyncio.to_thread(_memory_for_current_channel) if source == "weixin" and _SHARED_TURN.get() is None else None
                if memory is None and _SHARED_TURN.get() is None:
                    result = await client.chat(messages, model=CFG.llm.chat_model, tools=None, reasoning_effort="high")
                    reply = str(result.get("content") or "当前仅聊天，无法执行电脑或云端操作。")
                else:
                    registry = ToolRegistry()
                    if memory is not None and _SHARED_TURN.get() is None:
                        memory_tool_module.register_memory_tools(registry, lambda: memory)
                    registry = _channel_agent_registry(registry)
                    agent = Agent(
                        client, registry, CFG.agent, memory=memory if _SHARED_TURN.get() is None else None,
                        runtime_context=_with_shared_prompt("当前仅允许聊天、当前账号独立记忆及已授权项目共享记忆管理。远程操控关闭；不能操作电脑、读取文件、执行命令或创建定时任务。"),
                        abort_fn=lambda: bool(active["stop"]),
                    )
                    reply = ""
                    async for event in agent.run(text, history=list(history), model=CFG.llm.chat_model, reasoning_effort="high", approval_mode="full"):
                        if event.type in ("message", "message_end") and event.data.get("content"):
                            reply = event.data["content"]
                        elif event.type == "error":
                            raise RuntimeError(event.data.get("msg", "记忆聊天失败"))
                    reply = reply or "这次没有生成回复，请重试。"
            history.extend([{"role": "user", "content": text, **_shared_history_tag()},
                            {"role": "assistant", "content": reply, **_shared_history_tag()}])
            if len(history) > CFG.agent.max_history_messages:
                del history[:len(history) - CFG.agent.max_history_messages]
            await _channel_send(source, target, _redact_event_data(reply))
        except asyncio.CancelledError:
            await _channel_send(source, target, "已停止当前聊天。")
            raise
        except Exception as error:
            await _channel_send(source, target, _redact_event_data(f"聊天失败: {error}"))
        finally:
            if CHANNEL_ACTIVE_TASKS.get(session_key) is active:
                CHANNEL_ACTIVE_TASKS.pop(session_key, None)
            await _release_llm(client)


async def _handle_channel_command(source: str, target: str, text: str, account_id: str = "") -> None:
    """私聊小A(遥控):跑完整 agent(可操作电脑/答问),把结果发回该渠道。

    云端使用本地的 approval_mode="full" 策略执行普通操作；财务、隐私传输、
    第三方代码和不可信提示仍由 Agent 强制确认，远程锁定仍可冻结执行。
    另有紧急闸:发"锁定"立刻冻结所有远程执行,需在本机解锁。
    """
    stripped = text.strip()
    session_key = _channel_session_key(source, target, account_id)
    CHANNEL_LAST_USED[session_key] = time.time()
    _prune_channel_state()
    permission = _channel_permission(source, account_id)

    # —— 紧急闸:锁定/解锁(在获取会话锁之前处理,保证随时能锁) ——
    if stripped in ("锁定", "lock", "紧急锁定", "冻结"):
        CHANNEL_LOCKED["locked"] = True
        CHANNEL_RESUME.clear()
        for pending in tuple(CHANNEL_CONFIRM.values()):
            if not pending.done():
                pending.set_result(False)
        try:
            await asyncio.to_thread(_save_channel_lock, True)
        except Exception as e:
            await _channel_send(
                source, target,
                "远程遥控已在当前进程锁定，但锁定状态保存失败，"
                f"重启后可能失效: {type(e).__name__}",
            )
            return
        await _channel_send(source, target,
                            "已锁定:远程遥控已冻结,任何操作都不会执行。请到电脑上解锁后再用。")
        return
    if stripped in ("解锁", "unlock"):
        # 远程不允许解锁——否则盗号者发个"解锁"就绕过了。只能本机解。
        await _channel_send(source, target,
                            "出于安全,解锁只能在电脑上操作(设置页),远程无法解锁。")
        return

    if _is_channel_stop_request(stripped):
        active = CHANNEL_ACTIVE_TASKS.get(session_key)
        if active is None:
            await _channel_send(source, target, "当前没有正在执行的任务。")
            return
        active["stop"] = True
        pending = CHANNEL_CONFIRM.get(session_key)
        if pending is not None and not pending.done():
            pending.set_result(False)
        task = active.get("task")
        await _request_running_task_stop(task, grace_seconds=1.0)
        if task is not None and task.cancelled():
            await _channel_send(
                source, target,
                "已请求停止；当前工具步骤可能仍在后台收尾，完成前请勿重复发起同一操作。",
            )
        return

    # —— 待确认的操作:把"确认/取消"回复喂给挂起的 future(在会话锁之外,避免死锁) ——
    pending = CHANNEL_CONFIRM.get(session_key)
    if pending is not None and not pending.done():
        decision = _channel_reply_is_confirm(stripped)
        if decision is not None:
            pending.set_result(decision and _channel_permission(source)[0])
            return
        # 不是确认语:提示一下,不启动新任务(否则会和挂起的确认打架)
        await _channel_send(source, target, "上一个操作在等你确认,请回复「确认」或「取消」。")
        return

    if not permission[0]:
        _stop_channel_control(source, account_id or _channel_identity(source))
        await _channel_chat_only(source, target, text, account_id)
        return

    if CHANNEL_LOCKED["locked"]:
        await _channel_send(source, target,
                            "远程遥控当前处于锁定状态,已忽略。请在电脑上(设置页)解锁。")
        return

    lock = CHANNEL_LOCKS.setdefault(session_key, asyncio.Lock())
    # 忙碌期间普通消息作为插话交给当前任务,不另起任务抢占屏幕。
    if lock.locked():
        active = CHANNEL_ACTIVE_TASKS.get(session_key)
        if active is None:
            await _channel_send(source, target, "小A 正在处理上一条，请稍后再发。")
            return
        if session_key in CHANNEL_CONFIRM:
            await _channel_send(source, target, "上一个操作在等你确认，请回复「确认」或「取消」。")
            return
        project = CHANNEL_SHARED_CONTEXT.get(session_key)
        if project:
            try:
                actor, _ = _shared_channel_actor(source, target, account_id)
            except PermissionError:
                actor = ""
            if actor != project["actor"]:
                await _channel_send(source, target, "正在处理另一位成员的项目消息，请稍后再发。")
                return
        active["interjections"].append(stripped)
        active["interjection_history"].append(stripped)
        await _channel_send(
            source, target,
            "收到，已插入当前任务。小A 会在当前操作步骤结束后处理这条补充。",
        )
        return

    async with lock:
        try:
            text = await _prepare_shared_context(source, target, text, account_id)
        except Exception as exc:
            await _channel_send(source, target, _redact_event_data(f"项目资料暂时无法读取：{exc}"))
            return
        channel_decision: bool | None = None

        # Agent 负责对话授权；回调自身也只询问一次，拒绝和超时不会反复追问。
        async def channel_confirm(name: str, args: dict) -> bool:
            nonlocal channel_decision
            if CHANNEL_LOCKED["locked"] or _channel_permission(source) != permission:
                return False
            if channel_decision is not None:
                return channel_decision
            channel_decision = False
            categories = _remote_confirmation_categories(name, args)
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            CHANNEL_CONFIRM[session_key] = fut
            summary = _channel_argument_summary(args)
            host = _channel_cloud_host()
            target_text = f"云端 {host}" if host else "云端"
            reuse_text = ("本次任务只请求一次危险操作授权：确认后，本次任务后续明确要求的资金、凭据访问、"
                          "敏感信息外发、删除覆盖、停服卸载、第三方代码及技能等操作不再逐项询问；"
                          "硬性禁令和工具保护仍有效。取消后不执行需授权的步骤，请重新发送任务。")
            await _channel_send(
                source, target,
                f"小A 准备在{target_text}执行「{name}」：{_channel_confirmation_label(categories)}。"
                f"{('参数：' + summary + '。') if summary else ''}{reuse_text}"
                "确认请回「确认」，不做请回「取消」。")
            try:
                allowed = bool(await asyncio.wait_for(fut, timeout=300))
                channel_decision = allowed and not CHANNEL_LOCKED["locked"] and _channel_permission(source) == permission
                return channel_decision
            except asyncio.TimeoutError:
                await _channel_send(source, target, "等待确认超时(5分钟),已取消该操作。")
                return False
            finally:
                CHANNEL_CONFIRM.pop(session_key, None)

        approval_state = ConversationApproval()
        hist = CHANNEL_HISTORY.setdefault(session_key, [])
        reply, steps = "", []
        resume_notice = ""

        audit_trace = _AuditTrace(
            "channel:" + ":".join(session_key), uuid.uuid4().hex, "remote",
        )

        # 远程续做:用户回一句"继续"就从上次断点接着干(人不在电脑前也能推进长任务)
        resume = None
        if _SHARED_TURN.get() is None and text.strip() in ("继续", "接着做", "接着干", "continue", "go on"):
            resume = CHANNEL_RESUME.pop(session_key, None)
            if resume is None:
                await _channel_send(source, target, "没有待继续的任务(可能已做完)。")
                return
        active = {
            "task": asyncio.current_task(),
            "stop": False,
            "remote_control": True,
            "channel_identity": _channel_identity(source),
            "interjections": [],
            "interjection_history": [],
        }
        CHANNEL_ACTIVE_TASKS[session_key] = active
        channel_llm = LLM
        _retain_llm(channel_llm)
        channel_context_token = CURRENT_CHANNEL.set(session_key)
        channel_loop = asyncio.get_running_loop()

        foreground_notice_token = system_control.set_foreground_notice_callback(
            lambda active: _schedule_foreground_notice(channel_loop, active),
            on_escape=lambda: _stop_desktop_on_escape(channel_loop, active["task"], active),
        )
        error_message = ""
        completed_normally = False
        try:
            runtime_context = _LOCAL_CLOUD_RUNTIME_CONTEXT
            if CFG.role.is_hub:
                runtime_context = _HUB_RUNTIME_CONTEXT
            agent = Agent(
                channel_llm, _channel_agent_registry(REGISTRY), CFG.agent, confirm_fn=channel_confirm,
                memory=_memory_for_current_channel() if _SHARED_TURN.get() is None else None,
                skill_prompt_fn=_active_skill_prompt,
                runtime_context=_with_shared_prompt(runtime_context),
                abort_fn=lambda: CHANNEL_LOCKED["locked"] or active["stop"] or _channel_permission(source) != permission,
                interjection_fn=lambda: _take_channel_interjections(active),
                approval_state=approval_state,
            )
            try:
                async for ev in agent.run(
                    text, history=list(hist), resume_messages=resume,
                    model=CFG.llm.chat_model, reasoning_effort="high", approval_mode="full",
                ):
                    _normalize_event_punctuation(ev)
                    await asyncio.to_thread(audit_trace.consume, ev)
                    # 注意:流式回复走 message_end,两者都要收,否则远程回复会是空的
                    if ev.type in ("message", "message_end") and ev.data.get("content"):
                        reply = ev.data["content"]
                    elif ev.type == "tool_call":
                        name = str(ev.data.get("name") or "工具步骤")
                        steps.append(f"·{name}")
                    elif ev.type == "truncated":
                        # 存断点,并告诉用户回"继续"就能接着做(远程没有按钮可点)
                        CHANNEL_RESUME[session_key] = ev.data.get("resume_messages")
                        resume_notice = {
                            "context_limit": "上下文已接近模型上限",
                            "spinning": "检测到连续重复操作，已停止防止原地打转",
                            "step_limit": "任务达到防无限执行的最终安全上限",
                        }.get(ev.data.get("reason"), "任务已暂停")
                        steps.append(f"({resume_notice}，回我一句「继续」我就接着做)")
                    elif ev.type == "checkpoint":
                        checkpoint = ev.data.get("resume_messages")
                        if checkpoint:
                            CHANNEL_RESUME[session_key] = checkpoint
                        resume_notice = "模型服务中断，已保存完成步骤；继续前需核对外部状态"
                    elif ev.type == "aborted":
                        checkpoint = ev.data.get("resume_messages")
                        if checkpoint:
                            CHANNEL_RESUME[session_key] = checkpoint
                        reply = (
                            "已停止当前任务。已经执行的电脑或外部操作不会自动撤销，请核对实际状态；"
                            "需要接着做时回「继续」。"
                        )
                    elif ev.type == "done":
                        completed_normally = not ev.data.get("aborted") and not ev.data.get("truncated")
                    elif ev.type == "error":
                        error_message = str(ev.data.get("msg") or "模型暂时无法完成请求")
            except Exception as e:
                reply = f"处理出错:{e}"
        finally:
            if completed_normally and not active.get("stop") and not active.get("control_revoked") and await asyncio.to_thread(cad_module._close_task_owned_autocad):
                system_control.clear_owned_application("autocad")
            system_control.reset_foreground_notice_callback(foreground_notice_token)
            CURRENT_CHANNEL.reset(channel_context_token)
            if CHANNEL_ACTIVE_TASKS.get(session_key) is active:
                CHANNEL_ACTIVE_TASKS.pop(session_key, None)
            await _release_llm(channel_llm)
        if active.get("control_revoked") or _channel_permission(source) != permission:
            CHANNEL_RESUME.pop(session_key, None)
            reply = "本渠道远程操控已关闭，旧任务已停止，不能续做；已执行的步骤不会自动撤销。现在只允许聊天。"
        if not reply:
            reply = (
                f"这次没有完成回复：{error_message}"
                if error_message else "(已处理" + ("," + " ".join(steps) if steps else "") + ")"
            )
        # 暂停提示必须单独补上:它在 steps 里,而 steps 只在 reply 为空时才用,
        # 模型说了话就会把提示吞掉——那样远程根本不知道可以回"继续"。
        if session_key in CHANNEL_RESUME and "继续" not in reply:
            reply += f"\n\n({resume_notice or '任务已暂停'}，回我一句「继续」我就接着做。)"
        # 更新该会话历史(滑窗)
        hist.append({"role": "user", "content": text, **_shared_history_tag()})
        hist.extend({
            "role": "user", "content": f"[用户中途插话]\n{item}", **_shared_history_tag(),
        } for item in active["interjection_history"])
        hist.append({"role": "assistant", "content": reply, **_shared_history_tag()})
        if len(hist) > CFG.agent.max_history_messages:
            del hist[: len(hist) - CFG.agent.max_history_messages]
        # 远程渠道只发送一次最终文字回复；过程截图保留给 Agent 的本地识别和审计。
        await _channel_send(source, target, _redact_event_data(reply))


class SendReplyRequest(BaseModel):
    id: str
    text: str  # 用户确认/编辑后的最终文本


@app.post("/api/reply/regenerate")
async def api_reply_regenerate(req: SendReplyRequest) -> dict:
    _prune_pending_replies()
    info = PENDING_REPLIES.get(req.id)
    if info is None:
        return {"ok": False, "error": "草稿已过期或不存在"}
    guidance = str(req.text or "").strip()
    if len(guidance) > 4000:
        return {"ok": False, "error": "重拟提示不能超过 4000 个字符"}
    try:
        draft = await _compose_reply_draft(info, guidance)
        info["draft"] = draft
        return {"ok": True, "draft": draft}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/reply/send")
async def api_reply_send(req: SendReplyRequest) -> dict:
    _prune_pending_replies()
    info = PENDING_REPLIES.pop(req.id, None)
    if info is None:
        return {"ok": False, "error": "草稿已过期或不存在"}
    try:
        if info["source"] == "feishu":
            from backend.modules.chat import feishu_module
            await feishu_module.send_message(info["reply_target"], req.text)
        elif info["source"] == "weixin":
            from backend.modules.chat import weixin_clawbot
            await weixin_clawbot.send_message(
                info["reply_target"], req.text, info.get("context_token", ""),
                **({"account_id": info["account_id"]} if info.get("account_id") else {}),
            )
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/reply/ignore")
async def api_reply_ignore(req: SendReplyRequest) -> dict:
    _prune_pending_replies()
    PENDING_REPLIES.pop(req.id, None)
    return {"ok": True}


@app.post("/api/history/clear")
async def api_history_clear() -> dict:
    """兼容旧入口：永久清空所有对话的消息和本地历史。"""
    for conversation in DESKTOP_CONVERSATIONS.values():
        conversation["messages"] = []
        conversation["updated_at"] = int(time.time() * 1000)
    DESKTOP_HISTORY.clear()
    _save_conversations(DESKTOP_CONVERSATIONS)
    save_chat_history([])
    # 清空入口也必须删除所有可恢复断点，避免“聊天已清空”后还能继续旧任务。
    if _RESUME_STATES:
        _RESUME_STATES.clear()
        _persist_resume_states()
    _cleanup_attachments(remove_all_unreferenced=True)
    return {"ok": True}


@app.get("/api/attachments/{saved_name}")
async def api_attachment(saved_name: str):
    """Serve only files created in the local upload directory."""
    path = _saved_attachment_path(saved_name)
    if path is None:
        raise HTTPException(status_code=404, detail="附件不存在")
    return FileResponse(str(path))


@app.get("/api/attachments/{saved_name}/location")
async def api_attachment_location(saved_name: str) -> dict:
    """Return the local path so the desktop client can reveal the file in Explorer."""
    path = _saved_attachment_path(saved_name)
    if path is None:
        raise HTTPException(status_code=404, detail="附件不存在")
    return {"ok": True, "path": str(path.resolve())}


@app.get("/api/memory/export")
async def api_memory_export() -> dict:
    """导出记忆数据(向量记忆 + 对话历史),供 worker 每天拉回本地备份。
    走登录鉴权(auth_guard 已保护 /api/*),数据不外泄。
    """
    import base64
    try:
        files = await _collect_memory_export_files(include_shared=False)
        # Hub-token is execution capability, NOT permission to download all projects.
        files = {name: data for name, data in files.items()
                 if name != "shared-memory.sqlite3" and not name.startswith(("shared-library-", "shared-libraries"))}
        return {
            "ok": True,
            "files": {name: base64.b64encode(content).decode() for name, content in files.items()},
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "files": {}}


class MemoryImportRequest(BaseModel):
    files: dict[str, str] = {}
    request_id: str = Field(default="", max_length=100)
    device_id: str = Field(default="", max_length=100)


@app.post("/api/memory/import")
async def api_memory_import(req: MemoryImportRequest) -> dict:
    """接收 worker 上传的记忆快照,按 content_hash 去重合并进云端库(云端做真相源)。

    只增不删:云端已有的记忆一条都不动,worker 有而云端没有的补进来。
    鉴权走 hub_token(auth_guard 已放行),worker 没有网页密码。
    """
    if MEMORY is None:
        return {"ok": False, "error": "记忆尚未就绪"}
    encoded = (req.files or {}).get("memory.sqlite3")
    if not encoded:
        return {"ok": False, "error": "缺少 memory.sqlite3"}
    try:
        snapshot = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        return {"ok": False, "error": f"快照解码失败:{exc}"}
    try:
        result = await asyncio.to_thread(MEMORY.import_snapshot, snapshot)
        scoped_files = {name: base64.b64decode(content, validate=True) for name, content in req.files.items()
                        if name.startswith("weixin-memory-")}
        private = await asyncio.to_thread(_weixin_memories().import_files, scoped_files)
        result = {key: result[key] + private[key] for key in ("added", "skipped")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    request_id = req.request_id or str(uuid.uuid4())
    audit_saved = _memory_sync_audit().record(
        request_id=request_id, device_id=req.device_id, status="succeeded",
        added=result["added"], skipped=result["skipped"],
    )
    if not audit_saved:
        print("[memory-sync] 记忆已合并，但云端同步流水写入失败")
    print(f"[memory-sync] 已合并 worker 上传的记忆:新增 {result['added']} 条,"
          f"已存在 {result['skipped']} 条")
    return {"ok": True, "audit_recorded": audit_saved, **result}


@app.post("/api/memory/backup")
async def api_memory_backup(req: MemoryBackupRequest) -> dict:
    """Run one local memory backup immediately."""
    try:
        _apply_memory_backup_settings(req.directory, req.interval_minutes, req.keep_count)
        _rewrite_config()
        target = await _run_memory_backup_once()
        return {
            "ok": True, "path": str(target), "dir": str(CFG.memory_backup_dir), **BACKUP_STATUS,
        }
    except Exception as e:
        BACKUP_STATUS["last_error"] = f"{type(e).__name__}: {e}"
        return {"ok": False, "error": str(e), **BACKUP_STATUS}


def main() -> None:
    import uvicorn
    global _UVICORN_SERVER

    # 显式声明代理头处理,别靠默认值兜底(靠默认值时,"本机免密码"的正确性是隐性的、极脆)。
    # hub 挂在 nginx 后面:nginx 用 127.0.0.1 连本进程,若不解析 X-Forwarded-For,
    # 每个公网请求的 client.host 都会是 127.0.0.1 → _is_local 误判成本机 → 全网免密码。
    # forwarded_allow_ips 只信任本机 nginx 转发来的头,把 client.host 还原成真实公网 IP。
    tls = {}
    if cert := os.getenv("AGENT_TLS_CERT"):
        key = os.getenv("AGENT_TLS_KEY")
        if not key:
            raise RuntimeError("设置 AGENT_TLS_CERT 时必须同时设置 AGENT_TLS_KEY")
        tls = {"ssl_certfile": cert, "ssl_keyfile": key}
    config = uvicorn.Config(
        app, host=CFG.server.host, port=CFG.server.port,
        proxy_headers=True, forwarded_allow_ips="127.0.0.1",
        access_log=False, ws_max_size=32 * 1024 * 1024, **tls,
    )
    _UVICORN_SERVER = uvicorn.Server(config)
    _UVICORN_SERVER.run()


if __name__ == "__main__":
    main()
