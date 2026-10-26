"""定时任务调度器的回归测试(纯逻辑,不连网、不连真实 agent)。"""
from __future__ import annotations

import inspect
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from backend.core.agent import Agent
from backend.core.bridge import HubBridge, WorkerBridge
from backend.core.config import AgentConfig, Config
from backend.core.tools import Tool, ToolRegistry
from backend.modules.schedule import (
    MISSED_SKIP_SECONDS,
    Scheduler,
    next_time_epoch,
    next_weekly_epoch,
    once_epoch,
)

# 基准时刻:2026-08-24 00:00 Asia/Shanghai(周一)
BASE = datetime(2026, 8, 24, 0, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()


def _no_env_schedule():
    return patch.dict(os.environ, {"AGENT_SCHEDULE_TASKS": ""})


class TimezoneTests(unittest.TestCase):
    def test_once_epoch_respects_timezone(self):
        shanghai = once_epoch("2026-08-25T14:50:00", "Asia/Shanghai")
        utc = once_epoch("2026-08-25T14:50:00", "UTC")
        # 同为 14:50 的墙钟时间,上海(UTC+8)比 UTC 早 8 小时到达
        self.assertAlmostEqual(utc - shanghai, 8 * 3600, places=1)

    def test_next_time_epoch_respects_timezone(self):
        after = datetime(2026, 8, 25, 0, 0, 0, tzinfo=dt_timezone.utc).timestamp()
        expected = datetime(2026, 8, 25, 9, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        self.assertAlmostEqual(next_time_epoch("09:00", "Asia/Shanghai", after), expected, places=1)
        expected_utc = datetime(2026, 8, 25, 9, 0, 0, tzinfo=dt_timezone.utc).timestamp()
        self.assertAlmostEqual(next_time_epoch("09:00", "UTC", after), expected_utc, places=1)

    def test_next_weekly_epoch_returns_next_matching_weekday(self):
        base = datetime(2026, 1, 5, 8, 0, 0, tzinfo=dt_timezone.utc)  # 周一
        self.assertEqual(base.weekday(), 0)
        result = next_weekly_epoch("09:00", [6], "UTC", base.timestamp())  # 下一个周日
        result_dt = datetime.fromtimestamp(result, dt_timezone.utc)
        self.assertEqual(result_dt.weekday(), 6)
        self.assertEqual(result_dt.hour, 9)
        self.assertGreater(result_dt, base)


class SchedulerTests(unittest.TestCase):
    def _scheduler(self, tmp: str) -> Scheduler:
        return Scheduler(Path(tmp) / "scheduled_tasks.json", now_fn=lambda: BASE)

    def test_storage_roundtrip_survives_restart(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-sched-") as tmp:
            with _no_env_schedule():
                first = self._scheduler(tmp)
                first.add_task(name="开会提醒", text="14:50 开会", schedule="daily", time="14:50")
                # 模拟重启:新建实例,从盘加载
                second = self._scheduler(tmp)
            tasks = second.list_tasks()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["name"], "开会提醒")
            self.assertEqual(tasks[0]["text"], "14:50 开会")

    def test_three_tasks_trigger_independently(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-sched-") as tmp:
            with _no_env_schedule():
                scheduler = self._scheduler(tmp)
                scheduler.add_task(name="a", text="A", schedule="daily", time="09:00")
                scheduler.add_task(name="b", text="B", schedule="daily", time="10:00")
                scheduler.add_task(name="c", text="C", schedule="daily", time="11:00")

                due = scheduler.tick(now_epoch=BASE + 9.5 * 3600)
                self.assertEqual([t["name"] for t in due], ["a"])
                due = scheduler.tick(now_epoch=BASE + 10.5 * 3600)
                self.assertEqual([t["name"] for t in due], ["b"])
                due = scheduler.tick(now_epoch=BASE + 11.5 * 3600)
                self.assertEqual([t["name"] for t in due], ["c"])

    def test_one_shot_task_runs_once_then_completes(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-sched-") as tmp:
            with _no_env_schedule():
                scheduler = self._scheduler(tmp)
                scheduler.add_task(
                    name="once", text="一次性", schedule="once",
                    datetime="2026-08-24T09:00:00",
                )
                due = scheduler.tick(now_epoch=BASE + 9 * 3600)
                self.assertEqual([t["name"] for t in due], ["once"])
                task = next(t for t in scheduler.list_tasks() if t["name"] == "once")
                self.assertTrue(task["done"])
                self.assertFalse(task["enabled"])
                # 第二天不会再触发
                due = scheduler.tick(now_epoch=BASE + 33 * 3600)
                self.assertEqual([t["name"] for t in due if t["name"] == "once"], [])

    def test_recurring_task_advances_next_run(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-sched-") as tmp:
            with _no_env_schedule():
                scheduler = self._scheduler(tmp)
                scheduler.add_task(name="daily", text="D", schedule="daily", time="09:00")
                due = scheduler.tick(now_epoch=BASE + 9 * 3600)
                self.assertEqual(len(due), 1)
                task = next(t for t in scheduler.list_tasks() if t["name"] == "daily")
                # 下一天 09:00
                self.assertEqual(task["next_run"], BASE + 33 * 3600)

    def test_missed_task_is_skipped_not_catch_up(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-sched-") as tmp:
            with _no_env_schedule():
                scheduler = self._scheduler(tmp)
                scheduler.add_task(name="late", text="L", schedule="daily", time="09:00")
                # 迟了 3 小时(> 1 小时)→ 跳过,不触发
                due = scheduler.tick(now_epoch=BASE + 12 * 3600)
                self.assertEqual(due, [])
                task = next(t for t in scheduler.list_tasks() if t["name"] == "late")
                self.assertEqual(task["last_status"], "skipped")
                # next_run 推进到下一天 09:00
                self.assertEqual(task["next_run"], BASE + 33 * 3600)

    def test_location_filter_leaves_other_location_untouched(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-sched-") as tmp:
            with _no_env_schedule():
                local = Scheduler(Path(tmp) / "l.json", location="local", now_fn=lambda: BASE)
                local.add_task(name="cloud-task", text="C", schedule="daily", time="09:00", location="cloud")
                original = next(t for t in local.list_tasks() if t["name"] == "cloud-task")["next_run"]
                due = local.tick(now_epoch=BASE + 9.5 * 3600)
                self.assertEqual(due, [])  # local 调度器不触发 cloud 任务
                after = next(t for t in local.list_tasks() if t["name"] == "cloud-task")["next_run"]
                self.assertEqual(original, after)  # next_run 未被推进


class _ScriptedLLM:
    """按脚本吐工具调用;调用完后再吐一句"已完成"。"""

    def __init__(self, calls):
        self.calls = list(calls)  # [(tool_name, args_json), ...]
        self.turn = 0

    async def chat_stream_tools(self, messages, tools=None, **kwargs):
        del messages, tools, kwargs
        self.turn += 1
        if self.turn <= len(self.calls):
            name, args_json = self.calls[self.turn - 1]
            yield "done", {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call-{self.turn}", "type": "function",
                "function": {"name": name, "arguments": args_json},
            }]}
        else:
            yield "done", {"role": "assistant", "content": "已完成"}


class ScheduledApprovalModeTests(unittest.IsolatedAsyncioTestCase):
    """任务19:定时任务用 scheduled 档自动【拒绝】危险操作,而不是 safe 档的自动【通过】。"""

    def _danger_registry(self, called: list):
        registry = ToolRegistry()

        async def wipe_dir(path: str):
            called.append(path)
            return "wiped"

        registry.register(Tool(
            name="wipe_dir", description="危险:清空目录",
            parameters={"type": "object", "properties": {
                "path": {"type": "string"}}, "required": ["path"]},
            handler=wipe_dir, side_effect=True, danger=True,
        ))
        return registry

    def _action_registry(self, called: list):
        registry = ToolRegistry()

        async def perform_action(command: str):
            called.append(command)
            return "done"

        registry.register(Tool(
            name="perform_action", description="执行普通动作",
            parameters={"type": "object", "properties": {
                "command": {"type": "string"}}, "required": ["command"]},
            handler=perform_action, side_effect=True,
        ))
        return registry

    async def test_scheduled_mode_denies_danger_tool(self):
        called: list = []
        registry = self._danger_registry(called)
        confirmations: list = []

        async def deny_confirm(name, args):
            confirmations.append((name, args))
            return False

        events = [event async for event in Agent(
            _ScriptedLLM([("wipe_dir", '{"path":"/tmp"}')]),
            registry, AgentConfig(confirm_before_action=True),
            confirm_fn=deny_confirm,
        ).run("清空临时目录", approval_mode="scheduled")]

        # ① 工具没执行 ② confirm_fn 被调用一次 ③ 有 confirm_needed 事件
        self.assertEqual([], called)
        self.assertEqual([("wipe_dir", {"path": "/tmp"})], confirmations)
        self.assertTrue(any(event.type == "confirm_needed" for event in events))

    async def test_scheduled_mode_blocks_dangerous_args_but_allows_harmless(self):
        called: list = []
        registry = self._action_registry(called)
        confirmations: list = []

        async def deny_confirm(name, args):
            confirmations.append(args)
            return False

        events = [event async for event in Agent(
            _ScriptedLLM([
                ("perform_action", '{"command":"删除临时文件"}'),
                ("perform_action", '{"command":"format D:"}'),
                ("perform_action", '{"command":"shutdown /s"}'),
                ("perform_action", '{"command":"整理文件"}'),
            ]),
            registry, AgentConfig(confirm_before_action=True),
            confirm_fn=deny_confirm,
        ).run("执行批量动作", approval_mode="scheduled")]

        # 三个危险参数被拦,只有"整理文件"执行了
        self.assertEqual(["整理文件"], called)
        self.assertEqual(1, len(confirmations), "拒绝后同一对话不再重复请求授权")
        self.assertEqual(1, sum(event.type == "confirm_needed" for event in events))
        self.assertEqual(3, sum(event.type == "tool_result" and not event.data["ok"] for event in events))

    async def test_risk_mode_still_confirms_danger_tool(self):
        called: list = []
        registry = self._danger_registry(called)
        confirmations: list = []

        async def confirm_fn(name, args):
            confirmations.append((name, args))
            return True

        events = [event async for event in Agent(
            _ScriptedLLM([("wipe_dir", '{"path":"/tmp"}')]),
            registry, AgentConfig(confirm_before_action=True),
            confirm_fn=confirm_fn,
        ).run("清空临时目录", approval_mode="risk")]

        # 用户日常 risk 档仍然弹确认;确认后执行
        self.assertEqual([("wipe_dir", {"path": "/tmp"})], confirmations)
        self.assertEqual(["/tmp"], called)
        self.assertTrue(any(event.type == "confirm_needed" for event in events))


class ScheduledAgentConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_scheduled_agent_uses_scheduled_mode_and_returns_blocked(self):
        from backend.api import server

        observed: dict = {}

        class FakeAgent:
            def __init__(self, llm, registry, cfg, confirm_fn=None, **kwargs):
                del llm, registry, cfg, kwargs
                observed["confirm_fn"] = confirm_fn

            async def run(self, *args, approval_mode=None, **kwargs):
                del args, kwargs
                observed["approval_mode"] = approval_mode
                # 模拟 agent 想执行一个需确认的操作,confirm_fn 应返回 False
                allowed = await observed["confirm_fn"]("wipe_dir", {"path": "/tmp"})
                yield SimpleNamespace(
                    type="message", data={"content": "" if not allowed else "allowed"},
                )

        with patch.object(server, "Agent", FakeAgent), patch.object(server, "LLM", None):
            text, blocked = await server._run_scheduled_agent({"text": "删除目录"})
        self.assertEqual("scheduled", observed["approval_mode"])
        self.assertIsNotNone(blocked)
        self.assertIn("wipe_dir", blocked)

    async def test_channel_schedule_is_blocked_when_remote_control_is_off(self):
        from backend.api import server
        task = {"text": "screenshot", "channel": "feishu", "channel_target": "user-a"}
        with patch.object(server.CFG.feishu, "allowed_open_ids", ["user-a"]), \
             patch.object(server, "_channel_permission", return_value=(False, 1)), \
             patch.object(server, "Agent", side_effect=AssertionError("OFF schedule ran tools")):
            text, reason = await server._run_scheduled_agent(task)
        self.assertIsNotNone(reason)
        self.assertIn("关闭", text)

    async def test_channel_schedule_stops_after_permission_is_revoked(self):
        from backend.api import server
        permission = [True, 0]
        class FakeAgent:
            def __init__(self, *args, abort_fn=None, **kwargs):
                self.abort_fn = abort_fn
            async def run(self, *args, **kwargs):
                if self.abort_fn():
                    raise AssertionError("ON schedule blocked")
                permission[:] = [False, 1]
                if not self.abort_fn():
                    raise AssertionError("revoked schedule still enabled")
                yield SimpleNamespace(type="message", data={"content": "stopped"})
        task = {"text": "screenshot", "channel": "feishu", "channel_target": "user-a"}
        with patch.object(server.CFG.feishu, "allowed_open_ids", ["user-a"]), \
             patch.object(server, "_channel_permission", side_effect=lambda *args: tuple(permission)), \
             patch.object(server, "Agent", FakeAgent):
            text, reason = await server._run_scheduled_agent(task)
        self.assertIsNotNone(reason)
        self.assertIn("撤销", reason)

    async def test_fire_scheduled_task_records_blocked_status(self):
        from backend.api import server

        reason = "任务里有需要确认的操作（wipe_dir），已跳过未执行"

        async def fake_run_agent(task):
            del task
            return ("", reason)

        with patch.object(server, "_run_scheduled_agent", new=fake_run_agent), \
             patch.object(server, "_deliver_scheduled_notice", new=AsyncMock(return_value={})), \
             patch.object(server.SCHEDULER, "record_fire_result") as record:
            await server._fire_scheduled_task(
                {"id": "abc", "kind": "execute", "name": "清理", "text": "删目录"},
            )
        record.assert_called_once_with("abc", "blocked", reason)


class CloudSyncScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cloud_runtime_sync_includes_schedule_tasks(self):
        from backend.api import server

        local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        cfg = Config()
        with patch.object(server, "CFG", cfg), \
             patch("backend.core.cloud_deploy.sync_runtime_settings") as sync, \
             patch.object(server.SCHEDULER, "sync_payload", return_value='[{"id":"x"}]'):
            result = await server.api_cloud_runtime(local)
        self.assertTrue(result["ok"])
        values = sync.call_args.args[1]
        self.assertIn("AGENT_SCHEDULE_TASKS", values)
        self.assertEqual(values["AGENT_SCHEDULE_TASKS"], '[{"id":"x"}]')


class ScheduleCloudPushTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _request():
        return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))

    @staticmethod
    def _cloud_task():
        return {"id": "abc", "location": "cloud", "name": "提醒", "kind": "remind",
                "schedule": "daily", "time": "09:00", "text": "内容"}

    def _cloud_connected_tmp(self):
        tmp = tempfile.TemporaryDirectory(prefix="xiaoa-sched-cloud-")
        (Path(tmp.name) / "data" / "cloud-ssh").mkdir(parents=True)
        (Path(tmp.name) / "data" / "cloud-ssh" / "target.json").write_text(
            '{"host":"h","username":"u"}', encoding="utf-8",
        )
        return tmp

    async def test_create_cloud_task_pushes_schedule_to_cloud(self):
        from backend.api import server
        from backend.api.server import ScheduleTaskRequest

        req = ScheduleTaskRequest(name="提醒", location="cloud", text="内容")
        tmp = self._cloud_connected_tmp()
        try:
            with patch("backend.core.config.app_home", return_value=Path(tmp.name)), \
                 patch("backend.core.cloud_deploy.sync_runtime_settings") as sync, \
                 patch.object(server.SCHEDULER, "add_task", return_value=self._cloud_task()), \
                 patch.object(server.SCHEDULER, "list_tasks", return_value=[self._cloud_task()]), \
                 patch.object(server.SCHEDULER, "sync_payload", return_value='[{"id":"abc","location":"cloud"}]'):
                result = await server.api_schedule_add(req, self._request())
        finally:
            tmp.cleanup()
        self.assertTrue(result["ok"])
        self.assertNotIn("cloud_warning", result)
        sync.assert_called_once()
        values = sync.call_args.args[1]
        self.assertIn("AGENT_SCHEDULE_TASKS", values)
        self.assertIn("abc", values["AGENT_SCHEDULE_TASKS"])

    async def test_create_local_task_does_not_push(self):
        from backend.api import server
        from backend.api.server import ScheduleTaskRequest

        req = ScheduleTaskRequest(name="本地", location="local", text="内容")
        local_task = {"id": "loc", "location": "local", "name": "本地", "text": "内容"}
        with patch("backend.core.cloud_deploy.sync_runtime_settings") as sync, \
             patch.object(server.SCHEDULER, "add_task", return_value=local_task), \
             patch.object(server.SCHEDULER, "list_tasks", return_value=[local_task]):
            result = await server.api_schedule_add(req, self._request())
        self.assertTrue(result["ok"])
        self.assertNotIn("cloud_warning", result)
        sync.assert_not_called()

    async def test_create_cloud_task_without_cloud_connection_warns(self):
        from backend.api import server
        from backend.api.server import ScheduleTaskRequest

        req = ScheduleTaskRequest(name="提醒", location="cloud", text="内容")
        with tempfile.TemporaryDirectory(prefix="xiaoa-sched-nocloud-") as tmp, \
             patch("backend.core.config.app_home", return_value=Path(tmp)), \
             patch.object(server.SCHEDULER, "add_task", return_value=self._cloud_task()), \
             patch.object(server.SCHEDULER, "list_tasks", return_value=[self._cloud_task()]):
            result = await server.api_schedule_add(req, self._request())
        self.assertTrue(result["ok"])
        self.assertIn("尚未连接云端", result.get("cloud_warning", ""))

    async def test_push_failure_keeps_task_and_warns(self):
        from backend.api import server
        from backend.api.server import ScheduleTaskRequest

        req = ScheduleTaskRequest(name="提醒", location="cloud", text="内容")
        tmp = self._cloud_connected_tmp()
        try:
            with patch("backend.core.config.app_home", return_value=Path(tmp.name)), \
                 patch("backend.core.cloud_deploy.sync_runtime_settings",
                       side_effect=RuntimeError("ssh down")), \
                 patch.object(server.SCHEDULER, "add_task", return_value=self._cloud_task()), \
                 patch.object(server.SCHEDULER, "list_tasks", return_value=[self._cloud_task()]), \
                 patch.object(server.SCHEDULER, "sync_payload", return_value='[{"id":"abc"}]'):
                result = await server.api_schedule_add(req, self._request())
        finally:
            tmp.cleanup()
        self.assertTrue(result["ok"])
        self.assertIn("同步到云端失败", result.get("cloud_warning", ""))


class ScheduledNoticeDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_deliver_notice_survives_feishu_failure(self):
        from backend.api import server

        with patch.object(server, "broadcast", new=AsyncMock()) as broadcast_mock, \
             patch.object(server, "_send_feishu_reminder",
                          new=AsyncMock(side_effect=RuntimeError("feishu down"))):
            # 飞书异常被兜住,不应冒出;broadcast 仍然执行
            await server._deliver_scheduled_notice(
                {"id": "a", "name": "n", "kind": "remind", "location": "cloud"}, "内容",
            )
        broadcast_mock.assert_awaited_once()

    async def test_deliver_notice_survives_broadcast_failure(self):
        from backend.api import server

        with patch.object(server, "broadcast",
                          new=AsyncMock(side_effect=RuntimeError("no ws"))), \
             patch.object(server, "_send_feishu_reminder", new=AsyncMock()) as feishu_mock:
            await server._deliver_scheduled_notice(
                {"id": "a", "name": "n", "kind": "remind", "location": "cloud"}, "内容",
            )
        feishu_mock.assert_awaited_once()


class _HubWebSocket:
    """hub 侧 FastAPI WebSocket 假件(send_text)。"""

    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))


class _WorkerWebSocket:
    """worker 侧 websockets 假件(async for + send)。"""

    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)

    async def send(self, text):
        self.sent.append(json.loads(text))


class ScheduledNoticeToWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_order_uses_separate_message_for_capable_workers_only(self):
        order = [{"id": "default", "models": ["second", "first"]}]
        received = []
        bridge = HubBridge("good", get_model_order=lambda: order)
        ws = _HubWebSocket()
        bridge._ws = ws
        self.assertFalse(await bridge.sync_model_order())
        bridge._model_order_sync = True
        self.assertTrue(await bridge.sync_model_order())
        self.assertEqual({"type": "model_order", "providers": order}, ws.sent[0])
        on_order = AsyncMock(side_effect=lambda value: received.append(value))
        notice = AsyncMock()
        worker = WorkerBridge("wss://h", "tok", "pc", AsyncMock(),
                              on_notice=notice, on_model_order=on_order)
        incoming = _WorkerWebSocket([json.dumps(ws.sent[0])])
        await worker._serve(incoming)
        self.assertEqual([order], received)
        notice.assert_not_awaited()
        self.assertEqual([], incoming.sent)
        bridge._ws = None
        self.assertFalse(await bridge.sync_model_order())

    async def test_failed_order_save_does_not_break_worker_messages(self):
        notice = AsyncMock()
        worker = WorkerBridge("wss://h", "tok", "pc", AsyncMock(), on_notice=notice,
                              on_model_order=AsyncMock(side_effect=OSError("disk full")))
        incoming = _WorkerWebSocket([
            json.dumps({"type": "model_order", "providers": []}),
            json.dumps({"type": "notice", "payload": {"text": "still connected"}}),
        ])
        await worker._serve(incoming)
        notice.assert_awaited_once_with({"text": "still connected"})

    async def test_hub_notify_sends_notice_message(self):
        ws = _HubWebSocket()
        bridge = HubBridge("good")
        bridge._ws = ws
        self.assertTrue(await bridge.notify({"name": "n", "text": "t"}))
        self.assertEqual("notice", ws.sent[0]["type"])
        self.assertEqual({"name": "n", "text": "t"}, ws.sent[0]["payload"])

    async def test_hub_notify_offline_skips(self):
        self.assertFalse(await HubBridge("good").notify({"x": 1}))

    def test_hub_notify_shares_send_lock_with_dispatch(self):
        source = inspect.getsource(HubBridge.notify)
        self.assertIn("self._send_lock", source)

    async def test_worker_notice_calls_on_notice_without_tool_or_result(self):
        received: list = []
        tool_calls: list = []

        async def run_tool(tool, args):
            tool_calls.append((tool, args))
            return "x"

        async def on_notice(payload):
            received.append(payload)

        bridge = WorkerBridge("wss://h", "tok", "pc", run_tool, on_notice=on_notice)
        ws = _WorkerWebSocket([
            json.dumps({"type": "notice", "payload": {"name": "n", "text": "t"}}),
        ])
        await bridge._serve(ws)
        self.assertEqual([{"name": "n", "text": "t"}], received)
        self.assertEqual([], tool_calls)  # 没触发工具
        self.assertEqual([], ws.sent)      # 没回 result

    async def test_deliver_notice_pushes_to_worker_on_hub(self):
        from backend.api import server

        old_bridge = server.HUB_BRIDGE
        fake_bridge = MagicMock()
        fake_bridge.notify = AsyncMock(return_value=True)
        server.HUB_BRIDGE = fake_bridge
        try:
            with patch.object(server, "broadcast", new=AsyncMock()), \
                 patch.object(server, "_send_feishu_reminder", new=AsyncMock()):
                await server._deliver_scheduled_notice(
                    {"id": "a", "name": "n", "kind": "remind", "location": "cloud"}, "内容",
                )
        finally:
            server.HUB_BRIDGE = old_bridge
        fake_bridge.notify.assert_awaited_once()
        payload = fake_bridge.notify.call_args.args[0]
        self.assertEqual("a", payload["id"])
        self.assertEqual("内容", payload["text"])

    async def test_deliver_notice_worker_offline_keeps_feishu(self):
        from backend.api import server

        old_bridge = server.HUB_BRIDGE
        fake_bridge = MagicMock()
        fake_bridge.notify = AsyncMock(return_value=False)  # worker 离线
        server.HUB_BRIDGE = fake_bridge
        try:
            with patch.object(server, "broadcast", new=AsyncMock()), \
                 patch.object(server, "_send_feishu_reminder", new=AsyncMock()) as feishu:
                await server._deliver_scheduled_notice(
                    {"id": "a", "name": "n", "kind": "remind", "location": "cloud"}, "内容",
                )
        finally:
            server.HUB_BRIDGE = old_bridge
        feishu.assert_awaited_once()  # worker 离线不阻塞飞书


