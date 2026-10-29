"""Bounded desktop observations and guarded plans over the existing input tools."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import secrets
import re
import time

from backend.modules.computer import system_control

# Task-local: a remote worker also checks its physical Esc session between steps.
interrupt_check: ContextVar = ContextVar("desktop_interrupt_check", default=None)
request_stop: ContextVar = ContextVar("desktop_request_stop", default=None)
_ACTIONS = {
    "click_element": {"target_desc", "control_type", "automation_id", "double"},
    "type_text": {"text"},
    "press_hotkey": {"keys"},
}
_SUBMIT = re.compile(r"发送|提交|付款|转账|删除|清空|send|submit|pay|purchase|delete|remove", re.I)
_NAVIGATION_KEYS = {("ctrl", "a"), ("ctrl", "c"), ("home",), ("end",), ("left",), ("right",), ("up",), ("down",), ("pageup",), ("pagedown",)}
_INPUT_TYPES = {"EditControl", "DocumentControl", "ComboBoxControl"}


def interrupted() -> bool:
    try:
        system_control.check_foreground_permission()
    except RuntimeError:
        return True  # Keep completed-step evidence after physical Esc revokes the lease.
    check = interrupt_check.get()
    return bool(check and check())


def window_identity(window) -> dict:
    info = system_control.window_info(window)
    info["process"] = system_control._window_process_name(window)
    info["process_id"] = 0
    info["class_name"] = ""
    try:
        import win32gui
        import win32process
        info["class_name"] = win32gui.GetClassName(info["handle"])
        info["process_id"] = win32process.GetWindowThreadProcessId(info["handle"])[1]
    except Exception:
        pass
    return info


def same_window(before: dict, after: dict) -> bool:
    return all(before.get(key) == after.get(key) for key in
               ("handle", "title", "process_id", "class_name", "rect", "minimized"))


def matching_controls(controls: list, selector: dict) -> list:
    return [control for control in controls if not control.get("offscreen") and all(
        control.get(key) == value for key, value in selector.items()
    )]


def validate_plan(actions) -> None:
    if not isinstance(actions, list) or not 1 <= len(actions) <= 5:
        raise ValueError("actions 必须包含1到5个短步骤")
    for item in actions:
        if not isinstance(item, dict) or set(item) - {"tool", "arguments", "expect", "wait_ms", "after"}:
            raise ValueError("步骤仅支持tool/arguments/expect/wait_ms")
        name, args, expect = item.get("tool"), item.get("arguments", {}), item.get("expect")
        if not isinstance(name, str) or name not in _ACTIONS or not isinstance(args, dict) or set(args) - _ACTIONS[name]:
            raise ValueError("仅支持有目标的click_element、type_text和press_hotkey；其他操作单独调用原工具")
        if (not isinstance(expect, dict) or not expect or
                set(expect) - {"name", "control_type", "automation_id"} or
                any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in expect.values())):
            raise ValueError("每步必须提供本次控件树中唯一的expect目标")
        if not ("name" in expect or "automation_id" in expect):
            raise ValueError("expect必须包含name或automation_id")
        if name == "type_text" and (not isinstance(args.get("text"), str) or
                                     not 1 <= len(args["text"]) <= 1000):
            raise ValueError("短计划每步输入长度必须为1到1000")
        if name == "press_hotkey" and (not isinstance(args.get("keys"), list) or
                not 1 <= len(args["keys"]) <= 8 or
                any(not isinstance(k, str) or not k or len(k) > 20 for k in args["keys"])):
            raise ValueError("keys必须为1到8个键名")
        if name == "click_element" and (not isinstance(args.get("target_desc"), str) or
                not args["target_desc"].strip() or len(args["target_desc"]) > 300 or
                any(not isinstance(args[k], str) for k in ("automation_id", "control_type") if k in args) or
                ("double" in args and type(args["double"]) is not bool)):
            raise ValueError("点击必须提供有效目标描述")
        # Commit/send and multiline input remain individual guarded actions.
        if name == "type_text" and any(ord(char) < 32 or ord(char) == 127 for char in args["text"]):
            raise ValueError("含换行或控制字符的输入请单独调用原工具，短计划不隐式提交")
        if name == "press_hotkey" and tuple(k.lower() for k in args["keys"]) not in _NAVIGATION_KEYS:
            raise ValueError("短计划仅允许选择、复制和导航键；其他按键请单独调用原工具")
        if name == "click_element" and _SUBMIT.search(" ".join(str(v) for v in expect.values()) + " " + args["target_desc"]):
            raise ValueError("发送、提交和删除等关键动作请单独调用原工具并核验")
        after = item.get("after")
        if after is not None:
            if (not isinstance(after, dict) or set(after) - {"expect", "focused", "value_equals"}
                    or not isinstance(after.get("expect"), dict) or not after["expect"]
                    or set(after["expect"]) - {"name", "control_type", "automation_id"}
                    or not any(k in after["expect"] for k in ("name", "automation_id"))
                    or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in after["expect"].values())
                    or ("focused" in after and type(after["focused"]) is not bool)
                    or ("value_equals" in after and (not isinstance(after["value_equals"], str) or len(after["value_equals"]) > 20000))):
                raise ValueError("after支持唯一expect及focused/value_equals结果条件")
        wait = item.get("wait_ms", 0)
        if type(wait) is not int or not 0 <= wait <= 3000:
            raise ValueError("wait_ms必须为0到3000毫秒；按控件条件等待")


def register_tools(registry, *, resolve, inventory, inspect, capture, foreground, identity=window_identity):
    snapshots = {}

    async def observe(window="", handle=None, include_image=True, include_controls=True):
        timings = {}
        start = time.perf_counter()
        if interrupted():
            return {"ok": False, "status": "interrupted"}
        windows = await asyncio.to_thread(inventory)
        timings["windows_ms"] = round((time.perf_counter() - start) * 1000)
        target = await asyncio.to_thread(resolve, window, handle)
        if target is None:
            result = {"ok": False, "status": "window_missing_or_ambiguous", "windows": windows,
                      "next": "结合窗口清单和当前桌面定位目标；可能隐藏在托盘或存在同名窗口，不要重复启动、关闭或猜选主窗口。", "timings": timings}
            if include_image and not interrupted():
                start = time.perf_counter()
                try:
                    result.update(await capture(None))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    result["image_status"] = "capture_failed"
                    result["image_error"] = type(exc).__name__
                timings["capture_ms"] = round((time.perf_counter() - start) * 1000)
            return result
        info = await asyncio.to_thread(identity, target)
        start = time.perf_counter()
        tree = await inspect(info["handle"]) if include_controls else {"controls": [], "status": "not_requested"}
        timings["uia_ms"] = round((time.perf_counter() - start) * 1000)
        controls = tree.get("controls", []) if tree.get("ok") else []
        actionable = [c for c in controls if not c.get("offscreen") and c.get("control_type") in
                      {"ButtonControl", "EditControl", "DocumentControl", "MenuItemControl", "ListItemControl", "ComboBoxControl", "HyperlinkControl"}]
        current = await asyncio.to_thread(resolve, "", info["handle"])
        if current is None or not same_window(info, await asyncio.to_thread(identity, current)):
            return {"ok": False, "status": "window_changed", "windows": windows}
        token = secrets.token_hex(12)
        now = time.monotonic()
        for old in list(snapshots):
            if now - snapshots[old]["at"] > 60 or len(snapshots) >= 32:
                snapshots.pop(old, None)
        snapshots[token] = {"at": now, "window": info, "include_image": include_image}
        result = {"ok": True, "observation_id": token, "window": info, "windows": windows,
                  "controls": controls, "uia_status": tree.get("status"), "truncated": bool(tree.get("truncated")),
                  "route": "uia" if actionable and not tree.get("truncated") else "visual", "timings": timings,
                  "next": "完整控件树中唯一且可核对的目标才使用desktop_execute；截断/无控件走原有单步工具，界面变化后重新观察。"}
        if include_image and not interrupted():
            start = time.perf_counter()
            try:
                result.update(await capture(info))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result["image_status"] = "capture_failed"
                result["image_error"] = type(exc).__name__
            timings["capture_ms"] = round((time.perf_counter() - start) * 1000)
        # Capture can take time; never return a token for an obsolete window.
        current = await asyncio.to_thread(resolve, "", info["handle"])
        if current is None or not same_window(info, await asyncio.to_thread(identity, current)):
            snapshots.pop(token, None)
            result.update(ok=False, status="window_changed")
        return result

    @registry.tool(name="desktop_observe", description=(
        "通用桌面观察：一次获取窗口身份、紧凑控件摘要和局部截图，避免重复list_windows/截图。"
        "window或handle指定唯一窗口，不激活它；相同标题不猜。找不到唯一窗口时同时返回全窗口清单和桌面图，不发执行凭证。返回observation_id、uia/visual路径和分项耗时。"
        "优先已有文档/网页接口；控件不足用单步视觉工具，不反复读空树。"), parameters={
            "type": "object", "properties": {"window": {"type": "string"}, "handle": {"type": "integer"},
            "include_image": {"type": "boolean"}, "include_controls": {"type": "boolean"}}})
    async def desktop_observe(window="", handle=None, include_image=True, include_controls=True):
        if not isinstance(window, str) or (handle is not None and (type(handle) is not int or handle <= 0)):
            raise ValueError("window/handle无效")
        if type(include_image) is not bool or type(include_controls) is not bool:
            raise ValueError("观察选项必须为布尔值")
        return await observe(window, handle, include_image, include_controls)

    @registry.tool(name="desktop_execute", description=(
        "执行1到5个已知短步骤，每步重新检查唯一控件和窗口身份；界面/焦点变化、歧义、失败或插话立即停止。"
        "必须使用60秒内desktop_observe的observation_id，一次有效。"
        "每步tool为click_element/type_text/press_hotkey，arguments同原工具但不传句柄，expect为name/control_type/automation_id精确条件。"
        "输入/热键要求expect为当前焦点控件；热键仅允许Ctrl+A/C和导航键。wait_ms按目标出现等待0到3000ms。"
        "after可要求唯一expect控件出现、focused状态或value_equals文字相等；未核验不表示业务完成。"
        "不支持命令、任意嵌套工具、坐标批量或发送；保留原工具保护。返回逐步状态/耗时及最新观察；完成只代表步骤执行，不代表业务成功。"), parameters={
            "type": "object", "properties": {"observation_id": {"type": "string"},
                "actions": {"type": "array", "minItems": 1, "maxItems": 5, "items": {
                    "type": "object", "properties": {"tool": {"type": "string", "enum": list(_ACTIONS)},
                    "arguments": {"type": "object", "additionalProperties": False, "properties": {
                        "text": {"type": "string"}, "keys": {"type": "array", "items": {"type": "string"}},
                        "target_desc": {"type": "string"}, "control_type": {"type": "string"},
                        "automation_id": {"type": "string"}, "double": {"type": "boolean"}}},
                    "expect": {"type": "object", "minProperties": 1, "additionalProperties": False,
                        "properties": {k: {"type": "string"} for k in ("name", "control_type", "automation_id")}},
                    "after": {"type": "object", "additionalProperties": False, "required": ["expect"],
                        "properties": {"expect": {"type": "object", "properties": {
                            k: {"type": "string"} for k in ("name", "control_type", "automation_id")}, "additionalProperties": False},
                            "focused": {"type": "boolean"}, "value_equals": {"type": "string"}}},
                    "wait_ms": {"type": "integer", "minimum": 0, "maximum": 3000}}, "required": ["tool", "expect"]}}},
            "required": ["observation_id", "actions"]}, side_effect=True)
    async def desktop_execute(observation_id, actions):
        validate_plan(actions)  # Validate the whole plan before any side effects.
        if not isinstance(observation_id, str):
            raise ValueError("observation_id必须为字符串")
        saved = snapshots.get(observation_id)
        if saved is None or time.monotonic() - saved["at"] > 60:
            return {"ok": False, "status": "observation_expired", "completed": 0, "next": "重新desktop_observe；未执行步骤。"}
        snapshots.pop(observation_id)
        info = saved["window"]
        results = []
        status = "steps_completed"
        for index, item in enumerate(actions):
            start = time.perf_counter()
            if interrupted():
                status = "interrupted"
                break
            target = await asyncio.to_thread(resolve, "", info["handle"])
            if target is None or not same_window(info, await asyncio.to_thread(identity, target)):
                status = "window_changed"
                break
            if foreground() != info["handle"]:
                status = "focus_changed"
                break
            deadline = time.monotonic() + item.get("wait_ms", 0) / 1000
            while True:
                try:
                    tree = await inspect(info["handle"])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    tree = {"ok": False, "status": type(exc).__name__}
                if not tree.get("ok") or tree.get("truncated"):
                    status = "controls_incomplete"
                    break
                matches = matching_controls(tree.get("controls", []) if tree.get("ok") else [], item["expect"])
                if matches or time.monotonic() >= deadline or interrupted():
                    break
                await asyncio.sleep(min(.1, max(0, deadline - time.monotonic())))
            if status == "controls_incomplete":
                break
            if interrupted():
                status = "interrupted"
                break
            if len(matches) != 1:
                status = "target_ambiguous" if matches else "target_missing"
                break
            selected = matches[0]
            name = item["tool"]
            if selected.get("enabled") is False or selected.get("password"):
                status = "input_target_protected_or_disabled"
                break
            if name == "click_element" and _SUBMIT.search(" ".join(str(selected.get(k, "")) for k in ("name", "automation_id"))):
                status = "critical_action_requires_single_tool"
                break
            if name in {"type_text", "press_hotkey"} and not selected.get("focused"):
                status = "input_focus_unconfirmed"
                break
            if name == "type_text" and selected.get("control_type") not in _INPUT_TYPES:
                status = "input_target_invalid"
                break
            target = await asyncio.to_thread(resolve, "", info["handle"])
            if (target is None or not same_window(info, await asyncio.to_thread(identity, target))
                    or foreground() != info["handle"]):
                status = "window_changed"
                break
            args = dict(item.get("arguments", {}), handle=info["handle"])
            if name == "click_element":
                # Bind the actual unique control rather than trusting a second fuzzy description.
                args.update(target_desc=selected.get("name", ""), control_type=selected.get("control_type", ""),
                            automation_id=selected.get("automation_id", ""), allow_visual_fallback=False)
            if interrupted():
                status = "interrupted"
                break
            def verify_input_window(actual_control=None):
                current = resolve("", info["handle"])
                if current is None or foreground() != info["handle"] or not same_window(info, identity(current)):
                    raise RuntimeError("desktop_plan_window_changed_before_input")
                if actual_control is not None:
                    actual = {"name": str(actual_control.Name or ""),
                              "control_type": str(actual_control.ControlTypeName or ""),
                              "automation_id": str(actual_control.AutomationId or "")}
                    if (any(actual[k] != selected.get(k, "") for k in actual)
                            or not bool(actual_control.IsEnabled) or bool(actual_control.IsPassword)):
                        raise RuntimeError("desktop_plan_control_changed_before_click")
                if name in {"type_text", "press_hotkey"}:
                    import uiautomation as auto
                    # The lease may have waited after the async observation. Check
                    # the real focused element again immediately at input time.
                    with auto.UIAutomationInitializerInThread():
                        control = auto.GetFocusedControl()
                        actual = {"name": str(control.Name or ""),
                                  "control_type": str(control.ControlTypeName or ""),
                                  "automation_id": str(control.AutomationId or "")}
                        if (not matching_controls([actual], item["expect"]) or
                                bool(control.IsPassword) or not bool(control.IsEnabled)):
                            raise RuntimeError("desktop_plan_input_focus_changed")
            input_token = system_control.INPUT_PRECONDITION.set(verify_input_window)
            try:
                tool = registry.get(name)
                if tool is None or tool.handler.__module__ != "backend.modules.computer.computer_module":
                    status = "tool_unavailable"
                    break
                result = await registry.call(name, args)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                results.append({"index": index + 1, "tool": name, "ok": False,
                                "status": "action_unconfirmed", "error": type(exc).__name__,
                                "action_ms": round((time.perf_counter() - start) * 1000)})
                status = "action_unconfirmed"
                break
            finally:
                system_control.INPUT_PRECONDITION.reset(input_token)
            record = {"index": index + 1, "tool": name, "ok": result.get("ok") is True,
                      "status": result.get("status", "executed"),
                      "action_ms": round((time.perf_counter() - start) * 1000)}
            results.append(record)
            if not record["ok"]:
                status = "action_unconfirmed"
                break
            after = item.get("after")
            if after is not None:
                record["verified"] = False
                deadline = time.monotonic() + item.get("wait_ms", 0) / 1000
                try:
                    while True:
                        if interrupted():
                            status = "interrupted"
                            break
                        target = await asyncio.to_thread(resolve, "", info["handle"])
                        if target is None or not same_window(info, await asyncio.to_thread(identity, target)) or foreground() != info["handle"]:
                            status = "window_changed"
                            break
                        options = {"value_check": {"expect": after["expect"], "equals": after["value_equals"]}} if "value_equals" in after else {}
                        tree = await inspect(info["handle"], **options)
                        matches = matching_controls(tree.get("controls", []), after["expect"])
                        verified = (tree.get("ok") and not tree.get("truncated") and len(matches) == 1
                            and not matches[0].get("password") and matches[0].get("enabled") is not False
                            and ("focused" not in after or matches[0].get("focused") is after["focused"])
                            and ("value_equals" not in after or tree.get("value_matches") is True))
                        current = await asyncio.to_thread(resolve, "", info["handle"])
                        if interrupted():
                            status = "interrupted"
                            break
                        if current is None or not same_window(info, await asyncio.to_thread(identity, current)) or foreground() != info["handle"]:
                            status = "window_changed"
                            break
                        if verified:
                            record["verified"] = True
                            break
                        if time.monotonic() >= deadline:
                            status = "postcondition_unconfirmed"
                            break
                        await asyncio.sleep(.05)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    status = "postcondition_unconfirmed"
                if status != "steps_completed":
                    break
        response = {"ok": status == "steps_completed", "status": status,
                    "completed": sum(r["ok"] for r in results), "verified": sum(r.get("verified", False) for r in results), "steps": results,
                    "note": "只报告实际步骤；未执行的后续步骤没有副作用。结果不确定不自动重试，业务成功需核验。"}
        if not interrupted():
            try:
                response["observation"] = await observe(handle=info["handle"], include_image=saved["include_image"])
                for key in ("_observation_image", "_display_image"):
                    if key in response["observation"]:
                        response[key] = response["observation"].pop(key)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                response["observation_error"] = type(exc).__name__
        return response
