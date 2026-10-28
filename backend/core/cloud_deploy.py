"""Idempotent local-to-server deployment over the enrolled SSH key."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import shlex
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Callable

from backend import __version__
from backend.core.cloud_identity import enroll, identity_path, write_private_text
from backend.core.config import LLMProviderConfig, asset_root, provider_runtime_env
from backend.core.connection_string import masked_preview, parse_connection_string
from backend.core.ssh_client import SSHClient, SSHClientError


Progress = Callable[..., None]
REMOTE_ROOT = "/opt/xiaoa"
CREDENTIAL_FILE = f"{REMOTE_ROOT}/data/connection-string.txt"


def validate_target(host: str, username: str) -> tuple[str, str]:
    host = str(host).strip()
    username = str(username).strip()
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("公网 IP 格式不正确，请填写服务器的 IPv4 或 IPv6 地址") from exc
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", username):
        raise ValueError("远程用户名格式不正确")
    return host, username


def _payload_root() -> Path:
    bundled = asset_root() / "server-payload"
    return bundled if bundled.is_dir() else asset_root()


def _archive_payload(target: Path) -> None:
    root = _payload_root()
    frontend = root / "frontend" / "src"
    if not frontend.is_dir():
        frontend = asset_root() / "frontend" / "src"
    example = root / "config.example.toml"
    if not example.is_file():
        example = asset_root() / "config.example.toml"
    required = (root / "backend", root / "deploy", example, frontend)
    if not all(path.exists() for path in required):
        raise SSHClientError("本机安装包缺少云端部署文件，请重新安装小A")

    def clean(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(info.name).parts
        if info.issym() or info.islnk() or "__pycache__" in parts or info.name.endswith((".pyc", ".pyo")):
            return None
        return info

    with tarfile.open(target, "w:gz") as archive:
        archive.add(root / "backend", arcname="backend", filter=clean)
        archive.add(root / "deploy", arcname="deploy", filter=clean)
        archive.add(example, arcname="config.example.toml", filter=clean)
        archive.add(frontend, arcname="frontend/src", filter=clean)


def _parse_credentials(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        name, separator, value = line.partition("=")
        if separator:
            values[name.strip()] = value.strip()
    connection = values.get("XIAOA_CONNECTION_STRING", "")
    password = values.get("AGENT_WEB_PASSWORD", "")
    parse_connection_string(connection)
    # Existing deployments may use a password created before the current
    # length guidance was introduced.  A non-empty password is enough to
    # recover credentials; changing it belongs to an explicit settings save.
    if not password:
        raise SSHClientError("服务器未返回网页登录密码")
    result = {"connection_string": connection, "web_password": password}
    web_url = values.get("XIAOA_PUBLIC_WEB_URL", "")
    if web_url:
        from urllib.parse import urlsplit
        parsed = urlsplit(web_url)
        details = parse_connection_string(connection)
        if (parsed.scheme != "https" or parsed.hostname != details.host or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment
                or not parsed.port or (not details.fingerprint and parsed.port != details.port)):
            raise SSHClientError("服务器返回的网页地址无效")
        result["web_url"] = web_url
    return result


def _parse_remote_result(output: str | None) -> dict:
    if not isinstance(output, str):
        return {}
    for line in reversed(output.splitlines()):
        if not line.startswith("XIAOA_RESULT "):
            continue
        try:
            value = json.loads(line[len("XIAOA_RESULT "):])
        except (TypeError, ValueError):
            return {}
        if not isinstance(value, dict):
            return {}
        # Only retain the health fields needed by the local job/UI; remote
        # output is untrusted and must never become a local log or status blob.
        safe = {}
        for key in ("installed", "active", "healthy", "transaction_pending", "data_preserved"):
            if isinstance(value.get(key), bool):
                safe[key] = value[key]
        if isinstance(value.get("port"), int) and not isinstance(value["port"], bool):
            safe["port"] = value["port"]
        if isinstance(value.get("version"), str):
            safe["version"] = value["version"][:80]
        return safe
    return {}


def _elevation(client: SSHClient) -> str:
    if client.run("id -u", timeout=10).strip() == "0":
        return ""
    try:
        client.run("sudo -n true", timeout=10)
    except SSHClientError as exc:
        raise SSHClientError("远程用户不是 root，且没有免密 sudo；请改用 root 用户") from exc
    return "sudo -n "


def _apply_feishu(client: SSHClient, elevate: str, values: dict, *, restart: bool = True) -> None:
    if not values:
        return
    script = _payload_root() / "deploy" / "update_feishu.py"
    if not script.is_file():
        raise SSHClientError("本机缺少云端飞书配置脚本")
    remote_script = f"/tmp/xiaoa-feishu-update-{uuid.uuid4().hex}.py"
    remote_payload = f"/tmp/xiaoa-feishu-{uuid.uuid4().hex}.json"
    client.write_text(remote_script, script.read_text(encoding="utf-8"))
    client.write_text(remote_payload, json.dumps(values, ensure_ascii=False))
    logout = bool(values.get("logout"))
    if logout and restart:
        # Stop first so the old long connection cannot restore its in-memory credentials.
        command = (
            f"{elevate}systemctl stop xiaoa-hub; code=$?; "
            f"if [ $code -eq 0 ]; then {elevate}python3 {remote_script} "
            f"{remote_payload} {REMOTE_ROOT}/config.toml; code=$?; fi; "
            f"rm -f {remote_script} {remote_payload}; "
            f"if [ $code -eq 0 ]; then {elevate}systemctl start xiaoa-hub; code=$?; fi; "
            "exit $code"
        )
    else:
        command = (
            f"{elevate}python3 {remote_script} "
            f"{remote_payload} {REMOTE_ROOT}/config.toml; "
            f"code=$?; rm -f {remote_script} {remote_payload}; "
            + (f"[ $code -eq 0 ] && {elevate}systemctl restart xiaoa-hub; " if restart else "")
            + "exit $code"
        )
    client.run(command, timeout=60)


def _apply_hub_environment(client: SSHClient, elevate: str, values: dict[str, str]) -> None:
    remote_payload = f"/tmp/xiaoa-env-{uuid.uuid4().hex}.json"
    client.write_text(remote_payload, json.dumps(values, ensure_ascii=False))
    command = (
        f"{elevate}{REMOTE_ROOT}/.venv/bin/python {REMOTE_ROOT}/deploy/update_hub_env.py "
        f"{remote_payload} /etc/xiaoa-hub.env; "
        f"code=$?; rm -f {remote_payload}; exit $code"
    )
    client.run(command, timeout=60)


def _save_local_credentials(root: str | Path, credentials: dict[str, str],
                            host: str, username: str) -> None:
    base = Path(root) / "data" / "cloud-ssh"
    write_private_text(base / "web-password.txt", credentials["web_password"] + "\n")
    target = {"host": host, "username": username}
    if credentials.get("web_url"):
        target["web_url"] = credentials["web_url"]
    (base / "target.json").write_text(
        json.dumps(target, ensure_ascii=False), encoding="utf-8",
    )


def deploy(host: str, username: str, password: str, root: str | Path,
           *, feishu_app_id: str = "", feishu_app_secret: str = "",
           feishu_allowed_open_ids: list[str] | tuple[str, ...] = (),
           llm_protocol: str = "openai", llm_base_url: str = "", llm_api_key: str = "",
           chat_model: str = "", model_options: list[str] | tuple[str, ...] = (),
           vision_model: str = "", llm_providers: list[LLMProviderConfig] | None = None,
           llm_provider_name: str = "默认接口", llm_context_windows: dict[str, int] | None = None,
           context_window: int = 128000,
           voice_secret_id: str = "", voice_secret_key: str = "",
           voice_region: str = "", tts_voice: int = 101016, tts_speed: int = 0,
           speech_provider: str = "tencent", baidu_api_key: str = "", baidu_secret_key: str = "",
           baidu_tts_voice: int = 0, baidu_tts_speed: int = 5,
           operation: str = "deploy", web_password: str = "",
           progress: Progress = lambda *_a, **_k: None) -> dict:
    host, username = validate_target(host, username)
    if any(char in web_password for char in "\r\n\0"):
        raise ValueError("网页密码包含非法字符")
    protocol = str(llm_protocol).strip().lower()
    if protocol not in {"openai", "anthropic", "responses"}:
        protocol = "openai"
    progress("ssh", "正在验证远程账号…")
    identity = enroll(host, username, password, root)
    password = ""
    progress("ssh", "专用密钥已安装，后续不再使用远程密码")

    with tempfile.TemporaryDirectory(prefix="xiaoa-cloud-") as temp_dir:
        archive_path = Path(temp_dir) / "xiaoa-server.tar.gz"
        progress("upload", f"正在打包本机 v{__version__} 内置云端文件…")
        _archive_payload(archive_path)
        remote_archive = f"/tmp/xiaoa-upload-{uuid.uuid4().hex}.tar.gz"
        with SSHClient(host, username, key_filename=identity["path"]) as client:
            elevate = _elevation(client)
            try:
                client.run("command -v systemctl >/dev/null && command -v apt-get >/dev/null && test -f /etc/os-release", timeout=15)
            except SSHClientError as exc:
                raise SSHClientError("云端自动部署需要 Debian/Ubuntu 与 systemd；未修改原有部署") from exc
            progress("upload", "正在上传小A云端文件…")
            last_percent = [-1]
            def uploaded(sent, total):
                percent = int(sent * 100 / total) if total else 0
                if percent != last_percent[0]:
                    last_percent[0] = percent
                    progress("upload", f"已上传 {percent}% · {sent}/{total} 字节",
                             uploaded_bytes=sent, total_bytes=total)
            remote_script = f"/tmp/xiaoa-transaction-{uuid.uuid4().hex}.py"
            remote_settings = f"/tmp/xiaoa-settings-{uuid.uuid4().hex}.json"
            script = _payload_root() / "deploy" / "cloud_transaction.py"
            if not script.is_file():
                raise SSHClientError("本机缺少云端安全部署脚本，请更新小A")
            try:
                client.upload(archive_path, remote_archive, callback=uploaded)
                client.write_text(remote_script, script.read_text(encoding="utf-8-sig"))
                runtime = {
                    **provider_runtime_env(llm_providers or []),
                    "AGENT_LLM_PROVIDER_NAME": llm_provider_name,
                    "AGENT_LLM_CONTEXT_WINDOWS": json.dumps(llm_context_windows or {}),
                    "AGENT_CONTEXT_WINDOW": str(context_window),
                    "AGENT_LLM_PROTOCOL": protocol,
                    "AGENT_LLM_BASE_URL": llm_base_url.strip(),
                    "AGENT_LLM_API_KEY": llm_api_key.strip(),
                    "AGENT_CHAT_MODEL": chat_model.strip(),
                    "AGENT_MODEL_OPTIONS": ",".join(dict.fromkeys(str(m).strip() for m in model_options if str(m).strip())) or chat_model.strip(),
                    "AGENT_TENCENT_SECRET_ID": voice_secret_id.strip(),
                    "AGENT_TENCENT_SECRET_KEY": voice_secret_key.strip(),
                    "AGENT_TENCENT_REGION": voice_region.strip(),
                    "AGENT_SPEECH_PROVIDER": speech_provider,
                    "AGENT_BAIDU_API_KEY": baidu_api_key.strip(),
                    "AGENT_BAIDU_SECRET_KEY": baidu_secret_key.strip(),
                    "AGENT_BAIDU_TTS_VOICE": str(baidu_tts_voice),
                    "AGENT_BAIDU_TTS_SPEED": str(baidu_tts_speed),
                    "AGENT_TTS_VOICE": str(tts_voice), "AGENT_TTS_SPEED": str(tts_speed),
                }
                # Empty saved secrets are not an instruction to erase the cloud credentials.
                if web_password:
                    runtime["AGENT_WEB_PASSWORD"] = web_password
                for name in ("AGENT_LLM_API_KEY", "AGENT_TENCENT_SECRET_ID", "AGENT_TENCENT_SECRET_KEY", "AGENT_BAIDU_API_KEY", "AGENT_BAIDU_SECRET_KEY"):
                    if not runtime[name]:
                        del runtime[name]
                feishu = {key: value for key, value in {
                    "app_id": feishu_app_id.strip(), "app_secret": feishu_app_secret.strip(),
                    "allowed_open_ids": list(feishu_allowed_open_ids),
                }.items() if value}
                client.write_text(remote_settings, json.dumps({"runtime": runtime, "feishu": feishu}, ensure_ascii=False))
                buffer = [""]
                def output(chunk):
                    buffer[0] += chunk
                    lines = buffer[0].split("\n")
                    buffer[0] = lines.pop()[-8000:]
                    for line in lines:
                        if line.startswith("XIAOA_PROGRESS "):
                            try:
                                event = json.loads(line[len("XIAOA_PROGRESS "):])
                                if event.get("stage") in {"ssh", "runtime", "configure", "health", "rollback"}:
                                    progress(event["stage"], str(event.get("message", ""))[:600],
                                             **({"cloud_ready": True} if event.get("cloud_ready") is True else {}))
                            except (ValueError, TypeError):
                                pass
                digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
                action = "repair" if operation == "repair" else "deploy"
                progress("runtime", "安装包上传完成，正在校验并创建回退点…")
                # Debian minimal images may not ship Python yet; no service/code touched here.
                client.run(f"{elevate}sh -c 'command -v python3 >/dev/null || (apt-get update -y >/dev/null 2>&1 && DEBIAN_FRONTEND=noninteractive apt-get install -y python3 >/dev/null 2>&1)'", timeout=600)
                try:
                    remote_output = client.run_stream(
                        f"{elevate}python3 {remote_script} {action} --archive {remote_archive} "
                        f"--digest {digest} --host {shlex.quote(host)} --settings {remote_settings}",
                        output, timeout=1800,
                    )
                except SSHClientError as exc:
                    for line in reversed(str(exc).splitlines()):
                        if line.startswith("XIAOA_ERROR "):
                            raise SSHClientError(line[len("XIAOA_ERROR "):][:600]) from exc
                    raise
                remote_result = _parse_remote_result(remote_output)
                raw_credentials = client.run(
                    f"set -e; value=$({elevate}cat {CREDENTIAL_FILE}); "
                    f"{elevate}rm -f {CREDENTIAL_FILE}; printf '%s\n' \"$value\"", timeout=30,
                )
            finally:
                # Temp paths are UUID-generated by us, never interpolated user paths.
                try:
                    client.run(f"rm -f {remote_archive} {remote_script} {remote_settings}", timeout=15)
                except SSHClientError:
                    pass

    credentials = _parse_credentials(raw_credentials)
    if web_password:
        credentials["web_password"] = web_password
    _save_local_credentials(root, credentials, host, username)
    details = parse_connection_string(credentials["connection_string"])
    return {
        "connection_string": credentials["connection_string"],
        "connection": masked_preview(details),
        "host": host,
        "username": username,
        "remote_status": remote_result,
    }


def maintain(host: str, username: str, password: str, root: str | Path,
             *, operation: str = "check", purge: bool = False) -> dict:
    """Maintenance uses the exact same enrollment and fixed remote boundaries."""
    if operation not in {"check", "uninstall", "credentials"}:
        raise ValueError("未知云端维护操作")
    host, username = validate_target(host, username)
    key = identity_path(host, username, root)
    if not key.is_file() and not password:
        raise SSHClientError("专用密钥不存在，请填写远程密码或先部署")
    script = _payload_root() / "deploy" / "cloud_transaction.py"
    if not script.is_file():
        raise SSHClientError("本机缺少云端维护脚本")
    remote_script = f"/tmp/xiaoa-maintain-{uuid.uuid4().hex}.py"
    # Check/maintenance never enrolls or rotates SSH keys. Password fallback is
    # confined to authentication, never reruns a destructive operation on error.
    client = SSHClient(host, username, key_filename=str(key)) if key.is_file() else SSHClient(host, username, password=password)
    try:
        try:
            client.connect()
        except SSHClientError:
            if not key.is_file() or not password:
                raise
            client.close()
            client = SSHClient(host, username, password=password)
            client.connect()
        elevate = _elevation(client)
        client.write_text(remote_script, script.read_text(encoding="utf-8-sig"))
        try:
            python = f"{REMOTE_ROOT}/.venv/bin/python" if operation == "credentials" else "python3"
            raw = client.run(f"{elevate}{python} {remote_script} {operation}" + (" --purge" if purge else ""), timeout=120)
            line = next((line[len("XIAOA_RESULT "):] for line in raw.splitlines() if line.startswith("XIAOA_RESULT ")), raw.strip())
            return json.loads(line)
        finally:
            try:
                client.run(f"rm -f {remote_script}", timeout=15)
            except SSHClientError:
                pass
    finally:
        client.close()


def recover_connection(host: str, username: str, password: str, root: str | Path) -> dict:
    values = maintain(host, username, password, root, operation="credentials")
    parsed = _parse_credentials(
        f"XIAOA_CONNECTION_STRING={values.get('connection_string', '')}\n"
        f"AGENT_WEB_PASSWORD={values.get('web_password', '')}\n"
        f"XIAOA_PUBLIC_WEB_URL={values.get('web_url', '')}")
    _save_local_credentials(root, parsed, host, username)
    return {"connection_string": parsed["connection_string"],
            "connection": masked_preview(parse_connection_string(parsed["connection_string"]))}


def update_feishu(root: str | Path, allowed_open_ids: list[str] | None,
                   *, app_id: str = "", app_secret: str = "", logout: bool = False,
                   authorize_pending: bool = False, pending_open_id: str = "",
                   remark_open_id: str = "", remark: str = "", restart: bool = True) -> None:
    target_path = Path(root) / "data" / "cloud-ssh" / "target.json"
    if not target_path.is_file():
        raise SSHClientError("尚未连接云端，请先完成一次云端部署")
    target = json.loads(target_path.read_text(encoding="utf-8"))
    host, username = validate_target(target.get("host", ""), target.get("username", ""))
    key_path = identity_path(host, username, root)
    if not key_path.is_file():
        raise SSHClientError("本机云端专用密钥不存在，请重新连接云端")
    values: dict[str, object] = {}
    if allowed_open_ids is not None:
        values["allowed_open_ids"] = allowed_open_ids
    if logout:
        values["logout"] = True
    if authorize_pending and not logout:
        values["authorize_pending"] = True
        if pending_open_id:
            values["pending_open_id"] = pending_open_id
    if app_id.strip() and not logout:
        values["app_id"] = app_id.strip()
    if app_secret.strip() and not logout:
        values["app_secret"] = app_secret.strip()
    if remark_open_id and not logout:
        values["remark_open_id"] = remark_open_id
        values["remark"] = remark
    with SSHClient(host, username, key_filename=str(key_path)) as client:
        _apply_feishu(client, _elevation(client), values, restart=restart)


def get_feishu_status(root: str | Path) -> dict:
    """Read cloud Feishu settings without ever returning the App Secret."""
    target_path = Path(root) / "data" / "cloud-ssh" / "target.json"
    if not target_path.is_file():
        return {"connected": False, "app_id": "", "allowed_open_ids": [], "app_secret_configured": False}
    target = json.loads(target_path.read_text(encoding="utf-8"))
    host, username = validate_target(target.get("host", ""), target.get("username", ""))
    key_path = identity_path(host, username, root)
    if not key_path.is_file():
        raise SSHClientError("本机云端专用密钥不存在，请重新连接云端")
    script = _payload_root() / "deploy" / "update_feishu.py"
    if not script.is_file():
        raise SSHClientError("本机缺少云端飞书状态读取脚本")
    remote_script = f"/tmp/xiaoa-feishu-status-{uuid.uuid4().hex}.py"
    with SSHClient(host, username, key_filename=str(key_path)) as client:
        client.write_text(remote_script, script.read_text(encoding="utf-8"))
        command = (
            f"{_elevation(client)}{REMOTE_ROOT}/.venv/bin/python {remote_script} "
            f"--status {REMOTE_ROOT}/config.toml; code=$?; "
            f"rm -f {remote_script}; exit $code"
        )
        raw = client.run(command, timeout=30)
    try:
        status = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SSHClientError("云端返回的飞书状态无效") from exc
    if not isinstance(status, dict):
        raise SSHClientError("云端返回的飞书状态无效")
    return {"connected": True, **status}


def _apply_weixin(client: SSHClient, elevate: str, values: dict, *, restart: bool = True) -> None:
    script = _payload_root() / "deploy" / "update_weixin.py"
    if not script.is_file():
        raise SSHClientError("本机缺少云端微信配置脚本")
    remote_script = f"/tmp/xiaoa-weixin-update-{uuid.uuid4().hex}.py"
    remote_payload = f"/tmp/xiaoa-weixin-{uuid.uuid4().hex}.json"
    client.write_text(remote_script, script.read_text(encoding="utf-8"))
    client.write_text(remote_payload, json.dumps(values, ensure_ascii=False))
    # Remarks share the poller's state file; reload it to prevent stale writes.
    restart = restart or "remark" in values
    # Project maintenance reads TOML (tomli on Python 3.10), installed in hub venv.
    python = f"{REMOTE_ROOT}/.venv/bin/python" if "project_library" in values else "python3"
    if restart:
        # Stop before every state update: live pollers would overwrite the new
        # credentials/allowlist with their old in-memory snapshot.
        command = (
            f"{elevate}systemctl stop xiaoa-hub; code=$?; "
            f"if [ $code -eq 0 ]; then {elevate}{python} {remote_script} "
            f"{remote_payload} {REMOTE_ROOT}/data/weixin-clawbot.json; code=$?; fi; "
            f"rm -f {remote_script} {remote_payload}; "
            f"if [ $code -eq 0 ]; then {elevate}systemctl start xiaoa-hub; code=$?; fi; "
            "exit $code"
        )
    else:
        command = (
            f"{elevate}{python} {remote_script} "
            f"{remote_payload} {REMOTE_ROOT}/data/weixin-clawbot.json; "
            f"code=$?; rm -f {remote_script} {remote_payload}; "
            + (f"[ $code -eq 0 ] && {elevate}systemctl restart xiaoa-hub; " if restart else "")
            + "exit $code"
        )
    client.run(command, timeout=60)


def update_weixin(root: str | Path, values: dict[str, object], *, restart: bool = True) -> None:
    """Write isolated ClawBot credentials/allowlist to the cloud hub."""
    target_path = Path(root) / "data" / "cloud-ssh" / "target.json"
    if not target_path.is_file():
        raise SSHClientError("尚未连接云端，请先完成一次云端部署")
    target = json.loads(target_path.read_text(encoding="utf-8"))
    host, username = validate_target(target.get("host", ""), target.get("username", ""))
    key_path = identity_path(host, username, root)
    if not key_path.is_file():
        raise SSHClientError("本机云端专用密钥不存在，请重新连接云端")
    with SSHClient(host, username, key_filename=str(key_path)) as client:
        _apply_weixin(client, _elevation(client), values, restart=restart)


def _read_weixin_response(root: str | Path, mode: str, values: dict | None = None) -> dict:
    """Read public WeChat state or request a QR using tokens on the server."""
    target_path = Path(root) / "data" / "cloud-ssh" / "target.json"
    if not target_path.is_file():
        return {"connected": False, "allowed_user_ids": [], "account_id": ""}
    target = json.loads(target_path.read_text(encoding="utf-8"))
    host, username = validate_target(target.get("host", ""), target.get("username", ""))
    key_path = identity_path(host, username, root)
    if not key_path.is_file():
        raise SSHClientError("本机云端专用密钥不存在，请重新连接云端")
    script = _payload_root() / "deploy" / "update_weixin.py"
    if not script.is_file():
        raise SSHClientError("本机缺少云端微信状态读取脚本")
    remote_script = f"/tmp/xiaoa-weixin-status-{uuid.uuid4().hex}.py"
    with SSHClient(host, username, key_filename=str(key_path)) as client:
        client.write_text(remote_script, script.read_text(encoding="utf-8"))
        payload = remote_script + ".json" if values is not None else ""
        if payload:
            client.write_text(payload, json.dumps(values, ensure_ascii=False))
        command = (
            f"{_elevation(client)}{REMOTE_ROOT}/.venv/bin/python {remote_script} "
            f"--{mode} {payload + ' ' if payload else ''}{REMOTE_ROOT}/data/weixin-clawbot.json; code=$?; "
            f"rm -f {remote_script}{' ' + payload if payload else ''}; exit $code"
        )
        raw = client.run(command, timeout=30)
    try:
        status = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SSHClientError("云端返回的微信状态无效") from exc
    if not isinstance(status, dict):
        raise SSHClientError("云端返回的微信状态无效")
    return {"connected": True, **status}


def get_weixin_status(root: str | Path) -> dict:
    return _read_weixin_response(root, "status")


def desktop_project_memory(root: str | Path, values: dict) -> dict:
    """Use the registered SSH management identity; never impersonate a channel sender."""
    result = _read_weixin_response(root, "project-memory", values)
    if not result.get("ok"):
        raise SSHClientError("云端未确认共享项目操作；未回退本机记忆")
    return result


def get_project_libraries(root: str | Path) -> dict:
    """Read library metadata and authorized identities through management SSH."""
    return _read_weixin_response(root, "projects")


def get_shared_library_backup(root: str | Path) -> dict:
    """Full backup requires the separately enrolled management SSH key, never hub_token."""
    return _read_weixin_response(root, "shared-backup")


def get_weixin_qrcode(root: str | Path) -> dict:
    return _read_weixin_response(root, "qrcode")


def _remote_env_capabilities(client: SSHClient, elevate: str) -> set[str] | None:
    """读云端 update_hub_env.py 的 ALLOWED 集合,探测云端校验器版本。

    读不到(脚本太旧/没有该文件/命名空间导入失败)返回 None,表示无法探测,
    调用方按"继续推送、让真实报错兜底"处理。
    """
    try:
        raw = client.run(
            f"{elevate}cd {REMOTE_ROOT} && python3 -c "
            "'from deploy.update_hub_env import ALLOWED; print(*sorted(ALLOWED))'",
            timeout=30,
        )
    except SSHClientError:
        return None
    tokens = {token for token in raw.split() if token.strip()}
    return tokens or None


def sync_runtime_settings(root: str | Path, values: dict[str, str]) -> None:
    """用已登记的专用密钥同步 hub 运行配置并重启服务。

    推送前先探测云端校验器认识的字段集合;若本地要推的字段云端不认,
    直接给人话报错(而不是静默跳过或抛一堆 traceback),提示重新部署。
    """
    target_path = Path(root) / "data" / "cloud-ssh" / "target.json"
    if not target_path.is_file():
        raise SSHClientError("尚未连接云端，请先完成一次云端部署")
    target = json.loads(target_path.read_text(encoding="utf-8"))
    host, username = validate_target(target.get("host", ""), target.get("username", ""))
    key_path = identity_path(host, username, root)
    if not key_path.is_file():
        raise SSHClientError("本机云端专用密钥不存在，请重新连接云端")
    with SSHClient(host, username, key_filename=str(key_path)) as client:
        elevate = _elevation(client)
        allowed = _remote_env_capabilities(client, elevate)
        if allowed is not None:
            unknown = sorted(set(values) - allowed)
            if unknown:
                raise SSHClientError(
                    "云端脚本版本较旧，尚不识别本次要同步的新设置（"
                    + "、".join(unknown)
                    + "）。请到「消息连接」页重新执行一次云端部署，再回来保存。"
                )
        _apply_hub_environment(client, elevate, values)
        client.run(f"{elevate}systemctl restart xiaoa-hub", timeout=60)


def sync_web_password(root: str | Path, password: str) -> None:
    """Update only the enrolled hub password, including hubs with an older updater."""
    if any(char in password for char in "\r\n\0"):
        raise ValueError("网页密码包含非法字符")
    target_path = Path(root) / "data/cloud-ssh/target.json"
    target = json.loads(target_path.read_text(encoding="utf-8"))
    host, username = validate_target(target.get("host", ""), target.get("username", ""))
    key = identity_path(host, username, root)
    if not key.is_file():
        raise SSHClientError("本机云端专用密钥不存在，请重新连接云端")
    script = _payload_root() / "deploy/update_hub_env.py"
    remote_script = f"/tmp/xiaoa-password-{uuid.uuid4().hex}.py"
    remote_payload = f"/tmp/xiaoa-password-{uuid.uuid4().hex}.json"
    with SSHClient(host, username, key_filename=str(key), secret_values=(password,)) as client:
        elevate = _elevation(client)
        try:
            client.write_text(remote_script, script.read_text(encoding="utf-8-sig"))
            client.write_text(remote_payload, json.dumps({"AGENT_WEB_PASSWORD": password}))
            client.run(
                f"{elevate}python3 {remote_script} {remote_payload} /etc/xiaoa-hub.env && "
                f"{elevate}systemctl restart xiaoa-hub", timeout=60,
            )
        finally:
            try:
                client.run(f"rm -f {remote_script} {remote_payload}", timeout=15)
            except SSHClientError:
                pass
    write_private_text(target_path.parent / "web-password.txt", password + "\n")


def channel_lock_status(root: str | Path) -> bool:
    """读取实际接收飞书消息的云端 hub 锁定状态。"""
    target_path = Path(root) / "data" / "cloud-ssh" / "target.json"
    if not target_path.is_file():
        raise SSHClientError("尚未连接云端，无法读取云端锁定状态")
    target = json.loads(target_path.read_text(encoding="utf-8"))
    host, username = validate_target(target.get("host", ""), target.get("username", ""))
    key_path = identity_path(host, username, root)
    if not key_path.is_file():
        raise SSHClientError("本机云端专用密钥不存在，请重新连接云端")
    with SSHClient(host, username, key_filename=str(key_path)) as client:
        elevate = _elevation(client)
        raw = client.run(
            f"{elevate}cat {REMOTE_ROOT}/data/channel_lock.json 2>/dev/null || "
            "printf '{\"locked\":false}'",
            timeout=30,
        )
    try:
        return bool(json.loads(raw).get("locked"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise SSHClientError("云端返回的锁定状态无效") from exc


def unlock_channel(root: str | Path) -> None:
    """清除云端持久锁并重启 hub，让内存状态同步解除。"""
    target_path = Path(root) / "data" / "cloud-ssh" / "target.json"
    if not target_path.is_file():
        raise SSHClientError("尚未连接云端，无法解除云端锁定")
    target = json.loads(target_path.read_text(encoding="utf-8"))
    host, username = validate_target(target.get("host", ""), target.get("username", ""))
    key_path = identity_path(host, username, root)
    if not key_path.is_file():
        raise SSHClientError("本机云端专用密钥不存在，请重新连接云端")
    remote_payload = f"/tmp/xiaoa-lock-{uuid.uuid4().hex}.json"
    with SSHClient(host, username, key_filename=str(key_path)) as client:
        elevate = _elevation(client)
        client.write_text(remote_payload, '{"locked": false}\n')
        client.run(
            f"{elevate}install -m 0600 {remote_payload} "
            f"{REMOTE_ROOT}/data/channel_lock.json; rm -f {remote_payload}; "
            f"{elevate}systemctl restart xiaoa-hub",
            timeout=60,
        )


def channel_control_settings(root: str | Path, source: str | None = None,
                             enabled: bool | None = None, identity: str = "") -> dict:
    """Read/update live cloud permissions without restarting either channel listener."""
    from backend.core.channel_control import CHANNELS
    if source is not None and (source not in CHANNELS or type(enabled) is not bool):
        raise ValueError("远程操控开关值无效")
    if identity and (not isinstance(identity, str) or len(identity) > 256):
        raise ValueError("远程操控身份无效")
    target_path = Path(root) / "data" / "cloud-ssh" / "target.json"
    if not target_path.is_file():
        raise SSHClientError("尚未登记云端专用密钥，请先完成云端部署")
    target = json.loads(target_path.read_text(encoding="utf-8"))
    host, username = validate_target(target.get("host", ""), target.get("username", ""))
    key_path = identity_path(host, username, root)
    if not key_path.is_file():
        raise SSHClientError("本机云端专用密钥不存在，请重新连接云端")
    code = (
        "import sys,json; from pathlib import Path; "
        f"sys.path.insert(0,{REMOTE_ROOT!r}); "
        "from backend.core.channel_control import snapshot,update,update_account; "
    )
    operation = (f"snapshot(Path({REMOTE_ROOT!r}))" if source is None else
                 (f"update_account({source!r},{identity!r},{enabled!r},Path({REMOTE_ROOT!r}))"
                  if identity else f"update({source!r},{enabled!r},Path({REMOTE_ROOT!r}))"))
    code += f"print(json.dumps({operation}))"
    try:
        with SSHClient(host, username, key_filename=str(key_path)) as client:
            raw = client.run(
                f"{_elevation(client)}{REMOTE_ROOT}/.venv/bin/python -c {shlex.quote(code)}",
                timeout=30,
            )
    except SSHClientError as error:
        if "backend.core.channel_control" in str(error):
            raise SSHClientError("云端版本不支持独立远程操控开关，请先更新/重新部署云端小A") from error
        raise
    try:
        values = json.loads(raw)
        if not isinstance(values, dict) or any(type(values.get(name)) is not bool for name in CHANNELS):
            raise ValueError("invalid controls")
        accounts = values.get("accounts", {})
        if not isinstance(accounts, dict):
            raise ValueError("invalid account controls")
        for name in CHANNELS:
            identities = accounts.get(name, {})
            if not isinstance(identities, dict) or any(
                not isinstance(identity_name, str) or not identity_name
                or len(identity_name) > 256 or type(value) is not bool
                for identity_name, value in identities.items()
            ):
                raise ValueError("invalid account controls")
        if (source is not None and identity
                and values.get("accounts", {}).get(source, {}).get(identity) != enabled):
            raise ValueError("remote account update was not confirmed")
        if source is not None and not identity and values[source] != enabled:
            raise ValueError("remote update was not confirmed")
    except (TypeError, ValueError) as error:
        raise SSHClientError("云端远程操控开关未通过状态复查") from error
    return {**{name: values[name] for name in CHANNELS},
            "accounts": {name: dict(values.get("accounts", {}).get(name, {})) for name in CHANNELS}}
