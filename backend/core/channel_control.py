"""Independent, durable remote-control permissions for each channel identity."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading

from backend.core.config import app_home

CHANNELS = ("weixin", "feishu")
_LOCK = threading.RLock()


def _path(home: Path | None = None) -> Path:
    return (home or app_home()) / "data" / "channel-control.json"


def _defaults() -> dict:
    return {
        "weixin": False,
        "feishu": False,
        "accounts": {name: {} for name in CHANNELS},
        "revocations": {name: 0 for name in CHANNELS},
        "account_revocations": {name: {} for name in CHANNELS},
    }


def _validate_identities(values: object, *, field: str) -> dict[str, bool]:
    if not isinstance(values, dict):
        raise ValueError("远程操控配置无效")
    result = {}
    for identity, enabled in values.items():
        if not isinstance(identity, str) or not identity or len(identity) > 256 or type(enabled) is not bool:
            raise ValueError("远程操控配置无效")
        result[identity] = enabled
    return result


def _read(home: Path | None = None) -> dict:
    default = _defaults()
    try:
        raw = _path(home).read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    if len(raw) > 4096:
        raise ValueError("远程操控配置无效")
    values = json.loads(raw)
    if not isinstance(values, dict):
        raise ValueError("远程操控配置无效")
    revocations = values.get("revocations", {})
    if not isinstance(revocations, dict):
        raise ValueError("远程操控配置无效")
    accounts = values.get("accounts", {})
    if not isinstance(accounts, dict):
        raise ValueError("远程操控配置无效")
    account_revocations = values.get("account_revocations", {})
    if not isinstance(account_revocations, dict):
        raise ValueError("远程操控配置无效")
    for name in CHANNELS:
        enabled = values.get(name, False)
        revision = revocations.get(name, 0)
        if type(enabled) is not bool or type(revision) is not int or revision < 0:
            raise ValueError("远程操控配置无效")
        default[name] = enabled
        default["revocations"][name] = revision
        default["accounts"][name] = _validate_identities(accounts.get(name, {}), field=name)
        revisions = account_revocations.get(name, {})
        if not isinstance(revisions, dict):
            raise ValueError("远程操控配置无效")
        for identity, value in revisions.items():
            if (not isinstance(identity, str) or not identity or len(identity) > 256
                    or type(value) is not int or value < 0):
                raise ValueError("远程操控配置无效")
        default["account_revocations"][name] = dict(revisions)
    return default


def status(home: Path | None = None) -> dict[str, bool]:
    values = _read(home)
    return {name: values[name] for name in CHANNELS}


def snapshot(home: Path | None = None) -> dict:
    """Return channel defaults plus explicit per-identity permissions."""
    values = _read(home)
    return {
        **{name: values[name] for name in CHANNELS},
        "accounts": {name: dict(values["accounts"][name]) for name in CHANNELS},
    }


def _effective_account_value(values: dict, source: str, identity: str,
                            first_identity: str) -> tuple[bool, int]:
    accounts = values["accounts"][source]
    revisions = values["account_revocations"][source]
    if identity in accounts:
        return accounts[identity], revisions.get(identity, 0)
    # Only an explicit account grant enables tools, matching the settings UI.
    # Legacy channel switches must never silently authorize a scanned account.
    return False, revisions.get(identity, 0)


def ensure_identities(source: str, identities: list[str], first_identity: str = "",
                      home: Path | None = None) -> dict[str, bool]:
    """Materialize identities so new accounts default to remote-control off."""
    if source not in CHANNELS:
        raise ValueError("仅支持微信、飞书身份")
    clean = list(dict.fromkeys(item.strip() for item in identities if isinstance(item, str) and item.strip()))
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock(path):
        values = _read(home)
        accounts = values["accounts"][source]
        revisions = values["account_revocations"][source]
        changed = False
        for identity in clean:
            if len(identity) > 256:
                raise ValueError("身份标识不能超过 256 字符")
            if identity not in accounts:
                accounts[identity] = False
                revisions.setdefault(identity, 0)
                changed = True
        if changed:
            _write(path, values)
        return {identity: accounts[identity] for identity in clean}


def permission(source: str, home: Path | None = None) -> tuple[bool, int]:
    if source not in CHANNELS:
        return False, -1
    try:
        values = _read(home)
        return values[source], values["revocations"][source]
    except (OSError, ValueError, TypeError):
        return False, -1  # Corrupt/unreadable permissions never grant remote control.


def identity_permission(source: str, identity: str, first_identity: str,
                        home: Path | None = None) -> tuple[bool, int]:
    """Return the independently stored permission for one channel identity."""
    if source not in CHANNELS or not identity:
        return False, -1
    try:
        values = _read(home)
        return _effective_account_value(values, source, identity, first_identity)
    except (OSError, ValueError, TypeError):
        return False, -1


@contextmanager
def _write_lock(path: Path):
    with _LOCK:
        # Cloud SSH updates run in separate processes; flock prevents lost per-channel changes.
        if os.name == "posix":
            import fcntl
            with path.with_suffix(".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield
        else:
            yield


def _write(path: Path, values: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".channel-control-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(values, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def update(source: str, enabled: bool, home: Path | None = None) -> dict[str, bool]:
    if source not in CHANNELS or type(enabled) is not bool:
        raise ValueError("仅支持微信、飞书及布尔开关值")
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock(path):
        values = _read(home)
        if values[source] and not enabled:
            values["revocations"][source] += 1
        values[source] = enabled
        _write(path, values)
    return {name: values[name] for name in CHANNELS}


def update_account(source: str, identity: str, enabled: bool,
                   home: Path | None = None) -> dict:
    if source not in CHANNELS or not isinstance(identity, str) or not identity or len(identity) > 256:
        raise ValueError("远程操控身份无效")
    if type(enabled) is not bool:
        raise ValueError("远程操控开关值无效")
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock(path):
        values = _read(home)
        accounts = values["accounts"][source]
        revisions = values["account_revocations"][source]
        previous, _ = _effective_account_value(values, source, identity, identity)
        if previous and not enabled:
            revisions[identity] = revisions.get(identity, 0) + 1
        accounts[identity] = enabled
        revisions.setdefault(identity, 0)
        _write(path, values)
    return snapshot(home)
