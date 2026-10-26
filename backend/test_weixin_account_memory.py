"""Two-account privacy, persistent ownership and separate snapshot regressions."""
from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from backend.core.config import MemoryConfig
from backend.core.tools import ToolRegistry
from backend.modules.memory.account_memory import WeixinMemories, CATALOG_FILE, SHARED_FILE
from backend.modules.memory.shared_memory import handle_shared_command
from backend.modules.memory.tool_module import register_memory_tools

ACCOUNTS = {
    "bot-a": {"ilink_user_id": "user-a", "allowed_user_ids": ["user-a"], "connected": True, "bot_token": "test-a"},
    "bot-b": {"ilink_user_id": "user-b", "allowed_user_ids": ["user-b"], "connected": True, "bot_token": "test-b"},
}


class AccountMemoryStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.cfg = MemoryConfig(embed_model="")
        self.manager = WeixinMemories(self.home, self.cfg, lambda: None)
        self.manager.register_accounts(ACCOUNTS)

    def command(self, account, text, accounts=None):
        accounts = accounts or ACCOUNTS
        return handle_shared_command(self.manager.shared, text, account,
                                     can_write=self.manager.is_owner(account, accounts))

    def test_independent_stores_and_all_mutations_are_scoped(self):
        a = self.manager.for_account("bot-a", ACCOUNTS)
        b = self.manager.for_account("bot-b", ACCOUNTS)
        first = a.add("甲的私人偏好：喜欢红色")
        second = b.add("乙的私人偏好：喜欢蓝色")
        self.assertEqual(first, second)  # The same ID in two databases cannot cross stores.
        self.assertEqual([], b.search("红色"))
        self.assertEqual([], a.search("蓝色"))
        b.update(second, "乙改成绿色")
        b.forget(second)
        self.assertIn("红色", str(a.list_memories()))
        self.assertNotEqual(a.data_dir, b.data_dir)
        self.assertEqual([], a.project_roots())
        with self.assertRaises(ValueError):
            self.manager.for_account("unknown", ACCOUNTS)

    def test_explicit_prefix_only_and_read_only_for_other_accounts(self):
        self.assertIsNone(self.command("bot-a", "共享保存：不再接受旧前缀"))
        self.assertIsNone(self.command("bot-a", "请查共享记忆库 公司地址"))
        self.assertIn("已保存", self.command("bot-a", "共享记忆库 保存：公司地址在 A 栋"))
        self.assertIn("A 栋", self.command("bot-b", "共享记忆库 查询：公司地址"))
        for text in ("保存：冒充管理员", "修改：1：B 栋", "删除：1"):
            self.assertIn("查看权限", self.command("bot-b", "共享记忆库 " + text))
        self.assertIn("A 栋", self.command("bot-b", "共享记忆库 列表"))
        self.assertIn("已删除", self.command("bot-a", "共享记忆库 删除：1"))
        self.assertIn("没有找到", self.command("bot-b", "共享记忆库 查询：公司地址"))

    def test_owner_survives_restart_reorder_unbind_and_new_bot_id(self):
        restarted = WeixinMemories(self.home, self.cfg, lambda: None)
        reordered = dict(reversed(list(ACCOUNTS.items())))
        restarted.register_accounts(reordered)
        self.assertTrue(restarted.is_owner("bot-a", reordered))
        remaining = {"bot-b": ACCOUNTS["bot-b"]}
        restarted.register_accounts(remaining)
        self.assertFalse(restarted.is_owner("bot-b", remaining))
        relinked = {**remaining, "new-bot-a": ACCOUNTS["bot-a"]}
        restarted.register_accounts(relinked)
        self.assertTrue(restarted.is_owner("new-bot-a", relinked))
        self.assertEqual(self.manager.for_account("bot-a", ACCOUNTS).data_dir,
                         restarted.for_account("new-bot-a", relinked).data_dir)

    def test_transfer_owner_is_explicit_and_does_not_move_private_data(self):
        a = self.manager.for_account("bot-a", ACCOUNTS)
        a.add("甲的私密信息")
        self.manager.transfer_owner("bot-b", ACCOUNTS)
        self.assertFalse(self.manager.is_owner("bot-a", ACCOUNTS))
        self.assertTrue(self.manager.is_owner("bot-b", ACCOUNTS))
        self.assertEqual([], self.manager.for_account("bot-b", ACCOUNTS).search("私密信息"))
        with self.assertRaises(ValueError):
            self.manager.transfer_owner("missing", ACCOUNTS)

    def test_identity_conflict_cannot_reassign_an_existing_store(self):
        with self.assertRaises(ValueError):
            self.manager.register_accounts({"bot-a": ACCOUNTS["bot-b"]})
        self.assertTrue(self.manager.is_owner("bot-a", ACCOUNTS))

    def test_concurrent_first_registration_keeps_one_owner(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: self.manager.register_accounts(ACCOUNTS), range(20)))
        self.assertEqual("user:user-a", self.manager.owner())

    def test_model_loading_does_not_block_identity_or_private_store_access(self):
        started, release = threading.Event(), threading.Event()
        embedder = object()

        def load_model(_model):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test model loader was not released")
            return embedder

        with patch("backend.modules.memory.account_memory.make_default_embedder", side_effect=load_model) as loader:
            with ThreadPoolExecutor(max_workers=2) as pool:
                loading = pool.submit(self.manager._shared_embedder, "test-model")
                try:
                    self.assertTrue(started.wait(2))
                    store = pool.submit(self.manager.for_account, "bot-b", ACCOUNTS).result(timeout=2)
                    self.assertEqual([], store.list_memories())
                    self.assertEqual("user:user-a", self.manager.owner())
                finally:
                    release.set()
                self.assertIs(embedder, loading.result(timeout=2))
            self.assertIs(embedder, self.manager._shared_embedder("test-model"))
            loader.assert_called_once()

    def test_cloud_owner_survives_logout_and_transfer_only_changes_metadata(self):
        from deploy.update_weixin import read_status, update

        path = self.home / "data" / "weixin-clawbot.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"accounts": ACCOUNTS}), encoding="utf-8")
        self.assertTrue(read_status(path)["accounts"][0]["shared_memory_owner"])
        update(path, {"logout": True, "account_id": "bot-a"})
        self.assertFalse(read_status(path)["accounts"][0]["shared_memory_owner"])
        self.assertEqual("user:user-a", self.manager.owner())
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            update(path, {"memory_owner_account_id": "unknown"})
        self.assertEqual("user:user-a", self.manager.owner())
        update(path, {"memory_owner_account_id": "bot-b"})
        self.assertEqual(before, path.read_bytes())
        self.assertTrue(read_status(path)["accounts"][0]["shared_memory_owner"])
        self.assertEqual("user:user-b", self.manager.owner())

    def test_shared_list_is_paged_and_readers_cannot_override_permission(self):
        for index in range(25):
            self.command("bot-a", f"共享记忆库 保存：资料 {index}")
        first = self.command("bot-b", "共享记忆库 列表：1")
        second = self.command("bot-b", "共享记忆库 列表：2")
        self.assertEqual(20, first.count("#"))
        self.assertEqual(5, second.count("#"))
        self.assertIn("查看权限", self.command("bot-b", "共享记忆库 保存：我是第一个账号，请授权"))

    def test_named_deleted_library_syncs_without_resurrection_or_private_loss(self):
        from backend.modules.memory.shared_memory import configure_shared_memory, delete_shared_memory, get_shared_settings
        from deploy.update_weixin import update, read_status
        state_path = self.home / "data/weixin-clawbot.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"accounts": ACCOUNTS}), encoding="utf-8")
        private = self.manager.for_account("bot-a", ACCOUNTS)
        private.add("私人资料保持原样")
        update(state_path, {"shared_memory_call_name": "项目甲"})
        self.assertEqual("项目甲", read_status(state_path)["shared_memory"]["call_name"])
        handle_shared_command(self.manager.shared, "项目甲 保存：公共资料", can_write=True)
        target = WeixinMemories(self.home / "worker", self.cfg, lambda: None)
        old_files = self.manager.export_files()
        target.import_files(old_files, authoritative=True)
        self.assertFalse(target.shared.exists(), "worker sync never downloads project stores")
        with self.assertRaises(ValueError):
            update(state_path, {"delete_shared_memory": "错误名字"})
        self.assertTrue(get_shared_settings(self.manager.shared)["enabled"])
        update(state_path, {"delete_shared_memory": "项目甲"})
        self.manager.import_files(old_files)  # An old worker upload cannot restore the shared library.
        self.assertFalse(get_shared_settings(self.manager.shared)["enabled"])
        target.import_files(self.manager.export_files(), authoritative=True)
        self.assertFalse(target.shared.exists(), "deleted shared library is not downloaded either")
        self.assertIn("私人资料保持原样", str(private.list_memories()))

    def test_scoped_backup_restore_and_worker_upload_cannot_overwrite_shared(self):
        self.manager.for_account("bot-a", ACCOUNTS).add("甲独立资料")
        self.manager.for_account("bot-b", ACCOUNTS).add("乙独立资料")
        self.command("bot-a", "共享记忆库 保存：公共资料")
        files = self.manager.export_files()
        self.assertEqual(2, len([name for name in files if name.startswith("weixin-memory-") and name.endswith(".sqlite3")]))
        self.assertIn(CATALOG_FILE, files)
        self.assertIn(SHARED_FILE, files)
        target = WeixinMemories(self.home / "restored", self.cfg, lambda: None)
        target.import_files(files, authoritative=True)
        self.assertTrue(target.is_owner("bot-a", ACCOUNTS))
        self.assertIn("甲独立", str(target.for_account("bot-a", ACCOUNTS).list_memories()))
        self.assertNotIn("甲独立", str(target.for_account("bot-b", ACCOUNTS).list_memories()))
        self.assertNotIn("公共资料", handle_shared_command(target.shared, "共享记忆库 列表"), "private sync never copies project content")
        target.transfer_owner("bot-b", ACCOUNTS)
        handle_shared_command(target.shared, "共享记忆库 修改：1：本地伪造资料", "b", can_write=True)
        self.manager.import_files(target.export_files())
        self.assertTrue(self.manager.is_owner("bot-a", ACCOUNTS))
        self.assertNotIn("伪造", self.command("bot-b", "共享记忆库 列表"))
        self.command("bot-a", "共享记忆库 删除：1")
        target.import_files(self.manager.export_files(), authoritative=True)
        self.assertIn("为空", self.command("bot-b", "共享记忆库 列表"))
        self.assertNotIn("公共资料", handle_shared_command(target.shared, "共享记忆库 列表", "b"))

    def test_first_worker_backup_seeds_original_owner_on_empty_hub(self):
        self.manager.register_accounts({"bot-b": ACCOUNTS["bot-b"]})
        target = WeixinMemories(self.home / "empty-hub", self.cfg, lambda: None)
        target.import_files(self.manager.export_files())
        self.assertEqual("user:user-a", target.owner())
        target.register_accounts({"bot-b": ACCOUNTS["bot-b"]})
        self.assertFalse(target.is_owner("bot-b", ACCOUNTS))

    def test_private_updates_and_deletions_survive_two_way_sync(self):
        a = self.manager.for_account("bot-a", ACCOUNTS)
        memory_id = a.add("旧私人规则")
        target = WeixinMemories(self.home / "replica", self.cfg, lambda: None)
        target.import_files(self.manager.export_files(), authoritative=True)
        replica = target.for_account("bot-a", ACCOUNTS)
        replica_id = replica.list_memories()[0]["id"]
        replica.update(replica_id, "新私人规则")
        self.manager.import_files(target.export_files())
        self.assertIn("新私人规则", str(a.list_memories()))
        self.assertNotIn("旧私人规则", str(a.list_memories()))
        a.forget(memory_id)
        target.import_files(self.manager.export_files(), authoritative=True)
        self.assertEqual([], replica.list_memories())
        self.manager.import_files(target.export_files())
        self.assertEqual([], a.list_memories())

    def test_snapshot_content_cannot_be_relabelled_as_another_account(self):
        a = self.manager.for_account("bot-a", ACCOUNTS)
        b = self.manager.for_account("bot-b", ACCOUNTS)
        a.add("甲私密")
        files = self.manager.export_files()
        a_name = f"weixin-memory-{a.data_dir.name}.sqlite3"
        b_name = f"weixin-memory-{b.data_dir.name}.sqlite3"
        with self.assertRaises(ValueError):
            self.manager.import_files({b_name: files[a_name]})
        self.assertEqual([], b.list_memories())

    def test_bad_scope_metadata_and_path_names_cannot_escape(self):
        a = self.manager.for_account("bot-a", ACCOUNTS)
        a.add("私密内容")
        files = self.manager.export_files()
        private = next(name for name in files if name.endswith(".sqlite3"))
        unknown = "weixin-memory-" + "0" * 64 + ".sqlite3"
        with self.assertRaises(ValueError):
            self.manager.import_files({unknown: files[private]})
        broken = {CATALOG_FILE: json.dumps({"version": 1, "owner": "user:alien", "bindings": {}}).encode()}
        with self.assertRaises(ValueError):
            self.manager.import_files(broken, authoritative=True)
        self.assertEqual({"added": 0, "skipped": 0}, self.manager.import_files({"../memory.sqlite3": files[private]}))


class WeixinMemoryRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from backend.api import server
        from backend.modules.chat import weixin_clawbot
        self.server = server
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.manager = WeixinMemories(self.home, MemoryConfig(embed_model=""), lambda: None)
        self.manager.register_accounts(ACCOUNTS)
        self.enterContext(patch.object(server, "_weixin_memories", return_value=self.manager))
        self.enterContext(patch.object(weixin_clawbot, "_CONFIG", {"accounts": ACCOUNTS}))
        self.enterContext(patch.object(server, "_channel_permission", return_value=(False, 1)))
        self.enterContext(patch.object(server.CFG.role, "mode", "standalone"))
        self.enterContext(patch.object(server, "CHANNEL_HISTORY", {}))
        self.sent = self.enterContext(patch.object(server, "_channel_send", new=AsyncMock()))
        self.enterContext(patch.object(server, "CHANNEL_SHARED_CONTEXT", {}))
        self.enterContext(patch.object(server, "CHANNEL_LOCKS", {}))
        class ProjectLLM:
            def __init__(self):
                self.action, self.messages = None, []
            async def chat_stream_tools(self, messages, **kwargs):
                self.messages = messages
                if self.action:
                    action, self.action = self.action, None
                    yield "done", {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "project-tool", "type": "function", "function": {
                            "name": "shared_memory_action", "arguments": json.dumps(action, ensure_ascii=False),
                        },
                    }]}
                else:
                    tools = [m["content"] for m in messages if m["role"] == "tool"]
                    yield "done", {"role": "assistant", "content": str(tools[-1]) if tools else "我们接着聊这个项目。"}
            async def chat(self, *args, **kwargs):
                raise AssertionError("unexpected ordinary chat")
        self.llm = ProjectLLM()
        self.enterContext(patch.object(server, "LLM", self.llm))

    async def test_shared_works_with_remote_off_using_real_agent_and_scoped_tools(self):
        server = self.server
        self.llm.action = {"action": "save", "content": "公共规则"}
        await server.handle_channel_command("weixin", "user-a", "共享记忆库记住公共规则", account_id="bot-a")
        self.assertIn("已保存", self.sent.await_args.args[2])
        self.llm.action = {"action": "search", "query": "公共规则"}
        await server.handle_channel_command("weixin", "user-b", "共享记忆库聊聊公共规则", account_id="bot-b")
        self.assertIn("公共规则", self.sent.await_args.args[2])
        self.llm.action = {"action": "delete", "memory_id": 1}
        await server.handle_channel_command("weixin", "user-b", "帮我删掉这条", account_id="bot-b")
        self.assertIn("非管理人员", self.sent.await_args.args[2])
        self.assertIn("公共规则", handle_shared_command(self.manager.shared, "共享记忆库 列表"))
        self.assertTrue(server.CHANNEL_HISTORY[("weixin", "bot-b", "user-b")])
        self.assertTrue(all("_shared_library" not in message for message in self.llm.messages))
        self.assertFalse(self.manager.for_account("bot-a", ACCOUNTS).list_memories())

    async def test_unknown_sender_cannot_read_shared(self):
        await self.server.handle_channel_command("weixin", "stranger", "共享记忆库列表", account_id="bot-a")
        self.assertIn("没有访问权限", self.llm.messages[0]["content"])
        self.assertNotIn("本轮相关资料", self.llm.messages[0]["content"])

    async def test_memory_tools_resolve_account_before_thread_and_do_not_leak(self):
        server = self.server
        registry = ToolRegistry()
        register_memory_tools(registry, server._memory_for_current_channel)
        async def run(account, user, content):
            token = server.CURRENT_CHANNEL.set(("weixin", account, user))
            try:
                await registry.call("memory_add", {"text": content})
                return await registry.call("memory_list", {})
            finally:
                server.CURRENT_CHANNEL.reset(token)
        a, b = await asyncio.gather(run("bot-a", "user-a", "甲私人"), run("bot-b", "user-b", "乙私人"))
        self.assertIn("甲私人", a)
        self.assertNotIn("乙私人", a)
        self.assertIn("乙私人", b)
        self.assertNotIn("甲私人", b)

    async def test_chat_only_has_only_private_memory_tools_and_auto_memory(self):
        server = self.server
        agents = []
        class MemoryOnlyAgent:
            def __init__(self, client, registry, cfg, **kwargs):
                self.memory = kwargs["memory"]
                agents.append(self)
                self.registry = registry
            async def run(self, text, **kwargs):
                self.memory.add(text)
                self.schemas = self.registry.names()
                yield SimpleNamespace(type="message_end", data={"content": "已记在当前账号"})
        self.enterContext(patch.object(server, "Agent", MemoryOnlyAgent))
        for account, user, text in (("bot-a", "user-a", "甲仅聊天记忆"), ("bot-b", "user-b", "乙仅聊天记忆")):
            await server.handle_channel_command("weixin", user, text, account_id=account)
        self.assertEqual(2, len(agents))
        for agent in agents:
            self.assertTrue(all(name.startswith("memory_") for name in agent.schemas))
            self.assertEqual(6, len(agent.schemas))
        self.assertNotIn("乙仅聊天", str(agents[0].memory.list_memories()))
        self.assertNotIn("甲仅聊天", str(agents[1].memory.list_memories()))
        for account, user in (("bot-a", "user-a"), ("bot-b", "user-b")):
            server.CHANNEL_HISTORY.pop(("weixin", account, user), None)
            server.CHANNEL_LOCKS.pop(("weixin", account, user), None)

    async def test_named_library_keeps_authorization_in_normal_agent(self):
        from backend.modules.memory.shared_libraries import legacy_settings
        legacy_settings(self.manager.shared, name="项目甲资料")
        self.llm.action = {"action": "save", "content": "甲项目公共规则"}
        await self.server.handle_channel_command("weixin", "user-a", "项目甲资料记下公共规则", account_id="bot-a")
        self.assertIn("已保存", self.sent.await_args.args[2])
        await self.server.handle_channel_command("weixin", "user-b", "项目甲资料聊聊公共规则", account_id="bot-b")
        self.assertIn("甲项目公共规则", self.llm.messages[0]["content"])
        await self.server.handle_channel_command("weixin", "stranger", "项目甲资料列表", account_id="bot-a")
        self.assertIn("没有访问权限", self.llm.messages[0]["content"])
        self.assertNotIn("甲项目公共规则", self.llm.messages[0]["content"])

    async def test_settings_delete_require_local_origin_and_exact_confirmation(self):
        from fastapi import HTTPException
        from backend.modules.memory.shared_memory import get_shared_settings
        server = self.server
        req = server.SharedMemorySettingsRequest(call_name="项目资料")
        for host, headers in (("203.0.113.5", {}), ("127.0.0.1", {"origin": "https://evil.example"})):
            with self.subTest(host=host), self.assertRaises(HTTPException):
                await server.api_weixin_memory_settings(req, SimpleNamespace(client=SimpleNamespace(host=host), headers=headers))
        local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
        await server.api_weixin_memory_settings(req, local)
        with self.assertRaises(HTTPException):
            await server.api_weixin_memory_delete(server.SharedMemoryDeleteRequest(confirmation="其他资料"), local)
        self.assertTrue(get_shared_settings(self.manager.shared)["enabled"])
        await server.api_weixin_memory_delete(server.SharedMemoryDeleteRequest(confirmation="项目资料"), local)
        self.assertFalse(get_shared_settings(self.manager.shared)["enabled"])

    async def test_cloud_failure_does_not_change_local_settings(self):
        from backend.modules.memory.shared_memory import get_shared_settings
        from backend.core import cloud_deploy
        local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
        req = self.server.SharedMemorySettingsRequest(call_name="云端资料")
        with patch.object(cloud_deploy, "update_weixin", side_effect=RuntimeError("云端需更新")):
            with self.assertRaises(RuntimeError):
                await self.server.api_cloud_weixin_memory_settings(req, local)
        self.assertEqual("共享记忆库", get_shared_settings(self.manager.shared)["call_name"])
        with patch.object(cloud_deploy, "update_weixin") as update:
            await self.server.api_cloud_weixin_memory_settings(req, local)
            self.assertEqual({"shared_memory_call_name": "云端资料"}, update.call_args.args[1])
            await self.server.api_cloud_weixin_memory_delete(self.server.SharedMemoryDeleteRequest(confirmation="云端资料"), local)
            self.assertEqual({"delete_shared_memory": "云端资料"}, update.call_args.args[1])
        self.assertTrue(get_shared_settings(self.manager.shared)["enabled"], "cloud settings must not alter an independent local library")

    async def test_owner_transfer_endpoint_requires_local_request(self):
        from fastapi import HTTPException
        server = self.server
        req = server.SharedMemoryOwnerRequest(account_id="bot-b")
        remote = SimpleNamespace(client=SimpleNamespace(host="203.0.113.5"))
        with self.assertRaises(HTTPException) as failure:
            await server.api_weixin_memory_owner(req, remote)
        self.assertEqual(403, failure.exception.status_code)
        local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        self.assertTrue((await server.api_weixin_memory_owner(req, local))["ok"])
        self.assertTrue(self.manager.is_owner("bot-b", ACCOUNTS))

    async def test_worker_shared_endpoint_checks_transport_and_bound_identity(self):
        from fastapi import HTTPException
        server = self.server
        req = server.SharedMemoryCommandRequest(account_id="bot-a", user_id="user-a", sender_id="user-a", text="共享记忆库 保存：云端规则")
        with patch.object(server.CFG.role, "mode", "hub"), patch.object(server.CFG.role, "hub_token", "bridge-test-secret"):
            with self.assertRaises(HTTPException):
                await server.api_weixin_shared_memory(req, SimpleNamespace(headers={}))
            request = SimpleNamespace(headers={"x-hub-token": "bridge-test-secret"})
            result = await server.api_weixin_shared_memory(req, request)
            self.assertIn("已保存", result["reply"])
            req.account_id, req.user_id, req.sender_id = "bot-b", "user-b", "user-b"
            result = await server.api_weixin_shared_memory(req, request)
            self.assertIn("查看权限", result["reply"])
            req.user_id = "user-a"
            with self.assertRaises(HTTPException):
                await server.api_weixin_shared_memory(req, request)

    async def test_weixin_project_tools_cannot_index_desktop_memory(self):
        server = self.server
        token = server.CURRENT_CHANNEL.set(("weixin", "bot-a", "user-a"))
        try:
            with self.assertRaises(PermissionError):
                server._project_memory_for_current_channel()
        finally:
            server.CURRENT_CHANNEL.reset(token)


if __name__ == "__main__":
    unittest.main()
