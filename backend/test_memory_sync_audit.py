"""Persistence and bounded-retention checks for memory sync audit records."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.modules.memory.sync_audit import MemorySyncAudit


class MemorySyncAuditTests(unittest.TestCase):
    def test_device_id_and_events_survive_reopen_without_memory_content(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-sync-audit-") as tmp:
            path = Path(tmp) / "sync.sqlite3"
            audit = MemorySyncAudit(path)
            device_id = audit.device_id
            audit.record(request_id="req-1", device_id=device_id, status="started")
            audit.record(request_id="req-1", device_id=device_id, status="succeeded", added=2, skipped=3)
            reopened = MemorySyncAudit(path)

            self.assertEqual(device_id, reopened.device_id)
            summary = reopened.summary()
            self.assertEqual(1, summary["today_attempts"])
            self.assertEqual(1, summary["today_successes"])
            self.assertEqual(0, summary["pending"])
            self.assertTrue(summary["last_success"])
            conn = sqlite3.connect(path)
            try:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                columns = {row[1] for row in conn.execute("PRAGMA table_info(sync_events)")}
            finally:
                conn.close()
            self.assertEqual({"sync_events", "sync_meta"}, tables)
            self.assertNotIn("text", columns)

    def test_failure_is_not_counted_as_success(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-sync-audit-") as tmp:
            audit = MemorySyncAudit(Path(tmp) / "sync.sqlite3")
            audit.record(request_id="req-2", device_id="device", status="started")
            audit.record(request_id="req-2", device_id="device", status="failed", error="timeout")

            summary = audit.summary()
            self.assertEqual(1, summary["today_attempts"])
            self.assertEqual(0, summary["today_successes"])
            self.assertEqual("timeout", summary["last_error"])


if __name__ == "__main__":
    unittest.main()
