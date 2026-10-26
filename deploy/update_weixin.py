"""Safely update the isolated WeChat ClawBot state file on the cloud hub."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import sqlite3
from contextlib import closing
from urllib.request import Request, urlopen
from pathlib import Path


SECRET_FIELDS = {"bot_token", "ilink_bot_id", "baseurl", "ilink_user_id"}



def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _accounts(current: dict) -> dict:
    accounts = current.get("accounts")
    if isinstance(accounts, dict):
        return {str(key): value for key, value in accounts.items() if isinstance(value, dict)}
    if current.get("bot_token"):
        key = str(current.get("account_id") or current.get("ilink_bot_id") or current.get("ilink_user_id") or "default")
        return {key: {**current, "account_id": key}}
    return {}


def memory_owner(path: Path, accounts: dict, new_owner: str = "") -> str:
    catalog = path.parent / "weixin-memory" / "bindings.sqlite3"
    if not accounts and not catalog.exists():
        return ""
    catalog.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(catalog, timeout=15)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS bindings (account TEXT PRIMARY KEY, identity TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for account_id, config in accounts.items():
            identity = "user:" + str(config["ilink_user_id"]) if config.get("ilink_user_id") else "account:" + account_id
            row = db.execute("SELECT identity FROM bindings WHERE account=?", (account_id,)).fetchone()
            if row and row[0] != identity:
                raise ValueError("微信账号记忆身份冲突")
            db.execute("INSERT OR IGNORE INTO bindings VALUES (?, ?)", (account_id, identity))
            db.execute("INSERT OR IGNORE INTO settings VALUES ('owner', ?)", (identity,))
        if new_owner:
            if new_owner not in accounts:
                raise ValueError("指定微信账号尚未连接")
            identity = db.execute("SELECT identity FROM bindings WHERE account=?", (new_owner,)).fetchone()[0]
            db.execute("INSERT OR REPLACE INTO settings VALUES ('owner', ?)", (identity,))
        row = db.execute("SELECT value FROM settings WHERE key='owner'").fetchone()
        return row[0] if row else ""


def shared_memory_module(path: Path):
    # Installed cloud code owns parsing/storage; older hubs must be upgraded first.
    sys.path.insert(0, str(path.parent.parent))
    from backend.modules.memory import shared_memory
    return shared_memory


def read_status(path: Path) -> dict:
    current = _read(path)
    accounts = _accounts(current)
    owner = memory_owner(path, accounts)
    module = shared_memory_module(path)
    supported = hasattr(module, "get_shared_settings")
    shared = module.get_shared_settings(path.parent / "shared-memory.sqlite3") if supported else {"call_name": "共享记忆库", "enabled": True}
    shared["configurable"] = supported
    items = [{
        "account_id": key, "ilink_user_id": str(cfg.get("ilink_user_id", "")),
        "remark": str(cfg.get("remark", ""))[:80],
        "shared_memory_owner": owner == ("user:" + str(cfg["ilink_user_id"]) if cfg.get("ilink_user_id") else "account:" + key),
        "connected": bool(cfg.get("connected") and cfg.get("bot_token")),
        "allowed_user_ids": [str(uid) for uid in cfg.get("allowed_user_ids", []) if str(uid).strip()],
    } for key, cfg in accounts.items()]
    try:
        from backend.modules.memory import shared_libraries
    except ImportError:
        libraries = None
    else:
        shared_libraries.ensure_catalog(path.parent / "shared-memory.sqlite3", owner,
            ["user:" + str(cfg["ilink_user_id"]).strip() if cfg.get("ilink_user_id") else "account:" + key for key, cfg in accounts.items()])
        libraries = shared_libraries.list_libraries(path.parent / "shared-memory.sqlite3")
    return {
        "shared_libraries": libraries,
        "connected": any(item["connected"] for item in items),
        "account_id": items[0]["account_id"] if len(items) == 1 else "",
        "allowed_user_ids": sorted({str(uid) for cfg in accounts.values() for uid in cfg.get("allowed_user_ids", []) if str(uid).strip()}),
        "accounts": items, "shared_memory": shared,
    }


def project_status(path: Path) -> dict:
    shared_memory_module(path)
    try:
        from backend.modules.memory import shared_libraries
        if shared_libraries.CAPABILITY != "cross-channel-v1":
            raise ImportError("unsupported project library capability")
    except (ImportError, AttributeError) as exc:
        raise ValueError("云端版本不支持微信/飞书项目共享库，请先部署 / 更新云端") from exc
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    config_path = path.parent.parent / "config.toml"
    if config_path.exists():
        with config_path.open("rb") as handle:
            feishu = tomllib.load(handle).get("feishu", {})
    else:
        feishu = {}
    accounts = _accounts(_read(path))
    owner = memory_owner(path, accounts)
    shared = path.parent / "shared-memory.sqlite3"
    shared_libraries.ensure_catalog(shared, owner,
        ["user:" + str(cfg["ilink_user_id"]).strip() if cfg.get("ilink_user_id") else "account:" + key for key, cfg in accounts.items()])
    return {"ok": True, "capability": shared_libraries.CAPABILITY,
            "accounts": shared_libraries.authorized_accounts(accounts, feishu),
            "libraries": shared_libraries.list_libraries(shared)}


def update(path: Path, values: dict) -> None:
    current = _read(path)
    accounts = _accounts(current)
    if "project_library" in values:
        status = project_status(path)
        from backend.modules.memory import shared_libraries
        shared_libraries.manage_accounts(path.parent / "shared-memory.sqlite3", status["accounts"], values["project_library"])
        return
    if "shared_library" in values:
        shared_memory_module(path)
        try:
            from backend.modules.memory import shared_libraries
        except ImportError as exc:
            raise ValueError("云端版本不支持多项目共享库，请先部署 / 更新云端") from exc
        shared_libraries.manage(path.parent / "shared-memory.sqlite3", accounts, memory_owner(path, accounts), values["shared_library"])
        return
    if "shared_memory_call_name" in values or "delete_shared_memory" in values:
        module = shared_memory_module(path)
        if not hasattr(module, "configure_shared_memory"):
            raise ValueError("云端版本暂不支持共享库设置，请先部署 / 更新云端")
        shared = path.parent / "shared-memory.sqlite3"
        try:
            from backend.modules.memory.shared_libraries import legacy_settings
        except ImportError:
            raise ValueError("云端版本不支持多项目共享库，请先部署 / 更新云端")
        if "delete_shared_memory" in values:
            legacy_settings(shared, confirmation=values["delete_shared_memory"])
        else:
            legacy_settings(shared, name=values["shared_memory_call_name"])
        return
    if "memory_owner_account_id" in values:
        account_id = values["memory_owner_account_id"]
        if not isinstance(account_id, str) or not account_id or len(account_id) > 256:
            raise ValueError("共享库管理员账号无效")
        owner = memory_owner(path, accounts, account_id)
        try:
            from backend.modules.memory.shared_libraries import legacy_owner
        except ImportError:
            return
        legacy_owner(path.parent / "shared-memory.sqlite3", owner)
        return
    if values.get("logout"):
        if values.get("account_id"):
            accounts.pop(str(values["account_id"]), None)
        else:
            accounts = {}
    else:
        if "allowed_user_ids" in values:
            ids = values["allowed_user_ids"]
            if not isinstance(ids, list):
                raise ValueError("allowed_user_ids must be a list")
            ids = sorted({str(item).strip() for item in ids if str(item).strip()})
        else:
            ids = None
        if values.get("bot_token"):
            if not all(values.get(field) for field in ("ilink_bot_id", "baseurl")):
                raise ValueError("missing required WeChat credentials")
            key = str(values.get("account_id") or values["ilink_bot_id"])
            previous = accounts.get(key, {})
            accounts[key] = {
                "account_id": key, **{field: str(values.get(field, "")) for field in SECRET_FIELDS},
                "remark": str(previous.get("remark", ""))[:80],
                "allowed_user_ids": ids if ids is not None else previous.get("allowed_user_ids", []),
                "connected": True, "get_updates_buf": "", "last_user_id": "",
                "last_context_token": "", "user_contexts": {}, "seen_message_ids": [],
            }
        elif ids is not None:
            for key, cfg in accounts.items():
                if values.get("account_id") and key != str(values["account_id"]):
                    continue
                cfg["allowed_user_ids"] = list(ids)
                cfg["user_contexts"] = {uid: token for uid, token in cfg.get("user_contexts", {}).items() if uid in ids}
        elif "remark" in values:
            key = str(values.get("account_id", ""))
            if not key or key not in accounts:
                raise ValueError("微信账号不存在")
            remark = str(values.get("remark", "")).strip()
            if len(remark) > 80:
                raise ValueError("账号备注不能超过 80 个字符")
            accounts[key]["remark"] = remark
    memory_owner(path, accounts)
    current = {"accounts": accounts, "allowed_user_ids": sorted({uid for cfg in accounts.values() for uid in cfg.get("allowed_user_ids", [])})}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".weixin-clawbot-", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(current, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
        path.chmod(0o600)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def login_qrcode(path: Path) -> dict:
    tokens = [str(config["bot_token"]) for config in _accounts(_read(path)).values() if config.get("bot_token")][-10:]
    payload = json.dumps({"local_token_list": tokens}).encode("utf-8")
    request = Request("https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3",
                      data=payload, method="POST",
                      headers={"iLink-App-Id": "bot", "iLink-App-ClientVersion": "65536",
                               "Content-Type": "application/json"})
    with urlopen(request, timeout=25) as response:
        data = json.load(response)
    if not data.get("qrcode") or not data.get("qrcode_img_content"):
        raise ValueError("微信没有返回可用二维码")
    return {key: data[key] for key in ("qrcode", "qrcode_img_content")}


def shared_backup(path: Path) -> dict:
    import base64
    shared_memory_module(path)
    from backend.modules.memory import shared_libraries
    accounts = _accounts(_read(path))
    owner = memory_owner(path, accounts)
    shared = path.parent / "shared-memory.sqlite3"
    shared_libraries.ensure_catalog(shared, owner,
        ["user:" + str(cfg["ilink_user_id"]).strip() if cfg.get("ilink_user_id") else "account:" + key for key, cfg in accounts.items()])
    return {"ok": True, "files": {name: base64.b64encode(data).decode()
            for name, data in shared_libraries.export_files(shared).items()}}


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "--project-memory":
        path = Path(sys.argv[3])
        shared_memory_module(path)
        from backend.modules.memory import shared_libraries
        if not hasattr(shared_libraries, "desktop_context"):
            raise ValueError("云端版本尚未支持桌面共享项目对话，请先更新云端")
        values = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        result = shared_libraries.desktop_context(path.parent / "shared-memory.sqlite3", **values)
        print(json.dumps(result, ensure_ascii=False))
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--projects":
        print(json.dumps(project_status(Path(sys.argv[2])), ensure_ascii=False))
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--shared-backup":
        print(json.dumps(shared_backup(Path(sys.argv[2]))))
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--qrcode":
        print(json.dumps(login_qrcode(Path(sys.argv[2])), ensure_ascii=False))
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--status":
        print(json.dumps(read_status(Path(sys.argv[2])), ensure_ascii=False))
        return
    if len(sys.argv) != 3:
        raise SystemExit("usage: update_weixin.py <input.json> <state.json> | --status <state.json>")
    payload_path, state_path = map(Path, sys.argv[1:])
    values = json.loads(payload_path.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise ValueError("input must be a JSON object")
    update(state_path, values)


if __name__ == "__main__":
    main()
