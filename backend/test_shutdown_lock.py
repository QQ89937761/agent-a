"""Shutdown cleanup and fail-closed channel-lock persistence."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend.api import server


class ShutdownAndLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_closes_current_and_all_retired_clients_once(self):
        current = MagicMock()
        retired = MagicMock()
        current.aclose = AsyncMock()
        retired.aclose = AsyncMock()
        old_llm = server.LLM
        old_retired = set(server._LLM_RETIRE)
        old_usage = dict(server._LLM_USAGE)
        try:
            server.LLM = current
            server._LLM_RETIRE.clear()
            server._LLM_RETIRE.update({current, retired})
            server._LLM_USAGE.update({current: 1, retired: 2})

            await server._close_llm_clients()

            current.aclose.assert_awaited_once()
            retired.aclose.assert_awaited_once()
            self.assertEqual(set(), server._LLM_RETIRE)
            self.assertEqual({}, server._LLM_USAGE)
        finally:
            server.LLM = old_llm
            server._LLM_RETIRE.clear()
            server._LLM_RETIRE.update(old_retired)
            server._LLM_USAGE.clear()
            server._LLM_USAGE.update(old_usage)

    async def test_local_unlock_stays_locked_when_persistence_fails(self):
        old_locked = server.CHANNEL_LOCKED["locked"]
        server.CHANNEL_LOCKED["locked"] = True
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        try:
            with patch.object(server, "_save_channel_lock", side_effect=OSError("disk full")):
                response = await server.api_channel_unlock(request)

            self.assertEqual(500, response.status_code)
            self.assertTrue(server.CHANNEL_LOCKED["locked"])
            self.assertIn("仍保持锁定", response.body.decode("utf-8"))
        finally:
            server.CHANNEL_LOCKED["locked"] = old_locked

    async def test_remote_lock_reports_persistence_failure_but_locks_memory(self):
        old_locked = server.CHANNEL_LOCKED["locked"]
        target = "lock-persistence-test"
        server.CHANNEL_LOCKED["locked"] = False
        send = AsyncMock()
        try:
            with (
                patch.object(server, "_save_channel_lock", side_effect=OSError("read only")),
                patch.object(server, "_channel_send", send),
            ):
                await server.handle_channel_command("feishu", target, "锁定")

            self.assertTrue(server.CHANNEL_LOCKED["locked"])
            self.assertIn("保存失败", send.await_args.args[2])
            self.assertNotIn("已锁定:", send.await_args.args[2])
        finally:
            server.CHANNEL_LOCKED["locked"] = old_locked
            server.CHANNEL_LAST_USED.pop(("feishu", target), None)


if __name__ == "__main__":
    unittest.main()
