from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.modules.memory.shared_memory import (handle_shared_command, configure_shared_memory,
    delete_shared_memory, get_shared_settings, is_shared_command, validate_call_name)


class SharedMemoryTests(unittest.TestCase):
    def test_only_shared_prefix_triggers_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shared.sqlite3"
            self.assertIsNone(handle_shared_command(path, "查询公司地址", "user", can_write=True))
            self.assertIn("共享记忆库 查询", handle_shared_command(path, "共享记忆库 帮助", "user", can_write=True))
            self.assertIn("共享记忆库 查询", handle_shared_command(path, "共享记忆库", "user", can_write=True))

    def test_administrator_can_read_and_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shared.sqlite3"
            self.assertIn("已保存", handle_shared_command(path, "共享记忆库 保存：公司地址在A楼", "user", can_write=True))
            self.assertIn("公司地址在A楼", handle_shared_command(path, "共享记忆库 查询：公司地址", "user", can_write=True))
            self.assertIn("已更新", handle_shared_command(path, "共享记忆库 修改：1：公司地址在B楼", "user", can_write=True))
            self.assertIn("公司地址在B楼", handle_shared_command(path, "共享记忆库 查询：公司地址", "user", can_write=True))
            self.assertIn("已删除", handle_shared_command(path, "共享记忆库 删除：1", "user", can_write=True))

    def test_sensitive_values_are_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shared.sqlite3"
            result = handle_shared_command(path, "共享记忆库 保存：API token 是 abc", "user", can_write=True)
            self.assertIn("敏感", result)
            self.assertIn("没有找到", handle_shared_command(path, "共享记忆库 查询：API", "user", can_write=True))

    def test_custom_name_and_deletion_preserve_permission_and_disable_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shared.sqlite3"
            configure_shared_memory(path, "甲项目资料")
            self.assertTrue(is_shared_command(path, "甲项目资料 保存：甲项目地址"))
            self.assertFalse(is_shared_command(path, "请查甲项目资料"))
            self.assertIn("甲项目资料 查询", handle_shared_command(path, "甲项目资料 帮助"))
            self.assertIn("已更改", handle_shared_command(path, "共享记忆库 保存：旧入口不能写入", can_write=True))
            self.assertIn("查看权限", handle_shared_command(path, "甲项目资料 保存：不能越权"))
            self.assertIn("已保存", handle_shared_command(path, "甲项目资料 保存：甲项目地址", can_write=True))
            self.assertEqual({"call_name": "甲项目资料", "enabled": True}, get_shared_settings(path))
            delete_shared_memory(path)
            self.assertFalse(get_shared_settings(path)["enabled"])
            self.assertIn("已删除", handle_shared_command(path, "甲项目资料 保存：不能重新写入", can_write=True))
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(0, db.execute("SELECT COUNT(*) FROM shared_memory").fetchone()[0])
            configure_shared_memory(path, "乙项目资料")
            self.assertIn("为空", handle_shared_command(path, "乙项目资料 列表"))

    def test_invalid_names_do_not_change_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shared.sqlite3"
            for name in ("", "a", "../data", "含 空格", "x" * 31, "锁定", "解锁", "lock", "unlock", "冻结", None):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    validate_call_name(name)
            self.assertEqual("共享记忆库", get_shared_settings(path)["call_name"])


if __name__ == "__main__":
    unittest.main()
