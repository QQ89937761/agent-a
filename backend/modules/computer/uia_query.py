"""Serial, read-only UIA child with bounded lifetime and disposable responses."""
from __future__ import annotations
import asyncio
import json
import sys
import weakref
from pathlib import Path

from backend.core.process import hidden_subprocess_kwargs
from backend.modules.computer import system_control

_READERS = weakref.WeakSet()


class UIAReader:
    def __init__(self):
        self.process = None
        self.lock = asyncio.Lock()
        self.sequence = 0
        self.idle = None
        _READERS.add(self)

    async def _discard(self):
        if self.idle is not None:
            self.idle.cancel()
            self.idle = None
        process, self.process = self.process, None
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()

    async def close(self):
        async with self.lock:
            await self._discard()

    async def _expire(self, process, sequence):
        async with self.lock:
            if self.process is process and self.sequence == sequence:
                await self._discard()

    async def query(self, handle, *, query_timeout=3.0, **options):
        async with self.lock:
            if self.idle is not None:
                self.idle.cancel()
                self.idle = None
            try:
                async with asyncio.timeout(query_timeout):
                    system_control.check_foreground_permission()
                    if self.process is None or self.process.returncode is not None:
                        await self._discard()
                        command = [sys.executable]
                        if not getattr(sys, "frozen", False):
                            command.append(str(Path(__file__).resolve().parents[3] / "xiaoa_backend.py"))
                        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                            *command, "--uia-reader", stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                            limit=2 * 1024 * 1024, **hidden_subprocess_kwargs()))
                        try:
                            self.process = await asyncio.shield(spawning)
                        except asyncio.CancelledError:
                            self.process = await spawning
                            await self._discard()
                            raise
                    self.sequence += 1
                    request = {"id": self.sequence, "options": {"handle": handle, **options}}
                    self.process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
                    await self.process.stdin.drain()
                    read = asyncio.create_task(self.process.stdout.readline())
                    try:
                        while not read.done():
                            system_control.check_foreground_permission()
                            await asyncio.wait({read}, timeout=.05)
                        response = json.loads(read.result())
                    finally:
                        if not read.done():
                            read.cancel()
                        await asyncio.gather(read, return_exceptions=True)
                    if response.get("id") != self.sequence or not isinstance(response.get("result"), dict):
                        raise ValueError("UIA response mismatch")
                    return response["result"]
            except asyncio.CancelledError:
                await self._discard()
                raise
            except Exception as exc:
                await self._discard()
                return {"ok": False, "status": "uia_timeout" if isinstance(exc, TimeoutError) else "uia_unavailable",
                        "visual_fallback": True, "error": type(exc).__name__}
            finally:
                if self.process is not None:
                    process = self.process
                    self.idle = asyncio.get_running_loop().call_later(
                        30, lambda sequence=self.sequence: asyncio.create_task(self._expire(process, sequence)))


async def close_readers():
    await asyncio.gather(*(reader.close() for reader in list(_READERS)))


def serve():
    """Only bounded UIA snapshots; no code evaluation or input actions."""
    import uiautomation as auto
    from backend.modules.computer.computer_module import _uia_snapshot
    with auto.UIAutomationInitializerInThread():
        for line in sys.stdin:
            request = json.loads(line)
            options = request["options"]
            if (set(options) - {"handle", "max_depth", "limit", "include_offscreen", "value_check"}
                    or type(options.get("handle")) is not int or options["handle"] <= 0):
                raise ValueError("Invalid read-only query")
            result = _uia_snapshot(**options)
            print(json.dumps({"id": request["id"], "result": result}, ensure_ascii=True), flush=True)
