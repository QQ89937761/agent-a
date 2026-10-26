import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.core import update_manager as updater


class PortablePreparationTests(unittest.TestCase):
    def test_download_100_means_hash_verified(self):
        content = b'new-portable'
        response = io.BytesIO(content)
        response.headers = {'Content-Length': str(len(content))}
        progress = []
        with tempfile.TemporaryDirectory() as directory, patch.object(updater, '_open_update_url', return_value=response):
            updater.download_asset({'name': 'XiaoA.exe', 'url': 'unused', 'sha256': hashlib.sha256(content).hexdigest()}, directory, progress=lambda p, *_: progress.append(p))
        self.assertEqual(progress[-1], 100)
        self.assertTrue(all(p < 100 for p in progress[:-1]))

    def test_cancel_download_removes_partial_file(self):
        response = io.BytesIO(b'new')
        response.headers = {'Content-Length': '3'}
        def cancel(*_):
            raise updater.UpdateCancelled()
        with tempfile.TemporaryDirectory() as directory, patch.object(updater, '_open_update_url', return_value=response):
            with self.assertRaises(updater.UpdateCancelled):
                updater.download_asset({'name': 'XiaoA.exe', 'url': 'unused', 'sha256': 'a'*64}, directory, progress=cancel)
            self.assertFalse((Path(directory)/'XiaoA.exe').exists())

    def test_cancel_prepared_removes_only_update_workspace(self):
        directory = Path(tempfile.mkdtemp(prefix='xiaoa-update-'))
        config = directory/'update-config.json'
        config.write_text('{}', encoding='utf-8')
        (directory/'XiaoA.exe').write_bytes(b'new')
        updater.cancel_prepared_update(config)
        self.assertFalse(directory.exists())
        with self.assertRaises(updater.ReleaseError):
            updater.cancel_prepared_update(Path.cwd()/'update-config.json')

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell parser')
    def test_preparation_never_launches_and_script_parses_with_chinese_spaces(self):
        with tempfile.TemporaryDirectory(prefix='xiaoa-update-', suffix=' 中文 空格') as directory:
            base = Path(directory)
            target = base/'old.exe'; target.write_bytes(b'old')
            downloaded = base/'XiaoA.exe'; downloaded.write_bytes(b'new')
            with patch.object(updater.tempfile, 'mkdtemp', return_value=directory), patch.object(updater, 'portable_update_context', return_value=(base, downloaded)), patch.object(updater.subprocess, 'Popen') as launch:
                prepared = updater.prepare_portable_update({'version':'1.0.0', 'portable':{'sha256':hashlib.sha256(b'new').hexdigest()}}, executable=target, parent_pid=0, backend_pid=0)
            launch.assert_not_called()
            self.assertEqual(target.read_bytes(), b'old')
            helper = base/'install-update.ps1'
            self.assertTrue(helper.read_bytes().startswith(b'\xef\xbb\xbf'))
            code = "$t=$null;$e=$null;[System.Management.Automation.Language.Parser]::ParseFile('"+str(helper).replace("'", "''")+"',[ref]$t,[ref]$e)|Out-Null; if($e.Count){$e|ForEach-Object {$_.Message};exit 1}"
            result = subprocess.run(['powershell.exe','-NoProfile','-Command',code],capture_output=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stdout)
            downloaded.write_bytes(b'tampered')
            with self.assertRaisesRegex(updater.ReleaseError, 'SHA-256'):
                updater.restart_prepared_update(prepared['prepared_config'],executable=target,parent_pid=0,backend_pid=0)

    @unittest.skipUnless(os.name == 'nt', 'Windows updater')
    def test_restart_restores_launcher_temp_but_keeps_the_update_workspace(self):
        with tempfile.TemporaryDirectory(prefix='xiaoa-update-') as directory, \
                tempfile.TemporaryDirectory(prefix='xiaoa-launch-') as launch_temp:
            base = Path(directory)
            target = base / 'old.exe'; target.write_bytes(b'old')
            downloaded = base / 'XiaoA.exe'; downloaded.write_bytes(b'new')
            with patch.object(updater.tempfile, 'mkdtemp', return_value=directory), \
                    patch.object(updater, 'portable_update_context', return_value=(base, downloaded)):
                prepared = updater.prepare_portable_update(
                    {'version': '1.0.0', 'portable': {'sha256': hashlib.sha256(b'new').hexdigest()}},
                    executable=target, parent_pid=0, backend_pid=0,
                )
            def acknowledge(*args, **kwargs):
                (base / 'worker-ready').write_text('ready')
                return SimpleNamespace(poll=lambda: None)
            with patch.dict(os.environ, {'XIAOA_LAUNCH_TEMP': launch_temp, 'TEMP': directory, 'TMP': directory}), \
                    patch.object(updater.subprocess, 'Popen', side_effect=acknowledge) as launch:
                updater.restart_prepared_update(
                    prepared['prepared_config'], executable=target, parent_pid=0, backend_pid=0,
                )
                child_env = launch.call_args.kwargs['env']
                for name in ('TEMP', 'TMP', 'TMPDIR'):
                    self.assertEqual(Path(child_env[name]), Path(launch_temp))
                self.assertEqual(os.environ['TEMP'], directory, 'do not change backend task isolation')
            self.assertTrue((base / 'restart-approved').is_file())
            helper = (base / 'install-update.ps1').read_text(encoding='utf-8')
            launches = [line for line in helper.splitlines() if 'Start-Process -FilePath $Target' in line]
            self.assertEqual(len(launches), 5)
            self.assertTrue(all('-WindowStyle Hidden' in line for line in launches))

    @unittest.skipUnless(os.name == 'nt', 'Windows updater')
    def test_restart_rejects_arbitrary_config_directory(self):
        with self.assertRaisesRegex(updater.ReleaseError, '路径无效'):
            updater.restart_prepared_update(Path.cwd()/'update-config.json', executable='XiaoA.exe',parent_pid=0,backend_pid=0)

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell worker')
    def test_hidden_worker_acknowledges_without_closing_old_app(self):
        with tempfile.TemporaryDirectory(prefix='xiaoa-update-') as directory:
            base = Path(directory)
            target = base/'old.exe'; target.write_bytes(b'old')
            downloaded = base/'XiaoA.exe'; downloaded.write_bytes(b'new')
            with patch.object(updater.tempfile, 'mkdtemp', return_value=directory), patch.object(updater, 'portable_update_context', return_value=(base, downloaded)):
                prepared = updater.prepare_portable_update({'version':'1.0.0', 'portable':{'sha256':hashlib.sha256(b'new').hexdigest()}}, executable=target, parent_pid=0, backend_pid=0)
            value = json.loads(Path(prepared['prepared_config']).read_text(encoding='utf-8'))
            value['OldProcesses'] = []
            Path(prepared['prepared_config']).write_text(json.dumps(value), encoding='utf-8')
            process = subprocess.Popen(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(base/'install-update.ps1'), '-ConfigPath', prepared['prepared_config']], creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                import time
                deadline = time.monotonic()+15
                while time.monotonic()<deadline and not (base/'worker-ready').exists() and process.poll() is None:
                    time.sleep(.1)
                self.assertTrue((base/'worker-ready').exists(), 'PowerShell must acknowledge startup before any old process is closed')
                self.assertEqual(target.read_bytes(),b'old')
                self.assertIsNone(process.poll())
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)


class PortableUpdateApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from backend.api import server
        self.server = server
        self.directory = tempfile.TemporaryDirectory()
        self.state_path = Path(self.directory.name)/'state.json'
        self.saved_job = server._UPDATE_JOB.copy()
        self.saved_cancel = server._UPDATE_CANCEL.is_set()
        server._UPDATE_JOB.clear(); server._UPDATE_CANCEL.clear()
        self.patches = [patch.object(server,'_UPDATE_STATE_PATH',self.state_path)]
        for item in self.patches: item.start()
        self.request = SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'))

    async def asyncTearDown(self):
        for item in self.patches: item.stop()
        self.server._UPDATE_JOB.clear(); self.server._UPDATE_JOB.update(self.saved_job)
        if self.saved_cancel: self.server._UPDATE_CANCEL.set()
        else: self.server._UPDATE_CANCEL.clear()
        self.directory.cleanup()

    async def test_release_check_with_unconfigured_channel_returns_no_update(self):
        with patch.object(self.server, "_is_local", return_value=True), \
                patch.object(updater, "fetch_manifest", return_value=None):
            result = await self.server.api_update_release(self.request)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["latest"])
        self.assertFalse(result["update_available"])
        self.assertEqual(result["current_version"], self.server.__version__)

    async def test_cancel_legacy_staged_state_and_block_restart(self):
        self.server._UPDATE_JOB.update(stage='staged',percent=100)
        result = await self.server.api_update_restart(self.request)
        self.assertFalse(result['ok'])
        self.assertTrue((await self.server.api_update_cancel(self.request))['ok'])
        self.assertEqual(self.server._UPDATE_JOB['stage'],'cancelled')

    async def test_cancel_replacement_is_rejected(self):
        self.server._UPDATE_JOB.update(stage='replacing')
        self.assertFalse((await self.server.api_update_cancel(self.request))['ok'])
        self.assertFalse(self.server._UPDATE_CANCEL.is_set())

    async def test_restart_failure_restores_staged_for_retry_or_cancel(self):
        self.server._UPDATE_JOB.update(stage='staged',prepared_config='prepared')
        with patch.object(updater,'restart_prepared_update',side_effect=updater.ReleaseError('worker failed')):
            result = await self.server.api_update_restart(self.request)
        self.assertFalse(result['ok'])
        self.assertEqual(self.server._UPDATE_JOB['stage'],'staged')
        self.assertEqual(self.server._UPDATE_JOB['prepared_config'],'prepared')

    async def test_download_stages_without_restarting(self):
        manifest = {'version':'99.0.0'}
        with patch.object(updater,'fetch_manifest',return_value=manifest), patch.object(updater,'prepare_portable_update',return_value={'prepared_config':'prepared'}) as prepare, patch.object(updater,'restart_prepared_update') as restart, patch.dict(os.environ,{'XIAOA_EXECUTABLE_PATH':'XiaoA.exe','XIAOA_PARENT_PID':'123'}):
            await self.server._run_portable_update()
        self.assertEqual(self.server._UPDATE_JOB['stage'],'staged')
        self.assertEqual(self.server._UPDATE_JOB['percent'],100)
        self.assertEqual(self.server._UPDATE_JOB['prepared_config'],'prepared')
        prepare.assert_called_once(); restart.assert_not_called()

    async def test_cancel_while_checking_never_downloads(self):
        self.server._UPDATE_CANCEL.set()
        with patch.object(updater,'fetch_manifest',return_value={'version':'99.0.0'}), patch.object(updater,'prepare_portable_update') as prepare:
            await self.server._run_portable_update()
        self.assertEqual(self.server._UPDATE_JOB['stage'],'cancelled')
        prepare.assert_not_called()

    async def test_job_reads_external_worker_state(self):
        self.server._UPDATE_JOB.update(stage='restarting')
        updater.save_update_state(self.state_path,{'stage':'verifying','percent':100})
        result = await self.server.api_update_job(self.request)
        self.assertEqual(result['stage'],'verifying')

    async def test_remote_cannot_cancel_or_restart(self):
        from fastapi import HTTPException
        request = SimpleNamespace(client=SimpleNamespace(host='203.0.113.10'))
        for call in (self.server.api_update_cancel,self.server.api_update_restart):
            with self.assertRaises(HTTPException): await call(request)


if __name__ == '__main__':
    unittest.main()
