"""Persistent metadata-only audit for memory synchronization."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


class MemorySyncAudit:
    def __init__(self, db_path: str | Path, *, retention_days: int = 90, max_rows: int = 10_000):
        self.db_path = Path(db_path)
        self.retention_days = max(1, int(retention_days))
        self.max_rows = max(100, int(max_rows))
        self._lock = threading.RLock()
        self.last_error = ""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._db() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS sync_events (
                        request_id TEXT PRIMARY KEY,
                        device_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        added INTEGER,
                        skipped INTEGER,
                        error TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_sync_events_created
                        ON sync_events(created_at DESC);
                    CREATE TABLE IF NOT EXISTS sync_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                """)
                conn.execute(
                    "INSERT OR IGNORE INTO sync_meta(key, value) VALUES('device_id', ?)",
                    (str(uuid4()),),
                )
            self.available = True
        except Exception as exc:
            self.available = False
            self.last_error = f"{type(exc).__name__}: {exc}"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def _db(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @property
    def device_id(self) -> str:
        try:
            with self._db() as conn:
                row = conn.execute("SELECT value FROM sync_meta WHERE key='device_id'").fetchone()
            return str(row[0]) if row else ""
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return ""

    def record(
        self, *, request_id: str, device_id: str = "", status: str,
        added: int | None = None, skipped: int | None = None, error: str = "",
    ) -> bool:
        if status not in {"started", "succeeded", "failed"}:
            raise ValueError("invalid memory sync status")
        request_id = str(request_id)[:100]
        if not request_id:
            return False
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with self._lock, self._db() as conn:
                conn.execute("""
                    INSERT INTO sync_events(request_id, device_id, status, created_at, added, skipped, error)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(request_id) DO UPDATE SET
                        device_id=excluded.device_id, status=excluded.status,
                        created_at=excluded.created_at, added=excluded.added,
                        skipped=excluded.skipped, error=excluded.error
                """, (
                    request_id, str(device_id)[:100], status, now,
                    max(0, int(added)) if added is not None else None,
                    max(0, int(skipped)) if skipped is not None else None,
                    str(error)[:1000],
                ))
                cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).isoformat()
                conn.execute("DELETE FROM sync_events WHERE created_at < ?", (cutoff,))
                conn.execute("""
                    DELETE FROM sync_events WHERE request_id IN (
                        SELECT request_id FROM sync_events ORDER BY created_at DESC LIMIT -1 OFFSET ?
                    )
                """, (self.max_rows,))
            self.available = True
            self.last_error = ""
            return True
        except Exception as exc:
            self.available = False
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    def summary(self) -> dict:
        empty = {
            "today_attempts": 0, "today_successes": 0, "last_success": "",
            "last_error": "", "pending": 0, "available": self.available,
            "error": self.last_error,
        }
        try:
            local_now = datetime.now().astimezone()
            start = datetime.combine(local_now.date(), datetime.min.time(), tzinfo=local_now.tzinfo)
            start_utc = start.astimezone(timezone.utc).isoformat(timespec="seconds")
            with self._db() as conn:
                counts = conn.execute("""
                    SELECT COUNT(*) AS attempts,
                           SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS successes,
                           SUM(CASE WHEN status='started' THEN 1 ELSE 0 END) AS pending
                    FROM sync_events WHERE created_at >= ?
                """, (start_utc,)).fetchone()
                succeeded = conn.execute(
                    "SELECT created_at FROM sync_events WHERE status='succeeded' ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                failed = conn.execute(
                    "SELECT error FROM sync_events WHERE status='failed' ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            self.available = True
            self.last_error = ""
            return {
                "today_attempts": int(counts["attempts"] or 0),
                "today_successes": int(counts["successes"] or 0),
                "last_success": str(succeeded[0]) if succeeded else "",
                "last_error": str(failed[0]) if failed else "",
                "pending": int(counts["pending"] or 0),
                "available": True, "error": "",
            }
        except Exception as exc:
            self.available = False
            self.last_error = f"{type(exc).__name__}: {exc}"
            empty.update(available=False, error=self.last_error)
            return empty
