"""One-time SSH password enrollment and precise key removal."""
from __future__ import annotations

import hashlib
import io
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
import paramiko

from backend.core.ssh_client import SSHClient, SSHClientError
from backend.core.process import hidden_subprocess_kwargs


def identity_path(host: str, username: str, root: str | Path) -> Path:
    if configured := os.getenv("XIAOA_CLOUD_SSH_KEY"):
        return Path(configured).expanduser().resolve()
    identity = hashlib.sha256(f"{username}@{host}".encode("utf-8")).hexdigest()[:24]
    return Path(root) / "data" / "cloud-ssh" / f"{identity}.key"


def _public_line(key: paramiko.PKey) -> str:
    body = f"{key.get_name()} {key.get_base64()}"
    marker = hashlib.sha256(body.encode("ascii")).hexdigest()[:16]
    return f"{body} xiaoa-managed-{marker}"


def _append_command(public_line: str) -> str:
    line = shlex.quote(public_line)
    return (
        "umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
        "chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys; "
        f"grep -Fqx -- {line} ~/.ssh/authorized_keys || "
        f"printf '%s\\n' {line} >> ~/.ssh/authorized_keys"
    )


def _remove_command(public_line: str) -> str:
    line = shlex.quote(public_line)
    return (
        "if [ -f ~/.ssh/authorized_keys ]; then "
        "tmp=$(mktemp); grep -Fvx -- " + line +
        " ~/.ssh/authorized_keys > \"$tmp\" || true; "
        "cat \"$tmp\" > ~/.ssh/authorized_keys; rm -f \"$tmp\"; "
        "chmod 600 ~/.ssh/authorized_keys; fi"
    )


def write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".cloud-ssh-", dir=path.parent)
    try:
        os.chmod(temp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        if os.name == "nt" and path.exists():
            _restrict_windows_file(path)
        os.replace(temp_name, path)
        if os.name == "nt":
            _restrict_windows_file(path)
        else:
            os.chmod(path, 0o600)
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _restrict_windows_file(path: Path) -> None:
    username = os.environ.get("USERNAME")
    if not username:
        raise OSError("无法确定当前 Windows 用户，不能安全保存 SSH 私钥")
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{username}:(M)"],
        capture_output=True, timeout=10, check=False,
        **hidden_subprocess_kwargs(),
    )
    if result.returncode:
        raise OSError("无法收紧 SSH 私钥的 Windows 文件权限")


def enroll(host: str, username: str, password: str, root: str | Path,
           *, port: int = 22) -> dict[str, str]:
    """Install a unique key using the supplied password, then discard password."""
    path = identity_path(host, username, root)
    if path.is_file():
        try:
            key = paramiko.RSAKey.from_private_key_file(str(path))
            public_line = _public_line(key)
            with SSHClient(host, username, port=port, key_filename=str(path)) as client:
                client.run("true", timeout=20)
            return {
                "path": str(path), "public_key": public_line,
                "username": username, "host": host,
            }
        except (OSError, paramiko.SSHException, SSHClientError):
            pass
    if not password:
        raise ValueError("首次部署或专用密钥失效，请填写远程密码")
    key = paramiko.RSAKey.generate(3072)
    public_line = _public_line(key)
    private = io.StringIO()
    key.write_private_key(private)
    private_text = private.getvalue()
    try:
        with SSHClient(host, username, port=port, password=password) as client:
            client.run(_append_command(public_line), timeout=20)
        write_private_text(path, private_text)
    finally:
        private_text = ""
        password = ""
    return {"path": str(path), "public_key": public_line, "username": username, "host": host}


def unbind(host: str, username: str, root: str | Path, *, port: int = 22) -> None:
    """Remove only this installation's exact public key and delete its private key."""
    path = identity_path(host, username, root)
    if not path.is_file():
        return
    try:
        key = paramiko.RSAKey.from_private_key_file(str(path))
        public_line = _public_line(key)
        with SSHClient(host, username, port=port, key_filename=str(path)) as client:
            client.run(_remove_command(public_line), timeout=20)
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


__all__ = ["enroll", "identity_path", "unbind", "write_private_text"]
