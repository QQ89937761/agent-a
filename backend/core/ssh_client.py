"""Small, redacting SSH/SFTP wrapper used by the local deployment flow."""
from __future__ import annotations

from pathlib import Path
import socket
import time
from typing import Callable, Iterable

import paramiko


class SSHClientError(RuntimeError):
    """An SSH operation failed without exposing credentials."""


class SSHClient:
    """Synchronous Paramiko wrapper; callers must use ``asyncio.to_thread``."""

    def __init__(self, host: str, username: str, *, port: int = 22,
                 password: str = "", key_filename: str | None = None,
                 connect_timeout: float = 10.0, secret_values: Iterable[str] = ()):
        self.host = str(host).strip()
        self.username = str(username).strip()
        self.port = int(port)
        self.password = password
        self.key_filename = key_filename
        self.connect_timeout = max(float(connect_timeout), 1.0)
        self._secrets = tuple(value for value in (password, *secret_values) if value)
        self._client: paramiko.SSHClient | None = None

    def _redact(self, text: object) -> str:
        value = str(text)
        for secret in self._secrets:
            value = value.replace(secret, "[已隐藏]")
        return value

    def connect(self) -> None:
        if not self.host or not self.username:
            raise SSHClientError("SSH 地址和用户名不能为空")
        if not 1 <= self.port <= 65535:
            raise SSHClientError("SSH 端口必须在 1 到 65535 之间")
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        # First-use onboarding has no host-key field; keep the key in memory and
        # never overwrite an existing known_hosts entry.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password or None,
                key_filename=self.key_filename or None,
                timeout=self.connect_timeout,
                banner_timeout=self.connect_timeout,
                auth_timeout=self.connect_timeout,
                allow_agent=False,
                look_for_keys=False,
            )
        except paramiko.AuthenticationException as exc:
            client.close()
            raise SSHClientError("SSH 认证失败，请检查公网 IP、用户名或密码") from exc
        except (paramiko.ssh_exception.NoValidConnectionsError, TimeoutError, OSError) as exc:
            client.close()
            raise SSHClientError("SSH 无法连接，请检查公网 IP、端口和服务器网络") from exc
        except paramiko.SSHException as exc:
            client.close()
            raise SSHClientError(f"SSH 握手失败：{self._redact(exc)}") from exc
        self._client = client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "SSHClient":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _require_client(self) -> paramiko.SSHClient:
        if self._client is None:
            raise SSHClientError("SSH 尚未连接")
        return self._client

    def run(self, command: str, *, timeout: float = 30.0) -> str:
        client = self._require_client()
        try:
            _stdin, stdout, stderr = client.exec_command(command, timeout=max(timeout, 1.0))
            output = stdout.read().decode("utf-8", "replace")
            error = stderr.read().decode("utf-8", "replace")
            status = stdout.channel.recv_exit_status()
        except (TimeoutError, OSError, paramiko.SSHException) as exc:
            raise SSHClientError(f"SSH 执行命令失败：{self._redact(exc)}") from exc
        if status:
            detail = self._redact(error.strip() or output.strip())
            raise SSHClientError(f"远程命令退出码 {status}{': ' + detail if detail else ''}")
        return self._redact(output)

    def run_stream(self, command: str, on_output: Callable[[str], None],
                   *, timeout: float = 900.0) -> str:
        client = self._require_client()
        try:
            _stdin, stdout, _stderr = client.exec_command(command, timeout=max(timeout, 1.0))
            channel = stdout.channel
            deadline = time.monotonic() + max(timeout, 1.0)
            chunks: list[str] = []
            while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
                if time.monotonic() >= deadline:
                    channel.close()
                    raise SSHClientError("远程命令执行超时，已停止等待")
                data = b""
                if channel.recv_ready():
                    data += channel.recv(65536)
                if channel.recv_stderr_ready():
                    data += channel.recv_stderr(65536)
                if data:
                    text = self._redact(data.decode("utf-8", "replace"))
                    chunks.append(text)
                    on_output(text)
                else:
                    time.sleep(0.05)
            status = channel.recv_exit_status()
        except SSHClientError:
            raise
        except (TimeoutError, socket.timeout, OSError, paramiko.SSHException) as exc:
            raise SSHClientError(f"SSH 执行命令失败：{self._redact(exc)}") from exc
        output = "".join(chunks)
        if status:
            detail = output.strip()[-4000:]
            raise SSHClientError(f"远程命令退出码 {status}{': ' + detail if detail else ''}")
        return output

    def upload(self, local_path: str | Path, remote_path: str,
               *, callback: Callable[[int, int], None] | None = None) -> None:
        client = self._require_client()
        source = Path(local_path)
        if not source.is_file():
            raise SSHClientError(f"本地文件不存在：{source}")
        try:
            with client.open_sftp() as sftp:
                sftp.put(str(source), remote_path, **({"callback": callback} if callback is not None else {}))
        except (OSError, paramiko.SSHException) as exc:
            raise SSHClientError(f"上传文件失败：{self._redact(exc)}") from exc

    def read_text(self, remote_path: str, *, timeout: float = 30.0) -> str:
        client = self._require_client()
        try:
            with client.open_sftp() as sftp:
                with sftp.open(remote_path, "rb") as handle:
                    return self._redact(handle.read().decode("utf-8"))
        except (OSError, UnicodeError, paramiko.SSHException) as exc:
            raise SSHClientError(f"读取远程文件失败：{self._redact(exc)}") from exc

    def write_text(self, remote_path: str, content: str, *, mode: int = 0o600) -> None:
        client = self._require_client()
        try:
            with client.open_sftp() as sftp:
                with sftp.open(remote_path, "wb") as handle:
                    handle.write(content.encode("utf-8"))
                sftp.chmod(remote_path, mode)
        except (OSError, UnicodeError, paramiko.SSHException) as exc:
            raise SSHClientError(f"写入远程文件失败：{self._redact(exc)}") from exc
