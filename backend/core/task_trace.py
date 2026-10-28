"""Bounded, display-only task trails; never used as model context."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

ACTIVE_STATUSES = {"running", "waiting"}
MAX_EVENTS = 160
MAX_BYTES = 96 * 1024
MAX_RAW_STEP_BYTES = 128 * 1024
MAX_RAW_TASK_BYTES = 2 * 1024 * 1024


def new_trace(task_id: str, title: str) -> dict:
    return {"id": task_id, "title": str(title or "处理附件")[:80],
            "status": "running", "started_at": int(time.time() * 1000),
            "duration_ms": 0, "step_count": 0, "has_tools": False,
            "events": [], "omitted": 0, "current": "正在准备回复"}


def _text(value, limit=1600) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text[:limit] + ("…（内容已截断）" if len(text) > limit else "")


def _bound(trace: dict) -> None:
    events = trace["events"]
    # ponytail: bound display history, not the agent's execution or existing audit log.
    while len(events) > MAX_EVENTS or (len(events) > 1 and
            len(json.dumps(trace, ensure_ascii=False).encode("utf-8")) > MAX_BYTES):
        events.pop(0)
        trace["omitted"] += 1


def store_return(trace: dict, result, directory: Path) -> dict:
    """Store an already-redacted return separately; never expand chat/model context."""
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
    payload = text.encode("utf-8")
    remaining = max(0, MAX_RAW_TASK_BYTES - trace.get("raw_bytes", 0))
    if not remaining:
        return {"error": "本任务原始返回记录已达 2 MiB 上限，后续返回未保存。"}
    limited = payload[:min(MAX_RAW_STEP_BYTES, remaining)].decode("utf-8", errors="ignore").encode("utf-8")
    name = "trace-" + uuid.uuid4().hex + ".txt"
    path = directory / name
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_bytes(limited)
    except OSError:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return {"error": "原始返回保存失败，请检查磁盘空间和目录权限。"}
    trace["raw_bytes"] = trace.get("raw_bytes", 0) + len(limited)
    return {"saved_name": name, "bytes": len(limited),
            "truncated": len(limited) < len(payload)}


def consume(trace: dict, kind: str, data: dict) -> bool:
    """Consume only completed messages and meaningful steps, never token deltas/media."""
    now = int(time.time() * 1000)
    event = None
    if kind == "llm_timing":
        elapsed = max(0, int(data.get("llm_ms", 0)))
        trace["llm_ms"] = trace.get("llm_ms", 0) + elapsed
        rounds = trace.setdefault("model_timings", [])
        rounds.append({"step": data.get("step"), "llm_ms": elapsed})
        del rounds[:-MAX_EVENTS]
        trace["current"] = "正在整理结果"
    elif kind == "llm_retry":
        trace["current"] = _text(data.get("text") or "网关暂不可用，正在重试", 240)
        event = {"kind": "notice", "text": trace["current"], "ts": now,
                 "attempt": data.get("attempt"), "status_code": data.get("status_code"),
                 "elapsed_ms": data.get("elapsed_ms"), "empty_stream": data.get("empty_stream")}
    elif kind == "tool_call":
        trace["has_tools"] = True
        trace["step_count"] += 1
        trace["status"] = "running"
        trace["current"] = str(data.get("name") or "执行操作")[:80]
        args = data.get("arguments") or {}
        hint = next((args.get(key) for key in ("name", "url", "path", "command", "text")
                     if isinstance(args, dict) and args.get(key)), "")
        event = {"kind": "tool", "step": trace["step_count"], "name": trace["current"], "hint": _text(hint, 240),
                 "status": "running", "ts": now}
    elif kind == "tool_result":
        trace["status"] = "running"
        trace["current"] = "正在整理结果"
        item = next((item for item in reversed(trace["events"])
                     if item["kind"] == "tool" and item["name"] == data.get("name")
                     and item["status"] in {"running", "waiting", "stopped"}), None)
        if item is not None:
            item["status"] = "skipped" if data.get("skipped") else "done" if data.get("ok", True) else "failed"
            item["duration_ms"] = max(0, now - item["ts"])
            if "tool_ms" in data:
                item["tool_ms"] = max(0, int(data["tool_ms"]))
                trace["tool_ms"] = trace.get("tool_ms", 0) + item["tool_ms"]
            if data.get("raw_return"):
                item["raw_return"] = data["raw_return"]
            if not data.get("ok", True):
                item["detail"] = _text(data.get("result") or "执行失败")
            # Retain a short human result when tools explicitly supply one, not raw dumps.
            result = data.get("result")
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except (ValueError, TypeError):
                    result = None
            if isinstance(result, dict) and result.get("summary"):
                item["summary"] = _text(result["summary"], 600)
    elif kind in {"message", "message_end"} and data.get("content"):
        event = {"kind": "message", "text": _text(data["content"], 6000), "ts": now}
    elif kind == "confirm_needed":
        trace["status"] = "waiting"
        trace["current"] = "等待你确认操作"
        if trace["events"] and trace["events"][-1]["kind"] == "tool":
            trace["events"][-1]["status"] = "waiting"
    elif kind == "error":
        event = {"kind": "error", "text": _text(data.get("msg") or "执行出错"), "ts": now}
    elif kind == "interjection_applied":
        trace["status"] = "running"
        trace["current"] = "正在按插话调整任务"
        event = {"kind": "notice", "text": "已采用新的补充要求，保留已完成进度。", "ts": now}
    elif kind == "notice":
        event = {"kind": "notice", "text": _text(data.get("text") or ""), "ts": now}
    elif kind == "screenshot":
        event = {"kind": "notice", "text": "已查看屏幕（过程截图不长期保存）", "ts": now}
    elif kind == "progress" and data.get("cad_wait"):
        trace["current"] = _text(data["cad_wait"], 240)
    else:
        return False
    if event:
        trace["events"].append(event)
    trace["updated_at"] = now
    trace["duration_ms"] = max(0, now - trace["started_at"])
    _bound(trace)
    return True


def finish(trace: dict, status: str, *, final_reply: str = "") -> None:
    trace["status"] = status
    trace["finished_at"] = int(time.time() * 1000)
    trace["duration_ms"] = max(0, trace["finished_at"] - trace["started_at"])
    trace["current"] = ""
    # The final reply lives outside the trail; intermediate assistant messages stay inside.
    if final_reply and trace["events"] and trace["events"][-1]["kind"] == "message":
        trace["events"].pop()
    for item in trace["events"]:
        if item.get("status") in ACTIVE_STATUSES:
            item["status"] = "interrupted" if status == "interrupted" else "stopped"


def recover(messages: list[dict]) -> None:
    """Only call on disk load: a process restart must not pretend old tasks still run."""
    for message in messages:
        trace = message.get("task_trace") if isinstance(message, dict) else None
        if isinstance(trace, dict) and trace.get("status") in ACTIVE_STATUSES:
            trace["status"] = "interrupted"
            trace["current"] = "上次执行中断"
            trace["finished_at"] = trace.get("updated_at", trace.get("started_at", 0))
            for event in trace.get("events", []):
                if event.get("status") in ACTIVE_STATUSES:
                    event["status"] = "interrupted"
