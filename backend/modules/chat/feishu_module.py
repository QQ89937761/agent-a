"""飞书模块:用官方 lark-oapi 长连接接收消息(本机无需公网),交消息中心拟稿;并提供发送。

需要在飞书开放平台建"自建应用",拿到 App ID / App Secret,开通:
- 权限:im:message(收发消息)、im:resource(获取图片/文件资源)、im:message.group_at_msg 等
- 事件订阅:接收消息 v2.0(im.message.receive_v1),订阅方式选"长连接"

非配置/未装 lark-oapi 时,start_listener 跳过。
"""
from __future__ import annotations

import asyncio
import collections
import contextvars
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

from backend.core.config import Config, app_home

_client = None
_ws_thread = None
_ws_client = None
_ws_loop = None
_seen_msg_ids: "collections.OrderedDict[str, bool]" = None  # 去重:飞书会重投同一条消息
# 正在跑的任务要强引用住:run_coroutine_threadsafe 返回的 future 若被丢弃,
# 协程可能在 await 中途被 GC 回收而静默中断(语音消息就是这么丢的)。
_pending: set = set()
_last_open_id = ""
_last_app_id = ""
_LAST_OPEN_ID_FILE = "feishu-last-open-id.json"
_MAX_IMAGE_BYTES = 12 * 1024 * 1024
_PENDING_TTL_SECONDS = 10 * 60
_OPEN_ID_LOCK = threading.RLock()


def _last_open_id_path() -> Path:
    return app_home() / "data" / _LAST_OPEN_ID_FILE


def _read_saved_open_id(cfg: Config) -> str:
    try:
        state = json.loads(_last_open_id_path().read_text(encoding="utf-8"))
        value = state.get("open_id", "")
        app_id = getattr(cfg.feishu, "app_id", "")
        if state.get("app_id", app_id) != app_id:
            return ""
        return value if isinstance(value, str) and value in cfg.feishu.allowed_open_ids else ""
    except (OSError, ValueError, TypeError, AttributeError):
        return ""


def _update_open_id_state(update) -> None:
    with _OPEN_ID_LOCK:
        target = _last_open_id_path()
        try:
            state = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError, TypeError):
            state = {}
        update(state)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, target)


def _save_open_id(open_id: str) -> None:
    _update_open_id_state(lambda state: state.update(
        open_id=open_id, app_id=getattr(_cfg_ref.feishu, "app_id", "")))


def last_open_id(cfg: Config | None = None) -> str:
    """Read an actual received private message, never infer one from the whitelist."""
    cfg = cfg or _cfg_ref or _fallback_cfg()
    if (_last_app_id == getattr(cfg.feishu, "app_id", "")
            and _last_open_id in cfg.feishu.allowed_open_ids):
        return _last_open_id
    return _read_saved_open_id(cfg)


def _save_pending_open_id(open_id: str) -> None:
    def update(state):
        candidate = {"open_id": open_id, "app_id": getattr(_cfg_ref.feishu, "app_id", ""), "seen_at": time.time()}
        users = state.setdefault("pending_users", {})
        if not isinstance(users, dict):
            users = state["pending_users"] = {}
        users[open_id] = candidate
        state["pending_users"] = dict(sorted(users.items(), key=lambda item: item[1].get("seen_at", 0))[-100:])
        state["pending"] = candidate  # Compatibility with older settings clients.
    _update_open_id_state(update)


def pending_open_ids(cfg: Config | None = None) -> list[str]:
    cfg = cfg or _cfg_ref or _fallback_cfg()
    try:
        state = json.loads(_last_open_id_path().read_text(encoding="utf-8"))
        users = state.get("pending_users", {})
        candidates = list(users.values()) if isinstance(users, dict) else []
        candidates.append(state.get("pending", {}))
        result = []
        for candidate in sorted(candidates, key=lambda item: float(item.get("seen_at", 0)), reverse=True):
            open_id = candidate.get("open_id", "")
            if (candidate.get("app_id") == cfg.feishu.app_id
                    and isinstance(open_id, str) and open_id.startswith("ou_")
                    and 0 <= time.time() - float(candidate.get("seen_at", 0)) <= _PENDING_TTL_SECONDS
                    and open_id not in cfg.feishu.allowed_open_ids and open_id not in result):
                result.append(open_id)
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def pending_open_id(cfg: Config | None = None) -> str:
    users = pending_open_ids(cfg)
    return users[0] if len(users) == 1 else ""


