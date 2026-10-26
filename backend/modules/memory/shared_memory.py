"""Explicit shared-memory commands; write access is decided by the backend."""
from __future__ import annotations

from contextlib import contextmanager, closing
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterator

PREFIX = "共享记忆库"
DEFAULT_CONTEXT_TTL_MINUTES = 30
_USAGE = (
    "共享记忆库用法：共享记忆库 查询：关键词；共享记忆库 保存：内容；"
    "共享记忆库 修改：编号：内容；共享记忆库 删除：编号；共享记忆库 列表：页码。"
)
_SENSITIVE = re.compile(r"(?i)(password|passwd|token|secret|api[_ -]?key|private[_ -]?key|密码|口令|密钥|令牌|私钥)")


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    try:
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS shared_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )""")
        db.execute("CREATE TABLE IF NOT EXISTS shared_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.commit()
        yield db
    finally:
        db.close()


def _rows(db: sqlite3.Connection, query: str = "", page: int = 1) -> list[sqlite3.Row]:
    if not query:
        return db.execute(
            "SELECT id, content, created_by, updated_at FROM shared_memory WHERE active=1 "
            "ORDER BY updated_at DESC, id DESC LIMIT 20 OFFSET ?", ((page - 1) * 20,)
        ).fetchall()
    terms = [term for term in re.split(r"\s+", query.strip()) if term]
    sql = "SELECT id, content, created_by, updated_at FROM shared_memory WHERE active=1"
    params: list[str] = []
    for term in terms:
        sql += " AND content LIKE ?"
        params.append(f"%{term}%")
    sql += " ORDER BY updated_at DESC LIMIT 20"
    return db.execute(sql, params).fetchall()


def validate_call_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("调用名称必须是文字")
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff-]{2,30}", name) or name.lower() in {"锁定", "解锁", "lock", "unlock", "紧急锁定", "冻结", "停止", "停止任务", "停止当前任务", "取消任务", "中止", "中止任务", "暂停", "暂停任务", "停一下", "停一下吧", "先停下", "先停下吧", "先暂停", "先暂停一下", "stop", "stoptask", "canceltask", "abort"}:
        raise ValueError("调用名称须为 2～30 个汉字、字母、数字、下划线或短横线，且不能使用锁定、解锁或停止任务指令")
    return name


def _settings(db: sqlite3.Connection) -> dict:
    values = dict(db.execute("SELECT key, value FROM shared_settings"))
    return {"call_name": values.get("call_name", PREFIX), "enabled": values.get("enabled", "1") == "1"}


def context_ttl_minutes(path: Path) -> int:
    """Return the fixed lifetime for a conversational project context."""
    if not path.exists():
        return DEFAULT_CONTEXT_TTL_MINUTES
    with _connect(path) as db:
        value = dict(db.execute("SELECT key, value FROM shared_settings")).get("context_ttl_minutes", "")
    try:
        return max(5, min(240, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_TTL_MINUTES


def get_shared_settings(path: Path) -> dict:
    if not path.exists():
        return {"call_name": PREFIX, "enabled": True}
    with _connect(path) as db:
        return _settings(db)


def configure_shared_memory(path: Path, name: str, context_ttl_minutes_value: int | None = None) -> None:
    name = validate_call_name(name)
    with _connect(path) as db, db:
        values = [("call_name", name), ("enabled", "1")]
        if context_ttl_minutes_value is not None:
            if not 5 <= int(context_ttl_minutes_value) <= 240:
                raise ValueError("项目共享记忆保持时间须为 5～240 分钟")
            values.append(("context_ttl_minutes", str(int(context_ttl_minutes_value))))
        db.executemany("INSERT OR REPLACE INTO shared_settings VALUES (?, ?)", values)


def delete_shared_memory(path: Path, confirmation: str | None = None) -> None:
    # Retain a disabled, empty snapshot so cloud sync propagates deletion to workers.
    with _connect(path) as db, db:
        db.execute("PRAGMA secure_delete=ON")
        db.execute("BEGIN IMMEDIATE")
        if confirmation is not None and confirmation != _settings(db)["call_name"]:
            raise ValueError("请输入完整的共享库调用名称确认删除")
        db.execute("DELETE FROM shared_memory")
        db.execute("INSERT OR REPLACE INTO shared_settings VALUES ('enabled', '0')")


def is_shared_command(path: Path, text: str) -> bool:
    settings = get_shared_settings(path)
    return text.strip().startswith((PREFIX, settings["call_name"]))


def context_rows(path: Path, query: str, limit: int = 12) -> list[dict]:
    """Find useful project rows for the model without imposing a command grammar."""
    with _connect(path) as db:
        rows = db.execute(
            "SELECT id, content, updated_at FROM shared_memory WHERE active=1 "
            "ORDER BY updated_at DESC, id DESC"
        ).fetchall()
    query = str(query or "").strip()
    if not query:
        return [dict(row) for row in rows[:limit]]
    terms = [query]
    terms.extend(re.findall(r"(?=([\u4e00-\u9fff]{2}))", query))
    terms.extend(re.findall(r"[A-Za-z0-9_]{2,}", query.casefold()))
    scored = []
    for row in rows:
        content = str(row["content"])
        folded = content.casefold()
        score = sum((3 if term in content else 0) for term in terms if term)
        score += sum(1 for term in terms[1:] if term and term.casefold() in folded)
        if score:
            scored.append((score, row["updated_at"], dict(row)))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in scored[:limit]]


def handle_shared_command(path: Path, text: str, actor_id: str = "", *, can_write: bool = False) -> str | None:
    """Never infer write permission from text or actor-supplied account labels."""
    with _connect(path) as db, db:
        # Serialize deletion with commands; a concurrent save cannot repopulate a deleted library.
        db.execute("BEGIN IMMEDIATE")
        settings = _settings(db)
        name = settings["call_name"]
        value = text.strip()
        if not value.startswith((name, PREFIX)):
            return None
        if not settings["enabled"]:
            return "共享记忆库已删除；请在本机设置重新创建后再使用。"
        if not value.startswith(name):
            return f"调用名称已更改，请以“{name}”开头发送指令。"
        command = value[len(name):].strip().lstrip(":：").strip()
        usage = _USAGE.replace(PREFIX, name)
        return _handle_command(db, command, actor_id, can_write, usage, name)


def _handle_command(db: sqlite3.Connection, command: str, actor_id: str, can_write: bool, usage: str, name: str) -> str:
    if len(command) > 8000:
        return "共享记忆库指令过长，请控制在 8000 字以内。"
    if not command:
        return usage
    if command in {"帮助", "记忆帮助"}:
        return usage
    match = re.match(r"^(查询|搜索)\s*[:：]?\s*(.*)$", command, re.S)
    if match:
        query = match.group(2).strip()
        if not query:
            return f"请在“{name} 查询”后填写关键词。"
        rows = _rows(db, query)
        if not rows:
            return "共享记忆中没有找到相关内容。"
        return "共享查询结果：\n" + "\n".join(f"#{row['id']}：{row['content']}" for row in rows)
    page_match = re.fullmatch(r"(?:列表|列出)(?:\s*[:：]?\s*(\d{1,6}))?", command)
    if page_match:
        page = max(1, int(page_match.group(1) or 1))
        rows = _rows(db, page=page)
        return "共享记忆为空。" if not rows else f"共享记忆列表（第 {page} 页，每页 20 条）：\n" + "\n".join(
            f"#{row['id']}：{row['content']}" for row in rows
        )
    if re.match(r"^(保存|添加|修改|更新|删除|移除)", command) and not can_write:
        return "当前账号只有共享记忆库的查看权限，不能保存、修改或删除内容。"
    match = re.match(r"^(保存|添加)\s*[:：]\s*(.+)$", command, re.S)
    if match:
        content = match.group(2).strip()
        if _SENSITIVE.search(content):
            return "这条内容疑似包含密码、密钥、令牌或其他敏感凭证，不建议保存到共享记忆。"
        now = time.time()
        cursor = db.execute(
            "INSERT INTO shared_memory(content, created_by, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (content, actor_id or "weixin", now, now),
        )
        return f"已保存共享记忆 #{cursor.lastrowid}。"
    match = re.match(r"^(修改|更新)\s*[:：]?\s*(\d+)\s*[:：]\s*(.+)$", command, re.S)
    if match:
        content = match.group(3).strip()
        if _SENSITIVE.search(content):
            return "这条内容疑似包含敏感凭证，不建议保存到共享记忆。"
        cursor = db.execute(
            "UPDATE shared_memory SET content=?, updated_at=? WHERE id=? AND active=1",
            (content, time.time(), int(match.group(2))),
        )
        return "已更新共享记忆。" if cursor.rowcount else "没有找到这条共享记忆。"
    match = re.match(r"^(删除|移除)\s*[:：]?\s*(\d+)$", command)
    if match:
        cursor = db.execute("UPDATE shared_memory SET active=0, updated_at=? WHERE id=? AND active=1",
                            (time.time(), int(match.group(2))))
        return "已删除共享记忆。" if cursor.rowcount else "没有找到这条共享记忆。"
    return usage


def restore_shared_snapshot(path: Path, snapshot: bytes) -> None:
    """Restore the hub's authoritative rows, including deletions, without renumbering."""
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        source_path = Path(directory) / "shared.sqlite3"
        source_path.write_bytes(snapshot)
        with closing(sqlite3.connect(source_path)) as source:
            if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("共享记忆快照损坏")
            rows = source.execute(
                "SELECT id, content, created_by, created_at, updated_at, active FROM shared_memory"
            ).fetchall()
            has_settings = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_settings'").fetchone()
            settings = source.execute("SELECT key, value FROM shared_settings").fetchall() if has_settings else []
            values = dict(settings)
            validate_call_name(values.get("call_name", PREFIX))
            if values.get("enabled", "1") not in {"0", "1"}:
                raise ValueError("共享记忆库状态无效")
        with _connect(path) as db, db:
            db.execute("DELETE FROM shared_settings")
            db.executemany("INSERT INTO shared_settings VALUES (?, ?)", settings)
            db.execute("DELETE FROM shared_memory")
            db.executemany("INSERT INTO shared_memory VALUES (?, ?, ?, ?, ?, ?)", rows)
            db.commit()
