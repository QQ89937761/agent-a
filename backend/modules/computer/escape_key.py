"""Listen for physical Esc without consuming it or mistaking injected keys for user input."""
import ctypes
import threading
from ctypes import wintypes

_lock = threading.Lock()
_ready = threading.Event()
_thread = None
_error = None


def physical_escape(code: int, message: int, key: int, flags: int) -> bool:
    return code >= 0 and message in (0x100, 0x104) and key == 0x1B and not flags & 0x10


def _listen(callback):
    global _error
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
        _ready.set()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
    except Exception as error:
        _error = error
    finally:
        if hook:
            user32.UnhookWindowsHookEx(hook)
        _ready.set()


def ensure_listener(callback):
    global _thread
    with _lock:
        if _thread is None:
            _thread = threading.Thread(target=_listen, args=(callback,), name="desktop-escape", daemon=True)
            _thread.start()
    if not _ready.wait(1) or _error is not None or not _thread.is_alive():
        raise RuntimeError("无法启用 Esc 交还控制权，已取消桌面接管") from _error