def _spawn(coro, loop) -> None:
    """把协程丢到主事件循环跑,并持有 future 直到结束(防 GC 中断)。"""
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    _pending.add(fut)

    def _done(f):
        _pending.discard(f)
        try:
            f.result()  # 取一下异常,避免静默失败
        except Exception as e:
            print(f"[chat] 飞书任务异常: {type(e).__name__}: {e}")

    fut.add_done_callback(_done)


def available(cfg: Config) -> bool:
    if not (cfg.__dict__.get("feishu") and cfg.feishu.app_id and cfg.feishu.app_secret):
        return False
    try:
        import lark_oapi  # noqa: F401
        return True
    except ImportError:
        return False


def _fallback_cfg():
    """_cfg_ref 未初始化(worker 角色从不启动飞书监听)时,回落到全局配置。

    延迟导入避免循环依赖(server 导 feishu_module,feishu_module 不反向导 server)。
    """
    from backend.core.config import apply_env_overrides, load_config

    return apply_env_overrides(load_config())


def _get_client(cfg: Config):
    global _client
    if _client is None:
        import lark_oapi as lark
        cfg = cfg or _fallback_cfg()
        _client = lark.Client.builder() \
            .app_id(cfg.feishu.app_id) \
            .app_secret(cfg.feishu.app_secret) \
            .build()
    return _client


def reset_client() -> None:
    """丢弃旧凭据对应的 SDK 客户端；下次发送按新配置重建。"""
    global _client
    _client = None


def clear_pending_open_id(open_id: str = "") -> None:
    def update(state):
        if open_id:
            users = state.get("pending_users", {})
            if isinstance(users, dict):
                users.pop(open_id, None)
            if state.get("pending", {}).get("open_id") == open_id:
                state.pop("pending", None)
        else:
            state.pop("pending", None)
            state.pop("pending_users", None)
    try:
        _update_open_id_state(update)
    except (OSError, ValueError, TypeError):
        pass


def clear_last_open_id() -> None:
    """Remove the local recent-user record when the Feishu binding is removed."""
    global _last_open_id, _last_app_id
    _last_open_id = ""
    _last_app_id = ""
    try:
        _last_open_id_path().unlink()
    except FileNotFoundError:
        pass


async def send_message(chat_id: str, text: str, *, receive_id_type: str = "chat_id") -> None:
    """向某会话/用户发文本消息。

    receive_id_type 决定 chat_id 参数的语义:聊天用 "chat_id",给用户
    发提醒用 "open_id"(allowed_open_ids 存的是 open_id,传 chat_id 会被
    飞书以 230001 invalid receive_id 拒绝)。
    """
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    client = _get_client(_cfg_ref)
    req = CreateMessageRequest.builder() \
        .receive_id_type(receive_id_type) \
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": text}))
            .build()
        ).build()
    # lark-oapi 的同步调用放线程里执行,避免阻塞事件循环
    resp = await asyncio.get_running_loop().run_in_executor(
        None, client.im.v1.message.create, req)
    if not resp.success():
        # 常见:权限没开(im:message:send_as_bot)、chat_id 失效
        raise RuntimeError(f"飞书发送失败 code={resp.code} msg={resp.msg}")


