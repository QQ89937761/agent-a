"""Standalone Tencent iLink/WeChat ClawBot private-message channel."""
from __future__ import annotations

import asyncio
import base64
import contextvars
from io import BytesIO
import json
import hashlib
import mimetypes
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote

import httpx

from backend.core.cloud_identity import write_private_text

API_ROOT = "https://ilinkai.weixin.qq.com"
CDN_ROOT = "https://novac2c.cdn.weixin.qq.com/c2c"
_CALLBACK: Callable[..., Awaitable[None]] | None = None
_ACCOUNTS_CHANGED: Callable[[dict], None] | None = None
_STATE_PATH: Path | None = None
_CONFIG: dict = {}
_LOGINS: dict[str, dict] = {}
_POLL_TASK: asyncio.Task | None = None
_DISPATCH_TASKS: set[asyncio.Task] = set()
_SEEN: dict[str, None] = {}
_CLIENT: httpx.AsyncClient | None = None
_ACCOUNT_TASKS: dict[str, asyncio.Task] = {}
_ACCOUNT_SEEN: dict[str, dict[str, None]] = {}
_ACCOUNT_CONTEXT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "weixin_account_context", default="",
)


def _client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10))
    return _CLIENT
_APP_CLIENT_VERSION = "65536"  # 1.0.0 encoded as 0x00MMNNPP
_MAX_MEDIA_BYTES = 50 * 1024 * 1024
_ASR_HOOK = None
_VISION_HOOK = None


def _log(message: str) -> None:
    print(message, flush=True)


def _common_headers() -> dict[str, str]:
    return {"iLink-App-Id": "bot", "iLink-App-ClientVersion": _APP_CLIENT_VERSION}


def _account_id(config: dict | None = None) -> str:
    config = config or _CONFIG
    return str(config.get("account_id") or config.get("ilink_bot_id") or config.get("ilink_user_id") or "default")


def _accounts() -> dict[str, dict]:
    accounts = _CONFIG.get("accounts")
    if isinstance(accounts, dict):
        return {str(key): value for key, value in accounts.items() if isinstance(value, dict)}
    # Keep the old state shape usable for tests and one-version upgrades.
    if _CONFIG.get("bot_token") or _CONFIG.get("connected"):
        return {_account_id(_CONFIG): _CONFIG}
    return {}


def first_account_id() -> str:
    return next(iter(_accounts()), "")


def _get_account(account_id: str = "") -> tuple[str, dict]:
    accounts = _accounts()
    account_id = account_id or _ACCOUNT_CONTEXT.get()
    if account_id:
        if account_id not in accounts:
            raise RuntimeError("指定微信账号不存在，请重新连接该账号")
        return account_id, accounts[account_id]
    if len(accounts) == 1:
        key = next(iter(accounts))
        return key, accounts[key]
    if len(accounts) > 1:
        raise RuntimeError("已连接多个微信账号，请指定接收账号")
    return "", _CONFIG


def _migrate_config() -> None:
    if isinstance(_CONFIG.get("accounts"), dict):
        return
    previous = dict(_CONFIG)
    accounts = {}
    if previous.get("bot_token"):
        key = _account_id(previous)
        previous["account_id"] = key
        if previous.get("last_user_id") and previous.get("last_context_token"):
            previous.setdefault("user_contexts", {})[previous["last_user_id"]] = previous["last_context_token"]
        accounts[key] = previous
    _CONFIG.clear()
    _CONFIG.update({"accounts": accounts, "allowed_user_ids": previous.get("allowed_user_ids", [])})


def _new_login_id(kind: str) -> str:
    for key, login in tuple(_LOGINS.items()):
        if time.time() - login.get("started", time.time()) > 600 and not login.get("credentials"):
            _LOGINS.pop(key, None)
    return f"{kind}-{uuid.uuid4().hex}"


def _login_key(kind: str, login_id: str = "") -> str:
    if login_id:
        login = _LOGINS.get(login_id, {})
        return login_id if login.get("kind", login_id.split("-", 1)[0]) == kind else ""
    return next((key for key in reversed(_LOGINS)
                 if _LOGINS[key].get("kind", key.split("-", 1)[0]) == kind), kind)


def _headers(*, authenticated: bool = True, account_id: str = "") -> dict[str, str]:
    headers = _common_headers()
    if not authenticated:
        return headers
    _, config = _get_account(account_id)
    uin = base64.b64encode(str(secrets.randbits(32)).encode()).decode()
    headers.update({"AuthorizationType": "ilink_bot_token", "X-WECHAT-UIN": uin,
                    "Content-Type": "application/json"})
    if config.get("bot_token"):
        headers["Authorization"] = f"Bearer {config['bot_token']}"
    return headers


def _save() -> None:
    if _STATE_PATH is not None:
        write_private_text(_STATE_PATH, json.dumps(_CONFIG, ensure_ascii=False, indent=2))



