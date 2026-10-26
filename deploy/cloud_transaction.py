"""Root-only, fixed-scope hub installer. Uploaded to /tmp before replacing code.

One remote process owns a flock through install/config/health/rollback. No other
services or shared system packages are removed. Interrupted installs retain a
journal; the next explicitly requested deploy repairs it before starting anew.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import ssl
import shlex
import sys
import subprocess
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path('/opt/xiaoa')
ENV = Path('/etc/xiaoa-hub.env')
UNIT = Path('/etc/systemd/system/xiaoa-hub.service')
BACKUP = ROOT / '.deploy-backup'
CODE = ('backend', 'deploy', 'frontend', 'config.example.toml')
MARKER = 'XIAOA_PROGRESS '


def emit(stage, message, **extra):
    print(MARKER + json.dumps({'stage': stage, 'message': message, **extra}, ensure_ascii=False), flush=True)


def installer_failure(log_path, step):
    # Classify locally; never forward package output, URLs or credentials.
    with log_path.open('rb') as log:
        log.seek(max(0, log_path.stat().st_size - 65536))
        text = log.read().decode('utf-8', 'replace').casefold()
    if step == 'public-tls':
        return '公网 IP 证书申请或网页入口配置失败；请检查公网 80、安全组和 Nginx，旧版将自动恢复'
    if step == 'apt':
        if any(marker in text for marker in ('unmet dependencies', 'fix-broken', 'dpkg was interrupted')):
            return '服务器 APT/DPKG 存在未满足依赖或未完成的安装，需管理员修复系统包状态；小A不会自动修复其他系统包'
        if any(marker in text for marker in ('could not get lock', 'unable to acquire', 'is another process using it')):
            return '服务器 APT 被其他安装进程占用，请等待该进程完成后重试'
        if 'no space left on device' in text:
            return '服务器系统包安装空间不足，请释放空间后重试'
        if 'read-only file system' in text:
            return '服务器系统目录只读，无法安装缺少的依赖，请管理员检查挂载或运行权限'
        if any(marker in text for marker in ('failed to fetch', 'could not resolve', 'temporary failure resolving')):
            return '服务器软件源下载失败，请检查软件源地址、DNS 和网络后重试'
        return '安装系统依赖失败，请管理员检查 APT、Python 3.10+、venv 和 OpenSSL'
    return ''


def run(args, *, check=True, env=None):
    # Never forward arbitrary shell/pip output: it may contain secret-bearing URLs.
    if args[0] != 'bash':
        result = subprocess.run(args, capture_output=True, text=True, env=env, timeout=120)
    else:
        safe_markers = {
            'apt': '安装系统依赖（Python / OpenSSL，不安装 Nginx）',
            'system-runtime': '系统 Python、venv 和 OpenSSL 已可用，跳过 APT，仅重建小A独立运行环境',
            'venv': '创建独立 Python 运行环境',
            'pip': '安装云端 Python 依赖，耗时取决于服务器网络',
            'tls': '核验/生成本机连接 TLS 证书（已有指纹保留）',
            'public-tls': '申请公网 IP HTTPS 证书并配置自动续期（需放行公网 80）',
            'config': '保留已有 token、密码和配置；写入小A服务定义',
        }
        # Poll a private temporary log: genuine phase markers only, no fabricated progress.
        with tempfile.TemporaryDirectory(prefix='xiaoa-install-log-') as temp:
            log_path = Path(temp) / 'install.log'
            log_path.touch(mode=0o600)
            output = log_path.open('w', encoding='utf-8')
            proc = subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            deadline = time.monotonic() + 1500
            offset, pending, step = 0, '', ''
            try:
                while True:
                    # Poll first, then drain the log, including a final line without LF.
                    code = proc.poll()
                    with log_path.open(encoding='utf-8', errors='replace') as log:
                        log.seek(offset)
                        chunk = log.read()
                        offset = log.tell()
                    lines = (pending + chunk).split('\n')
                    pending = lines.pop()[-2000:]
                    if code is not None and pending:
                        lines.append(pending)
                    for line in lines:
                        key = line.removeprefix('XIAOA_STEP ').strip()
                        if line.startswith('XIAOA_STEP ') and key in safe_markers:
                            step = key
                            emit('runtime', safe_markers[key])
                    if code is not None:
                        detail = installer_failure(log_path, step) if code else ''
                        result = subprocess.CompletedProcess(args, code, '', detail)
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError('安装依赖超过 25 分钟，已停止安装并尝试回退')
                    time.sleep(0.2)
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                output.close()
    if check and result.returncode:
        if args[0] == 'bash' and result.stderr:
            raise RuntimeError(f'{result.stderr}（退出码 {result.returncode}）；本次部署已停止并尝试回退')
        raise RuntimeError(f'{Path(args[0]).name} 执行失败（退出码 {result.returncode}）；旧版将自动恢复')
    return result


def guard():
    for path in (*gateway_paths(), ROOT, ROOT.parent, ROOT / 'data', ROOT / 'data/hub-tls', ROOT / 'data/hub-tls/hub.crt',
                 ROOT / 'data/hub-tls/hub.key', ROOT / 'data/connection-string.txt', BACKUP, ENV, UNIT,
                 *(ROOT / name for name in (*CODE, 'config.toml', '.venv'))):
        if path.is_symlink():
            raise RuntimeError('小A部署路径含符号链接，已拒绝维护以避免误改其他服务')
    fragment = run(['systemctl', 'show', '-p', 'FragmentPath', '--value', 'xiaoa-hub'], check=False).stdout.strip()
    if fragment and fragment != str(UNIT):
        raise RuntimeError('同名服务来自其他安装位置，已拒绝覆盖/卸载')
    if UNIT.exists():
        text = UNIT.read_text(encoding='utf-8')
        if (f'WorkingDirectory={ROOT}\n' not in text or
                f'ExecStart={ROOT}/.venv/bin/python -m backend.api.server\n' not in text):
            raise RuntimeError('同名 systemd 服务不属于小A，已拒绝覆盖/卸载')


def remove(path):
    if path.is_symlink():
        raise RuntimeError('拒绝删除符号链接路径')
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def copy(source, dest):
    if source.is_dir():
        shutil.copytree(source, dest, symlinks=True)
    else:
        shutil.copy2(source, dest)


def write_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value), encoding='utf-8')
    temp.chmod(0o600)
    os.replace(temp, path)


def gateway_paths():
    return [ROOT / 'data/public-tls.json',
            Path('/etc/nginx/conf.d/xiaoa-ip-http.conf'),
            Path('/etc/nginx/conf.d/xiaoa-ip-https.conf'),
            Path('/etc/systemd/system/xiaoa-ip-cert-renew.service'),
            Path('/etc/systemd/system/xiaoa-ip-cert-renew.timer'),
            Path('/etc/systemd/system/xiaoa-ip-firewall.service')]


def gateway_owned(path):
    """Accept only the exact XiaoA IP-TLS signatures for each managed file."""
    if not path.exists():
        return True
    if not path.is_file():
        return False
    text = path.read_text(encoding='utf-8', errors='replace')
    signatures = {
        'xiaoa-ip-http.conf': '# Managed by XiaoA IP TLS.',
        'xiaoa-ip-https.conf': '# Managed by XiaoA IP TLS.',
        'xiaoa-ip-cert-renew.service': 'Description=XiaoA public IP certificate renewal',
        'xiaoa-ip-cert-renew.timer': 'Description=Check XiaoA IP certificate renewal every 12 hours',
        'xiaoa-ip-firewall.service': 'Description=XiaoA public IP web port firewall rules',
    }
    signature = signatures.get(path.name)
    lines = text.splitlines()
    if not signature:
        return False
    return any(line == signature or (path.name.endswith('.conf') and line.startswith(signature))
               for line in lines)


def validate_gateway_removal():
    """Validate every existing public gateway before uninstall mutates anything."""
    paths = gateway_paths()
    managed_state = paths[0].is_file()
    existing = [path for path in paths[1:] if path.exists()]
    if not managed_state and not existing:
        return False
    for path in existing:
        if not gateway_owned(path):
            raise RuntimeError('网页证书配置不属于小A，已拒绝删除')
    return True


def snapshot(rebuild):
    BACKUP.mkdir(mode=0o700)
    paths = [ROOT / name for name in (*CODE, 'config.toml')] + [ENV, UNIT] + gateway_paths()
    entries = []
    for index, path in enumerate(paths):
        item = {'path': str(path), 'saved': str(index), 'exists': path.exists()}
        entries.append(item)
        if path.is_symlink():
            raise RuntimeError('小A部署路径含符号链接，拒绝创建回退点')
        if path.exists():
            copy(path, BACKUP / str(index))
    state = {
        'entries': entries, 'active': run(['systemctl', 'is-active', '--quiet', 'xiaoa-hub'], check=False).returncode == 0,
        'enabled': run(['systemctl', 'is-enabled', '--quiet', 'xiaoa-hub'], check=False).returncode == 0,
        'rebuild': rebuild, 'venv_exists': (ROOT / '.venv').exists(), 'mutating': False,
    }
    write_json(BACKUP / 'state.json', state)
    return state


def restore():
    state_path = BACKUP / 'state.json'
    if not state_path.is_file():
        # No mutation happened until the complete journal was saved.
        if BACKUP.exists():
            remove(BACKUP)
        return
    state = json.loads(state_path.read_text(encoding='utf-8'))
    expected = {str(ROOT / name) for name in (*CODE, 'config.toml')} | {str(ENV), str(UNIT)}
    actual = {item['path'] for item in state['entries']}
    # Accept old journals made before the IP HTTPS gateway was introduced.
    if actual not in (expected, expected | {str(path) for path in gateway_paths()}):
        raise RuntimeError('回退记录路径无效，已停止维护')
    if state['mutating']:
        remove(ROOT / 'data/connection-string.txt')
        run(['systemctl', 'stop', 'xiaoa-hub'], check=False)
        for item in state['entries']:
            path = Path(item['path'])
            saved = BACKUP / item['saved']
            if not str(item['saved']).isdigit() or saved.parent != BACKUP or saved.is_symlink():
                raise RuntimeError('回退记录无效')
            remove(path)
            if item['exists']:
                copy(saved, path)
        if state['rebuild']:
            old = BACKUP / 'venv'
            if old.exists() or not state['venv_exists']:
                remove(ROOT / '.venv')
                if old.exists():
                    old.rename(ROOT / '.venv')
        if any(str(path) in actual for path in gateway_paths()):
            run(['systemctl', 'stop', 'xiaoa-ip-cert-renew.timer'], check=False)
            if gateway_paths()[-2].exists():
                run(['systemctl', 'daemon-reload'])
                run(['systemctl', 'enable', '--now', 'xiaoa-ip-cert-renew.timer'])
            else:
                run(['systemctl', 'disable', 'xiaoa-ip-cert-renew.timer'], check=False)
            if gateway_paths()[-1].exists():
                run(['systemctl', 'enable', 'xiaoa-ip-firewall.service'])
            else:
                run(['systemctl', 'disable', 'xiaoa-ip-firewall.service'], check=False)
            if shutil.which('nginx'):
                run(['nginx', '-t'])
                run(['systemctl', 'reload', 'nginx'])
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'enable' if state['enabled'] else 'disable', 'xiaoa-hub'], check=False)
        if state['active']:
            run(['systemctl', 'start', 'xiaoa-hub'])
    remove(BACKUP)
    emit('rollback', '已恢复原有代码、配置和运行环境；数据与证书未删除')


def extract(archive_path, dest):
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        for item in members:
            path = PurePosixPath(item.name)
            if (path.is_absolute() or '..' in path.parts or not path.parts or
                    path.parts[0] not in CODE or not (item.isfile() or item.isdir())):
                raise RuntimeError('安装包包含不安全路径/链接')
        # All members were checked, including link/device types; works on Python 3.10+.
        archive.extractall(dest, members=members, **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))
    for name in (*CODE,):
        if not (dest / name).exists():
            raise RuntimeError('安装包缺少必需文件：' + name)
    if not (dest / 'frontend/src/settings.html').is_file():
        raise RuntimeError('安装包缺少云端页面')


def health():
    if not ENV.is_file():
        return {'installed': False, 'active': False, 'healthy': False, 'transaction_pending': BACKUP.exists()}
    values = {}
    for line in ENV.read_text(encoding='utf-8').splitlines():
        name, sep, value = line.partition('=')
        if sep and name in {'AGENT_SERVER_PORT', 'AGENT_TLS_CERT'}:
            tokens = shlex.split(value)
            values[name] = tokens[0] if tokens else ''
    active = run(['systemctl', 'is-active', '--quiet', 'xiaoa-hub'], check=False).returncode == 0
    result = {'installed': (ROOT / 'backend').is_dir(), 'active': active, 'healthy': False,
              'port': int(values.get('AGENT_SERVER_PORT', '8443')), 'transaction_pending': BACKUP.exists()}
    if active:
        try:
            # Localhost probe only, pinned to the installed self-signed certificate.
            cert = values.get('AGENT_TLS_CERT', str(ROOT / 'data/hub-tls/hub.crt'))
            context = ssl.create_default_context(cafile=cert)
            context.check_hostname = False  # Certificate SAN is the PUBLIC_IP, not loopback.
            with urllib.request.urlopen(f"https://127.0.0.1:{result['port']}/health", context=context, timeout=3) as response:
                body = json.load(response)
            result['healthy'] = bool(body.get('ok')) and body.get('role') == 'hub'
            result['version'] = str(body.get('version', ''))[:80]
        except (OSError, ValueError):
            pass
    return result


def install(archive, digest, host, mode, settings):
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise RuntimeError('安装包 SHA256 校验失败，未改动原有部署')
    ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)
    if BACKUP.exists():
        emit('ssh', '发现上次中断的部署，先恢复上次回退点')
        restore()
    with tempfile.TemporaryDirectory(prefix='.deploy-staging-', dir=ROOT) as temp:
        staged = Path(temp)
        extract(archive, staged)
        if shutil.disk_usage(ROOT).free < max(512 * 1024 * 1024, archive.stat().st_size * 4):
            raise RuntimeError('服务器可用空间不足（至少需要 512MB），未修改原部署')
        rebuild = mode == 'repair' or not (ROOT / '.venv/bin/python').is_file()
        old_requirements = ROOT / 'backend/requirements-hub.txt'
        if not old_requirements.exists() or old_requirements.read_bytes() != (staged / 'backend/requirements-hub.txt').read_bytes():
            rebuild = True
        installed = (ROOT / 'backend').is_dir()
        if installed and not health().get('healthy'):
            # A deploy button also repairs a broken environment; ordinary healthy updates reuse it.
            rebuild = True
        state = snapshot(rebuild)
        try:
            state['mutating'] = True
            write_json(BACKUP / 'state.json', state)
            if state['active']:
                run(['systemctl', 'stop', 'xiaoa-hub'])
            if rebuild and (ROOT / '.venv').exists():
                (ROOT / '.venv').rename(BACKUP / 'venv')
            for name in CODE:
                remove(ROOT / name)
                (staged / name).rename(ROOT / name)
            emit('runtime', '正在安装运行环境和依赖' if rebuild else '依赖未变化，复用已有运行环境')
            env = dict(os.environ, XIAOA_SKIP_START='1', XIAOA_REUSE_RUNTIME='0' if rebuild else '1', DEBIAN_FRONTEND='noninteractive')
            run(['bash', str(ROOT / 'deploy/deploy_hub.sh'), host], env=env)
            emit('configure', '正在保留原有配置并同步本次模型/飞书设置')
            # Installed scripts validate and update only explicitly supplied fields.
            runtime = settings.get('runtime', {})
            feishu = settings.get('feishu', {})
            for script, values, target in [('update_hub_env.py', runtime, ENV), ('update_feishu.py', feishu, ROOT / 'config.toml')]:
                if values:
                    payload = staged / (script + '.json')
                    write_json(payload, values)
                    # Ubuntu 22.04's system Python lacks tomllib; tomli lives in our venv.
                    run([str(ROOT / '.venv/bin/python'), str(ROOT / 'deploy' / script), str(payload), str(target)])
            run(['systemctl', 'daemon-reload'])
            run(['systemctl', 'enable', 'xiaoa-hub'])
            run(['systemctl', 'restart', 'xiaoa-hub'])
            emit('health', '检查 systemd 与 TLS 健康接口（最长 60 秒）')
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                status = health()
                if status['healthy']:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('云端健康检查失败，自动恢复旧版；请检查日志/模型配置')
            write_json(ROOT / 'data/deployment.json', {'version': status.get('version', ''), 'time': time.time(), 'mode': mode, 'sha256': digest})
            remove(BACKUP)
            emit('health', '云端健康检查通过 · ' + ('修复重装' if mode == 'repair' else '更新' if installed else '首次安装'), cloud_ready=True)
            return status
        except BaseException:
            try:
                restore()
            except Exception:
                emit('rollback', '自动回退未完成，回退点仍保留；请检查服务器状态后执行修复')
                raise RuntimeError('部署失败且回退未完成；原回退点仍保留，请检查状态/修复') from None
            raise


def credentials():
    """Recover a completed deployment without rotating token/cert or reinstalling."""
    if not health().get('healthy'):
        raise RuntimeError('云端未健康上线，不能恢复本机连接')
    values = {}
    for line in ENV.read_text(encoding='utf-8').splitlines():
        name, sep, raw = line.partition('=')
        if sep:
            parts = shlex.split(raw)
            values[name] = parts[0] if parts else ''
    sys.path.insert(0, str(ROOT))
    from backend.core.connection_string import make_connection_string
    public_state = ROOT / 'data/public-tls.json'
    info = json.loads(public_state.read_text()) if public_state.is_file() else {}
    if info.get('tls_mode') == 'ca' and info.get('host') == values['AGENT_PUBLIC_HOST']:
        port, fingerprint = int(info['web_port']), ''
    else:
        port = int(values['AGENT_SERVER_PORT'])
        cert = Path(values.get('AGENT_TLS_CERT', str(ROOT / 'data/hub-tls/hub.crt')))
        der = ssl.PEM_cert_to_DER_cert(cert.read_text(encoding='utf-8'))
        fingerprint = hashlib.sha256(der).hexdigest()
    connection = make_connection_string(values['AGENT_PUBLIC_HOST'], port, fingerprint, values['AGENT_HUB_TOKEN'])
    result = {'connection_string': connection, 'web_password': values['AGENT_WEB_PASSWORD']}
    if info.get('host') == values['AGENT_PUBLIC_HOST']:
        host = info['host']
        host = f'[{host}]' if ':' in host else host
        result['web_url'] = f"https://{host}:{int(info['web_port'])}"

    # A lost SSH reply is recoverable from ENV; don't retain the temporary secret file.
    remove(ROOT / 'data/connection-string.txt')
    # Only sent over SSH to the local backend; not a progress/job/log entry.
    return result


def uninstall(purge):
    if BACKUP.exists():
        raise RuntimeError('存在未完成的部署回退点，请先修复再卸载')
    has_gateway = validate_gateway_removal()
    if UNIT.exists():
        run(['systemctl', 'stop', 'xiaoa-hub'])
        run(['systemctl', 'disable', 'xiaoa-hub'])
    if has_gateway:
        run(['systemctl', 'disable', '--now', 'xiaoa-ip-cert-renew.timer'], check=False)
        run(['systemctl', 'disable', '--now', 'xiaoa-ip-firewall.service'], check=False)
        for path in gateway_paths()[1:]:
            if path.exists():
                remove(path)
        run(['nginx', '-t'])
        run(['systemctl', 'reload', 'nginx'])
    for path in [UNIT, *(ROOT / name for name in (*CODE, '.venv'))]:
        remove(path)
    if purge:
        remove(ROOT / 'config.toml')
        remove(ROOT / 'data')
        remove(ENV)
    run(['systemctl', 'daemon-reload'])
    # Keep SSH enrollment and firewall rules: ownership of shared rules is not provable.
    return {'uninstalled': True, 'data_preserved': not purge}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['deploy', 'repair', 'check', 'uninstall', 'credentials'])
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--digest', default='')
    parser.add_argument('--host', default='')
    parser.add_argument('--settings', type=Path)
    parser.add_argument('--purge', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('云端维护需要 root 或免密 sudo')
    guard()
    if args.action in {'check', 'credentials'}:
        result = health() if args.action == 'check' else credentials()
        print(json.dumps(result, ensure_ascii=False))
        return
    import fcntl
    lock_path = Path('/opt/xiaoa-deploy.lock')
    if lock_path.is_symlink():
        raise RuntimeError('部署锁路径无效')
    with lock_path.open('a') as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('服务器已有另一个部署/维护任务，请稍后重试') from None
        guard()
        if args.action == 'uninstall':
            result = uninstall(args.purge)
        else:
            result = install(args.archive, args.digest, args.host, args.action,
                             json.loads(args.settings.read_text(encoding='utf-8')))
        print('XIAOA_RESULT ' + json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # This script only emits curated errors, never subprocess output/secrets.
        print('XIAOA_ERROR ' + str(exc)[:600], flush=True)
        raise SystemExit(1)
