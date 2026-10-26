"""Offline checks for persistent, redacted desktop operation auditing."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from backend.modules.computer.operation_audit import OperationAuditLog, REDACTED, redact_parameters


class OperationAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="xiaoa-audit-")
        self.root = Path(self.temp.name)
        self.log = OperationAuditLog(self.root / "operations.sqlite3")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_default_path_uses_app_home(self):
        with patch.dict(os.environ, {"XIAOA_HOME": str(self.root)}):
            log = OperationAuditLog()
        self.assertEqual(self.root / "data" / "audit" / "operations.sqlite3", log.db_path)
        self.assertTrue(log.available)

    def test_recursive_key_and_value_redaction(self):
        params = {
            "apiKey": "sk-should-never-appear",
            "nested": {"password": "secret-value", "message": "Authorization=abc123456789"},
            "note": "prefix sk-very-secret-token-123456 suffix",
            "headers": ["Bearer abcdefghijklmnop", "ordinary text"],
        }
        record_id = self.log.record(
            session_id="session-1", task_id="task-1", tool_name="type_text", category="desktop",
            parameters=params, approval_mode="risk", approval_result="approved", status="succeeded",
            duration_ms=42, error="request token=private-token-value", window_target="ChatGPT",
            screenshot_ref="screenshots/operation-1.png",
        )
        self.assertIsInstance(record_id, int)
        item = self.log.query()["items"][0]
        self.assertEqual(REDACTED, item["parameters"]["apiKey"])
        self.assertEqual(REDACTED, item["parameters"]["nested"]["password"])
        self.assertNotIn("abc123456789", item["parameters"]["nested"]["message"])
        self.assertNotIn("very-secret-token", item["parameters"]["note"])
        self.assertEqual("Bearer " + REDACTED, item["parameters"]["headers"][0])
        self.assertNotIn("private-token-value", item["error"])
        self.assertEqual("screenshots/operation-1.png", item["screenshot_ref"])

    def test_redaction_bounds_untrusted_shapes(self):
        value = {"safe": list(range(250)), "raw": b"abcdef"}
        cleaned = redact_parameters(value)
        self.assertEqual(201, len(cleaned["safe"]))
        self.assertEqual("[bytes:6]", cleaned["raw"])
        self.assertEqual("[max-depth]", redact_parameters([[[[[[[[["deep"]]]]]]]]])[0][0][0][0][0][0][0][0])

    def test_query_filter_pagination_and_export(self):
        for index in range(5):
            self.log.record(
                session_id="session-a" if index < 3 else "session-b", task_id=f"task-{index}",
                tool_name="click_element", category="desktop", status="failed" if index == 1 else "succeeded",
            )
        first = self.log.query(session_id="session-a", page=1, page_size=2)
        second = self.log.query(session_id="session-a", page=2, page_size=2)
        self.assertEqual(3, first["total"])
        self.assertEqual(2, len(first["items"]))
        self.assertEqual(1, len(second["items"]))
        exported = self.log.export(status="succeeded", limit=10)
        self.assertEqual("xiaoa-operation-audit/v1", exported["format"])
        self.assertEqual(4, exported["count"])

    def test_cleanup_retention_and_row_cap(self):
        for index in range(105):
            self.log.record(task_id=str(index), status="succeeded")
        old = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        connection = sqlite3.connect(self.log.db_path)
        try:
            connection.execute("UPDATE operations SET created_at=? WHERE id=1", (old,))
            connection.commit()
        finally:
            connection.close()
        cleaned = self.log.cleanup(retention_days=30, max_rows=100)
        self.assertTrue(cleaned["ok"])
        self.assertEqual(100, self.log.query(page_size=200)["total"])

    def test_concurrent_writes_and_database_failure_never_raise(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            ids = list(executor.map(lambda number: self.log.record(task_id=str(number), status="succeeded"), range(80)))
        self.assertTrue(all(isinstance(item, int) for item in ids))
        self.assertEqual(80, self.log.query(page_size=100)["total"])

        broken = OperationAuditLog(self.root)  # A directory cannot be opened as a SQLite file.
        self.assertFalse(broken.available)
        self.assertIsNone(broken.record(tool_name="click_element"))
        self.assertFalse(broken.query()["ok"])

    def test_failure_marks_unavailable_and_next_success_recovers(self):
        real_connect = self.log._connect
        with patch.object(self.log, "_connect", side_effect=sqlite3.OperationalError("disk full")):
            self.assertIsNone(self.log.record(tool_name="click_element"))
        self.assertFalse(self.log.available)
        self.assertIn("disk full", self.log.last_error)
        with patch.object(self.log, "_connect", side_effect=real_connect):
            self.assertIsInstance(self.log.record(tool_name="click_element"), int)
        self.assertTrue(self.log.available)


if __name__ == "__main__":
    unittest.main()
