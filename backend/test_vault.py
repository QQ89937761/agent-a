"""Credential recovery and concurrent updates without requiring native DPAPI."""
import base64
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from backend.core import vault


class VaultTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.target = Path(folder.name) / "secrets.dpapi"
        for mock in (patch.object(vault, "_path", return_value=self.target),
                     patch.object(vault, "_WINDOWS", True),
                     patch.object(vault, "_crypt", side_effect=lambda data, protect: data)):
            mock.start()
            self.addCleanup(mock.stop)

    def test_recovery_preserves_bytes_and_never_overwrites_backup(self):
        for payload in (b"not base64", base64.b64encode(b"[]")):
            self.target.write_bytes(payload)
            self.assertEqual("", vault.get("old"))
            vault.put("new", "value")
            self.assertEqual("value", vault.get("new"))
        self.assertEqual(b"not base64", self.target.with_name("secrets.dpapi.bak").read_bytes())
        self.assertEqual(base64.b64encode(b"[]"), self.target.with_name("secrets.dpapi.1.bak").read_bytes())

    def test_dpapi_failure_is_backed_up_before_delete(self):
        self.target.write_bytes(base64.b64encode(b"old-user-encrypted"))
        with patch.object(vault, "_crypt", side_effect=OSError("different user")):
            vault.put("old", "")
        self.assertFalse(self.target.exists())
        self.assertEqual(base64.b64encode(b"old-user-encrypted"),
                         self.target.with_name("secrets.dpapi.bak").read_bytes())

    def test_backup_failure_and_unsupported_platform_leave_original_untouched(self):
        self.target.write_bytes(b"original")
        original_open = Path.open
        def open_file(path, *args, **kwargs):
            if path.suffix == ".bak":
                raise PermissionError("denied")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", new=open_file):
            with self.assertRaises(PermissionError):
                vault.put("new", "value")
        self.assertEqual(b"original", self.target.read_bytes())
        with patch.object(vault, "_WINDOWS", False):
            self.assertEqual("", vault.get("key"))
            with self.assertRaises(RuntimeError):
                vault.put("new", "value")
        self.assertEqual(b"original", self.target.read_bytes())
        self.assertEqual([], list(self.target.parent.glob("*.bak")))

    def test_concurrent_updates_keep_every_key(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda index: vault.put(str(index), str(index)), range(40)))
        saved = json.loads(base64.b64decode(self.target.read_bytes()))
        self.assertEqual({str(i): str(i) for i in range(40)}, saved)
