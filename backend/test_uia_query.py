"""Read-only process lifecycle tests independent of Windows UI providers."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend.modules.computer.uia_query import UIAReader


class UIAReaderTests(unittest.IsolatedAsyncioTestCase):
    def process(self, mode="ok"):
        process = SimpleNamespace(returncode=None, requests=[])
        async def response():
            if mode == "hang":
                await asyncio.Event().wait()
            request = process.requests[-1]
            return json.dumps({"id": request["id"] + (mode == "wrong"), "result": {"ok": True}}).encode() + b"\n"
        process.stdin = SimpleNamespace(write=lambda line: process.requests.append(json.loads(line)), drain=AsyncMock())
        process.stdout = SimpleNamespace(readline=response)
        process.kill = lambda: setattr(process, "returncode", -1)
        process.wait = AsyncMock(side_effect=lambda: process.returncode)
        return process

    async def test_reuse_sequence_close_and_recreate(self):
        first, second = self.process(), self.process()
        reader = UIAReader()
        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=[first, second])) as spawn:
            for _ in range(3):
                self.assertTrue((await reader.query(9))["ok"])
            self.assertEqual(1, spawn.await_count)
            self.assertEqual([1, 2, 3], [r["id"] for r in first.requests])
            await reader.close()
            self.assertEqual(-1, first.returncode)
            self.assertTrue((await reader.query(9))["ok"])
            self.assertEqual(2, spawn.await_count)
            await reader.close()

    async def test_wrong_id_and_timeout_discard_without_replaying_query(self):
        for mode in ("wrong", "hang"):
            with self.subTest(mode=mode):
                process = self.process(mode)
                reader = UIAReader()
                with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
                    result = await reader.query(9, query_timeout=.1)
                self.assertFalse(result["ok"])
                self.assertEqual(1, len(process.requests))
                self.assertEqual(-1, process.returncode)
                self.assertIsNone(reader.process)

    async def test_cancel_kills_child_and_serial_waiter_can_start_fresh(self):
        first, second = self.process("hang"), self.process()
        reader = UIAReader()
        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=[first, second])):
            task = asyncio.create_task(reader.query(9))
            while not first.requests:
                await asyncio.sleep(0)
            following = asyncio.create_task(reader.query(9))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue((await following)["ok"])
            self.assertEqual(-1, first.returncode)
            await reader.close()

    async def test_idle_callback_cannot_discard_a_newer_request(self):
        process = self.process()
        reader = UIAReader()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            await reader.query(9)
            old_sequence = reader.sequence
            await reader.query(9)
            await reader._expire(process, old_sequence)
            self.assertIs(process, reader.process)
            await reader._expire(process, reader.sequence)
            self.assertIsNone(reader.process)
