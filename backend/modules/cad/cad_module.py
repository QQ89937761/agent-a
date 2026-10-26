"""AutoCAD COM 模块:连接、读图、基础改图。

用 pywin32 驱动本机 AutoCAD(ProgID "AutoCAD.Application"),供施工方在设计院
给的图纸上做标注、改文字、套图框、改图层。所有读图都返回单位/DWG 版本,
所有涉及坐标和长度的参数一律用"图纸自身单位"(建筑常是毫米、道路常是米,
混用会差 1000 倍)。

安全红线:
- 复用用户已开的 AutoCAD 时绝不关闭它、绝不未经确认保存(照抄 Office COM
  的所有权保护)。
- 任何修改类工具执行前,先把 DWG 强制备份一份到隔离工作区 backups/cad
  (唯一文件名，自动清理不会删除安全备份);备份失败就拒绝修改。CAD 图纸是交付物,
  AutoCAD 没有自动恢复,改坏可能无法回退。

COM 只在 Windows + 已装 pywin32/AutoCAD 时可用;非 Windows 环境 import 优雅
失败,register_tools 跳过注册并打印提示。
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import math
import os
import shutil
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from backend.core import isolation_workspace
from backend.core.tools import ToolRegistry
from backend.modules.computer import system_control
from backend.modules.office.fs_module import _resolve, _resolve_for_write

_IS_WINDOWS = sys.platform == "win32"
_CAD_PROGID = "AutoCAD.Application"
_MAX_ENTITIES = 500  # 读实体时单次返回条数上限,防止大图纸撑爆上下文
_MAX_DETAIL = 200    # dry_run 明细上限
_MAX_VERTICES = 64   # 单条多段线返回的顶点上限(墙线常几十个点,再多就是等高线之类)

# 探测走通的 COM 策略,缓存下来避免每次工具调用都重跑清缓存(很慢)。
# 测试里可以直接 reset 成 None 强制重探测。注意 gen_py 清缓存写文件不是线程安全的,
# 用 _GEN_LOCK 串行化。
_RESOLVED_STRATEGY: str | None = None
_GEN_LOCK = threading.Lock()
# rebuild 策略里"清 gen_py 缓存"是一次性修复:一旦做过就记住,后续调用只做"取对象"
# 这步,不再重复清缓存(清 + Rebuild 可能几十秒)。
_GEN_PY_CLEARED = False
# 冷启动等待的前端进度回调(register_tools 时注入)。为空不崩(测试/Linux 环境)。
_PROGRESS_CB: contextvars.ContextVar[Callable[[str], None] | None] = contextvars.ContextVar(
    "cad_progress_callback", default=None,
)

# AutoCAD 启动/忙时会用 RPC_E_CALL_REJECTED 拒掉调用(正在跑命令、有模态对话框、
# 刚启动还没进入消息循环)。这不是错误,退避重试就行。
_COM_RETRY = 4
_COM_RETRY_SLEEP = 2.0
# 刚 Dispatch 起的新实例冷启动慢(首次加载配置/插件常 15-30 秒),给 60 秒总预算。
# 只在我们自己起的实例(own_app=True)上用;用户已开的实例被拒是模态框,不该等 60 秒。
_COLD_START_RETRY = 30
_COLD_START_SLEEP = 2.0
# Dispatch 失败"类未注册"(AutoCAD LT 不带 ActiveX)的特征串。
_DISPATCH_ERROR_MARKERS = (
    "无效的类字符串", "class not registered", "-2147221164", "0x80040154",
)
_BUSY_MARKERS = (
    "call was rejected by callee",
    "被调用方拒绝",
    "被呼叫方拒绝",
    "rejected by callee",
    "-2147418111",   # 0x80010001 RPC_E_CALL_REJECTED
    "-2147417846",   # 0x8001010A RPC_E_SERVERCALL_RETRYLATER
    "0x80010001",
    "0x8001010a",
)

# DWG 文件头前 6 字节直接写着格式版本,比问 AutoCAD 可靠。存回去时按这个版本存,
# 免得把设计院的旧格式图纸升级成新版、对方打不开。
# 值是 AcSaveAsType 枚举(ActiveX 文档),不同 AutoCAD 版本大体一致。
_DWG_HEADER_TO_SAVETYPE = {
    "AC1015": 12,  # 2000
    "AC1018": 24,  # 2004
    "AC1021": 36,  # 2007
    "AC1024": 48,  # 2010
    "AC1027": 60,  # 2013
    "AC1032": 64,  # 2018
}
_DWG_HEADER_LABELS = {
    "AC1014": "R14", "AC1015": "AutoCAD 2000", "AC1018": "AutoCAD 2004",
    "AC1021": "AutoCAD 2007", "AC1024": "AutoCAD 2010",
    "AC1027": "AutoCAD 2013", "AC1032": "AutoCAD 2018+",
}


def _is_busy_error(exc: BaseException) -> bool:
    text = f"{exc}".lower()
    return any(marker in text for marker in _BUSY_MARKERS)


def _retry_on_busy(fn, *, what: str, retry_missing: bool = False):
    """AutoCAD 忙就退避重试；指定的启动期属性读取也可重试 AttributeError。"""
    import time
    last: BaseException | None = None
    for attempt in range(_COM_RETRY):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if not (_is_busy_error(exc) or (retry_missing and isinstance(exc, AttributeError))) or attempt == _COM_RETRY - 1:
                raise
            time.sleep(_COM_RETRY_SLEEP * (attempt + 1))
    raise RuntimeError(f"{what} 反复被 AutoCAD 拒绝: {last}")


def set_progress_callback(cb: Callable[[str], None] | None) -> None:
    """注入冷启动等待的前端进度回调;传 None 清除。为空不崩(测试/Linux 环境)。"""
    _PROGRESS_CB.set(cb)


def _report_progress(message: str) -> None:
    """冷启动等待进度:打到控制台 + 若有回调则通知前端。回调异常不吞主流程。"""
    try:
        print(message)
    except Exception:
        pass
    cb = _PROGRESS_CB.get()
    if cb is not None:
        try:
            cb(message)
        except Exception:
            pass


def _wait_for_cold_start(fn, *, what: str):
    """冷启动时旧版AutoCAD会短暂缺少Documents类型信息；与RPC忙一样等待初始化。"""
    import time
    last: BaseException | None = None
    for attempt in range(_COLD_START_RETRY):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if (not _is_busy_error(exc) and not isinstance(exc, AttributeError)) or attempt == _COLD_START_RETRY - 1:
                raise
            seconds = int((attempt + 1) * _COLD_START_SLEEP)
            # 第一次 + 之后每 10 秒(5 次 × 2s)一条,别每 2 秒刷爆界面
            if attempt == 0 or attempt % 5 == 0:
                _report_progress(f"正在启动 AutoCAD，已等 {seconds} 秒…")
            time.sleep(_COLD_START_SLEEP)
    raise RuntimeError(f"{what} 反复被 AutoCAD 拒绝: {last}")


def _error_nature(err: str) -> str:
    """按失败串判断性质,避免 COM 忙错误被归到普通对象错误。"""
    low = err.lower()
    if _is_busy_error(RuntimeError(err)):
        return "busy"
    if any(marker in low for marker in _DISPATCH_ERROR_MARKERS):
        return "dispatch"
    return "other"


def _numbers(value: Any) -> list[float]:
    """COM 返回的 variant 数组 / 元组转成可 JSON 序列化的浮点列表。

    GetVariable("EXTMIN") 之类返回的是 COM SAFEARRAY,直接进 JSON 会炸,
    必须在这里落地成 list[float]。
    """
    if value is None:
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    try:
        return [float(v) for v in value]
    except (TypeError, ValueError):
        return []


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dwg_header(path: Path) -> str:
    """读 DWG 前 6 字节的版本标识,如 AC1032。读不到返回空串。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(6).decode("ascii", "ignore")
    except OSError:
        return ""

# INSUNITS 系统变量含义(建筑常用 4=毫米,道路常用 6=米)
_UNIT_LABELS = {
    0: "无单位",
    1: "英寸(in)",
    2: "英尺(ft)",
    3: "英里(mi)",
    4: "毫米(mm)",
    5: "厘米(cm)",
    6: "米(m)",
    7: "千米(km)",
}


def _com_available() -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        import win32com.client  # noqa: F401
        return True
    except ImportError:
        return False


def _unit_label(insunits: Any) -> str:
    try:
        value = int(insunits)
    except (TypeError, ValueError):
        value = -1
    return _UNIT_LABELS.get(value, f"未知({insunits})")


def _doc_path(doc: Any) -> str:
    """已打开文档的完整路径;从未存过盘的新图返回空串(FullName 可能为空或抛错)。"""
    try:
        full = str(doc.FullName or "")
    except Exception:
        return ""
    return full


def _running_cad(client: Any) -> Any | None:
    """取用户已经开着的 AutoCAD 实例;没开返回 None。"""
    try:
        return client.GetActiveObject(_CAD_PROGID)
    except Exception:
        try:
            raw = client.pythoncom.GetActiveObject(_CAD_PROGID).QueryInterface(client.pythoncom.IID_IDispatch)
            return _dynamic_cad_object(client, raw)
        except Exception:
            return None


def _new_cad(client: Any) -> Any:
    """新起一个 AutoCAD 实例。

    注意两件真机事实:
    - AutoCAD 的 Application.Visible 不像 Word/Excel,设 False 会抛错,而且不设它
      AutoCAD 本来也会显示窗口。这里根本不碰 Visible。
    - AutoCAD 启动慢,COM 对象拿到后往往还没进消息循环,第一个调用会被
      RPC_E_CALL_REJECTED 拒掉。所以后续调用统一走 _retry_on_busy。
    - AutoCAD LT 不带 ActiveX,Dispatch 会直接失败,这里给出人话提示。
    """
    try:
        return client.Dispatch(_CAD_PROGID)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "启动 AutoCAD 失败。请确认:1) 本机装的是完整版 AutoCAD 而不是 "
            f"AutoCAD LT(LT 不支持 COM/ActiveX,无法被程序驱动);2) AutoCAD 能正常手动打开。原始错误:{exc}"
        ) from exc


def _silent_pdf_pc3(app: Any, plot_config: str, directory: Path) -> Path:
    """Clone the installed PDF driver with viewer launch disabled; never edit user settings."""
    import re
    import struct
    import zlib

    search_path = str(app.Preferences.Files.PrinterConfigPath)
    source = next((Path(os.path.expandvars(folder.strip().strip('"'))) / plot_config
                   for folder in search_path.split(";")
                   if folder.strip() and (Path(os.path.expandvars(folder.strip().strip('"'))) / plot_config).is_file()), None)
    if source is None:
        raise RuntimeError(f"找不到 PDF 打印配置文件:{plot_config}；未打印，避免自动打开预览")
    header = b"PIAFILEVERSION_2.0,PC3VER1,compress\r\npmzlibcodec"
    if source.stat().st_size > 4 * 1024 * 1024:
        raise RuntimeError("PDF PC3 配置文件过大，未打印")
    data = source.read_bytes()
    if not data.startswith(header) or len(data) < len(header) + 12:
        raise RuntimeError("不支持的 PDF PC3 格式，无法安全关闭自动预览，未打印")
    checksum, plain_size, packed_size = struct.unpack("<III", data[len(header):len(header) + 12])
    packed = data[len(header) + 12:]
    if packed_size != len(packed) or checksum != zlib.adler32(packed):
        raise RuntimeError("PDF PC3 配置校验失败，未打印")
    decoder = zlib.decompressobj()
    plain = decoder.decompress(packed, 8 * 1024 * 1024 + 1)
    if (len(plain) > 8 * 1024 * 1024 or len(plain) != plain_size or not decoder.eof
            or decoder.unused_data or decoder.unconsumed_tail):
        raise RuntimeError("PDF PC3 配置内容无效，未打印")
    # Autodesk PDF driver custom property, verified on the installed AutoCAD 2014 PC3.
    plain, count = re.subn(rb'(\bname="View_New_File[ \t]*\r?\n[ \t]*value=)(TRUE|FALSE)\b',
                           lambda match: match[1] + b"FALSE", plain)
    if count != 1:
        raise RuntimeError("PDF 驱动没有唯一可识别的自动预览选项，未打印；请先关闭驱动的完成后打开选项")
    packed = zlib.compress(plain)
    target = directory / "xiaoa-no-preview-pdf.pc3"
    target.write_bytes(header + struct.pack("<III", zlib.adler32(packed), len(plain), len(packed)) + packed)
    return target


def _app_usable(app: Any, *, cold_start: bool = False) -> tuple[bool, str]:
    """验证拿到的对象真能取 Documents 集合(取 .Count 即可)。返回 (可用, 性质)。

    忙(RPC_E_CALL_REJECTED 等 _BUSY_MARKERS)是"启动期/正忙",退避重试,不能判死;
    其它异常通常表示对象不可用；刚启动时 AttributeError 也可能是类型信息未就绪。
    cold_start=True 给 RPC 忙和 AttributeError 共 60 秒预算，否则用普通 12 秒预算。
    性质: "ok" / "busy"(重试耗尽仍是忙) / "bad"(非忙异常) /
          "unavailable"(对象本身是 None/拿不到,策略不适用,不算坏)。
    """
    if app is None:
        return False, "unavailable"
    try:
        if cold_start:
            _wait_for_cold_start(lambda: app.Documents.Count, what="读取 Documents")
        else:
            _retry_on_busy(lambda: app.Documents.Count, what="读取 Documents")
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        nature = "busy" if _is_busy_error(exc) else "bad"
        print(f"[cad] Documents 探测失败 ({nature}):{type(exc).__name__}: {exc}")
        return False, nature


def _has_running_instance(client: Any) -> bool:
    """AutoCAD 是不是已经在跑(ROT 里有实例)。Dispatch/EnsureDispatch 会附着到它。"""
    try:
        client.GetActiveObject(_CAD_PROGID)
        return True
    except Exception:
        # ponytail: a generated-wrapper failure does not mean the user's CAD is absent.
        try:
            client.pythoncom.GetActiveObject(_CAD_PROGID)
            return True
        except Exception:
            return False


def _cleanup_started_instance(client: Any, had_running: bool, existing_pids: set[int] | None = None) -> str | None:
    """探测失败收尾:若我们自己启动过实例(had_running=False),Quit 掉,别泄漏 acad.exe。

    我们 Dispatch 起的实例现在已在 ROT 里(GetActiveObject 能拿到它)。返回 Quit
    失败的提示(无则 None);用户已开着的实例(had_running=True)绝不碰。
    """
    if had_running and existing_pids is None:
        return None
    started = _running_cad(client)
    if started is None:
        return None
    if existing_pids is not None:
        try:
            import win32process
            _, pid = win32process.GetWindowThreadProcessId(int(started.HWND))
            if pid <= 0 or pid in existing_pids:
                return None
        except Exception:
            return None  # Cannot prove this process was started during our probe.
    try:
        started.Quit()
        return None
    except Exception:
        return "另外:小A 启动的 AutoCAD 没能自动关闭,如有残留请手动结束 acad.exe 进程"


def _gen_py_path(client: Any) -> str:
    try:
        return str(client.gencache.GetGeneratePath())
    except Exception:
        return "(无法获取 gen_py 路径)"


