"""Exercise the real hub/worker protocol and desktop runtime using in-memory sockets."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from backend.core.bridge import HubBridge, WorkerBridge
from backend.core.tools import ToolRegistry
from backend.core.agent import Agent
from backend.core.config import AgentConfig
from backend.modules.computer import desktop_runtime as runtime


class Wire:
    def __init__(self):
        self.worker_in, self.hub_in = asyncio.Queue(), asyncio.Queue()
        self.messages = []
        self.closed = False

    async def send(self, raw):
        message = json.loads(raw)
        self.messages.append(("worker", message))
        if self.closed:
            raise RuntimeError("disconnected")
        await self.hub_in.put(raw)

    async def send_text(self, raw):
        self.messages.append(("hub", json.loads(raw)))
        if self.closed:
            raise RuntimeError("disconnected")
        await self.worker_in.put(raw)

    async def receive_text(self):
        value = await self.hub_in.get()
        if value is None:
            raise RuntimeError("disconnected")
        return value

    async def recv(self):
        value = await self.worker_in.get()
        if value is None:
            raise RuntimeError("disconnected")
        return value

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.worker_in.get()
        if value is None:
            raise StopAsyncIteration
        return value

    async def close(self, **kwargs):
        if not self.closed:
            self.closed = True
            await self.worker_in.put(None)
            await self.hub_in.put(None)


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.005)


class RemoteDesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.info = {"handle": 12, "title": "Test", "process_id": 5, "class_name": "Window",
                     "rect": {"x": 0, "y": 0, "width": 200, "height": 100}, "minimized": False}
        self.registry = ToolRegistry()
        async def inspect(handle, **kwargs):
            return {"ok": True, "controls": [{"name": "Draft", "automation_id": "draft",
                "control_type": "EditControl", "focused": True}], "truncated": False}
        runtime.register_tools(self.registry, resolve=lambda *_: object(), inventory=lambda: [self.info],
            identity=lambda _: copy.deepcopy(self.info), foreground=lambda: 12,
            inspect=inspect, capture=AsyncMock(return_value={}))
        async def action(**kwargs):
            self.calls.append(kwargs)
            self.started.set()
            await self.release.wait()
            return {"ok": True}
        action.__module__ = "backend.modules.computer.computer_module"
        self.registry.tool("type_text", "draft", {}, side_effect=True)(action)
        self.plan = {"observation_id": (await self.registry.call("desktop_observe", {
            "handle": 12, "include_image": False}))["observation_id"], "actions": [{
                "tool": "type_text", "arguments": {"text": "draft"},
                "expect": {"automation_id": "draft", "control_type": "EditControl"},
            }] * 3}

    @asynccontextmanager
    async def connected(self, runner=None, *, capable=True):
        wire = Wire()
        bridge = HubBridge("test-token")
        worker = WorkerBridge("wss://example.invalid", "test-token", "pc", runner or self.registry.call)
        worker._desktop_stop = capable
        await wire.hub_in.put(json.dumps({"type": "hello", "token": "test-token",
            "worker_id": "pc", "desktop_stop": 1 if capable else 0}))
        hub_task = asyncio.create_task(bridge.handle_worker(wire))
        await until(lambda: bridge.worker_online)
        welcome = json.loads(await wire.recv())
        self.assertEqual(capable, welcome.get("desktop_stop") == 1)
        worker._epoch = welcome.get("desktop_epoch", "")
        worker_task = asyncio.create_task(worker._serve(wire))
        try:
            yield bridge, worker, wire
        finally:
            self.release.set()
            await wire.close()
            await asyncio.gather(hub_task, worker_task, return_exceptions=True)

    async def test_interjection_stops_after_current_action_and_preserves_completed(self):
        stop = asyncio.Event()
        async with self.connected() as (hub, worker, wire):
            token = runtime.interrupt_check.set(stop.is_set)
            task = asyncio.create_task(hub.dispatch("desktop_execute", self.plan))
            runtime.interrupt_check.reset(token)
            await asyncio.wait_for(self.started.wait(), 2)
            stop.set()
            await until(lambda: any(m.get("type") == "stopping" for _, m in wire.messages))
            self.assertFalse(task.done())  # acknowledgement is not completion
            self.release.set()
            result = await asyncio.wait_for(task, 3)
            self.assertEqual("interrupted", result["result"]["status"])
            self.assertEqual(1, result["result"]["completed"])
            self.assertEqual(1, len(self.calls))
            self.assertFalse(hub._unsettled)
            self.assertFalse(hub._pending)

    async def test_timeout_quarantines_new_work_until_late_terminal_result(self):
        async with self.connected() as (hub, worker, wire):
            task = asyncio.create_task(hub.dispatch("desktop_execute", self.plan, timeout=.5))
            await asyncio.wait_for(self.started.wait(), 2)
            result = await asyncio.wait_for(task, 2)
            self.assertEqual("remote_stop_unconfirmed", result["result"]["status"])
            self.assertEqual(1, len(self.calls))
            denied = await hub.dispatch("desktop_observe", {})
            self.assertEqual("remote_stop_unconfirmed", denied["result"]["status"])
            self.release.set()
            await until(lambda: not hub._pending)
            self.assertFalse(hub._unsettled)
            self.assertEqual(1, len(self.calls))
            self.assertTrue((await hub.dispatch("desktop_observe", {"handle": 12, "include_image": False}))["ok"])

    async def test_task_cancellation_sends_stop_and_does_not_detach_pending(self):
        async with self.connected() as (hub, worker, wire):
            task = asyncio.create_task(hub.dispatch("desktop_execute", self.plan))
            await asyncio.wait_for(self.started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(hub._pending)
            await until(lambda: any(m.get("type") == "stopping" for _, m in wire.messages))
            self.release.set()
            await until(lambda: not hub._pending)
            self.assertEqual(1, len(self.calls))

    async def test_disconnect_drains_before_reconnect_and_duplicate_is_not_replayed(self):
        async with self.connected() as (hub, worker, wire):
            task = asyncio.create_task(hub.dispatch("desktop_execute", self.plan))
            await asyncio.wait_for(self.started.wait(), 2)
            packet = next(m for direction, m in wire.messages if direction == "hub" and m.get("type") == "task")
            await wire.send_text(json.dumps(packet))
            await until(lambda: any(m.get("type") == "duplicate" for _, m in wire.messages))
            await wire.close()
            result = await asyncio.wait_for(task, 3)
            self.assertTrue(result["offline"])
            self.assertEqual(1, len(self.calls))
            self.assertTrue(hub._unsettled)
            self.release.set()
        self.assertIn(packet["id"], worker._drained)
        self.assertEqual(1, len(self.calls))
        # A replacement connection cannot erase uncertainty without exact drained IDs.
        for drained, blocked in (([], True), (worker._drained, False)):
            new = Wire()
            await new.hub_in.put(json.dumps({"type": "hello", "token": "test-token",
                "desktop_stop": 1, "desktop_drained": drained}))
            connection = asyncio.create_task(hub.handle_worker(new))
            await until(lambda: hub.worker_online)
            self.assertEqual(blocked, bool(hub._unsettled))
            self.assertNotEqual(packet["id"].split("-t")[0], hub._epoch)
            await new.close()
            await connection

    async def test_duplicate_completed_task_does_not_repeat_side_effect(self):
        self.release.set()
        async with self.connected() as (hub, worker, wire):
            self.assertTrue((await hub.dispatch("desktop_execute", self.plan))["result"]["ok"])
            packet = next(m for direction, m in wire.messages if direction == "hub" and m.get("type") == "task")
            await wire.send_text(json.dumps(packet))
            await until(lambda: any(m.get("type") == "duplicate" for _, m in wire.messages))
            self.assertEqual(3, len(self.calls))

    async def test_old_worker_is_single_step_on_both_sides(self):
        from backend.api import server
        async with self.connected(capable=False) as (hub, worker, wire):
            registry = ToolRegistry()
            with patch.object(server, "REGISTRY", registry), patch.object(server, "HUB_BRIDGE", hub), \
                    patch.object(server, "_WORKER_PROXY_TOOLS", set()):
                server._sync_worker_tools([{"schema": {"type": "function", "function": {
                    "name": "desktop_execute", "description": "plan", "parameters": {"type": "object"}}}}], persist=False)
                result = await registry.call("desktop_execute", self.plan)
                self.assertEqual("remote_single_step_required", result["status"])
            result = await hub.dispatch("desktop_execute", self.plan)
            self.assertEqual("remote_single_step_required", result["result"]["status"])
            self.assertFalse(self.calls)

    async def test_agent_channel_interjection_reaches_remote_plan_through_proxy(self):
        from backend.api import server
        pending, seen = [], []
        plan = self.plan
        class LLM:
            turn = 0
            async def chat_stream_tools(inner, messages, **kwargs):
                inner.turn += 1
                if inner.turn == 1:
                    yield "done", {"role": "assistant", "content": "", "tool_calls": [{
                        "id": "remote", "type": "function", "function": {
                            "name": "desktop_execute", "arguments": json.dumps(plan)}}]}
                else:
                    seen.extend(messages)
                    yield "done", {"role": "assistant", "content": "已停止后续步骤"}
        async with self.connected() as (hub, worker, wire):
            registry = ToolRegistry()
            with patch.object(server, "REGISTRY", registry), patch.object(server, "HUB_BRIDGE", hub), \
                    patch.object(server, "_WORKER_PROXY_TOOLS", set()), tempfile.TemporaryDirectory() as directory:
                server._sync_worker_tools([{"schema": {"type": "function", "function": {
                    "name": "desktop_execute", "description": "plan", "parameters": {"type": "object"}}}}], persist=False)
                def take():
                    items = pending[:]
                    pending.clear()
                    return items
                agent = Agent(LLM(), registry, AgentConfig(), interjection_fn=take,
                              result_dir=Path(directory))
                async def collect():
                    return [event async for event in agent.run("编辑草稿")]
                task = asyncio.create_task(collect())
                await asyncio.wait_for(self.started.wait(), 2)
                pending.append("改一下，先别继续输入")
                await until(lambda: any(m.get("type") == "stopping" for _, m in wire.messages))
                self.release.set()
                events = await asyncio.wait_for(task, 3)
                self.assertEqual(1, len(self.calls))
                self.assertTrue(any(m.get("role") == "user" and "先别继续" in str(m.get("content")) for m in seen))
                self.assertFalse(any(e.type == "error" for e in events))

    async def test_old_epoch_and_pre_stopped_packets_have_no_side_effects(self):
        self.release.set()
        async with self.connected() as (hub, worker, wire):
            await wire.send_text(json.dumps({"type": "task", "id": "old-t1",
                "tool": "desktop_execute", "args": self.plan}))
            await wire.send_text(json.dumps({"type": "ping"}))
            await until(lambda: any(m.get("type") == "pong" for _, m in wire.messages))
            self.assertFalse(self.calls)
            event = asyncio.Event()
            event.set()
            await worker._do_task(wire, {"id": "stopped", "tool": "desktop_execute", "args": self.plan}, event)
            self.assertFalse(self.calls)
            token = runtime.interrupt_check.set(lambda: True)
            try:
                result = await hub.dispatch("desktop_execute", self.plan)
            finally:
                runtime.interrupt_check.reset(token)
            self.assertEqual(0, result["result"]["completed"])
            self.assertFalse(hub._pending)

    async def test_terminal_reply_waits_for_shielded_input_to_finish(self):
        from backend.api import server
        from backend.modules.computer import system_control
        stopped = asyncio.Event()
        token = runtime.request_stop.set(stopped.set)
        started, finish = asyncio.Event(), asyncio.Event()
        async def physical_input():
            started.set()
            await finish.wait()
        async def run(tool, args):
            session = system_control._CONTROL_SESSION.get()
            session.control = type("Lease", (), {"worker": asyncio.create_task(physical_input())})()
            await asyncio.shield(session.control.worker)
        try:
            with patch.object(server, "REGISTRY") as registry:
                registry.call = run
                task = asyncio.create_task(server._run_worker_tool("desktop_execute", {}))
                await asyncio.wait_for(started.wait(), 1)
                task.cancel()
                await asyncio.sleep(.02)
                self.assertFalse(task.done())
                finish.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        finally:
            finish.set()
            runtime.request_stop.reset(token)

    async def test_physical_escape_uses_cooperative_stop_for_desktop(self):
        from backend.api import server
        from backend.modules.computer import system_control
        stopped = asyncio.Event()
        async def run(tool, args):
            self.assertEqual("desktop_execute", tool)
            system_control._CONTROL_SESSION.get().on_escape()
            await until(stopped.is_set)
            return {"ok": False, "status": "interrupted", "completed": 1}
        token = runtime.request_stop.set(stopped.set)
        try:
            with patch.object(server, "REGISTRY") as registry:
                registry.call = run
                result = await server._run_worker_tool("desktop_execute", {})
            self.assertEqual(1, result["completed"])
        finally:
            runtime.request_stop.reset(token)


if __name__ == "__main__":
    unittest.main()
