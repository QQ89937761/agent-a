"""Apply Feishu settings or remove the complete binding."""
from __future__ import annotations

import json
import re
import shlex
import sys
import os
import tempfile
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 cloud runtime
    import tomli as tomllib


def update_config(config_text: str, values: dict) -> str:
    match = re.search(r"(?ms)^\[feishu\]\s*\n(?P<body>.*?)(?=^\[|\Z)", config_text)
    if not match:
        raise ValueError("config.toml 缺少 [feishu] 配置段")
    body = match.group("body")
    replacements = {}
    if values.get("logout"):
        replacements.update({"app_id": json.dumps(""), "app_secret": json.dumps("")})
        replacements["remarks"] = "{}"
        values = {**values, "allowed_open_ids": []}
    elif values.get("app_id"):
        replacements["app_id"] = json.dumps(str(values["app_id"]), ensure_ascii=False)
    if values.get("logout") or "app_secret" in values:
        replacements["app_secret"] = json.dumps("")
    if "allowed_open_ids" in values:
        ids = [str(value).strip() for value in values["allowed_open_ids"] if str(value).strip()]
        replacements["allowed_open_ids"] = "[" + ", ".join(
            json.dumps(value, ensure_ascii=False) for value in ids
        ) + "]"
        existing = tomllib.loads(config_text).get("feishu", {}).get("remarks", {})
        if isinstance(existing, dict):
            replacements["remarks"] = "{" + ", ".join(
                f"{json.dumps(str(key), ensure_ascii=False)} = {json.dumps(str(value), ensure_ascii=False)}"
                for key, value in existing.items()
                if str(key) in ids and str(value).strip()
            ) + "}"
    if "remark_open_id" in values:
        feishu = tomllib.loads(config_text).get("feishu", {})
        remarks = feishu.get("remarks", {})
        if not isinstance(remarks, dict):
            remarks = {}
        open_id = str(values.get("remark_open_id", "")).strip()
        if not open_id:
            raise ValueError("飞书用户标识无效")
        remark = str(values.get("remark", "")).strip()
        if len(remark) > 80:
            raise ValueError("账号备注不能超过 80 个字符")
        if remark:
            remarks[open_id] = remark
        else:
            remarks.pop(open_id, None)
        replacements["remarks"] = "{" + ", ".join(
            f"{json.dumps(str(key), ensure_ascii=False)} = {json.dumps(str(value), ensure_ascii=False)}"
            for key, value in remarks.items() if str(key).strip() and str(value).strip()
        ) + "}"
    for name, value in replacements.items():
        pattern = rf"(?m)^{re.escape(name)}\s*=.*$"
        if re.search(pattern, body):
            body = re.sub(pattern, f"{name} = {value}", body, count=1)
        else:
            body += f"{name} = {value}\n"
    return config_text[:match.start("body")] + body + config_text[match.end("body"):]


def read_status(config_text: str, state_path: Path | None = None, env_text: str = "") -> dict:
    feishu = tomllib.loads(config_text).get("feishu", {})
    status = {
        "app_id": str(feishu.get("app_id", "")),
        "allowed_open_ids": [
            str(value) for value in feishu.get("allowed_open_ids", [])
            if str(value).strip()
        ],
        "app_secret_configured": bool(feishu.get("app_secret") or _environment(env_text).get("AGENT_FEISHU_APP_SECRET")),
    }
    remarks = feishu.get("remarks", {})
    if isinstance(remarks, dict) and remarks:
        filtered_remarks = {
            str(key): str(value)[:80] for key, value in remarks.items()
            if str(key) in status["allowed_open_ids"] and str(value).strip()
        }
        if filtered_remarks:
            status["remarks"] = filtered_remarks
    if state_path is not None:
        status.update(last_open_id="", recent_status="unrecorded", pending_available=False)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            open_id = state.get("open_id", "")
            if (state.get("app_id", status["app_id"]) == status["app_id"]
                    and isinstance(open_id, str) and open_id in status["allowed_open_ids"]):
                status.update(last_open_id=open_id, recent_status="recorded")
            candidates = _pending_users(state, status["app_id"], status["allowed_open_ids"])
            status.update(pending_available=bool(candidates), pending_open_ids=candidates)
        except FileNotFoundError:
            pass  # Older listeners do not persist a record; this says nothing about delivery.
        except (OSError, ValueError, TypeError, AttributeError):
            status["recent_status"] = "unavailable"
    return status


