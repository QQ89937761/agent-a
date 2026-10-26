"""memory_* 工具注册与 worker 角色可用性回归。"""
from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.core.config import MemoryConfig
from backend.core.tools import ToolRegistry
from backend.modules.memory.memory_module import MemoryModule
from backend.modules.memory.tool_module import register_memory_tools

SIX_MEMORY_TOOLS = (
    "memory_add", "memory_list", "memory_search",
    "memory_update", "memory_forget", "memory_pin",
)


class MemoryToolRegistrationTests(unittest.TestCase):
    def test_register_memory_tools_registers_all_six(self):
        reg = ToolRegistry()
        register_memory_tools(reg, lambda: None)
        for name in SIX_MEMORY_TOOLS:
            self.assertIn(name, reg.names())

    def test_server_registers_memory_tools_for_all_roles(self):
        from backend.api import server

        source = Path(server.__file__).read_text(encoding="utf-8")
        # else 分支(standalone/worker)不再用 is_standalone 门控 memory 工具
        self.assertNotIn(
            "if CFG.role.is_standalone:\n            memory_tool_module.register_memory_tools",
            source,
        )
        # hub 与 else 两个分支都注册 memory 工具
        self.assertGreaterEqual(source.count("memory_tool_module.register_memory_tools(REGISTRY"), 2)


class WorkerMemoryToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_add_persists_through_registry(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-mem-tools-") as tmp:
            memory = MemoryModule(MemoryConfig(embed_model=""), Path(tmp), None)
            reg = ToolRegistry()
            register_memory_tools(reg, lambda: memory)

            result = await reg.call("memory_add", {"text": "用户喜欢喝美式咖啡"})
            self.assertIn("已记录", result)

            rows = memory.list_memories("personal", limit=10)
            self.assertTrue(any("美式咖啡" in str(row) for row in rows))


class MemoryImportSnapshotTests(unittest.TestCase):
    def test_import_merges_and_dedupes(self):
        """worker 上传的记忆按 content_hash 去重合并;云端已有的不被删。"""
        from backend.modules.memory.vector_memory import VectorMemory

        with tempfile.TemporaryDirectory(prefix="xiaoa-mem-import-") as tmp:
            root = Path(tmp)
            source = VectorMemory(None, None, root / "src")
            source.add("甲方的联系人叫张三")
            source.add("常用路径 D:\\办公\\火箭基地")
            snapshot = source.export_snapshot()

            target = VectorMemory(None, None, root / "dst")
            target.add("甲方是寰宇航天")
            first = target.import_snapshot(snapshot)
            self.assertEqual({"added": 2, "skipped": 0}, first)
            second = target.import_snapshot(snapshot)
            self.assertEqual({"added": 0, "skipped": 2}, second)
            texts = [row["text"] for row in target.list_memories(namespace="", limit=50)]
            self.assertIn("甲方是寰宇航天", texts, "云端原有的记忆不能被删")
            self.assertIn("甲方的联系人叫张三", texts, "worker 的记忆要补进来")
            self.assertEqual(3, len(texts))


class WorkerCloudMemoryMergeTests(unittest.IsolatedAsyncioTestCase):
    async def test_cloud_snapshot_merges_into_live_worker_memory(self):
        from backend.api import server
        from backend.modules.memory.vector_memory import VectorMemory

        with tempfile.TemporaryDirectory(prefix="xiaoa-mem-pull-") as tmp:
            root = Path(tmp)
            cloud = VectorMemory(None, None, root / "cloud")
            cloud.add("微信里提到的长期偏好")
            local = VectorMemory(None, None, root / "local")
            local.add("本机已有的长期记忆")

            with patch.object(server, "MEMORY", local):
                result = await server._merge_memory_snapshot_into_runtime({
                    "memory.sqlite3": cloud.export_snapshot(),
                })

            self.assertEqual({"added": 1, "skipped": 0}, result)
            texts = [row["text"] for row in local.list_memories("", limit=20)]
            self.assertIn("微信里提到的长期偏好", texts)
            self.assertIn("本机已有的长期记忆", texts)

class MemoryModuleImportForwardTests(unittest.TestCase):
    def test_memory_module_forwards_import_snapshot(self):
        """MemoryModule 是包装类,必须把 import_snapshot 转发给真正的后端(否则接口 500)。"""
        from backend.modules.memory.vector_memory import VectorMemory

        with tempfile.TemporaryDirectory(prefix="xiaoa-mem-fwd-") as tmp:
            root = Path(tmp)
            source = VectorMemory(None, None, root / "src")
            source.add("要同步过去的事实")
            snapshot = source.export_snapshot()

            target = MemoryModule(MemoryConfig(embed_model=""), root / "dst", None)
            result = target.import_snapshot(snapshot)
            self.assertEqual(1, result["added"])
            rows = target.list_memories("", limit=10)
            self.assertTrue(any("要同步过去的事实" in str(row) for row in rows))


class MemoryImportApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_endpoint_merges_snapshot(self):
        from backend.api import server
        from backend.modules.memory.vector_memory import VectorMemory

        with tempfile.TemporaryDirectory(prefix="xiaoa-mem-api-") as tmp:
            root = Path(tmp)
            source = VectorMemory(None, None, root / "src")
            source.add("云端没有的事实")
            encoded = base64.b64encode(source.export_snapshot()).decode()
            target = VectorMemory(None, None, root / "dst")
            from backend.modules.memory.sync_audit import MemorySyncAudit
            audit = MemorySyncAudit(root / "cloud-sync.sqlite3")
            request = server.MemoryImportRequest(
                files={"memory.sqlite3": encoded}, request_id="request-1", device_id="device-1",
            )
            with patch.object(server, "MEMORY", target), patch.object(server, "_memory_sync_audit", return_value=audit):
                result = await server.api_memory_import(request)
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["audit_recorded"])
            self.assertEqual(1, result["added"])
            self.assertEqual(0, result["skipped"])
            self.assertEqual(1, audit.summary()["today_successes"])
            self.assertTrue(audit.summary()["last_success"])

    async def test_import_endpoint_rejects_missing_snapshot(self):
        from backend.api import server

        result = await server.api_memory_import(server.MemoryImportRequest(files={}))
        self.assertFalse(result["ok"])


class MemoryLoopWakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_interval_change_wakes_loop(self):
        """改了「备份间隔(分钟)」要唤醒同步 loop 立刻用新间隔,不用等旧的 sleep 走完。"""
        from backend.api import server

        event = asyncio.Event()
        fake_memory_cfg = SimpleNamespace(
            backup_dir="", backup_interval_minutes=60, backup_keep_count=30,
        )
        with patch.object(server, "_MEMORY_LOOP_WAKE", event), \
             patch.object(server, "_MEMORY_LOOP", asyncio.get_running_loop()), \
             patch.object(server.CFG, "memory", fake_memory_cfg):
            server._apply_memory_backup_settings(None, 15, None)
            await asyncio.sleep(0)
        self.assertTrue(event.is_set(), "改了间隔要唤醒 loop")
        self.assertEqual(15, fake_memory_cfg.backup_interval_minutes)


if __name__ == "__main__":
    unittest.main()