def _url(path: str, account_id: str = "") -> str:
    _, config = _get_account(account_id)
    return config.get("baseurl", "").rstrip("/") + path


def _qr_image_content(value: object) -> str:
    """Normalize iLink's QR payload so the settings page can always render it."""
    content = str(value or "").strip()
    if not content:
        return ""
    if content.startswith("data:image/"):
        return content
    try:
        decoded = base64.b64decode(content, validate=True)
    except (ValueError, TypeError):
        decoded = b""
    if decoded.startswith(b"\x89PNG\r\n\x1a\n"):
        return f"data:image/png;base64,{content}"
    if decoded.startswith(b"\xff\xd8\xff"):
        return f"data:image/jpeg;base64,{content}"

    import qrcode

    image = qrcode.make(content)
    output = BytesIO()
    image.save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _aes_cipher(raw: bytes, key: bytes) -> bytes:
    """Encrypt one iLink media blob with AES-128-ECB and PKCS#7 padding."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    padder = PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(raw) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _aes_decipher(raw: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decryptor.update(raw) + decryptor.finalize()
    unpadder = PKCS7(algorithms.AES.block_size).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def _media_key(value: object) -> bytes | None:
    """Decode iLink's raw, hex, base64(raw) and base64(hex) key encodings."""
    if not value:
        return None
    if isinstance(value, bytes):
        return value if len(value) == 16 else None
    text = str(value).strip()
    if len(text) == 32:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass
    padded = text + "=" * (-len(text) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            decoded = decoder(padded, validate=True) if decoder is base64.b64decode else decoder(padded)
        except (ValueError, TypeError):
            continue
        if len(decoded) == 16:
            return decoded
        if len(decoded) == 32:
            try:
                return bytes.fromhex(decoded.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                continue
    return None


def _media_url(media: dict) -> str:
    for name in ("full_url", "download_url", "url"):
        direct_url = str(media.get(name) or "").strip()
        if direct_url:
            return direct_url
    encrypted = str(media.get("encrypt_query_param") or "").strip()
    if not encrypted:
        raise RuntimeError("微信媒体缺少下载参数")
    return f"{CDN_ROOT}/download?encrypted_query_param={quote(encrypted, safe='')}"


def _item_type(item: dict) -> int | None:
    value = item.get("type")
    try:
        if value is not None and str(value).strip():
            return int(value)
    except (TypeError, ValueError):
        pass
    for kind, item_type in (("text_item", 1), ("voice_item", 3),
                            ("image_item", 2), ("file_item", 4),
                            ("video_item", 5)):
        if isinstance(item.get(kind), dict):
            return item_type
    return None


def _item_payload(item: dict, kind: str) -> dict:
    payload = item.get(kind)
    if isinstance(payload, dict):
        return payload
    # Some iLink responses put the media fields directly on the item.
    return item


def _media_format(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    return ""


def _media_payload(item: dict) -> tuple[dict, dict, str]:
    container = item if isinstance(item, dict) else {}
    for kind in ("image_item", "voice_item", "file_item", "video_item"):
        candidate = container.get(kind)
        if isinstance(candidate, dict):
            container = candidate
            break
    media = container.get("media")
    if not isinstance(media, dict) and isinstance(item.get("media"), dict):
        media = item["media"]
        container = item
    if not isinstance(media, dict):
        raise RuntimeError("微信媒体消息缺少 media 字段")
    return container, media, "嵌套媒体字段" if container is not item else "消息媒体字段"


async def _download_media(item: dict, *, top_level_key: str = "") -> bytes:
    container, media, field_source = _media_payload(item)
    url_source = "full_url" if media.get("full_url") else (
        "download_url" if media.get("download_url") else (
            "url" if media.get("url") else "encrypt_query_param"))
    try:
        response = await _client().get(_media_url(media), timeout=httpx.Timeout(40, connect=10))
        response.raise_for_status()
    except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException) as exc:
        raise TimeoutError("微信媒体下载超时，请检查网络后重新发送图片或文件") from exc
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        raise RuntimeError(f"微信媒体下载失败({url_source}): {detail}") from exc
    raw = response.content
    if len(raw) > _MAX_MEDIA_BYTES * 2:
        raise RuntimeError("微信媒体超过允许大小")
    key_values = (
        ("调用方", top_level_key),
        ("aeskey", container.get("aeskey")),
        ("aes_key", container.get("aes_key")),
        ("media.aeskey", media.get("aeskey")),
        ("media.aes_key", media.get("aes_key")),
    )
    decoded = raw
    key_source = "无"
    decrypt_error = None
    for source, value in key_values:
        key = _media_key(value)
        if not key:
            continue
        try:
            decoded = _aes_decipher(raw, key)
            key_source = source
            break
        except Exception as exc:
            decrypt_error = exc
    else:
        if decrypt_error and not _media_format(raw):
            detail = str(decrypt_error).strip() or type(decrypt_error).__name__
            raise RuntimeError(f"微信媒体解密失败({field_source}): {detail}") from decrypt_error
        if decrypt_error:
            _log("[chat] 微信媒体解密失败但原始内容已是可识别格式，按原始内容继续")
    _log(
        "[chat] 微信媒体下载完成: "
        f"status={response.status_code}, content_type={response.headers.get('content-type', '')}, "
        f"raw_size={len(raw)}, decoded_size={len(decoded)}, source={field_source}, "
        f"url={url_source}, key={key_source}, format={_media_format(decoded) or 'unknown'}"
    )
    if len(decoded) > _MAX_MEDIA_BYTES:
        raise RuntimeError("微信媒体超过 50MB 限制")
    return decoded


def _inbox_dir() -> Path:
    """Return the private local inbox used for media received from WeChat."""
    parent = _STATE_PATH.parent if _STATE_PATH is not None else Path.cwd() / "data"
    return parent / "weixin-inbox"


def _safe_media_name(name: object, fallback: str) -> str:
    """Keep an inbound filename readable without allowing path traversal."""
    candidate = Path(str(name or "")).name
    candidate = re.sub(r"[^\w. ()\u4e00-\u9fff-]", "_", candidate).strip(" .")
    return candidate or fallback


def _save_inbound_media(raw: bytes, name: object, fallback: str) -> Path:
    if not raw:
        raise RuntimeError("微信媒体内容为空")
    inbox = _inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_media_name(name, fallback)
    path = inbox / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:10]}-{safe_name}"
    path.write_bytes(raw)
    return path


