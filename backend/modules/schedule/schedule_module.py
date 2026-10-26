"""进程内定时任务调度器(不依赖 Windows 任务计划程序)。

任务以 JSON 列表落盘在 app_home()/data/scheduled_tasks.json,原子写入。
任务表会同步到云端(AGENT_SCHEDULE_TASKS 环境变量),由 server 的 asyncio 循环驱动。

执行位置(location):
  - "local":本地电脑(standalone/worker)执行
  - "cloud":云端 hub 执行(电脑关机也会跑)

调度器只推进与自身 location 一致的任务;另一种位置的任务保持原样,供同步到对端。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# 错过超过 1 小时就跳过,不在开机时一口气补跑。
MISSED_SKIP_SECONDS = 3600
VALID_KINDS = ("remind", "execute")
VALID_SCHEDULES = ("once", "daily", "weekly")
VALID_LOCATIONS = ("local", "cloud")
VALID_CHANNELS = ("feishu", "weixin")
DEFAULT_TIMEZONE = "Asia/Shanghai"
# 定义字段来自 env(本地是唯一编辑入口);进度字段来自文件(云端是唯一执行者)
_DEFINITION_FIELDS = (
    "name", "kind", "schedule", "time", "days", "datetime",
    "timezone", "location", "channel", "channel_target", "channel_account_id", "channel_receive_id_type", "channel_user_id", "text", "enabled",
)
_PROGRESS_FIELDS = ("done", "last_run", "last_status", "last_message")
# 签名故意不含 enabled:once 任务触发后 _advance 会把 enabled 置 false,
# 但本地 env 里还是 true(本地从不执行 cloud 任务)。若把 enabled 算进签名,
# 已触发的 once 任务会被误判成"定义变了"而重新触发。
_SIGNATURE_FIELDS = (
    "name", "kind", "schedule", "time", "days", "datetime",
    "timezone", "location", "channel", "channel_target", "channel_account_id", "channel_receive_id_type", "channel_user_id", "text",
)


def _definition_signature(task: dict) -> tuple:
    return tuple(str(task.get(field)) for field in _SIGNATURE_FIELDS)


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour, _, minute = (str(value or "")).partition(":")
    h = int(hour or 0)
    m = int(minute or 0)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("时间必须是 HH:MM 格式(0-23 时,0-59 分)")
    return h, m


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(tz or DEFAULT_TIMEZONE))
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def next_time_epoch(hhmm: str, tz: str, after_epoch: float) -> float:
    """下一个 HH:MM(在时区 tz)对应的绝对时间,严格晚于 after_epoch。"""
    zone = _zone(tz)
    hour, minute = _parse_hhmm(hhmm)
    after = datetime.fromtimestamp(after_epoch, zone)
    candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def next_weekly_epoch(hhmm: str, days: list[int], tz: str, after_epoch: float) -> float:
    """下一个落在 days(0=周一..6=周日)且为 HH:MM 的绝对时间,严格晚于 after_epoch。"""
    zone = _zone(tz)
    hour, minute = _parse_hhmm(hhmm)
    after = datetime.fromtimestamp(after_epoch, zone)
    valid_days = {int(day) for day in days if int(day) in range(7)}
    if not valid_days:
        raise ValueError("星期列表为空或无效")
    for offset in range(8):
        candidate = (after + timedelta(days=offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0,
        )
        if candidate.weekday() in valid_days and candidate > after:
            return candidate.timestamp()
    raise ValueError("无法计算下一次执行时间")


def once_epoch(datetime_str: str, tz: str) -> float:
    """把 ISO 本地时间(如 2026-08-25T14:50:00)按时区 tz 转成绝对时间。"""
    zone = _zone(tz)
    value = (datetime_str or "").strip()
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone)
    return dt.timestamp()


def _same_schedule(left: dict, right: dict) -> bool:
    for key in ("kind", "schedule", "time", "days", "datetime", "timezone", "location", "channel", "channel_target", "channel_account_id", "channel_receive_id_type", "channel_user_id", "text"):
        if left.get(key) != right.get(key):
            return False
    return True


class Scheduler:
    """任务表 + 到点判定。纯逻辑,不自己起循环(由 server 的 asyncio 循环驱动)。"""

    def __init__(
        self,
        storage_path: str | Path,
        *,
        location: str = "local",
        default_timezone: str = DEFAULT_TIMEZONE,
        now_fn=None,
    ):
        self.storage_path = Path(storage_path)
        self.location = location if location in VALID_LOCATIONS else "local"
        self.default_timezone = default_timezone or DEFAULT_TIMEZONE
        self._now_fn = now_fn or time.time
        self._lock = threading.RLock()
        self.tasks: list[dict] = []
        self.load()

    def now(self) -> float:
        return float(self._now_fn())

    # ---- 存储 ----
    def load(self) -> None:
        """读环境变量 + 本地文件,按 id 合并。

        env 是本地编辑的同步来源(sync_runtime_settings 写 AGENT_SCHEDULE_TASKS),
        但它推送的是本地视图,云端触发后把 done=true 只写进文件、不回写 env。
        以前 env 永远盖过文件 → hub 每次重启又把 env 里 done=false 的 once 任务
        重新触发一遍。现在按 id 合并:定义字段取 env,进度字段(done/last_run/...)
        取文件;本地删掉的任务(env 里没有)丢弃;定义变了就重算 next_run。
        """
        env_tasks: list[dict] | None = None
        env = os.getenv("AGENT_SCHEDULE_TASKS")
        if env:
            try:
                data = json.loads(env)
                if isinstance(data, list):
                    env_tasks = data
            except (json.JSONDecodeError, ValueError):
                env_tasks = None

        file_tasks: list[dict] | None = None
        try:
            if self.storage_path.exists():
                data = json.loads(self.storage_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    file_tasks = data
        except (OSError, json.JSONDecodeError, ValueError):
            file_tasks = None

        # 云端网页直接编辑后，文件是同一份同步输入的最新结果。只有桌面推来
        # 新的环境任务表时才重新合并；避免重启撤销网页的新增、修改和删除。
        if self.location == "cloud" and env_tasks is not None and file_tasks is not None:
            marker = self.storage_path.with_suffix(".sync-source.sha256")
            try:
                if marker.read_text() == hashlib.sha256(env.encode()).hexdigest():
                    env_tasks = None
            except OSError:
                pass
        if env_tasks is None:
            self.tasks = file_tasks or []
            self._recompute_all()
            return

        file_by_id = {
            str(task.get("id")): task
            for task in (file_tasks or [])
            if isinstance(task, dict) and task.get("id")
        }
        merged: list[dict] = []
        for env_task in env_tasks:
            if not isinstance(env_task, dict):
                continue
            task = dict(env_task)
            file_task = file_by_id.get(str(task.get("id") or ""))
            if file_task is not None and _definition_signature(task) == _definition_signature(file_task):
                # 定义没变:进度字段从文件恢复,next_run 也沿用文件(不重算)
                for field in _PROGRESS_FIELDS:
                    if field in file_task:
                        task[field] = file_task[field]
                task["next_run"] = file_task.get("next_run")
            else:
                # 新任务或定义变了(如改了时间):进度清零,next_run 交给 _recompute_all 重算
                task["done"] = False
                task["last_run"] = None
                task["last_status"] = None
                task["last_message"] = ""
                task["next_run"] = None
            merged.append(task)
        self.tasks = merged
        self._recompute_all()

    def save(self) -> None:
        payload = json.dumps(self.tasks, ensure_ascii=False)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.storage_path.with_suffix(".tmp")
        with self._lock:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self.storage_path)
            if self.location == "cloud" and (source := os.getenv("AGENT_SCHEDULE_TASKS")):
                marker = self.storage_path.with_suffix(".sync-source.sha256")
                marker.write_text(hashlib.sha256(source.encode()).hexdigest())

    def _recompute_all(self) -> None:
        now = self.now()
        for task in self.tasks:
            if (task.get("next_run") is None and task.get("enabled", True)
                    and not task.get("done", False)):
                task["next_run"] = self.compute_next_run(task, now)

    # ---- 任务 CRUD ----
    def _normalize(self, raw: dict, now: float | None = None) -> dict:
        now = self.now() if now is None else now
        kind = raw.get("kind") if raw.get("kind") in VALID_KINDS else "remind"
        schedule = raw.get("schedule") if raw.get("schedule") in VALID_SCHEDULES else "daily"
        days = [int(day) for day in (raw.get("days") or []) if int(day) in range(7)]
        task = {
            "id": str(raw.get("id") or uuid.uuid4().hex[:12]),
            "name": str(raw.get("name") or "").strip() or "未命名任务",
            "kind": kind,
            "schedule": schedule,
            "time": str(raw.get("time") or "09:00"),
            "days": sorted(set(days)),
            "datetime": str(raw.get("datetime") or ""),
            "timezone": str(raw.get("timezone") or self.default_timezone),
            "location": raw.get("location") if raw.get("location") in VALID_LOCATIONS else "local",
            # Legacy tasks did not have a channel; keep their historical Feishu delivery.
            "channel": raw.get("channel") if raw.get("channel") in VALID_CHANNELS else "feishu",
            "channel_target": str(raw.get("channel_target") or "").strip(),
            "channel_account_id": str(raw.get("channel_account_id") or "").strip(),
            "channel_receive_id_type": raw.get("channel_receive_id_type") if raw.get("channel_receive_id_type") in {"open_id", "chat_id"} else "open_id",
            "channel_user_id": str(raw.get("channel_user_id") or "").strip(),
            "text": str(raw.get("text") or "").strip(),
            "enabled": bool(raw.get("enabled", True)),
            "next_run": None,
            "last_run": None,
            "last_status": None,
            "last_message": "",
            "created_at": float(raw.get("created_at") or now),
            "done": bool(raw.get("done", False)),
        }
        _parse_hhmm(task["time"])
        if schedule == "weekly" and not task["days"]:
            task["days"] = [0]
        if schedule == "once":
            if not task["datetime"]:
                raise ValueError("一次性任务必须指定执行时间(datetime)")
            once_epoch(task["datetime"], task["timezone"])  # 校验格式
        if not task["text"]:
            raise ValueError("任务内容(text)不能为空")
        task["next_run"] = self.compute_next_run(task, now)
        return task

    def compute_next_run(self, task: dict, after_epoch: float) -> float | None:
        if task.get("done"):
            return None
        if task["schedule"] == "once":
            return once_epoch(task["datetime"], task["timezone"])
        if task["schedule"] == "daily":
            return next_time_epoch(task["time"], task["timezone"], after_epoch)
        return next_weekly_epoch(task["time"], task["days"], task["timezone"], after_epoch)

    def list_tasks(self) -> list[dict]:
        with self._lock:
            return [dict(task) for task in self.tasks]

    def add_task(self, **raw) -> dict:
        with self._lock:
            task = self._normalize(raw)
            self.tasks.append(task)
            self.save()
        return dict(task)

    def update_task(self, task_id: str, **raw) -> dict | None:
        with self._lock:
            for index, task in enumerate(self.tasks):
                if task["id"] != task_id:
                    continue
                updated = self._normalize({**task, **raw, "id": task_id})
                # 保留原执行进度(定义未变时不重置 next_run)
                if _same_schedule(task, updated):
                    updated["next_run"] = task.get("next_run")
                    updated["last_run"] = task.get("last_run")
                    updated["done"] = task.get("done", False)
                    updated["enabled"] = updated.get("enabled", task.get("enabled", True))
                self.tasks[index] = updated
                self.save()
                return dict(updated)
            return None

    def remove_task(self, task_id: str) -> bool:
        with self._lock:
            before = len(self.tasks)
            self.tasks = [task for task in self.tasks if task["id"] != task_id]
            if len(self.tasks) != before:
                self.save()
                return True
            return False

    def set_enabled(self, task_id: str, enabled: bool) -> bool:
        with self._lock:
            for task in self.tasks:
                if task["id"] == task_id:
                    task["enabled"] = bool(enabled)
                    if enabled and not task.get("done"):
                        task["next_run"] = self.compute_next_run(task, self.now())
                    self.save()
                    return True
            return False

    def record_fire_result(self, task_id: str, status: str, message: str = "") -> bool:
        """把一次触发的结果写回任务(如 blocked=被确认闸门拦下)。"""
        with self._lock:
            for task in self.tasks:
                if task["id"] == task_id:
                    task["last_status"] = status
                    task["last_message"] = message
                    self.save()
                    return True
            return False

    # ---- 到点判定 ----
    def tick(self, now_epoch: float | None = None) -> list[dict]:
        """推进时钟;返回本轮应触发的任务快照(不含"已跳过"的任务)。"""
        now = self.now() if now_epoch is None else now_epoch
        due: list[dict] = []
        with self._lock:
            changed = False
            for task in self.tasks:
                if not task.get("enabled", True) or task.get("done", False):
                    continue
                if task["location"] != self.location:
                    continue  # 另一种位置的任务留给对端执行,这里不动
                next_run = task.get("next_run")
                if next_run is None or next_run > now:
                    continue
                if now - next_run > MISSED_SKIP_SECONDS:
                    task["last_status"] = "skipped"
                    task["last_message"] = "错过执行时间点(超过 1 小时),已跳过"
                    task["last_run"] = next_run
                    self._advance(task, now)
                    changed = True
                    continue
                task["last_run"] = now
                task["last_status"] = "ok"
                task["last_message"] = ""
                self._advance(task, now)
                changed = True
                snapshot = dict(task)
                snapshot["_due_time"] = next_run  # 原定触发时刻,供迟到说明用(不落盘)
                due.append(snapshot)
            if changed:
                self.save()
        return due

    def _advance(self, task: dict, now: float) -> None:
        if task["schedule"] == "once":
            task["done"] = True
            task["enabled"] = False
            task["next_run"] = None
        else:
            task["next_run"] = self.compute_next_run(task, now)

    def sync_payload(self) -> str:
        with self._lock:
            return json.dumps(self.tasks, ensure_ascii=False)
