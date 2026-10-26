"""小A worker 连接串的编码、校验与脱敏。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
from dataclasses import dataclass


PREFIX = "XIAOA2"
LEGACY_PREFIX = "XIAOA1"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{16,256}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class ConnectionDetails:
    host: str
    port: int
    fingerprint: str
    token: str

    @property
    def hub_url(self) -> str:
        host = self.host
        try:
            ip = ipaddress.ip_address(host)
            if ip.version == 6:
                host = f"[{host}]"
        except ValueError:
            pass
        return f"wss://{host}:{self.port}/worker"


def _canonical(details: ConnectionDetails) -> bytes:
    return json.dumps({
        "fp": details.fingerprint.lower(),
        "host": details.host,
        "port": details.port,
        "token": details.token,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def make_connection_string(host: str, port: int, fingerprint: str, token: str) -> str:
    host = str(host).strip()
    try:
        port = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError("端口必须是数字") from exc
    fingerprint = str(fingerprint).replace(":", "").strip().lower()
    token = str(token).strip()
    _validate(host, port, fingerprint, token)
    prefix = LEGACY_PREFIX if fingerprint else PREFIX
    payload = base64.urlsafe_b64encode(_canonical(
        ConnectionDetails(host, port, fingerprint, token))).decode().rstrip("=")
    check = hashlib.sha256((prefix + "." + payload).encode("ascii")).hexdigest()[:12]
    return f"{prefix}.{payload}.{check}"


def parse_connection_string(value: str) -> ConnectionDetails:
    raw = str(value or "").strip()
    parts = raw.split(".")
    if len(parts) != 3 or parts[0] not in (PREFIX, LEGACY_PREFIX):
        raise ValueError("连接串格式无效，应以 XIAOA2. 开头")
    prefix = parts[0]
    payload, supplied = parts[1], parts[2].lower()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", payload) or not re.fullmatch(r"[0-9a-f]{12}", supplied):
        raise ValueError("连接串校验段格式无效，可能复制不完整")
    expected = hashlib.sha256((prefix + "." + payload).encode("ascii")).hexdigest()[:12]
    if not hmac.compare_digest(expected, supplied):
        raise ValueError("连接串校验失败，内容可能被改动或复制时缺字符")
    try:
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        details = ConnectionDetails(
            host=str(data["host"]), port=int(data["port"]),
            fingerprint=str(data["fp"]), token=str(data["token"]),
        )
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("连接串内容无法解码，请重新从服务器复制") from exc
    _validate(details.host, details.port, details.fingerprint, details.token)
    if (prefix == PREFIX and details.fingerprint) or (prefix == LEGACY_PREFIX and not details.fingerprint):
        raise ValueError("连接串证书校验方式与版本不匹配")
    return details


def masked_preview(details: ConnectionDetails) -> dict[str, str | int]:
    return {
        "host": details.host,
        "port": details.port,
        "fingerprint_prefix": details.fingerprint[:12],
        "hub_url": details.hub_url,
    }


def _validate(host: str, port: int, fingerprint: str, token: str) -> None:
    if not host or any(ord(ch) < 32 or ch.isspace() for ch in host):
        raise ValueError("连接串中的服务器地址无效")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        labels = host.rstrip(".").split(".")
        if len(labels) < 2 or any(not label for label in labels):
            raise ValueError("服务器地址必须是 IP 或完整域名")
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间")
    if fingerprint and not _HEX_RE.fullmatch(fingerprint):
        raise ValueError("证书指纹必须是 64 位 SHA256 十六进制值")
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError("连接口令格式无效或长度不足")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="生成小A worker 连接串")
    parser.add_argument("host")
    parser.add_argument("port", type=int)
    parser.add_argument("fingerprint")
    parser.add_argument("token")
    args = parser.parse_args()
    print(make_connection_string(args.host, args.port, args.fingerprint, args.token))


if __name__ == "__main__":
    main()