async def _upload_media(raw: bytes, user_id: str, media_type: int, account_id: str = "") -> dict:
    if not raw:
        raise RuntimeError("不能发送空文件")
    if len(raw) > _MAX_MEDIA_BYTES:
        raise RuntimeError("微信媒体超过 50MB 限制")
    stage = "加密媒体"
    try:
        key = secrets.token_bytes(16)
        encrypted = _aes_cipher(raw, key)
        filekey = uuid.uuid4().hex
        _log(f"[chat] 微信媒体上传开始: type={media_type}, size={len(raw)}")

        stage = "获取上传地址"
        upload = await _post("/ilink/bot/getuploadurl", {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": user_id,
            "rawsize": len(raw),
            "rawfilemd5": hashlib.md5(raw).hexdigest(),
            "filesize": len(encrypted),
            "no_need_thumb": True,
            "aeskey": key.hex(),
        }, **({"account_id": account_id} if account_id else {}))
        upload_url = str(upload.get("upload_full_url") or "").strip()
        if not upload_url:
            upload_param = str(upload.get("upload_param") or "").strip()
            if not upload_param:
                raise RuntimeError("微信没有返回媒体上传地址")
            upload_url = (
                f"{CDN_ROOT}/upload?encrypted_query_param={quote(upload_param, safe='')}"
                f"&filekey={quote(filekey, safe='')}"
            )

        stage = "上传媒体到 CDN"
        response = await _client().post(
            upload_url, content=encrypted,
            headers={"Content-Type": "application/octet-stream"},
            timeout=httpx.Timeout(60, connect=10),
        )
        response.raise_for_status()
        encrypted_param = str(response.headers.get("x-encrypted-param") or "").strip()
        if not encrypted_param:
            try:
                encrypted_param = str(response.json().get("encrypt_query_param") or "").strip()
            except (ValueError, json.JSONDecodeError):
                encrypted_param = ""
        if not encrypted_param:
            raise RuntimeError("微信媒体上传成功但没有返回下载参数")
        _log(f"[chat] 微信媒体已上传: type={media_type}, size={len(raw)}")
        # Current iLink clients use base64(hex(key)) in the media reference.
        return {
            "encrypt_query_param": encrypted_param,
            "aes_key": base64.b64encode(key.hex().encode("ascii")).decode("ascii"),
            "encrypt_type": 1,
            "cipher_size": len(encrypted),
            "raw_size": len(raw),
        }
    except Exception as exc:
        _log(f"[chat] 微信媒体上传失败: type={media_type}, stage={stage}, error={type(exc).__name__}")
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"微信媒体上传失败({stage}): {exc}") from exc


