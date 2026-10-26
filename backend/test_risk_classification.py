"""风险分类回归：看实际动作，不把查询、路径和预览文字当成执行。"""
import json
import unittest
from unittest.mock import AsyncMock, patch

from backend.core.agent import (
    Agent, ConversationApproval, _is_read_only_command, _looks_dangerous,
    _looks_financial, _looks_irreversible, _looks_private, _remote_confirmation_categories,
)
from backend.core.config import AgentConfig
from backend.core.tools import Tool, ToolRegistry


class RiskClassificationTests(unittest.TestCase):
    def test_office_plain_text_is_not_a_system_or_financial_action(self):
        for name, args in (
            ('word_find_replace', {'path': 'password-format D:.docx', 'find': '删除', 'replace': '支付; format D:; shutdown /s; net user admin 123'}),
            ('excel_write_cells', {'path': '删除-支付.xlsx', 'sheet': 'password', 'cells': {'A1': 'format D:; 支付', 'B1': 12, 'C1:D2': [['shutdown /s', '删除'], ['password', None]]}}),
            ('excel_append_row', {'path': 'format D:.xlsx', 'values': ['删除; 支付; format D:', 42, None]}),
        ):
            with self.subTest(name=name):
                self.assertTrue(_looks_dangerous(name, args))
                self.assertFalse(_looks_financial(name, args))
                self.assertFalse(_looks_irreversible(name, args))
                self.assertEqual({'overwrite'}, _remote_confirmation_categories(name, args))

    def test_com_formula_inputs_keep_irreversible_detection(self):
        for value in ('=EXEC("format D:")', '+EXEC("format D:")', '-EXEC("format D:")', '@EXEC("format D:")', ' \t=EXEC("format D:")', "=cmd|'/C format D:'!A0"):
            for name, args in (
                ('excel_write_cells', {'cells': {'A1:B1': [['normal', value]]}}),
                ('excel_append_row', {'values': ['normal', value]}),
            ):
                with self.subTest(name=name, value=value):
                    self.assertTrue(_looks_irreversible(name, args))

    def test_read_only_commands_ignore_action_words_in_data(self):
        for command in (
            'cat /tmp/delete-format-payment.txt',
            'type C:/work/delete-format-payment.txt',
            'Get-Content C:/work/delete-format-payment.txt',
            "grep 'format C:' docs.txt", 'ps aux | grep rm',
            'find /tmp -name delete.log', 'git branch --list', 'git tag --list',
            'git diff --no-ext-diff', 'systemctl status shutdown.service',
        ):
            with self.subTest(command=command):
                args = {'command': command}
                self.assertTrue(_is_read_only_command('code_run', args))
                self.assertFalse(_looks_dangerous('code_run', args))
                self.assertFalse(_looks_irreversible('code_run', args))
                self.assertFalse(_looks_financial('code_run', args))

    def test_execution_and_write_options_are_not_read_only(self):
        for command in (
            'find /tmp -delete', 'find /tmp -exec rm {} \\;',
            'find /tmp -execdir sh script.sh {} +', 'find /tmp -fprint results.txt',
            'find /tmp -fprintf results.txt %p', 'find /tmp -fls results.txt',
            'git branch -D main', 'git branch new-branch', 'git tag -d v1', 'git tag v2',
            'git diff --output=patch.txt', 'git log --output=log.txt',
            'git diff --ext-diff', 'git show --textconv HEAD',
            'rg --pre=script.sh pattern .', 'journalctl --vacuum-time=1s',
            'command rm -rf /tmp/data', 'less --log-file=output.log input.txt',
            'ls\nrm -rf /tmp/data', 'ls;\nrm x', 'ls &&\nrm x', 'ls\n\nrm x',
            'echo a # comment & shutdown /s', 'echo %COMMAND%',
            'echo !COMMAND!', 'echo $COMMAND', 'cat a.txt > b.txt',
            'docker exec app unknown -c ls', 'docker exec app python -c ls',
            '/tmp/ls /tmp/data', './git status', 'sudo ./ls',
            "bash --rcfile script.sh -c 'ls'", "powershell -File script.ps1 -c 'ls'",
            'rg --hostname-bin=script.sh pattern .',
            'cat $(rm -rf /tmp/data)', 'git status && shutdown /s',
        ):
            with self.subTest(command=command):
                self.assertFalse(_is_read_only_command('code_run', {'command': command}))

    def test_command_scope_does_not_treat_working_directory_as_action(self):
        args = {'command': 'python build.py', 'cwd': 'D:/删除-支付-format'}
        self.assertFalse(_looks_dangerous('code_run', args))
        self.assertFalse(_looks_financial('code_run', args))
        self.assertFalse(_looks_irreversible('code_run', args))

    def test_same_read_only_semantics_for_long_and_verification_commands(self):
        for name in ('run_command', 'code_run', 'cloud_exec', 'code_verify', 'code_process_start'):
            with self.subTest(name=name):
                self.assertTrue(_is_read_only_command(name, {'command': 'cat delete-format-payment.txt'}))
                self.assertFalse(_looks_dangerous(name, {'command': 'cat delete-format-payment.txt'}))

    def test_cad_preview_is_not_a_destructive_or_financial_action(self):
        args = {'path': 'D:/work/a.dwg', 'replacements': [{'old': '删除', 'new': '支付-格式化'}]}
        for preview in ({}, {'dry_run': True}):
            with self.subTest(preview=preview):
                params = {**args, **preview}
                self.assertFalse(_looks_dangerous('cad_replace_text', params))
                self.assertFalse(_looks_irreversible('cad_replace_text', params))
                self.assertFalse(_looks_financial('cad_replace_text', params))
        self.assertTrue(_looks_dangerous('cad_replace_text', {**args, 'dry_run': False}))
        self.assertFalse(_looks_irreversible('cad_replace_text', {**args, 'dry_run': False}))
        self.assertFalse(_looks_financial('cad_replace_text', {**args, 'dry_run': False}))
        self.assertEqual({'overwrite'}, _remote_confirmation_categories('cad_replace_text', {**args, 'dry_run': False}))

    def test_unknown_tools_cannot_claim_preview_to_bypass_risk(self):
        args = {'command': 'rm -rf /tmp/data', 'dry_run': True}
        self.assertTrue(_looks_dangerous('run_command', args))
        self.assertTrue(_looks_dangerous('unknown_tool', args))

    def test_real_high_risk_actions_and_remote_credential_reads_stay_protected(self):
        for command in ('format D:', 'shutdown /s', 'net user user new-pass', 'reg delete HKLM/SOFTWARE'):
            with self.subTest(command=command):
                self.assertTrue(_looks_dangerous('run_command', {'command': command}))
                self.assertTrue(_looks_irreversible('run_command', {'command': command}))
        self.assertTrue(_looks_financial('click_element', {'target_desc': '确认支付'}))
        self.assertEqual({'password'}, _remote_confirmation_categories('cloud_exec', {'command': 'cat password.txt'}))


    def test_literal_file_content_is_not_executed_or_paid(self):
        for name, args in (
            ('fs_write_text', {'path': '格式化-支付.txt', 'content': 'format D:; net user; 支付'}),
            ('code_write', {'path': 'payment.py', 'content': 'shutdown /s'}),
            ('code_edit', {'path': '删除.py', 'old_string': '格式化', 'new_string': '支付'}),
            ('code_apply_edits', {'root': 'password', 'edits': [
                {'path': 'format.py', 'old_string': 'a', 'new_string': 'format D:; 支付'},
            ]}),
            ('create_word_doc', {'path': '格式化.docx', 'blocks': [
                {'type': 'table', 'headers': ['密码'], 'rows': [['支付', 'shutdown /s']]},
            ]}),
        ):
            with self.subTest(name=name):
                self.assertFalse(_looks_irreversible(name, args))
                self.assertFalse(_looks_financial(name, args))
                if name == 'create_word_doc':
                    self.assertFalse(_looks_dangerous(name, args))
                    self.assertEqual(set(), _remote_confirmation_categories(name, args))
                else:
                    self.assertTrue(_looks_dangerous(name, args))
                    self.assertEqual({'overwrite'}, _remote_confirmation_categories(name, args))

    def test_attachment_names_are_not_shell_or_financial_actions(self):
        for name in ('browser_upload', 'send_file_to_chat', 'send_image_to_chat', 'cloud_upload'):
            args = {'path': '格式化-支付-password.txt', 'local_path': '格式化-支付-password.txt',
                    'remote_path': '/tmp/delete-payment-password.txt'}
            with self.subTest(name=name):
                self.assertFalse(_looks_dangerous(name, args))
                self.assertFalse(_looks_irreversible(name, args))
                self.assertFalse(_looks_financial(name, args))
                self.assertFalse(_looks_private(name, args))
                self.assertEqual({'overwrite'} if name == 'cloud_upload' else set(),
                                 _remote_confirmation_categories(name, args))

    def test_sensitive_transfers_include_cloud_source_not_remote_filename(self):
        for name in ('browser_upload', 'send_file_to_chat', 'send_image_to_chat', 'cloud_upload'):
            for filename in ('.env', '.env.local', 'id_rsa', 'id_ed25519', 'config.toml',
                             'known_hosts', 'ui_token.txt', 'secret.pem', 'private.key', 'cookies.json'):
                with self.subTest(name=name, filename=filename):
                    key = 'local_path' if name == 'cloud_upload' else 'path'
                    self.assertTrue(_looks_private(name, {key: 'D:/work/' + filename}))
        self.assertFalse(_looks_private('cloud_upload', {
            'local_path': 'D:/work/report.txt', 'remote_path': '/tmp/.env',
        }))

    def test_unknown_and_interactive_tools_keep_nested_execution_detection(self):
        args = {'payload': {'items': ['format D:; 支付; Invoke-Expression $code']}}
        for name in ('unknown_tool', 'tool_create', 'browser_type', 'send_text_to_window'):
            with self.subTest(name=name):
                self.assertTrue(_looks_dangerous(name, args))
                self.assertTrue(_looks_irreversible(name, args))
                self.assertTrue(_looks_financial(name, args))


