"""Pure-stdlib offline Linux/Windows transaction tests; never connect to a server."""
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from deploy import cloud_transaction as tx

class TransactionTests(unittest.TestCase):
    def test_service_keeps_config_replaceable_without_widening_sandbox(self):
        script = (Path(__file__).resolve().parent.parent / "deploy/deploy_hub.sh").read_text(encoding="utf-8")
        unit = script.split('cat >"$SERVICE" <<EOF', 1)[1].split("\nEOF", 1)[0]
        self.assertIn("ProtectSystem=full", unit)
        self.assertIn("ProtectHome=true", unit)
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("ReadWritePaths=$APP_DIR/data\n", unit)
        for line in unit.splitlines():
            if line.startswith(("ReadWritePaths=", "BindPaths=", "BindReadOnlyPaths=")):
                self.assertNotIn("config.toml", line, "a file mount prevents atomic config replacement")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        root = base / 'xiaoa'
        root.mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for key, value in [('ROOT', root), ('BACKUP', root / '.deploy-backup'),
                           ('ENV', base / 'hub.env'), ('UNIT', base / 'hub.service')]:
            self.assertTrue(value.resolve().is_relative_to(base))
            self.stack.enter_context(patch.object(tx, key, value))
        self.stack.enter_context(patch.object(tx, 'gateway_paths', return_value=[
            root / 'data/public-tls.json', base / 'xiaoa-ip-http.conf', base / 'xiaoa-ip-https.conf',
            base / 'xiaoa-ip-cert-renew.service', base / 'xiaoa-ip-cert-renew.timer', base / 'xiaoa-ip-firewall.service']))
        self.commands = []
        self.stack.enter_context(patch.object(tx, 'run', side_effect=self.fake_run))
        self.stack.enter_context(patch.object(tx, 'health', return_value={'healthy': True, 'version': '0.9.0'}))
        for name in ('backend', 'deploy', 'frontend', '.venv/bin', 'data/hub-tls'):
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / 'backend/module.py').write_text('old code', encoding='utf-8')
        (root / 'backend/requirements-hub.txt').write_text('old dependency', encoding='utf-8')
        (root / '.venv/bin/python').write_text('old runtime', encoding='utf-8')
        (root / 'config.example.toml').write_text('old example', encoding='utf-8')
        (root / 'config.toml').write_text('private config', encoding='utf-8')
        (root / 'data/memory.json').write_text('precious memory', encoding='utf-8')
        tx.ENV.write_text('private env', encoding='utf-8')
        tx.UNIT.write_text(f'WorkingDirectory={tx.ROOT}\nExecStart={tx.ROOT}/.venv/bin/python -m backend.api.server\n', encoding='utf-8')
        self.archive = base / 'payload.tar.gz'
        staged = base / 'payload'
        (staged / 'backend').mkdir(parents=True)
        (staged / 'backend/module.py').write_text('new code', encoding='utf-8')
        (staged / 'backend/requirements-hub.txt').write_text('new dependency', encoding='utf-8')
        (staged / 'deploy').mkdir()
        (staged / 'deploy/deploy_hub.sh').write_text('# shell', encoding='utf-8')
        (staged / 'frontend/src').mkdir(parents=True)
        (staged / 'frontend/src/settings.html').write_text('page', encoding='utf-8')
        (staged / 'config.example.toml').write_text('new example', encoding='utf-8')
        with tarfile.open(self.archive, 'w:gz') as handle:
            for name in tx.CODE:
                handle.add(staged / name, arcname=name)
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def fake_run(self, args, **_kwargs):
        self.commands.append(args)
        return SimpleNamespace(returncode=0, stdout='')

    def test_failed_install_restores_code_config_unit_runtime_and_keeps_data(self):
        def failing(args, **kwargs):
            if args[0] == 'bash':
                (tx.ROOT / 'data/connection-string.txt').write_text('temporary secret', encoding='utf-8')
                tx.ENV.write_text('changed env', encoding='utf-8')
                (tx.ROOT / 'config.toml').write_text('changed config', encoding='utf-8')
                (tx.ROOT / '.venv/bin').mkdir(parents=True)
                (tx.ROOT / '.venv/bin/python').write_text('broken runtime', encoding='utf-8')
                raise RuntimeError('dependency failure')
            return self.fake_run(args, **kwargs)
        with patch.object(tx, 'run', side_effect=failing):
            with self.assertRaisesRegex(RuntimeError, 'dependency failure'):
                tx.install(self.archive, self.digest, '203.0.113.10', 'repair', {})
        self.assertEqual('old code', (tx.ROOT / 'backend/module.py').read_text(encoding='utf-8'))
        self.assertEqual('private env', tx.ENV.read_text(encoding='utf-8'))
        self.assertEqual('private config', (tx.ROOT / 'config.toml').read_text(encoding='utf-8'))
        self.assertEqual('old runtime', (tx.ROOT / '.venv/bin/python').read_text(encoding='utf-8'))
        self.assertEqual('precious memory', (tx.ROOT / 'data/memory.json').read_text(encoding='utf-8'))
        self.assertFalse((tx.ROOT / 'data/connection-string.txt').exists())
        self.assertFalse(tx.BACKUP.exists())

    def test_commit_only_after_health_and_checksum_failure_does_not_mutate(self):
        with self.assertRaisesRegex(RuntimeError, 'SHA256'):
            tx.install(self.archive, 'bad', '203.0.113.10', 'deploy', {})
        self.assertEqual('old code', (tx.ROOT / 'backend/module.py').read_text(encoding='utf-8'))
        with patch.object(tx, 'health', return_value={'healthy': True, 'version': '0.9.0'}):
            tx.install(self.archive, self.digest, '203.0.113.10', 'deploy', {})
        self.assertEqual('new code', (tx.ROOT / 'backend/module.py').read_text(encoding='utf-8'))
        self.assertFalse(tx.BACKUP.exists())
        self.assertTrue((tx.ROOT / 'data/deployment.json').is_file())
        self.assertEqual('private env', tx.ENV.read_text(encoding='utf-8'))

    def test_config_updates_use_installed_runtime_not_system_python(self):
        tx.install(self.archive, self.digest, '203.0.113.10', 'deploy', {
            'runtime': {'AGENT_CHAT_MODEL': 'test-model'},
            'feishu': {'allowed_open_ids': ['ou_test']},
        })
        updates = [args for args in self.commands if len(args) > 1 and
                   Path(args[1]).name in {'update_hub_env.py', 'update_feishu.py'}]
        self.assertEqual(2, len(updates))
        self.assertTrue(all(args[0] == str(tx.ROOT / '.venv/bin/python') for args in updates))

    def test_connection_recovery_removes_stale_temporary_credentials(self):
        tx.ENV.write_text('AGENT_PUBLIC_HOST=203.0.113.10\nAGENT_SERVER_PORT=8443\n'
                          'AGENT_HUB_TOKEN=0123456789abcdef0123456789abcdef\nAGENT_WEB_PASSWORD=test-web-password\n', encoding='utf-8')
        (tx.ROOT / 'data/hub-tls/hub.crt').write_text('certificate', encoding='utf-8')
        temporary = tx.ROOT / 'data/connection-string.txt'
        temporary.write_text('stale temporary secret', encoding='utf-8')
        with patch.object(tx.ssl, 'PEM_cert_to_DER_cert', return_value=b'certificate'):
            result = tx.credentials()
        self.assertEqual('test-web-password', result['web_password'])
        self.assertTrue(result['connection_string'])
        self.assertFalse(temporary.exists())

    def test_gateway_ownership_accepts_each_managed_ip_tls_unit(self):
        signatures = {
            'xiaoa-ip-http.conf': '# Managed by XiaoA IP TLS.',
            'xiaoa-ip-https.conf': '# Managed by XiaoA IP TLS.',
            'xiaoa-ip-cert-renew.service': 'Description=XiaoA public IP certificate renewal',
            'xiaoa-ip-cert-renew.timer': 'Description=Check XiaoA IP certificate renewal every 12 hours',
            'xiaoa-ip-firewall.service': 'Description=XiaoA public IP web port firewall rules',
        }
        for path in tx.gateway_paths()[1:]:
            with self.subTest(path=path.name):
                path.write_text(signatures[path.name], encoding='utf-8')
                self.assertTrue(tx.gateway_owned(path))

    def test_gateway_ownership_rejects_unmanaged_same_named_file(self):
        path = tx.gateway_paths()[-2]
        path.write_text('Description=Some other application', encoding='utf-8')
        self.assertFalse(tx.gateway_owned(path))

    def test_uninstall_rejects_foreign_gateway_before_stopping_service(self):
        tx.ROOT.joinpath('data/public-tls.json').parent.mkdir(exist_ok=True)
        tx.ROOT.joinpath('data/public-tls.json').write_text('{}', encoding='utf-8')
        foreign = tx.gateway_paths()[-2]
        foreign.write_text('Description=Some other application\n', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, '不属于小A'):
            tx.uninstall(False)
        self.assertEqual([], self.commands)
        self.assertTrue(tx.UNIT.exists())
        self.assertTrue(foreign.exists())

    def test_uninstall_removes_owned_gateway_files(self):
        import json
        tx.ROOT.joinpath('data/public-tls.json').parent.mkdir(exist_ok=True)
        tx.ROOT.joinpath('data/public-tls.json').write_text(json.dumps({'web_port': 8444}), encoding='utf-8')
        signatures = {
            'xiaoa-ip-http.conf': '# Managed by XiaoA IP TLS.',
            'xiaoa-ip-https.conf': '# Managed by XiaoA IP TLS.',
            'xiaoa-ip-cert-renew.service': 'Description=XiaoA public IP certificate renewal',
            'xiaoa-ip-cert-renew.timer': 'Description=Check XiaoA IP certificate renewal every 12 hours',
            'xiaoa-ip-firewall.service': 'Description=XiaoA public IP web port firewall rules',
        }
        for path in tx.gateway_paths()[1:]:
            path.write_text(signatures[path.name], encoding='utf-8')
        tx.uninstall(False)
        self.assertFalse(any(path.exists() for path in tx.gateway_paths()[1:]))
        self.assertTrue((tx.ROOT / 'data/memory.json').exists())

    def test_ca_credentials_recover_unified_port_without_leaf_fingerprint(self):
        import json
        from backend.core.connection_string import parse_connection_string
        tx.ENV.write_text('AGENT_PUBLIC_HOST=8.8.8.8\nAGENT_SERVER_PORT=8443\n'
                          'AGENT_HUB_TOKEN=0123456789abcdef0123456789abcdef\nAGENT_WEB_PASSWORD=test-web-password\n')
        (tx.ROOT / 'data/public-tls.json').write_text(json.dumps(
            {'host': '8.8.8.8', 'web_port': 8444, 'tls_mode': 'ca'}))
        result = tx.credentials()
        details = parse_connection_string(result['connection_string'])
        self.assertEqual('', details.fingerprint)
        self.assertEqual(8444, details.port)
        self.assertEqual('https://8.8.8.8:8444', result['web_url'])

    def test_health_failure_rolls_back_before_commit(self):
        with patch.object(tx, 'health', side_effect=[{'healthy': True}, {'healthy': False}]), \
             patch.object(tx.time, 'monotonic', side_effect=[0, 0, 61]), \
             patch.object(tx.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, '健康检查失败'):
                tx.install(self.archive, self.digest, '203.0.113.10', 'deploy', {})
        self.assertEqual('old code', (tx.ROOT / 'backend/module.py').read_text(encoding='utf-8'))
        self.assertEqual('old runtime', (tx.ROOT / '.venv/bin/python').read_text(encoding='utf-8'))
        self.assertEqual('private env', tx.ENV.read_text(encoding='utf-8'))
        self.assertFalse((tx.ROOT / 'data/deployment.json').exists())
        self.assertFalse(tx.BACKUP.exists())
        self.assertIn(['systemctl', 'start', 'xiaoa-hub'], self.commands)

    def test_gateway_files_roll_back_with_application_failure(self):
        for path in tx.gateway_paths():
            path.write_text('old gateway', encoding='utf-8')
        state = tx.snapshot(False)
        state['mutating'] = True
        tx.write_json(tx.BACKUP / 'state.json', state)
        for path in tx.gateway_paths():
            path.write_text('new gateway', encoding='utf-8')
        tx.restore()
        for path in tx.gateway_paths():
            self.assertEqual('old gateway', path.read_text(encoding='utf-8'))

    def test_interrupted_transaction_can_be_restored(self):
        state = tx.snapshot(True)
        state['mutating'] = True
        tx.write_json(tx.BACKUP / 'state.json', state)
        (tx.ROOT / '.venv').rename(tx.BACKUP / 'venv')
        (tx.ROOT / 'backend/module.py').write_text('half written', encoding='utf-8')
        tx.restore()
        self.assertEqual('old code', (tx.ROOT / 'backend/module.py').read_text(encoding='utf-8'))
        self.assertTrue((tx.ROOT / '.venv/bin/python').is_file())

    def test_uninstall_preserves_data_and_env_purge_is_separate(self):
        self.assertTrue(tx.uninstall(False)['data_preserved'])
        self.assertFalse((tx.ROOT / 'backend').exists())
        self.assertTrue((tx.ROOT / 'data/memory.json').is_file())
        self.assertTrue(tx.ENV.exists())
        self.assertTrue((tx.ROOT / 'config.toml').is_file())
        self.assertFalse(any('nginx' in str(command) or 'apt-get' in str(command) for command in self.commands))
        tx.uninstall(True)
        self.assertFalse((tx.ROOT / 'data').exists())
        self.assertFalse(tx.ENV.exists())

    def test_same_named_other_service_is_rejected(self):
        tx.UNIT.write_text('WorkingDirectory=/some-other-app\n', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, '不属于小A'):
            tx.guard()
        self.assertTrue((tx.ROOT / 'data/memory.json').exists())

    def test_archive_traversal_links_and_devices_are_rejected(self):
        for name, kind in [('../escape', tarfile.REGTYPE), ('/opt/escape', tarfile.REGTYPE),
                           ('backend/link', tarfile.SYMTYPE), ('backend/device', tarfile.CHRTYPE)]:
            with tarfile.open(self.archive, 'w:gz') as handle:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = '/etc/shadow'
                handle.addfile(member)
            with self.assertRaisesRegex(RuntimeError, '不安全'):
                tx.extract(self.archive, Path(self.temp.name) / 'extract')