async def begin_login() -> dict:
    tokens = [str(config["bot_token"]) for config in _accounts().values() if config.get("bot_token")][-10:]
    response = await _client().post(
        f"{API_ROOT}/ilink/bot/get_bot_qrcode", params={"bot_type": "3"},
        headers=_headers(authenticated=False), json={"local_token_list": tokens},
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("qrcode") or not data.get("qrcode_img_content"):
        raise RuntimeError("WeChat did not return a usable QR code")
    login_id = _new_login_id("local")
    _LOGINS[login_id] = {
        "qrcode": data["qrcode"], "redirect_host": "", "started": time.time(), "kind": "local",
    }
    image = _qr_image_content(data["qrcode_img_content"])
    if not image:
        raise RuntimeError("WeChat returned an invalid QR image")
    return {"qrcode": data["qrcode"], "image": image, "login_id": login_id}


async def login_status(login_id: str = "") -> dict:
    key = _login_key("local", login_id)
    login = _LOGINS.get(key)
    if not login:
        return {"connected": False, "login_status": "expired" if login_id else "not_started", "expired": bool(login_id)}
    if login.get("account_id"):
        return {**status(), "login_status": "connected", "account_id": login["account_id"]}
    base = (login.get("redirect_host") or API_ROOT).rstrip("/")
    if not base.startswith("https://"):
        base = "https://" + base
    response = await _client().get(f"{base}/ilink/bot/get_qrcode_status",
                                 params={"qrcode": login["qrcode"]},
                                 headers=_headers(authenticated=False),
                                 timeout=httpx.Timeout(40, connect=10))
    response.raise_for_status()
    if _LOGINS.get(key) is not login:
        return {"connected": False, "login_status": "expired", "expired": True}
    data = response.json()
    state = data.get("status", "wait")
    if state == "scaned_but_redirect" and data.get("redirect_host"):
        login["redirect_host"] = data["redirect_host"]
    if state == "confirmed":
        if not all(data.get(field) for field in ("bot_token", "ilink_bot_id", "baseurl")):
            _LOGINS.pop(key, None)
            return {"connected": False, "login_status": "error", "expired": True,
                    "error": "微信确认响应缺少必要登录凭据"}
        _migrate_config()
        user_id = str(data.get("ilink_user_id") or "").strip()
        account_id = str(data["ilink_bot_id"])
        accounts = _CONFIG["accounts"]
        previous = accounts.get(account_id, {})
        account = {
            "account_id": account_id,
            **{field: data.get(field, "") for field in
               ("bot_token", "ilink_bot_id", "baseurl", "ilink_user_id")},
            "remark": str(previous.get("remark", ""))[:80],
            "allowed_user_ids": sorted(set(previous.get("allowed_user_ids", _CONFIG.get("allowed_user_ids", []) if not accounts else [])) | ({user_id} if user_id else set())),
            "get_updates_buf": "", "last_user_id": "", "last_context_token": "",
            "user_contexts": {}, "seen_message_ids": [], "connected": True,
        }
        old_task = _ACCOUNT_TASKS.pop(account_id, None)
        if old_task:
            old_task.cancel()
        accounts[account_id] = account
        if _ACCOUNTS_CHANGED is not None:
            try:
                await asyncio.to_thread(_ACCOUNTS_CHANGED, _accounts())
            except Exception:
                if previous:
                    accounts[account_id] = previous
                else:
                    accounts.pop(account_id, None)
                await start_listener()
                raise
        _ACCOUNT_SEEN.pop(account_id, None)
        _CONFIG["allowed_user_ids"] = sorted({uid for cfg in accounts.values() for uid in cfg.get("allowed_user_ids", [])})
        login["account_id"] = account_id
        _save()
        if old_task:
            await asyncio.gather(old_task, return_exceptions=True)
        await start_listener()
        return {**status(), "login_status": "connected", "account_id": account_id}
    if state in {"expired", "need_verifycode", "verify_code_blocked", "error", "binded_redirect"}:
        _LOGINS.pop(key, None)
    return {"connected": False,
            "login_status": "already_bound" if state == "binded_redirect" else state,
            "expired": key not in _LOGINS}


async def begin_cloud_login(qr_data: dict | None = None) -> dict:
    if qr_data is None:
        response = await _client().post(
            f"{API_ROOT}/ilink/bot/get_bot_qrcode", params={"bot_type": "3"},
            headers=_headers(authenticated=False), json={"local_token_list": []},
        )
        response.raise_for_status()
        data = response.json()
    else:
        data = qr_data
    if not data.get("qrcode") or not data.get("qrcode_img_content"):
        raise RuntimeError("微信没有返回可用二维码")
    login_id = _new_login_id("cloud")
    _LOGINS[login_id] = {"qrcode": data["qrcode"], "redirect_host": "", "started": time.time(), "kind": "cloud"}
    image = _qr_image_content(data["qrcode_img_content"])
    if not image:
        raise RuntimeError("微信返回的二维码图片无效")
    return {"qrcode": data["qrcode"], "image": image, "login_id": login_id}


async def cloud_login_status(login_id: str = "") -> dict:
    key = _login_key("cloud", login_id)
    login = _LOGINS.get(key)
    if not login:
        return {"connected": False, "login_status": "not_started"}
    if login.get("credentials"):
        return {"connected": True, "login_status": "connected", "_credentials": login["credentials"]}
    base = (login.get("redirect_host") or API_ROOT).rstrip("/")
    if not base.startswith("https://"):
        base = "https://" + base
    response = await _client().get(
        f"{base}/ilink/bot/get_qrcode_status", params={"qrcode": login["qrcode"]},
        headers=_headers(authenticated=False),
        timeout=httpx.Timeout(40, connect=10),
    )
    response.raise_for_status()
    if _LOGINS.get(key) is not login:
        return {"connected": False, "login_status": "expired", "expired": True}
    data = response.json()
    state = data.get("status", "wait")
    if state == "scaned_but_redirect" and data.get("redirect_host"):
        login["redirect_host"] = data["redirect_host"]
    if state == "confirmed":
        if not all(data.get(key) for key in ("bot_token", "ilink_bot_id", "baseurl")):
            _LOGINS.pop(key, None)
            return {"connected": False, "login_status": "error", "expired": True,
                    "error": "微信确认响应缺少必要登录凭据"}
        credentials = {key: data.get(key, "") for key in
                       ("bot_token", "ilink_bot_id", "baseurl", "ilink_user_id")}
        login["credentials"] = credentials
        return {"connected": True, "login_status": "connected", "_credentials": credentials}
    if state in {"expired", "need_verifycode", "verify_code_blocked", "error", "binded_redirect"}:
        _LOGINS.pop(key, None)
    return {"connected": False,
            "login_status": "already_bound" if state == "binded_redirect" else state,
            "expired": key not in _LOGINS}


def complete_cloud_login(login_id: str = "") -> None:
    """Discard credentials only after the cloud state file was updated successfully."""
    _LOGINS.pop(_login_key("cloud", login_id), None)


def status() -> dict:
    accounts = _accounts()
    items = []
    allowed = set(_CONFIG.get("allowed_user_ids", []) or [])
    for account_id, config in accounts.items():
        ids = [str(item) for item in config.get("allowed_user_ids", []) if str(item).strip()]
        allowed.update(ids)
        items.append({
            "account_id": account_id,
            "ilink_user_id": str(config.get("ilink_user_id", "")),
            "remark": str(config.get("remark", ""))[:80],
            "connected": bool(config.get("connected") and config.get("bot_token")),
            "allowed_user_ids": ids,
        })
    legacy_connected = bool(_CONFIG.get("connected") and _CONFIG.get("bot_token"))
    return {
        "connected": bool(items and any(item["connected"] for item in items)) or legacy_connected,
        "ilink_user_id": items[0]["ilink_user_id"] if len(items) == 1 else "",
        "allowed_user_ids": sorted(allowed),
        "accounts": items,
    }


def configure(allowed_user_ids: list[str]) -> dict:
    values = sorted(set(item.strip() for item in allowed_user_ids if item.strip()))
    for config in _accounts().values():
        config["allowed_user_ids"] = list(values)
        contexts = config.get("user_contexts", {})
        config["user_contexts"] = {uid: token for uid, token in contexts.items() if uid in values}
    _CONFIG["allowed_user_ids"] = values
    _save()
    return status()


def set_account_remark(account_id: str, remark: str) -> dict:
    """Persist a short human label without changing credentials or allowlists."""
    account_id = str(account_id).strip()
    if not account_id or len(account_id) > 256:
        raise ValueError("微信账号标识无效")
    if not isinstance(remark, str) or len(remark) > 80:
        raise ValueError("账号备注不能超过 80 个字符")
    _, config = _get_account(account_id)
    config["remark"] = remark.strip()
    _save()
    return status()


async def logout(account_id: str = "") -> dict:
    global _CONFIG
    if account_id:
        _, config = _get_account(account_id)
        config["connected"] = False
        task = _ACCOUNT_TASKS.pop(account_id, None)
        tasks = [item for item in _DISPATCH_TASKS if getattr(item, "weixin_account_id", "") == account_id]
        if task:
            tasks.append(task)
        for item in tasks:
            item.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        _CONFIG.get("accounts", {}).pop(account_id, None)
        _ACCOUNT_SEEN.pop(account_id, None)
        for key, login in tuple(_LOGINS.items()):
            if login.get("account_id") == account_id:
                _LOGINS.pop(key, None)
        _CONFIG["allowed_user_ids"] = sorted({uid for cfg in _accounts().values() for uid in cfg.get("allowed_user_ids", [])})
        _save()
        return status()
    await stop_listener()
    # A single settings action must invalidate both local and in-progress cloud
    # logins; otherwise an old cloud QR session can reconnect after logout.
    _LOGINS.clear()
    _ACCOUNT_SEEN.clear()
    _SEEN.clear()
    _CONFIG = {"allowed_user_ids": [], "connected": False, "accounts": {}}
    _save()
    return status()


async def _post(path: str, payload: dict, *, account_id: str = "") -> dict:
    payload.setdefault("base_info", {"channel_version": "1.0.0", "bot_agent": "XiaoA/0.9.0"})
    response = await _client().post(_url(path, account_id), headers=_headers(account_id=account_id), json=payload)
    response.raise_for_status()
    data = response.json()
    if data.get("ret") == -14:
        _, config = _get_account(account_id)
        config["connected"] = False
        _save()
        raise RuntimeError("微信账号登录已过期，请重新扫码连接该账号")
    if data.get("ret") not in (None, 0):
        raise RuntimeError(f"iLink {path} ret={data['ret']} errmsg={data.get('errmsg', '')}")
    return data


async def send_message(user_id: str, text: str, context_token: str, account_id: str = "") -> None:
    if not context_token:
        raise RuntimeError("缺少微信回复上下文，已跳过发送以避免发错会话")
    await _post("/ilink/bot/sendmessage", {
        "msg": {"to_user_id": user_id, "client_id": uuid.uuid4().hex,
                "message_type": 2, "message_state": 2,
                "context_token": context_token,
                "item_list": [{"type": 1, "text_item": {"text": text}}]},
    }, **({"account_id": account_id} if account_id else {}))


async def send_scheduled_message(text: str, account_id: str = "", user_id: str = "") -> None:
    """Send a notice to an explicit account/session; legacy mode uses the only account."""
    selected_id, config = _get_account(account_id)
    allowed = config.get("allowed_user_ids", [])
    if not user_id:
        if len(allowed) != 1:
            raise RuntimeError("请为定时任务指定微信接收用户")
        user_id = str(allowed[0])
    if user_id not in allowed:
        raise RuntimeError("定时任务接收用户已撤销授权")
    context_token = str(config.get("user_contexts", {}).get(user_id) or (
        config.get("last_context_token", "") if config.get("last_user_id") == user_id else ""))
    if not user_id or not context_token:
        raise RuntimeError("没有可用的微信会话，请先从微信给小A发送一条消息")
    await send_message(user_id, text, context_token, selected_id)


async def send_image(user_id: str, image_bytes: bytes, context_token: str, account_id: str = "") -> None:
    if not context_token:
        raise RuntimeError("缺少微信回复上下文，已跳过发送以避免发错会话")
    media = await _upload_media(image_bytes, user_id, 1, **({"account_id": account_id} if account_id else {}))
    try:
        await _post("/ilink/bot/sendmessage", {
            "msg": {"to_user_id": user_id, "client_id": uuid.uuid4().hex,
                    "message_type": 2, "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{"type": 2, "image_item": {
                        "media": {key: media[key] for key in
                                  ("encrypt_query_param", "aes_key", "encrypt_type")},
                        "mid_size": media["cipher_size"],
                    }}]},
        }, **({"account_id": account_id} if account_id else {}))
    except Exception as exc:
        _log(f"[chat] 微信图片发送失败: stage=发送消息, error={type(exc).__name__}")
        raise RuntimeError(f"微信图片发送失败(发送消息): {exc}") from exc
    _log("[chat] 微信图片已发送")