async def send_image(chat_id: str, image_bytes: bytes, *, receive_id_type: str = "chat_id") -> None:
    """向某会话发一张图片(先上传拿 image_key,再发 msg_type=image)。"""
    import io

    from lark_oapi.api.im.v1 import (
        CreateImageRequest, CreateImageRequestBody,
        CreateMessageRequest, CreateMessageRequestBody,
    )

    client = _get_client(_cfg_ref)
    loop = asyncio.get_running_loop()

    up = CreateImageRequest.builder().request_body(
        CreateImageRequestBody.builder()
        .image_type("message")
        .image(io.BytesIO(image_bytes))
        .build()
    ).build()
    up_resp = await loop.run_in_executor(None, client.im.v1.image.create, up)
    if not up_resp.success() or not up_resp.data or not up_resp.data.image_key:
        raise RuntimeError(f"上传图片失败: code={up_resp.code} msg={up_resp.msg}")
    image_key = up_resp.data.image_key

    req = CreateMessageRequest.builder() \
        .receive_id_type(receive_id_type) \
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("image")
            .content(json.dumps({"image_key": image_key}))
            .build()
        ).build()
    response = await loop.run_in_executor(None, client.im.v1.message.create, req)
    if not response.success():
        raise RuntimeError(f"飞书媒体发送失败 code={response.code} msg={response.msg}")


async def send_file(chat_id: str, file_path: str, *, file_name: str | None = None, receive_id_type: str = "chat_id") -> None:
    """向某会话发一个文件(先上传拿 file_key,再发 msg_type=file)。"""
    import os

    from lark_oapi.api.im.v1 import (
        CreateFileRequest, CreateFileRequestBody,
        CreateMessageRequest, CreateMessageRequestBody,
    )

    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")
    name = os.path.basename(file_name or file_path)
    # 飞书按扩展名分类;不认识的一律 stream
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    ftype = ext if ext in ("opus", "mp4", "pdf", "doc", "xls", "ppt") else "stream"

    client = _get_client(_cfg_ref)
    loop = asyncio.get_running_loop()

    with open(file_path, "rb") as f:
        up = CreateFileRequest.builder().request_body(
            CreateFileRequestBody.builder()
            .file_type(ftype)
            .file_name(name)
            .file(f)
            .build()
        ).build()
        up_resp = await loop.run_in_executor(None, client.im.v1.file.create, up)
    if not up_resp.success() or not up_resp.data or not up_resp.data.file_key:
        raise RuntimeError(f"上传文件失败: code={up_resp.code} msg={up_resp.msg}")

    req = CreateMessageRequest.builder() \
        .receive_id_type(receive_id_type) \
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("file")
            .content(json.dumps({"file_key": up_resp.data.file_key}))
            .build()
        ).build()
    response = await loop.run_in_executor(None, client.im.v1.message.create, req)
    if not response.success():
        raise RuntimeError(f"飞书媒体发送失败 code={response.code} msg={response.msg}")


async def download_resource(message_id: str, file_key: str, res_type: str = "file") -> bytes:
    """下载消息里的资源(语音/图片/文件)。res_type: "file" 或 "image"。
    需要应用开通 im:resource(读取消息资源)权限。
    """
    from lark_oapi.api.im.v1 import GetMessageResourceRequest

    client = _get_client(_cfg_ref)
    req = GetMessageResourceRequest.builder() \
        .message_id(message_id) \
        .file_key(file_key) \
        .type(res_type) \
        .build()
    resp = await asyncio.get_running_loop().run_in_executor(
        None, client.im.v1.message_resource.get, req)
    if not resp.success():
        raise RuntimeError(f"下载资源失败 code={resp.code} msg={resp.msg}(可能缺 im:resource 权限)")
    data = resp.file
    # lark 返回的可能是 file-like,也可能已是 bytes
    if hasattr(data, "read"):
        data = data.read()
    if not data:
        raise RuntimeError("下载资源为空")
    return data


_cfg_ref: Config = None  # type: ignore
_EVENT_APP_ID = contextvars.ContextVar("feishu_event_app_id", default="")


def event_app_id() -> str:
    return _EVENT_APP_ID.get()


