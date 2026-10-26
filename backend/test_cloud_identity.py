from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.core import cloud_identity


class CloudIdentityTests(unittest.TestCase):
    def test_enroll_appends_one_marked_key_and_keeps_password_out_of_disk(self):
        fake = MagicMock()
        fake.__enter__.return_value = fake
        with tempfile.TemporaryDirectory() as tmp, patch.object(cloud_identity, "SSHClient", return_value=fake):
            result = cloud_identity.enroll("203.0.113.10", "root", "root-secret", tmp)
            command = fake.run.call_args.args[0]
            self.assertIn("xiaoa-managed-", command)
            self.assertNotIn("root-secret", command)
            private = Path(result["path"])
            self.assertTrue(private.is_file())
            self.assertNotIn("root-secret", private.read_text(encoding="utf-8"))
            if os.name != "nt":
                self.assertEqual(0o600, private.stat().st_mode & 0o777)
            self.assertEqual("root", result["username"])

    def test_enroll_failure_does_not_leave_private_key(self):
        fake = MagicMock()
        fake.__enter__.return_value = fake
        fake.run.side_effect = RuntimeError("remote failed")
        with tempfile.TemporaryDirectory() as tmp, patch.object(cloud_identity, "SSHClient", return_value=fake):
            with self.assertRaises(RuntimeError):
                cloud_identity.enroll("host", "root", "secret", tmp)
            self.assertFalse(list((Path(tmp) / "data" / "cloud-ssh").glob("*.key")))

    def test_enroll_reuses_working_identity_without_adding_another_key(self):
        fake = MagicMock()
        fake.__enter__.return_value = fake
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(cloud_identity, "SSHClient", return_value=fake) as ssh_type:
            first = cloud_identity.enroll("host", "root", "secret", tmp)
            fake.reset_mock()
            ssh_type.reset_mock()
            second = cloud_identity.enroll("host", "root", "", tmp)
        self.assertEqual(first["path"], second["path"])
        self.assertEqual("true", fake.run.call_args.args[0])
        self.assertEqual(str(Path(first["path"])), ssh_type.call_args.kwargs["key_filename"])

    def test_first_deployment_without_password_is_rejected_without_network_or_key_generation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(cloud_identity, "SSHClient") as ssh_type:
            with self.assertRaisesRegex(ValueError, "首次部署"):
                cloud_identity.enroll("host", "root", "", tmp)
            ssh_type.assert_not_called()

    def test_windows_acl_output_is_never_decoded_as_utf8(self):
        import subprocess
        import sys
        original_run = subprocess.run
        def gbk_command(command, **kwargs):
            kwargs.pop("creationflags", None)
            # The command's output is unused; GBK bytes must remain bytes.
            return original_run([sys.executable, "-c",
                "import sys;sys.stdout.buffer.write(bytes([0xd2,0xd1]))"], **kwargs)
        with patch.dict(os.environ, {"USERNAME": "test-user"}), \
                patch.object(cloud_identity.subprocess, "run", side_effect=gbk_command):
            cloud_identity._restrict_windows_file(Path("unused-private-file"))
        with patch.dict(os.environ, {"USERNAME": "test-user"}), \
                patch.object(cloud_identity.subprocess, "run", return_value=MagicMock(returncode=1)):
            with self.assertRaises(OSError):
                cloud_identity._restrict_windows_file(Path("unused-private-file"))

    def test_private_file_can_be_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.txt"
            cloud_identity.write_private_text(path, "first")
            cloud_identity.write_private_text(path, "second")
            self.assertEqual("second", path.read_text(encoding="utf-8"))

    def test_unbind_removes_exact_key_and_private_file(self):
        fake = MagicMock()
        fake.__enter__.return_value = fake
        with tempfile.TemporaryDirectory() as tmp, patch.object(cloud_identity, "SSHClient", return_value=fake):
            result = cloud_identity.enroll("host", "root", "secret", tmp)
            cloud_identity.unbind("host", "root", tmp)
            command = fake.run.call_args.args[0]
            self.assertIn("grep -Fvx", command)
            self.assertIn("xiaoa-managed-", command)
            self.assertFalse(Path(result["path"]).exists())
            second_call = cloud_identity.SSHClient.call_args_list[1]
            self.assertNotIn("password", second_call.kwargs)
            self.assertTrue(second_call.kwargs["key_filename"].endswith(".key"))


if __name__ == "__main__":
    unittest.main()