def _env_path(config_path: Path) -> Path:
    return Path("/etc/xiaoa-hub.env") if os.name != "nt" else config_path.parent / "xiaoa-hub.env"


def _environment(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        name, sep, raw = line.partition("=")
        if sep and name.startswith("AGENT_"):
            try:
                parsed = shlex.split(raw, posix=True)
                values[name] = parsed[0] if parsed else ""
            except ValueError:
                continue
    return values


def _pending_open_id(pending: dict, app_id: str, allowed_open_ids: list[str]) -> str:
    try:
        candidate = pending.get("open_id", "")
        age = time.time() - float(pending.get("seen_at", 0))
        if (pending.get("app_id") == app_id and isinstance(candidate, str)
                and candidate.startswith("ou_") and candidate not in allowed_open_ids
                and 0 <= age <= 600):
            return candidate
    except (AttributeError, TypeError, ValueError):
        pass
    return ""


def _pending_users(state: dict, app_id: str, allowed: list[str]) -> list[str]:
    users = state.get("pending_users", {})
    candidates = list(users.values()) if isinstance(users, dict) else []
    candidates.append(state.get("pending", {}))
    return list(dict.fromkeys(candidate for value in candidates
                             if (candidate := _pending_open_id(value, app_id, allowed))))


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--status":
        config_path = Path(sys.argv[2])
        env_path = _env_path(config_path)
        try:
            env_text = env_path.read_text(encoding="utf-8")
        except OSError:
            env_text = ""
        print(json.dumps(read_status(
            config_path.read_text(encoding="utf-8"),
            config_path.parent / "data" / "feishu-last-open-id.json", env_text,
        )))
        return
    if len(sys.argv) != 3:
        raise SystemExit("用法: update_feishu.py <输入JSON> <config.toml> | --status <config.toml>")
    payload_path, config_path = map(Path, sys.argv[1:])
    values = json.loads(payload_path.read_text(encoding="utf-8"))
    original = config_path.read_text(encoding="utf-8")
    state_path = config_path.parent / "data" / "feishu-last-open-id.json"
    if values.get("authorize_pending"):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        pending = state.get("pending", {})
        feishu = tomllib.loads(original).get("feishu", {})
        allowed = feishu.get("allowed_open_ids", [])
        candidates = _pending_users(state, feishu.get("app_id", ""), allowed)
        candidate = values.get("pending_open_id") or (candidates[0] if len(candidates) == 1 else "")
        if not candidate or candidate not in candidates:
            raise ValueError("最近未授权的飞书用户不存在或已过期，请让对方重新私聊")
        values = {**values, "allowed_open_ids": list(allowed) + [candidate]}
    env_path = _env_path(config_path)
    current_env = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    current_secret = _environment(current_env).get("AGENT_FEISHU_APP_SECRET", "")
    legacy_secret = tomllib.loads(original).get("feishu", {}).get("app_secret", "")
    if legacy_secret and "app_secret" not in values and not current_secret and not values.get("logout"):
        values = {**values, "app_secret": ""}
    updated = update_config(original, values)
    if values.get("logout") or "app_secret" in values or (legacy_secret and not current_secret):
        env_path.parent.mkdir(parents=True, exist_ok=True)
        secret = "" if values.get("logout") else str(
            values.get("app_secret") or current_secret or legacy_secret
        )
        env_updated = _update_env(current_env, secret)
        fd, env_temp = tempfile.mkstemp(prefix=".xiaoa-hub.", dir=env_path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(env_updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(env_temp, 0o600)
            os.replace(env_temp, env_path)
        finally:
            try: os.unlink(env_temp)
            except FileNotFoundError: pass
    temp = config_path.with_suffix(".toml.tmp")
    temp.write_text(updated, encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(config_path)
    if values.get("logout"):
        try:
            state_path.unlink()
        except FileNotFoundError:
            pass
    elif values.get("authorize_pending"):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("pending", {}).get("open_id") == candidate:
                state.pop("pending", None)
            state.get("pending_users", {}).pop(candidate, None)
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except (FileNotFoundError, OSError, ValueError, TypeError):
            pass


def _update_env(text: str, secret: str) -> str:
    if any(char in secret for char in "\r\n\0"):
        raise ValueError("App Secret 包含非法字符")
    lines = [line for line in text.splitlines() if not line.startswith("AGENT_FEISHU_APP_SECRET=")]
    if secret:
        lines.append("AGENT_FEISHU_APP_SECRET=" + shlex.quote(secret))
    else:
        lines.append("AGENT_FEISHU_APP_SECRET=")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