class RealRunnerTests(unittest.TestCase):
    def test_install_failure_classification_never_exposes_package_credentials(self):
        cases = [
            ('E: Unmet dependencies. Try apt --fix-broken install', '未满足依赖'),
            ('dpkg was interrupted', '未完成的安装'),
            ('Could not get lock /var/lib/dpkg/lock', '其他安装进程'),
            ('No space left on device', '空间不足'),
            ('Read-only file system', '系统目录只读'),
            ('Failed to fetch https://secret:password@repo.example/', '软件源下载失败'),
            ('Unknown apt error', '安装系统依赖失败'),
        ]
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / 'install.log'
            for text, expected in cases:
                with self.subTest(text=text):
                    log.write_text('private credential\n' + text, encoding='utf-8')
                    detail = tx.installer_failure(log, 'apt')
                    self.assertIn(expected, detail)
                    self.assertNotIn('private', detail)
                    self.assertNotIn('password', detail)
                    self.assertNotIn('repo.example', detail)
                    self.assertEqual('', tx.installer_failure(log, 'pip'))

    @unittest.skipUnless(os.name != 'nt', 'Linux-only subprocess phase reader')
    def test_fast_apt_failure_reports_reason_and_final_phase_without_secrets(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / 'failure.sh'
            script.write_text('echo "private secret: E: Unmet dependencies"\n'
                              'printf "XIAOA_STEP apt"\nexit 100\n', encoding='utf-8')
            with redirect_stdout(output), self.assertRaisesRegex(RuntimeError, '未满足依赖') as caught:
                tx.run(['bash', str(script)])
        self.assertIn('系统依赖', output.getvalue())
        self.assertIn('100', str(caught.exception))
        self.assertNotIn('private secret', output.getvalue() + str(caught.exception))

    @unittest.skipUnless(os.name != 'nt', 'Linux-only subprocess phase reader')
    def test_only_curated_real_phase_markers_are_forwarded(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / 'safe-test.sh'
            script.write_text('echo "private credential should never appear"\necho "XIAOA_STEP pip"\nsleep 0.3\necho "XIAOA_STEP tls"\n', encoding='utf-8')
            with redirect_stdout(output):
                tx.run(['bash', str(script)])
        self.assertNotIn('private credential', output.getvalue())
        self.assertIn('Python', output.getvalue())
        self.assertIn('TLS', output.getvalue())

    def test_non_shell_failure_never_includes_raw_credentials(self):
        with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess(['x'], 1, 'private secret', 'private secret')):
            with self.assertRaises(RuntimeError) as caught:
                tx.run(['python3', 'test'])
        self.assertNotIn('private secret', str(caught.exception))


@unittest.skipUnless(os.name != 'nt', 'Linux-only deployment shell probes')
class SystemRuntimeTests(unittest.TestCase):
    def run_probe(self, ready, apt_failure=0):
        root = Path(__file__).resolve().parent.parent
        script = (root / 'deploy/deploy_hub.sh').read_text(encoding='utf-8')
        functions = script[script.index('system_runtime_ready()'):script.index('random_secret()')]
        # Fake only host tools. Never invoke APT or modify the system in a test.
        harness = f'''set -e
ready={int(ready)}
python3() {{ [ "$ready" = 1 ]; }}
openssl() {{ [ "$ready" = 1 ]; }}
apt-get() {{
  echo "APT_CALL $*"
  if [ {apt_failure} -ne 0 ]; then return {apt_failure}; fi
  if [ "$1" = install ]; then ready=1; fi
}}
'''
        return subprocess.run(['bash', '-c', harness + functions + '\nensure_system_runtime\n'],
                              capture_output=True, text=True, timeout=10)

    def test_ready_system_skips_broken_apt(self):
        result = self.run_probe(True, apt_failure=100)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('XIAOA_STEP system-runtime', result.stdout)
        self.assertNotIn('APT_CALL', result.stdout)

    def test_missing_system_runtime_installs_dependencies(self):
        result = self.run_probe(False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('APT_CALL update -y', result.stdout)
        self.assertIn('APT_CALL install -y python3 python3-venv openssl', result.stdout)

    def test_missing_runtime_and_broken_apt_stop_before_venv(self):
        result = self.run_probe(False, apt_failure=100)
        self.assertEqual(100, result.returncode)
        self.assertIn('XIAOA_STEP apt', result.stdout)
        self.assertNotIn('APT_CALL install', result.stdout)


if __name__ == '__main__':
    unittest.main()
