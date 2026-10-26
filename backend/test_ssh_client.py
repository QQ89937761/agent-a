from __future__ import annotations

import io
import unittest
from unittest.mock import MagicMock, patch

from backend.core import ssh_client


class SSHClientTests(unittest.TestCase):
    def test_connect_uses_password_without_agent_or_key_scan(self):
        fake = MagicMock()
        with patch.object(ssh_client.paramiko, "SSHClient", return_value=fake):
            client = ssh_client.SSHClient("203.0.113.10", "root", password="secret")
            client.connect()
        fake.connect.assert_called_once_with(
            hostname="203.0.113.10", port=22, username="root", password="secret",
            key_filename=None, timeout=10.0, banner_timeout=10.0, auth_timeout=10.0,
            allow_agent=False, look_for_keys=False,
        )
        fake.set_missing_host_key_policy.assert_called_once()
        client.close()

    def test_authentication_error_is_readable_and_redacted(self):
        fake = MagicMock()
        fake.connect.side_effect = ssh_client.paramiko.AuthenticationException("secret")
        with patch.object(ssh_client.paramiko, "SSHClient", return_value=fake):
            with self.assertRaisesRegex(ssh_client.SSHClientError, "认证失败") as caught:
                ssh_client.SSHClient("host", "root", password="secret").connect()
        self.assertNotIn("secret", str(caught.exception))

    def test_unreachable_port_is_readable(self):
        fake = MagicMock()
        fake.connect.side_effect = ssh_client.paramiko.ssh_exception.NoValidConnectionsError(
            {("203.0.113.10", 22): OSError("refused")},
        )
        with patch.object(ssh_client.paramiko, "SSHClient", return_value=fake):
            with self.assertRaisesRegex(ssh_client.SSHClientError, "无法连接"):
                ssh_client.SSHClient("203.0.113.10", "root").connect()

    def test_run_redacts_output_and_reports_nonzero_status(self):
        fake = MagicMock()
        stdout = MagicMock()
        stdout.read.return_value = b"token=secret\n"
        stdout.channel.recv_exit_status.return_value = 0
        stderr = MagicMock()
        stderr.read.return_value = b""
        fake.exec_command.return_value = (None, stdout, stderr)
        client = ssh_client.SSHClient("host", "root", password="secret")
        client._client = fake
        self.assertEqual("token=[已隐藏]\n", client.run("printf secret"))

        stdout.channel.recv_exit_status.return_value = 7
        stderr.read.return_value = b"failed secret"
        with self.assertRaisesRegex(ssh_client.SSHClientError, "退出码 7") as caught:
            client.run("false")
        self.assertNotIn("secret", str(caught.exception))

    def test_upload_and_read_text_use_sftp(self):
        fake = MagicMock()
        sftp = MagicMock()
        sftp.open.return_value.__enter__.return_value.read.return_value = b"hello"
        fake.open_sftp.return_value.__enter__.return_value = sftp
        client = ssh_client.SSHClient("host", "root")
        client._client = fake
        with patch("pathlib.Path.is_file", return_value=True):
            client.upload("local.txt", "/tmp/remote.txt")
        self.assertEqual("hello", client.read_text("/tmp/remote.txt"))
        sftp.put.assert_called_once_with("local.txt", "/tmp/remote.txt")
        sftp.open.assert_called_once_with("/tmp/remote.txt", "rb")

    def test_upload_forwards_real_byte_progress(self):
        fake = MagicMock()
        sftp = MagicMock()
        fake.open_sftp.return_value.__enter__.return_value = sftp
        client = ssh_client.SSHClient("host", "root")
        client._client = fake
        progress = MagicMock()
        with patch("pathlib.Path.is_file", return_value=True):
            client.upload("local.txt", "/tmp/remote.txt", callback=progress)
        sftp.put.assert_called_once_with("local.txt", "/tmp/remote.txt", callback=progress)

    def test_streaming_command_reports_redacted_progress(self):
        fake = MagicMock()
        channel = MagicMock()
        channel.exit_status_ready.side_effect = [False, True]
        channel.recv_ready.side_effect = [True, False]
        channel.recv_stderr_ready.return_value = False
        channel.recv.return_value = b"install secret\n"
        channel.recv_exit_status.return_value = 0
        stdout = MagicMock(channel=channel)
        fake.exec_command.return_value = (None, stdout, None)
        client = ssh_client.SSHClient("host", "root", password="secret")
        client._client = fake
        progress = []
        output = client.run_stream("install", progress.append)
        self.assertEqual("install [已隐藏]\n", output)
        self.assertEqual(["install [已隐藏]\n"], progress)

    def test_write_text_uses_private_mode(self):
        fake = MagicMock()
        sftp = MagicMock()
        handle = sftp.open.return_value.__enter__.return_value
        fake.open_sftp.return_value.__enter__.return_value = sftp
        client = ssh_client.SSHClient("host", "root")
        client._client = fake
        client.write_text("/tmp/value.json", "{}")
        handle.write.assert_called_once_with(b"{}")
        sftp.chmod.assert_called_once_with("/tmp/value.json", 0o600)


if __name__ == "__main__":
    unittest.main()
