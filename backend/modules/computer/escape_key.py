"""Listen for physical Esc without consuming it or mistaking injected keys for user input."""
import atexit
import ctypes
import subprocess
import sys
import threading
from pathlib import Path

from backend.core.process import hidden_subprocess_kwargs
from ctypes import wintypes

_lock = threading.Lock()
_ready = threading.Event()
_thread = None
_process = None
_error = None


def physical_escape(code: int, message: int, key: int, flags: int) -> bool:
    return code >= 0 and message in (0x100, 0x104) and key == 0x1B and not flags & 0x10


def _listen(callback, ready):
    hook = None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hook_proc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        class KeyboardEvent(ctypes.Structure):
            _fields_ = [("key", wintypes.DWORD), ("scan", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]

        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, hook_proc, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]

        @hook_proc
        def receive(code, message, address):
            if code >= 0:
                event = ctypes.cast(address, ctypes.POINTER(KeyboardEvent)).contents
                if physical_escape(code, message, event.key, event.flags):
                    callback()
            return user32.CallNextHookEx(hook, code, message, address)

        hook = user32.SetWindowsHookExW(13, receive, None, 0)  # WH_KEYBOARD_LL
        if not hook:
            raise ctypes.WinError(ctypes.get_last_error())
        ready()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
    finally:
        if hook:
            user32.UnhookWindowsHookEx(hook)


def serve():
    """Keep the synchronous OS hook away from the busy backend's GIL."""
    escaped = threading.Event()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
    message = wintypes.MSG()
    user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)  # Create the thread queue.

    def watch_parent():
        # EOF on the private pipe means the backend exited, including a crash.
        sys.stdin.read()
        user32.PostThreadMessageW(thread_id, 0x12, 0, 0)  # WM_QUIT

    def notify():
        while True:
            escaped.wait()
            escaped.clear()
            print("escape", flush=True)

    threading.Thread(target=watch_parent, daemon=True).start()
    threading.Thread(target=notify, daemon=True).start()
    _listen(escaped.set, lambda: print("ready", flush=True))


def stop_listener():
    """Uninstall the hook when the foreground lease ends."""
    global _process
    with _lock:
        process, _process = _process, None
        if process is not None:
            try:
                process.stdin.close()
                process.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=0.5)
            finally:
                process.stdout.close()


def _monitor(process, callback):
    global _error
    try:
        for line in process.stdout:
            if _process is not process:
                return
            if line.strip() == "ready":
                _ready.set()
            elif line.strip() == "escape":
                callback()
    except (OSError, ValueError):
        pass  # The owner may close the pipe during normal shutdown.
    finally:
        if _process is process:
            _error = RuntimeError("Esc listener exited")
            _ready.set()
            callback()  # Fail closed if the stop channel disappears during control.


def ensure_listener(callback):
    global _thread, _process, _error
    with _lock:
        if _process is None or _process.poll() is not None:
            _ready.clear()
            _error = None
            command = [sys.executable]
            if not getattr(sys, "frozen", False):
                command.append(str(Path(__file__).resolve().parents[3] / "xiaoa_backend.py"))
            _process = subprocess.Popen(
                [*command, "--escape-listener"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                **hidden_subprocess_kwargs(),
            )
            _thread = threading.Thread(target=_monitor, args=(_process, callback),
                                       name="desktop-escape", daemon=True)
            _thread.start()
        if not _ready.wait(3) or _error is not None or _process.poll() is not None:
            raise RuntimeError("无法启用 Esc 交还控制权，已取消桌面接管") from _error


atexit.register(stop_listener)