def _bound_callback(callback, app_id: str):
    async def dispatch(sender, text, chat_id, is_command=False):
        # A queued text/ASR/image callback from an old app may finish after settings
        # have changed. Never reinterpret it as a user of the new app.
        if _cfg_ref is None or getattr(_cfg_ref.feishu, "app_id", "") != app_id:
            return
        token = _EVENT_APP_ID.set(app_id)
        try:
            await _call(callback, sender, text, chat_id, is_command)
        finally:
            _EVENT_APP_ID.reset(token)
    return dispatch



def handle_event(data, on_message: Callable[..., None], loop) -> None:
    """处理一条飞书消息事件。抽成模块级函数,便于单测。

    分流规则:
      - 群消息(非 p2p)一律忽略,不打扰
      - 文字 → 直接当命令交给小A
      - 语音 → 下载音频 → ASR 转文字 → 当命令交给小A
      - 图片 → 下载图片 → 视觉模型转文字 → 当命令交给小A
      - 其它(文件…) → 明确回一句"还不支持",而不是没反应
    """
    global _seen_msg_ids, _last_open_id, _last_app_id
    try:
        msg = data.event.message
        chat_id = msg.chat_id
        # 去重:飞书长连接在处理慢时会重投同一条消息,用 message_id 挡掉重复
        mid = getattr(msg, "message_id", "") or ""
        if _seen_msg_ids is None:
            _seen_msg_ids = collections.OrderedDict()
        if mid:
            if mid in _seen_msg_ids:
                return  # 已处理过,忽略重投
            _seen_msg_ids[mid] = True
            while len(_seen_msg_ids) > 500:
                _seen_msg_ids.popitem(last=False)

        sender = getattr(data.event.sender.sender_id, "open_id", "对方") if data.event.sender else "对方"
        # chat_type: "p2p"=私聊机器人(当作命令小A),"group"=群(忽略)
        if (getattr(msg, "chat_type", "") or "") != "p2p":
            return
        allowed = set(getattr(_cfg_ref.feishu, "allowed_open_ids", []) or [])
        # 默认拒绝:没配白名单时,谁都不能遥控(以前是留空=放行所有,方向反了,
        # 等于公网上谁加到这个机器人都能操作你电脑)。要用飞书遥控,必须先在设置页
        # 核对待授权用户并点击授权。未授权用户只记录候选,不会执行消息。
        if not allowed or sender not in allowed:
            print(f"[chat] 已拒绝未授权的飞书用户: {sender}")
            try:
                if sender.startswith("ou_"):
                    _save_pending_open_id(sender)
            except OSError as exc:
                print(f"[chat] 飞书待授权用户状态保存失败: {exc}")
            _spawn(_notify(
                chat_id,
                "你尚未获得这个小A的使用授权。请联系管理员，在电脑的“设置 → 消息连接 → 飞书”中核对待授权用户并点击“授权此用户”。授权后请重新发送消息。",
            ), loop)
            return
        _last_open_id = sender
        _last_app_id = getattr(_cfg_ref.feishu, "app_id", "")
        try:
            _save_open_id(sender)
        except OSError as exc:
            # The message must still be handled when the optional status file is not writable.
            print(f"[chat] 飞书最近用户状态保存失败: {exc}")

        on_message = _bound_callback(on_message, getattr(_cfg_ref.feishu, "app_id", ""))
        mtype = getattr(msg, "message_type", "") or ""
        try:
            body = json.loads(msg.content or "{}")
        except json.JSONDecodeError:
            body = {}

        if mtype == "text":
            content = body.get("text", "")
            if content:
                _spawn(_call(on_message, sender, content, chat_id, True), loop)
        elif mtype == "audio":
            fk = body.get("file_key", "")
            if fk and mid:
                _spawn(_handle_audio(on_message, sender, chat_id, mid, fk), loop)
        elif mtype == "image":
            image_key = body.get("image_key", "")
            if image_key and mid:
                _spawn(_handle_image(
                    on_message, sender, chat_id, mid, image_key,
                    str(body.get("text") or body.get("caption") or "").strip(),
                ), loop)
            else:
                _spawn(_notify(chat_id, "图片消息缺少 image_key，无法下载。"), loop)
        else:
            _spawn(_notify(chat_id, f"我目前只能处理文字、语音和图片消息({mtype} 还不支持)。"), loop)
    except Exception as e:
        print(f"[chat] 飞书消息处理异常: {e}")