class NewExcelFormulaClassificationTests(unittest.TestCase):
    def test_data_and_local_calculations_are_not_execution(self):
        for value in (
            '支付; format D:; password', '+cmd| /c calc', '@WEBSERVICE("x")',
            '=SUM(A1:A2)', '=IF(A1="支付",1,0)', '=PMT(0.01,12,100)',
            '=SUM(Table1[[#Headers],[支付]])', '=SUM(Table1[删除!支付|格式化])', "='中文表'!A1", '=1+2',
            '="cmd| /c format D:; 支付"', '=IF(TRUE,"[book.xlsx]Sheet!A1",0)',
        ):
            with self.subTest(value=value):
                args = {'path': '格式化-支付-password.xlsx', 'rows': [[value]]}
                self.assertFalse(_looks_dangerous('create_excel_file', args))
                self.assertFalse(_looks_financial('create_excel_file', args))
                self.assertFalse(_looks_irreversible('create_excel_file', args))
                self.assertEqual(set(), _remote_confirmation_categories('create_excel_file', args))

    def test_invalid_arrays_are_not_misreported_as_executable_formulas(self):
        for args in ({'headers': '=WEBSERVICE("x")'}, {'rows': ['=SUM(A1)']},
                     {'rows': '=EXEC("cmd")'}, {'headers': {}}):
            with self.subTest(args=args):
                self.assertFalse(_looks_dangerous('create_excel_file', args))
                self.assertEqual(set(), _remote_confirmation_categories('create_excel_file', args))

    def test_external_unknown_and_unparseable_formulas_require_authorization(self):
        for value in (
            '=WEBSERVICE("https://example.test")', '=RTD("prog",,"topic")',
            '=HYPERLINK("https://example.test","查看")', '=INDIRECT(A1)',
            '=CUBEVALUE("connection", "measure")', '=MY_UDF(A1)',
            '=CALL("dll","func","J")', '=EXEC("cmd /c calc")', '=PY("print(1)")',
            '=_xlfn.IMAGE("https://example.test")', "='[book.xlsx]Sheet'!A1",
            "=cmd|'/C calc'!A0", '=SUM((A1:A2)', '=SUM("unclosed)',
            '=book.xlsx!SomeName', "='https://example.test/book.xlsx'!A1",
            "='book.xlsm'!SomeName", '=external!SomeName',
        ):
            for key in ('headers', 'rows'):
                with self.subTest(value=value, key=key):
                    args = {key: [value] if key == 'headers' else [[value]]}
                    self.assertTrue(_looks_dangerous('create_excel_file', args))
                    self.assertFalse(_looks_financial('create_excel_file', args))
                    self.assertFalse(_looks_irreversible('create_excel_file', args))
                    self.assertEqual({'external_code'},
                                     _remote_confirmation_categories('create_excel_file', args))

    def test_only_executable_formulas_keep_irreversible_command_detection(self):
        for value in ('=EXEC("format D:")', "=cmd|'/C format D:'!A0",
                      '=CALL("kernel32","WinExec","JC","format D:")'):
            with self.subTest(value=value):
                self.assertTrue(_looks_irreversible('create_excel_file', {'rows': [[value]]}))
        for value in ('=WEBSERVICE("https://example.test/?q=format D:")',
                      '=MY_UDF("format D:")', '=IF(TRUE,"EXEC format D:",0)'):
            with self.subTest(value=value):
                self.assertFalse(_looks_irreversible('create_excel_file', {'rows': [[value]]}))


