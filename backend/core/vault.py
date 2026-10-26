from __future__ import annotations
"""Windows DPAPI-backed store for credentials excluded from config.toml."""
import base64, ctypes, ctypes.wintypes as wintypes, json, os, tempfile, threading

_LOCK = threading.RLock()
_WINDOWS = os.name == "nt"
_STORE_NAME = "secrets.dpapi"
_DPAPI_FLAGS = 0x1  # CRYPTPROTECT_UI_FORBIDDEN; DPAPI's default scope is the current user.
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

def _path():
    from backend.core.config import app_home
    return app_home() / "data" / _STORE_NAME

def _crypt(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI is available only on Windows")
    source = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    inp, out = _Blob(len(data), source), _Blob()
    api = ctypes.windll.crypt32
    api.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    api.CryptProtectData.restype = wintypes.BOOL
    api.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    api.CryptUnprotectData.restype = wintypes.BOOL
    ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    ctypes.windll.kernel32.LocalFree.restype = ctypes.c_void_p
    ok = (api.CryptProtectData(ctypes.byref(inp), "XiaoA secrets", None, None, None, _DPAPI_FLAGS, ctypes.byref(out))
          if protect else api.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, _DPAPI_FLAGS, ctypes.byref(out)))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)

def _read(*, preserve_failed: bool = False) -> dict[str, str]:
    if not _WINDOWS:
        return {}
    target = _path()
    try:
        payload = target.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError:
        if preserve_failed:
            raise
        return {}
    try:
        value = json.loads(_crypt(base64.b64decode(payload, validate=True), False).decode("utf-8"))
        if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                              for k, v in value.items()):
            raise ValueError("Invalid credential store")
        return value
    except (OSError, ValueError, TypeError, RuntimeError):
        if preserve_failed:
            # Keep the original bytes before a write; backup failure must abort the write.
            index = 0
            while True:
                backup = target.with_name(target.name + (f".{index}" if index else "") + ".bak")
                try:
                    with backup.open("xb") as handle:
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                    break
                except FileExistsError:
                    index += 1
        return {}

def _write(values: dict[str, str]) -> None:
    target = _path(); target.parent.mkdir(parents=True, exist_ok=True)
    payload = base64.b64encode(_crypt(json.dumps(values, ensure_ascii=False).encode("utf-8"), True))
    fd, name = tempfile.mkstemp(prefix=".vault-", dir=target.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        os.replace(name, target)
    finally:
        try: os.close(fd)
        except OSError: pass
        try: os.unlink(name)
        except FileNotFoundError: pass

def get(name: str) -> str:
    with _LOCK:
        return _read().get(name, "")


def put(name: str, value: str) -> None:
    if not _WINDOWS:
        raise RuntimeError("DPAPI is available only on Windows")
    # ponytail: one process lock covers this small store's read/modify/replace cycle.
    with _LOCK:
        values = _read(preserve_failed=True)
        if value: values[name] = value
        else: values.pop(name, None)
        if values: _write(values)
        else:
            try: _path().unlink()
            except FileNotFoundError: pass
