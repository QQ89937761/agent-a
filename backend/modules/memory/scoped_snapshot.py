"""Stable record IDs and tombstones for isolated account snapshot synchronization."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile


def prepare_scope(memory, scope: str) -> None:
    with memory._backend._db() as db:
        row = db.execute("SELECT value FROM memory_meta WHERE key='weixin_scope'").fetchone()
        if row and row[0] != scope:
            raise ValueError("记忆数据库账号归属不符")
        db.execute("INSERT OR IGNORE INTO memory_meta VALUES ('weixin_scope', ?)", (scope,))
        columns = {row[1] for row in db.execute("PRAGMA table_info(memories)")}
        if "sync_id" not in columns:
            db.execute("ALTER TABLE memories ADD COLUMN sync_id TEXT")
            db.execute("ALTER TABLE memories ADD COLUMN sync_revision REAL NOT NULL DEFAULT 0")
        db.execute("UPDATE memories SET sync_id=lower(hex(randomblob(16))), sync_revision=updated_at WHERE sync_id IS NULL")
        db.executescript("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_account_memory_sync ON memories(sync_id);
            CREATE TRIGGER IF NOT EXISTS account_memory_insert AFTER INSERT ON memories
            WHEN NEW.sync_id IS NULL BEGIN
                UPDATE memories SET sync_id=lower(hex(randomblob(16))),
                    sync_revision=(julianday('now') - 2440587.5)*86400.0 WHERE id=NEW.id;
            END;
            CREATE TRIGGER IF NOT EXISTS account_memory_update AFTER UPDATE OF text, active, pinned ON memories
            WHEN NEW.sync_revision=OLD.sync_revision BEGIN
                UPDATE memories SET sync_revision=max(
                    (julianday('now') - 2440587.5)*86400.0, OLD.sync_revision+0.000001) WHERE id=NEW.id;
            END;
        """)


def merge_scope(memory, snapshot: bytes, scope: str) -> dict[str, int]:
    columns = "namespace, kind, text, source_path, source_ref, content_hash, vector, importance, pinned, active, created_at, updated_at, sync_id, sync_revision"
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "incoming.sqlite3"
        path.write_bytes(snapshot)
        with closing(sqlite3.connect(path)) as source:
            if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("账号记忆快照损坏")
            row = source.execute("SELECT value FROM memory_meta WHERE key='weixin_scope'").fetchone()
            if not row or row[0] != scope:
                raise ValueError("账号记忆快照归属不符，不能导入其他账号")
            rows = source.execute(f"SELECT {columns} FROM memories").fetchall()
    added = skipped = 0
    with memory._backend._db() as db:
        for row in rows:
            existing = db.execute("SELECT sync_revision FROM memories WHERE sync_id=?", (row[-2],)).fetchone()
            if existing and existing[0] >= row[-1]:
                skipped += 1
                continue
            if existing:
                assignments = ", ".join(f"{name.strip()}=?" for name in columns.split(","))
                db.execute(f"UPDATE memories SET {assignments} WHERE sync_id=?", (*row, row[-2]))
            else:
                db.execute(f"INSERT INTO memories ({columns}) VALUES ({','.join('?' for _ in row)})", row)
            added += 1
    return {"added": added, "skipped": skipped}
