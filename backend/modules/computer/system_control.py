"""系统控制模块:打开应用 / 打开网址 / 运行命令 / 窗口管理。

这是让 Agent "能操作几乎所有软件"的第一层能力——先把软件打开、把窗口切到前台,
后续再用截屏+视觉(第三层)或控件树(第二层)去操作里面的内容。

打开应用的策略(由可靠到兜底):
1. 直接是可执行文件路径 -> 直接启动
2. 在开始菜单快捷方式里按名字模糊匹配(覆盖绝大多数已安装软件)
3. 交给 Windows 的 `start` 让系统自己找(能开 notepad、calc 等)

Windows 专属;非 Windows 下 register_tools 会跳过。
"""
from __future__ import annotations

import asyncio
import contextvars
from contextlib import nullcontext
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable, TypeVar

from backend.core.config import subprocess_env_without_secrets
from backend.core.process import hidden_subprocess_kwargs
from backend.core.tools import ToolRegistry
from backend.core import isolation_workspace
from backend.modules.computer.escape_key import ensure_listener, stop_listener

_IS_WINDOWS = sys.platform == "win32"
_CONTROL_LOCK = threading.Lock()
_CONTROL_STATE_LOCK = threading.Lock()
_CONTROL_OWNER: ControlToken | None = None
_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

# Keep application ownership inside the current agent task. A later CAD tool
# can distinguish an AutoCAD instance opened by XiaoA from one already open.
_OWNED_APPLICATIONS: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "owned_applications", default=frozenset(),
)
_OWNED_APPLICATION_PIDS: contextvars.ContextVar[dict[str, int]] = contextvars.ContextVar(
    "owned_application_pids", default={},
)

# The UI notice is scoped to the current agent task. Context variables are
# copied into asyncio.to_thread(), so synchronous desktop helpers get the same
# notification without coupling this low-level module to the WebSocket server.
_ForegroundNotice = Callable[[bool], None]
_FOREGROUND_NOTICE: contextvars.ContextVar[_ForegroundNotice | None] = contextvars.ContextVar(
    "foreground_notice", default=None,
)
_CONTROL_SESSION: contextvars.ContextVar[ControlSession | None] = contextvars.ContextVar(
    "control_session", default=None,
)


# Optional task-scoped guard, installed only by bounded desktop plans. Original
# individual tools retain their existing focus-recovery behavior.
INPUT_PRECONDITION: contextvars.ContextVar[Callable[..., None] | None] = contextvars.ContextVar(
    "desktop_input_precondition", default=None,
)


def set_foreground_notice_callback(callback: _ForegroundNotice | None, *, on_escape=None):
    """Install a task-local callback and return its context token."""
    return (_FOREGROUND_NOTICE.set(callback), _CONTROL_SESSION.set(
        ControlSession(on_escape) if on_escape is not None else None,
    ))


def reset_foreground_notice_callback(token) -> None:
    session = _CONTROL_SESSION.get()
    if session is not None:
        session.revoked.set()
        if session.control is not None and _CONTROL_OWNER is session.control:
            release_control(session.control, force=True)
    _CONTROL_SESSION.reset(token[1])
    _FOREGROUND_NOTICE.reset(token[0])


class ControlSession:
    """One task keeps desktop ownership across its input, screenshot and thinking steps."""
    def __init__(self, on_escape):
        self.control = None
        self.revoked = threading.Event()
        self.on_escape = on_escape
        self.owned_applications: dict[str, int | None] = {}
        self.sends: dict[str, tuple[float, str]] = {}
        self.message_inputs: set[tuple[int, str]] = set()


def check_foreground_permission() -> None:
    session = _CONTROL_SESSION.get()
    if session is not None and session.revoked.is_set():
        raise RuntimeError("桌面控制权已交还用户，本任务不能再次操作鼠标键盘")


def _escape_foreground(expected: ControlToken | None = None) -> None:
    with _CONTROL_STATE_LOCK:
        control = _CONTROL_OWNER
        if expected is not None and control is not expected:
            return  # A late event from an old helper cannot stop a new task.
        session = control.session if control is not None else None
        if session is None or not control.foreground:
            return
        first_escape = not session.revoked.is_set()
        session.revoked.set()
    release_control(control, force=True)
    if first_escape:
        try:
            session.on_escape()
        except Exception:
            _LOGGER.exception("failed to stop task after Esc")


def mark_owned_application(name: str, process_id: int | None = None) -> None:
    key = str(name or "").strip().lower()
    if key:
        session = _CONTROL_SESSION.get()
        if session is not None:
            session.owned_applications[key] = int(process_id) if process_id is not None else None
            return
        _OWNED_APPLICATIONS.set(_OWNED_APPLICATIONS.get() | {key})
        if process_id is not None and int(process_id) > 0:
            _OWNED_APPLICATION_PIDS.set({**_OWNED_APPLICATION_PIDS.get(), key: int(process_id)})


def owned_application(name: str) -> bool:
    key = str(name or "").strip().lower()
    session = _CONTROL_SESSION.get()
    return key in session.owned_applications if session is not None else key in _OWNED_APPLICATIONS.get()


def owned_application_pid(name: str) -> int | None:
    key = str(name or "").strip().lower()
    session = _CONTROL_SESSION.get()
    return session.owned_applications.get(key) if session is not None else _OWNED_APPLICATION_PIDS.get().get(key)


def clear_owned_application(name: str) -> None:
    key = str(name or "").strip().lower()
    if key:
        session = _CONTROL_SESSION.get()
        if session is not None:
            session.owned_applications.pop(key, None)
            return
        _OWNED_APPLICATIONS.set(_OWNED_APPLICATIONS.get() - {key})
        _OWNED_APPLICATION_PIDS.set({item: pid for item, pid in _OWNED_APPLICATION_PIDS.get().items() if item != key})

def _notify_foreground(token: ControlToken, active: bool) -> None:
    callback = token.notice_callback
    if callback is None:
        return
    try:
        callback(bool(active))
    except Exception:
        # A UI notification must never make a desktop action fail.
        _LOGGER.debug("foreground notice callback failed", exc_info=True)


