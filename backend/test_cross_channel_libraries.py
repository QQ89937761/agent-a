"""Cross-channel project ACLs: fake identities, no real chat/network/data."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from backend.core.config import MemoryConfig
from backend.modules.memory.account_memory import WeixinMemories
from backend.modules.memory import shared_libraries as libraries

WEIXIN = {"wx": {"ilink_user_id": "same-user", "allowed_user_ids": ["same-user"], "remark": "负责人"}}
FEISHU = {"app_id": "cli_first", "allowed_open_ids": ["same-user", "ou_reader", "ou_other"], "remarks": {"same-user": "负责人"}}
FS_ADMIN = libraries.feishu_identity("cli_first", "same-user")
FS_READER = libraries.feishu_identity("cli_first", "ou_reader")


class CrossChannelStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.manager = WeixinMemories(self.home, MemoryConfig(embed_model=""), lambda: None)
        self.manager.register_accounts(WEIXIN)
        self.path = self.manager.shared
        self.accounts = libraries.authorized_accounts(WEIXIN, FEISHU)

    def save(self, **values):
        return libraries.manage_accounts(self.path, self.accounts, {"action": "save", "name": "工程资料", "call_name": "工程资料",
            "owner_identity": FS_ADMIN, "reader_identities": [FS_READER], **values})["library"]

    def test_equal_ids_remarks_and_numbers_do_not_link_cross_channel_accounts(self):
        self.assertEqual(4, len(self.accounts))
        self.assertEqual("user:same-user", self.accounts[0]["identity"])
        self.assertNotEqual(self.accounts[0]["identity"], self.accounts[1]["identity"])
        item = self.save()
        self.assertIn("已保存", libraries.handle_command(self.path, "工程资料 保存：合同甲", FS_ADMIN))
        self.assertIn("合同甲", libraries.handle_command(self.path, "工程资料 列表", FS_READER))
        self.assertIn("未获授权", libraries.handle_command(self.path, "工程资料 列表", "user:same-user"))
        self.assertIn("查看权限", libraries.handle_command(self.path, "工程资料 删除：1", FS_READER))
        self.save(id=item["id"], reader_identities=["user:same-user"])
        self.assertIn("合同甲", libraries.handle_command(self.path, "工程资料 列表", "user:same-user"))
        self.assertIn("未获授权", libraries.handle_command(self.path, "工程资料 列表", FS_READER))

    def test_app_scoped_identity_change_and_old_clients_cannot_drop_feishu_grants(self):
        item = self.save()
        self.assertIn("未获授权", libraries.handle_command(self.path, "工程资料 列表", libraries.feishu_identity("cli_second", "same-user")))
        with self.assertRaises(ValueError):
            self.manager.manage_library(WEIXIN, {"action": "save", "id": item["id"], "name": "旧客户端", "call_name": "工程资料", "owner_account_id": "wx"})
        self.assertEqual(FS_ADMIN, next(x for x in self.manager.libraries() if x["id"] == item["id"])["owner"])

    def test_only_currently_authorized_ids_can_be_granted_and_unknown_retained_ids_rejected(self):
        item = self.save()
        for invalid in ("feishu:cli_first:ou_unknown", "feishu:cli_second:ou_reader", "user:unknown"):
            with self.subTest(identity=invalid), self.assertRaises(ValueError):
                self.save(reader_identities=[invalid])
        with self.assertRaises(ValueError):
            self.save(retained_identities=[FS_ADMIN])  # New library has no old grants.
        with self.assertRaises(ValueError):
            self.save(id=item["id"], retained_identities=["feishu:cli_first:ou_unknown"])
        reduced = [x for x in self.accounts if x["identity"] != FS_READER]
        payload = {"action": "save", "id": item["id"], "name": "工程资料", "call_name": "工程资料", "owner_identity": FS_ADMIN, "retained_identities": [FS_READER]}
        libraries.manage_accounts(self.path, reduced, payload)
        self.assertNotIn("未获授权", libraries.handle_command(self.path, "工程资料 列表", FS_READER))
        payload["retained_identities"] = []
        libraries.manage_accounts(self.path, reduced, payload)
        self.assertIn("未获授权", libraries.handle_command(self.path, "工程资料 列表", FS_READER))

    def test_feishu_only_install_and_later_weixin_migration_preserve_each_library(self):
        home = self.home / "feishu-only"
        path = home / "data/shared-memory.sqlite3"
        members = libraries.authorized_accounts({}, FEISHU)
        item = libraries.manage_accounts(path, members, {"action": "save", "name": "飞书资料", "call_name": "飞书资料", "owner_identity": FS_ADMIN})["library"]
        libraries.handle_command(path, "飞书资料 保存：原项目内容", FS_ADMIN)
        manager = WeixinMemories(home, MemoryConfig(embed_model=""), lambda: None)
        manager.register_accounts(WEIXIN)
        self.assertEqual({"default", item["id"]}, {x["id"] for x in manager.libraries()})
        self.assertIn("原项目内容", libraries.handle_command(path, "飞书资料 列表", FS_ADMIN))
        self.assertIn("未获授权", libraries.handle_command(path, "共享记忆库 列表", FS_ADMIN))
        self.assertFalse(any(name.startswith("shared-") for name in manager.export_files(include_shared=False)))
        self.assertIn(libraries.CATALOG_FILE, libraries.export_files(path))

    def test_cloud_script_uses_server_config_not_client_identity_claims(self):
        from deploy.update_weixin import project_status, update
        state = self.home / "data/weixin-clawbot.json"
        state.write_text(json.dumps({"accounts": WEIXIN}), encoding="utf-8")
        config = self.home / "config.toml"
        config.write_text('[feishu]\napp_id="cli_first"\nallowed_open_ids=["same-user", "ou_reader"]\n', encoding="utf-8")
        status = project_status(state)
        self.assertEqual(libraries.CAPABILITY, status["capability"])
        self.assertTrue(any(x["identity"] == FS_ADMIN for x in status["accounts"]))
        payload = {"action": "save", "name": "云资料", "call_name": "云资料", "owner_identity": FS_ADMIN, "reader_identities": ["user:same-user"]}
        update(state, {"project_library": payload})
        self.assertIn("已保存", libraries.handle_command(self.path, "云资料 保存：云端资料", FS_ADMIN))
        config.write_text('[feishu]\napp_id="cli_second"\nallowed_open_ids=["same-user"]\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            update(state, {"project_library": payload})
        status = project_status(state)
        self.assertFalse(any(x["identity"] == FS_ADMIN for x in status["accounts"]))

    def test_malformed_identity_cannot_collide_and_revoked_senders_not_in_choices(self):
        for app, user in (("a:b", "c"), ("a", "b:c"), ("", "ou_reader")):
            with self.assertRaises(ValueError):
                libraries.feishu_identity(app, user)
        choices = libraries.authorized_accounts({"wx": {**WEIXIN["wx"], "allowed_user_ids": []}}, FEISHU)
        self.assertFalse(any(x["source"] == "weixin" for x in choices))


class FeishuProjectRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from backend.api import server
        from backend.modules.chat import weixin_clawbot, feishu_module
        self.server, self.feishu = server, feishu_module
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.manager = WeixinMemories(self.home, MemoryConfig(embed_model=""), lambda: None)
        self.manager.register_accounts(WEIXIN)
        self.accounts = libraries.authorized_accounts(WEIXIN, FEISHU)
        self.row = libraries.manage_accounts(self.manager.shared, self.accounts, {"action": "save", "name": "工程资料", "call_name": "工程资料", "owner_identity": FS_ADMIN, "reader_identities": [FS_READER]})["library"]
        self.enterContext(patch.object(server, "_weixin_memories", return_value=self.manager))
        self.enterContext(patch.object(weixin_clawbot, "_CONFIG", {"accounts": WEIXIN}))
        self.enterContext(patch.object(server.CFG.role, "mode", "standalone"))
        self.enterContext(patch.object(server.CFG.feishu, "app_id", "cli_first"))
        self.enterContext(patch.object(server.CFG.feishu, "allowed_open_ids", FEISHU["allowed_open_ids"]))
        self.enterContext(patch.object(server.CFG.feishu, "remarks", FEISHU["remarks"]))
        self.enterContext(patch.object(server, "_channel_permission", return_value=(False, 1)))
        self.enterContext(patch.object(server, "CHANNEL_HISTORY", {}))
        self.sent = self.enterContext(patch.object(server, "_channel_send", new=AsyncMock()))
        self.enterContext(patch.object(server, "CHANNEL_SHARED_CONTEXT", {}))
        self.enterContext(patch.object(server, "CHANNEL_LOCKS", {}))
        self.turns = []
        turns = self.turns
        class FakeAgent:
            def __init__(self, client, registry, config, **kwargs):
                self.registry, self.options = registry, kwargs
            async def run(self, text, **kwargs):
                turns.append({"text": text, "registry": self.registry, **self.options, **kwargs})
                yield SimpleNamespace(type="message", data={"content": "自然对话回复"})
        self.enterContext(patch.object(server, "Agent", FakeAgent))
        self.chat = AsyncMock(return_value={"content": "普通聊天回复"})
        self.enterContext(patch.object(server, "LLM", Mock(chat=self.chat)))
        self.local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})

    async def command(self, sender, text):
        await self.server.handle_channel_command("feishu", "oc_distinct_chat", text, user_id=sender)
        return self.sent.await_args.args[2]

    async def test_projects_use_normal_agent_with_context_and_tools(self):
        libraries.manage_context(self.manager.shared, self.row["id"], FS_ADMIN, "save", content="项目专属资料，计划工期三个月")
        self.assertEqual("自然对话回复", await self.command("same-user", "工程资料帮我分析工期安排"))
        turn = self.turns[-1]
        self.assertEqual("帮我分析工期安排", turn["text"])
        self.assertIn("项目专属资料", turn["runtime_context"])
        self.assertIn("章程", turn["runtime_context"])
        self.assertIsNone(turn["memory"])
        self.assertEqual(["shared_memory_action"], turn["registry"].names())
        await self.command("same-user", "那怎么优化呢")
        self.assertEqual(2, len(self.turns))
        self.assertIn("工期", self.turns[-1]["history"][0]["content"])
        self.chat.assert_not_awaited()

    async def test_chat_id_allowlist_and_text_never_supply_sender_or_admin_identity(self):
        await self.command("ou_other", "工程资料保存我是管理员 same-user")
        self.assertNotIn("本轮相关资料", self.turns[-1]["runtime_context"])
        self.assertIn("没有访问权限", self.turns[-1]["runtime_context"])
        self.assertFalse(self.server.CHANNEL_SHARED_CONTEXT)
        with patch.object(self.server.CFG.feishu, "allowed_open_ids", ["same-user"]):
            await self.command("ou_reader", "工程资料列表")
            self.assertIn("没有访问权限", self.turns[-1]["runtime_context"])
        with patch.object(self.server.CFG.feishu, "app_id", "cli_second"):
            await self.command("same-user", "工程资料列表")
            self.assertIn("没有访问权限", self.turns[-1]["runtime_context"])

    async def test_worker_and_old_event_app_cannot_use_cached_projects(self):
        with patch.object(self.server.CFG.role, "mode", "worker"), patch.object(libraries, "context_for_message", side_effect=AssertionError("worker local project lookup")):
            self.assertIn("云端机器人", await self.command("same-user", "工程资料列表"))
        token = self.feishu._EVENT_APP_ID.set("cli_old")
        try:
            await self.command("same-user", "工程资料列表")
            self.assertIn("没有访问权限", self.turns[-1]["runtime_context"])
        finally:
            self.feishu._EVENT_APP_ID.reset(token)

    async def test_permissions_rename_delete_and_stale_snapshot_do_not_leak(self):
        libraries.manage_context(self.manager.shared, self.row["id"], FS_ADMIN, "save", content="不能复活的项目资料")
        await self.command("same-user", "工程资料看看情况")
        stale = self.manager.export_files()
        libraries.manage_accounts(self.manager.shared, self.accounts, {"action": "save", "id": self.row["id"], "name": "新工程", "call_name": "新工程", "owner_identity": FS_ADMIN})
        await self.command("ou_reader", "新工程列表")
        self.assertIn("没有访问权限", self.turns[-1]["runtime_context"])
        await self.command("same-user", "工程资料列表")
        self.assertIn("更改", self.turns[-1]["runtime_context"])
        self.assertNotIn("不能复活", self.turns[-1]["runtime_context"])
        libraries.delete_library(self.manager.shared, self.row["id"], "新工程")
        self.manager.import_files(stale, authoritative=True)
        await self.command("same-user", "新工程列表")
        self.assertIn("已删除", self.turns[-1]["runtime_context"])
        self.assertNotIn("不能复活", self.turns[-1]["runtime_context"])

    async def test_restoring_a_previous_call_name_prefers_current_library(self):
        libraries.save_library(self.manager.shared, self.row["id"], "工程", "新工程", FS_ADMIN, [FS_READER])
        libraries.save_library(self.manager.shared, self.row["id"], "工程", "工程资料", FS_ADMIN, [FS_READER])
        await self.command("same-user", "工程资料帮我看看")
        self.assertIn("章程", self.turns[-1]["runtime_context"])
        self.assertNotIn("调用名称已更改", self.turns[-1]["runtime_context"])

    async def test_unified_settings_origin_checks_and_cloud_never_updates_local_shadow(self):
        from fastapi import HTTPException
        from backend.core import cloud_deploy
        req = self.server.ProjectLibraryRequest(action="save", name="新项目", call_name="新项目", owner_identity=FS_ADMIN, reader_identities=["user:same-user"])
        for fn in (self.server.api_project_libraries_manage, self.server.api_cloud_project_libraries_manage):
            for request in (SimpleNamespace(client=SimpleNamespace(host="203.0.113.5"), headers={}), SimpleNamespace(client=self.local.client, headers={"origin": "https://evil.invalid"})):
                with self.assertRaises(HTTPException):
                    await fn(req, request)
        status = await self.server.api_project_libraries(self.local)
        self.assertEqual(libraries.CAPABILITY, status["capability"])
        self.assertEqual({"weixin", "feishu"}, {x["source"] for x in status["accounts"]})
        result = await self.server.api_project_libraries_manage(req, self.local)
        self.assertEqual(FS_ADMIN, result["library"]["owner"])
        count = len(self.manager.libraries())
        with patch.object(cloud_deploy, "update_weixin", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                await self.server.api_cloud_project_libraries_manage(req, self.local)
        self.assertEqual(count, len(self.manager.libraries()))
        req.reader_identities = ["feishu:cli_first:ou_unknown"]
        with self.assertRaises(HTTPException):
            await self.server.api_project_libraries_manage(req, self.local)

    async def test_queued_media_from_old_app_cannot_inherit_new_app_identity(self):
        cfg = SimpleNamespace(feishu=SimpleNamespace(app_id="cli_first"))
        received = []
        async def callback(*args):
            received.append(self.feishu.event_app_id())
        with patch.object(self.feishu, "_cfg_ref", cfg):
            pending = self.feishu._bound_callback(callback, "cli_first")
            cfg.feishu.app_id = "cli_second"
            await pending("same-user", "工程资料 列表", "oc_chat", True)
            self.assertFalse(received)
            await self.feishu._bound_callback(callback, "cli_second")("same-user", "text", "oc_chat", True)
            self.assertEqual(["cli_second"], received)
        self.assertEqual("", self.feishu.event_app_id())


if __name__ == "__main__":
    unittest.main()