class ScheduledNoticeResultsTests(unittest.IsolatedAsyncioTestCase):
    async def test_deliver_notice_records_feishu_unconfigured(self):
        from backend.api import server

        cfg = Config()
        cfg.feishu.app_id = ""
        cfg.feishu.app_secret = ""
        with patch.object(server, "CFG", cfg), \
             patch.object(server, "broadcast", new=AsyncMock()), \
             patch.object(server, "HUB_BRIDGE", None):
            results = await server._deliver_scheduled_notice(
                {"id": "a", "name": "n", "kind": "remind", "location": "cloud"}, "内容",
            )
        self.assertEqual("未配置跳过", results["feishu"])

    async def test_feishu_reminder_sends_to_open_id(self):
        from backend.api import server

        cfg = Config()
        cfg.feishu.app_id = "cli_test"
        cfg.feishu.app_secret = "secret"
        cfg.feishu.allowed_open_ids = ["ou_abc"]
        with patch.object(server, "CFG", cfg), \
             patch("backend.modules.chat.feishu_module.send_message", new=AsyncMock()) as send:
            result = await server._send_feishu_reminder("提醒内容")
        self.assertEqual("发送成功", result)
        send.assert_awaited_once_with("ou_abc", "提醒内容", receive_id_type="open_id")