def start_listener(cfg: Config, on_message: Callable[[str, str, str], None], loop) -> str:
    """启动飞书长连接,收到消息回调 on_message(sender, text, chat_id)。"""
    global _ws_thread, _ws_client, _ws_loop, _cfg_ref
    _cfg_ref = cfg
    if not available(cfg):
        return "飞书监听未启动:未配置 App ID/Secret 或未安装 lark-oapi。"
    if _ws_thread and _ws_thread.is_alive():
        return "飞书监听已在运行。"

    app_id, app_secret = cfg.feishu.app_id, cfg.feishu.app_secret

    def _on_msg(data) -> None:
        if (_cfg_ref.feishu.app_id, _cfg_ref.feishu.app_secret) != (app_id, app_secret):
            return
        handle_event(data, on_message, loop)

    def run():
        global _ws_client, _ws_loop
        ws_client = None
        new_loop = None
        # lark 的 ws.client 有个模块级全局 loop,在库首次被导入时(主线程)就抓住了
        # uvicorn 的主循环,导致 start() 报 "event loop is already running"。
        # 解法:在本线程建新循环,并把 lark 那个全局 loop 覆盖成新的。
        try:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            _ws_loop = new_loop
            import lark_oapi as lark
            import lark_oapi.ws.client as lark_ws_client
            from lark_oapi.core.enum import LogLevel
            lark_ws_client.loop = new_loop  # 关键:覆盖库里抓住的主循环
            handler = lark.EventDispatcherHandler.builder("", "") \
                .register_p2_im_message_receive_v1(_on_msg) \
                .build()
            # 飞书长连接会定期自动重连,lark 把它打成红色 ERROR(no close frame received),
            # 其实下一步就 reconnect→connected,是正常心跳。用 log_level 压到 CRITICAL,
            # 只在真崩时才吭声(直接传参最稳,库启动时不会再把级别重置回 INFO)。
            ws_client = lark.ws.Client(app_id, app_secret,
                                       event_handler=handler, log_level=LogLevel.CRITICAL)
            _ws_client = ws_client
            ws_client.start()  # 阻塞
        except Exception as e:
            print(f"[chat] 飞书长连接线程退出: {e}")
        finally:
            if _ws_client is ws_client:
                _ws_client = None
                _ws_loop = None
            if new_loop is not None and not new_loop.is_closed():
                new_loop.close()

    _ws_thread = threading.Thread(target=run, daemon=True)
    _ws_thread.start()
    return "飞书长连接已启动。"


def stop_listener() -> None:
    """停止旧凭据的长连接，供保存设置时热重载。"""
    global _ws_thread, _ws_client, _ws_loop
    client, thread, loop = _ws_client, _ws_thread, _ws_loop
    _ws_client = None
    _ws_loop = None
    _ws_thread = None
    if client is not None:
        disconnect = getattr(client, "_disconnect", None)
        if callable(disconnect) and loop is not None and not loop.is_closed():
            try:
                future = asyncio.run_coroutine_threadsafe(disconnect(), loop)
                future.result(timeout=2)
            except Exception as exc:
                print(f"[chat] 飞书长连接断开失败: {exc}")
        for method in ("stop", "close"):
            try:
                callback = getattr(client, method, None)
                if callable(callback):
                    callback()
                    break
            except Exception as exc:
                print(f"[chat] 飞书长连接停止失败: {exc}")
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=2)


async def _call(fn, sender, content, chat_id, is_command=False):
    r = fn(sender, content, chat_id, is_command)
    if asyncio.iscoroutine(r):
        await r


