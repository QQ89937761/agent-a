"""Focused offline checks for the code workbench."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.core.tools import ToolRegistry
from backend.modules.computer import code_module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class CodeWorkbenchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.registry = ToolRegistry()
        self.processes = code_module.register_tools(self.registry)
        self.temp = tempfile.TemporaryDirectory(prefix="xiaoa-code-workbench-")
        self.root = Path(self.temp.name)
        self.isolation_patch = patch.object(code_module.isolation_workspace, "root", return_value=self.root / "isolation")
        self.isolation_patch.start()
        self.addCleanup(self.isolation_patch.stop)

    async def asyncTearDown(self) -> None:
        for process_id in list(self.processes._items):
            try:
                self.processes.stop(process_id)
            except Exception:
                pass
        self.temp.cleanup()

    async def test_registers_workbench_tools(self):
        expected = {
            "code_read", "code_write", "code_edit", "code_apply_edits", "code_search",
            "code_project_instructions", "code_git_status", "code_git_diff", "code_git_log",
            "code_checkpoint_create", "code_checkpoint_restore", "code_process_start",
            "code_process_poll", "code_process_write", "code_process_stop",
            "code_detect_commands", "code_verify", "code_run",
        }
        self.assertTrue(expected.issubset(set(self.registry.names())))

    async def test_multi_file_edit_validates_everything_before_writing(self):
        first, second = self.root / "a.py", self.root / "b.py"
        first.write_text("value = 1\n", encoding="utf-8")
        second.write_text("name = 'old'\n", encoding="utf-8")
        failed = json.loads(await self.registry.call("code_apply_edits", {
            "root": str(self.root),
            "edits": [
                {"path": "a.py", "old_string": "value = 1", "new_string": "value = 2"},
                {"path": "b.py", "old_string": "missing", "new_string": "new"},
            ],
        }))
        self.assertFalse(failed["ok"])
        self.assertEqual("value = 1\n", first.read_text(encoding="utf-8"))
        self.assertEqual("name = 'old'\n", second.read_text(encoding="utf-8"))

        success = json.loads(await self.registry.call("code_apply_edits", {
            "root": str(self.root),
            "edits": [
                {"path": "a.py", "old_string": "value = 1", "new_string": "value = 2"},
                {"path": "b.py", "old_string": "'old'", "new_string": "'new'"},
            ],
        }))
        self.assertTrue(success["ok"])
        self.assertIn("value = 2", first.read_text(encoding="utf-8"))
        self.assertIn("'new'", second.read_text(encoding="utf-8"))

    async def test_search_fallback_and_nested_agents_discovery(self):
        # CI temp directories can be inside the checkout; give the fixture its own boundary.
        if shutil.which("git"):
            _git(self.root, "init")
        (self.root / "AGENTS.md").write_text("root rule", encoding="utf-8")
        nested = self.root / "src" / "feature"
        nested.mkdir(parents=True)
        (self.root / "src" / "AGENTS.md").write_text("src rule", encoding="utf-8")
        (nested / "main.py").write_text("def unique_function():\n    pass\n", encoding="utf-8")
        (self.root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

        with patch("backend.modules.computer.code_module.shutil.which", return_value=None):
            found = json.loads(await self.registry.call("code_search", {
                "directory": str(self.root), "pattern": "unique_function", "ext": ".py",
            }))
        self.assertEqual("python", found["engine"])
        self.assertIn("src/feature/main.py:1", found["matches"][0])

        instructions = json.loads(await self.registry.call("code_project_instructions", {"path": str(nested)}))
        self.assertEqual(str(self.root.resolve()), instructions["project_root"])
        self.assertEqual(["root rule", "src rule"], [item["content"] for item in instructions["applicable"]])

    @unittest.skipUnless(bool(code_module.shutil.which("git")), "Git is required")
    async def test_git_status_and_checkpoint_restore_require_fresh_preview(self):
        repo = self.root / "repo"
        repo.mkdir()
        _git(repo, "init")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test")
        tracked = repo / "tracked.txt"
        tracked.write_text("committed\n", encoding="utf-8")
        _git(repo, "add", "tracked.txt")
        _git(repo, "commit", "-m", "initial")

        tracked.write_text("user checkpoint\n", encoding="utf-8")
        original_untracked = repo / "notes.txt"
        original_untracked.write_text("keep me\n", encoding="utf-8")
        status = json.loads(await self.registry.call("code_git_status", {"repo": str(repo)}))
        self.assertFalse(status["clean"])
        self.assertEqual(2, len(status["files"]))

        created = json.loads(await self.registry.call("code_checkpoint_create", {"repo": str(repo), "label": "before agent"}))
        self.assertTrue(created["ok"], created)
        checkpoint = code_module._checkpoint_dir(repo, created["checkpoint_id"])
        self.assertTrue(checkpoint.is_relative_to(self.root / "isolation" / "workspace" / "backups" / "code"))
        self.assertTrue((checkpoint / "manifest.json").is_file())
        self.assertFalse((repo / ".git" / "xiaoa-checkpoints").exists())
        tracked.write_text("agent edit\n", encoding="utf-8")
        original_untracked.write_text("agent changed note\n", encoding="utf-8")
        added_after = repo / "generated.txt"
        added_after.write_text("remove on restore\n", encoding="utf-8")

        preview = json.loads(await self.registry.call("code_checkpoint_restore", {
            "repo": str(repo), "checkpoint_id": created["checkpoint_id"],
        }))
        self.assertTrue(preview["preview"])
        tracked.write_text("changed after preview\n", encoding="utf-8")
        stale = json.loads(await self.registry.call("code_checkpoint_restore", {
            "repo": str(repo), "checkpoint_id": created["checkpoint_id"],
            "confirm_token": preview["confirm_token"],
        }))
        self.assertFalse(stale["ok"])
        self.assertEqual("changed after preview\n", tracked.read_text(encoding="utf-8"))

        fresh = json.loads(await self.registry.call("code_checkpoint_restore", {
            "repo": str(repo), "checkpoint_id": created["checkpoint_id"],
        }))
        restored = json.loads(await self.registry.call("code_checkpoint_restore", {
            "repo": str(repo), "checkpoint_id": created["checkpoint_id"],
            "confirm_token": fresh["confirm_token"],
        }))
        self.assertTrue(restored["ok"])
        self.assertEqual("user checkpoint\n", tracked.read_text(encoding="utf-8"))
        self.assertEqual("keep me\n", original_untracked.read_text(encoding="utf-8"))
        self.assertFalse(added_after.exists())

    async def test_long_process_poll_write_and_stop(self):
        command = f'"{sys.executable}" -u -c "import time; print(input(), flush=True); time.sleep(30)"'
        started = json.loads(await self.registry.call("code_process_start", {
            "command": command, "cwd": str(self.root),
        }))
        process_id = started["process_id"]
        written = json.loads(await self.registry.call("code_process_write", {
            "process_id": process_id, "text": "hello-process",
        }))
        self.assertGreater(written["written"], 0)
        output = ""
        cursor = 0
        for _ in range(30):
            polled = json.loads(await self.registry.call("code_process_poll", {
                "process_id": process_id, "cursor": cursor,
            }))
            output += polled["output"]
            cursor = polled["cursor"]
            if "hello-process" in output:
                break
            await asyncio.sleep(0.05)
        self.assertIn("hello-process", output)
        stopped = json.loads(await self.registry.call("code_process_stop", {"process_id": process_id}))
        self.assertEqual("stopped", stopped["status"])

    async def test_detect_and_run_verification(self):
        (self.root / "package.json").write_text(json.dumps({"scripts": {"test": "node test.js", "build": "node build.js"}}), encoding="utf-8")
        detected = json.loads(await self.registry.call("code_detect_commands", {"root": str(self.root)}))
        self.assertEqual(["npm run test", "npm run build"], [item["command"] for item in detected["commands"]])
        path_python = shutil.which("python") or shutil.which("python3")
        self.assertIsNotNone(path_python)
        command = f'"{Path(path_python).name}" -c "print(12345)"'
        verified = json.loads(await self.registry.call("code_verify", {
            "root": str(self.root), "command": command, "timeout": 10,
        }))
        self.assertTrue(verified["ok"])
        self.assertIn("12345", verified["output"])

    async def test_model_controlled_processes_do_not_inherit_managed_secrets(self):
        secret_name = "AGENT_LLM_API_KEY"
        command = (
            f'"{sys.executable}" -c "import os; print(os.getenv(\'{secret_name}\'))"'
        )
        with patch.dict(os.environ, {secret_name: "must-not-leak"}, clear=False):
            verified = json.loads(await self.registry.call("code_verify", {
                "root": str(self.root), "command": command, "timeout": 10,
            }))
            compatible = await self.registry.call("code_run", {
                "cwd": str(self.root), "command": command, "timeout": 10,
            })
            started = json.loads(await self.registry.call("code_process_start", {
                "cwd": str(self.root), "command": command,
            }))

        self.assertEqual("None", verified["output"].strip())
        self.assertIn("None", compatible)
        output = ""
        cursor = 0
        for _ in range(30):
            polled = json.loads(await self.registry.call("code_process_poll", {
                "process_id": started["process_id"], "cursor": cursor,
            }))
            output += polled["output"]
            cursor = polled["cursor"]
            if polled["status"] == "finished":
                break
            await asyncio.sleep(0.05)
        self.assertIn("None", output)


if __name__ == "__main__":
    unittest.main()
