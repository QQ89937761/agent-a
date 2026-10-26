"""PyInstaller/开发环境共用的后端入口。"""
import os
import sys
import time
from pathlib import Path


def _ensure_stdio() -> None:
    """Windowed PyInstaller apps have no console streams; uvicorn expects them."""
    # PyInstaller's windowed bootloader sets Python streams to None even when
    # Electron supplies valid output pipes. Recover those handles before fallback.
    if os.name == "nt":
        for name, identifier in (("stdout", -11), ("stderr", -12)):
            if getattr(sys, name) is not None:
                continue
            duplicate = None
            try:
                import _winapi
                import io
                import msvcrt
                handle = _winapi.GetStdHandle(identifier)
                # A windowed process may have no inherited standard handle.
                if handle in (None, 0, -1):
                    continue
                process = _winapi.GetCurrentProcess()
                duplicate = _winapi.DuplicateHandle(process, handle,
                                                    process, 0, False, _winapi.DUPLICATE_SAME_ACCESS)
                fd = msvcrt.open_osfhandle(duplicate, os.O_WRONLY)
                duplicate = None  # fd now owns the duplicated handle.
                setattr(sys, name, io.open(fd, "w", encoding="utf-8", errors="replace", buffering=1))
            except (OSError, ValueError):
                if duplicate is not None:
                    _winapi.CloseHandle(duplicate)
    stream = None
    if sys.stdout is None or sys.stderr is None:
        log_dir = Path(os.environ.get("XIAOA_HOME") or Path.cwd())
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = (log_dir / "backend.log").open("a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    for output in (sys.stdout, sys.stderr):
        if hasattr(output, "reconfigure"):
            output.reconfigure(encoding="utf-8", errors="replace", write_through=True)


_ensure_stdio()

# The frozen backend ships the reviewed source payload for cloud deployment.
# Put it on the import path so runtime channel imports (WeChat/Feishu) resolve
# from the same packaged version as the main server.
if getattr(sys, "_MEIPASS", ""):
    payload = Path(sys._MEIPASS) / "server-payload"
    if payload.is_dir():
        sys.path.insert(0, str(payload))

# A private UIA query process must return before importing or starting the server.
if __name__ == '__main__' and len(sys.argv) == 4 and sys.argv[1] == '--inspect-ui':
    from backend.modules.computer.computer_module import inspect_ui_worker
    inspect_ui_worker(sys.argv[2], sys.argv[3])
    raise SystemExit(0)

if __name__ == "__main__":
    print(f"[startup] python entry unix_ms={round(time.time() * 1000)}", flush=True)

# Channel modules are bundled via hiddenimports and loaded on demand by the server.
from backend.api.server import main  # noqa: E402


if __name__ == "__main__":
    main()