async def send_file(
    user_id: str, file_path: str, context_token: str, *, file_name: str | None = None,
    account_id: str = "",
) -> None:
    if not context_token:
        raise RuntimeError("缺少微信回复上下文，已跳过发送以避免发错会话")
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")
    display_name = Path(str(file_name or path.name)).name or path.name
    raw = path.read_bytes()
    media = await _upload_media(raw, user_id, 3, **({"account_id": account_id} if account_id else {}))
    try:
        await _post("/ilink/bot/sendmessage", {
            "msg": {"to_user_id": user_id, "client_id": uuid.uuid4().hex,
                    "message_type": 2, "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{"type": 4, "file_item": {
                        "media": {key: media[key] for key in
                                  ("encrypt_query_param", "aes_key", "encrypt_type")},
                        "file_name": display_name, "len": str(len(raw)),
                    }}]},
        }, **({"account_id": account_id} if account_id else {}))
    except Exception as exc:
        _log(f"[chat] 微信文件发送失败: stage=发送消息, error={type(exc).__name__}")
        raise RuntimeError(f"微信文件发送失败(发送消息): {exc}") from exc
    _log(f"[chat] 微信文件已发送: name={display_name}, size={len(raw)}")


def set_asr_hook(fn) -> None:
    global _ASR_HOOK
    _ASR_HOOK = fn


