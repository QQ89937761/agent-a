"""Fail-closed redaction and fail-open persistence for desktop operation logs."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.core.config import app_home


REDACTED = "***REDACTED***"
_MAX_TEXT = 4_000
_MAX_ITEMS = 200
_SENSITIVE_KEYS = re.compile(
    r"(^|_)(api_?key|key|token|secret|password|passwd|pwd|authorization|auth|cookie|credential|private_?key|access_?key|refresh_?token|client_?secret|app_?secret)($|_)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_KNOWN_TOKEN = re.compile(r"(?i)\b(?:sk-[A-Za-z0-9_-]{8,}|gh[opusr]_[A-Za-z0-9_]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|AKIA[A-Z0-9]{12,})\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{8,})?\b")
_ASSIGNMENT = re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|pwd|authorization|access[_-]?key)=)[^&\s]+")
_GENERIC_TOKEN = re.compile(r"(?<![\w./\\-])(?=[A-Za-z0-9+/=_-]{32,}(?![\w./\\-]))(?=[A-Za-z0-9+/=_-]*[A-Za-z])(?=[A-Za-z0-9+/=_-]*\d)[A-Za-z0-9+/=_-]+")
# 媒体字段:值通常是 base64/图片二进制,不该走文本打码(否则高熵 token 正则会把整段毁成 ***REDACTED***)
_MEDIA_FIELDS = frozenset({"image", "images", "_observation_image", "_display_image", "audio", "b64", "thumbnail"})
_BASE64_BODY_RE = re.compile(r"^[A-Za-z0-9+/=]+$")


def _looks_like_media_data(value: Any) -> bool:
    """data 字段名太泛,只在值确实是 data URL 或长 base64 时才算媒体。"""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if text.startswith("data:") and ";base64," in text:
        return True
    body = text.replace("\r", "").replace("\n", "")
    return len(body) >= 64 and len(body) % 4 == 0 and bool(_BASE64_BODY_RE.match(body))


def _is_media_field(key: str, value: Any) -> bool:
    if key in _MEDIA_FIELDS:
        return True
    return key == "data" and _looks_like_media_data(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_text(value: Any, limit: int = _MAX_TEXT) -> str:
    text = str(value)
    text = _BEARER.sub(lambda match: match.group(1) + REDACTED, text)
    text = _KNOWN_TOKEN.sub(REDACTED, text)
    text = _JWT.sub(REDACTED, text)
    text = _ASSIGNMENT.sub(lambda match: match.group(1) + REDACTED, text)
    text = _GENERIC_TOKEN.sub(REDACTED, text)
    return text[:limit]


def redact_parameters(value: Any, *, _depth: int = 0, skip_media: bool = False,
                      text_limit: int = _MAX_TEXT, omit_media: bool = False) -> Any:
    """Return a bounded JSON-safe copy with secret keys and token values removed.

    skip_media=True 时媒体字段(image/_observation_image/data 等 base64)整段原样保留,
    不经过文本打码;用于发给前端/远程的事件。落盘(审计/断点)保持 skip_media=False。
    原始返回记录可提高 text_limit，但用 omit_media=True 丢弃媒体，仍保留结构与凭据限制。
    """
    if _depth >= 8:
        return "[max-depth]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value, text_limit)
    if isinstance(value, bytes):
        return f"[bytes:{len(value)}]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                result["[truncated]"] = f"{len(value) - _MAX_ITEMS} more items"
                break
            key = _redact_text(raw_key, 200)
            if omit_media and _is_media_field(raw_key, item):
                result[key] = "[媒体内容未保存]"
            elif skip_media and _is_media_field(raw_key, item):
                result[key] = item  # 媒体二进制整段原样,不文本打码
            elif _SENSITIVE_KEYS.search(key.replace("-", "_")):
                result[key] = REDACTED
            else:
                result[key] = redact_parameters(item, _depth=_depth + 1, skip_media=skip_media, text_limit=text_limit, omit_media=omit_media)
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        result = [redact_parameters(item, _depth=_depth + 1, skip_media=skip_media, text_limit=text_limit, omit_media=omit_media) for item in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            result.append(f"[truncated:{len(items) - _MAX_ITEMS}]")
        return result
    return _redact_text(value, text_limit)


class OperationAuditLog:
    """Thread-safe SQLite operation log whose public methods never raise."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        retention_days: int = 90,
        max_rows: int = 50_000,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else app_home() / "data" / "audit" / "operations.sqlite3"
        self.retention_days = max(1, min(int(retention_days), 3650))
        self.max_rows = max(100, min(int(max_rows), 1_000_000))
        self.available = False
        self.last_error = ""
        self._lock = threading.RLock()
        self._last_cleanup = 0.0
        try:
            self.db_path = self.db_path.expanduser().resolve()
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
            self.available = True
        except Exception as exc:  # Logging must never prevent application startup.
            self.last_error = _redact_text(exc)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    task_id TEXT NOT NULL DEFAULT '',
                    tool_name TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    parameters_json TEXT NOT NULL DEFAULT '{}',
                    approval_mode TEXT NOT NULL DEFAULT '',
                    approval_result TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    window_target TEXT NOT NULL DEFAULT '',
                    screenshot_ref TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_operations_created ON operations(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_operations_session ON operations(session_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_operations_task ON operations(task_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_operations_tool ON operations(tool_name, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_operations_status ON operations(status, created_at DESC);
                """
            )

    def _fail(self, exc: Exception) -> None:
        self.available = False
        self.last_error = _redact_text(exc)

    @staticmethod
    def _field(value: Any, limit: int = 300) -> str:
        return _redact_text(value or "", limit).replace("\x00", "")

    @staticmethod
    def _screenshot(value: Any) -> str:
        text = str(value or "")
        if text.lower().startswith("data:") or len(text) > 1_000:
            return "[screenshot-data-omitted]"
        return _redact_text(text, 1_000).replace("\x00", "")

    def record(
        self,
        *,
        session_id: str = "",
        task_id: str = "",
        tool_name: str = "",
        category: str = "",
        parameters: Any = None,
        approval_mode: str = "",
        approval_result: str = "",
        status: str = "",
        duration_ms: int | float = 0,
        error: Any = "",
        window_target: Any = "",
        screenshot_ref: Any = "",
        created_at: str = "",
    ) -> int | None:
        """Persist one event; return its id, or None on any logging failure."""
        try:
            payload = json.dumps(redact_parameters(parameters or {}), ensure_ascii=False, separators=(",", ":"))
            duration = max(0, min(int(duration_ms or 0), 86_400_000))
            row = (
                self._field(created_at, 80) or _utc_now(), self._field(session_id), self._field(task_id),
                self._field(tool_name), self._field(category), payload,
                self._field(approval_mode, 80), self._field(approval_result, 80), self._field(status, 80),
                duration, self._field(error, _MAX_TEXT), self._field(window_target, 1_000),
                self._screenshot(screenshot_ref),
            )
            with self._lock, self._connection() as connection:
                cursor = connection.execute(
                    """INSERT INTO operations(
                        created_at,session_id,task_id,tool_name,category,parameters_json,
                        approval_mode,approval_result,status,duration_ms,error,window_target,screenshot_ref
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    row,
                )
                record_id = int(cursor.lastrowid)
            self.available = True
            if time.monotonic() - self._last_cleanup >= 3600:
                self.cleanup()
            return record_id
        except Exception as exc:
            self._fail(exc)
            return None

    @staticmethod
    def _filters(
        *,
        session_id: str = "",
        task_id: str = "",
        tool_name: str = "",
        category: str = "",
        status: str = "",
        created_from: str = "",
        created_to: str = "",
    ) -> tuple[str, list[str]]:
        clauses: list[str] = []
        values: list[str] = []
        for column, value in (
            ("session_id", session_id), ("task_id", task_id), ("tool_name", tool_name),
            ("category", category), ("status", status),
        ):
            if value:
                clauses.append(f"{column}=?")
                values.append(str(value)[:300])
        if created_from:
            clauses.append("created_at>=?")
            values.append(str(created_from)[:80])
        if created_to:
            clauses.append("created_at<=?")
            values.append(str(created_to)[:80])
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", values

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["parameters"] = json.loads(item.pop("parameters_json"))
        except (ValueError, TypeError):
            item["parameters"] = {}
            item.pop("parameters_json", None)
        return item

    def query(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        session_id: str = "",
        task_id: str = "",
        tool_name: str = "",
        category: str = "",
        status: str = "",
        created_from: str = "",
        created_to: str = "",
    ) -> dict[str, Any]:
        """Return newest-first filtered records with bounded pagination."""
        safe_page = max(1, int(page or 1))
        safe_size = max(1, min(int(page_size or 50), 200))
        try:
            where, values = self._filters(
                session_id=session_id, task_id=task_id, tool_name=tool_name,
                category=category, status=status, created_from=created_from, created_to=created_to,
            )
            with self._lock, self._connection() as connection:
                total = int(connection.execute("SELECT COUNT(*) FROM operations" + where, values).fetchone()[0])
                rows = connection.execute(
                    "SELECT * FROM operations" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                    [*values, safe_size, (safe_page - 1) * safe_size],
                ).fetchall()
            return {"ok": True, "page": safe_page, "page_size": safe_size, "total": total, "items": [self._row(row) for row in rows]}
        except Exception as exc:
            self._fail(exc)
            return {"ok": False, "page": safe_page, "page_size": safe_size, "total": 0, "items": [], "error": self.last_error}

    def export(
        self,
        *,
        limit: int = 10_000,
        session_id: str = "",
        task_id: str = "",
        tool_name: str = "",
        category: str = "",
        status: str = "",
        created_from: str = "",
        created_to: str = "",
    ) -> dict[str, Any]:
        """Return a stable JSON-compatible export structure; no file is written."""
        maximum = max(1, min(int(limit or 10_000), 50_000))
        try:
            where, values = self._filters(
                session_id=session_id, task_id=task_id, tool_name=tool_name,
                category=category, status=status, created_from=created_from, created_to=created_to,
            )
            with self._lock, self._connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM operations" + where + " ORDER BY id ASC LIMIT ?", [*values, maximum]
                ).fetchall()
            items = [self._row(row) for row in rows]
            return {
                "ok": True,
                "format": "xiaoa-operation-audit/v1",
                "exported_at": _utc_now(),
                "count": len(items),
                "truncated": len(items) >= maximum,
                "items": items,
            }
        except Exception as exc:
            self._fail(exc)
            return {"ok": False, "format": "xiaoa-operation-audit/v1", "count": 0, "items": [], "error": self.last_error}

    def cleanup(self, *, retention_days: int | None = None, max_rows: int | None = None) -> dict[str, Any]:
        """Delete expired records, then retain only the newest max_rows records."""
        days = self.retention_days if retention_days is None else max(1, min(int(retention_days), 3650))
        rows = self.max_rows if max_rows is None else max(100, min(int(max_rows), 1_000_000))
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            with self._lock, self._connection() as connection:
                expired = connection.execute("DELETE FROM operations WHERE created_at<?", (cutoff,)).rowcount
                overflow = connection.execute(
                    """DELETE FROM operations WHERE id IN (
                        SELECT id FROM operations ORDER BY id DESC LIMIT -1 OFFSET ?
                    )""",
                    (rows,),
                ).rowcount
            self._last_cleanup = time.monotonic()
            return {"ok": True, "deleted": max(0, expired) + max(0, overflow), "retention_days": days, "max_rows": rows}
        except Exception as exc:
            self._fail(exc)
            return {"ok": False, "deleted": 0, "error": self.last_error}
