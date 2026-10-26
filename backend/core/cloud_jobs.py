"""Bounded durable deployment state, six real steps, no credential storage."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

STAGES = ('ssh', 'upload', 'runtime', 'configure', 'health', 'worker')
TITLES = ('检查服务器', '上传安装文件', '安装运行环境', '更新配置与启动服务', '检查云端服务', '连接本机 worker')
TERMINAL = {'done', 'error', 'partial', 'interrupted'}
FIELDS = {'job_id', 'ok', 'stage', 'message', 'error', 'created_at', 'updated_at',
          'finished_at', 'operation', 'host', 'username', 'version', 'steps', 'logs',
          'uploaded_bytes', 'total_bytes', 'connection', 'worker_id', 'cloud_ready', 'remote_status'}


def new_job(job_id, operation, host, username, version):
    now = time.time()
    return {'job_id': job_id, 'operation': operation, 'host': host, 'username': username,
            'version': version, 'ok': None, 'stage': 'queued', 'message': '已排队',
            'created_at': now, 'updated_at': now, 'logs': [],
            'steps': [{'key': key, 'title': title, 'state': 'pending'} for key, title in zip(STAGES, TITLES)]}


def update(job, stage, message, **extra):
    now = time.time()
    job.update(stage=stage, message=message, updated_at=now)
    job.update({k: v for k, v in extra.items() if k in FIELDS})
    steps = job['steps']
    maintenance = job['operation'] in {'check', 'uninstall'}
    if maintenance:
        for step in steps[1:]:
            step['state'] = 'skipped'
    if job['operation'] == 'retry':
        for step in steps[1:4]:
            step['state'] = 'skipped'
    if stage in STAGES:
        index = STAGES.index(stage)
        for i, step in enumerate(steps):
            if i < index and step['state'] not in {'done', 'skipped'}:
                step.update(state='done', finished_at=now)
            if i == index:
                step.setdefault('started_at', now)
                step.update(state='running', message=message)
    elif stage in TERMINAL:
        job['finished_at'] = now
        for step in steps:
            if stage == 'done' and job['operation'] in {'deploy', 'repair', 'retry'} and step['state'] != 'skipped':
                step.update(state='done')
                step.setdefault('finished_at', now)
            elif step['state'] == 'running':
                state = 'done' if stage == 'done' else 'warning' if stage in {'partial', 'interrupted'} else 'error'
                step.update(state=state, finished_at=now)
    if message and (not job['logs'] or job['logs'][-1]['message'] != message):
        job['logs'].append({'stage': stage, 'message': message, 'time': now})
        del job['logs'][:-100]


def save(path: Path, jobs):
    # ponytail: one local deployment at a time; only current + previous result retained.
    latest = sorted(jobs.values(), key=lambda j: j['created_at'])[-2:]
    data = [{k: v for k, v in job.items() if k in FIELDS} for job in latest]
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def load(path: Path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        jobs = {}
        if not isinstance(data, list):
            return {}
        for value in data[-2:]:
            if not isinstance(value, dict) or len(str(value.get('job_id', ''))) != 32:
                continue
            job = {k: v for k, v in value.items() if k in FIELDS}
            if len(job.get('steps', [])) != 6:
                continue
            if job.get('stage') not in TERMINAL:
                update(job, 'interrupted', '本机程序退出导致等待中断；请先检查云端状态，不会盲目续跑或重新上传', ok=False)
            jobs[job['job_id']] = job
        return jobs
    except (OSError, ValueError, TypeError, KeyError):
        return {}