def set_vision_hook(fn) -> None:
    global _VISION_HOOK
    _VISION_HOOK = fn


async def _message_text(message: dict) -> str:
    """Convert iLink text, voice and image items into one agent input."""
    parts: list[str] = []
    items = message.get("item_list", [])
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise RuntimeError("微信消息 item_list 格式无效")
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        item_type = _item_type(item)
        if item_type == 1:
            text_item = _item_payload(item, "text_item")
            text = str(text_item.get("text", "")).strip()
            if text:
                parts.append(text)
        elif item_type == 3:
            voice = _item_payload(item, "voice_item")
            text = str(voice.get("text", "")).strip()
            if not text and _ASR_HOOK:
                audio = await _download_media(item)
                text = str(await _ASR_HOOK(audio, "ogg") or "").strip()
            if text:
                parts.append(text)
        elif item_type == 2:
            image = _item_payload(item, "image_item")
            caption = str(image.get("caption", "") or image.get("text", "")).strip()
            raw = await _download_media(item)
            image_format = _media_format(raw)
            if not image_format:
                raise RuntimeError("微信图片下载后不是有效图片，可能是媒体解密参数不匹配")
            image_path = _save_inbound_media(
                raw, f"微信图片.{image_format}", f"wechat-image.{image_format}",
            )
            if _VISION_HOOK:
                image_note = f"[微信图片]\n本机路径: {image_path}"
                try:
                    description = str(await _VISION_HOOK(
                        caption or "请描述这张图片，并提取其中与当前任务有关的信息。",
                        raw, image_format,
                    ) or "").strip()
                except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException):
                    description = "识图超时；原图已保存在本机，请稍后重试或补充图片说明。"
                    _log("[chat] 微信图片已下载保存，但视觉识别超时；继续派发消息")
                if description:
                    image_note += f"\n图片内容:\n{description}"
                if caption:
                    image_note = f"{caption}\n{image_note}"
                parts.append(image_note)
            else:
                parts.append(
                    f"{caption + chr(10) if caption else ''}[微信图片]\n"
                    f"本机路径: {image_path}"
                )
        elif item_type == 4:
            file = _item_payload(item, "file_item")
            file_name = file.get("file_name", "")
            raw = await _download_media(item)
            file_path = _save_inbound_media(raw, file_name, "wechat-file.bin")
            parts.append(
                f"[微信文件]\n文件名: {file_path.name}\n"
                f"本机路径: {file_path}\n文件大小: {len(raw)} 字节"
            )
        elif item_type is not None:
            _log(f"[chat] 微信消息忽略未知项目类型: index={index}, type={item_type}")
    return "\n".join(parts).strip()