class ControlToken:
    """Capability token for one foreground-control lease."""

    __slots__ = ("worker", "release_requested", "notice_callback", "session", "foreground", "abandoned", "actions", "worker_isolated")

    def __init__(self) -> None:
        self.worker: asyncio.Task[Any] | None = None
        self.release_requested = False
        self.notice_callback = _FOREGROUND_NOTICE.get()
        self.session = _CONTROL_SESSION.get()
        self.foreground = False
        self.abandoned = False
        self.actions = None
        self.worker_isolated = False


# 用户叫法和真实窗口标题并不总一致（Codex 桌面版当前显示为 ChatGPT）。
_WINDOW_ALIASES = {
    "微信": ("微信", "wechat", "weixin"),
    "wechat": ("微信", "wechat", "weixin"),
    "weixin": ("微信", "wechat", "weixin"),
    "飞书": ("飞书", "feishu", "lark"),
    "feishu": ("飞书", "feishu", "lark"),
    "钉钉": ("钉钉", "dingtalk"),
    "qq": ("qq", "腾讯qq"),
    "vscode": ("visual studio code", "vscode", "code"),
    "code": ("visual studio code", "vscode", "code"),
    "wps": ("wps", "wps office", "wps 文字", "wps 表格", "wps 演示"),
    "word": ("word", "microsoft word", "wps 文字", ".docx - wps", ".doc - wps"),
    "excel": ("excel", "microsoft excel", "wps 表格", ".xlsx - wps", ".xls - wps"),
    "ppt": ("powerpoint", "wps 演示", ".pptx - wps", ".ppt - wps"),
    "codex": ("codex", "chatgpt"),
    "chatgpt": ("chatgpt", "codex"),
    "小a": ("小a", "ai 办公助手", "xiaoa"),
    "xiaoa": ("xiaoa", "小a", "ai 办公助手"),
    "cad": ("cad", "autocad", "autodesk autocad"),
    "autocad": ("autocad", "autodesk autocad"),
}


def window_queries(title: str) -> tuple[str, ...]:
    """展开窗口别名，保持用户原词优先。"""
    key = title.strip().lower()
    values = _WINDOW_ALIASES.get(key, (key,))
    return tuple(dict.fromkeys(value.lower() for value in values if value))


def window_hidden(window: Any) -> bool:
    try:
        import win32gui
        return bool(win32gui.IsWindow(int(window._hWnd))) and not bool(win32gui.IsWindowVisible(int(window._hWnd)))
    except Exception:
        return False


_HIDDEN_HELPER_WORDS = (
    "关于", "about", "输入法", "ime", "updater", "helper", "crash", "notification",
)
_KNOWN_WINDOW_PROCESSES = {
    "微信": {"wechat.exe", "weixin.exe"}, "wechat": {"wechat.exe", "weixin.exe"},
    "weixin": {"wechat.exe", "weixin.exe"},
    "飞书": {"feishu.exe", "lark.exe"}, "feishu": {"feishu.exe", "lark.exe"},
    "钉钉": {"dingtalk.exe"}, "qq": {"qq.exe"},
    "vscode": {"code.exe"}, "code": {"code.exe"},
    "wps": {"wps.exe", "et.exe", "wpp.exe"},
    "word": {"winword.exe"}, "excel": {"excel.exe"}, "ppt": {"powerpnt.exe"},
    "codex": {"codex.exe", "chatgpt.exe"}, "chatgpt": {"codex.exe", "chatgpt.exe"},
    "小a": {"xiaoa.exe"}, "xiaoa": {"xiaoa.exe"},
}