class ScheduledLateNoticeTests(unittest.TestCase):
    def test_with_late_notice_adds_delay_text(self):
        from backend.api import server

        now = datetime.now().timestamp()
        text = server._with_late_notice("开会提醒", {"_due_time": now - 1200})
        self.assertIn("原定", text)
        self.assertIn("延迟 20 分钟", text)

    def test_with_late_notice_omitted_when_on_time(self):
        from backend.api import server

        now = datetime.now().timestamp()
        self.assertEqual(
            "开会提醒", server._with_late_notice("开会提醒", {"_due_time": now - 30}),
        )


class ScheduleLocationRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_task_uses_default_feishu_channel(self):
        from backend.api import server

        with patch.object(server, "broadcast", new=AsyncMock()), \
             patch.object(server, "_send_feishu_reminder", new=AsyncMock(return_value="ok")) as feishu, \
             patch.object(server, "HUB_BRIDGE", None):
            results = await server._deliver_scheduled_notice(
                {"id": "a", "name": "n", "kind": "remind", "location": "local"}, "content",
            )
        self.assertEqual("ok", results["feishu"])
        feishu.assert_awaited_once()

    async def test_local_task_uses_selected_weixin_channel(self):
        from backend.api import server

        with patch.object(server, "broadcast", new=AsyncMock()), \
             patch.object(server, "_send_weixin_reminder", new=AsyncMock(return_value="ok")) as weixin, \
             patch.object(server, "_send_feishu_reminder", new=AsyncMock()) as feishu, \
             patch.object(server, "HUB_BRIDGE", None):
            results = await server._deliver_scheduled_notice(
                {"id": "a", "name": "n", "kind": "remind", "location": "local", "channel": "weixin"}, "content",
            )
        self.assertEqual("ok", results["weixin"])
        weixin.assert_awaited_once()
        feishu.assert_not_awaited()

    async def test_cloud_task_uses_selected_weixin_channel(self):
        from backend.api import server

        with patch.object(server, "broadcast", new=AsyncMock()), \
             patch.object(server, "_send_weixin_reminder", new=AsyncMock(return_value="ok")) as weixin, \
             patch.object(server, "_send_feishu_reminder", new=AsyncMock()) as feishu, \
             patch.object(server, "HUB_BRIDGE", None):
            results = await server._deliver_scheduled_notice(
                {"id": "a", "name": "n", "kind": "remind", "location": "cloud", "channel": "weixin"}, "content",
            )
        self.assertEqual("ok", results["weixin"])
        weixin.assert_awaited_once()
        feishu.assert_not_awaited()

    async def test_cloud_task_sends_feishu(self):
        from backend.api import server

        cfg = Config()
        cfg.feishu.app_id = "cli_x"
        cfg.feishu.app_secret = "secret"
        cfg.feishu.allowed_open_ids = ["ou_abc"]
        with patch.object(server, "CFG", cfg), \
             patch.object(server, "broadcast", new=AsyncMock()), \
             patch.object(server, "HUB_BRIDGE", None), \
             patch("backend.modules.chat.feishu_module.send_message", new=AsyncMock()) as send:
            results = await server._deliver_scheduled_notice(
                {"id": "a", "name": "n", "kind": "remind", "location": "cloud"}, "内容",
            )
        self.assertEqual("发送成功", results["feishu"])
        send.assert_awaited_once()


