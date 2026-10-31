"""Disposable, allowlisted desktop actions. A stopped lease cannot resume input."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from multiprocessing.shared_memory import SharedMemory

from backend.core.process import hidden_subprocess_kwargs
from backend.core.config import subprocess_env_without_secrets

_PG = {"click", "doubleClick", "moveTo", "scroll", "dragTo", "write", "typewrite", "press", "hotkey"}
_UIA = {"_uia_click_named_control", "_uia_select_named_control", "_uia_background_send", "_focus_semantic_text_input"}


def describe(function, args, kwargs):
    """Only production desktop functions cross the process boundary."""
    module, name = getattr(function, "__module__", ""), getattr(function, "__name__", "")
    owner = getattr(function, "__self__", None)
    if module == "pyautogui" and name in _PG:
        kind = "input"
    elif module == "backend.modules.computer.computer_module" and name in _UIA:
        kind = "uia"
    elif module == "backend.modules.computer.system_control" and name == "focus_window_object":
        kind, args = "focus", (int(args[0]._hWnd), *args[1:])
    elif module.startswith("pygetwindow.") and name in {"restore", "activate"} and owner is not None:
        kind, args = "window", (int(owner._hWnd),)
    else:
        return None
    return {"kind": kind, "name": name, "args": args, "kwargs": kwargs}


def _release_inputs(held):
    if not any(held):
        return
    import ctypes
    user32 = ctypes.windll.user32
    for code in range(256):
        if held[code]:
            user32.keybd_event(code, 0, 2, 0)
            held[code] = 0
    for index, flag in enumerate((4, 64, 16), 512):
        if held[index]:
            user32.mouse_event(flag, 0, 0, 0, 0)
            held[index] = 0


class ActionWorker:
    def __init__(self):
        self.process = None
        self.memory = None
        self.lock = threading.RLock()
        self.closed = False

    def execute(self, action, guard=None):
        with self.lock:
            if self.closed:
                raise RuntimeError("desktop_worker_revoked")
            process = self.process
        if process is None:
            memory = SharedMemory(create=True, size=520)
            memory.buf[:] = bytes(520)
            command = [sys.executable]
            if not getattr(sys, "frozen", False):
                command.append(str(Path(__file__).resolve().parents[3] / "xiaoa_backend.py"))
            try:
                # Creation itself can be slow. A revoked startup never receives an action.
                process = subprocess.Popen(
                    [*command, "--desktop-worker"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, env=subprocess_env_without_secrets(), **hidden_subprocess_kwargs())
                with self.lock:
                    if self.closed:
                        process.kill()
                        process.wait(timeout=3)
                        for stream in (process.stdin, process.stdout):
                            stream.close()
                        raise RuntimeError("desktop_worker_revoked")
                    self.process, self.memory = process, memory
            except BaseException:
                memory.close()
                memory.unlink()
                raise
            process.stdin.write((json.dumps({"memory": memory.name}) + "\n").encode())
            process.stdin.flush()
        # No state lock across native work or blocking pipe reads.
        process.stdin.write((json.dumps({"action": action, "guard": guard}) + "\n").encode())
        process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            raise RuntimeError("desktop_worker_exited")
        result = json.loads(line)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["result"]

    def close(self):
        with self.lock:
            self.closed = True
            process, memory = self.process, self.memory
            # Keep the references and mutex on a failed kill/wait: never abandon live input.
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=3)
            self.process, self.memory = None, None
            try:
                if memory is not None:
                    _release_inputs(memory.buf)
            finally:
                if process is not None:
                    for stream in (process.stdin, process.stdout):
                        if stream:
                            stream.close()
                if memory is not None:
                    memory.close()
                    memory.unlink()


def _guard(descriptor):
    from backend.modules.computer import system_control
    from backend.modules.computer.desktop_runtime import window_identity, same_window, matching_controls
    import pygetwindow as gw
    import uiautomation as auto

    def verify(actual_control=None):
        info, selected = descriptor["window"], descriptor["selected"]
        current = system_control.resolve_window(gw, "", info["handle"], unique=True)
        import win32gui
        if current is None or win32gui.GetForegroundWindow() != info["handle"] or not same_window(info, window_identity(current)):
            raise RuntimeError("desktop_plan_window_changed_before_input")
        control = actual_control
        if descriptor["tool"] in {"type_text", "press_hotkey"}:
            control = auto.GetFocusedControl()
        if control is not None:
            actual = {"name": str(control.Name or ""), "control_type": str(control.ControlTypeName or ""),
                      "automation_id": str(control.AutomationId or "")}
            selector = selected if actual_control is not None else descriptor["expect"]
            if (not matching_controls([actual], selector) or bool(control.IsPassword) or not bool(control.IsEnabled)):
                raise RuntimeError("desktop_plan_control_changed_before_input")
    return verify


def _execute(action, guard):
    import pyautogui as pg
    import pygetwindow as gw
    import uiautomation as auto
    from backend.modules.computer import computer_module, system_control
    kind, name, args, kwargs = action["kind"], action["name"], action["args"], action["kwargs"]
    if kind == "input" and name in _PG:
        function = getattr(pg, name)
    elif kind == "uia" and name in _UIA:
        function = getattr(computer_module, name)
    elif (kind == "focus" and name == "focus_window_object") or (kind == "window" and name in {"restore", "activate"}):
        window = system_control.resolve_window(gw, "", args[0], unique=True)
        if window is None:
            raise RuntimeError("input_target_missing")
        if kind == "focus":
            function, args = system_control.focus_window_object, [window, *args[1:]]
        else:
            function, args = getattr(window, name), []
    else:
        raise ValueError("Unsupported desktop action")
    token = system_control.INPUT_PRECONDITION.set(_guard(guard) if guard else None)
    try:
        with auto.UIAutomationInitializerInThread():
            return system_control._run_controlled_input(function, args, kwargs)
    finally:
        system_control.INPUT_PRECONDITION.reset(token)


def serve():
    """A pipe reader watches parent EOF even if the action's COM thread hangs."""
    incoming = queue.Queue()
    memory = None
    stopped = threading.Event()
    def read():
        try:
            for line in sys.stdin.buffer:
                incoming.put(json.loads(line))
        finally:
            stopped.set()
            if memory is not None:
                _release_inputs(memory.buf)
            os._exit(0)
    threading.Thread(target=read, daemon=True).start()
    memory = SharedMemory(name=incoming.get()["memory"])
    # Input-only worker needs no screenshots or dialogs. Pillow can import NumPy
    # before PyScreeze reaches OpenCV; skip the whole optional image/UI chain.
    for module in ("cv2", "pyscreeze", "mouseinfo", "pymsgbox"):
        sys.modules[module] = None
    import pyautogui as pg
    pg.FAILSAFE = False
    def check_parent():
        if stopped.is_set():
            raise RuntimeError("desktop_worker_parent_exited")
    pg.failSafeCheck = check_parent
    platform = pg.platformModule
    # Record actual virtual keys, including implicit shift/control/alt used by write().
    user32 = platform.ctypes.windll.user32
    original_key_event = user32.keybd_event
    def key_event(code, scan, flags, extra):
        if not flags & 2:
            memory.buf[code] = 1
        result = original_key_event(code, scan, flags, extra)
        if flags & 2:
            memory.buf[code] = 0
        return result
    user32.keybd_event = key_event
    def track_mouse(method, down):
        original = getattr(platform, method)
        def call(x, y, button):
            code = 512 + ("left", "middle", "right").index(button)
            if down:
                memory.buf[code] = 1
            result = original(x, y, button)
            if not down:
                memory.buf[code] = 0
            return result
        setattr(platform, method, call)
    track_mouse("_mouseDown", True)
    track_mouse("_mouseUp", False)
    while True:
        request = incoming.get()
        try:
            result = {"result": _execute(request["action"], request.get("guard"))}
        except Exception as exc:
            result = {"error": str(exc)}
        finally:
            _release_inputs(memory.buf)
        print(json.dumps(result, ensure_ascii=True), flush=True)
