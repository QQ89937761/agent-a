"""Command completion must not depend on a launched GUI closing its output handles."""
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.modules.computer import system_control


class ConsoleCommandTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows shell handle inheritance regression")
    def test_inherited_output_handles_do_not_delay_exit_or_timeout(self):
        import psutil

        for parent_sleep in (0, 10):
            with self.subTest(parent_sleep=parent_sleep), TemporaryDirectory() as root:
                release = Path(root) / "release"
                pid_file = Path(root) / "pid"
                child_file = Path(root) / "child.py"
                child_file.write_text(
                    "import pathlib,time,tempfile,os,sys\n"
                    "os.chdir(tempfile.gettempdir())\n"
                    "release=pathlib.Path(sys.argv[1]); deadline=time.monotonic()+6\n"
                    "while not release.exists() and time.monotonic()<deadline: time.sleep(.02)\n",
                    encoding="utf-8",
                )
                parent_file = Path(root) / "parent.py"
                parent_file.write_text(
                    "import subprocess,sys,pathlib,time,os,tempfile\n"
                    "p=subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]], stdout=sys.stdout, stderr=sys.stderr)\n"
                    "pathlib.Path(sys.argv[3]).write_text(str(p.pid)+' '+str(os.getpid()))\n"
                    "release=pathlib.Path(sys.argv[2]); os.chdir(tempfile.gettempdir())\n"
                    "deadline=time.monotonic()+float(sys.argv[4])\n"
                    "while not release.exists() and time.monotonic()<deadline: time.sleep(.02)\n",
                    encoding="utf-8",
                )
                command = subprocess.list2cmdline([
                    sys.executable, str(parent_file), str(child_file), str(release), str(pid_file), str(parent_sleep),
                ])
                started = time.monotonic()
                try:
                    if parent_sleep:
                        with self.assertRaises(subprocess.TimeoutExpired):
                            system_control._run_console_command(command, root, timeout=1)
                    else:
                        result = system_control._run_console_command(command, root, timeout=1)
                        self.assertEqual(0, result.returncode)
                    self.assertLess(time.monotonic() - started, 3)
                    self.assertTrue(pid_file.exists(), "child must actually inherit the handles")
                    self.assertTrue(psutil.pid_exists(int(pid_file.read_text().split()[0])))
                finally:
                    release.touch()
                    if pid_file.exists():
                        for pid in pid_file.read_text().split():
                            try:
                                psutil.Process(int(pid)).wait(timeout=7)
                            except psutil.NoSuchProcess:
                                pass

    def test_output_is_bytes_bounded_and_secrets_are_filtered(self):
        def execute(*args, **kwargs):
            self.assertNotIn("AGENT_LLM_API_KEY", kwargs["env"])
            self.assertIs(kwargs["stderr"], subprocess.STDOUT)
            self.assertEqual(subprocess.DEVNULL, kwargs["stdin"])
            kwargs["stdout"].write(b"x" * 100000)
            return subprocess.CompletedProcess(args[0], 7)

        with patch.dict(os.environ, {"AGENT_LLM_API_KEY": "test-only"}), \
             patch.object(system_control.subprocess, "run", side_effect=execute):
            result = system_control._run_console_command("test", os.getcwd())
        self.assertEqual(7, result.returncode)
        self.assertEqual(b"x" * 65536, result.stdout)
        self.assertEqual(b"", result.stderr)