class OneCallLLM:
    def __init__(self, name, args):
        self.name, self.args, self.turn = name, args, 0

    async def chat_stream_tools(self, *_args, **_kwargs):
        self.turn += 1
        if self.turn == 1:
            yield 'done', {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'risk-call', 'type': 'function', 'function': {
                    'name': self.name, 'arguments': json.dumps(self.args, ensure_ascii=False),
                },
            }]}
        else:
            yield 'done', {'role': 'assistant', 'content': '完成'}


class RiskExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def run_action(self, name, args, *, mode='risk', user='检查指定文件', side_effect=True,
                         danger=True, state=None, confirm_result=False):
        handler = AsyncMock(return_value='done')
        confirm = AsyncMock(return_value=confirm_result)
        registry = ToolRegistry()
        registry.register(Tool(name, name, {'type': 'object'}, handler,
                               side_effect=side_effect, danger=danger))
        events = [event async for event in Agent(
            OneCallLLM(name, args), registry, AgentConfig(confirm_before_action=True),
            confirm_fn=confirm, approval_state=state,
        ).run(user, approval_mode=mode)]
        return handler, confirm, events

    async def test_wps_type_text_does_not_treat_document_words_as_payment(self):
        handler = AsyncMock(return_value={"ok": True})
        handler.__module__ = "backend.modules.computer.computer_module"
        confirm = AsyncMock(return_value=False)
        registry = ToolRegistry()
        registry.register(Tool("type_text", "type_text", {"type": "object"}, handler,
                               side_effect=True, danger=False))
        text = bytes("\\u6211\\u7684\\u81ea\\u6211\\u4ecb\\u7ecd\\u5305\\u542b\\u4ed8\\u6b3e\\u548c\\u652f\\u4ed8\\u7b49\\u666e\\u901a\\u6587\\u5b57", "ascii").decode("unicode_escape")
        args = {"text": text}
        with patch("backend.modules.computer.computer_module._foreground_window_handle",
                   return_value=96), patch(
                       "backend.modules.computer.computer_module.is_document_text_input",
                       return_value=True,
                   ):
            events = [event async for event in Agent(
                OneCallLLM("type_text", args), registry, AgentConfig(confirm_before_action=True),
                confirm_fn=confirm,
            ).run("WPS document task", approval_mode="risk")]
        handler.assert_awaited_once()
        confirm.assert_not_awaited()
        self.assertFalse(any(event.type == "confirm_needed" for event in events))
        self.assertEqual(96, handler.await_args.kwargs["handle"])

    async def test_legacy_office_reads_always_require_approval(self):
        for mode in (None, 'risk', 'full', 'safe', 'remote', 'scheduled'):
            for name, path in (('excel_read_range', 'report.xls'), ('excel_read_all', 'report.ods'),
                               ('word_read_text', 'report.doc')):
                with self.subTest(name=name, mode=mode):
                    handler, confirm, events = await self.run_action(
                        name, {'path': path}, mode=mode, side_effect=False, danger=False,
                    )
                    handler.assert_not_awaited()
                    confirm.assert_awaited_once()
                    self.assertEqual('external_code', next(e.data['reason'] for e in events if e.type == 'confirm_needed'))

    async def test_common_office_reads_never_consume_approval(self):
        for mode in (None, 'risk', 'full', 'safe', 'remote', 'scheduled'):
            for name, path in (('excel_read_range', '支付-format D:.xlsx'),
                               ('excel_read_all', 'password.xlsm'), ('word_read_text', '删除.docm')):
                with self.subTest(name=name, mode=mode):
                    state = ConversationApproval()
                    handler, confirm, _ = await self.run_action(
                        name, {'path': path}, mode=mode, state=state, user='检查支付说明',
                    )
                    handler.assert_awaited_once()
                    confirm.assert_not_awaited()
                    self.assertFalse(state.requested)

    async def test_legacy_office_approval_reused_for_reads_and_writes(self):
        for mode in ('full', 'safe', 'remote'):
            state = ConversationApproval()
            for index, (name, path) in enumerate((
                ('excel_read_all', 'a.xls'), ('word_read_text', 'a.doc'), ('excel_write_cells', 'a.xlsx'),
            )):
                with self.subTest(mode=mode, name=name):
                    handler, confirm, _ = await self.run_action(
                        name, {'path': path}, mode=mode, state=state, confirm_result=True,
                        side_effect=False, danger=False,
                    )
                    handler.assert_awaited_once()
                    if index == 0:
                        confirm.assert_awaited_once()
                    else:
                        confirm.assert_not_awaited()

    async def test_rejected_legacy_approval_keeps_safe_reads_available(self):
        for mode in ('full', 'safe', 'remote'):
            state = ConversationApproval()
            for index, (name, path) in enumerate((('excel_read_all', 'a.xls'), ('word_read_text', 'a.doc'))):
                handler, confirm, _ = await self.run_action(
                    name, {'path': path}, mode=mode, state=state, side_effect=False, danger=False,
                )
                handler.assert_not_awaited()
                if index == 0:
                    confirm.assert_awaited_once()
                else:
                    confirm.assert_not_awaited()
            handler, confirm, _ = await self.run_action(
                'word_read_text', {'path': 'safe.docx'}, mode=mode, state=state,
                side_effect=False, danger=False,
            )
            handler.assert_awaited_once()
            confirm.assert_not_awaited()

    async def test_office_writes_ask_for_overwrite_not_external_execution(self):
        for name, args in (
            ('word_find_replace', {'path': '说明.docx', 'find': '密码', 'replace': '支付; format D:'}),
            ('excel_write_cells', {'path': '说明.xlsx', 'cells': {'A1': '支付; format D:'}}),
            ('excel_append_row', {'path': '说明.xlsx', 'values': ['支付; format D:']}),
        ):
            for mode in (None, 'risk', 'remote', 'full', 'safe'):
                with self.subTest(name=name, mode=mode):
                    handler, confirm, events = await self.run_action(name, args, mode=mode, user='修改支付说明')
                    if mode in {'full', 'safe'}:
                        handler.assert_awaited_once()
                        confirm.assert_not_awaited()
                    else:
                        handler.assert_not_awaited()
                        confirm.assert_awaited_once()
                        self.assertEqual('overwrite' if mode == 'remote' else 'risk',
                                         next(e.data['reason'] for e in events if e.type == 'confirm_needed'))

    async def test_com_metadata_cannot_bypass_file_execution_approval(self):
        for name in ('word_find_replace', 'excel_write_cells', 'excel_append_row'):
            for mode in ('full', 'safe', 'remote'):
                with self.subTest(name=name, mode=mode):
                    handler, confirm, _ = await self.run_action(name, {}, mode=mode, side_effect=False, danger=False)
                    handler.assert_not_awaited()
                    confirm.assert_awaited_once()

    async def test_com_approval_reused_across_word_and_excel(self):
        state = ConversationApproval()
        for index, name in enumerate(('word_find_replace', 'excel_append_row', 'excel_write_cells')):
            handler, confirm, _ = await self.run_action(name, {}, mode='full', state=state, confirm_result=True)
            handler.assert_awaited_once()
            if index == 0:
                confirm.assert_awaited_once()
            else:
                confirm.assert_not_awaited()

    async def test_rejected_com_approval_does_not_reprompt_or_block_plain_data_creation(self):
        state = ConversationApproval()
        for index, name in enumerate(('word_find_replace', 'excel_append_row', 'excel_write_cells')):
            handler, confirm, _ = await self.run_action(name, {}, mode='remote', state=state)
            handler.assert_not_awaited()
            if index == 0:
                confirm.assert_awaited_once()
            else:
                confirm.assert_not_awaited()
        handler, confirm, _ = await self.run_action('create_excel_file', {'rows': [['支付; format D:']]},
                                                  mode='remote', state=state, danger=False)
        handler.assert_awaited_once()
        confirm.assert_not_awaited()

    async def test_approved_com_operation_preserves_remote_irreversible_hard_block(self):
        state = ConversationApproval(requested=True, allowed=True)
        for name, args in (
            ('excel_write_cells', {'cells': {'A1': '=EXEC("format D:")'}}),
            ('excel_append_row', {'values': ["=cmd|'/C format D:'!A0"]}),
        ):
            handler, confirm, events = await self.run_action(name, args, mode=None, state=state)
            handler.assert_not_awaited()
            confirm.assert_not_awaited()
            self.assertTrue(any('已拦下' in e.data.get('result', '') for e in events if e.type == 'tool_result'))

    async def test_read_only_danger_tool_runs_without_consuming_approval(self):
        for mode in ('risk', 'scheduled', None):
            with self.subTest(mode=mode):
                state = ConversationApproval()
                handler, confirm, events = await self.run_action(
                    'code_run', {'command': 'cat delete-format-payment.txt'}, mode=mode, state=state,
                )
                handler.assert_awaited_once()
                confirm.assert_not_awaited()
                self.assertFalse(state.requested)
                self.assertFalse(any(e.type == 'confirm_needed' for e in events))

    async def test_payment_context_does_not_block_read_only_or_cad_preview(self):
        for name, args in (
            ('code_run', {'command': 'cat bill.txt'}),
            ('cad_replace_text', {'path': 'a.dwg', 'replacements': [{'old': '支付', 'new': '付款'}]}),
        ):
            with self.subTest(name=name):
                handler, confirm, _ = await self.run_action(name, args, user='支付前先核对账单', mode='full')
                handler.assert_awaited_once()
                confirm.assert_not_awaited()

    async def test_registered_query_does_not_inherit_risk_from_search_terms(self):
        for mode in (None, 'risk', 'remote'):
            with self.subTest(mode=mode):
                handler, confirm, _ = await self.run_action(
                    'code_search', {'pattern': '删除-格式化-支付', 'directory': 'D:/work'},
                    side_effect=False, danger=False, mode=mode,
                )
                handler.assert_awaited_once()
                confirm.assert_not_awaited()

    async def test_real_cad_write_requires_confirmation_even_with_harmless_text(self):
        handler, confirm, _ = await self.run_action(
            'cad_replace_text', {'path': 'a.dwg', 'replacements': [{'old': 'a', 'new': 'b'}], 'dry_run': False},
            danger=False,
        )
        handler.assert_not_awaited()
        confirm.assert_awaited_once()

    async def test_invalid_preview_values_are_not_treated_as_read_only(self):
        for value in (False, 0, None, 'false'):
            with self.subTest(value=value):
                handler, confirm, _ = await self.run_action('cad_replace_text', {'dry_run': value}, danger=False)
                handler.assert_not_awaited()
                confirm.assert_awaited_once()

    async def test_cad_replacement_text_is_data_not_disk_or_payment_operation(self):
        handler, confirm, events = await self.run_action(
            'cad_replace_text', {'dry_run': False, 'replacements': [{'old': '格式化', 'new': '支付'}]},
            mode=None, user='修改图纸中的支付说明', danger=False,
        )
        handler.assert_not_awaited()
        confirm.assert_awaited_once()
        self.assertEqual('risk', next(e.data['reason'] for e in events if e.type == 'confirm_needed'))

    async def test_command_tool_cannot_skip_hard_block_with_missing_metadata(self):
        handler, confirm, _ = await self.run_action(
            'run_command', {'command': 'format D:'}, mode=None, side_effect=False, danger=False,
        )
        handler.assert_not_awaited()
        confirm.assert_not_awaited()

    async def test_denied_conversation_can_still_run_queries(self):
        state = ConversationApproval(requested=True, allowed=False)
        handler, confirm, _ = await self.run_action('code_run', {'command': 'git status'}, state=state)
        handler.assert_awaited_once()
        confirm.assert_not_awaited()
        handler, confirm, _ = await self.run_action('code_run', {'command': 'rm -rf /tmp/data'}, state=state)
        handler.assert_not_awaited()
        confirm.assert_not_awaited()

    async def test_query_exemption_cannot_skip_external_code_or_sensitive_upload(self):
        for name, args, reason in (
            ('extension_inspect', {}, 'external_code'),
            ('browser_upload', {'path': 'D:/work/.env'}, 'privacy'),
            ('cloud_exec', {'command': 'cat password.txt'}, 'password'),
        ):
            with self.subTest(name=name):
                handler, confirm, events = await self.run_action(name, args, side_effect=False, danger=False)
                handler.assert_not_awaited()
                confirm.assert_awaited_once()
                self.assertEqual(reason, next(e.data['reason'] for e in events if e.type == 'confirm_needed'))

    async def test_mutating_find_and_git_options_require_confirmation(self):
        for command in ('find /tmp -delete', 'git branch -D main', 'git diff --output=patch.txt'):
            with self.subTest(command=command):
                handler, confirm, _ = await self.run_action('code_run', {'command': command})
                handler.assert_not_awaited()
                confirm.assert_awaited_once()


    async def test_literal_writes_are_overwrite_not_format_or_payment(self):
        for name, args in (
            ('fs_write_text', {'path': '说明.txt', 'content': 'format D:; 支付'}),
            ('code_write', {'path': '说明.py', 'content': 'shutdown /s; 支付'}),
            ('code_edit', {'path': '说明.py', 'old_string': '格式化', 'new_string': '支付'}),
            ('code_apply_edits', {'root': 'project', 'edits': [
                {'path': '说明.txt', 'old_string': 'a', 'new_string': 'format D:; 支付'},
            ]}),
        ):
            for mode in (None, 'risk', 'remote'):
                with self.subTest(name=name, mode=mode):
                    handler, confirm, events = await self.run_action(
                        name, args, mode=mode, danger=False, user='更新支付说明文档',
                    )
                    handler.assert_not_awaited()
                    confirm.assert_awaited_once()
                    reason = next(e.data['reason'] for e in events if e.type == 'confirm_needed')
                    self.assertEqual('overwrite' if mode == 'remote' else 'risk', reason)
            handler, confirm, _ = await self.run_action(
                name, args, mode='full', danger=False, user='更新支付说明文档',
            )
            handler.assert_awaited_once()
            confirm.assert_not_awaited()

    async def test_write_metadata_cannot_turn_overwrites_into_queries(self):
        for name in ('fs_write_text', 'code_write', 'code_edit', 'code_apply_edits'):
            with self.subTest(name=name):
                handler, confirm, _ = await self.run_action(
                    name, {}, mode='remote', side_effect=False, danger=False,
                )
                handler.assert_not_awaited()
                confirm.assert_awaited_once()

    async def test_new_word_doc_does_not_consume_conversation_approval(self):
        for mode in (None, 'risk', 'remote', 'full'):
            with self.subTest(mode=mode):
                state = ConversationApproval()
                handler, confirm, events = await self.run_action(
                    'create_word_doc', {'path': '格式化.docx', 'paragraphs': ['支付; shutdown /s']},
                    mode=mode, danger=False, user='写一份支付和格式化操作的说明', state=state,
                )
                handler.assert_awaited_once()
                confirm.assert_not_awaited()
                self.assertFalse(state.requested)
                self.assertFalse(any(e.type == 'confirm_needed' for e in events))

    async def test_normal_attachments_do_not_inherit_payment_context(self):
        for name in ('browser_upload', 'send_file_to_chat', 'send_image_to_chat'):
            for mode in (None, 'risk', 'remote', 'full'):
                with self.subTest(name=name, mode=mode):
                    handler, confirm, _ = await self.run_action(
                        name, {'path': '格式化-支付-password.txt'}, mode=mode, danger=False,
                        user='把支付说明文件发给我',
                    )
                    handler.assert_awaited_once()
                    confirm.assert_not_awaited()

    async def test_sensitive_upload_keeps_privacy_approval_in_full_mode(self):
        for name in ('browser_upload', 'send_file_to_chat', 'send_image_to_chat', 'cloud_upload'):
            with self.subTest(name=name):
                key = 'local_path' if name == 'cloud_upload' else 'path'
                handler, confirm, events = await self.run_action(
                    name, {key: 'D:/work/.env', 'remote_path': '/tmp/report.txt'},
                    mode='full', danger=False, user='发送配置文件',
                )
                handler.assert_not_awaited()
                confirm.assert_awaited_once()
                self.assertEqual('privacy', next(e.data['reason'] for e in events if e.type == 'confirm_needed'))

    async def test_cloud_upload_keeps_overwrite_approval_without_payment_false_positive(self):
        handler, confirm, events = await self.run_action(
            'cloud_upload', {'local_path': '格式化-支付.txt', 'remote_path': '/tmp/password.txt'},
            mode='full', danger=False, user='上传支付说明',
        )
        handler.assert_not_awaited()
        confirm.assert_awaited_once()
        self.assertEqual('overwrite', next(e.data['reason'] for e in events if e.type == 'confirm_needed'))

    async def test_literal_tools_still_share_once_per_conversation_decision(self):
        for allowed in (True, False):
            with self.subTest(allowed=allowed):
                state = ConversationApproval(requested=True, allowed=allowed)
                for name, args in (
                    ('code_edit', {'path': 'doc.txt', 'old_string': 'a', 'new_string': 'format D:'}),
                    ('cloud_upload', {'local_path': 'D:/work/.env', 'remote_path': '/tmp/report'}),
                    ('fs_write_text', {'path': 'report.txt', 'content': '支付'}),
                ):
                    handler, confirm, events = await self.run_action(name, args, state=state, danger=False)
                    self.assertEqual(int(allowed), handler.await_count)
                    confirm.assert_not_awaited()
                    self.assertFalse(any(e.type == 'confirm_needed' for e in events))
                handler, confirm, _ = await self.run_action(
                    'send_file_to_chat', {'path': '支付说明.txt'}, state=state, danger=False,
                )
                handler.assert_awaited_once()
                confirm.assert_not_awaited()

    async def test_window_text_and_real_payment_click_are_not_literal_data_exemptions(self):
        for name, args in (
            ('send_text_to_window', {'window': 'terminal', 'text': 'format D:'}),
            ('browser_type', {'ref': 'terminal-input', 'text': 'format D:'}),
        ):
            with self.subTest(name=name):
                handler, confirm, _ = await self.run_action(name, args, mode=None, danger=False)
                handler.assert_not_awaited()
                confirm.assert_not_awaited()
        handler, confirm, events = await self.run_action(
            'click_element', {'target_desc': '确认支付'}, mode='full', danger=False,
        )
        handler.assert_not_awaited()
        confirm.assert_awaited_once()
        self.assertEqual('financial', next(e.data['reason'] for e in events if e.type == 'confirm_needed'))

    async def test_new_excel_data_and_calculations_do_not_consume_approval(self):
        for mode in ('risk', 'full', 'remote', None):
            with self.subTest(mode=mode):
                state = ConversationApproval()
                handler, confirm, _ = await self.run_action(
                    'create_excel_file', {'path': '格式化.xlsx', 'headers': ['支付'],
                                          'rows': [['=IF(A1="支付",1,0)', 'password']]},
                    mode=mode, danger=False, user='制作支付计算表', state=state,
                )
                handler.assert_awaited_once()
                confirm.assert_not_awaited()
                self.assertFalse(state.requested)

    async def test_external_excel_formula_is_hard_authorization_in_all_modes(self):
        for mode in ('risk', 'full', 'safe', 'remote', None):
            for side_effect in (True, False):
                with self.subTest(mode=mode, side_effect=side_effect):
                    handler, confirm, events = await self.run_action(
                        'create_excel_file', {'rows': [['=WEBSERVICE("https://example.test")']]},
                        mode=mode, danger=False, side_effect=side_effect,
                    )
                    handler.assert_not_awaited()
                    confirm.assert_awaited_once()
                    self.assertEqual('external_code', next(
                        e.data['reason'] for e in events if e.type == 'confirm_needed'))

    async def test_excel_formulas_reuse_approved_and_denied_conversation_decisions(self):
        for allowed in (True, False):
            state = ConversationApproval()
            for index, formula in enumerate(('=RTD("prog",,"topic")', '=MY_UDF(A1)')):
                with self.subTest(allowed=allowed, formula=formula):
                    handler, confirm, _ = await self.run_action(
                        'create_excel_file', {'rows': [[formula]]},
                        mode='full', danger=False, state=state, confirm_result=allowed,
                    )
                    self.assertEqual(int(allowed), handler.await_count)
                    self.assertEqual(int(index == 0), confirm.await_count)
                    self.assertTrue(state.requested)
                    self.assertEqual(allowed, state.allowed)
            handler, confirm, _ = await self.run_action(
                'create_excel_file', {'rows': [['=SUM(A1:A2)', '支付说明']]},
                danger=False, state=state,
            )
            handler.assert_awaited_once()
            confirm.assert_not_awaited()

    async def test_remote_irreversible_excel_commands_keep_hard_block_even_if_authorized(self):
        for value in ('=EXEC("format D:")', "=cmd|'/C format D:'!A0"):
            with self.subTest(value=value):
                handler, confirm, events = await self.run_action(
                    'create_excel_file', {'rows': [[value]]}, mode=None, danger=False,
                    state=ConversationApproval(requested=True, allowed=True),
                )
                handler.assert_not_awaited()
                confirm.assert_not_awaited()
                self.assertTrue(any(e.type == 'tool_result' and '不可逆' in e.data['result']
                                    for e in events))


if __name__ == '__main__':
    unittest.main()
