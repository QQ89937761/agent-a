"""Exercise real Portable replacement/restart in an isolated workspace (no real keys)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from unittest.mock import patch

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.core import update_manager


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('executable', type=Path)
    parser.add_argument('--old-executable', type=Path)
    args = parser.parse_args()
    new_source = args.executable.resolve()
    old_source = (args.old_executable or args.executable).resolve()
    if os.name != 'nt' or any(not p.is_file() or not p.is_relative_to(ROOT) for p in (new_source, old_source)):
        raise RuntimeError('Smoke only accepts Windows EXEs in this workspace')
    expected = json.loads((ROOT/'frontend/package.json').read_text(encoding='utf-8'))['version']
    # The shipped Electron/UI use this port; never connect to an existing user's instance.
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', 1985))
        except OSError as error:
            raise RuntimeError('Exit the running XiaoA before update smoke testing (port 1985 is occupied).') from error
        port = probe.getsockname()[1]
    allowed = (ROOT/'data/isolation/tmp').resolve()
    allowed.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix='portable-update-', dir=allowed)).resolve()
    executable = home/'XiaoA.exe'
    shutil.copy2(old_source, executable)
    with new_source.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    env = {k:v for k,v in os.environ.items() if not k.startswith(('AGENT_', 'XIAOA_', 'PORTABLE_'))}
    app_home = home/'XiaoA-data'
    env.update(XIAOA_HOME=str(app_home), AGENT_SERVER_PORT=str(port), AGENT_SERVER_HOST='127.0.0.1',
               XIAOA_USER_DATA_DIR=str(home/'electron-profile'), HF_HUB_OFFLINE='1',
               PYTHONHOME='', PYTHONPATH='', PYTHONNOUSERSITE='1')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f'http://127.0.0.1:{port}'
    startup = subprocess.STARTUPINFO()
    startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    old = subprocess.Popen([str(executable), f'--user-data-dir={home / "electron-profile"}'], cwd=home, env=env, startupinfo=startup)
    known = [old.pid]
    prepared = None
    try:
        deadline = time.monotonic()+300
        while time.monotonic()<deadline:
            if old.poll() is not None:
                raise RuntimeError('Old portable exited before startup')
            try:
                with opener.open(base+'/health', timeout=1) as response:
                    health = json.load(response)
                if health.get('ok'):
                    break
            except (OSError, ValueError):
                time.sleep(.5)
        else:
            raise RuntimeError('Old portable failed startup')
        backend = next(child for child in psutil.Process(old.pid).children(recursive=True) if child.name() == 'xiaoa-backend.exe')
        known.append(backend.pid)
        print(f'OLD_PORTABLE_READY version={health["version"]}', flush=True)

        def copy_verified_asset(manifest, directory, progress=None):
            downloaded = Path(directory)/'XiaoA.exe'
            shutil.copy2(new_source, downloaded)
            return Path(directory), downloaded

        state_path = update_manager.update_state_path(app_home)
        with patch.dict(os.environ, env, clear=True), patch.object(update_manager, 'portable_update_context', side_effect=copy_verified_asset):
            prepared = update_manager.prepare_portable_update({'version':expected, 'portable':{'sha256':digest}},
                executable=executable, parent_pid=old.pid, backend_pid=backend.pid,
                state_path=state_path, log_path=app_home/'data/update.log', health_url=base+'/health')
            if old.poll() is not None:
                raise RuntimeError('Preparation unexpectedly closed old app')
            print('DOWNLOAD_PREPARATION_KEEPS_APP_RUNNING', flush=True)
            update_manager.restart_prepared_update(prepared['prepared_config'], executable=executable,
                parent_pid=old.pid, backend_pid=backend.pid)
        print('UPDATE_WORKER_ACKNOWLEDGED', flush=True)
        deadline = time.monotonic()+360
        while time.monotonic()<deadline:
            state = update_manager.load_update_state(state_path)
            if state['stage'] in ('done', 'rolled_back', 'error'):
                break
            time.sleep(.5)
        if state['stage'] != 'done':
            raise RuntimeError(f'Portable replacement failed: {state}')
        if old.poll() is None or backend.is_running():
            raise RuntimeError('Old app processes remain')
        with executable.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                raise RuntimeError('Replaced outer EXE hash mismatch')
        with opener.open(base+'/health', timeout=3) as response:
            health = json.load(response)
        if health.get('version') != expected:
            raise RuntimeError('New version health mismatch')
        # The settings UI polls this endpoint before rendering cancel controls;
        # it synchronizes the external worker's durable final state.
        with opener.open(base+'/api/update/job', timeout=5) as response:
            if json.load(response).get('stage') != 'done':
                raise RuntimeError('Frozen job endpoint did not observe worker completion')
        # Verify the actual frozen API can clear state, not just source unit tests.
        token = (app_home/'ui_token.txt').read_text(encoding='utf-8').strip()
        request = urllib.request.Request(base+'/api/update/cancel', method='POST', headers={'X-UI-Token':token})
        with opener.open(request, timeout=5) as response:
            result = json.load(response)
            if not result.get('ok'):
                raise RuntimeError(f'Frozen cancel endpoint failed: {result}')
        with opener.open(base+'/api/update/job', timeout=5) as response:
            if json.load(response).get('stage') != 'cancelled':
                raise RuntimeError('Frozen cancel endpoint did not clear state')
        print(f'PORTABLE_UPDATE_SMOKE_PASSED version={expected} old_processes_closed=True hash_verified=True cancel_api=True', flush=True)
    finally:
        print(f'UPDATE_SMOKE_DIAGNOSTICS={home}', flush=True)
        log = app_home/'data/update.log'
        if log.exists():
            print(log.read_text(encoding='utf-8-sig', errors='replace')[-3000:], flush=True)
        # Kill only known test PIDs or exact executable paths inside this test.
        # Retain the isolated files/logs for diagnosis; never touch installed XiaoA.
        for item in psutil.process_iter(['pid', 'exe']):
            try:
                candidate = Path(item.info['exe'] or '').resolve()
                if item.pid in known or candidate == executable or candidate.is_relative_to(home):
                    subprocess.run(['taskkill', '/PID', str(item.pid), '/T', '/F'], capture_output=True, check=False)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                pass


if __name__ == '__main__':
    main()
