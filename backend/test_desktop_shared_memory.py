"""Desktop saves must reach the same authoritative store that channels query."""
import tempfile
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.api import server
from backend.core.tools import ToolRegistry
from backend.modules.memory import shared_libraries as libraries
from backend import test_deferred_conversations as fixtures


class DesktopProjectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.path = self.home / 'data/shared-memory.sqlite3'
        libraries.ensure_catalog(self.path, 'user:admin', ['user:admin'])
        self.item = libraries.save_library(self.path, '', '完整工程名称', '火箭项目', 'user:admin', ['feishu:app:reader'])
        self.enterContext(patch.object(server, 'app_home', return_value=self.home))
        self.enterContext(patch.object(server, '_weixin_memories', return_value=SimpleNamespace(shared=self.path)))
        self.enterContext(patch.object(server, '_SHARED_ROUTE_HOME', self.home))
        self.enterContext(patch.object(server, '_SHARED_ROUTE_NAMES', None))
        self.enterContext(patch.object(server, '_SHARED_ROUTE_READY', False))
        self.enterContext(patch.object(server.CFG.role, 'mode', 'standalone'))
        self.token = server._SHARED_TURN.set(None)
        self.addCleanup(server._SHARED_TURN.reset, self.token)

    async def test_local_save_and_channel_read_share_store_and_keep_private_tools_out(self):
        state = {}
        self.assertEqual([], await server._prepare_desktop_shared(state, '火箭项目 保存合同', [{'role':'user','content':'私有资料'}], True))
        self.assertIn('完整工程名称', server._SHARED_TURN.get()['prompt'])
        registry = ToolRegistry()
        for name in ['memory_add', 'project_index', 'fs_read_text']:
            registry.tool(name, '', {})(AsyncMock())
        selected = server._channel_agent_registry(registry)
        self.assertEqual(['fs_read_text', 'shared_memory_action'], selected.names())
        reply = await selected.get('shared_memory_action').handler(action='save', content='合同总价：123万元；来源合同第1页')
        self.assertIn('已保存共享记忆', reply)
        for identity in ['user:admin', 'feishu:app:reader']:
            context = libraries.context_for_message(self.path, '火箭项目的总造价是多少', identity)
            self.assertTrue(context['authorized'])
            self.assertIn('123万元', context['prompt'])
        self.assertIn('非管理人员', libraries.manage_context(self.path, self.item['id'], 'feishu:app:reader', 'delete', memory_id=1))
        expires = state['shared_context']['expires_at']
        history = [{'role':'assistant','content':'本项目资料','_shared_library':self.item['id']},
                   {'role':'assistant','content':'别的项目','_shared_library':'other'}]
        self.assertEqual(history[:1], await server._prepare_desktop_shared(state, '那工作内容呢', history, True))
        self.assertEqual(expires, state['shared_context']['expires_at'])
        with patch.object(server.time, 'monotonic', return_value=expires+1):
            self.assertEqual([], await server._prepare_desktop_shared(state, '继续', history, True))
        self.assertIsNone(server._SHARED_TURN.get())

    async def test_ordinary_chat_does_not_probe_cloud_and_prefix_has_no_separator_requirement(self):
        request = self.enterContext(patch.object(server, '_desktop_project_request', new=AsyncMock()))
        self.assertFalse(server._shared_route_matches('今天帮我整理一下会议纪要'))
        self.assertTrue(server._shared_route_matches('火箭项目的总造价是多少'))
        self.assertEqual([{'role': 'user', 'content': '私人上下文'}], await server._prepare_desktop_shared(
            {}, '今天帮我整理一下会议纪要', [{'role': 'user', 'content': '私人上下文'}], True))
        request.assert_not_awaited()

    async def test_cold_cloud_catalog_keeps_tools_responsive_without_private_memory_fallback(self):
        target = self.home / 'data/cloud-ssh/target.json'
        target.parent.mkdir(parents=True)
        target.write_text('{}', encoding='utf-8')
        request = self.enterContext(patch.object(server, '_desktop_project_request', new=AsyncMock()))
        await server._prepare_desktop_shared({}, '另一项目帮我保存合同', [], True)
        request.assert_not_awaited()
        self.assertFalse(server._SHARED_TURN.get()['authorized'])
        registry = ToolRegistry()
        for name in ('memory_add', 'project_index', 'fs_read_text'):
            registry.tool(name, '', {})(AsyncMock())
        self.assertNotIn('memory_add', server._channel_agent_registry(registry).names())
        self.assertIn('fs_read_text', server._channel_agent_registry(registry).names())

    async def test_saved_routes_survive_restart_and_rename_without_display_name_matching(self):
        server._save_shared_route_names({'ok': True, 'libraries': [
            {'name': '仅展示的工程全称', 'call_name': '新工程', 'aliases': ['旧工程']}]})
        server._SHARED_ROUTE_NAMES = None  # Simulate a fresh process.
        self.assertTrue(server._shared_route_matches('新工程保存合同'))
        self.assertTrue(server._shared_route_matches('旧工程的工期'))
        self.assertFalse(server._shared_route_matches('仅展示的工程全称保存合同'))
        self.assertTrue(server._SHARED_ROUTE_READY)
        self.assertIn('新工程', json.loads(server._shared_route_cache_path().read_text(encoding='utf-8'))['names'])

    async def test_new_cloud_project_is_routable_even_when_metadata_refresh_fails(self):
        from backend.core import cloud_deploy
        request = SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'), headers={})
        self.enterContext(patch.object(cloud_deploy, 'update_weixin'))
        self.enterContext(patch.object(server, '_refresh_shared_route_names_async', new=AsyncMock()))
        req = server.ProjectLibraryRequest(action='save', name='新建工程全称', call_name='新工程', owner_identity='user:admin')
        await server.api_cloud_project_libraries_manage(req, request)
        server._SHARED_ROUTE_NAMES = None
        self.assertTrue(server._shared_route_matches('新工程保存合同'))
        self.assertFalse(server._SHARED_ROUTE_READY, 'a partial write result is not the complete catalog')
        invoke = self.enterContext(patch.object(server, '_desktop_project_request', new=AsyncMock(side_effect=RuntimeError('offline'))))
        await server._prepare_desktop_shared({}, '新工程保存合同', [], True)
        invoke.assert_awaited_once()
        self.assertFalse(server._SHARED_TURN.get()['authorized'])

    async def test_remote_browser_cannot_use_management_identity_and_cloud_failure_never_falls_back(self):
        request = self.enterContext(patch.object(server, '_desktop_project_request', new=AsyncMock(side_effect=RuntimeError('offline'))))
        self.assertEqual([], await server._prepare_desktop_shared({}, '火箭项目 保存', [], False))
        request.assert_not_awaited()
        self.assertEqual([], await server._prepare_desktop_shared({}, '火箭项目 保存', [], True))
        self.assertFalse(server._SHARED_TURN.get()['authorized'])
        self.assertIn('服务暂不可用', server._SHARED_TURN.get()['prompt'])
        self.assertIn('没有已授权', await server._shared_memory_action(action='save', content='不能落入本机库'))
        self.assertEqual(0, next(x for x in libraries.list_libraries(self.path) if x['id']==self.item['id'])['count'])


class DesktopProjectEndpointTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.DeferredConversationTests.asyncSetUp
    asyncTearDown = fixtures.DeferredConversationTests.asyncTearDown
    connect = fixtures.DeferredConversationTests.connect
    disconnect = fixtures.DeferredConversationTests.disconnect
    input = fixtures.DeferredConversationTests.input
    completed = fixtures.DeferredConversationTests.completed

    async def test_real_websocket_activates_shared_tools_before_running_agent(self):
        self.stack.enter_context(patch.object(server, '_SHARED_ROUTE_HOME', server.app_home()))
        seen = []
        context = {'authorized':True, 'library_id':'project', 'call_name':'火箭项目', 'ttl_minutes':30,
                   'revision':1, 'prompt':'项目章程与合同', 'user_text':'保存合同'}
        self.stack.enter_context(patch.object(server, '_desktop_project_request', new=AsyncMock(return_value={'ok':True,'context':context})))
        self.stack.enter_context(patch.object(server, '_SHARED_ROUTE_NAMES', ('火箭项目',)))
        class CaptureAgent:
            def __init__(self, *args, **kwargs):
                self.memory = kwargs['memory']
            async def run(self, text, **kwargs):
                seen.append((self.tools.names(), self.memory, self.runtime_context, kwargs['history']))
                yield SimpleNamespace(type='message_end', data={'content':'已保存共享资料'})
                yield SimpleNamespace(type='done', data={})
        self.stack.enter_context(patch.object(server, 'Agent', CaptureAgent))
        await self.connect()
        await self.input(text='火箭项目 保存合同')
        await self.completed()
        self.assertIn('shared_memory_action', seen[0][0])
        self.assertFalse(any(x.startswith(('memory_', 'project_')) for x in seen[0][0]))
        self.assertIsNone(seen[0][1])
        self.assertIn('项目章程', seen[0][2])
        messages = self.conversations['conv_draft']['messages']
        self.assertTrue(all(x['_shared_library']=='project' for x in messages))

    async def test_cloud_failure_still_runs_chat_without_private_memory_tools(self):
        self.stack.enter_context(patch.object(server, '_SHARED_ROUTE_HOME', server.app_home()))
        seen = []
        self.stack.enter_context(patch.object(server, '_desktop_project_request', new=AsyncMock(side_effect=RuntimeError('offline'))))
        self.stack.enter_context(patch.object(server, '_SHARED_ROUTE_NAMES', ('火箭项目',)))
        class CaptureAgent:
            def __init__(self, *args, **kwargs):
                self.memory = kwargs['memory']
            async def run(self, text, **kwargs):
                seen.append((self.tools.names(), self.memory, self.runtime_context))
                yield SimpleNamespace(type='message_end', data={'content':'共享服务连接失败，不能保存项目资料。'})
                yield SimpleNamespace(type='done', data={})
        self.stack.enter_context(patch.object(server, 'Agent', CaptureAgent))
        await self.connect()
        await self.input(text='火箭项目 保存合同')
        await self.completed()
        self.assertEqual(1, len(seen))
        self.assertFalse(any(x.startswith(('memory_', 'project_')) for x in seen[0][0]))
        self.assertIsNone(seen[0][1])
        self.assertIn('服务暂不可用', seen[0][2])
        self.assertEqual('denied', self.conversations['conv_draft']['messages'][-1]['_shared_library'])