async def _dispatch_callback(account_id: str, user_id: str, text: str, context_token: str) -> None:
    started_at = time.monotonic()
    if context_token:
        try:
            kwargs = {"account_id": account_id} if account_id else {}
            await send_message(user_id, "收到，正在处理。", context_token, **kwargs)
            _log("[chat] 微信处理回执已提交发送接口")
        except Exception as exc:
            _log(f"[chat] 微信处理回执发送失败: {type(exc).__name__}")
    else:
        _log("[chat] 微信消息缺少回复上下文，无法发送即时回执")

    try:
        if _CALLBACK:
            _log("[chat] 微信消息已派发给 Agent")
            await _CALLBACK("weixin", user_id, text, context_token, *([account_id] if account_id else []))
            _log(f"[chat] 微信消息处理完成，耗时 {time.monotonic() - started_at:.1f}s")
        else:
            _log("[chat] 微信消息未派发：Agent 回调未初始化")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log(f"[chat] 微信消息处理失败: {type(exc).__name__}")
        if context_token:
            try:
                kwargs = {"account_id": account_id} if account_id else {}
                await send_message(user_id, f"处理微信消息失败：{exc}", context_token, **kwargs)
            except Exception as send_exc:
                _log(f"[chat] 微信错误回复失败: {type(send_exc).__name__}")


async def _poll(account_id: str = "") -> None:
    selected_id, config = _get_account(account_id)
    multi = isinstance(_CONFIG.get("accounts"), dict)
    effective_id = selected_id if multi else ""
    seen = _SEEN if config is _CONFIG else _ACCOUNT_SEEN.setdefault(selected_id, {
        str(item): None for item in config.get("seen_message_ids", [])[-4096:] if str(item)
    })
    while config.get("connected"):
        try:
            payload = {"get_updates_buf": config.get("get_updates_buf", "")}
            data = await _post("/ilink/bot/getupdates", payload, **(
                {"account_id": effective_id} if effective_id else {}
            ))
            for message in data.get("msgs", []):
                msg_id = str(message.get("message_id", ""))
                if not msg_id or msg_id in seen:
                    continue
                if message.get("message_type") != 1:
                    _log(
                        "[chat] 微信消息已忽略: "
                        f"type={message.get('message_type')}, "
                        f"state={message.get('message_state')}"
                    )
                    _remember_message(msg_id, effective_id)
                    continue
                user_id = str(message.get("from_user_id", ""))
                context_token = str(message.get("context_token", ""))
                items = message.get("item_list", [])
                _log(
                    "[chat] 收到微信入站消息: "
                    f"type={message.get('message_type')}, "
                    f"context={'yes' if context_token else 'no'}, "
                    f"items={len(items) if isinstance(items, list) else 0}"
                )
                try:
                    text = await _message_text(message)
                except Exception as exc:
                    text = ""
                    detail = str(exc).strip() or type(exc).__name__
                    _log(f"[chat] 微信消息解析失败: {detail}")
                    if context_token:
                        try:
                            kwargs = {"account_id": effective_id} if effective_id else {}
                            await send_message(user_id, f"微信消息解析失败：{detail}", context_token, **kwargs)
                        except Exception as send_exc:
                            _log(f"[chat] 微信解析错误回复失败: {type(send_exc).__name__}")
                if not user_id or not text:
                    _log("[chat] 微信消息未派发: 缺少发送方或可处理内容")
                    _remember_message(msg_id, effective_id)
                    continue
                if user_id not in config.get("allowed_user_ids", []):
                    _log("[chat] 微信消息未派发: 发送方不在白名单")
                    if context_token:
                        try:
                            kwargs = {"account_id": effective_id} if effective_id else {}
                            await send_message(user_id, "This account is not authorized.", context_token, **kwargs)
                        except Exception as send_exc:
                            _log(f"[chat] 微信白名单提示发送失败: {type(send_exc).__name__}")
                    _remember_message(msg_id, effective_id)
                    continue
                # iLink requires the recent conversation token for outbound messages.
                # Persist the latest authorized session so scheduled cloud notices can use it.
                if context_token:
                    config["last_user_id"] = user_id
                    config["last_context_token"] = context_token
                    config.setdefault("user_contexts", {})[user_id] = context_token
                    _save()
                if _CALLBACK:
                    task = asyncio.create_task(
                        _dispatch_callback(effective_id, user_id, text, context_token),
                        name=f"weixin-message-{msg_id}",
                    )
                    task.weixin_account_id = effective_id
                    _DISPATCH_TASKS.add(task)
                    task.add_done_callback(_DISPATCH_TASKS.discard)
                    _log("[chat] 微信消息已排入 Agent 处理队列")
                else:
                    _log("[chat] 收到微信消息，但 Agent 回调未初始化")
                _remember_message(msg_id, effective_id)
            # Advance the server cursor only after every message in this response is handled.
            config["get_updates_buf"] = data.get("get_updates_buf", config.get("get_updates_buf", ""))
            _save()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log(f"[chat] WeChat ClawBot polling failed: {type(exc).__name__}")
            await asyncio.sleep(3)


