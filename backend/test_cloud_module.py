from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from backend.core.agent import Agent, _remote_confirmation_categories
from backend.core.config import AgentConfig
from backend.core.tools import Tool, ToolRegistry
from backend.modules.cloud import cloud_module


class _FakeSSH:
    instances: list["_FakeSSH"] = []

    def __init__(self, host: str, username: str, *, key_filename: str):
        self.host = host
        self.username = username
        self.key_filename = key_filename
        self.commands: list[str] = []
        self.uploads: list[tuple[str, str]] = []
        self.closed = False
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def run(self, command: str, *, timeout: float = 30.0) -> str:
        self.commands.append(command)
        return "cloud output"

    def read_text(self, path: str) -> str:
        return f"contents of {path}"

    def upload(self, local_path: str, remote_path: str) -> None:
        self.uploads.append((local_path, remote_path))


class CloudModuleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _FakeSSH.instances.clear()

    def _root(self) -> Path:
        root = Path(tempfile.mkdtemp())
        record = root / "data" / "cloud-ssh"
        record.mkdir(parents=True)
        (record / "target.json").write_text(
            '{"host":"203.0.113.10","username":"root"}', encoding="utf-8",
        )
        # identity_path only requires the enrolled key file to exist here.
        (record / "5c3d606a7c154ab068e8ab1d.key").write_text("key", encoding="utf-8")
        return root

    async def test_unenrolled_target_returns_human_message_and_schema_has_no_credentials(self):
        registry = ToolRegistry()
        cloud_module.register_tools(registry, tempfile.mkdtemp(), client_factory=_FakeSSH)
        tool = registry.get("cloud_exec")
        self.assertIsNotNone(tool)
        self.assertNotIn("password", str(tool.parameters).lower())
        result = await tool.handler(command="uname -a")
        self.assertIn("尚未连接云端", result)

    async def test_tools_use_enrolled_identity_and_run_off_event_loop(self):
        root = self._root()
        registry = ToolRegistry()
        cloud_module.register_tools(registry, root, client_factory=_FakeSSH)
        self.assertEqual("cloud output", await registry.call("cloud_exec", {"command": "cat /etc/os-release"}))
        self.assertEqual("contents of /var/log/syslog", await registry.call(
            "cloud_read_file", {"path": "/var/log/syslog"},
        ))
        local = root / "note.txt"
        local.write_text("hello", encoding="utf-8")
        self.assertIn("已上传", await registry.call("cloud_upload", {
            "local_path": str(local), "remote_path": "/tmp/note.txt",
        }))
        self.assertEqual("root", _FakeSSH.instances[0].username)
        self.assertTrue(all(item.key_filename for item in _FakeSSH.instances))

    def test_remote_confirmation_distinguishes_read_only_and_destructive_commands(self):
        self.assertEqual(set(), _remote_confirmation_categories(
            "cloud_exec", {"command": "docker ps; cat /etc/os-release 2>/dev/null"},
        ))
        self.assertIn("delete", _remote_confirmation_categories(
            "cloud_exec", {"command": "rm -f /tmp/old.log"},
        ))
        self.assertIn("overwrite", _remote_confirmation_categories(
            "cloud_exec", {"command": "printf x > /tmp/out.txt"},
        ))
        self.assertIn("overwrite", _remote_confirmation_categories(
            "cloud_upload", {"local_path": "note.txt", "remote_path": "/tmp/note.txt"},
        ))
        self.assertEqual({"password"}, _remote_confirmation_categories(
            "cloud_exec", {"command": "cat password.txt"},
        ))
        self.assertEqual(set(), _remote_confirmation_categories(
            "cloud_exec", {"command": "grep delete /var/log/app.log"},
        ))

    def test_remote_service_stop_commands_require_confirmation(self):
        commands = (
            "systemctl stop xiaoa-hub",
            "systemctl disable xiaoa-hub",
            "systemctl kill xiaoa-hub",
            "systemctl stop zhxa",
            "service nginx stop",
            "docker compose down",
            "docker-compose down",
            "docker-compose stop",
            "docker-compose -f a.yml down",
            "docker stop zhxa",
            "podman stop zhxa",
            "pkill -f uvicorn",
            "killall uvicorn",
            "kill -9 1234",
            "kill -SIGKILL 1234",
            "kill -SIGTERM 1234",
            "kill -TERM 1234",
            "kill -15 1234",
            "kill 1234",
            "supervisorctl stop xiaoa",
            "nginx -s stop",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIn(
                    "service_stop",
                    _remote_confirmation_categories("cloud_exec", {"command": command}),
                )

    def test_remote_read_only_commands_remain_unblocked(self):
        commands = (
            "systemctl status xiaoa-hub",
            "service nginx status",
            "docker ps",
            "journalctl -u xiaoa-hub -n 50",
            "ss -tlnp",
            "df -h",
            "ps aux",
            "pkill -0 1234",
            "grep kill somefile",
            "ps aux | grep -c killall",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(
                    set(),
                    _remote_confirmation_categories("cloud_exec", {"command": command}),
                )

    async def test_remote_maintenance_approval_is_reused_by_category_only(self):
        registry = ToolRegistry()
        called = []

        async def cloud_exec(command: str):
            called.append(command)
            return "ok"

        registry.register(Tool(
            "cloud_exec", "云端运行", {"type": "object"}, cloud_exec, side_effect=True,
        ))

        class MaintenanceLLM:
            def __init__(self):
                self.turn = 0
                self.commands = (
                    "rm -f /tmp/old-1",
                    "rm -f /tmp/old-2",
                    "printf x > /tmp/new",
                )

            async def chat_stream_tools(self, messages, tools=None, **kwargs):
                self.turn += 1
                if self.turn <= len(self.commands):
                    yield "done", {"role": "assistant", "content": None, "tool_calls": [{
                        "id": f"call-{self.turn}", "type": "function",
                        "function": {"name": "cloud_exec", "arguments":
                                      "{\"command\":\"" + self.commands[self.turn - 1] + "\"}"},
                    }]}
                else:
                    yield "done", {"role": "assistant", "content": "完成"}

        confirm = AsyncMock(return_value=True)
        events = [event async for event in Agent(
            MaintenanceLLM(), registry, AgentConfig(confirm_before_action=True), confirm_fn=confirm,
        ).run("清理并更新云端文件", approval_mode="remote")]

        self.assertEqual([
            "rm -f /tmp/old-1", "rm -f /tmp/old-2", "printf x > /tmp/new",
        ], called)
        self.assertEqual(1, confirm.await_count)
        self.assertEqual(1, sum(event.type == "confirm_needed" for event in events))
        confirmation = next(event for event in events if event.type == "confirm_needed")
        self.assertEqual("delete", confirmation.data["reason"])
        self.assertEqual(["task"], confirmation.data["approval_scope"])

    async def test_service_stop_confirms_in_risk_mode_without_danger_flag(self):
        registry = ToolRegistry()
        called = []

        async def cloud_exec(command: str):
            called.append(command)
            return "stopped"

        registry.register(Tool(
            "cloud_exec", "云端运行", {"type": "object"}, cloud_exec, side_effect=True,
        ))

        class OneCallLLM:
            def __init__(self):
                self.turn = 0

            async def chat_stream_tools(self, messages, tools=None, **kwargs):
                self.turn += 1
                if self.turn > 1:
                    yield "done", {"role": "assistant", "content": "已取消"}
                    return
                yield "done", {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "stop-call", "type": "function",
                    "function": {"name": "cloud_exec", "arguments":
                                  '{"command":"docker compose down"}'},
                }]}

        confirm = AsyncMock(return_value=False)
        events = [event async for event in Agent(
            OneCallLLM(), registry, AgentConfig(confirm_before_action=True), confirm_fn=confirm,
        ).run("停止云端服务", approval_mode="risk")]
        confirm.assert_awaited_once()
        self.assertEqual([], called)
        self.assertTrue(any(
            event.type == "confirm_needed" and event.data["reason"] == "service_stop"
            for event in events
        ))


if __name__ == "__main__":
    unittest.main()