# 语音和视觉钩子由 server 在启动时注入,避免本模块直接依赖具体能力实现。
_asr_hook = None
_vision_hook = None


def set_asr_hook(fn) -> None:
    global _asr_hook
    _asr_hook = fn


def set_vision_hook(fn) -> None:
    global _vision_hook
    _vision_hook = fn


async def _notify(chat_id: str, text: str) -> None:
    """给会话发一句提示,失败只打印。"""
    try:
        await send_message(chat_id, text)
    except Exception as e:
        print(f"[chat] 飞书提示发送失败: {e}")


async def _handle_audio(on_message, sender: str, chat_id: str,
                        message_id: str, file_key: str) -> None:
    """飞书语音消息:下载 → ASR 转文字 → 当作命令执行。"""
    if _asr_hook is None:
        await _notify(chat_id, "语音功能没启用(需要在设置页填腾讯云密钥)。")
        return
    try:
        audio = await download_resource(message_id, file_key, "file")
    except Exception as e:
        await _notify(chat_id, f"语音下载失败:{e}")
        return
    try:
        # 飞书语音是 opus 编码的 ogg,腾讯云 ASR 原生支持 ogg-opus
        text = (await _asr_hook(audio, "ogg-opus") or "").strip()
    except Exception as e:
        await _notify(chat_id, f"语音识别失败:{e}")
        return
    if not text:
        await _notify(chat_id, "没听清,能再说一遍吗?")
        return
    # 回显识别结果,让用户知道小A听到了什么(听错时好排查)
    await _notify(chat_id, f"🎤 听到:{text}")
    await _call(on_message, sender, text, chat_id, True)


def _image_format(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    return ""


async def _handle_image(on_message, sender: str, chat_id: str,
                        message_id: str, image_key: str, caption: str = "") -> None:
    """飞书图片消息:下载 → 视觉模型理解 → 当作命令执行。"""
    if _vision_hook is None:
        await _notify(chat_id, "图片识别功能没启用，请先在设置页配置支持图片的视觉模型。")
        return
    try:
        image = await download_resource(message_id, image_key, "image")
    except Exception as e:
        await _notify(
            chat_id,
            f"图片下载失败:{e}\n请确认飞书应用已开通 im:resource（获取与上传图片或文件资源）权限。",
        )
        return
    if len(image) > _MAX_IMAGE_BYTES:
        await _notify(chat_id, "图片超过 12MB，无法识别。请压缩后再发送。")
        return
    image_format = _image_format(image)
    if not image_format:
        await _notify(chat_id, "无法识别图片格式，目前支持 JPG、PNG、GIF 和 WebP。")
        return
    prompt = (
        f"用户随图说明：{caption}\n请结合说明识别图片内容，提取可见文字和完成请求所需的信息。"
        if caption else
        "请查看这张图片，准确描述其中的内容并提取可见文字，供后续助手继续处理。"
    )
    try:
        description = (await _vision_hook(prompt, image, image_format) or "").strip()
    except (asyncio.TimeoutError, TimeoutError):
        await _notify(chat_id, "图片识别超时，请稍后重试。")
        return
    except Exception as e:
        detail = str(e)
        lower = detail.lower()
        if any(word in lower for word in (
            "vision", "image_url", "image input", "support image", "multimodal",
            "不支持图片", "不支持图像", "视觉",
        )):
            await _notify(chat_id, f"当前视觉模型不支持图片，请在设置页更换模型。详情:{detail}")
        else:
            await _notify(chat_id, f"图片识别失败，请检查视觉模型和网关配置。详情:{detail}")
        return
    if not description:
        await _notify(chat_id, "视觉模型没有返回识别结果，请换一张图片或更换视觉模型。")
        return
    content = f"用户发来一张图片。"
    if caption:
        content += f"\n随图说明:{caption}"
    content += f"\n图片识别结果:\n{description}"
    await _call(on_message, sender, content, chat_id, True)
