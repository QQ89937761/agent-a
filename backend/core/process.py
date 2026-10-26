"""Shared subprocess settings for desktop and packaged Windows runs."""
from __future__ import annotations

import os
import subprocess
from typing import Any, Callable


def hidden_subprocess_kwargs(*, new_process_group: bool = False) -> dict[str, Any]:
    """Keep helper consoles hidden without changing process-group behavior."""
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if new_process_group:
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags} if flags else {}
    return {"start_new_session": True} if new_process_group else {}


def reduce_desktop_startup_priority() -> Callable[[], None]:
    """Yield CPU to the desktop during initialization; restore when ready."""
    unchanged = lambda: None
    if os.name != "nt" or not os.environ.get("XIAOA_PARENT_PID"):
        return unchanged
    try:
        import psutil
        process = psutil.Process()
        previous = process.nice()
        # Respect an explicit priority set by the user or launcher.
        if previous != psutil.NORMAL_PRIORITY_CLASS:
            return unchanged
        process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception as error:
        print(f"[startup] process priority unchanged: {type(error).__name__}")
        return unchanged

    def restore() -> None:
        try:
            if process.nice() == psutil.BELOW_NORMAL_PRIORITY_CLASS:
                process.nice(previous)
        except Exception as error:
            print(f"[startup] process priority restore failed: {type(error).__name__}")

    return restore