def _remember_message(msg_id: str, account_id: str = "") -> None:
    selected_id, config = _get_account(account_id)
    seen = _SEEN if config is _CONFIG else _ACCOUNT_SEEN.setdefault(selected_id, {
        str(item): None for item in config.get("seen_message_ids", [])[-4096:] if str(item)
    })
    seen.pop(msg_id, None)
    seen[msg_id] = None
    if len(seen) > 4096:
        seen.pop(next(iter(seen)))
    config["seen_message_ids"] = list(seen)
    _save()


async def start_listener() -> None:
    global _POLL_TASK
    accounts = _accounts()
    if isinstance(_CONFIG.get("accounts"), dict):
        for account_id, config in accounts.items():
            task = _ACCOUNT_TASKS.get(account_id)
            if config.get("connected") and (task is None or task.done()):
                _ACCOUNT_TASKS[account_id] = asyncio.create_task(
                    _poll(account_id), name=f"weixin-clawbot-poll-{account_id}",
                )
        return
    if _CONFIG.get("connected") and (_POLL_TASK is None or _POLL_TASK.done()):
        _POLL_TASK = asyncio.create_task(_poll(), name="weixin-clawbot-poll")


async def stop_listener() -> None:
    global _POLL_TASK
    tasks = list(_ACCOUNT_TASKS.values())
    _ACCOUNT_TASKS.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    task, _POLL_TASK = _POLL_TASK, None
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    pending, _DISPATCH_TASKS_COPY = tuple(_DISPATCH_TASKS), _DISPATCH_TASKS.copy()
    for callback_task in pending:
        callback_task.cancel()
    if _DISPATCH_TASKS_COPY:
        await asyncio.gather(*_DISPATCH_TASKS_COPY, return_exceptions=True)


async def initialize(path: Path, on_message: Callable[..., Awaitable[None]], on_accounts_changed: Callable[[dict], None] | None = None) -> None:
    global _STATE_PATH, _CONFIG, _CALLBACK, _ACCOUNTS_CHANGED
    await stop_listener()
    _STATE_PATH, _CALLBACK = path, on_message
    _ACCOUNTS_CHANGED = on_accounts_changed
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        _CONFIG = value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        _CONFIG = {}
    if not isinstance(_CONFIG.get("accounts"), dict):
        if _CONFIG.get("bot_token") and not path.with_suffix(path.suffix + ".bak").exists():
            write_private_text(path.with_suffix(path.suffix + ".bak"), json.dumps(_CONFIG, ensure_ascii=False))
        _migrate_config()
        _save()
    _SEEN.clear()
    _ACCOUNT_SEEN.clear()
    for account_id, config in _accounts().items():
        config["account_id"] = account_id
        if not isinstance(config.get("allowed_user_ids"), list):
            config["allowed_user_ids"] = []
        if not isinstance(config.get("user_contexts"), dict):
            config["user_contexts"] = {}
        if config.get("last_user_id") and config.get("last_context_token"):
            config["user_contexts"].setdefault(config["last_user_id"], config["last_context_token"])
    if _ACCOUNTS_CHANGED is not None:
        await asyncio.to_thread(_ACCOUNTS_CHANGED, _accounts())
    await start_listener()