class FeishuCfgFallbackTests(unittest.TestCase):
    def test_get_client_falls_back_to_global_cfg(self):
        from backend.modules.chat import feishu_module

        old_client = feishu_module._client
        feishu_module._client = None
        try:
            cfg = Config()
            cfg.feishu.app_id = "cli_test"
            cfg.feishu.app_secret = "secret"
            fake_lark = MagicMock()
            builder = fake_lark.Client.builder.return_value
            builder.app_id.return_value = builder
            builder.app_secret.return_value = builder
            builder.build.return_value = "CLIENT"
            with patch("backend.core.config.load_config", return_value=cfg), \
                 patch("backend.core.config.apply_env_overrides", return_value=cfg), \
                 patch.dict("sys.modules", {"lark_oapi": fake_lark}):
                client = feishu_module._get_client(None)
            self.assertEqual("CLIENT", client)
            builder.app_id.assert_called_once_with("cli_test")
            builder.app_secret.assert_called_once_with("secret")
        finally:
            feishu_module._client = old_client


class ScheduleLoadMergeTests(unittest.TestCase):
    def _task(self, **overrides):
        task = {
            "id": "t1", "name": "测试", "kind": "remind", "schedule": "once",
            "time": "09:00", "days": [0], "datetime": "2026-08-24T14:00:00",
            "timezone": "Asia/Shanghai", "location": "cloud", "text": "内容",
            "enabled": True, "next_run": None, "last_run": None,
            "last_status": None, "last_message": "", "created_at": BASE, "done": False,
        }
        task.update(overrides)
        return task

    def test_done_from_file_survives_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scheduled_tasks.json"
            env = json.dumps([self._task(done=False)])
            path.write_text(
                json.dumps([self._task(done=True, enabled=False, last_run=BASE + 14 * 3600)]),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"AGENT_SCHEDULE_TASKS": env}):
                scheduler = Scheduler(str(path), location="cloud", now_fn=lambda: BASE + 14.5 * 3600)
            tasks = scheduler.list_tasks()
            self.assertEqual(1, len(tasks))
            self.assertTrue(tasks[0]["done"])
            due = scheduler.tick(now_epoch=BASE + 14.5 * 3600)
            self.assertEqual([], due)

    def test_three_restarts_fire_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scheduled_tasks.json"
            env = json.dumps([self._task(done=False)])
            fired = 0
            with patch.dict(os.environ, {"AGENT_SCHEDULE_TASKS": env}):
                for _ in range(3):
                    scheduler = Scheduler(str(path), location="cloud", now_fn=lambda: BASE + 14.5 * 3600)
                    due = scheduler.tick(now_epoch=BASE + 14.5 * 3600)
                    fired += len(due)
                    if due:
                        scheduler.save()  # 触发后写盘 done=true
            self.assertEqual(1, fired)

    def test_task_deleted_locally_disappears_after_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scheduled_tasks.json"
            env = json.dumps([self._task(id="t1")])
            path.write_text(
                json.dumps([self._task(id="t1"), self._task(id="t2", name="已删除")]),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"AGENT_SCHEDULE_TASKS": env}):
                scheduler = Scheduler(str(path), location="cloud", now_fn=lambda: BASE)
            self.assertEqual(["t1"], [t["id"] for t in scheduler.list_tasks()])

    def test_cloud_web_edits_survive_restart_until_next_desktop_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scheduled_tasks.json"
            env = json.dumps([self._task()])
            with patch.dict(os.environ, {"AGENT_SCHEDULE_TASKS": env}):
                scheduler = Scheduler(path, location="cloud", now_fn=lambda: BASE)
                scheduler.update_task("t1", name="网页修改")
                added = scheduler.add_task(name="网页新增", text="测试提醒", location="cloud")
                scheduler = Scheduler(path, location="cloud", now_fn=lambda: BASE)
                self.assertEqual(["网页修改", "网页新增"], [t["name"] for t in scheduler.list_tasks()])
                scheduler.remove_task("t1")
                scheduler = Scheduler(path, location="cloud", now_fn=lambda: BASE)
                self.assertEqual([added["id"]], [t["id"] for t in scheduler.list_tasks()])
            with patch.dict(os.environ, {"AGENT_SCHEDULE_TASKS": json.dumps([self._task(name="新的桌面同步")])}):
                scheduler = Scheduler(path, location="cloud", now_fn=lambda: BASE)
                self.assertEqual(["新的桌面同步"], [t["name"] for t in scheduler.list_tasks()])

    def test_time_change_recomputes_next_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scheduled_tasks.json"
            env_task = self._task(datetime="2026-08-24T15:00:00", done=False)
            file_task = self._task(datetime="2026-08-24T14:00:00", done=True, enabled=False)
            env = json.dumps([env_task])
            path.write_text(json.dumps([file_task]), encoding="utf-8")
            with patch.dict(os.environ, {"AGENT_SCHEDULE_TASKS": env}):
                scheduler = Scheduler(str(path), location="cloud", now_fn=lambda: BASE + 13 * 3600)
            tasks = scheduler.list_tasks()
            self.assertFalse(tasks[0]["done"])
            self.assertEqual(BASE + 15 * 3600, tasks[0]["next_run"])


class ScheduleRecipientLabelsTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_and_cloud_labels_match_channel_account_order(self):
        from backend.api import server
        from backend.modules.chat import weixin_clawbot

        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        cfg = Config()
        cfg.feishu.allowed_open_ids = ["ou_first", "ou_second"]
        cfg.feishu.remarks = {"ou_first": "本机备注"}
        weixin = {"accounts": [
            {"account_id": "empty", "connected": False, "allowed_user_ids": []},
            {"account_id": "bot-a", "connected": True, "remark": "XiaoA", "allowed_user_ids": ["wx-a"]},
            {"account_id": "bot-b", "connected": False, "remark": "张三", "allowed_user_ids": ["wx-b", "wx-c"]},
        ]}
        cloud_feishu = {"allowed_open_ids": ["ou_cloud_a", "ou_cloud_b"], "remarks": {"ou_cloud_a": "云端备注"}}
        for location in ("local", "cloud"):
            with self.subTest(location=location), patch.object(server, "CFG", cfg), \
                 patch.object(server, "_automatic_schedule_location", return_value=location), \
                 patch.object(weixin_clawbot, "status", return_value=weixin), \
                 patch("backend.core.cloud_deploy.get_weixin_status", return_value=weixin), \
                 patch("backend.core.cloud_deploy.get_feishu_status", return_value=cloud_feishu):
                wx = await server.api_schedule_recipients(request, "weixin")
                self.assertTrue(wx["ok"])
                self.assertEqual([("2", "XiaoA", "bot-a", "wx-a", True), ("3.1", "张三", "bot-b", "wx-b", False), ("3.2", "张三", "bot-b", "wx-c", False)],
                                 [(r["number"], r["remark"], r["account_id"], r["target"], r["connected"]) for r in wx["recipients"]])
                fs = await server.api_schedule_recipients(request, "feishu")
                self.assertTrue(fs["ok"])
                expected_ids = cloud_feishu["allowed_open_ids"] if location == "cloud" else cfg.feishu.allowed_open_ids
                self.assertEqual(expected_ids, [r["target"] for r in fs["recipients"]])
                self.assertEqual(["1", "2"], [r["number"] for r in fs["recipients"]])
                self.assertEqual(["云端备注" if location == "cloud" else "本机备注", ""], [r["remark"] for r in fs["recipients"]])


if __name__ == "__main__":
    unittest.main()