def _clear_gen_py(client: Any) -> str | None:
    """清空 pywin32 类型库缓存(gen_py)并重建。返回 None 表示成功,否则返回错误说明。

    清缓存有副作用(影响本机其它用 pywin32 的程序),只在 late/application 都失败后
    才调用,并且打印一行说明。gencache 写文件不是线程安全的,串行化。
    """
    path = _gen_py_path(client)
    with _GEN_LOCK:
        try:
            if path and path != "(无法获取 gen_py 路径)" and os.path.isdir(path):
                print(f"[cad] 清空 pywin32 类型库缓存(gen_py):{path}")
                shutil.rmtree(path, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            return f"清缓存失败:{exc}"
        try:
            client.gencache.Rebuild()
        except Exception as exc:  # noqa: BLE001
            return f"gencache.Rebuild 失败:{exc}"
    return None


def _cad_env_error(gen_py: str, attempts: list[str], nature: str) -> str:
    """所有策略全败时,按最后一次失败的性质给措辞,别把三种可能一次性倒给用户。"""
    if nature == "busy":
        return (
            "AutoCAD 启动太慢或一直在忙（可能有对话框等着你点），已等待约 60 秒仍无响应。"
            "请手动打开 AutoCAD、确认没有弹窗，再让我重试。没有动你的类型库缓存。"
        )
    if nature == "dispatch":
        return (
            "无法启动 AutoCAD：报错是「无效的类字符串」之类，可能装的是 AutoCAD LT——"
            "LT 不带 ActiveX/COM，无法被程序驱动。请确认本机装的是完整版 AutoCAD。"
        )
    if nature == "bad":
        return (
            "AutoCAD COM 对象未能提供可用的 Documents，不能仅据此认定图纸或 gen_py 缓存损坏。"
            "请确认 AutoCAD 没有启动提示或模态弹窗；原始异常已写入小A日志。"
            f"类型库缓存位置:{gen_py}。已尝试:" + " / ".join(attempts)
        )
    return "无法通过 COM 驱动 AutoCAD。已尝试:" + " / ".join(attempts)


def _late_object(client: Any, prefer_running: bool) -> tuple[Any, bool, str | None]:
    """策略 A:优先复用运行中的 AutoCAD(GetActiveObject),否则新起(Dispatch)。

    返回 (obj, own_app, error)。own_app=True 表示这个实例是我们 Dispatch 起的。
    """
    errors: list[str] = []
    if prefer_running:
        try:
            return client.GetActiveObject(_CAD_PROGID), False, None
        except Exception as exc:  # noqa: BLE001
            errors.append(f"GetActiveObject 失败:{exc}")
    try:
        return client.Dispatch(_CAD_PROGID), True, None
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Dispatch 失败:{exc}")
    return None, True, ";".join(errors)


def _dynamic_cad_object(client: Any, raw: Any) -> Any:
    """Keep application, collections and method-returned children free of gen_py."""
    dynamic = client.dynamic

    class CadDispatch(dynamic.CDispatch):
        def __AttrToID__(self, attr):
            # pywin32 otherwise swallows RPC busy during name lookup as AttributeError.
            return _retry_on_busy(lambda: self._oleobj_.GetIDsOfNames(0, attr), what=f"读取 COM 成员 {attr}")

        def _wrap_dispatch_(self, ob, userName=None, returnCLSID=None):
            return dynamic.DumbDispatch(ob, userName, createClass=type(self))

    # ponytail: no type-info methods, which would wrap Item/Open results via client.Dispatch.
    return dynamic.DumbDispatch(raw, createClass=CadDispatch)


def _cad_pid_is_new(app: Any, existing_pids: set[int]) -> bool:
    try:
        import win32process
        _, pid = win32process.GetWindowThreadProcessId(int(app.HWND))
        return pid > 0 and pid not in existing_pids
    except Exception:
        return False  # Dispatch alone does not prove that we started a process.


def _run_strategy(
    client: Any, strategy: str, prefer_running: bool, had_running: bool,
) -> tuple[Any, bool, str | None, str]:
    """跑一条策略,返回 (app, own_app, error, nature);error 为 None 表示成功。

    had_running 是 _resolve_cad_app 在**整个探测链路开始之前**拍的快照,所有策略
    共用这一个值。own_app = not had_running。不能在各条策略内部重新探——前面的
    策略(尤其 late)可能已经 Dispatch 起过实例并让它进了 ROT,再探会把"我们自己
    起的"误判成"用户已开的"(不 Quit,泄漏 acad.exe)。

    nature 是失败性质:busy(启动期被拒)/ dispatch(类未注册,LT)/ bad(取不到
    Documents)/ other。只有确认不是 busy 才允许走到 rebuild(清 gen_py 是破坏性动作)。
    """
    own = not had_running
    if strategy == "dynamic":
        try:
            # ponytail: use raw IDispatch and dynamic child wrappers, never gen_py.
            if prefer_running and had_running:
                raw = client.pythoncom.GetActiveObject(_CAD_PROGID).QueryInterface(client.pythoncom.IID_IDispatch)
                obj = _dynamic_cad_object(client, raw)
            else:
                obj = _dynamic_cad_object(client, _CAD_PROGID)
        except Exception as exc:  # noqa: BLE001
            nature = "busy" if _is_busy_error(exc) else _error_nature(str(exc))
            return None, own, f"纯动态绑定失败:{exc}", nature
        usable, nature = _app_usable(obj, cold_start=own)
        if usable:
            return obj, own, None, "ok"
        return None, own, "纯动态绑定后仍取不到 Documents", nature
    if strategy == "late":
        existing_pids = system_control._process_ids_by_name("acad.exe") if had_running else set()
        obj, late_own, err = _late_object(client, prefer_running)
        if err is not None:
            return None, own, err, _error_nature(err)
        usable, nature = _app_usable(obj, cold_start=own or late_own)
        if usable:
            return obj, own or (late_own and _cad_pid_is_new(obj, existing_pids)), None, "ok"
        return None, own, "取不到 Documents", nature
    if strategy == "application":
        existing_pids = system_control._process_ids_by_name("acad.exe") if had_running else set()
        obj, late_own, err = _late_object(client, prefer_running)
        if err is not None:
            return None, own, err, _error_nature(err)
        if obj is None:
            return None, own, "拿不到 Application 对象", "unavailable"
        try:
            app2 = obj.Application
        except Exception as exc:  # noqa: BLE001
            return None, own, f".Application 取不到:{exc}", ("busy" if _is_busy_error(exc) else "bad")
        usable, nature = _app_usable(app2, cold_start=own or late_own)
        if usable:
            return app2, own or (late_own and _cad_pid_is_new(app2, existing_pids)), None, "ok"
        return None, own, "Application 对象上仍取不到 Documents", nature
    if strategy == "rebuild":
        global _GEN_PY_CLEARED
        if not _GEN_PY_CLEARED:
            err = _clear_gen_py(client)
            if err is not None:
                return None, own, err, "other"
            _GEN_PY_CLEARED = True
        try:
            obj3 = client.Dispatch(_CAD_PROGID)
        except Exception as exc:  # noqa: BLE001
            return None, own, f"清缓存后 Dispatch 失败:{exc}", _error_nature(str(exc))
        usable, nature = _app_usable(obj3, cold_start=own)
        if usable:
            return obj3, own, None, "ok"
        return None, own, "清缓存后仍取不到 Documents", nature
    if strategy == "early":
        try:
            app4 = client.gencache.EnsureDispatch(_CAD_PROGID)
        except Exception as exc:  # noqa: BLE001
            return None, own, f"EnsureDispatch 失败:{exc}", _error_nature(str(exc))
        usable, nature = _app_usable(app4, cold_start=own)
        if usable:
            return app4, own, None, "ok"
        return None, own, "早绑定后仍取不到 Documents", nature
    return None, own, f"未知策略:{strategy}", "other"


def _probe(client: Any, prefer_running: bool, had_running: bool, existing_pids: set[int] | None = None) -> tuple[Any, str, bool]:
    """跑一遍完整探测,全败抛人话错误。

    非破坏性的先试(late/dynamic/application/early 都不删东西),rebuild 会 rmtree 掉
    gen_py 目录,放最后且要过闸——有 bad 且完全没有 busy 时才允许清缓存；COM 忙可能
    被 pywin32 包装成 AttributeError，因此只要出现 busy 就不冒险破坏类型库缓存。
    """
    failures: list[str] = []
    natures: list[str] = []
    for strategy in ("late", "dynamic", "application", "early"):
        app, own, err, nature = _run_strategy(client, strategy, prefer_running, had_running)
        if err is None:
            return app, strategy, own
        failures.append(f"{strategy}(失败: {err})")
        natures.append(nature)
    # rebuild 闸门:前面没出现过 bad 就不清 gen_py(44.1)。unavailable 不算 bad(44.2)。
    if "bad" in natures and "busy" not in natures:
        app, own, err, nature = _run_strategy(client, "rebuild", prefer_running, had_running)
        if err is None:
            return app, "rebuild", own
        failures.append(f"rebuild(失败: {err})")
        natures.append(nature)
    else:
        reason = "存在 AutoCAD 忙/拒绝调用错误" if "busy" in natures else "没有'对象模型损坏'的证据"
        failures.append(f"rebuild(跳过:{reason},不冒险清类型库缓存)")
    # Busy takes precedence: pywin32 may expose a rejected COM call as AttributeError.
    if "dispatch" in natures:
        final_nature = "dispatch"
    elif "busy" in natures:
        final_nature = "busy"
    elif "bad" in natures:
        final_nature = "bad"
    else:
        final_nature = "other"
    note = _cleanup_started_instance(client, had_running, existing_pids)
    message = _cad_env_error(_gen_py_path(client), failures, final_nature)
    if note:
        message += "\n" + note
    raise RuntimeError(message)


def _is_task_cad(app: Any) -> bool:
    if not system_control.owned_application("autocad"):
        return False
    expected = system_control.owned_application_pid("autocad")
    if expected is None:
        return False
    try:
        import win32process
        return win32process.GetWindowThreadProcessId(int(app.HWND))[1] == expected
    except Exception:
        return False


def _retain_task_cad(app: Any, own: bool, existing_pids: set[int] | None = None) -> bool:
    if system_control._CONTROL_SESSION.get() is None:
        return own
    try:
        import win32process
        _, pid = win32process.GetWindowThreadProcessId(int(app.HWND))
        if pid <= 0:
            return own
        if existing_pids is not None:
            if pid in existing_pids:
                return False  # Dispatch attached to an existing process: do not claim or quit it.
        elif not own:
            return False
        system_control.mark_owned_application("autocad", pid)
        return False  # The task cleanup owns Quit; tools still close their documents.
    except Exception:
        return own  # Without a verified PID, keep the existing per-call cleanup.


def _resolve_cad_app(
    client: Any, *, prefer_running: bool = True,
) -> tuple[Any, str, bool]:
    """取到并验证可用的 Application 对象,多策略兜底。返回 (app, strategy, own_app)。

    own_app 回答的是"这个 acad.exe 是不是我们这次调用启动的"。所以快照必须在
    **整个探测链路开始之前**拍一次(late 内部会自己 Dispatch 起实例),所有策略
    和缓存命中路径都共用这一个值。

    走通的策略缓存下来(_RESOLVED_STRATEGY),后续调用直接从该策略起步,不再每次
    重跑一遍完整探测——rebuild 要清 gen_py + Rebuild,可能几十秒,每次都做会让
    每个 CAD 工具都超时。
    """
    global _RESOLVED_STRATEGY
    existing_pids = (system_control._process_ids_by_name("acad.exe")
                     if system_control._CONTROL_SESSION.get() is not None else None)
    had_running = _has_running_instance(client)  # 入口快照,整个探测链路共用
    # A remembered task PID may no longer be the application in the ROT.
    # Preserve the real running snapshot; PID checks decide whether to retain it.
    probe_had_running = had_running
    if _RESOLVED_STRATEGY is not None:
        app, own, err, _nature = _run_strategy(
            client, _RESOLVED_STRATEGY, prefer_running, probe_had_running,
        )
        if err is None:
            # launch_app 启动的实例归当前任务所有，但不属于这个 CAD 工具的
            # 临时生命周期；读图/写图结束后必须保留给后续可视化步骤。
            return app, _RESOLVED_STRATEGY, _retain_task_cad(app, own and not _is_task_cad(app), existing_pids)
        _RESOLVED_STRATEGY = None  # 缓存的策略失效了,重新探测

    app, strategy, own = _probe(client, prefer_running, probe_had_running, existing_pids)
    _RESOLVED_STRATEGY = strategy
    # 同一任务已经通过 launch_app 打开的 CAD 不由单个 CAD 工具关闭。
    return app, strategy, _retain_task_cad(app, own and not _is_task_cad(app), existing_pids)


def _cad_documents(app: Any) -> list[Any]:
    documents = _retry_on_busy(lambda: app.Documents, what="读取 Documents", retry_missing=True)
    if getattr(documents, "_oleobj_", None) is None:
        return [documents.Item(index) for index in range(documents.Count)]
    import pythoncom
    import win32com.client
    # AutoCAD 2014 can retain the closed startup drawing in Count. Its
    # IEnumVARIANT contains only live documents; never skip failed Item calls.
    enum = documents._oleobj_.InvokeTypes(
        pythoncom.DISPID_NEWENUM, 0,
        pythoncom.DISPATCH_METHOD | pythoncom.DISPATCH_PROPERTYGET, (13, 10), (),
    )
    return [_dynamic_cad_object(win32com.client, item)
            for item in enum.QueryInterface(pythoncom.IID_IEnumVARIANT)]


def _open_cad_drawing(client: Any, path: Path) -> tuple[Any, Any, bool, bool]:
    """返回 (app, doc, own_doc, own_app)。

    own_doc/own_app 分开记,因为存在"复用用户已开的 AutoCAD、但图纸是我们打开的"
    这种情况——此时可以关掉文档,但绝不能 Quit 用户的 AutoCAD。
    """
    target = path.resolve()
    app, _strategy, own_app = _resolve_cad_app(client, prefer_running=True)
    if getattr(app, "_oleobj_", None) is not None:
        app = _dynamic_cad_object(client, app)

    # 已经开着这张图?直接复用,不取得文档所有权。
    def _scan() -> Any | None:
        for doc in _cad_documents(app):
            existing = _doc_path(doc)
            if not existing:
                continue  # 未存盘的新图,不参与匹配
            try:
                if Path(existing).resolve() == target:
                    return doc
            except OSError:
                continue
        return None

    # 39.2:Documents 整个取不到(环境缺陷)必须原样抛出去,不能吞成"打不开图纸"。
    # _scan 内部已只兜单个文档的读取失败(OSError),这里不再有 except Exception 吞错。
    # 44.4:枚举和打开失败都要收尾——若实例是我们起的(own_app),Quit 掉再抛,别泄漏 acad.exe。
    try:
        found = _retry_on_busy(_scan, what="枚举已打开的图纸")
        if found is not None:
            return app, found, False, own_app  # 用户的图纸 + 用户的 AutoCAD,两者都不动
        doc = _retry_on_busy(
            lambda: app.Documents.Open(str(target)), what=f"打开图纸 {target.name}", retry_missing=True,
        )
    except Exception:
        if own_app:
            try:
                app.Quit()
            except Exception:
                pass
        raise
    return app, doc, True, own_app


def _release_cad_drawing(app: Any, doc: Any, own_doc: bool, own_app: bool) -> None:
    if own_doc:
        try:
            doc.Close(False)
        except Exception:
            pass
    if own_app:  # 只关我们自己起的 AutoCAD,用户开的绝不 Quit
        try:
            app.Quit()
            system_control.clear_owned_application("autocad")
        except Exception:
            pass


def _close_task_owned_autocad() -> bool:
    """Close only a task-owned CAD session, and never dismiss unsaved-work prompts."""
    if not system_control.owned_application("autocad"):
        return False
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            app = _running_cad(win32com.client)
            if app is None:
                return False
            owned_pid = system_control.owned_application_pid("autocad")
            if owned_pid is None:
                logging.getLogger(__name__).warning(
                    "task-owned AutoCAD PID is unknown; refusing to close an unverified COM instance"
                )
                return False
            import win32process
            _thread_id, actual_pid = win32process.GetWindowThreadProcessId(int(app.HWND))
            if int(actual_pid) != owned_pid:
                logging.getLogger(__name__).warning(
                    "AutoCAD COM PID mismatch: expected=%s actual=%s; refusing to close",
                    owned_pid, actual_pid,
                )
                return False
            documents = _retry_on_busy(lambda: _cad_documents(app), what="枚举任务图纸")
            if any(not doc.Saved for doc in documents):
                logging.getLogger(__name__).warning(
                    "task-owned AutoCAD has unsaved drawing; leaving it open to preserve changes"
                )
                return False
            app.Quit()
            system_control.clear_owned_application("autocad")
            return True
        finally:
            pythoncom.CoUninitialize()
    except Exception as exc:
        logging.getLogger(__name__).exception(
            "task-owned AutoCAD did not close; a modal dialog may still be open"
        )
        return False


def _modify_summary(saved_as: str, backup: str, own_app: bool) -> str:
    """给改图工具拼一句人话收尾,如实反映 own_app(关了就说关了,没关就说为什么没关)。"""
    backup_name = backup if backup else "(无)"
    if own_app:
        close_note = "已关闭由小A 启动的 AutoCAD"
    elif system_control.owned_application("autocad"):
        close_note = "AutoCAD 由当前任务复用，任务结束时检查未保存图纸后关闭"
    else:
        close_note = "AutoCAD 未被关闭(图纸是你原本就开着的)"
    return f"已保存({saved_as}),原文件已备份为 {backup_name},{close_note}。"


def _backup_drawing(path: Path) -> str:
    """修改前强制备份;失败时抛异常拒绝修改。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    directory = isolation_workspace.workspace() / "backups" / "cad"
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / f"{path.stem}.备份-{stamp}-{uuid.uuid4().hex[:12]}{path.suffix}"
    shutil.copy2(path, backup)
    if not backup.is_file():
        raise RuntimeError("备份文件未能生成,已拒绝修改(请检查磁盘空间/目录权限)。")
    return str(backup)


def _lock_files(path: Path) -> list[str]:
    """AutoCAD 打开图纸时会在同目录留 .dwl/.dwl2 锁文件。存在就说明别处正开着它。"""
    found = []
    for suffix in (".dwl", ".dwl2"):
        candidate = path.with_suffix(suffix)
        if candidate.is_file():
            found.append(candidate.name)
    return found


def _check_writable(path: Path, pre_locks: list[str]) -> None:
    """改图前先确认这文件真能写。写不了要在动手之前就说清楚,别等保存时甩错误码。

    pre_locks 必须是**我们自己打开图纸之前**拍的锁文件快照——我们用 COM 打开图纸
    本身也会生成 .dwl,在打开之后才查会把自己的锁当成别人的,直接自我否决。
    """
    if not os.access(path, os.W_OK):
        raise RuntimeError(
            f"图纸是只读的,改不了:{path}\n"
            "请右键 → 属性 → 取消勾选「只读」,或换一份可写的副本。"
        )
    if pre_locks:
        raise RuntimeError(
            f"图纸正被 AutoCAD 占用(存在锁文件 {', '.join(pre_locks)}):{path}\n"
            "请在 AutoCAD 里关掉这张图再让我改。如果 AutoCAD 已经关了但锁文件还在,"
            "那是上次异常退出留下的,手动删掉这个锁文件即可。"
        )


def _save_drawing(doc: Any, path: Path, header: str) -> str:
    """按原文件的 DWG 格式存回去,防止把设计院的旧格式图纸升级成新版对方打不开。

    header 是修改前读到的 DWG 文件头(如 AC1027)。能映射到 AcSaveAsType 就用
    SaveAs 指定格式;映射不到或 SaveAs 被拒就退回 Save()。返回实际用的方式,
    调用方要把它报给用户——"存成了哪个格式"对交付图纸是要紧事。

    两次都失败时抛人话错误 + 原始 COM 错误码。真机上见过
    -2145320861(0x80210063,AutoCAD 的 OLE 错误区段),含义是"写不进去",
    通常是文件只读、被别的 AutoCAD 占用,或者目标格式这个版本不支持。
    """
    save_type = _DWG_HEADER_TO_SAVETYPE.get(header)
    first_error: BaseException | None = None
    if save_type is not None:
        try:
            _retry_on_busy(
                lambda: doc.SaveAs(str(path), save_type), what="按原格式保存",
            )
            label = _DWG_HEADER_LABELS.get(header, header)
            return f"SaveAs({label})"
        except Exception as exc:  # noqa: BLE001
            first_error = exc  # 退回 Save() 再试
    try:
        _retry_on_busy(doc.Save, what="保存图纸")
    except Exception as exc:  # noqa: BLE001
        detail = f"Save 失败:{exc}"
        if first_error is not None:
            detail += f";先前 SaveAs 也失败:{first_error}"
        raise RuntimeError(
            f"改动做完了但存不回去,原文件未被改动(备份仍在)。{detail}\n"
            "常见原因:①图纸设了只读属性 ②同目录有 .dwl 锁文件、别处正开着这张图 "
            "③磁盘满或没有写权限 ④目标 DWG 格式你这个 AutoCAD 版本不支持。"
        ) from exc
    return "Save(AutoCAD 默认格式)"


def _modelspace_entities(doc: Any) -> list[Any]:
    try:
        count = doc.ModelSpace.Count
    except Exception:
        return []
    return [doc.ModelSpace.Item(index) for index in range(count)]


def _bounding_box(entity: Any) -> dict[str, list[float]] | None:
    """实体的包围盒。这是唯一一个所有实体类型都支持的几何入口。

    Why: 多段线、样条线、填充、块参照都没有 InsertionPoint/StartPoint,
    只靠那几个属性会全部落到 [0,0] 兜底值,拿回来的坐标是假的,没法据此标注。
    GetBoundingBox 有两个 [out] 参数；无类型信息的动态包装必须显式传出参。
    """
    try:
        if getattr(entity, "_oleobj_", None) is not None:
            import pythoncom
            from win32com.client import VARIANT
            outputs = [VARIANT(pythoncom.VT_BYREF | pythoncom.VT_VARIANT, None) for _ in range(2)]
            _retry_on_busy(lambda: entity.GetBoundingBox(*outputs), what="读取实体包围盒")
            min_pt, max_pt = (item.value for item in outputs)
        else:
            min_pt, max_pt = entity.GetBoundingBox()
    except Exception:
        return None
    low, high = _numbers(min_pt), _numbers(max_pt)
    if len(low) < 2 or len(high) < 2:
        return None
    return {"min": low[:2], "max": high[:2]}


def _coordinate_pairs(value: Any, stride: int) -> list[list[float]]:
    """把 COM 扁平坐标数组按 stride 切成点列表。

    轻多段线(AcDbPolyline)的 Coordinates 是 [x1,y1,x2,y2,…](stride 2),
    老式多段线(AcDb2dPolyline/3dPolyline)是 [x1,y1,z1,…](stride 3)。
    """
    flat = _numbers(value)
    if len(flat) < stride:
        return []
    return [flat[i:i + 2] for i in range(0, len(flat) - stride + 1, stride)]


def _entity_geometry(entity: Any, name: str) -> dict[str, Any]:
    """按实体类型取真实几何。取不到的字段就不放,不编造。

    坐标一律只取 X/Y 两维——建筑施工图基本是平面图,带上 Z 只是噪声。
    """
    geo: dict[str, Any] = {}

    def _put(key: str, attr: str) -> None:
        value = getattr(entity, attr, None)
        if value is None:
            return
        pair = _numbers(value)[:2]
        if len(pair) == 2:
            geo[key] = pair

    if "Polyline" in name:
        # AcDbPolyline 的 Coordinates 是二维对,AcDb2dPolyline/3dPolyline 是三维
        stride = 2 if name.endswith("AcDbPolyline") or name == "AcDbPolyline" else 3
        points = _coordinate_pairs(getattr(entity, "Coordinates", None), stride)
        if not points and stride == 2:  # 猜错 stride 时换另一种再试
            points = _coordinate_pairs(getattr(entity, "Coordinates", None), 3)
        if points:
            geo["vertices"] = points[:_MAX_VERTICES]
            geo["vertex_count"] = len(points)
            if len(points) > _MAX_VERTICES:
                geo["vertices_truncated"] = True
        for key, attr in (("closed", "Closed"), ("length", "Length"), ("area", "Area")):
            try:
                raw = getattr(entity, attr)
            except Exception:
                continue
            geo[key] = bool(raw) if key == "closed" else round(float(raw), 3)
    elif "Line" in name:
        _put("start", "StartPoint")
        _put("end", "EndPoint")
        try:
            geo["length"] = round(float(entity.Length), 3)
        except Exception:
            pass
    elif "Circle" in name or "Arc" in name:
        _put("center", "Center")
        for key, attr in (("radius", "Radius"), ("start_angle", "StartAngle"),
                          ("end_angle", "EndAngle")):
            try:
                geo[key] = round(float(getattr(entity, attr)), 4)
            except Exception:
                continue
    elif "Text" in name:
        _put("position", "InsertionPoint")
        if "position" not in geo:
            _put("position", "TextPosition")
        for key, attr in (("height", "Height"), ("rotation", "Rotation")):
            try:
                geo[key] = round(float(getattr(entity, attr)), 3)
            except Exception:
                continue
    elif "BlockReference" in name:
        _put("insertion", "InsertionPoint")
        try:
            geo["block_name"] = str(entity.Name)
        except Exception:
            pass
        for key, attr in (("rotation", "Rotation"), ("scale_x", "XScaleFactor")):
            try:
                geo[key] = round(float(getattr(entity, attr)), 4)
            except Exception:
                continue
    elif "Dimension" in name:
        _put("text_position", "TextPosition")
        try:
            geo["measurement"] = round(float(entity.Measurement), 3)
        except Exception:
            pass

    box = _bounding_box(entity)
    if box is not None:
        geo["bbox"] = box
        width = round(box["max"][0] - box["min"][0], 3)
        height = round(box["max"][1] - box["min"][1], 3)
        geo["size"] = [width, height]
    return geo


def _entity_position(entity: Any) -> list[float]:
    """实体的代表点。优先用锚点属性,没有锚点的(多段线等)退回包围盒中心。

    包围盒兜底很要紧:之前一律返回 [0,0],多段线/填充/块的位置全是假的。
    """
    for attr in ("InsertionPoint", "StartPoint", "Center", "TextPosition"):
        value = getattr(entity, attr, None)
        if value is None:
            continue
        pair = _numbers(value)[:2]
        if len(pair) == 2:
            return pair
    box = _bounding_box(entity)
    if box is not None:
        return [
            round((box["min"][0] + box["max"][0]) / 2, 6),
            round((box["min"][1] + box["max"][1]) / 2, 6),
        ]
    return [0.0, 0.0]


def _entity_text(entity: Any) -> str:
    """TEXT/MTEXT 取 TextString;标注取 TextOverride(若空)或 Measurement。"""
    name = str(getattr(entity, "ObjectName", "") or "")
    if "Dimension" in name:
        return str(getattr(entity, "TextOverride", "") or "")
    return str(getattr(entity, "TextString", "") or "")


def _in_area(position: list[float], area: Any, box: Any = None) -> bool:
    """实体是否落在查询范围内。

    有包围盒时按"是否与范围相交"判定,而不是只看代表点——一道横穿范围的长墙线,
    它的中心点很可能在范围之外,按点筛会把它漏掉,而这恰恰是要标注的那根线。
    """
    if not area or not isinstance(area, (list, tuple)) or len(area) != 4:
        return True
    try:
        x1, y1, x2, y2 = [float(v) for v in area]
    except (TypeError, ValueError):
        return True
    left, right = min(x1, x2), max(x1, x2)
    bottom, top = min(y1, y2), max(y1, y2)
    if isinstance(box, dict):
        low, high = box.get("min"), box.get("max")
        if isinstance(low, list) and isinstance(high, list) and len(low) >= 2 and len(high) >= 2:
            return not (high[0] < left or low[0] > right or high[1] < bottom or low[1] > top)
    x, y = position
    return left <= x <= right and bottom <= y <= top


def _point(value: Any) -> list[float]:
    if value is None:
        return [0.0, 0.0, 0.0]
    if isinstance(value, (list, tuple)):
        parts = [float(v) for v in value[:3]]
        parts += [0.0] * (3 - len(parts))
        return parts
    return [float(value), 0.0, 0.0]


def _com_point(value: Any) -> Any:
    """把三元组包成 AutoCAD ActiveX 要的 COM VARIANT(VT_ARRAY|VT_R8)。

    pywin32 把普通 list 编排成通用变体数组,AutoCAD 拒收,报 -2145320939 参数无效。
    点一律三维(给两维也报参数无效)。纯计算逻辑仍用 _point(),只有传给 COM 的用这个。
    顶层不能 import win32com(Linux 下会挂),所以延迟 import。
    """
    from win32com.client import VARIANT
    import pythoncom

    x, y, z = _point(value)
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [x, y, z])


def _points(value: Any, *, minimum: int = 2) -> list[list[float]]:
    """Validate and normalize a list of 2D/3D points before sending it to COM."""
    if not isinstance(value, (list, tuple)) or len(value) < minimum:
        raise ValueError(f"至少需要 {minimum} 个点")
    points = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise ValueError("每个点都必须是 [x, y] 或 [x, y, z]")
        points.append(_point(point))
    return points


def _com_xy_points(points: list[list[float]]) -> Any:
    """把点列包成 AddLightWeightPolyline 所需的二维 COM 数组。"""
    from win32com.client import VARIANT
    import pythoncom

    flat = [coordinate for point in points for coordinate in point[:2]]
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, flat)


def _layer_info(layer: Any) -> dict[str, Any]:
    """Read the common layer fields without making one missing field fatal."""
    info: dict[str, Any] = {"name": str(layer.Name)}
    for key, attr in (
        ("color", "Color"), ("linetype", "Linetype"),
        ("lineweight", "Lineweight"), ("on", "LayerOn"),
        ("frozen", "Freeze"), ("locked", "Lock"),
    ):
        try:
            value = getattr(layer, attr)
        except Exception:
            continue
        if key in ("on", "frozen", "locked"):
            info[key] = bool(value)
        elif key == "linetype":
            info[key] = str(value)
        else:
            info[key] = _int_or_none(value)
    return info


def _set_layer_properties(layer: Any, values: dict[str, Any]) -> None:
    """Set only explicitly supplied layer properties; fail before saving on COM errors."""
    attrs = {
        "color": "Color", "linetype": "Linetype", "lineweight": "Lineweight",
        "on": "LayerOn", "frozen": "Freeze", "locked": "Lock",
    }
    for key, attr in attrs.items():
        if key not in values or values[key] is None:
            continue
        try:
            setattr(layer, attr, values[key] if key != "linetype" else str(values[key]))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"设置图层属性 {key} 失败:{exc}") from exc


def _find_layer(doc: Any, name: str) -> Any | None:
    for index in range(doc.Layers.Count):
        layer = doc.Layers.Item(index)
        if str(layer.Name) == name:
            return layer
    return None



def _edit_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} 必须是有限数值")
    return float(value)


def _edit_point(value: Any, label: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) not in (2, 3):
        raise ValueError(f"{label} 必须是 [x,y] 或 [x,y,z]")
    return [_edit_number(v, label) for v in value] + [0.0] * (3 - len(value))


def _edit_filters(layer: str, types: str, area: Any, text: str) -> dict[str, Any]:
    for label, value in (("layer", layer), ("types", types), ("text", text)):
        if not isinstance(value, str):
            raise ValueError(f"{label} 必须是字符串")
    layer, types = layer.strip(), types.strip()
    if types and any(not token.strip() or token.strip() == "*" for token in types.split(",")):
        raise ValueError("types 必须是逗号分隔的具体实体类型，不能使用通配符或空类型")
    if area is not None:
        if not isinstance(area, (list, tuple)) or len(area) != 4:
            raise ValueError("area 必须是 [x1,y1,x2,y2]")
        x1, y1, x2, y2 = [_edit_number(v, "area") for v in area]
        area = [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
    if not (layer or types or area is not None or text.strip()):
        raise ValueError("至少提供 layer/types/area/text 中一个有效筛选条件，禁止无条件修改整张图")
    if text and not text.strip():
        raise ValueError("text 不能只有空白字符")
    return {"layer": layer, "types": types, "area": area, "text": text}


def _edit_type_matches(name: str, types: str) -> bool:
    # Exact aliases keep LINE from accidentally matching POLYLINE/MLINE/SPLINE.
    name = name.casefold().removeprefix("acdb")
    for token in types.split(","):
        token = token.strip().casefold().removeprefix("acdb")
        if token == name or (token == "dimension" and name.endswith("dimension")):
            return True
        if token == "polyline" and name in ("polyline", "2dpolyline", "3dpolyline"):
            return True
    return False


def _selected_entities(doc: Any, filters: dict[str, Any]) -> list[tuple[Any, dict[str, Any]]]:
    selected = []
    # Snapshot the collection before Copy/Mirror/Delete can change its indices.
    count = _retry_on_busy(lambda: doc.ModelSpace.Count, what="读取实体数量")
    entities = [_retry_on_busy(lambda i=i: doc.ModelSpace.Item(i), what="读取实体")
                for i in range(count)]
    for entity in entities:
        layer = str(_retry_on_busy(lambda: entity.Layer, what="读取实体图层"))
        name = str(_retry_on_busy(lambda: entity.ObjectName, what="读取实体类型"))
        if filters["layer"] and layer != filters["layer"]:
            continue
        if filters["types"] and not _edit_type_matches(name, filters["types"]):
            continue
        text = _entity_text(entity) if filters["text"] else ""
        if filters["text"] and filters["text"] not in text:
            continue
        box = _bounding_box(entity)
        position = None
        for attr in ("InsertionPoint", "StartPoint", "Center", "TextPosition", "Coordinates"):
            value = _numbers(getattr(entity, attr, None))
            if len(value) >= 2 and all(math.isfinite(v) for v in value[:2]):
                position = value[:2]
                break
        if box is not None:
            if not all(math.isfinite(v) for point in box.values() for v in point):
                raise RuntimeError("实体包围盒包含无效坐标，已拒绝修改")
            if position is None:
                position = [(lo + hi) / 2 for lo, hi in zip(box["min"], box["max"])]
        if filters["area"] is not None:
            # Never turn an unreadable location into a fabricated origin.
            if position is None or not _in_area(position, filters["area"], box):
                continue
        item = {"type": name, "layer": layer, "position": position}
        if box is not None:
            item["bbox"] = box
        if text:
            item["text"] = text
        try:
            item["handle"] = str(entity.Handle)
        except AttributeError:
            pass
        selected.append((entity, item))
    return selected


_ENTITY_PROPERTIES = {
    "layer": "Layer", "color": "Color", "linetype": "Linetype",
    "linetype_scale": "LinetypeScale", "lineweight": "Lineweight", "visible": "Visible",
}
_LINEWEIGHTS = {-3, -2, -1, 0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50,
                53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211}


def _edit_properties(properties: Any) -> dict[str, Any]:
    if not isinstance(properties, dict) or not properties or properties.keys() - _ENTITY_PROPERTIES.keys():
        raise ValueError("properties 必须为非空对象，仅支持 layer/color/linetype/linetype_scale/lineweight/visible")
    values = dict(properties)
    for key, value in values.items():
        if key in ("layer", "linetype"):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} 必须为非空字符串")
            values[key] = value.strip()
        elif key == "visible":
            if not isinstance(value, bool):
                raise ValueError("visible 必须为布尔值")
        elif key == "color":
            if type(value) is not int or not 0 <= value <= 256:
                raise ValueError("color 必须为 0~256 的 ACI 整数（0=ByBlock，256=ByLayer）")
        elif key == "lineweight":
            if type(value) is not int or value not in _LINEWEIGHTS:
                raise ValueError("lineweight 必须为 AutoCAD 标准线宽枚举，单位为 1/100 毫米；-1/-2/-3 为随层/随块/默认")
        else:
            values[key] = _edit_number(value, key)
            if values[key] <= 0:
                raise ValueError("linetype_scale 必须大于 0")
    return values

def register_tools(
    registry: ToolRegistry, on_progress: Callable[[str], None] | None = None,
) -> None:
    if not _com_available():
        print("[cad] 非 Windows 或未安装 pywin32/AutoCAD,CAD 工具跳过注册。")
        return
    if on_progress is not None:
        set_progress_callback(on_progress)

    import win32com.client  # type: ignore

    def _com_async(fn):
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            # COM operations do not take over the mouse or keyboard.
            def call():
                import pythoncom
                pythoncom.CoInitialize()
                try:
                    return fn(*args, **kwargs)
                finally:
                    pythoncom.CoUninitialize()
            return await asyncio.to_thread(call)
        return wrapped

    def _modify_drawing(
        path: str, operation: Callable[[Any], dict[str, Any]], *,
        preview: Callable[[Any], dict[str, Any]] | None = None, dry_run: bool = False,
    ) -> dict[str, Any]:
        """Run writes and optional read-only previews through one ownership lifecycle."""
        p = _resolve(path)
        if not p.is_file():
            raise RuntimeError(f"图纸不存在:{p}")
        header = _dwg_header(p)
        pre_locks = _lock_files(p)
        app, doc, own_doc, own_app = _open_cad_drawing(win32com.client, p)
        try:
            if not dry_run and not own_doc:
                raise RuntimeError(
                    f"这张图纸正开在你的 AutoCAD 里,不能替我改(会和你手上的编辑冲突)。"
                    f"请先在 AutoCAD 里保存并关闭它,再让我改:{p}"
                )
            details = preview(doc) if preview is not None else {}
            if preview is not None and (dry_run or not details["matched"]):
                return {
                    **details, "dry_run": dry_run, "changed": 0,
                    "backup": None, "saved_as": None, "ok": True,
                    "summary": ("仅预览，未修改、备份或保存。核对后显式传 dry_run=false 执行。"
                                if dry_run else "未找到匹配实体，未修改、备份或保存。"),
                }
            _check_writable(p, pre_locks)
            backup = _backup_drawing(p)
            try:
                result = operation(doc)
                saved_as = _save_drawing(doc, p, header)
            except Exception as exc:
                if preview is None:
                    raise
                raise RuntimeError(f"本批改图或保存失败，正在关闭图纸并放弃未保存改动；备份:{backup}；{exc}") from exc
            return {
                **details, **result,
                **({"dry_run": False} if preview is not None else {}),
                "backup": backup, "saved_as": saved_as,
                "summary": _modify_summary(saved_as, backup, own_app),
                "ok": result.get("ok", True),
            }
        finally:
            _release_cad_drawing(app, doc, own_doc, own_app)

    def _add_lightweight_polyline(modelspace: Any, points: list[list[float]], closed: bool) -> Any:
        try:
            entity = modelspace.AddLightWeightPolyline(_com_xy_points(points))
        except AttributeError as exc:
            raise RuntimeError("当前 AutoCAD COM 不支持轻量多段线(AddLightWeightPolyline)") from exc
        entity.Closed = bool(closed)
        return entity

    def _apply_layer(entity: Any, layer: Any) -> None:
        if layer:
            entity.Layer = str(layer)

    # ---------- 实体变换与整理：筛选快照 + 默认预览 + 整批保存 ----------
    def _edit_tool(name: str, description: str, extra: dict, required: list[str]):
        return registry.tool(
            name=name,
            description=(description + " 仅处理模型空间；layer/types/area/text 取交集，至少提供一个筛选。"
                         "area 按包围盒相交（含横穿边界的实体）；types 精确匹配类型或 DIMENSION/POLYLINE 家族；"
                         "text 为区分大小写的文字子串。默认 dry_run=true 只预览，显式 false 才备份并保存；"
                         "预览最多列出 200 个，matched 为全部匹配数量，执行处理全部匹配实体。"
                         "坐标使用图纸自身单位、WCS；不能修改用户已打开的图纸或锁定图层。"
                         "这是驱动 AutoCAD 的精确工具，不需要先 launch_app 打开 AutoCAD，也不需要截屏点界面。"),
            parameters={
                "type": "object", "properties": {
                    "path": {"type": "string", "description": "DWG 绝对路径"},
                    "layer": {"type": "string", "description": "源图层，精确匹配"},
                    "types": {"type": "string", "description": "逗号分隔，如 LINE,TEXT 或 AcDbLine；LINE 不含 POLYLINE"},
                    "area": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                    "text": {"type": "string", "description": "文字子串，区分大小写"},
                    "dry_run": {"type": "boolean", "default": True}, **extra,
                }, "required": ["path", *required],
            }, side_effect=True, danger=name in ("cad_erase_entities", "cad_mirror_entities", "cad_join_entities", "cad_explode"),
        )

    def _edit_entities(path, layer, types, area, text, dry_run, action, parameters, apply,
                       *, prepare_selected=None, apply_batch=None):
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run 必须为布尔值，只有显式 false 才执行")
        filters = _edit_filters(layer, types, area, text)
        selected = []
        properties = parameters.get("properties", {})

        def prepare(doc):
            target = None
            if properties.get("layer"):
                target = _find_layer(doc, properties["layer"])
                if target is None:
                    raise ValueError(f"目标图层不存在:{properties['layer']}；请先用 cad_create_layer 创建")
            linetype = properties.get("linetype")
            if linetype and linetype.casefold() not in ("bylayer", "byblock"):
                loaded = [str(doc.Linetypes.Item(i).Name).casefold() for i in range(doc.Linetypes.Count)]
                if linetype.casefold() not in loaded:
                    raise ValueError(f"线型尚未加载:{linetype}")
            selected.extend(_selected_entities(doc, filters))
            for entity, item in selected:
                if properties:
                    item["properties_before"] = {
                        key: _retry_on_busy(lambda attr=attr: getattr(entity, attr), what="读取实体属性")
                        for key, attr in _ENTITY_PROPERTIES.items() if key in properties
                    }
            if not dry_run and selected:
                names = {item["layer"] for _, item in selected}
                if target is not None:
                    names.add(str(target.Name))
                for name in names:
                    source = _find_layer(doc, name)
                    if source is None or _retry_on_busy(lambda: source.Lock, what="检查图层锁"):
                        raise RuntimeError(f"图层不存在或已锁定，已拒绝整批修改:{name}")
            planned = prepare_selected(doc, selected) if prepare_selected is not None and selected else {}
            return {
                **planned, "action": action, "filters": filters, "parameters": parameters,
                "matched": len(selected), "preview": [item for _, item in selected[:_MAX_DETAIL]],
                "truncated": len(selected) > _MAX_DETAIL,
            }

        def execute(doc):
            if apply_batch is not None:
                return apply_batch(doc, selected)
            for index, (entity, _) in enumerate(selected):
                try:
                    apply(entity)
                except Exception as exc:
                    raise RuntimeError(f"第 {index + 1} 个匹配实体执行 {action} 失败:{exc}") from exc
            return {"changed": len(selected)}

        return _modify_drawing(path, execute, preview=prepare, dry_run=dry_run)

    point_schema = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 3}

    @_edit_tool("cad_move_entities", "按筛选批量平移实体。offset 为位移向量。", {"offset": point_schema}, ["offset"])
    @_com_async
    def cad_move_entities(path: str, offset: list, layer: str = "", types: str = "",
                          area: Any = None, text: str = "", dry_run: bool = True):
        offset = _edit_point(offset, "offset")
        if not any(offset):
            raise ValueError("offset 不能为零向量")
        def apply(entity):
            _retry_on_busy(lambda: entity.Move(_com_point([0, 0, 0]), _com_point(offset)), what="平移实体")
        return _edit_entities(path, layer, types, area, text, dry_run, "move", {"offset": offset}, apply)

    @_edit_tool("cad_copy_entities", "按筛选复制实体并平移，保留原实体。offset 可为零（原位复制）。", {"offset": point_schema}, ["offset"])
    @_com_async
    def cad_copy_entities(path: str, offset: list, layer: str = "", types: str = "",
                          area: Any = None, text: str = "", dry_run: bool = True):
        offset = _edit_point(offset, "offset")
        def apply(entity):
            copied = _retry_on_busy(entity.Copy, what="复制实体")
            _retry_on_busy(lambda: copied.Move(_com_point([0, 0, 0]), _com_point(offset)), what="平移副本")
        return _edit_entities(path, layer, types, area, text, dry_run, "copy", {"offset": offset}, apply)

    @_edit_tool("cad_rotate_entities", "按筛选绕 WCS Z 轴旋转实体；base_point 为基点，angle 为弧度（逆时针为正）。",
                {"base_point": point_schema, "angle": {"type": "number"}}, ["base_point", "angle"])
    @_com_async
    def cad_rotate_entities(path: str, base_point: list, angle: float, layer: str = "", types: str = "",
                            area: Any = None, text: str = "", dry_run: bool = True):
        base_point, angle = _edit_point(base_point, "base_point"), _edit_number(angle, "angle")
        def apply(entity):
            _retry_on_busy(lambda: entity.Rotate(_com_point(base_point), angle), what="旋转实体")
        return _edit_entities(path, layer, types, area, text, dry_run, "rotate", {"base_point": base_point, "angle": angle}, apply)

    @_edit_tool("cad_scale_entities", "按筛选以 base_point 为基点等比缩放；factor 必须大于零。",
                {"base_point": point_schema, "factor": {"type": "number", "exclusiveMinimum": 0}}, ["base_point", "factor"])
    @_com_async
    def cad_scale_entities(path: str, base_point: list, factor: float, layer: str = "", types: str = "",
                           area: Any = None, text: str = "", dry_run: bool = True):
        base_point, factor = _edit_point(base_point, "base_point"), _edit_number(factor, "factor")
        if factor <= 0:
            raise ValueError("factor 必须大于零")
        def apply(entity):
            _retry_on_busy(lambda: entity.ScaleEntity(_com_point(base_point), factor), what="缩放实体")
        return _edit_entities(path, layer, types, area, text, dry_run, "scale", {"base_point": base_point, "factor": factor}, apply)

    @_edit_tool("cad_mirror_entities", "按筛选沿 p1/p2 确定的 WCS XY 镜像轴创建镜像副本；两点 XY 不得重合且 Z 必须相同。erase_source 默认 false 保留原件。",
                {"p1": point_schema, "p2": point_schema, "erase_source": {"type": "boolean", "default": False}}, ["p1", "p2"])
    @_com_async
    def cad_mirror_entities(path: str, p1: list, p2: list, erase_source: bool = False,
                            layer: str = "", types: str = "", area: Any = None, text: str = "", dry_run: bool = True):
        p1, p2 = _edit_point(p1, "p1"), _edit_point(p2, "p2")
        if p1[:2] == p2[:2] or p1[2] != p2[2]:
            raise ValueError("镜像轴两点的 XY 不得重合且 Z 必须相同")
        if not isinstance(erase_source, bool):
            raise ValueError("erase_source 必须为布尔值")
        def apply(entity):
            _retry_on_busy(lambda: entity.Mirror(_com_point(p1), _com_point(p2)), what="镜像实体")
            if erase_source:
                _retry_on_busy(entity.Delete, what="删除镜像原件")
        return _edit_entities(path, layer, types, area, text, dry_run, "mirror", {"p1": p1, "p2": p2, "erase_source": erase_source}, apply)

    @_edit_tool("cad_erase_entities", "删除筛选匹配的实体。务必先核对预览的范围和 matched 数量。", {}, [])
    @_com_async
    def cad_erase_entities(path: str, layer: str = "", types: str = "", area: Any = None, text: str = "", dry_run: bool = True):
        return _edit_entities(path, layer, types, area, text, dry_run, "erase", {},
                              lambda entity: _retry_on_busy(entity.Delete, what="删除实体"))

    @_edit_tool("cad_move_entities_to_layer", "把筛选匹配的实体移到已存在的 target_layer，不自动创建图层。",
                {"target_layer": {"type": "string"}}, ["target_layer"])
    @_com_async
    def cad_move_entities_to_layer(path: str, target_layer: str, layer: str = "", types: str = "",
                                   area: Any = None, text: str = "", dry_run: bool = True):
        properties = _edit_properties({"layer": target_layer})
        return _edit_entities(path, layer, types, area, text, dry_run, "move_to_layer", {"properties": properties},
                              lambda entity: _retry_on_busy(lambda: setattr(entity, "Layer", properties["layer"]), what="修改实体图层"))

    @_edit_tool("cad_change_entity_properties", "批量修改通用属性：图层、ACI 颜色、已加载线型、正数线型比例、标准枚举线宽、可见性；拒绝任意 COM 属性。",
                {"properties": {"type": "object", "minProperties": 1, "additionalProperties": False, "properties": {
                    "layer": {"type": "string"}, "color": {"type": "integer", "minimum": 0, "maximum": 256},
                    "linetype": {"type": "string"}, "linetype_scale": {"type": "number", "exclusiveMinimum": 0},
                    "lineweight": {"type": "integer", "enum": sorted(_LINEWEIGHTS)}, "visible": {"type": "boolean"},
                }}}, ["properties"])
    @_com_async
    def cad_change_entity_properties(path: str, properties: dict, layer: str = "", types: str = "",
                                      area: Any = None, text: str = "", dry_run: bool = True):
        properties = _edit_properties(properties)
        def apply(entity):
            for key, value in properties.items():
                _retry_on_busy(lambda key=key, value=value: setattr(entity, _ENTITY_PROPERTIES[key], value), what=f"修改实体属性 {key}")
        return _edit_entities(path, layer, types, area, text, dry_run, "change_properties", {"properties": properties}, apply)

    # ---------- 工程编辑:第二批 ----------
    # ponytail: ActiveX has no general Trim/Fillet/JoinEntity methods. Limit these
    # edits to planar LINE geometry and use real endpoint/AddArc/polyline APIs.
    def _engineering_call(fn, what):
        try:
            return _retry_on_busy(fn, what=what)
        except Exception as exc:
            raise RuntimeError(f"AutoCAD COM {what}失败（版本/实体可能不支持），未保存:{exc}") from exc

    def _engineering_method(entity, name):
        method = _engineering_call(lambda: getattr(entity, name), f"读取 {name} 接口")
        if not callable(method):
            raise RuntimeError(f"当前 AutoCAD COM 不支持 {name}，已拒绝修改")
        return method

    def _line_points(entity):
        name = _engineering_call(lambda: entity.ObjectName, "读取实体类型")
        if name != "AcDbLine":
            raise ValueError(f"本工具当前仅支持 WCS XY 平面的 LINE，不能处理 {name}")
        a = _edit_point(_engineering_call(lambda: entity.StartPoint, "读取起点"), "StartPoint")
        b = _edit_point(_engineering_call(lambda: entity.EndPoint, "读取终点"), "EndPoint")
        length = math.dist(a[:2], b[:2])
        thickness = _edit_number(_engineering_call(lambda: entity.Thickness, "读取直线厚度"), "Thickness")
        if a[2] != b[2] or not math.isfinite(length) or length <= 1e-9 or thickness != 0:
            raise ValueError("仅支持非零有限长度、同一标高、Thickness=0 的二维直线")
        return [a, b]

    def _cross(a, b):
        return a[0] * b[1] - a[1] * b[0]

    def _intersection(a, b, c, d):
        if len({a[2], b[2], c[2], d[2]}) != 1:
            raise ValueError("直线必须位于同一 WCS XY 标高平面")
        v, w = [b[i] - a[i] for i in range(2)], [d[i] - c[i] for i in range(2)]
        determinant = _cross(v, w)
        if abs(determinant) <= 1e-12 * math.hypot(*v) * math.hypot(*w):
            raise ValueError("直线平行、共线或夹角过小，无法确定唯一交点")
        delta = [c[i] - a[i] for i in range(2)]
        t, u = _cross(delta, w) / determinant, _cross(delta, v) / determinant
        point = [a[0] + t * v[0], a[1] + t * v[1], a[2]]
        if not all(math.isfinite(n) for n in [*point, t, u]):
            raise ValueError("坐标计算超出有限数值范围，已拒绝修改")
        return point, t, u

    def _set_endpoint(entity, end, point):
        attr = "StartPoint" if end == "start" else "EndPoint"
        _engineering_call(lambda: setattr(entity, attr, _com_point(point)), f"写入 {attr}")
        actual = _edit_point(_engineering_call(lambda: getattr(entity, attr), f"验证 {attr}"), attr)
        if math.dist(actual, point) > 1e-8:
            raise RuntimeError(f"AutoCAD COM {attr} 写入后不一致，已拒绝保存")

    def _end_name(end):
        if not isinstance(end, str) or end not in ("start", "end"):
            raise ValueError("end 必须是 start 或 end")
        return end

    def _copy_engineering_style(source, target):
        for attr in (*_ENTITY_PROPERTIES.values(), "Thickness"):
            value = _engineering_call(lambda attr=attr: getattr(source, attr), f"读取 {attr}")
            _engineering_call(lambda attr=attr, value=value: setattr(target, attr, value), f"设置 {attr}")
        # TrueColor must follow Color so RGB colors are not reduced to ACI.
        try:
            color = source.TrueColor
        except AttributeError:
            return
        _engineering_call(lambda: setattr(target, "TrueColor", color), "设置 TrueColor")

    def _generated_entities(value, seen):
        if not isinstance(value, (list, tuple)) or not value:
            raise RuntimeError("AutoCAD COM 未返回非空实体数组，无法验证操作成功，已拒绝保存")
        records = []
        for entity in value:
            name = _engineering_call(lambda: entity.ObjectName, "验证生成实体类型")
            handle = _engineering_call(lambda: entity.Handle, "验证生成实体 Handle")
            if not isinstance(name, str) or not name.startswith("AcDb") or not isinstance(handle, str) or not handle.strip():
                raise RuntimeError("生成实体缺少有效 Handle/类型，已拒绝保存")
            if handle.casefold() in seen:
                raise RuntimeError("生成结果包含原件或重复 Handle，无法验证创建成功，已拒绝保存")
            seen.add(handle.casefold())
            records.append({"type": name, "handle": handle})
        return records

    @_edit_tool("cad_offset", "调用真实 COM Offset 偏移曲线，保留原件。distance 非零；方向由 AutoCAD 曲线定义决定。",
                {"distance": {"type": "number"}}, ["distance"])
    @_com_async
    def cad_offset(path: str, distance: float, layer: str = "", types: str = "",
                   area: Any = None, text: str = "", dry_run: bool = True):
        distance = _edit_number(distance, "distance")
        if distance == 0:
            raise ValueError("distance 不能为零")
        methods = []
        def prepare(doc, selected):
            methods.extend(_engineering_method(entity, "Offset") for entity, _ in selected)
            return {}
        def execute(doc, selected):
            created = []
            seen = {str(item.get("handle", "")).casefold() for _, item in selected}
            for method in methods:
                created.extend(_generated_entities(_engineering_call(lambda: method(distance), "Offset 偏移"), seen))
            return {"changed": len(selected), "created": len(created), "entities": created[:_MAX_DETAIL],
                    "entities_truncated": len(created) > _MAX_DETAIL}
        return _edit_entities(path, layer, types, area, text, dry_run, "offset", {"distance": distance},
                              None, prepare_selected=prepare, apply_batch=execute)

    boundary_schema = {
        "boundary_start": point_schema, "boundary_end": point_schema,
        "end": {"type": "string", "enum": ["start", "end"], "default": "end"},
    }

    def _boundary_edit(path, boundary_start, boundary_end, end, layer, types, area, text, dry_run, action):
        c, d = _edit_point(boundary_start, "boundary_start"), _edit_point(boundary_end, "boundary_end")
        end = _end_name(end)
        if c[2] != d[2] or math.dist(c[:2], d[:2]) <= 1e-9:
            raise ValueError("边界必须是非零长度的二维线段")
        plans = []
        def prepare(doc, selected):
            for entity, item in selected:
                a, b = _line_points(entity)
                point, t, u = _intersection(a, b, c, d)
                if not 0 <= u <= 1:
                    raise ValueError("交点不在线段边界内，已拒绝整批修改")
                if action == "trim" and not 0 < t < 1:
                    raise ValueError("修剪交点必须在待修剪直线内部")
                if action == "extend" and not (t < 0 if end == "start" else t > 1):
                    raise ValueError("交点不在指定端点的延伸方向，已拒绝修改")
                item["endpoints_before"] = [a, b]
                item["endpoint_after"] = point
                plans.append((entity, point))
            return {}
        def execute(doc, selected):
            for entity, point in plans:
                _set_endpoint(entity, end, point)
            return {"changed": len(plans)}
        return _edit_entities(path, layer, types, area, text, dry_run, action,
                              {"boundary_start": c, "boundary_end": d, "end": end}, None,
                              prepare_selected=prepare, apply_batch=execute)

    @_edit_tool("cad_trim", "修剪二维 LINE 的指定端点至边界线段交点；保留另一端。boundary_start/end 为 WCS 边界线段坐标，非图内实体引用；不支持曲线或中段切断。",
                boundary_schema, ["boundary_start", "boundary_end"])
    @_com_async
    def cad_trim(path: str, boundary_start: list, boundary_end: list, end: str = "end",
                 layer: str = "", types: str = "", area: Any = None, text: str = "", dry_run: bool = True):
        return _boundary_edit(path, boundary_start, boundary_end, end, layer, types, area, text, dry_run, "trim")

    @_edit_tool("cad_extend", "延伸二维 LINE 的指定端点至边界线段交点。boundary_start/end 为 WCS 边界线段坐标，非图内实体引用；不支持曲线。",
                boundary_schema, ["boundary_start", "boundary_end"])
    @_com_async
    def cad_extend(path: str, boundary_start: list, boundary_end: list, end: str = "end",
                   layer: str = "", types: str = "", area: Any = None, text: str = "", dry_run: bool = True):
        return _boundary_edit(path, boundary_start, boundary_end, end, layer, types, area, text, dry_run, "extend")

    ends_schema = {"type": "array", "items": {"type": "string", "enum": ["start", "end"]},
                   "minItems": 2, "maxItems": 2, "description": "按预览模型空间顺序指定两条线要修改的端点，如 [end,start]"}

    def _corner_edit(path, ends, layer, types, area, text, dry_run, action, values):
        if not isinstance(ends, (list, tuple)) or len(ends) != 2:
            raise ValueError("ends 必须是两个 start/end 组成的数组")
        ends = [_end_name(end) for end in ends]
        plan = {}
        def prepare(doc, selected):
            if len(selected) != 2:
                raise ValueError("圆角/倒角必须精确匹配两条直线；请缩小筛选范围")
            lines = [_line_points(entity) for entity, _ in selected]
            corner, _, _ = _intersection(*lines[0], *lines[1])
            directions, lengths = [], []
            for line, end in zip(lines, ends):
                keep = line[1 if end == "start" else 0]
                length = math.dist(corner[:2], keep[:2])
                if length <= 1e-9:
                    raise ValueError("保留端点不能位于交点，请核对 ends")
                direction = [(keep[i] - corner[i]) / length for i in range(2)]
                change = line[0 if end == "start" else 1]
                if sum((change[i] - corner[i]) * direction[i] for i in range(2)) >= length:
                    raise ValueError("ends 选择方向有误，不能越过保留端点")
                directions.append(direction)
                lengths.append(length)
            cosine = max(-1.0, min(1.0, sum(a * b for a, b in zip(*directions))))
            angle = math.acos(cosine)
            if angle <= 1e-9 or math.pi - angle <= 1e-9:
                raise ValueError("夹角过小或共线，无法创建圆角/倒角")
            if action == "fillet":
                radius = values["radius"]
                distances = [radius / math.tan(angle / 2)] * 2
                bisector = [sum(pair) for pair in zip(*directions)]
                norm = math.hypot(*bisector)
                center = [corner[i] + bisector[i] / norm * radius / math.sin(angle / 2) for i in range(2)] + [corner[2]]
                plan["center"] = center
            else:
                distances = [values["distance1"], values["distance2"]]
            if any(d >= length - 1e-9 for d, length in zip(distances, lengths)):
                raise ValueError("圆角半径/倒角距离过大，会耗尽直线，已拒绝修改")
            points = [[corner[i] + direction[i] * distance for i in range(2)] + [corner[2]]
                      for direction, distance in zip(directions, distances)]
            if not all(math.isfinite(v) for p in points + [plan.get("center", corner)] for v in p):
                raise ValueError("圆角/倒角坐标超出有限数值范围")
            plan["points"] = points
            for (_, item), line, point in zip(selected, lines, points):
                item["endpoints_before"] = line
                item["endpoint_after"] = point
            return {"corner": corner, **plan}
        def execute(doc, selected):
            points = plan["points"]
            if action == "fillet":
                center = plan["center"]
                angles = [math.atan2(p[1] - center[1], p[0] - center[0]) % math.tau for p in points]
                if (angles[1] - angles[0]) % math.tau > math.pi:
                    angles.reverse()
                entity = _engineering_call(lambda: doc.ModelSpace.AddArc(_com_point(center), values["radius"], *angles), "AddArc 圆角")
            else:
                entity = _engineering_call(lambda: doc.ModelSpace.AddLine(*[_com_point(p) for p in points]), "AddLine 倒角")
            _copy_engineering_style(selected[0][0], entity)
            for (source, _), end, point in zip(selected, ends, points):
                _set_endpoint(source, end, point)
            return {"changed": 2, "created": 1}
        return _edit_entities(path, layer, types, area, text, dry_run, action, {"ends": ends, **values}, None,
                              prepare_selected=prepare, apply_batch=execute)

    @_edit_tool("cad_fillet", "对恰好两条同标高二维 LINE 创建内侧相切圆弧并修剪/延伸指定端点；radius>0；新弧沿用首条线属性。不支持其他曲线。",
                {"radius": {"type": "number", "exclusiveMinimum": 0}, "ends": ends_schema}, ["radius", "ends"])
    @_com_async
    def cad_fillet(path: str, radius: float, ends: list, layer: str = "", types: str = "",
                   area: Any = None, text: str = "", dry_run: bool = True):
        radius = _edit_number(radius, "radius")
        if radius <= 0:
            raise ValueError("radius 必须大于零")
        return _corner_edit(path, ends, layer, types, area, text, dry_run, "fillet", {"radius": radius})

    @_edit_tool("cad_chamfer", "对恰好两条同标高二维 LINE 创建倒角连接线并修剪/延伸指定端点；两距离>0，沿用首条线属性。不支持其他曲线。",
                {"distance1": {"type": "number", "exclusiveMinimum": 0},
                 "distance2": {"type": "number", "exclusiveMinimum": 0}, "ends": ends_schema}, ["distance1", "distance2", "ends"])
    @_com_async
    def cad_chamfer(path: str, distance1: float, distance2: float, ends: list, layer: str = "", types: str = "",
                    area: Any = None, text: str = "", dry_run: bool = True):
        distances = {key: _edit_number(value, key) for key, value in (("distance1", distance1), ("distance2", distance2))}
        if any(d <= 0 for d in distances.values()):
            raise ValueError("distance1/distance2 必须大于零")
        return _corner_edit(path, ends, layer, types, area, text, dry_run, "chamfer", distances)

    @_edit_tool("cad_join_entities", "把同标高、同图层且通用属性一致的二维 LINE 单链/单环合成轻量多段线，成功后删除源线；自动按端点排序。拒绝断链、分叉、重叠和其他曲线。tolerance 为端点连接容差，会吸附容差内端点。",
                {"tolerance": {"type": "number", "exclusiveMinimum": 0, "default": 1e-6}}, [])
    @_com_async
    def cad_join_entities(path: str, tolerance: float = 1e-6, layer: str = "", types: str = "",
                          area: Any = None, text: str = "", dry_run: bool = True):
        tolerance = _edit_number(tolerance, "tolerance")
        if tolerance <= 0:
            raise ValueError("tolerance 必须大于零")
        plan = {}
        def prepare(doc, selected):
            if len(selected) < 2:
                raise ValueError("合并至少需要两条直线")
            lines = [_line_points(entity) for entity, _ in selected]
            if len({p[2] for line in lines for p in line}) != 1:
                raise ValueError("合并直线必须位于同一标高平面")
            styles = [[_engineering_call(lambda e=e, attr=attr: getattr(e, attr), f"读取 {attr}")
                       for attr in _ENTITY_PROPERTIES.values()] for e, _ in selected]
            if any(style != styles[0] for style in styles):
                raise ValueError("合并实体图层/通用属性必须一致，避免丢失属性")
            colors = []
            for entity, _ in selected:
                try:
                    color = entity.TrueColor
                except AttributeError:
                    colors.append(None)
                else:
                    colors.append(tuple(_engineering_call(lambda attr=attr: getattr(color, attr), f"读取颜色 {attr}")
                                        for attr in ("ColorMethod", "Red", "Green", "Blue")))
            if any(color != colors[0] for color in colors):
                raise ValueError("合并实体 TrueColor 必须一致，避免丢失颜色")
            for i, (a, b) in enumerate(lines):
                v = [b[k] - a[k] for k in range(2)]
                length = math.hypot(*v)
                if length <= tolerance:
                    raise ValueError("直线长度不能小于连接容差")
                for c, d in lines[:i]:
                    if all(abs(_cross(v, [p[k] - a[k] for k in range(2)])) / length <= 1e-9 for p in (c, d)):
                        projected = [sum((p[k] - a[k]) * v[k] for k in range(2)) / length for p in (c, d)]
                        if min(length, max(projected)) - max(0, min(projected)) > 1e-9:
                            raise ValueError("存在重叠直线，已拒绝合并")
            nodes, links, edges = [], [], []
            for line in lines:
                edge = []
                for point in line:
                    matches = [i for i, node in enumerate(nodes) if math.dist(point, node) <= tolerance]
                    if len(matches) > 1:
                        raise ValueError("容差产生歧义，无法唯一连接端点")
                    if matches:
                        node = matches[0]
                    else:
                        node = len(nodes)
                        nodes.append(point)
                        links.append([])
                    edge.append(node)
                if edge[0] == edge[1] or any(set(edge) == set(old) for old in edges):
                    raise ValueError("存在零长度/重叠边，已拒绝合并")
                for node in edge:
                    links[node].append(len(edges))
                edges.append(edge)
            if any(len(link) > 2 for link in links):
                raise ValueError("实体形成分叉，不能合并成单条多段线")
            ends = [i for i, link in enumerate(links) if len(link) == 1]
            if len(ends) not in (0, 2):
                raise ValueError("实体断开，无法合并成单链/单环")
            closed = not ends
            current = ends[0] if ends else 0
            points, used = [nodes[current]], set()
            while True:
                remaining = [edge for edge in links[current] if edge not in used]
                if not remaining:
                    break
                edge = remaining[0]
                used.add(edge)
                a, b = edges[edge]
                current = b if current == a else a
                points.append(nodes[current])
            if len(used) != len(edges):
                raise ValueError("实体包含多个不连通链/环，已拒绝合并")
            if closed:
                points.pop()
            plan.update(points=points, closed=closed)
            return {"vertices": points[:_MAX_DETAIL], "vertices_truncated": len(points) > _MAX_DETAIL, "closed": closed}
        def execute(doc, selected):
            entity = _engineering_call(lambda: _add_lightweight_polyline(doc.ModelSpace, plan["points"], plan["closed"]), "创建合并多段线")
            _engineering_call(lambda: setattr(entity, "Normal", _com_point([0, 0, 1])), "设置多段线法向")
            _engineering_call(lambda: setattr(entity, "Elevation", plan["points"][0][2]), "设置多段线标高")
            _engineering_call(lambda: setattr(entity, "ConstantWidth", 0.0), "设置多段线零宽度")
            # Normal/Elevation use OCS; reassign and read back XY before deleting sources.
            _engineering_call(lambda: setattr(entity, "Coordinates", _com_xy_points(plan["points"])), "设置合并顶点")
            coordinates = _numbers(_engineering_call(lambda: entity.Coordinates, "验证合并顶点"))
            expected = [v for point in plan["points"] for v in point[:2]]
            if (len(coordinates) != len(expected) or not all(math.isfinite(v) for v in coordinates)
                    or any(abs(a - b) > 1e-8 for a, b in zip(coordinates, expected))):
                raise RuntimeError("合并多段线顶点不一致，拒绝删除源线或保存")
            elevation = _engineering_call(lambda: entity.Elevation, "验证合并标高")
            normal = _numbers(_engineering_call(lambda: entity.Normal, "验证合并法向"))
            if elevation != plan["points"][0][2] or normal != [0.0, 0.0, 1.0] or bool(entity.Closed) != plan["closed"]:
                raise RuntimeError("合并多段线标高/法向/闭合状态不一致，拒绝删除源线或保存")
            _copy_engineering_style(selected[0][0], entity)
            for source, _ in selected:
                _engineering_call(source.Delete, "删除合并源线")
            return {"changed": len(selected), "created": 1, "erased": len(selected)}
        return _edit_entities(path, layer, types, area, text, dry_run, "join", {"tolerance": tolerance}, None,
                              prepare_selected=prepare, apply_batch=execute)

    @_edit_tool("cad_explode", "调用真实 COM Explode 分解复合实体；erase_source 默认 true，验证生成实体后删除原件；false 保留原件。不支持外部参照、比较参照及带属性块，避免静默无效或属性丢失。",
                {"erase_source": {"type": "boolean", "default": True}}, [])
    @_com_async
    def cad_explode(path: str, erase_source: bool = True, layer: str = "", types: str = "",
                    area: Any = None, text: str = "", dry_run: bool = True):
        if not isinstance(erase_source, bool):
            raise ValueError("erase_source 必须为布尔值")
        methods = []
        def prepare(doc, selected):
            for entity, item in selected:
                if item["type"] in ("AcDbExternalReference", "AcDbComparedReference"):
                    raise ValueError("不支持分解外部参照、比较参照或带属性块")
                if item["type"] == "AcDbBlockReference":
                    definition = _engineering_call(lambda: doc.Blocks.Item(entity.Name), "读取块定义")
                    if (_engineering_call(lambda: definition.IsXRef, "检查外部参照") or
                            _engineering_call(lambda: entity.HasAttributes, "检查块属性")):
                        raise ValueError("不支持分解外部参照、比较参照或带属性块")
                methods.append(_engineering_method(entity, "Explode"))
                if erase_source:
                    _engineering_method(entity, "Delete")
            return {}
        def execute(doc, selected):
            generated = []
            seen = {str(item.get("handle", "")).casefold() for _, item in selected}
            for (source, _), method in zip(selected, methods):
                generated.extend(_generated_entities(_engineering_call(method, "Explode 分解"), seen))
                if erase_source:
                    _engineering_call(source.Delete, "删除分解源件")
            return {"changed": len(selected), "created": len(generated), "erased": len(selected) if erase_source else 0,
                    "entities": generated[:_MAX_DETAIL], "entities_truncated": len(generated) > _MAX_DETAIL}
        return _edit_entities(path, layer, types, area, text, dry_run, "explode", {"erase_source": erase_source}, None,
                              prepare_selected=prepare, apply_batch=execute)


    # ---------- 第三批：填充、引线、图块与交付输出 ----------
    # ActiveX references: AddHatch/AppendOuterLoop, AddLeader/AddMLeader,
    # Block.AddAttribute(Height, Mode, Prompt, InsertionPoint, Tag, Value).
    def _delivery_tool(name, description, extra, required, *, output=False):
        return registry.tool(
            name=name,
            description=(description + " 默认 dry_run=true 仅预览；显式 false 才执行。"
                         + ("输出默认不覆盖；仅 overwrite=true 才允许覆盖并备份旧目标，原图不变。"
                            if output else "直接修改原 DWG，必须同时 overwrite=true；先备份，再按原格式保存。")
                         + "拒绝用户已打开或占用的图纸；COM 不支持就报错。不需要先 launch_app 或截屏点界面。坐标为图纸单位/WCS。"),
            parameters={"type": "object", "properties": {
                "path": {"type": "string", "description": "源 DWG 路径"},
                "dry_run": {"type": "boolean", "default": True},
                "overwrite": {"type": "boolean", "default": False}, **extra,
            }, "required": ["path", *required]}, side_effect=True,
        )

    def _delivery_flags(dry_run, overwrite):
        if not isinstance(dry_run, bool) or not isinstance(overwrite, bool):
            raise ValueError("dry_run/overwrite 必须为布尔值，只有显式 false/true 才执行/覆盖")

    def _delivery_string(value, label, *, empty=False):
        if not isinstance(value, str) or "\x00" in value or (not empty and not value.strip()):
            raise ValueError(f"{label} 必须为{'非空' if not empty else ''}字符串且不含 NUL")
        return value

    def _delivery_positive(value, label):
        value = _edit_number(value, label)
        if value <= 0:
            raise ValueError(f"{label} 必须大于 0")
        return value

    def _delivery_points(value):
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            raise ValueError("points 至少包含两个不同的 [x,y] 或 [x,y,z] 点")
        points = [_edit_point(point, "points") for point in value]
        if any(a == b for a, b in zip(points, points[1:])):
            raise ValueError("引线相邻点不能重合")
        return points

    def _com_xyz_points(points):
        from win32com.client import VARIANT
        import pythoncom
        return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [v for p in points for v in p])

    def _delivery_layer(doc, name):
        _delivery_string(name, "layer", empty=True)
        layer = _find_layer(doc, name) if name else doc.ActiveLayer
        if layer is None:
            raise ValueError(f"图层不存在:{name}")
        if layer.Lock:
            raise ValueError(f"图层已锁定:{layer.Name}")
        return str(layer.Name)

    def _delivery_modify(path, operation, prepare, dry_run, overwrite):
        _delivery_flags(dry_run, overwrite)
        if not dry_run and not overwrite:
            raise ValueError("本工具修改原 DWG，默认不覆盖；请先预览，确认后显式传 dry_run=false、overwrite=true")
        _resolve_for_write(path)

        def preview(doc):
            return {"matched": 1, **prepare(doc)}
        def execute(doc):
            try:
                return operation(doc)
            except Exception as exc:
                raise RuntimeError(f"AutoCAD COM 改图工具执行失败（版本/实体可能不支持），未保存:{exc}") from exc
        return _modify_drawing(path, execute, preview=preview, dry_run=dry_run)

    def _delivery_block(doc, name):
        _delivery_string(name, "block_name")
        if name.startswith("*") or "|" in name:
            raise ValueError("不能操作匿名/布局/外部参照图块")
        for index in range(doc.Blocks.Count):
            block = doc.Blocks.Item(index)
            if str(block.Name).casefold() == name.casefold():
                if block.IsLayout or block.IsXRef:
                    raise ValueError("不能操作布局块或外部参照定义")
                return block
        raise ValueError(f"图块定义不存在:{name}；请先用 cad_read_blocks 查看")

    _point_schema = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 3}
    _leader_schema = {
        "points": {"type": "array", "items": _point_schema, "minItems": 2},
        "text": {"type": "string"}, "height": {"type": "number", "exclusiveMinimum": 0, "default": 2.5},
        "layer": {"type": "string"},
    }

    @_delivery_tool("cad_hatch", "对模型空间中单个已闭合的圆/二维多段线进行关联填充；boundary_handle 来自 cad_read_entities。"
                    "仅支持单外环，不处理孔洞或散线拼环，边界须为有效无自交曲线；pattern 使用已安装的预定义 PAT，如 SOLID/ANSI31，angle 为弧度。",
                    {"boundary_handle": {"type": "string"}, "pattern": {"type": "string", "default": "SOLID"},
                     "scale": {"type": "number", "exclusiveMinimum": 0, "default": 1},
                     "angle": {"type": "number", "default": 0}, "layer": {"type": "string"}}, ["boundary_handle"])
    @_com_async
    def cad_hatch(path: str, boundary_handle: str, pattern: str = "SOLID", scale: float = 1,
                  angle: float = 0, layer: str = "", dry_run: bool = True, overwrite: bool = False):
        _delivery_string(boundary_handle, "boundary_handle")
        _delivery_string(pattern, "pattern")
        scale = _delivery_positive(scale, "scale")
        angle = _edit_number(angle, "angle")
        boundary, target, elevation = None, "", 0.0

        def prepare(doc):
            nonlocal boundary, target, elevation
            target = _delivery_layer(doc, layer)
            for i in range(doc.ModelSpace.Count):
                candidate = doc.ModelSpace.Item(i)
                if str(candidate.Handle).casefold() == boundary_handle.casefold():
                    boundary = candidate
                    break
            if boundary is None:
                raise ValueError(f"模型空间没有此边界 Handle:{boundary_handle}")
            name = str(boundary.ObjectName)
            if name not in ("AcDbCircle", "AcDbPolyline", "AcDb2dPolyline"):
                raise ValueError("填充边界当前仅支持圆或闭合二维多段线")
            if name != "AcDbCircle" and not boundary.Closed:
                raise ValueError("填充边界必须闭合")
            _delivery_positive(boundary.Area, "边界面积")
            normal = _edit_point(list(boundary.Normal), "边界法向")
            if any(abs(a - b) > 1e-9 for a, b in zip(normal, (0, 0, 1))):
                raise ValueError("目前仅支持 WCS XY 平面的边界")
            elevation = (_edit_point(list(boundary.Center), "边界圆心")[2] if name == "AcDbCircle"
                         else _edit_number(boundary.Elevation, "边界标高"))
            source_layer = _find_layer(doc, str(boundary.Layer))
            if source_layer is None or source_layer.Lock:
                raise ValueError("填充边界图层不存在或已锁定")
            _engineering_method(doc.ModelSpace, "AddHatch")
            return {"boundary_handle": str(boundary.Handle), "layer": target, "pattern": pattern}

        def operation(doc):
            from win32com.client import VARIANT
            import pythoncom
            loop = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [boundary])
            hatch = _engineering_call(lambda: doc.ModelSpace.AddHatch(1, pattern, True), "AddHatch")
            # Autodesk: no other access on the new Hatch before AppendOuterLoop.
            _engineering_call(lambda: hatch.AppendOuterLoop(loop), "AppendOuterLoop")
            hatch.Elevation = elevation
            hatch.PatternScale = scale
            hatch.PatternAngle = angle
            hatch.Layer = target
            _engineering_call(hatch.Evaluate, "Evaluate hatch")
            _delivery_positive(hatch.Area, "实际填充面积")
            return {"added": 1, "entities": _generated_entities([hatch], {str(boundary.Handle).casefold()})}
        return _delivery_modify(path, operation, prepare, dry_run, overwrite)

    @_delivery_tool("cad_add_leader", "添加带箭头的直线引线及关联 MText；2～3 个点，文字放在末点，MText 字高用 Height。",
                    {**_leader_schema, "width": {"type": "number", "exclusiveMinimum": 0, "default": 100}}, ["points", "text"])
    @_com_async
    def cad_add_leader(path: str, points: list, text: str, height: float = 2.5, width: float = 100,
                       layer: str = "", dry_run: bool = True, overwrite: bool = False):
        points = _delivery_points(points)
        if len(points) > 3:
            raise ValueError("普通引线只接受 2～3 个点")
        _delivery_string(text, "text")
        height, width = _delivery_positive(height, "height"), _delivery_positive(width, "width")
        target = ""

        def prepare(doc):
            nonlocal target
            target = _delivery_layer(doc, layer)
            _engineering_method(doc.ModelSpace, "AddLeader")
            _engineering_method(doc.ModelSpace, "AddMText")
            return {"points": points, "text": text, "layer": target}

        def operation(doc):
            note = _engineering_call(lambda: doc.ModelSpace.AddMText(_com_point(points[-1]), width, text), "AddMText")
            note.Height, note.Layer = height, target
            leader = _engineering_call(lambda: doc.ModelSpace.AddLeader(_com_xyz_points(points), note, 2), "AddLeader")
            leader.Layer = target
            return {"added": 2, "entities": _generated_entities([note, leader], set())}
        return _delivery_modify(path, operation, prepare, dry_run, overwrite)

    @_delivery_tool("cad_add_multileader", "使用真实 AddMLeader(pointsArray, leaderLineIndex=0) 添加带文字的多重引线。"
                    "当前单引线簇，文字使用 MLeader.TextHeight（不同于 MText.Height）。",
                    _leader_schema, ["points", "text"])
    @_com_async
    def cad_add_multileader(path: str, points: list, text: str, height: float = 2.5,
                            layer: str = "", dry_run: bool = True, overwrite: bool = False):
        points = _delivery_points(points)
        _delivery_string(text, "text")
        height = _delivery_positive(height, "height")
        target = ""

        def prepare(doc):
            nonlocal target
            target = _delivery_layer(doc, layer)
            _engineering_method(doc.ModelSpace, "AddMLeader")
            return {"points": points, "text": text, "layer": target}

        def operation(doc):
            result = _engineering_call(lambda: doc.ModelSpace.AddMLeader(_com_xyz_points(points), 0), "AddMLeader")
            # AutoCAD 2014/pywin32 returns (MLeader, in/out leaderLineIndex).
            parts = result if isinstance(result, tuple) else (result,)
            entities = [item for item in parts if getattr(item, "ObjectName", "") == "AcDbMLeader"]
            if len(entities) != 1:
                raise RuntimeError("AddMLeader 未返回唯一有效的多重引线实体，未保存")
            entity = entities[0]
            entity.ContentType = 2  # acMTextContent
            entity.TextString, entity.TextHeight, entity.Layer = text, height, target
            return {"added": 1, "entities": _generated_entities([entity], set())}
        return _delivery_modify(path, operation, prepare, dry_run, overwrite)

    @_delivery_tool("cad_insert_block", "插入本图已有的命名图块，不重新定义图块；scale=[sx,sy,sz] 必须为正，rotation 为弧度。",
                    {"block_name": {"type": "string"}, "insertion": _point_schema,
                     "scale": {"type": "array", "items": {"type": "number", "exclusiveMinimum": 0}, "minItems": 3, "maxItems": 3},
                     "rotation": {"type": "number", "default": 0}, "layer": {"type": "string"}}, ["block_name", "insertion"])
    @_com_async
    def cad_insert_block(path: str, block_name: str, insertion: list, scale: list | None = None, rotation: float = 0,
                         layer: str = "", dry_run: bool = True, overwrite: bool = False):
        insertion = _edit_point(insertion, "insertion")
        scale = [1, 1, 1] if scale is None else scale
        if not isinstance(scale, (list, tuple)) or len(scale) != 3:
            raise ValueError("scale 必须为三个正数 [sx,sy,sz]")
        scale = [_delivery_positive(v, "scale") for v in scale]
        rotation = _edit_number(rotation, "rotation")
        target, name = "", ""

        def prepare(doc):
            nonlocal target, name
            name = str(_delivery_block(doc, block_name).Name)
            target = _delivery_layer(doc, layer)
            _engineering_method(doc.ModelSpace, "InsertBlock")
            return {"block_name": name, "insertion": insertion, "scale": scale, "rotation": rotation, "layer": target}

        def operation(doc):
            entity = _engineering_call(lambda: doc.ModelSpace.InsertBlock(_com_point(insertion), name, *scale, rotation), "InsertBlock")
            entity.Layer = target
            return {"added": 1, "entities": _generated_entities([entity], set())}
        return _delivery_modify(path, operation, prepare, dry_run, overwrite)

    @_delivery_tool("cad_add_attribute", "向已有命名图块定义添加属性定义（不是对块参照调用假接口）。"
                    "调用 Block.AddAttribute(height,mode,prompt,insertion,tag,value)；mode 为 0～63 位标志组合。"
                    "tag 不可含空白/感叹号且不可重复；不替换已有属性，不自动同步已插入的块参照。",
                    {"block_name": {"type": "string"}, "height": {"type": "number", "exclusiveMinimum": 0},
                     "mode": {"type": "integer", "minimum": 0, "maximum": 63}, "prompt": {"type": "string"},
                     "insertion": _point_schema, "tag": {"type": "string"}, "value": {"type": "string"}},
                    ["block_name", "height", "mode", "prompt", "insertion", "tag", "value"])
    @_com_async
    def cad_add_attribute(path: str, block_name: str, height: float, mode: int, prompt: str, insertion: list,
                          tag: str, value: str, dry_run: bool = True, overwrite: bool = False):
        height = _delivery_positive(height, "height")
        insertion = _edit_point(insertion, "insertion")
        if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 63:
            raise ValueError("mode 必须为 0～63 的整数位标志组合")
        _delivery_string(prompt, "prompt", empty=True)
        _delivery_string(value, "value")
        _delivery_string(tag, "tag")
        if any(c.isspace() or c == "!" for c in tag):
            raise ValueError("tag 不允许空白或感叹号")
        tag = tag.upper()
        block = None

        def prepare(doc):
            nonlocal block
            block = _delivery_block(doc, block_name)
            _delivery_layer(doc, "")
            _engineering_method(block, "AddAttribute")
            for i in range(block.Count):
                entity = block.Item(i)
                if str(entity.ObjectName) == "AcDbAttributeDefinition" and str(entity.TagString).casefold() == tag.casefold():
                    raise ValueError(f"属性 tag 已存在，拒绝覆盖:{tag}")
            return {"block_name": str(block.Name), "tag": tag, "value": value,
                    "note": "只修改图块定义；现有块参照不自动同步，新插入块可使用该属性。"}

        def operation(doc):
            entity = _engineering_call(lambda: block.AddAttribute(height, mode, prompt, _com_point(insertion), tag, value), "Block.AddAttribute")
            return {"added": 1, "entities": _generated_entities([entity], set())}
        return _delivery_modify(path, operation, prepare, dry_run, overwrite)

    @registry.tool(
        name="cad_read_blocks", description="读取命名块定义、属性定义和模型空间块参照。属性定义与参照分开返回；均有返回上限及截断标志，不修改图纸。不需要先 launch_app 或截屏点界面。",
        parameters={"type": "object", "properties": {"path": {"type": "string"}, "block_name": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_ENTITIES, "default": 100}}, "required": ["path"]})
    @_com_async
    def cad_read_blocks(path: str, block_name: str = "", limit: int = 100):
        _delivery_string(block_name, "block_name", empty=True)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_ENTITIES:
            raise ValueError(f"limit 必须为 1～{_MAX_ENTITIES} 的整数")
        p = _resolve(path)
        if not p.is_file():
            raise RuntimeError(f"图纸不存在:{p}")
        app, doc, own_doc, own_app = _open_cad_drawing(win32com.client, p)
        try:
            blocks, references = [], []
            block_total, reference_total = 0, 0
            for i in range(doc.Blocks.Count):
                block = doc.Blocks.Item(i)
                name = str(block.Name)
                if name.startswith("*") or (block_name and name.casefold() != block_name.casefold()):
                    continue
                block_total += 1
                if len(blocks) >= limit:
                    continue
                attributes, attribute_total = [], 0
                for j in range(block.Count):
                    entity = block.Item(j)
                    if str(entity.ObjectName) != "AcDbAttributeDefinition":
                        continue
                    attribute_total += 1
                    if len(attributes) < limit:
                        attributes.append({"tag": str(entity.TagString), "value": str(entity.TextString),
                                           "prompt": str(entity.PromptString), "height": float(entity.Height),
                                           "mode": int(entity.Mode), "insertion": _numbers(entity.InsertionPoint),
                                           "handle": str(entity.Handle)})
                blocks.append({"name": name, "entity_count": int(block.Count), "is_xref": bool(block.IsXRef),
                               "attributes": attributes, "attribute_total": attribute_total,
                               "attributes_truncated": attribute_total > len(attributes)})
            for i in range(doc.ModelSpace.Count):
                entity = doc.ModelSpace.Item(i)
                if str(entity.ObjectName) != "AcDbBlockReference":
                    continue
                name = str(entity.Name)
                if block_name and name.casefold() != block_name.casefold():
                    continue
                reference_total += 1
                if len(references) < limit:
                    references.append({"name": name, "handle": str(entity.Handle), "layer": str(entity.Layer),
                                       "insertion": _numbers(entity.InsertionPoint), "has_attributes": bool(entity.HasAttributes)})
            return {"blocks": blocks, "block_total": block_total, "blocks_truncated": block_total > len(blocks),
                    "references": references, "reference_total": reference_total, "references_truncated": reference_total > len(references)}
        except Exception as exc:
            raise RuntimeError(f"AutoCAD COM 读取图块失败（版本可能不支持）:{exc}") from exc
        finally:
            _release_cad_drawing(app, doc, own_doc, own_app)

    def _output_target(source, output_path, suffix, overwrite):
        _delivery_string(output_path, "output_path")
        raw = Path(output_path)
        if raw.is_symlink():
            raise ValueError("输出目标不能是符号链接")
        target = _resolve_for_write(output_path)
        if target.suffix.casefold() != suffix:
            raise ValueError(f"输出扩展名必须为 {suffix}")
        if target == source or (target.exists() and os.path.samefile(source, target)):
            raise ValueError("输出路径不能与原图相同（含硬链接）")
        locks = _lock_files(target)
        if locks:
            raise RuntimeError(f"输出目标有锁文件，拒绝覆盖:{', '.join(locks)}")
        if target.exists():
            if not overwrite:
                raise FileExistsError(f"目标已存在，默认不覆盖:{target}；确认后显式 overwrite=true")
            if not target.is_file():
                raise ValueError(f"输出目标不是普通文件:{target}")
            _check_writable(target, _lock_files(target))
        if target.parent.exists() and not target.parent.is_dir():
            raise ValueError("输出父路径不是目录")
        return target

    def _plot_pair(config, method, *, points=False):
        if getattr(config, "_oleobj_", None) is None:
            return _engineering_call(getattr(config, method), method)
        import pythoncom
        from win32com.client import VARIANT
        outputs = [VARIANT(pythoncom.VT_BYREF | (pythoncom.VT_VARIANT if points else pythoncom.VT_R8),
                           None if points else 0.0) for _ in range(2)]
        _engineering_call(lambda: getattr(config, method)(*outputs), method)
        return tuple(item.value for item in outputs)

    def _plot_pdf(doc, stage, layout_name, plot_config):
        _engineering_call(doc.Activate, "Activate 导出文档")
        background = _engineering_call(lambda: doc.GetVariable("BACKGROUNDPLOT"), "读取 BACKGROUNDPLOT")
        original_layout = doc.ActiveLayout
        layout = original_layout
        if layout_name:
            layouts = [doc.Layouts.Item(i) for i in range(doc.Layouts.Count)]
            layout = next((x for x in layouts if str(x.Name).casefold() == layout_name.casefold()), None)
            if layout is None:
                raise ValueError(f"布局不存在:{layout_name}")
        snapshot = None
        snapshot_ready = False
        # Bind before plotting: AutoCAD 2014 can reject name lookup immediately after a plot.
        restore_settings = layout.CopyFrom
        delete_snapshot = None
        try:
            # Layout derives from PlotConfiguration. CopyFrom preserves ALL plot settings,
            # including ConfigName="None", without assigning that forbidden string.
            snapshot = _engineering_call(
                lambda: doc.PlotConfigurations.Add("xiaoa_" + uuid.uuid4().hex, bool(layout.ModelType)),
                "创建临时打印配置",
            )
            _engineering_call(lambda: snapshot.CopyFrom(layout), "快照布局打印配置")
            snapshot_ready = True
            delete_snapshot = snapshot.Delete
            _engineering_call(lambda: doc.SetVariable("BACKGROUNDPLOT", 0), "设置前台打印")
            doc.ActiveLayout = layout
            _engineering_call(layout.RefreshPlotDeviceInfo, "RefreshPlotDeviceInfo")
            devices = _engineering_call(layout.GetPlotDeviceNames, "GetPlotDeviceNames")
            if plot_config.casefold() not in [str(x).casefold() for x in devices]:
                raise RuntimeError(f"未安装 PDF 打印设备:{plot_config}")
            layout.ConfigName = plot_config
            _engineering_call(layout.RefreshPlotDeviceInfo, "RefreshPlotDeviceInfo")
            # Switching PC3 can reset paper/scale. Fail on incompatible media rather
            # than silently printing at another size; read back retained settings.
            def keep_setting(attr):
                expected = _engineering_call(lambda: getattr(snapshot, attr), f"读取打印设置 {attr}")
                current = _engineering_call(lambda: getattr(layout, attr), f"核对打印设置 {attr}")
                if current != expected:
                    value = expected
                    if attr == "PlotOrigin":
                        from win32com.client import VARIANT
                        import pythoncom
                        value = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(expected))
                    _engineering_call(lambda: setattr(layout, attr, value), f"保留打印设置 {attr}")
                    if _engineering_call(lambda: getattr(layout, attr), f"验证打印设置 {attr}") != expected:
                        raise RuntimeError(f"打印设备未保留 {attr}，拒绝输出错误尺寸/比例的 PDF")

            for attr in ("PaperUnits", "CanonicalMediaName", "PlotRotation", "UseStandardScale"):
                keep_setting(attr)
            if snapshot.UseStandardScale:
                keep_setting("StandardScale")
            else:
                scale = _plot_pair(snapshot, "GetCustomScale")
                _engineering_call(lambda: layout.SetCustomScale(*scale), "保留自定义打印比例")
                actual = _plot_pair(layout, "GetCustomScale")
                if len(actual) != 2 or any(not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12) for a, b in zip(actual, scale)):
                    raise RuntimeError("打印设备未保留自定义打印比例，拒绝输出 PDF")
            if snapshot.PlotType == 4:  # acWindow: set the window BEFORE PlotType
                window = _plot_pair(snapshot, "GetWindowToPlot", points=True)
                from win32com.client import VARIANT
                import pythoncom
                corners = [VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(point)) for point in window]
                _engineering_call(lambda: layout.SetWindowToPlot(*corners), "保留打印窗口")
                actual = _plot_pair(layout, "GetWindowToPlot", points=True)
                if tuple(map(tuple, actual)) != tuple(map(tuple, window)):
                    raise RuntimeError("打印设备未保留打印窗口，拒绝输出 PDF")
            elif snapshot.PlotType == 3:  # acView
                keep_setting("ViewToPlot")
            keep_setting("PlotType")
            keep_setting("CenterPlot")
            if not snapshot.CenterPlot:
                keep_setting("PlotOrigin")
            silent_config = _silent_pdf_pc3(doc.Application, plot_config, stage.parent)
            # plotConfig accepts a FULL PC3 path. The private copy suppresses PDF viewers
            # before staging is published/moved, without altering the user's installed PC3.
            success = _engineering_call(lambda: doc.Plot.PlotToFile(str(stage), str(silent_config)), "PlotToFile")
            if not success:
                raise RuntimeError("PlotToFile 返回失败，未发布 PDF")
        finally:
            failures = []
            for label, restore in (
                ("打印设置", lambda: restore_settings(snapshot) if snapshot_ready else None),
                ("当前布局", lambda: setattr(doc, "ActiveLayout", original_layout)),
                ("BACKGROUNDPLOT", lambda: doc.SetVariable("BACKGROUNDPLOT", background)),
            ):
                try:
                    _engineering_call(restore, f"恢复{label}")
                except Exception as exc:
                    failures.append(str(exc))
            if snapshot is not None:
                try:
                    _engineering_call(delete_snapshot or snapshot.Delete, "删除临时打印配置")
                except Exception as exc:
                    failures.append(str(exc))
            if failures:
                raise RuntimeError("PDF 设置恢复失败，未发布输出:" + ";".join(failures))

    def _delivery_output(path, output_path, kind, dry_run, overwrite, *, layout_name="", plot_config=""):
        import tempfile
        _delivery_flags(dry_run, overwrite)
        source = _resolve_for_write(path)
        if not source.is_file():
            raise RuntimeError(f"图纸不存在:{source}")
        target = _output_target(source, output_path, "." + kind, overwrite)
        header = _dwg_header(source)
        save_type = _DWG_HEADER_TO_SAVETYPE.get(header)
        if kind != "pdf" and save_type is None:
            raise ValueError(f"无法确定原 DWG 格式:{header}；拒绝自动升级或猜测导出版本")
        if kind == "dxf":
            save_type += 1  # ac2000/2004/2007/2010/2013/2018_dxf
        if dry_run:
            return {"dry_run": True, "output_path": str(target), "format": header if kind != "pdf" else "PDF",
                    "backup": None, "target_backup": None, "saved_as": None, "ok": True,
                    "summary": "仅预览路径和格式，未创建输出、备份或打开 AutoCAD。"}
        pre_locks = _lock_files(source)
        backup, target_backup = None, None
        target.parent.mkdir(parents=True, exist_ok=True)
        # Same-volume staging allows atomic publish, keeping old targets intact on COM failure.
        with tempfile.TemporaryDirectory(prefix=".xiaoa-cad-", dir=target.parent) as folder:
            stage = Path(folder) / target.name
            app, doc, own_doc, own_app = _open_cad_drawing(win32com.client, source)
            try:
                if not own_doc:
                    raise RuntimeError("原图正开在你的 AutoCAD 里，请先保存并关闭后再导出")
                _check_writable(source, pre_locks)
                for opened_doc in _engineering_call(lambda: _cad_documents(app), "检查输出目标是否已打开"):
                    opened = _doc_path(opened_doc)
                    if opened and Path(opened).resolve() == target:
                        raise RuntimeError("输出目标正开在 AutoCAD 中，拒绝覆盖")
                backup = _backup_drawing(source)
                if kind == "pdf":
                    _plot_pdf(doc, stage, layout_name, plot_config)
                    saved_as = "PlotToFile(PDF)"
                else:
                    _engineering_call(lambda: doc.SaveAs(str(stage), save_type), f"SaveAs {kind.upper()}（原版本 {header}）")
                    saved_as = f"SaveAs({header} {kind.upper()})"
            except Exception as exc:
                raise RuntimeError(f"AutoCAD COM 输出失败，目标未发布；原图备份:{backup}；{exc}") from exc
            finally:
                _release_cad_drawing(app, doc, own_doc, own_app)
            if not stage.is_file() or stage.stat().st_size == 0:
                raise RuntimeError("COM 未生成非空输出文件，拒绝报告成功或覆盖目标")
            with stage.open("rb") as stream:
                signature = stream.read(65536)
                stream.seek(max(0, stage.stat().st_size - 256))
                tail = stream.read()
            if kind == "dwg" and _dwg_header(stage) != header:
                raise RuntimeError("输出 DWG 版本与原图不符，拒绝发布")
            if kind == "dxf":
                import re
                version = re.search(rb"\$ACADVER\s+1\s+(AC[0-9]{4})", signature)
                if not (b"SECTION" in signature and b"EOF" in tail and version
                        and version.group(1).decode("ascii") == header):
                    raise RuntimeError("输出不是对应原版本的文本 DXF，拒绝发布")
            if kind == "pdf" and not (signature.startswith(b"%PDF-") and b"%%EOF" in tail):
                raise RuntimeError("输出不是完整 PDF，拒绝发布")
            _output_target(source, output_path, "." + kind, overwrite)
            if target.exists():
                target_backup = _backup_drawing(target)
            if overwrite:
                os.replace(stage, target)
            else:
                os.link(stage, target)  # fails atomically if another process created the target
            return {"dry_run": False, "output_path": str(target), "size": target.stat().st_size,
                    "backup": backup, "target_backup": target_backup, "saved_as": saved_as, "ok": True,
                    "summary": f"已核验并输出 {target}；原图未改写，备份:{backup}；目标旧文件备份:{target_backup or '(无)'}。"}

    _output_schema = {"output_path": {"type": "string", "description": "目标完整路径；默认不能存在"}}

    @_delivery_tool("cad_save_as", "按源 DWG 版本另存到新路径；版本不支持直接报错，不退回 Save 原图。", _output_schema, ["output_path"], output=True)
    @_com_async
    def cad_save_as(path: str, output_path: str, dry_run: bool = True, overwrite: bool = False):
        return _delivery_output(path, output_path, "dwg", dry_run, overwrite)

    @_delivery_tool("cad_export_dxf", "用 SaveAs 的真实 DXF 格式枚举导出整图文本 DXF，版本对应原 DWG。", _output_schema, ["output_path"], output=True)
    @_com_async
    def cad_export_dxf(path: str, output_path: str, dry_run: bool = True, overwrite: bool = False):
        return _delivery_output(path, output_path, "dxf", dry_run, overwrite)

    @_delivery_tool("cad_export_pdf", "用已安装的 PDF PC3 打印单个布局，沿用该布局纸张/比例/范围设置，不自动排版。"
                    "先激活文档，设 BACKGROUNDPLOT=0，结束恢复后台打印/当前布局/设备。",
                    {**_output_schema, "layout_name": {"type": "string", "description": "留空使用当前布局"},
                     "plot_config": {"type": "string", "default": "DWG To PDF.pc3"}}, ["output_path"], output=True)
    @_com_async
    def cad_export_pdf(path: str, output_path: str, layout_name: str = "", plot_config: str = "DWG To PDF.pc3",
                       dry_run: bool = True, overwrite: bool = False):
        _delivery_string(layout_name, "layout_name", empty=True)
        _delivery_string(plot_config, "plot_config")
        if not plot_config.casefold().endswith(".pc3") or "pdf" not in plot_config.casefold():
            raise ValueError("plot_config 必须是 PDF .pc3 打印配置")
        return _delivery_output(path, output_path, "pdf", dry_run, overwrite, layout_name=layout_name, plot_config=plot_config)

    # ---------- 读图:图纸体检 ----------
    @registry.tool(
        name="cad_open_info",
        description=(
            "打开一张 DWG 图纸(只读)并返回'图纸体检报告':单位(毫米/米/英寸)、DWG 版本、"
            "图层清单、块定义(名称+插入次数)、标注/文字样式、图形范围、按类型的实体统计。"
            "改图前必须先调它,弄清单位与图层,避免混用毫米/米差 1000 倍。"
            "这是驱动 AutoCAD 的精确工具,不需要先 launch_app 打开 AutoCAD、也不需要截屏点界面。"
        ),
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "dwg 文件绝对路径"}},
            "required": ["path"],
        },
    )
    @_com_async
    def cad_open_info(path: str) -> dict[str, Any]:
        p = _resolve(path)
        if not p.is_file():
            raise RuntimeError(f"图纸不存在:{p}")
        header = _dwg_header(p)
        app, doc, own_doc, own_app = _open_cad_drawing(win32com.client, p)
        try:
            insunits = _int_or_none(_retry_on_busy(
                lambda: doc.GetVariable("INSUNITS"), what="读单位",
            ))
            layers = []
            for index in range(doc.Layers.Count):
                layer = doc.Layers.Item(index)
                layers.append(_layer_info(layer))

            # 块:*Model_Space / *Paper_Space 这些是布局用的系统块,不是用户图块,
            # 报出来只会干扰判断。插入次数要数模型空间里的块参照,Blocks.Item(i).Count
            # 是"块定义内部的实体数",不是插入次数。
            block_names = []
            for index in range(doc.Blocks.Count):
                try:
                    name = str(doc.Blocks.Item(index).Name)
                except Exception:
                    continue
                if name.startswith("*"):
                    continue
                block_names.append(name)

            stats: dict[str, int] = {}
            insert_counts: dict[str, int] = {}
            for entity in _modelspace_entities(doc):
                key = str(getattr(entity, "ObjectName", "未知") or "未知")
                stats[key] = stats.get(key, 0) + 1
                if "BlockReference" in key:
                    try:
                        ref = str(entity.Name)
                    except Exception:
                        continue
                    insert_counts[ref] = insert_counts.get(ref, 0) + 1
            blocks = [
                {"name": name, "inserted": insert_counts.get(name, 0)}
                for name in block_names
            ]

            def _style_names(collection: Any) -> list[str]:
                try:
                    return [str(collection.Item(i).Name) for i in range(collection.Count)]
                except Exception:
                    return []

            return {
                "path": str(p),
                "unit": _unit_label(insunits),
                "insunits": insunits,
                "dwg_format": _DWG_HEADER_LABELS.get(header, header or "未知"),
                "layers": layers,
                "layer_count": len(layers),
                "blocks": blocks,
                "dim_styles": _style_names(doc.DimStyles),
                "text_styles": _style_names(doc.TextStyles),
                "extents": {
                    "min": _numbers(doc.GetVariable("EXTMIN")),
                    "max": _numbers(doc.GetVariable("EXTMAX")),
                },
                "entity_stats": stats,
                "entity_total": sum(stats.values()),
                "reused_open_drawing": not own_doc,
            }
        finally:
            _release_cad_drawing(app, doc, own_doc, own_app)

    # ---------- 读图:图层清单 ----------
    @registry.tool(
        name="cad_list_layers",
        description=(
            "只读取 DWG 的图层清单及颜色、线型、线宽、开关、冻结、锁定状态。"
            "画图或改图前可先用它确认目标图层。"
            "这是驱动 AutoCAD 的精确工具,不需要先 launch_app 打开 AutoCAD、也不需要截屏点界面。"
        ),
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "dwg 文件绝对路径"}},
            "required": ["path"],
        },
    )
    @_com_async
    def cad_list_layers(path: str) -> dict[str, Any]:
        p = _resolve(path)
        if not p.is_file():
            raise RuntimeError(f"图纸不存在:{p}")
        app, doc, own_doc, own_app = _open_cad_drawing(win32com.client, p)
        try:
            layers = [_layer_info(doc.Layers.Item(index)) for index in range(doc.Layers.Count)]
            return {"path": str(p), "count": len(layers), "layers": layers}
        finally:
            _release_cad_drawing(app, doc, own_doc, own_app)

    # ---------- 改图:图层 ----------
    @registry.tool(
        name="cad_create_layer",
        description=(
            "在 DWG 中创建图层;如果同名图层已经存在则复用并更新传入属性。"
            "支持 color(ACI 颜色号)、linetype、lineweight、on、frozen、locked。执行前会备份原图。"
            "这是驱动 AutoCAD 的精确工具,不需要先 launch_app 打开 AutoCAD、也不需要截屏点界面。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "dwg 文件绝对路径"},
                "name": {"type": "string", "description": "图层名"},
                "color": {"type": "integer", "description": "ACI 颜色号,如 7"},
                "linetype": {"type": "string"},
                "lineweight": {"type": "integer"},
                "on": {"type": "boolean"}, "frozen": {"type": "boolean"},
                "locked": {"type": "boolean"},
            },
            "required": ["path", "name"],
        },
        side_effect=True,
    )
    @_com_async
    def cad_create_layer(path: str, name: str, color: int | None = None,
                         linetype: str | None = None, lineweight: int | None = None,
                         on: bool | None = None, frozen: bool | None = None,
                         locked: bool | None = None) -> dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            raise ValueError("图层名不能为空")
        values = {"color": color, "linetype": linetype, "lineweight": lineweight,
                  "on": on, "frozen": frozen, "locked": locked}

        def operation(doc: Any) -> dict[str, Any]:
            layer = _find_layer(doc, name)
            created = layer is None
            if created:
                try:
                    layer = doc.Layers.Add(name)
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(f"创建图层失败:{exc}") from exc
            _set_layer_properties(layer, values)
            return {"layer": _layer_info(layer), "created": created}

        return _modify_drawing(path, operation)

    @registry.tool(
        name="cad_update_layer",
        description=(
            "更新 DWG 中已有图层的颜色、线型、线宽、开关、冻结、锁定状态。"
            "图层不存在会报错,不会悄悄创建;执行前会备份原图。"
            "这是驱动 AutoCAD 的精确工具,不需要先 launch_app 打开 AutoCAD、也不需要截屏点界面。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "dwg 文件绝对路径"},
                "name": {"type": "string"}, "color": {"type": "integer"},
                "linetype": {"type": "string"}, "lineweight": {"type": "integer"},
                "on": {"type": "boolean"}, "frozen": {"type": "boolean"},
                "locked": {"type": "boolean"},
            },
            "required": ["path", "name"],
        },
        side_effect=True,
    )
    @_com_async
    def cad_update_layer(path: str, name: str, color: int | None = None,
                         linetype: str | None = None, lineweight: int | None = None,
                         on: bool | None = None, frozen: bool | None = None,
                         locked: bool | None = None) -> dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            raise ValueError("图层名不能为空")
        values = {"color": color, "linetype": linetype, "lineweight": lineweight,
                  "on": on, "frozen": frozen, "locked": locked}

        def operation(doc: Any) -> dict[str, Any]:
            layer = _find_layer(doc, name)
            if layer is None:
                raise RuntimeError(f"图层不存在:{name}")
            _set_layer_properties(layer, values)
            return {"layer": _layer_info(layer), "created": False}

        return _modify_drawing(path, operation)

    # ---------- 读图:按筛选读实体 ----------
    @registry.tool(
        name="cad_read_entities",
        description=(
            "按图层名、实体类型、矩形范围筛选读取模型空间实体,返回真实几何:"
            "多段线给全部顶点+是否闭合+长度+面积,直线给起终点+长度,圆弧给圆心+半径+起止角,"
            "文字给插入点+字高,块参照给插入点+块名,每个实体都给包围盒 bbox 和外形尺寸 size。"
            "要在已有图形上加标注,先用它读回顶点/包围盒,再把坐标交给 cad_add_dimensions。"
            "大图纸实体很多,默认最多返回 500 条且会报告总数;用 area/layer/types 缩小范围。"
            "坐标一律是图纸自身单位(先看 cad_open_info 的 unit)。"
            "这是驱动 AutoCAD 的精确工具,不需要先 launch_app 打开 AutoCAD、也不需要截屏点界面。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "dwg 绝对路径"},
                "layer": {"type": "string", "description": "图层名,精确匹配;空=不限"},
                "types": {"type": "string", "description": "逗号分隔的实体类型,如 LINE,TEXT,DIMENSION;空=不限"},
                "area": {"type": "array", "items": {"type": "number"}, "description": "矩形范围 [x1,y1,x2,y2],用于空间筛选"},
                "limit": {"type": "integer", "description": "最多返回条数,默认 500"},
            },
            "required": ["path"],
        },
    )
    @_com_async
    def cad_read_entities(
        path: str, layer: str = "", types: str = "",
        area: Any = None, limit: int = _MAX_ENTITIES,
    ) -> dict[str, Any]:
        p = _resolve(path)
        if not p.is_file():
            raise RuntimeError(f"图纸不存在:{p}")
        app, doc, own_doc, own_app = _open_cad_drawing(win32com.client, p)
        try:
            type_list = [t.strip().lower() for t in str(types).split(",") if t.strip()] if types else []
            matched = []
            for entity in _modelspace_entities(doc):
                try:
                    entity_layer = str(entity.Layer)
                except Exception:
                    entity_layer = ""
                if layer and entity_layer != layer:
                    continue
                name = str(getattr(entity, "ObjectName", "") or "")
                if type_list and not any(t in name.lower() for t in type_list):
                    continue
                geometry = _entity_geometry(entity, name)
                position = _entity_position(entity)
                if not _in_area(position, area, geometry.get("bbox")):
                    continue
                item: dict[str, Any] = {
                    "type": name,
                    "layer": entity_layer,
                    "position": position,
                }
                handle = getattr(entity, "Handle", None)
                if handle:
                    item["handle"] = str(handle)
                text = _entity_text(entity)
                if text:
                    item["text"] = text
                item.update(geometry)
                matched.append(item)
            total = len(matched)
            limited = matched[:max(1, int(limit))]
            return {
                "total": total,
                "returned": len(limited),
                "truncated": total > len(limited),
                "entities": limited,
            }
        finally:
            _release_cad_drawing(app, doc, own_doc, own_app)

    # ---------- 读图:读文字 ----------
    @registry.tool(
        name="cad_read_text",
        description=(
            "读一张图里所有 TEXT/MTEXT/标注的文字内容 + 位置 + 所在图层。"
            "改图最常见的批量动作是改文字(图名、日期、标高、说明),这是它的前提。"
            "默认最多返回 500 条。"
            "这是驱动 AutoCAD 的精确工具,不需要先 launch_app 打开 AutoCAD、也不需要截屏点界面。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "dwg 绝对路径"},
                "layer": {"type": "string", "description": "图层名,空=不限"},
            },
            "required": ["path"],
        },
    )
    @_com_async
    def cad_read_text(path: str, layer: str = "") -> dict[str, Any]:
        p = _resolve(path)
        if not p.is_file():
            raise RuntimeError(f"图纸不存在:{p}")
        app, doc, own_doc, own_app = _open_cad_drawing(win32com.client, p)
        try:
            texts = []
            for entity in _modelspace_entities(doc):
                name = str(getattr(entity, "ObjectName", "") or "")
                if not ("Text" in name or "Dimension" in name):
                    continue
                if layer and str(entity.Layer) != layer:
                    continue
                texts.append({
                    "type": name,
                    "layer": str(entity.Layer),
                    "text": _entity_text(entity),
                    "position": _entity_position(entity),
                })
            total = len(texts)
            return {
                "total": total,
                "returned": min(total, _MAX_ENTITIES),
                "truncated": total > _MAX_ENTITIES,
                "texts": texts[:_MAX_ENTITIES],
            }
        finally:
            _release_cad_drawing(app, doc, own_doc, own_app)

    # ---------- 改图:批量查找替换文字 ----------
    @registry.tool(
        name="cad_replace_text",
        description=(
            "批量查找替换 TEXT/MTEXT 文字(图名、日期、设计变更编号……)。"
            "默认 dry_run=True 只报告会改哪些/共几处,不真正改动;显式传 "
            "dry_run=False 才真改并保存。真改前会强制备份原文件。"
            "改前建议先 cad_read_text 看清要改的文字。"
            "这是驱动 AutoCAD 的精确工具,不需要先 launch_app 打开 AutoCAD、也不需要截屏点界面。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "dwg 绝对路径"},
                "replacements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old": {"type": "string"},
                            "new": {"type": "string"},
                            "layer": {"type": "string", "description": "只在指定图层替换;空=全部图层"},
                        },
                        "required": ["old", "new"],
                    },
                },
                "dry_run": {"type": "boolean", "description": "默认 true 只报告不修改;false 才真改"},
            },
            "required": ["path", "replacements"],
        },
        side_effect=True,
    )
    @_com_async
    def cad_replace_text(path: str, replacements: list, dry_run: bool = True) -> dict[str, Any]:
        p = _resolve(path)
        if not p.is_file():
            raise RuntimeError(f"图纸不存在:{p}")
        header = _dwg_header(p)
        pre_locks = _lock_files(p)  # 必须在我们自己打开图纸之前拍快照
        app, doc, own_doc, own_app = _open_cad_drawing(win32com.client, p)
        try:
            if not own_doc:
                raise RuntimeError(
                    f"这张图纸正开在你的 AutoCAD 里,不能替我改(会和你手上的编辑冲突)。"
                    f"请先在 AutoCAD 里保存并关闭它,再让我改:{p}"
                )
            matches = []   # (entity, old, new)
            preview = []   # 纯 Python 数据,给用户看的明细
            for entity in _modelspace_entities(doc):
                name = str(getattr(entity, "ObjectName", "") or "")
                if "Text" not in name:
                    continue
                current = str(getattr(entity, "TextString", "") or "")
                for repl in replacements:
                    if not isinstance(repl, dict):
                        continue
                    old = str(repl.get("old") or "")
                    new = str(repl.get("new") or "")
                    if not old:
                        continue
                    if repl.get("layer") and str(entity.Layer) != str(repl["layer"]):
                        continue
                    if old in current:
                        matches.append((entity, old, new))
                        if len(preview) < _MAX_DETAIL:
                            # 只放可序列化的字段。COM 实体对象绝不能进返回值。
                            preview.append({
                                "type": name,
                                "layer": str(entity.Layer),
                                "before": current,
                                "after": current.replace(old, new),
                                "position": _entity_position(entity),
                            })
            if dry_run:
                return {
                    "dry_run": True,
                    "changes": len(matches),
                    "detail": preview,
                    "detail_truncated": len(matches) > len(preview),
                    "note": "未做任何改动。确认无误后传 dry_run=False 才会真改并备份保存。",
                }
            _check_writable(p, pre_locks)
            backup = _backup_drawing(p)
            changed = 0
            for entity, old, new in matches:
                entity.TextString = str(getattr(entity, "TextString", "") or "").replace(old, new)
                changed += 1
            saved_as = _save_drawing(doc, p, header)
            return {
                "dry_run": False,
                "backup": backup,
                "changes": changed,
                "saved_as": saved_as,
                "summary": _modify_summary(saved_as, backup, own_app),
                "ok": True,
            }
        finally:
            _release_cad_drawing(app, doc, own_doc, own_app)

    @registry.tool(
        name="cad_draw_arc",
        description=(
            "批量在 DWG 中画圆弧;每项提供 center=[x,y]、radius、start_angle、end_angle,"
            "角度单位为弧度,可指定图层。执行前会备份原图并保存。"
            "这是驱动 AutoCAD 的精确工具,不需要先 launch_app 打开 AutoCAD、也不需要截屏点界面。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "items": {"type": "array", "items": {"type": "object", "properties": {
                    "center": {"type": "array", "items": {"type": "number"}},
                    "radius": {"type": "number"},
                    "start_angle": {"type": "number"},
                    "end_angle": {"type": "number"},
                    "layer": {"type": "string"},
                }, "required": ["center", "radius", "start_angle", "end_angle"]}},
            },
            "required": ["path", "items"],
        },
        side_effect=True,
    )
    @_com_async
    def cad_draw_arc(path: str, items: list) -> dict[str, Any]:
        def operation(doc: Any) -> dict[str, Any]:
            added, failed = 0, []
            for index, item in enumerate(items or []):
                if not isinstance(item, dict):
                    failed.append({"index": index, "reason": "不是对象"})
                    continue
                try:
                    radius = float(item.get("radius"))
                    if radius <= 0:
                        raise ValueError("半径必须大于 0")
                    entity = doc.ModelSpace.AddArc(
                        _com_point(item.get("center")), radius,
                        float(item.get("start_angle")), float(item.get("end_angle")),
                    )
                    _apply_layer(entity, item.get("layer"))
                    added += 1
                except Exception as exc:  # noqa: BLE001
                    failed.append({"index": index, "reason": str(exc)})
            return {"added": added, "failed": failed}

        return _modify_drawing(path, operation)

    @registry.tool(
        name="cad_draw_ellipse",
        description=(
            "批量在 DWG 中画椭圆;每项提供 center、major_axis 主轴向量和 ratio 短轴/长轴比例,"
            "ratio 必须大于 0 且不超过 1,可指定图层。执行前会备份原图并保存。"
            "这是驱动 AutoCAD 的精确工具,不需要先 launch_app 打开 AutoCAD、也不需要截屏点界面。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "items": {"type": "array", "items": {"type": "object", "properties": {
                    "center": {"type": "array", "items": {"type": "number"}},
                    "major_axis": {"type": "array", "items": {"type": "number"}},
                    "ratio": {"type": "number"}, "layer": {"type": "string"},
                }, "required": ["center", "major_axis", "ratio"]}},
            },
            "required": ["path", "items"],
        },
        side_effect=True,
    )
    @_com_async
    def cad_draw_ellipse(path: str, items: list) -> dict[str, Any]:
        def operation(doc: Any) -> dict[str, Any]:
            added, failed = 0, []
            for index, item in enumerate(items or []):
                if not isinstance(item, dict):
                    failed.append({"index": index, "reason": "不是对象"})
                    continue
                try:
                    major_axis = _point(item.get("major_axis"))
                    ratio = float(item.get("ratio"))
                    if major_axis[0] == 0 and major_axis[1] == 0 and major_axis[2] == 0:
                        raise ValueError("major_axis 不能是零向量")
                    if ratio <= 0 or ratio > 1:
                        raise ValueError("ratio 必须大于 0 且不超过 1")
                    entity = doc.ModelSpace.AddEllipse(
                        _com_point(item.get("center")), _com_point(major_axis), ratio,
                    )
                    _apply_layer(entity, item.get("layer"))
                    added += 1
                except Exception as exc:  # noqa: BLE001
                    failed.append({"index": index, "reason": str(exc)})
            return {"added": added, "failed": failed}

        return _modify_drawing(path, operation)

    @registry.tool(
        name="cad_draw_point",
        description=(
            "批量在 DWG 中插入点;每项提供 point=[x,y] 或 [x,y,z],可指定图层。"
            "执行前会备份原图并保存。"
            "这是驱动 AutoCAD 的精确工具,不需要先 launch_app 打开 AutoCAD、也不需要截屏点界面。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "items": {"type": "array", "items": {"type": "object", "properties": {
                    "point": {"type": "array", "items": {"type": "number"}},
                    "layer": {"type": "string"},
                }, "required": ["point"]}},
            },
            "required": ["path", "items"],
        },
        side_effect=True,
    )
    @_com_async
    def cad_draw_point(path: str, items: list) -> dict[str, Any]:
        def operation(doc: Any) -> dict[str, Any]:
            added, failed = 0, []
            for index, item in enumerate(items or []):
                if not isinstance(item, dict):
                    failed.append({"index": index, "reason": "不是对象"})
                    continue
                try:
                    entity = doc.ModelSpace.AddPoint(_com_point(item.get("point")))
                    _apply_layer(entity, item.get("layer"))
                    added += 1
                except Exception as exc:  # noqa: BLE001
                    failed.append({"index": index, "reason": str(exc)})
            return {"added": added, "failed": failed}

        return _modify_drawing(path, operation)

    # ---------- 读图:结果验证 ----------
    @registry.tool(
        name="cad_verify_drawing",
        description=(
            "只读验证 DWG 是否能被 AutoCAD COM 正常打开和枚举,返回实体总数、类型统计、"
            "图层数量、图纸范围和少量实体样本。适合每次改图保存后复核,不会修改或备份图纸。"
            "这是驱动 AutoCAD 的精确工具,不需要先 launch_app 打开 AutoCAD、也不需要截屏点界面。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "sample_limit": {"type": "integer", "description": "样本最多返回多少条,默认 20"},
            },
            "required": ["path"],
        },
    )
    @_com_async
    def cad_verify_drawing(path: str, sample_limit: int = 20) -> dict[str, Any]:
        p = _resolve(path)
        if not p.is_file():
            raise RuntimeError(f"图纸不存在:{p}")
        sample_limit = max(0, min(int(sample_limit), 100))
        header = _dwg_header(p)
        app, doc, own_doc, own_app = _open_cad_drawing(win32com.client, p)
        try:
            stats: dict[str, int] = {}
            samples: list[dict[str, Any]] = []
            for entity in _modelspace_entities(doc):
                name = str(getattr(entity, "ObjectName", "未知") or "未知")
                stats[name] = stats.get(name, 0) + 1
                if len(samples) < sample_limit:
                    item = {"type": name, "layer": str(getattr(entity, "Layer", "") or "")}
                    item["position"] = _entity_position(entity)
                    item.update(_entity_geometry(entity, name))
                    samples.append(item)
            extents = {
                "min": _numbers(doc.GetVariable("EXTMIN")),
                "max": _numbers(doc.GetVariable("EXTMAX")),
            }
            return {
                "ok": True,
                "path": str(p),
                "dwg_format": _DWG_HEADER_LABELS.get(header, header or "未知"),
                "layer_count": int(doc.Layers.Count),
                "entity_total": sum(stats.values()),
                "entity_stats": stats,
                "extents": extents,
                "sample": samples,
                "sample_truncated": sum(stats.values()) > len(samples),
                "reused_open_drawing": not own_doc,
            }
        finally:
            _release_cad_drawing(app, doc, own_doc, own_app)

    # ---------- 改图:批量加文字 ----------
    @registry.tool(
        name="cad_add_text",
        description=(
            "批量在图纸上加文字(标注说明、图名、标高、日期)。一次调用传几十条,"
            "不要一条一个工具调用。坐标和高度一律是图纸自身单位(见 cad_open_info 的 unit);"
            "加前会强制备份。"
            "这是驱动 AutoCAD 的精确工具,不需要先 launch_app 打开 AutoCAD、也不需要截屏点界面。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "dwg 绝对路径"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                            "height": {"type": "number", "description": "字高(图纸自身单位),默认 2.5"},
                            "layer": {"type": "string"},
                            "rotation": {"type": "number", "description": "旋转角(弧度),默认 0"},
                        },
                        "required": ["text", "x", "y"],
                    },
                },
            },
            "required": ["path", "items"],
        },
        side_effect=True,
    )
    @_com_async
    def cad_add_text(path: str, items: list) -> dict[str, Any]:
        p = _resolve(path)
        if not p.is_file():
            raise RuntimeError(f"图纸不存在:{p}")
        header = _dwg_header(p)
        pre_locks = _lock_files(p)  # 必须在我们自己打开图纸之前拍快照
        app, doc, own_doc, own_app = _open_cad_drawing(win32com.client, p)
        try:
            if not own_doc:
                raise RuntimeError(
                    f"这张图纸正开在你的 AutoCAD 里,不能替我改(会和你手上的编辑冲突)。"
                    f"请先在 AutoCAD 里保存并关闭它,再让我改:{p}"
                )
            _check_writable(p, pre_locks)
            backup = _backup_drawing(p)
            added = 0
            skipped = 0
            for item in items:
                if not isinstance(item, dict):
                    skipped += 1
                    continue
                text = str(item.get("text") or "")
                if not text:
                    skipped += 1
                    continue
                entity = doc.ModelSpace.AddText(
                    text, _com_point([item.get("x", 0), item.get("y", 0)]), float(item.get("height", 2.5)),
                )
                if item.get("layer"):
                    entity.Layer = str(item["layer"])
                if item.get("rotation"):
                    entity.Rotation = float(item["rotation"])
                added += 1
            saved_as = _save_drawing(doc, p, header)
            return {
                "added": added, "skipped": skipped,
                "backup": backup, "saved_as": saved_as,
                "summary": _modify_summary(saved_as, backup, own_app),
                "ok": True,
            }
        finally:
            _release_cad_drawing(app, doc, own_doc, own_app)

    # ---------- 改图:基础几何 ----------
    @registry.tool(
        name="cad_draw_line",
        description=(
            "批量在 DWG 中画直线,每项使用 start=[x,y]、end=[x,y],可指定图层。"
            "坐标使用图纸自身单位;执行前会备份原图并保存。"
            "这是驱动 AutoCAD 的精确工具,不需要先 launch_app 打开 AutoCAD、也不需要截屏点界面。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "items": {"type": "array", "items": {"type": "object", "properties": {
                    "start": {"type": "array", "items": {"type": "number"}},
                    "end": {"type": "array", "items": {"type": "number"}},
                    "layer": {"type": "string"},
                }, "required": ["start", "end"]}},
            },
            "required": ["path", "items"],
        },
        side_effect=True,
    )
    @_com_async
    def cad_draw_line(path: str, items: list) -> dict[str, Any]:
        def operation(doc: Any) -> dict[str, Any]:
            added, failed = 0, []
            for index, item in enumerate(items or []):
                if not isinstance(item, dict):
                    failed.append({"index": index, "reason": "不是对象"})
                    continue
                try:
                    start, end = _point(item.get("start")), _point(item.get("end"))
                    if start[:2] == end[:2]:
                        raise ValueError("起点和终点不能相同")
                    entity = doc.ModelSpace.AddLine(_com_point(start), _com_point(end))
                    _apply_layer(entity, item.get("layer"))
                    added += 1
                except Exception as exc:  # noqa: BLE001
                    failed.append({"index": index, "reason": str(exc)})
            return {"added": added, "failed": failed}

        return _modify_drawing(path, operation)

    @registry.tool(
        name="cad_draw_polyline",
        description=(
            "批量在 DWG 中画轻量多段线;每项提供 points=[[x,y],...],默认不闭合,"
            "可用 closed=true 闭合,并可指定图层。执行前会备份原图并保存。"
            "这是驱动 AutoCAD 的精确工具,不需要先 launch_app 打开 AutoCAD、也不需要截屏点界面。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "items": {"type": "array", "items": {"type": "object", "properties": {
                    "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
                    "closed": {"type": "boolean"}, "layer": {"type": "string"},
                }, "required": ["points"]}},
            },
            "required": ["path", "items"],
        },
        side_effect=True,
    )
    @_com_async
    def cad_draw_polyline(path: str, items: list) -> dict[str, Any]:
        def operation(doc: Any) -> dict[str, Any]:
            added, failed = 0, []
            for index, item in enumerate(items or []):
                if not isinstance(item, dict):
                    failed.append({"index": index, "reason": "不是对象"})
                    continue
                try:
                    points = _points(item.get("points"))
                    entity = _add_lightweight_polyline(doc.ModelSpace, points, bool(item.get("closed", False)))
                    _apply_layer(entity, item.get("layer"))
                    added += 1
                except Exception as exc:  # noqa: BLE001
                    failed.append({"index": index, "reason": str(exc)})
            return {"added": added, "failed": failed}

        return _modify_drawing(path, operation)

    @registry.tool(
        name="cad_draw_rectangle",
        description=(
            "批量在 DWG 中画矩形;每项用 p1=[x1,y1]、p2=[x2,y2] 表示对角点,可指定图层。"
            "矩形由闭合轻量多段线创建,执行前会备份原图并保存。"
            "这是驱动 AutoCAD 的精确工具,不需要先 launch_app 打开 AutoCAD、也不需要截屏点界面。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "items": {"type": "array", "items": {"type": "object", "properties": {
                    "p1": {"type": "array", "items": {"type": "number"}},
                    "p2": {"type": "array", "items": {"type": "number"}},
                    "layer": {"type": "string"},
                }, "required": ["p1", "p2"]}},
            },
            "required": ["path", "items"],
        },
        side_effect=True,
    )
    @_com_async
    def cad_draw_rectangle(path: str, items: list) -> dict[str, Any]:
        def operation(doc: Any) -> dict[str, Any]:
            added, failed = 0, []
            for index, item in enumerate(items or []):
                if not isinstance(item, dict):
                    failed.append({"index": index, "reason": "不是对象"})
                    continue
                try:
                    x1, y1, _ = _point(item.get("p1"))
                    x2, y2, _ = _point(item.get("p2"))
                    if x1 == x2 or y1 == y2:
                        raise ValueError("矩形两个对角点不能在同一条水平或竖直线上")
                    points = [[x1, y1, 0.0], [x2, y1, 0.0], [x2, y2, 0.0], [x1, y2, 0.0]]
                    entity = _add_lightweight_polyline(doc.ModelSpace, points, True)
                    _apply_layer(entity, item.get("layer"))
                    added += 1
                except Exception as exc:  # noqa: BLE001
                    failed.append({"index": index, "reason": str(exc)})
            return {"added": added, "failed": failed}

        return _modify_drawing(path, operation)

    @registry.tool(
        name="cad_add_mtext",
        description=(
            "批量在 DWG 中添加多行文字;每项提供 text、insertion=[x,y],可选 width、height、"
            "rotation 和 layer。坐标和字高使用图纸自身单位;执行前会备份原图并保存。"
            "这是驱动 AutoCAD 的精确工具,不需要先 launch_app 打开 AutoCAD、也不需要截屏点界面。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "items": {"type": "array", "items": {"type": "object", "properties": {
                    "text": {"type": "string"},
                    "insertion": {"type": "array", "items": {"type": "number"}},
                    "width": {"type": "number"}, "height": {"type": "number"},
                    "rotation": {"type": "number"}, "layer": {"type": "string"},
                }, "required": ["text", "insertion"]}},
            },
            "required": ["path", "items"],
        },
        side_effect=True,
    )
    @_com_async
    def cad_add_mtext(path: str, items: list) -> dict[str, Any]:
        def operation(doc: Any) -> dict[str, Any]:
            added, skipped, failed = 0, 0, []
            for index, item in enumerate(items or []):
                if not isinstance(item, dict) or not str(item.get("text") or ""):
                    skipped += 1
                    continue
                try:
                    text = str(item["text"])
                    width = float(item.get("width", 100.0))
                    height = float(item.get("height", 2.5))
                    if width <= 0 or height <= 0:
                        raise ValueError("width 和 height 必须大于 0")
                    entity = doc.ModelSpace.AddMText(
                        _com_point(item.get("insertion")), width, text,
                    )
                    entity.Height = height
                    if item.get("rotation") is not None:
                        entity.Rotation = float(item["rotation"])
                    _apply_layer(entity, item.get("layer"))
                    added += 1
                except Exception as exc:  # noqa: BLE001
                    failed.append({"index": index, "reason": str(exc)})
            return {"added": added, "skipped": skipped, "failed": failed}

        return _modify_drawing(path, operation)

    # ---------- 改图:精确画圆 ----------
    @registry.tool(
        name="cad_draw_circle",
        description=(
            "在 AutoCAD 当前图纸里精确画圆,不需要截图、鼠标找命令或逐步点击。"
            "center=[x,y]、radius 使用图纸自身单位;传 path 时针对指定 DWG。"
            "默认保存并备份;未指定 path 时新建空白图纸并保存到 output_path 或隔离工作区，不混入用户当前图纸。"
            "output_path 可指定另存位置;save=False 只改当前打开图纸、不落盘。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "可选,已有 DWG 路径;不传且保存时新建空白图纸；save=False 则编辑当前图纸并保持打开"},
                "output_path": {"type": "string", "description": "可选,另存为的新 DWG 路径;相对路径属于隔离工作区,不覆盖已有文件"},
                "center": {"type": "array", "items": {"type": "number"}, "description": "圆心 [x,y]"},
                "radius": {"type": "number", "description": "半径,必须大于 0"},
                "layer": {"type": "string", "description": "可选图层名"},
                "save": {"type": "boolean", "description": "是否保存;默认 true"},
            },
            "required": ["center", "radius"],
        },
        side_effect=True,
    )
    @_com_async
    def cad_draw_circle(
        center: list, radius: float, path: str = "", layer: str = "", save: bool = True,
        output_path: str = "",
    ) -> dict[str, Any]:
        radius = float(radius)
        if radius <= 0:
            raise ValueError("半径必须大于 0")
        point = _point(center)
        p = _resolve(path) if str(path or "").strip() else None
        if p is not None and not p.is_file():
            raise RuntimeError(f"图纸不存在:{p}")

        if not isinstance(save, bool):
            raise ValueError("save 必须为布尔值")
        if not save and str(output_path or "").strip():
            raise ValueError("save=False 时不能指定 output_path；请明确是否保存")
        header = _dwg_header(p) if p is not None else ""
        pre_locks = _lock_files(p) if p is not None else []
        created_doc = False
        completed = False
        if p is not None:
            app, doc, own_doc, own_app = _open_cad_drawing(win32com.client, p)
        else:
            app, _strategy, own_app = _resolve_cad_app(win32com.client, prefer_running=True)
            own_doc = False
            try:
                if getattr(app, "_oleobj_", None) is not None:
                    app = _dynamic_cad_object(win32com.client, app)
                documents = _retry_on_busy(lambda: _cad_documents(app), what="枚举当前图纸")
                doc = (_retry_on_busy(lambda: app.ActiveDocument, what="读取当前图纸", retry_missing=True)
                       if documents else None)
                task_owned = own_app or _is_task_cad(app)
                blank = (task_owned and doc is not None and not _doc_path(doc)
                         and _retry_on_busy(lambda: doc.ModelSpace.Count, what="检查初始图纸") == 0
                         and _retry_on_busy(lambda: doc.PaperSpace.Count, what="检查初始布局") == 0)
                if doc is None or (save and not blank):
                    doc = _retry_on_busy(lambda: app.Documents.Add(), what="新建图纸", retry_missing=True)
                    own_doc = True
                    created_doc = True
                elif task_owned:
                    own_doc = True
            except Exception:
                if own_app:
                    app.Quit()
                raise
        try:
            backup = ""
            snapshot_saved = False
            if save:
                if p is None or str(output_path or "").strip():
                    source = _doc_path(doc)
                    if source and Path(source).is_absolute() and Path(source).is_file():
                        header = _dwg_header(Path(source))
                    p = (_resolve_for_write(output_path) if str(output_path or "").strip()
                         else isolation_workspace.workspace() / (
                             f"CAD-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:12]}.dwg"
                         ))
                    if p.suffix.lower() != ".dwg":
                        raise ValueError("另存路径必须是 .dwg 文件")
                    if p.exists():
                        raise FileExistsError(f"不覆盖已有图纸,请换一个另存路径:{p}")
                    p.parent.mkdir(parents=True, exist_ok=True)
                    # SaveAs 建立当前图纸的完整快照，再备份再改图；失败不会画出重复实体。
                    save_type = _DWG_HEADER_TO_SAVETYPE.get(header)
                    _retry_on_busy(
                        lambda: (doc.SaveAs(str(p), save_type) if save_type is not None
                                 else doc.SaveAs(str(p))), what="另存图纸",
                    )
                    header = _dwg_header(p)
                    snapshot_saved = True
                    pre_locks = []  # 新副本由当前实例持有，不把自身锁当成外部占用。
                elif not own_doc:
                    raise RuntimeError(
                        "这张图纸正开在你的 AutoCAD 里,不能替我保存修改。"
                        f"请先在 AutoCAD 里保存并关闭它,再让我改:{p}"
                    )
                _check_writable(p, pre_locks)
                backup = _backup_drawing(p)
            entity = _retry_on_busy(lambda: doc.ModelSpace.AddCircle(_com_point(point), radius), what="画圆")
            if layer:
                entity.Layer = str(layer)
            saved_as = ""
            if save:
                if snapshot_saved:
                    _retry_on_busy(doc.Save, what="保存新副本")
                    saved_as = "Save(新副本)"
                else:
                    saved_as = _save_drawing(doc, p, header)
            completed = True
            return {
                "ok": True,
                "center": point[:2],
                "radius": radius,
                "path": str(p) if p is not None else _doc_path(doc),
                "saved_as": saved_as,
                "backup": backup,
                "summary": (
                    f"已在 {point[:2]} 画圆,半径 {radius},已保存到 {p}。"
                    if save else f"已在 {point[:2]} 画圆,半径 {radius},尚未保存。"
                ),
            }
        finally:
            if not save:
                # Successful unsaved work stays open, including drawings opened
                # by path. On failure only a document we just created may close.
                _release_cad_drawing(app, doc, created_doc and not completed, False)
            else:
                _release_cad_drawing(app, doc, own_doc, own_app)

    # ---------- 常用三维实体：真实 ActiveX 3DSolid，非二维 AddSolid ----------
    def _solid_record(entity):
        if entity is None or str(entity.ObjectName) != "AcDb3dSolid":
            raise RuntimeError("COM未返回真实3DSolid，拒绝报告成功")
        volume = _delivery_positive(entity.Volume, "实体体积")
        if getattr(entity, "_oleobj_", None) is not None:
            # Our cache-free DumbDispatch strategy has no [out] type information.
            from win32com.client import VARIANT
            import pythoncom
            low = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_VARIANT, None)
            high = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_VARIANT, None)
            raw = _dynamic_cad_object(win32com.client, entity._oleobj_)
            _engineering_call(lambda: raw.GetBoundingBox(low, high), "读取三维包围盒")
            low, high = low.value, high.value
        else:
            low, high = entity.GetBoundingBox()
        if len(low) != 3 or len(high) != 3:
            raise RuntimeError("三维包围盒必须返回XYZ，不能推测缺失Z值")
        low, high = _edit_point(list(low), "bbox.min"), _edit_point(list(high), "bbox.max")
        if any(a > b for a, b in zip(low, high)):
            raise RuntimeError("实体包围盒无效")
        handle = str(entity.Handle)
        if not handle or any(c not in "0123456789abcdefABCDEF" for c in handle):
            raise RuntimeError("三维实体Handle无效")
        # ActiveX documents Centroid as XY; keep exactly the dimensions actually returned.
        centroid = list(entity.Centroid)
        if len(centroid) not in (2, 3):
            raise RuntimeError("三维实体重心返回值无效")
        centroid = [_edit_number(v, "centroid") for v in centroid]
        return {"handle": handle, "type": "AcDb3dSolid", "layer": str(entity.Layer),
                "volume": volume, "centroid": centroid,
                "bbox": {"min": low, "max": high}}

    def _3d_handles(handles):
        if not isinstance(handles, list) or not 1 <= len(handles) <= _MAX_ENTITIES:
            raise ValueError(f"handles必须为1～{_MAX_ENTITIES}个实体Handle的数组")
        for handle in handles:
            _delivery_string(handle, "handle")
            if any(c not in "0123456789abcdefABCDEF" for c in handle):
                raise ValueError("Handle必须为十六进制字符串，请从读图工具返回值获取")
        if len({h.casefold() for h in handles}) != len(handles):
            raise ValueError("handles不能重复")
        return handles

    def _3d_entities(doc, handles, allowed, *, writable=True):
        wanted = {h.casefold() for h in handles}
        entities = {}
        for i in range(doc.ModelSpace.Count):
            entity = doc.ModelSpace.Item(i)
            handle = str(entity.Handle).casefold()
            if handle in wanted:
                if str(entity.ObjectName) not in allowed:
                    raise ValueError(f"实体{handle}类型不支持:{entity.ObjectName}")
                if writable:
                    _delivery_layer(doc, str(entity.Layer))
                entities[handle] = entity
        if wanted - entities.keys():
            raise ValueError("模型空间没有这些Handle:" + ",".join(sorted(wanted - entities.keys())))
        return [entities[h.casefold()] for h in handles]

    def _3d_remove_if_present(doc, handle):
        for i in range(doc.ModelSpace.Count):
            entity = doc.ModelSpace.Item(i)
            if str(entity.Handle).casefold() == handle.casefold():
                _engineering_call(entity.Delete, "清理三维临时实体")
                return

    @_delivery_tool("cad_create_3d_solid", "创建单个三维实体：box长方体、wedge楔体、cylinder圆柱、cone圆锥、sphere球、torus圆环。"
                    "center直接传给ActiveX：box/cylinder/cone是包围盒中心，wedge是楔体面中心，sphere/torus是几何中心，不是底角点。"
                    "box/wedge需length/width/height；cylinder/cone需radius/height；sphere需radius；torus需radius和tube_radius且radius>tube_radius。"
                    "尺寸必须为正；初始方向沿WCS，圆柱/圆锥轴向为Z。返回真实体积、重心及三维包围盒。",
                    {"kind": {"type": "string", "enum": ["box", "wedge", "cylinder", "cone", "sphere", "torus"]},
                     "center": point_schema, "length": {"type": "number"}, "width": {"type": "number"},
                     "height": {"type": "number"}, "radius": {"type": "number"}, "tube_radius": {"type": "number"},
                     "layer": {"type": "string"}}, ["kind", "center"])
    @_com_async
    def cad_create_3d_solid(path: str, kind: str, center: list, length: float | None = None, width: float | None = None,
                            height: float | None = None, radius: float | None = None, tube_radius: float | None = None,
                            layer: str = "", dry_run: bool = True, overwrite: bool = False):
        shapes = {"box": ("AddBox", ["length", "width", "height"]), "wedge": ("AddWedge", ["length", "width", "height"]),
                  "cylinder": ("AddCylinder", ["radius", "height"]), "cone": ("AddCone", ["radius", "height"]),
                  "sphere": ("AddSphere", ["radius"]), "torus": ("AddTorus", ["radius", "tube_radius"])}
        if not isinstance(kind, str) or kind not in shapes:
            raise ValueError("kind必须为box/wedge/cylinder/cone/sphere/torus")
        center = _edit_point(center, "center")
        method, required = shapes[kind]
        supplied = dict(length=length, width=width, height=height, radius=radius, tube_radius=tube_radius)
        if any(value is not None and key not in required for key, value in supplied.items()):
            raise ValueError("该形体不使用这些多余尺寸参数")
        dimensions = {key: _delivery_positive(supplied[key], key) for key in required}
        if kind == "torus" and dimensions["radius"] <= dimensions["tube_radius"]:
            raise ValueError("圆环主半径必须大于管半径，当前只支持普通环形体")
        try:
            if kind in ("box", "wedge"):
                expected_volume = math.prod(dimensions.values()) / (2 if kind == "wedge" else 1)
            elif kind in ("cylinder", "cone"):
                expected_volume = math.pi * dimensions["radius"] ** 2 * dimensions["height"] / (3 if kind == "cone" else 1)
            elif kind == "sphere":
                expected_volume = 4 * math.pi * dimensions["radius"] ** 3 / 3
            else:
                expected_volume = 2 * math.pi ** 2 * dimensions["radius"] * dimensions["tube_radius"] ** 2
            _delivery_positive(expected_volume, "预计实体体积")
        except OverflowError as exc:
            raise ValueError("尺寸超出有限数值范围") from exc
        target = ""
        def prepare(doc):
            nonlocal target
            target = _delivery_layer(doc, layer)
            _engineering_method(doc.ModelSpace, method)
            return {"kind": kind, "center": center, "dimensions": dimensions, "layer": target}
        def operation(doc):
            seen = {str(doc.ModelSpace.Item(i).Handle).casefold() for i in range(doc.ModelSpace.Count)}
            entity = _engineering_call(lambda: getattr(doc.ModelSpace, method)(_com_point(center), *dimensions.values()), method)
            if entity is None or str(entity.Handle).casefold() in seen:
                raise RuntimeError("未创建独立三维实体，拒绝保存")
            entity.Layer = target
            record = _solid_record(entity)
            if not math.isclose(record["volume"], expected_volume, rel_tol=1e-7, abs_tol=1e-12):
                raise RuntimeError("实体体积与请求尺寸不符，拒绝保存")
            return {"added": 1, "changed": 1, "entities": [record]}
        return _delivery_modify(path, operation, prepare, dry_run, overwrite)

    @registry.tool(name="cad_read_solids", description="只读列出模型空间3DSolid的Handle、体积、重心、XYZ包围盒，供后续精确选取。"
                   "layer留空读取全部图层，不需要launch_app或截屏点界面。",
                   parameters={"type": "object", "properties": {"path": {"type": "string"}, "layer": {"type": "string"},
                               "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_ENTITIES, "default": 100}}, "required": ["path"]})
    @_com_async
    def cad_read_solids(path: str, layer: str = "", limit: int = 100):
        _delivery_string(layer, "layer", empty=True)
        if type(limit) is not int or not 1 <= limit <= _MAX_ENTITIES:
            raise ValueError(f"limit必须为1～{_MAX_ENTITIES}的整数")
        p = _resolve(path)
        if not p.is_file():
            raise RuntimeError(f"图纸不存在:{p}")
        app, doc, own_doc, own_app = _open_cad_drawing(win32com.client, p)
        try:
            records, total = [], 0
            for i in range(doc.ModelSpace.Count):
                entity = doc.ModelSpace.Item(i)
                if str(entity.ObjectName) == "AcDb3dSolid" and (not layer or str(entity.Layer) == layer):
                    total += 1
                    if len(records) < limit:
                        records.append(_solid_record(entity))
            return {"entities": records, "total": total, "truncated": total > limit, "ok": True}
        finally:
            _release_cad_drawing(app, doc, own_doc, own_app)

    def _3d_copy(entity, originals):
        copy = _engineering_call(entity.Copy, "复制三维操作源件")
        if copy is None or not str(copy.Handle) or str(copy.Handle).casefold() in originals:
            raise RuntimeError("COM未返回独立副本，拒绝修改源件")
        if str(copy.ObjectName) != str(entity.ObjectName):
            raise RuntimeError("副本实体类型错误")
        return copy

    def _3d_profile(doc, handle):
        source = _3d_entities(doc, [handle], {"AcDbRegion", "AcDbCircle", "AcDbPolyline"})[0]
        if source.ObjectName == "AcDbPolyline" and not source.Closed:
            raise ValueError("拉伸/旋转成型需要闭合多段线")
        _delivery_positive(source.Area, "轮廓面积")
        if source.ObjectName != "AcDbRegion" and _edit_number(source.Thickness, "轮廓厚度") != 0:
            raise ValueError("轮廓必须为零厚度二维曲线；请先清除Thickness")
        _engineering_method(source, "Copy")
        if source.ObjectName != "AcDbRegion":
            _engineering_method(doc.ModelSpace, "AddRegion")
        return source

    def _3d_form(path, handle, method, args, layer, dry_run, overwrite):
        _3d_handles([handle])
        source, target = None, ""
        def prepare(doc):
            nonlocal source, target
            source = _3d_profile(doc, handle)
            target = _delivery_layer(doc, layer or str(source.Layer))
            _engineering_method(doc.ModelSpace, method)
            return {"profile_handle": handle, "preserve_profile": True, "parameters": args, "layer": target}
        def operation(doc):
            original_handles = {str(doc.ModelSpace.Item(i).Handle).casefold() for i in range(doc.ModelSpace.Count)}
            working = _3d_copy(source, original_handles)
            temporary = [str(working.Handle)]
            region = working
            if str(working.ObjectName) != "AcDbRegion":
                from win32com.client import VARIANT
                import pythoncom
                regions = _engineering_call(lambda: doc.ModelSpace.AddRegion(
                    VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [working])), "从闭合轮廓创建临时Region")
                if not isinstance(regions, (list, tuple)) or len(regions) != 1:
                    raise RuntimeError("轮廓未形成唯一有效面域；不支持散线、多外环或自交轮廓")
                region = regions[0]
                if str(region.ObjectName) != "AcDbRegion" or str(region.Handle).casefold() in original_handles:
                    raise RuntimeError("COM未创建独立Region，拒绝修改")
                temporary.append(str(region.Handle))
            _delivery_positive(region.Area, "临时Region面积")
            converted = [_com_point(a) if isinstance(a, list) else a for a in args]
            entity = _engineering_call(lambda: getattr(doc.ModelSpace, method)(region, *converted), method)
            if entity is None or str(entity.Handle).casefold() in original_handles:
                raise RuntimeError("未创建独立三维实体")
            entity.Layer = target
            record = _solid_record(entity)
            for temp in temporary:
                _3d_remove_if_present(doc, temp)
            _3d_entities(doc, [handle], {str(source.ObjectName)})  # original profile must survive
            return {"added": 1, "changed": 1, "entities": [record], "profile_preserved": True}
        return _delivery_modify(path, operation, prepare, dry_run, overwrite)

    @_delivery_tool("cad_extrude", "拉伸模型空间单个闭合圆/轻量多段线/Region形成3DSolid。先在副本上创建Region，保留原轮廓并清理临时面域。"
                    "height非零（可负，沿轮廓OCS法向），taper_angle为弧度且绝对值小于π/2；自交/不闭合/不支持的轮廓直接报错。",
                    {"profile_handle": {"type": "string"}, "height": {"type": "number"},
                     "taper_angle": {"type": "number", "default": 0}, "layer": {"type": "string"}}, ["profile_handle", "height"])
    @_com_async
    def cad_extrude(path: str, profile_handle: str, height: float, taper_angle: float = 0, layer: str = "",
                    dry_run: bool = True, overwrite: bool = False):
        height, taper_angle = _edit_number(height, "height"), _edit_number(taper_angle, "taper_angle")
        if height == 0 or abs(taper_angle) >= math.pi / 2:
            raise ValueError("height不能为零，taper_angle必须在(-π/2,π/2)内")
        return _3d_form(path, profile_handle, "AddExtrudedSolid", [height, taper_angle], layer, dry_run, overwrite)

    @_delivery_tool("cad_revolve", "将单个闭合圆/轻量多段线/Region绕轴旋转成型并保留原轮廓。"
                    "axis_point是WCS轴上一点，axis_direction是非零方向向量（不是第二个点）；0<angle<=2π弧度，遵循右手法则。",
                    {"profile_handle": {"type": "string"}, "axis_point": point_schema, "axis_direction": point_schema,
                     "angle": {"type": "number", "default": math.tau}, "layer": {"type": "string"}},
                    ["profile_handle", "axis_point", "axis_direction"])
    @_com_async
    def cad_revolve(path: str, profile_handle: str, axis_point: list, axis_direction: list, angle: float = math.tau,
                    layer: str = "", dry_run: bool = True, overwrite: bool = False):
        axis_point, axis_direction = _edit_point(axis_point, "axis_point"), _edit_point(axis_direction, "axis_direction")
        angle = _delivery_positive(angle, "angle")
        norm = math.hypot(*axis_direction)
        if norm == 0 or not math.isfinite(norm) or angle > math.tau:
            raise ValueError("axis_direction必须非零且有限，angle不能大于2π")
        direction = [v / norm for v in axis_direction]
        return _3d_form(path, profile_handle, "AddRevolvedSolid", [axis_point, direction, angle], layer, dry_run, overwrite)

    def _3d_plane(p1, p2, p3):
        points = [_edit_point(p, "plane_point") for p in (p1, p2, p3)]
        a, b = [[points[i][k] - points[0][k] for k in range(3)] for i in (1, 2)]
        lengths = [math.hypot(*v) for v in (a, b)]
        if min(lengths) <= 1e-12 or not all(math.isfinite(n) for n in lengths):
            raise ValueError("平面三点必须不同且距离有限")
        a, b = [[v / n for v in row] for row, n in zip((a, b), lengths)]
        cross = [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]
        if math.hypot(*cross) <= 1e-9:
            raise ValueError("平面三点不能共线")
        return points

    def _3d_selected_edit(path, handles, prepare_action, operation, dry_run, overwrite):
        handles = _3d_handles(handles)
        selected, before = [], []
        def prepare(doc):
            selected.extend(_3d_entities(doc, handles, {"AcDb3dSolid"}))
            before.extend(_solid_record(entity) for entity in selected)
            details = prepare_action(doc, selected)
            return {"handles": handles, "before": before, **details}
        def execute(doc):
            return operation(doc, selected, before)
        return _delivery_modify(path, execute, prepare, dry_run, overwrite)

    @_delivery_tool("cad_boolean", "对恰好两个3DSolid作union并集/intersection交集/subtract差集；差集顺序为target减tool。"
                    "先检查真实干涉体积，再在副本上运算并核对体积，避免AutoCAD无交集时不报错但返回原件。"
                    "默认keep_sources=true保留两件原实体，结果为新实体；false在成功核验后删除两件原件。",
                    {"target_handle": {"type": "string"}, "tool_handle": {"type": "string"},
                     "operation": {"type": "string", "enum": ["union", "intersection", "subtract"]},
                     "keep_sources": {"type": "boolean", "default": True}}, ["target_handle", "tool_handle", "operation"])
    @_com_async
    def cad_boolean(path: str, target_handle: str, tool_handle: str, operation: str, keep_sources: bool = True,
                    dry_run: bool = True, overwrite: bool = False):
        if not isinstance(operation, str) or operation not in ("union", "intersection", "subtract") or type(keep_sources) is not bool:
            raise ValueError("operation必须为union/intersection/subtract且keep_sources必须为布尔值")
        def prepare(doc, selected):
            for entity in selected:
                _engineering_method(entity, "Copy")
            _engineering_method(selected[0], "Boolean")
            _engineering_method(selected[0], "CheckInterference")
            return {"operation": operation, "keep_sources": keep_sources}
        def execute(doc, selected, before):
            originals = {str(doc.ModelSpace.Item(i).Handle).casefold() for i in range(doc.ModelSpace.Count)}
            left = _3d_copy(selected[0], originals)
            right = _3d_copy(selected[1], originals | {str(left.Handle).casefold()})
            right_handle = str(right.Handle)
            if getattr(left, "_oleobj_", None) is not None:
                # Explicit [out] Boolean works with both generated and DumbDispatch
                # objects after rewrapping via our existing cache-free strategy.
                from win32com.client import VARIANT
                import pythoncom
                flag = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BOOL, False)
                raw = _dynamic_cad_object(win32com.client, left._oleobj_)
                overlap_solid = _engineering_call(lambda: raw.CheckInterference(right, True, flag), "检查实体交集")
                response = [overlap_solid, bool(flag.value)]
            else:
                response = _engineering_call(lambda: left.CheckInterference(right, True), "检查实体交集")
            parts = response if isinstance(response, (tuple, list)) else [response]
            overlap = [x for x in parts if x is not None and not isinstance(x, (bool, int))]
            if any(isinstance(x, (bool, int)) and not x for x in parts) and overlap:
                raise RuntimeError("干涉返回值互相矛盾")
            if len(overlap) > 1:
                raise RuntimeError("无法解读CheckInterference的COM返回值")
            intersection_volume = 0.0
            if overlap:
                overlap_record = _solid_record(overlap[0])
                if overlap_record["handle"].casefold() in originals | {str(left.Handle).casefold(), right_handle.casefold()}:
                    raise RuntimeError("干涉检查未生成独立实体，拒绝删除任何源件")
                intersection_volume = overlap_record["volume"]
                _engineering_call(overlap[0].Delete, "清理干涉临时体")
            elif any(bool(x) for x in parts if isinstance(x, (bool, int))):
                raise RuntimeError("干涉为真但没有可核验的干涉实体")
            if intersection_volume > min(item["volume"] for item in before) * (1 + 1e-7):
                raise RuntimeError("干涉体积大于输入体积，拒绝保存")
            if operation != "union" and intersection_volume == 0:
                raise ValueError("两实体没有正体积交集，未执行差集或交集")
            a, b = [item["volume"] for item in before]
            expected = {"union": a+b-intersection_volume, "intersection": intersection_volume,
                        "subtract": a-intersection_volume}[operation]
            if not math.isfinite(expected) or expected <= 0:
                raise ValueError("布尔结果为空或体积无效，拒绝保存")
            # AcBooleanType: 0=acUnion, 1=acIntersection, 2=acSubtraction.
            _engineering_call(lambda: left.Boolean({"union": 0, "intersection": 1, "subtract": 2}[operation], right), "布尔运算")
            result = _solid_record(left)
            if not math.isclose(result["volume"], expected, rel_tol=1e-7, abs_tol=1e-9):
                raise RuntimeError("布尔结果体积与真实干涉不符，可能未执行，拒绝保存")
            _3d_remove_if_present(doc, right_handle)
            if not keep_sources:
                for entity in selected:
                    _engineering_call(entity.Delete, "删除布尔源件")
            return {"changed": 1, "added": 1, "erased": 0 if keep_sources else 2,
                    "entities": [result], "intersection_volume": intersection_volume}
        return _3d_selected_edit(path, [target_handle, tool_handle], prepare, execute, dry_run, overwrite)

    @_delivery_tool("cad_rotate_3d", "绕由axis_start/axis_end两个WCS点定义的任意轴批量旋转3DSolid，angle为弧度。"
                    "仅精确处理handles指定实体，原位旋转；验证体积保持不变。",
                    {"handles": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                     "axis_start": point_schema, "axis_end": point_schema, "angle": {"type": "number"}},
                    ["handles", "axis_start", "axis_end", "angle"])
    @_com_async
    def cad_rotate_3d(path: str, handles: list, axis_start: list, axis_end: list, angle: float,
                      dry_run: bool = True, overwrite: bool = False):
        a, b = _edit_point(axis_start, "axis_start"), _edit_point(axis_end, "axis_end")
        angle = _edit_number(angle, "angle")
        distance = math.dist(a, b)
        if distance <= 1e-12 or not math.isfinite(distance) or abs(math.remainder(angle, math.tau)) <= 1e-12:
            raise ValueError("旋转轴两点必须不同，角度不能是整圈或零")
        def prepare(doc, selected):
            for entity in selected:
                _engineering_method(entity, "Rotate3D")
            return {"axis_start": a, "axis_end": b, "angle": angle}
        def execute(doc, selected, before):
            results = []
            for entity, old in zip(selected, before):
                _engineering_call(lambda: entity.Rotate3D(_com_point(a), _com_point(b), angle), "Rotate3D")
                result = _solid_record(entity)
                if not math.isclose(result["volume"], old["volume"], rel_tol=1e-8, abs_tol=1e-9):
                    raise RuntimeError("旋转后体积改变，拒绝保存")
                results.append(result)
            return {"changed": len(results), "entities": results}
        return _3d_selected_edit(path, handles, prepare, execute, dry_run, overwrite)

    @_delivery_tool("cad_mirror_3d", "按三个非共线WCS点定义的平面镜像3DSolid；始终创建新副本并保留原件。",
                    {"handles": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                     "plane_p1": point_schema, "plane_p2": point_schema, "plane_p3": point_schema},
                    ["handles", "plane_p1", "plane_p2", "plane_p3"])
    @_com_async
    def cad_mirror_3d(path: str, handles: list, plane_p1: list, plane_p2: list, plane_p3: list,
                      dry_run: bool = True, overwrite: bool = False):
        points = _3d_plane(plane_p1, plane_p2, plane_p3)
        def prepare(doc, selected):
            for entity in selected:
                _engineering_method(entity, "Mirror3D")
            return {"plane": points, "preserve_sources": True}
        def execute(doc, selected, before):
            seen = {str(doc.ModelSpace.Item(i).Handle).casefold() for i in range(doc.ModelSpace.Count)}
            results = []
            for entity, old in zip(selected, before):
                copy = _engineering_call(lambda: entity.Mirror3D(*[_com_point(p) for p in points]), "Mirror3D")
                result = _solid_record(copy)
                if result["handle"].casefold() in seen or not math.isclose(result["volume"], old["volume"], rel_tol=1e-8, abs_tol=1e-9):
                    raise RuntimeError("镜像未返回独立等体积实体")
                seen.add(result["handle"].casefold())
                results.append(result)
            return {"added": len(results), "changed": len(results), "entities": results}
        return _3d_selected_edit(path, handles, prepare, execute, dry_run, overwrite)

    @_delivery_tool("cad_slice_solid", "按三个非共线WCS点定义的平面对单个3DSolid剖切。先在副本上剖成两半并验证体积守恒。"
                    "side=both/positive/negative选保留部分；正侧按三点右手法则法向。默认keep_source=true保留原件，false在核验成功后删除原件。",
                    {"handle": {"type": "string"}, "plane_p1": point_schema, "plane_p2": point_schema, "plane_p3": point_schema,
                     "side": {"type": "string", "enum": ["both", "positive", "negative"], "default": "both"},
                     "keep_source": {"type": "boolean", "default": True}}, ["handle", "plane_p1", "plane_p2", "plane_p3"])
    @_com_async
    def cad_slice_solid(path: str, handle: str, plane_p1: list, plane_p2: list, plane_p3: list,
                        side: str = "both", keep_source: bool = True, dry_run: bool = True, overwrite: bool = False):
        points = _3d_plane(plane_p1, plane_p2, plane_p3)
        if not isinstance(side, str) or side not in ("both", "positive", "negative") or type(keep_source) is not bool:
            raise ValueError("side必须为both/positive/negative，keep_source必须为布尔值")
        def prepare(doc, selected):
            _engineering_method(selected[0], "Copy")
            _engineering_method(selected[0], "SliceSolid")
            return {"plane": points, "side": side, "keep_source": keep_source}
        def execute(doc, selected, before):
            source = selected[0]
            seen = {str(doc.ModelSpace.Item(i).Handle).casefold() for i in range(doc.ModelSpace.Count)}
            positive = _3d_copy(source, seen)
            negative = _engineering_call(lambda: positive.SliceSolid(*[_com_point(p) for p in points], True), "SliceSolid")
            results = [_solid_record(positive), _solid_record(negative)]
            if (results[1]["handle"].casefold() in seen | {results[0]["handle"].casefold()}
                    or not math.isclose(sum(r["volume"] for r in results), before[0]["volume"], rel_tol=1e-7, abs_tol=1e-9)):
                raise RuntimeError("剖切未生成两件有效等总体积实体，拒绝保存")
            if side != "both":
                _engineering_call((negative if side == "positive" else positive).Delete, "删除未选剖切侧")
                results = [results[0 if side == "positive" else 1]]
            if not keep_source:
                _engineering_call(source.Delete, "删除剖切原件")
            return {"added": len(results), "changed": 1, "erased": int(not keep_source), "entities": results}
        return _3d_selected_edit(path, [handle], prepare, execute, dry_run, overwrite)

    # ---------- 批量尺寸标注：ActiveX 精确接口，不发送界面命令 ----------
    def _dimstyle(doc, name):
        _delivery_string(name, "style_name")
        for index in range(doc.DimStyles.Count):
            style = doc.DimStyles.Item(index)
            if str(style.Name).casefold() == name.casefold():
                return style
        raise ValueError(f"标注样式不存在:{name}；请先用 cad_list_dimstyles 查看或创建")

    # DimStyle has no TextHeight/ArrowheadSize setters. Copy a transient dimension's
    # style + overrides back into the named style; never change drawing DIM variables.
    _dim_properties = {
        "text_height": ("TextHeight", "positive"), "arrow_size": ("ArrowheadSize", "nonnegative"),
        "overall_scale": ("ScaleFactor", "positive"), "measurement_scale": ("LinearScaleFactor", "positive"),
        "text_gap": ("TextGap", "nonnegative"), "extension_offset": ("ExtensionLineOffset", "nonnegative"),
        "extension_extend": ("ExtensionLineExtend", "nonnegative"),
        "precision": ("PrimaryUnitsPrecision", 8),
        "text_color": ("TextColor", 256), "dimension_color": ("DimensionLineColor", 256),
        "extension_color": ("ExtensionLineColor", 256),
        "text_style": ("TextStyle", "text"), "prefix": ("TextPrefix", "text"), "suffix": ("TextSuffix", "text"),
    }

    def _dim_properties_input(properties):
        if not isinstance(properties, dict) or properties.keys() - _dim_properties.keys():
            raise ValueError("properties仅允许:" + ",".join(_dim_properties))
        values = {}
        for key, value in properties.items():
            attr, rule = _dim_properties[key]
            if isinstance(rule, int):
                if type(value) is not int or not 0 <= value <= rule:
                    raise ValueError(f"{key}必须为0～{rule}的整数")
            elif rule == "text":
                _delivery_string(value, key, empty=key != "text_style")
            else:
                value = _edit_number(value, key)
                if value < 0 or (rule == "positive" and value == 0):
                    raise ValueError(f"{key}必须为{'正数' if rule == 'positive' else '非负数'}")
            values[attr] = value
        return values

    def _dim_properties_schema():
        return {"type": "object", "additionalProperties": False, "properties": {
            key: ({"type": "integer", "minimum": 0, "maximum": rule} if isinstance(rule, int)
                  else {"type": "string"} if rule == "text"
                  else {"type": "number", "exclusiveMinimum": 0} if rule == "positive"
                  else {"type": "number", "minimum": 0})
            for key, (_, rule) in _dim_properties.items()}}

    def _check_dim_properties(doc, values):
        if "TextStyle" in values:
            names = [str(doc.TextStyles.Item(i).Name) for i in range(doc.TextStyles.Count)]
            match = next((name for name in names if name.casefold() == values["TextStyle"].casefold()), None)
            if match is None:
                raise ValueError(f"文字样式不存在:{values['TextStyle']}")
            values["TextStyle"] = match
        if values:
            _delivery_layer(doc, "")
            _engineering_method(doc.ModelSpace, "AddDimRotated")

    def _set_dim_properties(doc, style, values):
        if not values:
            return
        proxy = _engineering_call(lambda: doc.ModelSpace.AddDimRotated(
            _com_point([0, 0, 0]), _com_point([1, 0, 0]), _com_point([0, 1, 0]), 0.0), "创建临时标注样式代理")
        if proxy is None:
            raise RuntimeError("未创建临时标注，拒绝保存样式")
        try:
            proxy.StyleName = str(style.Name)
            if str(proxy.StyleName).casefold() != str(style.Name).casefold():
                raise RuntimeError("临时标注未采用目标样式，拒绝覆盖样式")
            for attr, value in values.items():
                _engineering_call(lambda attr=attr, value=value: setattr(proxy, attr, value), "设置标注样式 " + attr)
                actual = _engineering_call(lambda attr=attr: getattr(proxy, attr), "验证标注样式 " + attr)
                if (not math.isclose(actual, value, rel_tol=1e-9, abs_tol=1e-12)
                        if isinstance(value, float) else actual != value):
                    raise RuntimeError(f"样式属性{attr}写后读回不一致，拒绝保存")
            _engineering_call(lambda: style.CopyFrom(proxy), "保存标注样式设置")
        finally:
            _engineering_call(proxy.Delete, "删除临时标注样式代理")

    @registry.tool(
        name="cad_list_dimstyles",
        description="只读列出DWG已有标注样式名称及当前样式；不把文档DIM变量冒充各个样式设置。不需要launch_app或截屏点界面。",
        parameters={"type": "object", "properties": {"path": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_ENTITIES, "default": 100}}, "required": ["path"]},
    )
    @_com_async
    def cad_list_dimstyles(path: str, limit: int = 100):
        if type(limit) is not int or not 1 <= limit <= _MAX_ENTITIES:
            raise ValueError(f"limit必须为1～{_MAX_ENTITIES}的整数")
        p = _resolve(path)
        if not p.is_file():
            raise RuntimeError(f"图纸不存在:{p}")
        app, doc, own_doc, own_app = _open_cad_drawing(win32com.client, p)
        try:
            active = str(doc.ActiveDimStyle.Name)
            count = doc.DimStyles.Count
            return {"styles": [{"name": str(doc.DimStyles.Item(i).Name),
                                "active": str(doc.DimStyles.Item(i).Name).casefold() == active.casefold()}
                               for i in range(min(count, limit))], "active": active, "total": count,
                    "truncated": count > limit, "ok": True}
        finally:
            _release_cad_drawing(app, doc, own_doc, own_app)

    @_delivery_tool("cad_create_dimstyle", "创建命名标注样式，先复制base_style（留空复制当前命名样式，不带文档覆盖）。"
                    "可设置字高、箭头大小、总比例、测量比例、小数精度、颜色、文字样式、前后缀等；不改当前样式。"
                    "同名即拒绝；text_height可能受固定字高文字样式影响。",
                    {"style_name": {"type": "string"}, "base_style": {"type": "string"},
                     "properties": _dim_properties_schema()}, ["style_name"])
    @_com_async
    def cad_create_dimstyle(path: str, style_name: str, base_style: str = "", properties: dict | None = None,
                            dry_run: bool = True, overwrite: bool = False):
        _delivery_string(style_name, "style_name")
        _delivery_string(base_style, "base_style", empty=True)
        if len(style_name) > 255 or any(c in style_name for c in '<>/\\":;?*|=,`') or any(ord(c) < 32 for c in style_name):
            raise ValueError("style_name含非法字符或过长")
        values = _dim_properties_input({} if properties is None else properties)
        source = None
        def prepare(doc):
            nonlocal source
            if any(str(doc.DimStyles.Item(i).Name).casefold() == style_name.casefold() for i in range(doc.DimStyles.Count)):
                raise ValueError(f"标注样式已存在，拒绝覆盖:{style_name}")
            source = _dimstyle(doc, base_style) if base_style else doc.ActiveDimStyle
            _engineering_method(doc.DimStyles, "Add")
            _engineering_method(source, "CopyFrom")
            _check_dim_properties(doc, values)
            return {"style_name": style_name, "base_style": str(source.Name), "properties": properties or {}}
        def operation(doc):
            style = _engineering_call(lambda: doc.DimStyles.Add(style_name), "创建标注样式")
            _engineering_call(lambda: style.CopyFrom(source), "复制基础标注样式")
            _set_dim_properties(doc, style, values)
            return {"created": 1, "changed": 1, "style_name": str(style.Name)}
        return _delivery_modify(path, operation, prepare, dry_run, overwrite)

    @_delivery_tool("cad_update_dimstyle", "修改已有命名标注样式的白名单属性；已有使用此样式的标注可随样式更新，实体自身覆盖仍保留。"
                    "通过临时真实尺寸实体的CopyFrom写入样式，删除临时实体后保存，不改当前样式或DIM文档覆盖。",
                    {"style_name": {"type": "string"}, "properties": _dim_properties_schema()}, ["style_name", "properties"])
    @_com_async
    def cad_update_dimstyle(path: str, style_name: str, properties: dict, dry_run: bool = True, overwrite: bool = False):
        _delivery_string(style_name, "style_name")
        values = _dim_properties_input(properties)
        if not values:
            raise ValueError("properties不能为空")
        style = None
        def prepare(doc):
            nonlocal style
            style = _dimstyle(doc, style_name)
            _engineering_method(style, "CopyFrom")
            _check_dim_properties(doc, values)
            return {"style_name": str(style.Name), "properties": properties}
        def operation(doc):
            _set_dim_properties(doc, style, values)
            return {"changed": 1, "style_name": str(style.Name)}
        return _delivery_modify(path, operation, prepare, dry_run, overwrite)

    @_delivery_tool("cad_set_dimstyle", "设定图纸当前标注样式，影响后续新建标注；不批量替换已有标注。",
                    {"style_name": {"type": "string"}}, ["style_name"])
    @_com_async
    def cad_set_dimstyle(path: str, style_name: str, dry_run: bool = True, overwrite: bool = False):
        _delivery_string(style_name, "style_name")
        style = None
        def prepare(doc):
            nonlocal style
            style = _dimstyle(doc, style_name)
            return {"previous_style": str(doc.ActiveDimStyle.Name), "style_name": str(style.Name)}
        def operation(doc):
            doc.ActiveDimStyle = style
            if str(doc.ActiveDimStyle.Name).casefold() != str(style.Name).casefold():
                raise RuntimeError("当前标注样式写入失败，拒绝保存")
            return {"changed": 1, "style_name": str(style.Name)}
        return _delivery_modify(path, operation, prepare, dry_run, overwrite)

    @_edit_tool("cad_apply_dimstyle", "把style_name应用到筛选出的现有尺寸标注，保留几何及实体本身覆盖。"
                "必须提供layer/area/text之一，不无条件替换全图；不处理普通文字和引线。",
                {"style_name": {"type": "string"}}, ["style_name"])
    @_com_async
    def cad_apply_dimstyle(path: str, style_name: str, layer: str = "", area: Any = None, text: str = "", dry_run: bool = True):
        if not layer and area is None and not text:
            raise ValueError("至少提供layer/area/text之一，拒绝无筛选替换全图标注")
        _delivery_string(style_name, "style_name")
        name = ""
        def prepare(doc, selected):
            nonlocal name
            name = str(_dimstyle(doc, style_name).Name)
            return {"style_name": name}
        def apply(entity):
            _engineering_call(lambda: setattr(entity, "StyleName", name), "应用标注样式")
            if str(entity.StyleName).casefold() != name.casefold():
                raise RuntimeError("标注样式应用失败，拒绝保存")
        return _edit_entities(path, layer, "DIMENSION", area, text, dry_run, "apply_dimstyle",
                              {"style_name": style_name}, apply, prepare_selected=prepare)

    @registry.tool(
        name="cad_add_dimensions",
        description=(
            "批量加真实尺寸标注：linear/aligned/radius/angular/continuous/diameter/arc_length/ordinate/baseline。"
            "坐标为图纸单位、3D WCS点（二维点补z=0）；新增类型限同标高XY平面。"
            "diameter的p1/p2是直径两端，不是圆心；arc_length的center为圆心、p1/p2为圆弧两端、text_pos为标注弧上的点。"
            "ordinate用p1指定特征点、text_pos指定引线末点，axis为x/y，测量原点沿用图纸当前UCS，不伪造文字数值。"
            "baseline用p1作共同基准、points作各测点、text_pos作首条尺寸线位置、spacing作后续向外间距；rotation为弧度。"
            "基线仅创建线性共基准尺寸，不伪称角度基线或DIMBASELINE关联链；continuous保留逐段独立标注。"
            "每项可指定style_name和layer；默认保持原工具直接执行行为，dry_run=true只预览。"
            "先校验和备份，拒绝占用图纸/锁定图层，COM执行失败不保存。不需要launch_app或截屏点界面。"
        ),
        parameters={"type": "object", "properties": {
            "path": {"type": "string"}, "dry_run": {"type": "boolean", "default": False},
            "items": {"type": "array", "minItems": 1, "maxItems": _MAX_ENTITIES,
                      "items": {"type": "object", "properties": {
                "type": {"type": "string", "enum": ["linear", "aligned", "radius", "angular", "continuous",
                                                          "diameter", "arc_length", "ordinate", "baseline"]},
                "p1": point_schema, "p2": point_schema, "text_pos": point_schema,
                "center": point_schema, "vertex": point_schema,
                "points": {"type": "array", "items": point_schema, "minItems": 1, "maxItems": _MAX_ENTITIES},
                "rotation": {"type": "number", "default": 0},
                "spacing": {"type": "number", "exclusiveMinimum": 0},
                "axis": {"type": "string", "enum": ["x", "y"]},
                "leader_length": {"type": "number", "exclusiveMinimum": 0},
                "style_name": {"type": "string"}, "layer": {"type": "string"},
            }, "required": ["type", "p1"]}},
        }, "required": ["path", "items"]}, side_effect=True,
    )
    @_com_async
    def cad_add_dimensions(path: str, items: list, dry_run: bool = False) -> dict[str, Any]:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run 必须为布尔值")
        if not isinstance(items, list) or not 1 <= len(items) <= _MAX_ENTITIES:
            raise ValueError(f"items 必须为1～{_MAX_ENTITIES}项的数组")
        _resolve_for_write(path)
        plans, failed = [], []

        def prepare(doc):
            for index, item in enumerate(items):
                kind = item.get("type", "linear") if isinstance(item, dict) else ""
                try:
                    if not isinstance(item, dict):
                        raise ValueError("不是对象")
                    methods = {"linear": "AddDimRotated", "continuous": "AddDimRotated", "baseline": "AddDimRotated",
                               "aligned": "AddDimAligned", "radius": "AddDimRadial", "angular": "AddDimAngular",
                               "diameter": "AddDimDiametric", "arc_length": "AddDimArc", "ordinate": "AddDimOrdinate"}
                    if kind not in methods:
                        raise ValueError(f"不支持的标注类型:{kind}")
                    method = _engineering_method(doc.ModelSpace, methods[kind])
                    p1 = _edit_point(item.get("p1"), "p1")
                    p2 = _edit_point(item.get("p2"), "p2") if kind not in ("baseline", "ordinate") else None
                    pos = _edit_point(item.get("text_pos"), "text_pos") if item.get("text_pos") is not None else None
                    if kind not in ("radius", "diameter") and pos is None:
                        raise ValueError("此标注类型必须提供text_pos")
                    style = str(_dimstyle(doc, item["style_name"]).Name) if "style_name" in item else ""
                    layer = item.get("layer", "")
                    _delivery_string(layer, "layer", empty=True)
                    _delivery_layer(doc, layer)
                    calls = []
                    if kind in ("radius", "diameter"):
                        distance = math.dist(p1, p2)
                        if distance <= 1e-9:
                            raise ValueError("圆心/圆上点或直径两端不能重合")
                        if kind == "diameter" and (p1[2] != p2[2] or (pos is not None and pos[2] != p1[2])):
                            raise ValueError("直径标注仅支持同标高XY平面")
                        leader = _delivery_positive(item.get("leader_length", distance * 0.1), "leader_length")
                        calls.append((p1, p2, leader))
                    elif kind == "arc_length":
                        center = _edit_point(item.get("center"), "center")
                        radii = [math.dist(center, point) for point in (p1, p2)]
                        if (not all(math.isfinite(r) for r in radii)
                                or any(p[2] != center[2] for p in (p1, p2, pos)) or min(radii) <= 1e-9
                                or not math.isclose(*radii, rel_tol=1e-8, abs_tol=1e-8)
                                or math.dist(p1, p2) <= 1e-9 or math.dist(center, pos) <= 1e-9):
                            raise ValueError("弧长标注要求同标高、两端等半径且不重合，text_pos不能在圆心")
                        calls.append((center, p1, p2, pos))
                    elif kind == "ordinate":
                        axis = item.get("axis")
                        if axis not in ("x", "y") or pos[2] != p1[2] or math.dist(p1, pos) <= 1e-9:
                            raise ValueError("坐标标注axis必须为x/y，特征点和引线末点须同标高且不同")
                        calls.append((p1, pos, axis == "x"))
                    elif kind == "baseline":
                        points = item.get("points")
                        if not isinstance(points, list) or not 1 <= len(points) <= _MAX_ENTITIES:
                            raise ValueError("基线points必须为非空测点数组")
                        points = [_edit_point(point, "points") for point in points]
                        spacing = _delivery_positive(item.get("spacing"), "spacing")
                        rotation = _edit_number(item.get("rotation", 0), "rotation")
                        direction = [math.cos(rotation), math.sin(rotation)]
                        normal = [-direction[1], direction[0]]
                        projections = [sum((point[k] - p1[k]) * direction[k] for k in range(2)) for point in points]
                        if any(p[2] != p1[2] for p in [pos, *points]) or any(not math.isfinite(v) or abs(v) <= 1e-9 for v in projections):
                            raise ValueError("基线须同标高，测点在尺寸方向上的距离不能为零")
                        if any(math.isclose(a, b, abs_tol=1e-9) for i, a in enumerate(projections) for b in projections[:i]):
                            raise ValueError("基线测点投影重复")
                        outward = sum((pos[k] - p1[k]) * normal[k] for k in range(2))
                        if not math.isfinite(outward) or abs(outward) <= 1e-9:
                            raise ValueError("首条基线标注位置不能在测量基线上")
                        sign = 1 if outward > 0 else -1
                        for i, point in enumerate(points):
                            position = [pos[k] + sign * spacing * i * normal[k] for k in range(2)] + [p1[2]]
                            if not all(math.isfinite(v) for v in position):
                                raise ValueError("基线尺寸位置超出有限数值范围")
                            calls.append((p1, point, position, rotation))
                    elif kind == "aligned":
                        if math.dist(p1, p2) <= 1e-9:
                            raise ValueError("标注两端不能重合")
                        calls.append((p1, p2, pos))
                    elif kind == "angular":
                        vertex = _edit_point(item.get("vertex", item.get("p1")), "vertex")
                        # Autodesk allows AngleVertex to coincide with an endpoint.
                        calls.append((vertex, p1, p2, pos))
                    else:
                        rotation = _edit_number(item.get("rotation", 0), "rotation")
                        if abs((p2[0]-p1[0])*math.cos(rotation)+(p2[1]-p1[1])*math.sin(rotation)) <= 1e-9:
                            raise ValueError("尺寸方向投影长度不能为零")
                        calls.append((p1, p2, pos, rotation))
                    if sum(len(p["calls"]) for p in plans) + len(calls) > _MAX_ENTITIES:
                        raise ValueError(f"单次最多创建{_MAX_ENTITIES}条标注")
                    plans.append({"index": index, "type": kind, "method": method, "calls": calls,
                                  "layer": layer, "style_name": style, "text_pos": pos})
                except (ValueError, TypeError, OverflowError, RuntimeError) as exc:
                    failed.append({"index": index, "type": kind, "reason": str(exc)})
            return {"matched": len(plans), "planned": sum(len(p["calls"]) for p in plans), "failed": failed,
                    "preview": [{"index": p["index"], "type": p["type"], "count": len(p["calls"]),
                                 "style_name": p["style_name"], "layer": p["layer"],
                                 "arguments": p["calls"][:_MAX_DETAIL], "truncated": len(p["calls"]) > _MAX_DETAIL}
                                for p in plans[:_MAX_DETAIL]], "truncated": len(plans) > _MAX_DETAIL}

        def operation(doc):
            added = 0
            for plan in plans:
                for args in plan["calls"]:
                    converted = [_com_point(arg) if isinstance(arg, list) else arg for arg in args]
                    entity = _engineering_call(lambda: plan["method"](*converted), "创建" + plan["type"] + "标注")
                    if entity is None:
                        raise RuntimeError("COM未返回标注实体，拒绝保存")
                    if plan["style_name"]:
                        entity.StyleName = plan["style_name"]
                        if str(entity.StyleName).casefold() != plan["style_name"].casefold():
                            raise RuntimeError("标注样式应用失败，拒绝保存")
                    if plan["layer"]:
                        entity.Layer = plan["layer"]
                    if plan["type"] in ("radius", "diameter") and plan["text_pos"] is not None:
                        entity.TextPosition = _com_point(plan["text_pos"])
                    added += 1
            return {"added": added, "changed": added, "failed": failed, "ok": not failed}

        result = _modify_drawing(path, operation, preview=prepare, dry_run=dry_run)
        if not plans and failed:
            result["summary"] = "所有标注参数校验失败，未创建、备份或保存；请查看failed明细。"
        return {**result, "added": result.get("added", 0), "ok": not failed}


EXPOSED_TOOLS = [
    "cad_open_info", "cad_list_layers", "cad_read_entities", "cad_read_text",
    "cad_replace_text", "cad_create_layer", "cad_update_layer", "cad_add_text",
    "cad_add_mtext", "cad_draw_line", "cad_draw_polyline", "cad_draw_rectangle",
    "cad_draw_circle", "cad_draw_arc", "cad_draw_ellipse", "cad_draw_point",
    "cad_verify_drawing", "cad_add_dimensions",
    "cad_create_3d_solid", "cad_read_solids", "cad_extrude", "cad_revolve",
    "cad_boolean", "cad_rotate_3d", "cad_mirror_3d", "cad_slice_solid",
    "cad_list_dimstyles", "cad_create_dimstyle", "cad_update_dimstyle", "cad_set_dimstyle", "cad_apply_dimstyle",
    "cad_move_entities", "cad_copy_entities", "cad_rotate_entities", "cad_scale_entities",
    "cad_mirror_entities", "cad_erase_entities", "cad_move_entities_to_layer",
    "cad_change_entity_properties",
    "cad_offset", "cad_trim", "cad_extend", "cad_fillet", "cad_chamfer",
    "cad_join_entities", "cad_explode",
    "cad_hatch", "cad_add_leader", "cad_add_multileader", "cad_insert_block",
    "cad_add_attribute", "cad_read_blocks", "cad_save_as", "cad_export_dxf", "cad_export_pdf",
]