def _normal_window_rect(hwnd: int, raw_rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Use rcNormalPosition only for iconic/off-screen geometry checks and reporting."""
    left, top, right, bottom = raw_rect
    if right - left >= 180 and bottom - top >= 120 and min(left, top) > -20000:
        return raw_rect
    try:
        import win32gui
        placement = win32gui.GetWindowPlacement(hwnd)
        normal = placement[4] if placement and len(placement) > 4 else None
        if normal and len(normal) == 4:
            nleft, ntop, nright, nbottom = map(int, normal)
            if nright - nleft >= 180 and nbottom - ntop >= 120:
                return nleft, ntop, nright, nbottom
    except Exception:
        pass
    return raw_rect


def _hidden_process_matches(title: str, queries: tuple[str, ...], process_name: str) -> bool:
    normalized_title = re.sub(r"[\W_]+", "", title.casefold())
    normalized_queries = {re.sub(r"[\W_]+", "", q.casefold()) for q in queries}
    if normalized_title in normalized_queries:
        return True
    known_processes = set()
    for query in queries:
        known_processes.update(_KNOWN_WINDOW_PROCESSES.get(query.casefold(), set()))
    return bool(process_name and process_name.casefold() in known_processes)


def find_hidden_main_windows(gw: Any, title: str = "", handle: int | None = None, *, exact: bool = False) -> list[Any]:
    """Discover only explicitly requested hidden top-level app windows; never activate here."""
    if handle is None and not title.strip():
        return []
    try:
        import win32gui
        queries = window_queries(title)
        matches = []
        excluded = {"ime", "msctfime ui", "default ime", "base_powermessagewindow",
                    "wxtrayiconmessagewindow", "systemmessagewindow", "chrome_systemmessagewindow",
                    "displayicc_systemmessagewindow", "shell_traywnd", "shell_secondarytraywnd",
                    "progman", "workerw", "tooltips_class32"}
        def visit(hwnd, _):
            try:
                if win32gui.IsWindowVisible(hwnd) or (handle is not None and hwnd != handle):
                    return
                name = win32gui.GetWindowText(hwnd).strip()
                kind = win32gui.GetClassName(hwnd).casefold()
                if not name or kind in excluded or name.casefold() in excluded:
                    return
                lowered_name = name.casefold()
                if any(word in lowered_name for word in (*_SECONDARY, *_HIDDEN_HELPER_WORDS)):
                    return
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                normal_left, normal_top, normal_right, normal_bottom = _normal_window_rect(
                    hwnd, (left, top, right, bottom),
                )
                if normal_right - normal_left < 180 or normal_bottom - normal_top < 120:
                    return
                window = gw.Window(hwnd)
                if handle is None:
                    # Hidden title substring matches are too broad: require an
                    # exact alias or a known process for the requested app.
                    process_name = "" if exact else _window_process_name(window)
                    if not _hidden_process_matches(name, queries, process_name):
                        return
                elif int(hwnd) != int(handle):
                    return
                matches.append(window)
            except Exception:
                return  # A window may disappear during enumeration.
        win32gui.EnumWindows(visit, None)
        exact_matches = [w for w in matches if w.title.strip().casefold() in queries]
        return exact_matches or matches
    except (ImportError, AttributeError, OSError):
        return []


def window_info(window: Any) -> dict[str, Any]:
    """把 pygetwindow 对象压成有界、可序列化的结果。"""
    handle = int(getattr(window, "_hWnd", 0) or 0)
    left = int(getattr(window, "left", 0) or 0)
    top = int(getattr(window, "top", 0) or 0)
    width = max(0, int(getattr(window, "width", 0) or 0))
    height = max(0, int(getattr(window, "height", 0) or 0))
    minimized = bool(getattr(window, "isMinimized", False))
    if handle:
        try:
            import win32gui
            raw = tuple(map(int, win32gui.GetWindowRect(handle)))
            minimized = minimized or min(raw[0], raw[1]) <= -20000 or bool(getattr(win32gui, "IsIconic", lambda _h: False)(handle))
            normal = _normal_window_rect(handle, raw)
            if normal != raw and (minimized or min(raw[0], raw[1]) <= -20000):
                left, top, right, bottom = normal
                width, height = max(0, right - left), max(0, bottom - top)
            elif raw:
                left, top, right, bottom = raw
                width, height = max(0, right - left), max(0, bottom - top)
            minimized = minimized or min(left, top) <= -20000
        except Exception:
            minimized = minimized or min(left, top) <= -20000
    return {
        "handle": handle,
        "title": str(getattr(window, "title", "") or "")[:300],
        "active": bool(getattr(window, "isActive", False)),
        "minimized": minimized,
        "hidden": window_hidden(window),
        "rect": {
            "x": left, "y": top, "width": width, "height": height,
        },
    }


# 绝不允许关小A 自己:小A 前端是 Electron,后端是 python;标题里也可能带"小A"。
_PROTECTED_FRAGMENTS = ("xiaoa", "小a", "ai 办公助手", "electron", "python", "pythonw")


def _is_protected(title: str = "", process_name: str = "") -> str | None:
    """返回命中的保护词;没命中返回 None。绝不允许关小A 自己(xiaoa/electron/python)。"""
    for frag in _PROTECTED_FRAGMENTS:
        if frag in (title or "").lower():
            return frag
        if frag in (process_name or "").lower():
            return frag
    return None


def _window_process_name(window: Any) -> str:
    """取窗口所属进程名(小写,含 .exe)。拿不到返回空串。"""
    try:
        import win32process
        hwnd = int(getattr(window, "_hWnd", 0) or 0)
        if not hwnd:
            return ""
        _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return ""
        import psutil
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


def _process_ids_by_name(name: str) -> set[int]:
    try:
        import psutil
        return {
            int(process.info["pid"])
            for process in psutil.process_iter(["pid", "name"])
            if str(process.info.get("name") or "").lower() == name.lower()
        }
    except Exception:
        return set()


def _post_close(window: Any) -> bool:
    """向窗口发 WM_CLOSE(礼貌关闭,不 kill)。"""
    try:
        import ctypes
        hwnd = int(getattr(window, "_hWnd", 0) or 0)
        if not hwnd:
            return False
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        return True
    except Exception:
        return False


def _window_still_exists(window: Any) -> bool:
    """窗口是否还在。拿不到就当还在,绝不误报已关。"""
    try:
        import pygetwindow as gw
        hwnd = int(getattr(window, "_hWnd", 0) or 0)
        return any(int(getattr(w, "_hWnd", 0) or 0) == hwnd for w in gw.getAllWindows())
    except Exception:
        return True


def _find_window_by_process(gw: Any, target: str) -> Any | None:
    """按精确进程名(小写,含 .exe)找窗口,不做模糊匹配。"""
    for window in gw.getAllWindows():
        if _window_process_name(window) == target:
            return window
    return None


def _kill_window_process(window: Any) -> bool:
    """强杀窗口所属进程。仅在 force=true 且已通过确认闸门时调用。"""
    try:
        import win32process
        hwnd = int(getattr(window, "_hWnd", 0) or 0)
        if not hwnd:
            return False
        _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return False
        import psutil
        psutil.Process(pid).kill()
        return True
    except Exception:
        return False


def resolve_window(gw: Any, title: str = "", handle: int | None = None, *, exact: bool = False,
                   unique: bool = False) -> Any | None:
    """按句柄或标题找窗口；句柄优先，标题支持 Codex -> ChatGPT 等别名。"""
    windows = list(gw.getAllWindows())
    if handle is not None:
        matches = [w for w in windows if int(getattr(w, "_hWnd", 0) or 0) == handle]
        matches = matches or find_hidden_main_windows(gw, title, handle, exact=exact)
    else:
        queries = window_queries(title)
        if not queries:
            return None
        if unique:
            matches = [w for w in windows if str(w.title).strip().casefold() in queries]
            if not matches and not exact:
                matches = [w for w in windows if any(q in str(w.title).casefold() for q in queries)]
            if not matches:
                matches = find_hidden_main_windows(gw, title, exact=exact)
            return matches[0] if len(matches) == 1 else None
        # Preserve query priority and Z-order: do not aggregate aliases before
        # choosing a result, which is important for multi-window applications.
        matches = []
        for query in queries:
            matches = [w for w in windows if str(w.title).strip().casefold() == query]
            if matches:
                break
        if not matches and not exact:
            for query in queries:
                matches = [w for w in windows if query in str(w.title).casefold()]
                if matches:
                    break
        if not matches:
            matches = find_hidden_main_windows(gw, title, exact=exact)
    if unique and len(matches) != 1:
        return None
    return matches[0] if matches else None


def focus_window_object(window: Any, *, maximize: bool = False) -> dict[str, Any]:
    """Restore and activate through Win32 only; UIA calls belong in the isolated reader."""
    handle = int(getattr(window, "_hWnd", 0) or 0)
    if handle and window_hidden(window):
        import win32gui
        import win32con
        check_foreground_permission()
        win32gui.ShowWindow(handle, win32con.SW_SHOW)
        check_foreground_permission()
        win32gui.ShowWindow(handle, win32con.SW_RESTORE)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and not win32gui.IsWindowVisible(handle):
            check_foreground_permission()
            time.sleep(0.05)
    if bool(getattr(window, "isMinimized", False)):
        window.restore()
    check_foreground_permission()
    try:
        window.activate()
    except Exception:
        import win32gui
        if not handle or int(win32gui.GetForegroundWindow()) != handle:
            raise
    if maximize:
        window.maximize()
    return {"focused": True, "semantic": False, "window": window_info(window)}


def user_idle_seconds() -> float | None:
    """Windows 最后一次真实键鼠输入距今秒数；无法读取时返回 None。"""
    if not _IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now = int(ctypes.windll.kernel32.GetTickCount())
        return ((now - int(info.dwTime)) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return None


async def acquire_control(timeout: float = 8.0, *, foreground: bool = True) -> ControlToken | None:
    """跨所有小A任务的输入互斥锁，不阻塞事件循环。"""
    global _CONTROL_OWNER
    session = _CONTROL_SESSION.get()
    check_foreground_permission()
    if session is not None:
        if session.control is not None and _CONTROL_OWNER is session.control:
            if foreground:
                activate_foreground(session.control)
            return session.control
    deadline = time.monotonic() + max(0.1, min(float(timeout), 30.0))
    while time.monotonic() < deadline:
        check_foreground_permission()
        if _CONTROL_LOCK.acquire(blocking=False):
            if session is not None and session.revoked.is_set():
                _CONTROL_LOCK.release()
                check_foreground_permission()
            token = ControlToken()
            token.foreground = foreground
            with _CONTROL_STATE_LOCK:
                if foreground and session is not None:
                    try:
                        ensure_listener(lambda: _escape_foreground(token))
                    except Exception:
                        stop_listener()
                        _CONTROL_LOCK.release()
                        raise
                _CONTROL_OWNER = token
                if session is not None:
                    session.control = token
                if token.foreground:
                    _notify_foreground(token, True)
            return token
        await asyncio.sleep(0.05)
    return None


def acquire_control_sync(timeout: float = 8.0, *, foreground: bool = True) -> ControlToken | None:
    """Thread-side foreground-control acquisition for synchronous UI libraries."""
    global _CONTROL_OWNER
    session = _CONTROL_SESSION.get()
    check_foreground_permission()
    if session is not None:
        if session.control is not None and _CONTROL_OWNER is session.control:
            if foreground:
                activate_foreground(session.control)
            return session.control
    seconds = max(0.1, min(float(timeout), 30.0))
    if not _CONTROL_LOCK.acquire(timeout=seconds):
        return None
    if session is not None and session.revoked.is_set():
        _CONTROL_LOCK.release()
        check_foreground_permission()
    token = ControlToken()
    token.foreground = foreground
    with _CONTROL_STATE_LOCK:
        if foreground and session is not None:
            try:
                ensure_listener(lambda: _escape_foreground(token))
            except Exception:
                stop_listener()
                _CONTROL_LOCK.release()
                raise
        _CONTROL_OWNER = token
        if session is not None:
            session.control = token
        if token.foreground:
            _notify_foreground(token, True)
    return token


def activate_foreground(token: ControlToken) -> None:
    """Promote a silent UIA lease only when foreground keyboard/mouse input is needed."""
    check_foreground_permission()
    with _CONTROL_STATE_LOCK:
        if _CONTROL_OWNER is not token or (token.session is not None and token.session.revoked.is_set()):
            raise RuntimeError("前台控制 token 已失效")
        if token.session is not None:
            ensure_listener(lambda: _escape_foreground(token))
        if not token.foreground:
            token.foreground = True
            _notify_foreground(token, True)


def release_control(token: ControlToken, *, force: bool = False) -> bool:
    """Kill isolated native work before unlocking; never release a live thread fallback."""
    global _CONTROL_OWNER
    with _CONTROL_STATE_LOCK:
        if _CONTROL_OWNER is not token:
            _LOGGER.warning("ignored foreground-control release from non-owner")
            return False
        if token.foreground and token.session is not None and not force and not token.session.revoked.is_set():
            return True
        if token.actions is not None:
            if force and token.session is not None:
                token.session.revoked.set()
            token.actions.close()  # Terminate native input before releasing its lease.
            if token.worker_isolated:
                token.worker = None  # The killed pipe reader cannot affect a new lease.
        if token.worker is not None:
            token.release_requested = True
            if force:
                token.abandoned = True
                if token.session is not None:
                    token.session.revoked.set()
                _CONTROL_OWNER = None
                if token.foreground:
                    _notify_foreground(token, False)
                stop_listener()
            return True
        _CONTROL_OWNER = None
        if token.foreground:
            _notify_foreground(token, False)
        _CONTROL_LOCK.release()
        stop_listener()
    return True


def _control_worker_done(token: ControlToken, worker: asyncio.Task[Any]) -> None:
    """Finish a deferred release only after the executor worker has returned."""
    global _CONTROL_OWNER
    try:
        error = None if worker.cancelled() else worker.exception()
    except asyncio.CancelledError:
        error = None
    with _CONTROL_STATE_LOCK:
        if token.worker is not worker:
            return
        token.worker = None
        should_release = token.release_requested
        if token.abandoned:
            _CONTROL_LOCK.release()
        elif _CONTROL_OWNER is token and should_release:
            _CONTROL_OWNER = None
            if token.foreground:
                _notify_foreground(token, False)
            _CONTROL_LOCK.release()
            stop_listener()
    if error is not None and should_release:
        _LOGGER.warning("cancelled foreground worker failed during cleanup: %s", error)


def foreground_control_active() -> bool:
    with _CONTROL_STATE_LOCK:
        return _CONTROL_OWNER is not None and _CONTROL_OWNER.foreground


async def run_controlled(
    token: ControlToken, function: Callable[..., _T], /, *args: Any,
    timeout: float | None = None, **kwargs: Any,
) -> _T:
    """Run production input in a killable child; local callables retain a safe thread lease."""
    check_foreground_permission()
    with _CONTROL_STATE_LOCK:
        if _CONTROL_OWNER is not token:
            raise RuntimeError("前台控制 token 已失效")
        if token.worker is not None:
            raise RuntimeError("同一前台控制 token 已有操作在执行")
        from backend.modules.computer.action_worker import ActionWorker, describe
        action = describe(function, args, kwargs) if _IS_WINDOWS else None
        precondition = INPUT_PRECONDITION.get()
        if action is not None:
            descriptor = getattr(precondition, "descriptor", None)
            if precondition is not None and descriptor is None:
                raise RuntimeError("desktop_guard_not_serializable")
            if token.actions is None:
                token.actions = ActionWorker()
            worker = asyncio.create_task(asyncio.to_thread(token.actions.execute, action, descriptor))
        else:
            worker = asyncio.create_task(asyncio.to_thread(_run_controlled_input, function, args, kwargs))
        token.worker = worker
        token.worker_isolated = action is not None
    worker.add_done_callback(lambda done: _control_worker_done(token, done))
    default_timeout = 8.0 if (function is focus_window_object or getattr(function, "__name__", "").startswith(("_uia_", "_focus_semantic_"))) else 5.0
    timeout_seconds = max(0.1, min(float(timeout if timeout is not None else default_timeout), 30.0))
    deadline = asyncio.timeout(timeout_seconds)
    try:
        async with deadline:
            return await asyncio.shield(worker)
    except asyncio.CancelledError:
        if token.actions is not None:
            release_control(token, force=True)
        raise
    except TimeoutError as exc:
        if not deadline.expired():
            raise
        release_control(token, force=True)
        _LOGGER.error("desktop call timed out after %.1fs: %s.%s", timeout_seconds,
                      getattr(function, "__module__", ""), getattr(function, "__name__", repr(function)))
        raise TimeoutError("desktop_call_timeout") from exc
    except Exception:
        if token.actions is not None:
            release_control(token, force=True)
        raise


def _run_controlled_input(function, args, kwargs):
    check_foreground_permission()
    precondition = INPUT_PRECONDITION.get()
    if precondition is not None:
        precondition()
    try:
        if (getattr(function, '__module__', '') == 'backend.modules.computer.computer_module'
                and function.__name__.startswith(('_uia_', '_focus_semantic_'))):
            try:
                import uiautomation as auto
            except ImportError:
                return function(*args, **kwargs)
            with getattr(auto, 'UIAutomationInitializerInThread', nullcontext)():
                return function(*args, **kwargs)
        return function(*args, **kwargs)
    except BaseException:
        # PyAutoGUI has no finally around dragTo's mouse-up. Release only this action's held input.
        pg = sys.modules.get("pyautogui")
        if pg is not None and getattr(function, "__module__", "") == "pyautogui":
            if function.__name__ == "dragTo":
                pg.platformModule._mouseUp(*pg.position(), kwargs.get("button", "left"))
            elif function.__name__ == "hotkey":
                for key in reversed(args):
                    pg.platformModule._keyUp(str(key).lower())
        raise


async def wait_for_user_idle(min_idle: float = 1.5, timeout: float = 8.0) -> bool:
    """An acquired desktop lease proceeds immediately; Esc revokes it explicitly."""
    check_foreground_permission()
    session = _CONTROL_SESSION.get()
    with _CONTROL_STATE_LOCK:
        control = _CONTROL_OWNER
        owned = control is not None and (control.session is session)
    if owned:
        activate_foreground(control)
        return True
    deadline = time.monotonic() + max(0.1, min(float(timeout), 30.0))
    while time.monotonic() < deadline:
        check_foreground_permission()
        idle = user_idle_seconds()
        if idle is None or idle >= min_idle:
            return True
        await asyncio.sleep(min(0.15, max(0.01, deadline - time.monotonic())))
    return False


def _run_console_command(command: str, directory: str, timeout: float = 60) -> subprocess.CompletedProcess:
    # GUI children can inherit pipes and keep communicate() blocked after shell exit/timeout.
    # A temporary file lets us wait only for the shell, without killing the user's application.
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            command, shell=True, cwd=directory, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, timeout=timeout,
            env=subprocess_env_without_secrets(), **hidden_subprocess_kwargs(),
        )
        output.seek(0)
        return subprocess.CompletedProcess(command, result.returncode, output.read(64 * 1024), b"")


def _decode_console(raw: bytes) -> str:
    """解码 Windows 命令行输出:多为 GBK/GB18030,少数 UTF-8。逐个尝试。"""
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# 常用软件:中文/英文别名 -> Windows 真实程序名(这些多在 PATH 上,可直接启动)
_APP_ALIASES = {
    "记事本": "notepad", "notepad": "notepad",
    "计算器": "calc", "calc": "calc", "calculator": "calc",
    "画图": "mspaint", "mspaint": "mspaint", "paint": "mspaint",
    "写字板": "write", "wordpad": "write",
    "命令提示符": "cmd", "cmd": "cmd", "命令行": "cmd", "终端": "cmd",
    "资源管理器": "explorer", "文件管理器": "explorer", "文件资源管理器": "explorer", "explorer": "explorer",
    "任务管理器": "taskmgr", "taskmgr": "taskmgr",
    "控制面板": "control", "注册表": "regedit", "registry": "regedit",
    "chrome": "chrome", "谷歌浏览器": "chrome", "谷歌": "chrome",
    "edge": "msedge", "浏览器": "msedge",
    "截图工具": "snippingtool", "snipping": "snippingtool",
    # Microsoft Office(真实程序名与显示名不同,且不在 PATH,靠 App Paths 注册表解析)
    "word": "winword", "微软word": "winword", "文档": "winword",
    "excel": "excel", "微软excel": "excel", "表格": "excel", "电子表格": "excel",
    "ppt": "powerpnt", "powerpoint": "powerpnt", "幻灯片": "powerpnt", "演示文稿": "powerpnt",
    "outlook": "outlook", "邮件": "outlook", "onenote": "onenote",
    "access": "msaccess",
}


# 学到的映射存这里(用户教过的"名字->路径"),跨会话持久
_LEARNED_PATH = Path(__file__).resolve().parents[3] / "data" / "app_aliases.json"

# 次要快捷方式的关键词:命中则大幅降权(避免开成 设置/卸载/帮助 等)
_SECONDARY = [
    "设置", "配置", "卸载", "uninstall", "帮助", "help", "readme", "说明",
    "import", "导入", "迁移", "migrate", "reset", "重置", "修复", "repair",
    "license", "许可", "激活", "activate", "文档", "manual", "website", "官网",
    "update", "更新", "工具", "tool", "console", "manager 设置",
]


def _load_learned() -> dict:
    try:
        import json
        return json.loads(_LEARNED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_learned(d: dict) -> None:
    import json
    _LEARNED_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LEARNED_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _start_menu_dirs() -> list[Path]:
    dirs = []
    for env in ("ProgramData", "APPDATA"):
        base = os.environ.get(env)
        if base:
            p = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            if p.exists():
                dirs.append(p)
    return dirs


def _desktop_dirs() -> list[Path]:
    dirs = []
    up = os.environ.get("USERPROFILE")
    pub = os.environ.get("PUBLIC")
    for base in (up, pub):
        if base:
            for name in ("Desktop", "桌面"):
                d = Path(base) / name
                if d.exists():
                    dirs.append(d)
    return dirs


def _score_shortcut(stem_low: str, name_low: str) -> int:
    """给一个快捷方式名打分,分越高越可能是用户想要的主程序。"""
    if stem_low == name_low:
        score = 1000
    elif stem_low.startswith(name_low):
        score = 600
    elif name_low in stem_low:
        score = 400
    elif stem_low in name_low:
        score = 300
    else:
        return -1  # 不相关
    # 次要项大幅降权
    for kw in _SECONDARY:
        if kw in stem_low:
            score -= 500
            break
    # 名字越短越可能是主程序(减去长度差)
    score -= abs(len(stem_low) - len(name_low))
    return score


def _find_shortcut(name: str) -> Path | None:
    """在桌面 + 开始菜单里找最匹配的主程序快捷方式(带打分)。"""
    name_low = name.lower().strip()
    best = None
    best_score = 0
    # 桌面优先级略高(用户放桌面的通常就是想用的)
    for d, bonus in [(d, 50) for d in _desktop_dirs()] + [(d, 0) for d in _start_menu_dirs()]:
        for lnk in d.rglob("*.lnk"):
            s = _score_shortcut(lnk.stem.lower(), name_low) + bonus
            if s > best_score:
                best_score, best = s, lnk
    return best if best_score >= 300 else None


def register_tools(registry: ToolRegistry) -> None:
    if not _IS_WINDOWS:
        print("[system] 非 Windows,系统控制工具跳过注册。")
        return

    @registry.tool(
        name="list_windows",
        description="列出当前可操作窗口及句柄、标题和位置。可用 title 过滤；Codex 会自动匹配真实的 ChatGPT 窗口。",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "可选的标题或别名关键词"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
    )
    async def list_windows(title: str = "", limit: int = 30) -> dict[str, Any]:
        try:
            import pygetwindow as gw
        except ImportError:
            return {"ok": False, "error": "未安装 pygetwindow", "windows": []}
        limit = max(1, min(int(limit), 50))
        queries = window_queries(title) if title.strip() else ()
        windows = []
        for window in gw.getAllWindows():
            candidate = str(getattr(window, "title", "") or "").strip()
            if not candidate or (queries and not any(q in candidate.lower() for q in queries)):
                continue
            windows.append(window_info(window))
            if len(windows) >= limit:
                break
        if not windows and title.strip():
            windows = [window_info(w) for w in find_hidden_main_windows(gw, title)[:limit]]
        return {"ok": True, "count": len(windows), "windows": windows}

    @registry.tool(
        name="launch_app",
        description=(
            "按名字打开一个已安装的应用程序,比如 'Chrome'、'WPS'、'微信'、'记事本'、'计算器'。"
            "会自动在开始菜单里查找。也可直接传入 exe 的完整路径。有副作用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "应用名称或 exe 完整路径"},
            },
            "required": ["name"],
        },
        side_effect=True,
    )
    async def launch_app(name: str) -> str:
        key = name.lower().strip()
        cad_pids_before = _process_ids_by_name("acad.exe") if key in {"cad", "autocad"} or "autocad" in key else set()

        async def mark_started(target: str = "") -> bool:
            text = f"{name} {target}".lower()
            if key in {"cad", "autocad"} or "autocad" in text or Path(target).name.lower() in {"acad.exe", "acadlt.exe"}:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    new_pids = _process_ids_by_name("acad.exe") - cad_pids_before
                    if new_pids:
                        mark_owned_application("autocad", min(new_pids))
                        return True
                    await asyncio.sleep(0.2)
                return False
            return True

        # A launch request is idempotent while the application already owns a
        # visible window. Do not focus it here: focusing can steal the user's
        # keyboard/mouse; the agent has a separate guarded focus_window tool.
        try:
            import pygetwindow as gw
            existing = resolve_window(gw, title=name)
        except Exception:
            existing = None
        if existing is not None:
            title = str(getattr(existing, "title", "") or name)
            return f"检测到已经打开:{title}。未重复启动;需要切到前台时请使用 focus_window。"

        # 0) 用户教过的映射优先(学习能力)
        learned = _load_learned()
        if key in learned:
            target = learned[key]
            try:
                os.startfile(target)  # type: ignore[attr-defined]
                owned = await mark_started(target)
                suffix = "" if owned else "；无法核验 AutoCAD 进程归属，本轮不会自动关闭"
                return f"已启动:{name}(用你教过的设置){suffix}"
            except Exception:
                pass  # 学的路径失效了,继续往下常规查找

        # 1) 直接是存在的路径
        p = Path(name)
        if p.exists() and p.suffix.lower() in (".exe", ".lnk", ".bat"):
            os.startfile(str(p))  # type: ignore[attr-defined]
            owned = await mark_started(str(p))
            suffix = "" if owned else "；无法核验 AutoCAD 进程归属，本轮不会自动关闭"
            return f"已启动:{p.name}{suffix}"

        # 2) 常用软件别名 -> 真实程序名(os.startfile 走 ShellExecute,能解析 App Paths)
        exe = _APP_ALIASES.get(key)
        if exe:
            try:
                os.startfile(exe)  # type: ignore[attr-defined]
                owned = await mark_started(exe)
                suffix = "" if owned else "；无法核验 AutoCAD 进程归属，本轮不会自动关闭"
                return f"已启动:{name}{suffix}"
            except OSError as error:
                fallback = {"winword": "wps", "excel": "et", "powerpnt": "wpp"}.get(exe.removesuffix(".exe").lower())
                if fallback and (isinstance(error, FileNotFoundError) or getattr(error, "winerror", None) in {2, 3, 1155}):
                    try:
                        os.startfile(fallback)
                        return f"已启动:{name}（使用 WPS 兼容应用）"
                    except OSError:
                        pass
            except Exception as e:
                return f"启动 {name} 出错:{e}"

        # 3) 桌面 + 开始菜单快捷方式(带打分,避开设置/卸载类次要项)
        lnk = await asyncio.to_thread(_find_shortcut, name)
        if lnk is not None:
            os.startfile(str(lnk))  # type: ignore[attr-defined]
            owned = await mark_started(str(lnk))
            suffix = "" if owned else "；无法核验 AutoCAD 进程归属，本轮不会自动关闭" if key in {"cad", "autocad"} else ""
            return f"已启动:{lnk.stem}{suffix}。如果这个不对,告诉我'记住XX是<桌面图标名或路径>',我下次就开对。"

        # 4) 没找到:提示可以教它
        return (
            f"没能确定「{name}」是哪个程序。你可以:1)把它的完整路径告诉我;"
            f"2)或告诉我桌面上那个图标的名字,我用 remember_app 记住,下次直接开对。"
        )

    @registry.tool(
        name="remember_app",
        description=(
            "记住某个软件叫什么、对应哪个程序,以后用户说这个名字就直接开对。"
            "target 传桌面图标名(如'AutoCAD 2014')或 exe/快捷方式完整路径。教一次永久记住。有副作用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "用户习惯的叫法,如 'CAD'"},
                "target": {"type": "string", "description": "桌面图标名 或 程序完整路径"},
            },
            "required": ["name", "target"],
        },
        side_effect=True,
    )
    async def remember_app(name: str, target: str) -> str:
        # 解析 target:先看是不是现成路径,否则在桌面/开始菜单按名字找其对应的 .lnk
        resolved = None
        p = Path(target)
        if p.exists():
            resolved = str(p)
        else:
            lnk = await asyncio.to_thread(_find_shortcut, target)
            if lnk is not None:
                resolved = str(lnk)
        if resolved is None:
            return f"没找到「{target}」。请给我它的完整路径(在桌面图标上右键→属性→目标,把那串复制给我)。"

        learned = _load_learned()
        learned[name.lower().strip()] = resolved
        _save_learned(learned)
        # 记住后顺手打开验证
        try:
            cad_pids_before = _process_ids_by_name("acad.exe")
            os.startfile(resolved)  # type: ignore[attr-defined]
            if name.strip().lower() in {"cad", "autocad"} or "autocad" in f"{name} {resolved}".lower() or Path(resolved).name.lower() in {"acad.exe", "acadlt.exe"}:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    new_pids = _process_ids_by_name("acad.exe") - cad_pids_before
                    if new_pids:
                        mark_owned_application("autocad", min(new_pids))
                        break
                    await asyncio.sleep(0.2)
            return f"记住了:以后「{name}」就开 {Path(resolved).name}。已帮你打开。"
        except Exception as e:
            return f"已记住「{name}」→{resolved},但这次打开出错:{e}"

    @registry.tool(
        name="open_url",
        description="仅当用户明确要求使用系统默认浏览器时打开网址。这不是小A独立浏览器，普通网页及网页自动化默认使用 browser_open。有副作用。",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        side_effect=True,
    )
    async def open_url(url: str) -> str:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        webbrowser.open(url)
        return f"已在系统默认浏览器打开（非小A专用浏览器）:{url}"

    @registry.tool(
        name="run_command",
        description=(
            "在 Windows 命令行执行一条命令并返回输出。用于没有专门工具时的兜底操作。"
            "打开软件优先使用 launch_app，打开文件优先使用专门的文件工具。命令最长等待60秒。"
            "未指定 cwd 时在隔离工作区执行，生成的相对路径文件留在该目录。危险操作,执行前会请求确认。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "仅用户指定工作目录时填写；省略则使用隔离工作区"},
                "command": {"type": "string", "description": "要执行的命令行"},
            },
            "required": ["command"],
        },
        side_effect=True,
        danger=True,
    )
    async def run_command(command: str, cwd: str = "") -> str:
        try:
            directory = Path(cwd).expanduser() if cwd else isolation_workspace.workspace()
            if not directory.is_absolute():
                directory = isolation_workspace.workspace() / directory
            directory = directory.resolve()
            if not directory.is_dir():
                raise ValueError(f"工作目录不存在:{directory}")
            out = await asyncio.to_thread(
                _run_console_command, command, str(directory),
            )  # 捕获原始字节,自己解码(Windows 命令行多为 GBK)
            raw = (out.stdout or b"") + (out.stderr or b"")
            text = _decode_console(raw)
            return text.strip()[:4000] or f"命令已执行(无输出),退出码 {out.returncode}"
        except subprocess.TimeoutExpired:
            return "命令执行超时(60秒)，已停止等待。已启动的应用或子进程可能仍在运行，请先核实现状，不要直接重复执行。"
        except Exception as e:
            return f"命令执行失败:{e}"

    @registry.tool(
        name="focus_window",
        description="按精确句柄或标题/别名把窗口切到前台。Codex 会匹配标题为 ChatGPT 的窗口。有副作用。",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "窗口标题或别名，如 Codex、微信、Chrome"},
                "handle": {"type": "integer", "description": "list_windows 返回的精确窗口句柄，优先于标题"},
                "exact": {"type": "boolean", "description": "标题是否必须精确匹配，默认否"},
                "maximize": {"type": "boolean", "description": "是否最大化，默认否"},
            },
        },
        side_effect=True,
    )
    async def focus_window(
        title: str = "", handle: int | None = None, exact: bool = False, maximize: bool = False,
    ) -> dict[str, Any]:
        if handle is None and not title.strip():
            return {"ok": False, "error": "title 和 handle 至少提供一个"}
        try:
            import pygetwindow as gw
        except ImportError:
            return {"ok": False, "error": "未安装 pygetwindow"}
        window = resolve_window(gw, title, handle, exact=exact)
        if window is None:
            return {"ok": False, "error": f"没找到窗口：{title or handle}"}
        control = await acquire_control(8.0)
        if control is None:
            return {"ok": False, "status": "control_busy", "control_mode": "needs_foreground", "error": "另一项小A电脑操作尚未结束"}
        try:
            if not await wait_for_user_idle(1.5, 8.0):
                return {"ok": False, "status": "user_active", "control_mode": "needs_foreground", "error": "检测到你正在使用鼠标或键盘，已取消抢占窗口"}
            focused = await run_controlled(control, focus_window_object, window, maximize=maximize)
            return {"ok": True, "control_mode": "foreground_exclusive", **focused}
        except Exception as e:
            return {"ok": False, "error": f"聚焦失败：{str(e)[:300]}", "window": window_info(window)}
        finally:
            release_control(control)

    @registry.tool(
        name="close_window",
        description=(
            "礼貌关闭一个窗口。按精确窗口标题或精确进程名匹配,不做模糊匹配。"
            "先发关闭消息(WM_CLOSE),等一小会儿,不强杀;目标程序有未保存内容时不绕过"
            "弹窗、不强杀,会如实报告已放弃。绝不关闭小A 自己(小A/electron/python 进程)。"
            "force=true 才会强杀进程,且会请求确认。有副作用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "窗口精确标题,与 process_name 二选一"},
                "process_name": {"type": "string", "description": "进程名(如 acad.exe),与 title 二选一"},
                "force": {"type": "boolean", "description": "是否强杀进程(默认否;强杀会请求确认)"},
            },
        },
        side_effect=True,
    )
    async def close_window(title: str = "", process_name: str = "", force: bool = False) -> dict[str, Any]:
        if not title.strip() and not process_name.strip():
            return {"ok": False, "error": "title 和 process_name 至少提供一个"}
        # 自我保护:绝不允许关小A 自己。
        protected = _is_protected(title, process_name)
        if protected:
            return {"ok": False, "error": f"已拒绝:目标是或包含小A 自己(命中保护词 {protected}),不能关闭。"}
        try:
            import pygetwindow as gw
        except ImportError:
            return {"ok": False, "error": "未安装 pygetwindow"}
        window = None
        if title.strip():
            wanted = title.strip().lower()
            window = next(
                (w for w in gw.getAllWindows()
                 if str(getattr(w, "title", "") or "").strip().lower() == wanted),
                None,
            )
            if window is None:
                return {"ok": False, "error": f"没找到精确标题为「{title}」的窗口"}
        if window is None and process_name.strip():
            window = await asyncio.to_thread(_find_window_by_process, gw, process_name.strip().lower())
            if window is None:
                return {"ok": False, "error": f"没找到进程名为「{process_name}」的窗口"}
        if window is None:
            return {"ok": False, "error": "没找到匹配的窗口"}
        # 匹配到窗口后再自查一次(标题/进程名本身可能就是小A)。
        actual_title = str(getattr(window, "title", "") or "").strip()
        protected = _is_protected(actual_title, _window_process_name(window))
        if protected:
            return {"ok": False, "error": "已拒绝:匹配到的窗口属于小A 自己,不能关闭。"}
        posted = await asyncio.to_thread(_post_close, window)
        if not posted:
            return {"ok": False, "error": "无法向该窗口发送关闭消息"}
        await asyncio.sleep(1.0)
        if not await asyncio.to_thread(_window_still_exists, window):
            return {"ok": True, "closed": True, "title": actual_title, "summary": f"已关闭窗口:{actual_title}"}
        # 还没关:多半弹了"是否保存"或未响应。默认不强杀,如实报告。
        if force:
            killed = await asyncio.to_thread(_kill_window_process, window)
            if killed:
                return {"ok": True, "closed": True, "force": True, "summary": f"已强杀窗口:{actual_title}"}
            return {"ok": False, "closed": False, "error": f"强杀失败:{actual_title}"}
        return {"ok": False, "closed": False, "error": "目标程序有未保存内容或未响应,已放弃关闭(不会强杀)。"}
