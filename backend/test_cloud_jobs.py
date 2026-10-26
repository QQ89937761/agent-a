"""Offline checks for durable jobs, transactions and destructive-operation guards."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.api import server
from backend.core import cloud_jobs, cloud_deploy
from backend.core.config import Config
from backend.core.connection_string import make_connection_string


class DurableJobsTests(unittest.TestCase):
    def test_real_steps_and_restart_interrupt_and_bounded_history(self):
        job = cloud_jobs.new_job('a' * 32, 'deploy', '203.0.113.10', 'root', '0.9.0')
        cloud_jobs.update(job, 'upload', 'upload', uploaded_bytes=4, total_bytes=10, password='do-not-save')
        self.assertEqual('done', job['steps'][0]['state'])
        self.assertEqual('running', job['steps'][1]['state'])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'jobs.json'
            cloud_jobs.save(path, {job['job_id']: job})
            self.assertNotIn('do-not-save', path.read_text(encoding='utf-8'))
            recovered = cloud_jobs.load(path)[job['job_id']]
            self.assertEqual('interrupted', recovered['stage'])
            self.assertFalse(recovered['ok'])
            self.assertEqual('warning', recovered['steps'][1]['state'])
            jobs = {str(i): cloud_jobs.new_job(str(i) * 32, 'deploy', '', '', '') for i in range(5)}
            cloud_jobs.save(path, jobs)
            self.assertEqual(2, len(json.loads(path.read_text(encoding='utf-8'))))

    def test_maintenance_does_not_claim_six_deployment_steps_completed(self):
        job = cloud_jobs.new_job('b' * 32, 'check', '', '', '')
        cloud_jobs.update(job, 'ssh', 'checking')
        cloud_jobs.update(job, 'done', 'healthy', ok=True)
        self.assertEqual('done', job['steps'][0]['state'])
        self.assertTrue(all(step['state'] == 'skipped' for step in job['steps'][1:]))


class DeploymentApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_locality_confirmation_cross_site_and_concurrency(self):
        local = SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'), headers={})
        remote = SimpleNamespace(client=SimpleNamespace(host='203.0.113.9'), headers={})
        cross_site = SimpleNamespace(client=local.client, headers={'origin': 'https://evil.invalid'})
        req = server.CloudDeployRequest(host='203.0.113.10', operation='uninstall', password='remote-secret')
        with tempfile.TemporaryDirectory() as temp, patch.object(server, 'app_home', return_value=Path(temp)), \
             patch.object(server, '_CLOUD_DEPLOY_JOBS', {}), patch.object(server, '_run_cloud_deploy', new=AsyncMock()) as run:
            self.assertFalse((await server.api_cloud_deploy(req, local))['ok'])
            req.confirmation = 'remote_password'
            req.purge_data = True
            self.assertFalse((await server.api_cloud_deploy(req, local))['ok'])
            req.purge_confirmation = '删除数据 203.0.113.10'
            for request in (remote, cross_site):
                with self.assertRaises(Exception) as caught:
                    await server.api_cloud_deploy(req, request)
                self.assertEqual(403, caught.exception.status_code)
            run.assert_not_called()
            req.operation = 'deploy'
            first = await server.api_cloud_deploy(req, local)
            self.assertTrue(first['ok'])
            second = await server.api_cloud_deploy(req, local)
            self.assertFalse(second['ok'])
            self.assertEqual(first['job_id'], second['active_job_id'])
            latest = await server.api_cloud_deploy_latest(local)
            self.assertEqual(first['job_id'], latest['job']['job_id'])
            await asyncio.sleep(0)

    async def test_cloud_success_worker_failure_is_partial_and_secrets_are_redacted(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(server, 'app_home', return_value=Path(temp)), \
             patch.object(server, '_CLOUD_DEPLOY_JOBS', {}), patch.object(server, '_replace_worker_bridge', new=AsyncMock(side_effect=RuntimeError('no worker'))):
            job_id = 'c' * 32
            server._CLOUD_DEPLOY_JOBS[job_id] = cloud_jobs.new_job(job_id, 'deploy', '', '', '')
            cfg = Config()
            cfg.llm.api_key = 'really-private-value'
            with patch.object(server, 'CFG', cfg):
                server._cloud_job_update(job_id, stage='health', message='really-private-value', password='not-stored')
            await server._cloud_connect_worker(job_id)
            job = server._CLOUD_DEPLOY_JOBS[job_id]
            self.assertEqual('partial', job['stage'])
            self.assertTrue(job['cloud_ready'])
            self.assertEqual('done', job['steps'][4]['state'])
            text = (Path(temp) / 'data/cloud-deploy-jobs.json').read_text(encoding='utf-8')
            self.assertNotIn('really-private-value', text)
            self.assertNotIn('not-stored', text)

    async def test_retry_only_checks_status_and_reconnects_does_not_upload(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(server, 'app_home', return_value=Path(temp)), \
             patch.object(server, '_CLOUD_DEPLOY_JOBS', {}), patch.object(cloud_deploy, 'maintain', return_value={'healthy': True, 'version': '0.9.6'}), \
             patch.object(cloud_deploy, 'deploy') as deploy, patch.object(server, '_cloud_connect_worker', new=AsyncMock()) as connect, \
             patch.object(cloud_deploy, 'recover_connection', return_value={
                 'connection_string': make_connection_string('203.0.113.10', 8443, 'f' * 64, 't' * 32),
                 'connection': {'host': '203.0.113.10', 'port': 8443}}), \
             patch.object(server, 'CFG', Config()), patch.object(server, '_rewrite_config'):
            target = Path(temp) / 'data/cloud-ssh/target.json'
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({'host': '203.0.113.10', 'username': 'root'}), encoding='utf-8')
            job_id = 'd' * 32
            server._CLOUD_DEPLOY_JOBS[job_id] = cloud_jobs.new_job(job_id, 'retry', '203.0.113.10', 'root', '')
            req = server.CloudDeployRequest(host='203.0.113.10', operation='retry')
            await server._run_cloud_deploy(job_id, req)
            deploy.assert_not_called()
            connect.assert_awaited_once()
            self.assertEqual('0.9.6', server._CLOUD_DEPLOY_JOBS[job_id]['remote_status']['version'])

    async def test_deploy_and_repair_never_download_cloud_release(self):
        from backend.core import update_manager

        connection_string = make_connection_string('203.0.113.10', 8443, 'f' * 64, 't' * 32)
        deploy_result = {
            'connection_string': connection_string,
            'connection': {'host': '203.0.113.10', 'port': 8443},
            'remote_status': {'healthy': True, 'version': '0.9.6'},
        }
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(server, 'app_home', return_value=Path(temp)), \
             patch.object(server, '_CLOUD_DEPLOY_JOBS', {}), \
             patch.object(server, 'CFG', Config()), \
             patch.object(server, '_rewrite_config'), \
             patch.object(server, '_cloud_connect_worker', new=AsyncMock()), \
             patch.object(update_manager, 'fetch_manifest', side_effect=AssertionError('no website access')) as fetch, \
             patch.object(cloud_deploy, 'deploy', return_value=deploy_result) as deploy:
            for operation in ('deploy', 'repair'):
                for use_release in (False, True):
                    with self.subTest(operation=operation, legacy_use_release=use_release):
                        deploy.reset_mock()
                        job_id = 'e' * 32
                        server._CLOUD_DEPLOY_JOBS[job_id] = cloud_jobs.new_job(job_id, operation, '', '', '')
                        req = server.CloudDeployRequest(
                            host='203.0.113.10', password='server-password',
                            operation=operation, use_release=use_release,
                        )
                        await server._run_cloud_deploy(job_id, req)
                        deploy.assert_called_once()
                        self.assertEqual(operation, deploy.call_args.kwargs['operation'])
                        self.assertNotIn('cloud_archive', deploy.call_args.kwargs)
                        self.assertNotIn('transaction_script', deploy.call_args.kwargs)
                        self.assertTrue(server._CLOUD_DEPLOY_JOBS[job_id]['cloud_ready'])
                        self.assertEqual('0.9.6', server._CLOUD_DEPLOY_JOBS[job_id]['remote_status']['version'])
                        fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
