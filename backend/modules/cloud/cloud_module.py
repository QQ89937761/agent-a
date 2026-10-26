"""Small cloud-server tools backed by the enrolled SSH identity.

The model receives only operational arguments.  Host, username and the
private key are loaded from the local cloud deployment record.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Callable

from backend.core.cloud_identity import identity_path
from backend.core.ssh_client import SSHClient, SSHClientError
from backend.core.tools import ToolRegistry


def _target(root: str | Path) -> tuple[str, str, Path]:
    record = Path(root) / "data" / "cloud-ssh" / "target.json"
    if not record.is_file():
        raise SSHClientError("尚未连接云端，请先在设置里完成一次云端部署")
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
        host = str(data["host"]).strip()
        username = str(data["username"]).strip()
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise SSHClientError("云端连接记录损坏，请重新连接云端") from exc
    key = identity_path(host, username, root)
    if not key.is_file():
        raise SSHClientError("云端专用密钥不存在，请在设置里重新连接云端")
    return host, username, key


def register_tools(
    registry: ToolRegistry,
    root: str | Path,
    *,
    client_factory: type[SSHClient] = SSHClient,
) -> None:
    """Register cloud tools. Handlers are async; Paramiko stays off the loop."""

    def with_factory(action: Callable[[SSHClient], str]) -> str:
        host, username, key = _target(root)
        with client_factory(host, username, key_filename=str(key)) as client:
            return action(client)

    @registry.tool(
        name="cloud_exec",
        description=(
            "在已连接的云服务器上执行一条 shell 命令并返回输出。"
            "只读命令可直接执行；删除、覆盖、卸载或停服等高风险命令会先请求确认。"
            "不要为此工具拼接 SSH 脚本，也不要索要密码。"
        ),
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string", "description": "要在云服务器执行的 shell 命令"},
                           "timeout": {"type": "integer", "minimum": 1, "maximum": 900}},
            "required": ["command"],
            "additionalProperties": False,
        },
        side_effect=True,
    )
    async def cloud_exec(command: str, timeout: int = 60) -> str:
        command = str(command or "").strip()
        if not command:
            return "云端命令不能为空"
        timeout = max(1, min(int(timeout), 900))
        try:
            return await asyncio.to_thread(
                with_factory,
                lambda client: client.run(command, timeout=timeout),
            )
        except SSHClientError as exc:
            return str(exc)
        except Exception as exc:
            return f"云端执行失败：{type(exc).__name__}"

    @registry.tool(
        name="cloud_read_file",
        description="读取已连接云服务器上的文本文件（只读，不会修改文件）。",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "云服务器上的文件路径"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    async def cloud_read_file(path: str) -> str:
        path = str(path or "").strip()
        if not path:
            return "云端文件路径不能为空"
        try:
            return await asyncio.to_thread(
                with_factory,
                lambda client: client.read_text(path),
            )
        except SSHClientError as exc:
            return str(exc)
        except Exception as exc:
            return f"云端读取失败：{type(exc).__name__}"

    @registry.tool(
        name="cloud_upload",
        description="把本机普通文件上传到已连接的云服务器。目标文件若会被覆盖会先请求确认。",
        parameters={
            "type": "object",
            "properties": {
                "local_path": {"type": "string", "description": "本机文件路径"},
                "remote_path": {"type": "string", "description": "云服务器目标路径"},
            },
            "required": ["local_path", "remote_path"],
            "additionalProperties": False,
        },
        side_effect=True,
    )
    async def cloud_upload(local_path: str, remote_path: str) -> str:
        local_path = str(local_path or "").strip()
        remote_path = str(remote_path or "").strip()
        if not local_path or not remote_path:
            return "本机路径和云端目标路径都不能为空"
        if not Path(local_path).is_file():
            return f"本机文件不存在：{local_path}"
        try:
            await asyncio.to_thread(
                with_factory,
                lambda client: (client.upload(local_path, remote_path) or ""),
            )
            return f"已上传到云端：{remote_path}"
        except SSHClientError as exc:
            return str(exc)
        except Exception as exc:
            return f"云端上传失败：{type(exc).__name__}"


__all__ = ["register_tools"]
