import os
import subprocess
import unittest
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.core import process
from backend.core.process import hidden_subprocess_kwargs, reduce_desktop_startup_priority


class HiddenSubprocessTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows subprocess flags only exist on Windows")
    def test_hides_windows_console_and_keeps_process_group(self):
        plain = hidden_subprocess_kwargs()
        grouped = hidden_subprocess_kwargs(new_process_group=True)
        self.assertTrue(plain["creationflags"] & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(grouped["creationflags"] & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(grouped["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP)


class DesktopStartupPriorityTests(unittest.TestCase):
    def setUp(self):
        self.current = 32
        self.changes = []
        self.child = MagicMock()
        self.child.nice.side_effect = self.nice
        self.psutil = SimpleNamespace(
            Process=MagicMock(return_value=self.child),
            NORMAL_PRIORITY_CLASS=32, BELOW_NORMAL_PRIORITY_CLASS=16384,
        )
        self.enterContext(patch.dict(sys.modules, {"psutil": self.psutil}))
        self.enterContext(patch.object(process.os, "name", "nt"))
        self.enterContext(patch.dict(os.environ, {"XIAOA_PARENT_PID": "1234"}))

    def nice(self, value=None):
        if value is not None:
            self.current = value
            self.changes.append(value)
        return self.current

    def test_only_initialization_is_lowered_and_restore_is_idempotent(self):
        restore = reduce_desktop_startup_priority()
        self.assertEqual(self.current, 16384)
        restore()
        restore()
        self.assertEqual(self.changes, [16384, 32])

    def test_cloud_and_manual_backend_launches_keep_their_priority(self):
        with patch.dict(os.environ, {"XIAOA_PARENT_PID": ""}):
            reduce_desktop_startup_priority()()
        with patch.object(process.os, "name", "posix"):
            reduce_desktop_startup_priority()()
        self.psutil.Process.assert_not_called()

    def test_explicit_launcher_priority_is_preserved(self):
        self.current = 64
        reduce_desktop_startup_priority()()
        self.assertEqual(self.changes, [])

    def test_user_priority_change_during_startup_is_preserved(self):
        restore = reduce_desktop_startup_priority()
        self.current = 128
        restore()
        self.assertEqual(self.current, 128)
        self.assertEqual(self.changes, [16384])

    def test_access_denied_does_not_prevent_startup_or_restore(self):
        self.child.nice.side_effect = OSError("access denied")
        reduce_desktop_startup_priority()()
        self.child.nice.side_effect = self.nice
        restore = reduce_desktop_startup_priority()
        self.child.nice.side_effect = OSError("access denied")
        restore()


if __name__ == "__main__":
    unittest.main()
