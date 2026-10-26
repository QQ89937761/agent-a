"""CAD COM 模块测试:用假 win32com 注入,Linux CI 也能跑,不需要真 AutoCAD。

假对象刻意模仿几个真机行为,免得测试绿了真机还是崩:
- Application.Visible 一旦被赋值就抛错(AutoCAD 真的不让设,Word/Excel 才让)。
- DispatchEx 直接失败(我们要的是 Dispatch,复用已开实例)。
- GetVariable("EXTMIN") 返回元组(COM 里是 SAFEARRAY),用来钉死 JSON 可序列化。
"""
from __future__ import annotations

import copy
import json
import math
import sys
import struct
import unittest
import zlib
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest import mock

from backend.core.tools import ToolRegistry
from backend.modules.cad import cad_module

_BUSY = RuntimeError("Call was rejected by callee. (-2147418111)")


def _dwg_file(folder: Path, name: str = "施工图.dwg", header: bytes = b"AC1027") -> Path:
    """造一份带真实 DWG 版本头的假图纸文件。"""
    path = folder / name
    path.write_bytes(header + b"\x00" * 32)
    return path


class _FakeVariant:
    """假 win32com.client.VARIANT:记下 vt 和 value,好断言点参数被正确包成 VARIANT。"""
    def __init__(self, vt, value):
        self.vt = vt
        self.value = value


def _unwrap(value):
    """把 VARIANT/元组都退成纯 tuple,供旧断言和 _CadEntity 使用。"""
    if isinstance(value, _FakeVariant):
        return tuple(value.value)
    return tuple(value)


class _CadEntity:
    def __init__(self, object_name, layer="0", text="", position=(0.0, 0.0), name="",
                 bbox=None):
        self.ObjectName = object_name
        self.Layer = layer
        self.TextString = text
        self.InsertionPoint = tuple(position)
        self.StartPoint = tuple(position)
        self.EndPoint = tuple(position)
        self.TextOverride = ""
        self.Measurement = 0.0
        self.Rotation = 0.0
        self._bbox = bbox
        if name:
            self.Name = name

    def GetBoundingBox(self):
        # 真机上这两个是 [out] 参数,pywin32 晚绑定下以元组形式返回。
        if self._bbox is None:
            raise RuntimeError("该实体不支持 GetBoundingBox")
        (x1, y1), (x2, y2) = self._bbox
        return (x1, y1, 0.0), (x2, y2, 0.0)



class _CadMText(_CadEntity):
    """ActiveX MText exposes Height, NOT TextHeight; forbid permissive fake setters."""
    def __setattr__(self, name, value):
        if name == "TextHeight":
            raise AttributeError("AcadMText.TextHeight does not exist; use Height")
        super().__setattr__(name, value)


class _CadEditableEntity(_CadEntity):
    """Mutable fake: copies/deletes alter Count, catching unsafe live iteration."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.Color = 256
        self.Linetype = "ByLayer"
        self.LinetypeScale = 1.0
        self.Lineweight = -1
        self.Visible = True
        self.Handle = "A1"
        self.calls = []
        self._space = None

    def _transform(self, fn):
        for attr in ("InsertionPoint", "StartPoint", "EndPoint"):
            value = list(getattr(self, attr))
            value += [0.0] * (3 - len(value))
            setattr(self, attr, tuple(fn(value)))

    def Move(self, start, end):
        self.calls.append(("Move", start, end))
        offset = [b - a for a, b in zip(_unwrap(start), _unwrap(end))]
        self._transform(lambda p: [p[i] + offset[i] for i in range(3)])

    def _copy(self):
        entity = copy.copy(self)
        entity.calls = []
        entity.Handle = f"C{self._space.Count}"
        self._space.entities.append(entity)
        return entity

    def Copy(self):
        self.calls.append(("Copy",))
        return self._copy()

    def Rotate(self, base, angle):
        self.calls.append(("Rotate", base, angle))
        x, y, _ = _unwrap(base)
        c, s = math.cos(angle), math.sin(angle)
        self._transform(lambda p: [x + (p[0] - x) * c - (p[1] - y) * s,
                                   y + (p[0] - x) * s + (p[1] - y) * c, p[2]])

    def ScaleEntity(self, base, factor):
        self.calls.append(("ScaleEntity", base, factor))
        origin = _unwrap(base)
        self._transform(lambda p: [origin[i] + factor * (p[i] - origin[i]) for i in range(3)])

    def Mirror(self, p1, p2):
        self.calls.append(("Mirror", p1, p2))
        x, y, _ = _unwrap(p1)
        end = _unwrap(p2)
        dx, dy = end[0] - x, end[1] - y
        length_sq = dx * dx + dy * dy
        def reflect(p):
            projection = ((p[0] - x) * dx + (p[1] - y) * dy) / length_sq
            return [2 * (x + projection * dx) - p[0], 2 * (y + projection * dy) - p[1], p[2]]
        entity = self._copy()
        entity._transform(reflect)
        return entity

    def Delete(self):
        self.calls.append(("Delete",))
        self._space.entities.remove(self)

class _CadPolyline:
    """多段线:没有 InsertionPoint/StartPoint/Center,几何全在 Coordinates 里。

    这正是真机上"只返回占位坐标 [0,0]"的成因——旧代码只认那几个锚点属性。
    """

    def __init__(self, coordinates, layer="0", closed=True, stride=2,
                 object_name="AcDbPolyline", length=None, area=None):
        self.ObjectName = object_name
        self.Layer = layer
        self.Coordinates = tuple(coordinates)
        self.Closed = closed
        self._stride = stride
        if length is not None:
            self.Length = length
        if area is not None:
            self.Area = area

    def GetBoundingBox(self):
        flat = list(self.Coordinates)
        xs = flat[0::self._stride]
        ys = flat[1::self._stride]
        return (min(xs), min(ys), 0.0), (max(xs), max(ys), 0.0)


class _CadLayer:
    def __init__(self, name, color=7, linetype="Continuous", on=True, frozen=False,
                 locked=False, drop=()):
        self.Name = name
        self.Color = color
        self.Linetype = linetype
        self.Lineweight = -3
        self.LayerOn = on
        self.Freeze = frozen
        self.Lock = locked
        for attr in drop:  # 模拟某些 AutoCAD 版本上不存在的属性
            delattr(self, attr)


class _CadBlock:
    def __init__(self, name, count=0):
        self.Name = name
        self.Count = count


class _CadStyle:
    def __init__(self, name):
        self.Name = name


class _CadCollection:
    def __init__(self, items):
        self.items = list(items)

    @property
    def Count(self):
        return len(self.items)

    def Item(self, index):
        return self.items[index]

    def Add(self, name):
        layer = _CadLayer(name)
        self.items.append(layer)
        return layer


class _CadModelSpace:
    def __init__(self, entities):
        self.entities = list(entities)
        self.added = []
        self.dim_calls = []        # 旧格式(unwrap 成 tuple),兼容已有测试
        self.add_text_calls = []   # (text, position, height) 原始参数
        self.add_circle_calls = [] # (center, radius) 原始参数
        self.add_dim_calls = []    # 原始参数(含 VARIANT),供 40.2 断言
        self.add_line_calls = []
        self.add_polyline_calls = []
        self.add_polyline_raw_calls = []
        self.add_mtext_calls = []
        self.add_arc_calls = []
        self.add_ellipse_calls = []
        self.add_point_calls = []

    @property
    def Count(self):
        return len(self.entities) + len(self.added)

    def Item(self, index):
        return (self.entities + self.added)[index]

    def AddText(self, text, position, height):
        self.add_text_calls.append((text, position, height))
        entity = _CadEntity("AcDbText", text=text, position=_unwrap(position))
        self.added.append(entity)
        return entity

    def AddCircle(self, center, radius):
        self.add_circle_calls.append((center, radius))
        entity = _CadEntity("AcDbCircle", position=_unwrap(center))
        entity.Radius = radius
        self.added.append(entity)
        return entity

    def AddLine(self, start, end):
        self.add_line_calls.append((start, end))
        entity = _CadEntity("AcDbLine", position=_unwrap(start))
        entity.StartPoint = _unwrap(start)
        entity.EndPoint = _unwrap(end)
        self.added.append(entity)
        return entity

    def AddLightWeightPolyline(self, points):
        self.add_polyline_raw_calls.append(points)
        points = tuple(_unwrap(points))
        self.add_polyline_calls.append(points)
        entity = _CadPolyline(points, closed=False)
        self.added.append(entity)
        return entity

    def AddMText(self, insertion, width, text):
        self.add_mtext_calls.append((insertion, width, text))
        entity = _CadMText("AcDbMText", text=text, position=_unwrap(insertion))
        entity.Width = width
        entity.Height = 2.5
        self.added.append(entity)
        return entity

    def AddArc(self, center, radius, start_angle, end_angle):
        self.add_arc_calls.append((center, radius, start_angle, end_angle))
        entity = _CadEntity("AcDbArc", position=_unwrap(center))
        entity.Radius = radius
        entity.StartAngle = start_angle
        entity.EndAngle = end_angle
        self.added.append(entity)
        return entity

    def AddEllipse(self, center, major_axis, ratio):
        self.add_ellipse_calls.append((center, major_axis, ratio))
        entity = _CadEntity("AcDbEllipse", position=_unwrap(center))
        entity.Ratio = ratio
        self.added.append(entity)
        return entity

    def AddPoint(self, point):
        self.add_point_calls.append(point)
        entity = _CadEntity("AcDbPoint", position=_unwrap(point))
        self.added.append(entity)
        return entity

    def AddDimRotated(self, p1, p2, pos, rot):
        self.add_dim_calls.append(("rotated", p1, p2, pos, rot))
        self.dim_calls.append(("rotated", _unwrap(p1), _unwrap(pos), rot))
        entity = _CadEntity("AcDbRotatedDimension", position=_unwrap(pos))
        self.added.append(entity)
        return entity

    def AddDimAligned(self, p1, p2, pos):
        self.add_dim_calls.append(("aligned", p1, p2, pos))
        self.dim_calls.append(("aligned",))
        entity = _CadEntity("AcDbAlignedDimension", position=_unwrap(pos))
        self.added.append(entity)
        return entity

    def AddDimRadial(self, center, chord, leader):
        self.add_dim_calls.append(("radial", center, chord, leader))
        if not leader:  # 真机上 LeaderLength=0 会被拒
            raise RuntimeError("无效的 LeaderLength")
        self.dim_calls.append(("radial", leader))
        entity = _CadEntity("AcDbRadialDimension", position=_unwrap(center))
        self.added.append(entity)
        return entity

    def AddDimAngular(self, vertex, p1, p2, pos):
        self.add_dim_calls.append(("angular", vertex, p1, p2, pos))
        self.dim_calls.append(("angular",))
        entity = _CadEntity("AcDbAngularDimension", position=_unwrap(pos))
        self.added.append(entity)
        return entity


class _CadDocument:
    def __init__(self, full_name, modelspace=None, variables=None, layers=None,
                 blocks=None, saveas_error=None):
        self.FullName = full_name
        self.ModelSpace = _CadModelSpace(modelspace or [])
        self.PaperSpace = _CadModelSpace([])
        self.Layers = _CadCollection(layers if layers is not None else [_CadLayer(name="0")])
        self.Blocks = _CadCollection(blocks if blocks is not None else [])
        self.DimStyles = _CadCollection([_CadStyle("ISO-25")])
        self.ActiveDimStyle = self.DimStyles.Item(0)
        self.ActiveLayer = self.Layers.Item(0)
        self.TextStyles = _CadCollection([_CadStyle("Standard")])
        # EXTMIN/EXTMAX 是元组,和 COM 的 SAFEARRAY 一样不能直接进 JSON
        self._variables = {"INSUNITS": 4, "EXTMIN": (0.0, 0.0, 0.0),
                           "EXTMAX": (42000.0, 29700.0, 0.0)}
        self._variables.update(variables or {})
        self.saveas_error = saveas_error
        self.saved_as = []
        self.save_called = 0
        self.closed = 0
        self._documents = None

    def GetVariable(self, name):
        return self._variables.get(name)

    def SaveAs(self, path, version=None):
        if self.saveas_error is not None:
            raise self.saveas_error
        self.saved_as.append((str(path), version))

    def Save(self):
        self.save_called += 1

    def Close(self, save_changes):
        del save_changes
        self.closed += 1
        if self._documents is not None and self in self._documents.docs:
            self._documents.docs.remove(self)


class _NoNameDocument(_CadDocument):
    """从未存盘的新图:读 FullName 会抛错。"""

    @property
    def FullName(self):
        raise RuntimeError("尚未保存")

    @FullName.setter
    def FullName(self, value):
        del value


class _CadDocuments:
    def __init__(self, docs=(), open_result=None, open_error=None, open_error_times=0,
                 count_error=None, lock_on_open=False):
        self.docs = [d for d in docs if d is not None]
        for doc in self.docs:
            doc._documents = self
        self.open_result = open_result
        self.open_error = open_error
        self.open_error_times = open_error_times
        self.count_error = count_error
        # 真机行为:AutoCAD 打开图纸就会在同目录生成 .dwl 锁文件。开着它才能测出
        # "锁快照拍晚了会把自己的锁当成别人的"这个 bug。
        self.lock_on_open = lock_on_open
        self.open_calls = []

    @property
    def Count(self):
        if self.count_error is not None:
            raise self.count_error
        return len(self.docs)

    def Item(self, index):
        return self.docs[index]

    def Add(self):
        doc = self.open_result if self.open_result is not None else _CadDocument("")
        doc._documents = self
        self.docs.append(doc)
        return doc

    def Open(self, path):
        self.open_calls.append(str(path))
        if self.open_error is not None and len(self.open_calls) <= self.open_error_times:
            raise self.open_error
        if self.lock_on_open:
            Path(str(path)).with_suffix(".dwl").write_text("AutoCAD 打开图纸产生的锁")
        doc = self.open_result if self.open_result is not None else _CadDocument(str(path))
        doc._documents = self
        self.docs.append(doc)
        return doc


class _StaleCadDocuments(_CadDocuments):
    """AutoCAD 2014: startup drawing remains in Count, but not IEnumVARIANT."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.enum = SimpleNamespace(QueryInterface=mock.Mock(side_effect=lambda _iid: iter(self.docs)))
        self._oleobj_ = SimpleNamespace(InvokeTypes=mock.Mock(return_value=self.enum))

    @property
    def Count(self):
        return len(self.docs) + 1

    def Item(self, index):
        raise RuntimeError("stale Documents.Item must not be used")


class _DocumentsWithFailingItem:
    """40.9 假件:Documents.Count 可取,但 Item/Open 抛 AttributeError。

    模拟"对象模型只坏了一半"的真机形态——用于区分 _scan 里的 Item 错误
    和 .Open 错误,证明错误从 Item 传播、没被吞掉改走 Open。
    """
    @property
    def Count(self):
        return 1  # 可取,所以 _resolve_cad_app 会判定 late 可用

    def Item(self, index):
        del index
        raise AttributeError("AutoCAD.Application.Documents.Item")

    def Open(self, path):
        del path
        raise AttributeError("AutoCAD.Application.Documents.Open")


class _CadApplication:
    def __init__(self, docs=(), open_result=None, **kwargs):
        self.Documents = _CadDocuments(docs=docs, open_result=open_result, **kwargs)
        self.quit_called = False

    def __setattr__(self, name, value):
        # 真机行为:AutoCAD 的 Application.Visible 不接受赋值。
        if name == "Visible":
            raise RuntimeError("AutoCAD: 不支持设置 Visible")
        super().__setattr__(name, value)

    def Quit(self):
        self.quit_called = True


class _NoDocumentsApp:
    """模拟真机:GetActiveObject/Dispatch 拿到的对象,取 .Documents 抛 AttributeError。

    真机抛的是 pywin32 晚绑定的固定格式 <对象名>.<属性名>,不是 com_error。
    可选带 .Application 回到真正的 Application 对象(覆盖 ROT 里挂的是 Document 的情况)。
    """

    def __init__(self, application=None):
        self._application = application
        self.quit_called = False
        self.documents_accesses = 0

    @property
    def Application(self):
        return self._application  # 可能是 None

    def __getattr__(self, name):
        if name == "Documents":
            self.documents_accesses += 1
        raise AttributeError(f"AutoCAD.Application.{name}")

    def Quit(self):
        self.quit_called = True


class _BusyStartupApp:
    """44.x 冷启动假件:Documents.Count 拒 reject_times 次(None=永远忙)后正常。

    模拟真机:Dispatch 起一个新实例,它启动期头几秒一直拒调用(RPC_E_CALL_REJECTED)。
    Application 返回自身,Documents 也返回自身,Open 返回指定的 doc。
    """
    def __init__(self, reject_times, open_doc=None):
        self.reject_times = reject_times  # None = 永远忙
        self.count_calls = 0
        self.quit_called = False
        self.open_doc = open_doc

    @property
    def Application(self):
        return self

    @property
    def Documents(self):
        return self

    @property
    def Count(self):
        self.count_calls += 1
        if self.reject_times is None or self.count_calls <= self.reject_times:
            raise _BUSY
        return 0

    def Item(self, index):
        raise IndexError(index)

    def Open(self, path):
        return self.open_doc

    def Quit(self):
        self.quit_called = True


class _FakeGenCache:
    def __init__(self, client):
        self.client = client

    def GetGeneratePath(self):
        return self.client.gen_py_path

    def Rebuild(self):
        self.client.rebuild_calls += 1

    def EnsureDispatch(self, progid):
        del progid
        self.client.ensure_calls += 1
        if self.client.early_error is not None:
            raise self.client.early_error
        # 真机语义:EnsureDispatch 新起的实例也进 ROT。返回 None(不适用)时不覆盖 active。
        if self.client.early_app is not None:
            self.client.active = self.client.early_app
        return self.client.early_app


class _CadClient:
    def __init__(self, active=None, dispatch_doc=None, dispatch_error=None,
                 lock_on_open=False, early_app=None, early_error=None,
                 gen_py_path="/tmp/gen_py", dispatch_result=None,
                 dispatch_results=None):
        self.active = active
        self.dispatch_doc = dispatch_doc
        self.dispatch_error = dispatch_error
        self.lock_on_open = lock_on_open
        self.dispatch_app = None
        self.dispatched = 0
        # 39.1 策略 C/D 用
        self.early_app = early_app
        self.early_error = early_error
        self.gen_py_path = gen_py_path
        self.rebuild_calls = 0
        self.ensure_calls = 0
        self.gencache = _FakeGenCache(self)
        # 40.7:真机上 Dispatch 会附着到已开实例(GetActiveObject 与 Dispatch 返回
        # 同一对象)。dispatch_result 让假件如实模拟"附着"语义,而不是新起进程。
        self.dispatch_result = dispatch_result
        # 42.9:按顺序返回多个 Dispatch 结果,每个都进 ROT(late 先起坏实例,
        # rebuild 清缓存后再取可用实例)。
        self.dispatch_results = list(dispatch_results) if dispatch_results else None

    def GetActiveObject(self, progid):
        if progid != "AutoCAD.Application":
            raise AssertionError(progid)
        if self.active is None:
            raise RuntimeError("no active AutoCAD.Application")
        return self.active

    def Dispatch(self, progid):
        if progid != "AutoCAD.Application":
            raise AssertionError(progid)
        if self.dispatch_error is not None:
            raise self.dispatch_error
        self.dispatched += 1
        if self.dispatch_results is not None and self.dispatch_results:
            result = self.dispatch_results.pop(0)
            self.active = result  # 新起的实例进 ROT
            return result
        if self.dispatch_result is not None:
            return self.dispatch_result  # 附着到已开实例(如实模拟真机语义)
        self.dispatch_app = _CadApplication(
            open_result=self.dispatch_doc, lock_on_open=self.lock_on_open,
        )
        # 真机语义:Dispatch 在没有实例时会新起一个,起完它就进了 ROT,
        # 之后 GetActiveObject 就能拿到它(这正是 41.8 快照时机要防的)。
        self.active = self.dispatch_app
        return self.dispatch_app

    def DispatchEx(self, progid):
        # 用 DispatchEx 会强开新进程,和用户已开的 AutoCAD 抢文件锁。
        raise AssertionError("不该再用 DispatchEx,应当用 Dispatch 复用已开实例")


@contextmanager
def _registered_tools(client):
    client_module = ModuleType("win32com.client")
    client_module.GetActiveObject = client.GetActiveObject
    client_module.Dispatch = client.Dispatch
    client_module.DispatchEx = client.DispatchEx
    client_module.gencache = client.gencache
    client_module.VARIANT = _FakeVariant
    win32com_module = ModuleType("win32com")
    win32com_module.__path__ = []
    win32com_module.client = client_module
    pythoncom_module = ModuleType("pythoncom")
    pythoncom_module.CoInitialize = lambda: None
    pythoncom_module.CoUninitialize = lambda: None
    pythoncom_module.VT_ARRAY = 0x2000
    pythoncom_module.VT_R8 = 5
    pythoncom_module.VT_BYREF = 0x4000
    pythoncom_module.VT_BOOL = 11
    pythoncom_module.VT_VARIANT = 12
    pythoncom_module.VT_DISPATCH = 9
    pythoncom_module.DISPID_NEWENUM = -4
    pythoncom_module.DISPATCH_METHOD = 1
    pythoncom_module.DISPATCH_PROPERTYGET = 2
    pythoncom_module.IID_IEnumVARIANT = "IEnumVARIANT"
    modules = {
        "win32com": win32com_module,
        "win32com.client": client_module,
        "pythoncom": pythoncom_module,
    }
    with mock.patch.dict(sys.modules, modules), \
         mock.patch.object(cad_module, "_com_available", return_value=True), \
         mock.patch.object(cad_module, "_COM_RETRY_SLEEP", 0):
        cad_module._RESOLVED_STRATEGY = None  # 每个测试都从空缓存开始
        registry = ToolRegistry()
        cad_module.register_tools(registry)
        yield registry


class CadAvailabilityTests(unittest.TestCase):
    def test_register_skips_when_com_unavailable(self):
        registry = ToolRegistry()
        with mock.patch.object(cad_module, "_com_available", return_value=False):
            cad_module.register_tools(registry)
        self.assertEqual([], registry.names())

    def test_all_cad_tools_registered(self):
        client = _CadClient(active=None)
        with _registered_tools(client) as registry:
            names = set(registry.names())
        self.assertEqual(set(cad_module.EXPOSED_TOOLS), names)
        self.assertEqual(55, len(cad_module.EXPOSED_TOOLS))
        self.assertEqual(55, len(names))


class CadOwnershipTests(unittest.TestCase):
    def test_verified_task_cad_is_retained_and_unverified_pid_uses_normal_cleanup(self):
        app = _CadApplication()
        app.HWND = 55
        process = SimpleNamespace(GetWindowThreadProcessId=lambda _hwnd: (1, 42))
        token = cad_module.system_control.set_foreground_notice_callback(None, on_escape=lambda: None)
        try:
            with mock.patch.dict(sys.modules, {"win32process": process}):
                self.assertFalse(cad_module._retain_task_cad(app, True))
                self.assertEqual(42, cad_module.system_control.owned_application_pid("autocad"))
                self.assertTrue(cad_module._is_task_cad(app))
                cad_module._release_cad_drawing(app, None, False, False)
                self.assertFalse(app.quit_called)
                process.GetWindowThreadProcessId = lambda _hwnd: (1, 99)
                self.assertFalse(cad_module._is_task_cad(app))
                cad_module.system_control.clear_owned_application("autocad")
                self.assertFalse(cad_module._retain_task_cad(app, False, {42}))
                self.assertEqual(99, cad_module.system_control.owned_application_pid("autocad"))
                cad_module.system_control.clear_owned_application("autocad")
                self.assertFalse(cad_module._retain_task_cad(app, True, {99}))
                self.assertFalse(cad_module.system_control.owned_application("autocad"))
        finally:
            cad_module.system_control.reset_foreground_notice_callback(token)

    def test_probe_cleanup_with_running_snapshot_only_quits_new_verified_pid(self):
        for pid, closed in ((42, False), (99, True)):
            app = _CadApplication()
            app.HWND = 55
            client = _CadClient(active=app)
            process = SimpleNamespace(GetWindowThreadProcessId=lambda _hwnd: (1, pid))
            with self.subTest(pid=pid), mock.patch.dict(sys.modules, {"win32process": process}):
                cad_module._cleanup_started_instance(client, True, {42})
            self.assertEqual(closed, app.quit_called)

    def test_dispatch_fallback_ownership_requires_new_pid(self):
        for strategy in ("late", "application"):
            for pid, owned in ((42, False), (99, True)):
                app = _CadApplication()
                app.HWND = 55
                app.Application = app
                client = _CadClient(active=None, dispatch_result=app)
                with self.subTest(strategy=strategy, pid=pid), \
                     mock.patch.object(cad_module.system_control, "_process_ids_by_name", return_value={42}), \
                     mock.patch.dict(sys.modules, {"win32process": SimpleNamespace(
                         GetWindowThreadProcessId=lambda _hwnd: (1, pid))}):
                    got, own, error, _ = cad_module._run_strategy(client, strategy, True, True)
                self.assertIs(app, got)
                self.assertIsNone(error)
                self.assertEqual(owned, own)

            app = _CadApplication()
            app.HWND = 55
            app.Application = app
            client = _CadClient(active=None, dispatch_result=app)
            with self.subTest(strategy=strategy, pid="unavailable"), \
                 mock.patch.object(cad_module.system_control, "_process_ids_by_name", return_value={42}), \
                 mock.patch.dict(sys.modules, {"win32process": SimpleNamespace(
                     GetWindowThreadProcessId=mock.Mock(side_effect=OSError("access denied")))}):
                got, own, error, _ = cad_module._run_strategy(client, strategy, True, True)
            self.assertIs(app, got)
            self.assertIsNone(error)
            self.assertFalse(own, "Unreadable PID must not grant ownership based on Dispatch alone")

    def test_cold_start_budget_follows_dispatch_fallback_even_with_running_snapshot(self):
        app = _CadApplication()
        client = _CadClient(active=None, dispatch_result=app)
        with mock.patch.object(cad_module, "_app_usable", return_value=(True, "ok")) as usable:
            got, _, error, _ = cad_module._run_strategy(client, "late", True, True)
        self.assertIs(app, got)
        self.assertIsNone(error)
        usable.assert_called_once_with(app, cold_start=True)

    def test_documents_missing_attribute_is_retried_but_not_permanent_failure(self):
        documents = _CadDocuments()
        with mock.patch("time.sleep"):
            class StartingApp:
                attempts = 0
                @property
                def Documents(self):
                    self.attempts += 1
                    if self.attempts == 1:
                        raise AttributeError("<unknown>.Documents")
                    return documents
            starting = StartingApp()
            self.assertEqual([], cad_module._cad_documents(starting))
            self.assertEqual(2, starting.attempts)

    def test_task_close_keeps_user_autocad_untouched(self):
        with mock.patch.object(cad_module.system_control, "owned_application", return_value=False), \
             mock.patch.dict(sys.modules, {"pythoncom": mock.Mock(), "win32com": mock.Mock()}):
            self.assertFalse(cad_module._close_task_owned_autocad())

    def test_task_close_refuses_unsaved_owned_drawing(self):
        app = SimpleNamespace(
            HWND=55,
            Documents=SimpleNamespace(Count=1, Item=lambda _index: SimpleNamespace(Saved=False)),
            Quit=mock.Mock(),
        )
        win32com = mock.Mock()
        win32com.client.GetActiveObject.return_value = app
        win32process = mock.Mock()
        win32process.GetWindowThreadProcessId.return_value = (1, 42)
        with mock.patch.object(cad_module.system_control, "owned_application", return_value=True), \
             mock.patch.object(cad_module.system_control, "owned_application_pid", return_value=42), \
             mock.patch.object(cad_module.system_control, "clear_owned_application") as clear, \
             mock.patch.dict(sys.modules, {"pythoncom": mock.Mock(), "win32com": win32com,
                                           "win32com.client": win32com.client, "win32process": win32process}):
            self.assertFalse(cad_module._close_task_owned_autocad())
        app.Quit.assert_not_called()
        clear.assert_not_called()

    def test_task_close_preserves_unsaved_empty_default_drawing(self):
        doc = SimpleNamespace(
            Saved=False, FullName="", ModelSpace=SimpleNamespace(Count=0),
            PaperSpace=SimpleNamespace(Count=0), Close=mock.Mock(),
        )
        app = SimpleNamespace(
            HWND=55, Documents=SimpleNamespace(Count=1, Item=lambda _index: doc), Quit=mock.Mock(),
        )
        win32com = mock.Mock()
        win32com.client.GetActiveObject.return_value = app
        win32process = mock.Mock()
        win32process.GetWindowThreadProcessId.return_value = (1, 42)
        with mock.patch.object(cad_module.system_control, "owned_application", return_value=True), \
             mock.patch.object(cad_module.system_control, "owned_application_pid", return_value=42), \
             mock.patch.object(cad_module.system_control, "clear_owned_application") as clear, \
             mock.patch.dict(sys.modules, {"pythoncom": mock.Mock(), "win32com": win32com,
                                           "win32com.client": win32com.client, "win32process": win32process}):
            self.assertFalse(cad_module._close_task_owned_autocad())
        doc.Close.assert_not_called()
        app.Quit.assert_not_called()
        clear.assert_not_called()

    def test_task_close_refuses_unmatched_autocad_process(self):
        app = SimpleNamespace(HWND=55, Documents=SimpleNamespace(Count=0), Quit=mock.Mock())
        win32com = mock.Mock()
        win32com.client.GetActiveObject.return_value = app
        win32process = mock.Mock()
        win32process.GetWindowThreadProcessId.return_value = (1, 99)
        with mock.patch.object(cad_module.system_control, "owned_application", return_value=True), \
             mock.patch.object(cad_module.system_control, "owned_application_pid", return_value=42), \
             mock.patch.dict(sys.modules, {"pythoncom": mock.Mock(), "win32com": win32com,
                                           "win32com.client": win32com.client, "win32process": win32process}):
            self.assertFalse(cad_module._close_task_owned_autocad())
        app.Quit.assert_not_called()

    def test_com_document_enumerator_ignores_stale_count_and_preserves_errors(self):
        doc = _CadDocument("user.dwg")
        app = _CadApplication()
        app.Documents = _StaleCadDocuments(docs=[doc])
        client = _CadClient(active=app)
        with _registered_tools(client), \
             mock.patch.object(cad_module, "_dynamic_cad_object", side_effect=lambda _client, raw: raw) as wrap:
            self.assertEqual([doc], cad_module._cad_documents(app))
            wrap.assert_called_once_with(sys.modules["win32com.client"], doc)
            app.Documents._oleobj_.InvokeTypes.assert_called_once_with(-4, 0, 3, (13, 10), ())
            app.Documents.enum.QueryInterface.assert_called_once_with("IEnumVARIANT")
            app.Documents.enum.QueryInterface.side_effect = RuntimeError("enumeration failed")
            with self.assertRaisesRegex(RuntimeError, "enumeration failed"):
                cad_module._cad_documents(app)

    def test_task_close_uses_live_enumerator_and_keeps_unsaved_drawings(self):
        for saved in (True, False):
            doc = _CadDocument("task.dwg")
            doc.Saved = saved
            app = _CadApplication()
            app.HWND = 55
            app.Documents = _StaleCadDocuments(docs=[doc])
            client = _CadClient(active=app)
            win32process = SimpleNamespace(GetWindowThreadProcessId=lambda _hwnd: (1, 42))
            with self.subTest(saved=saved), _registered_tools(client), \
                 mock.patch.dict(sys.modules, {"win32process": win32process}), \
                 mock.patch.object(cad_module, "_dynamic_cad_object", side_effect=lambda _client, raw: raw), \
                 mock.patch.object(cad_module.system_control, "owned_application", return_value=True), \
                 mock.patch.object(cad_module.system_control, "owned_application_pid", return_value=42), \
                 mock.patch.object(cad_module.system_control, "clear_owned_application") as clear:
                self.assertEqual(saved, cad_module._close_task_owned_autocad())
            self.assertEqual(saved, app.quit_called)
            self.assertEqual(int(saved), clear.call_count)
            self.assertEqual(0, doc.closed)

    def test_reusing_open_drawing_never_closes_it(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path))
            app = _CadApplication(docs=[doc])
            client = _CadClient(active=app)
            got_app, got_doc, own_doc, own_app = cad_module._open_cad_drawing(client, path)
            self.assertFalse(own_doc)
            self.assertFalse(own_app)
            self.assertIs(doc, got_doc)
            cad_module._release_cad_drawing(got_app, got_doc, own_doc, own_app)
            self.assertEqual(0, doc.closed)
            self.assertFalse(app.quit_called)

    def test_own_drawing_closes_on_release(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path))
            client = _CadClient(active=None, dispatch_doc=doc)
            got_app, got_doc, own_doc, own_app = cad_module._open_cad_drawing(client, path)
            self.assertTrue(own_doc)
            self.assertTrue(own_app)
            self.assertEqual(1, client.dispatched)
            cad_module._release_cad_drawing(got_app, got_doc, own_doc, own_app)
            self.assertEqual(1, doc.closed)
            self.assertTrue(got_app.quit_called)

    def test_reuses_running_autocad_to_open_new_file(self):
        """用户开着 AutoCAD 但没开这张图:借他的实例打开,关文档但不关他的 AutoCAD。"""
        with TemporaryDirectory() as tmp:
            other = _CadDocument(str(_dwg_file(Path(tmp), "别的图.dwg")))
            path = _dwg_file(Path(tmp))
            target = _CadDocument(str(path))
            app = _CadApplication(docs=[other], open_result=target)
            client = _CadClient(active=app)
            got_app, got_doc, own_doc, own_app = cad_module._open_cad_drawing(client, path)
            self.assertIs(target, got_doc)
            self.assertTrue(own_doc, "图纸是我们打开的,应当归我们关")
            self.assertFalse(own_app, "AutoCAD 是用户的,绝不能 Quit")
            self.assertEqual(0, client.dispatched, "不该另起一个 AutoCAD 进程")
            cad_module._release_cad_drawing(got_app, got_doc, own_doc, own_app)
            self.assertEqual(1, target.closed)
            self.assertFalse(app.quit_called, "用户的 AutoCAD 被关掉了")

    def test_cad_started_by_same_task_is_kept_for_visual_followup(self):
        """先由 launch_app 打开的 CAD 不能被 cad_open_info 的收尾误关。"""
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path))
            app = _CadApplication(docs=[doc])
            client = _CadClient(active=app)
            with mock.patch.object(cad_module.system_control, "owned_application", return_value=True):
                got_app, got_doc, own_doc, own_app = cad_module._open_cad_drawing(client, path)
            self.assertFalse(own_doc)
            self.assertFalse(own_app)
            cad_module._release_cad_drawing(got_app, got_doc, own_doc, own_app)
            self.assertFalse(app.quit_called, "后续可视化操作仍需要这个 CAD 实例")

    def test_never_assigns_visible(self):
        """AutoCAD 不允许设 Application.Visible;设了就抛错,开图直接失败。"""
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path))
            client = _CadClient(active=None, dispatch_doc=doc)
            got_app, got_doc, own_doc, own_app = cad_module._open_cad_drawing(client, path)
            self.assertIs(doc, got_doc)
            cad_module._release_cad_drawing(got_app, got_doc, own_doc, own_app)

    def test_unsaved_new_drawing_does_not_break_scan(self):
        """用户开着一张从没存盘的新图,FullName 抛错,不能连带整个枚举挂掉。"""
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            target = _CadDocument(str(path))
            app = _CadApplication(docs=[_NoNameDocument("")], open_result=target)
            client = _CadClient(active=app)
            got_app, got_doc, own_doc, own_app = cad_module._open_cad_drawing(client, path)
            self.assertIs(target, got_doc)
            cad_module._release_cad_drawing(got_app, got_doc, own_doc, own_app)

    def test_autocad_lt_gives_plain_language_error(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            client = _CadClient(active=None, dispatch_error=RuntimeError("无效的类字符串"))
            with self.assertRaises(RuntimeError) as ctx:
                cad_module._open_cad_drawing(client, path)
            self.assertIn("AutoCAD LT", str(ctx.exception))


class CadBusyRetryTests(unittest.TestCase):
    def test_busy_error_is_retried(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise _BUSY
            return "ok"

        with mock.patch.object(cad_module, "_COM_RETRY_SLEEP", 0):
            self.assertEqual("ok", cad_module._retry_on_busy(flaky, what="测试"))
        self.assertEqual(3, len(calls))

    def test_non_busy_error_raises_immediately(self):
        calls = []

        def broken():
            calls.append(1)
            raise RuntimeError("路径不存在")

        with mock.patch.object(cad_module, "_COM_RETRY_SLEEP", 0):
            with self.assertRaises(RuntimeError):
                cad_module._retry_on_busy(broken, what="测试")
        self.assertEqual(1, len(calls), "非忙错误不该重试")

    def test_open_retries_when_autocad_busy(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path))
            app = _CadApplication(docs=[], open_result=doc,
                                  open_error=_BUSY, open_error_times=2)
            client = _CadClient(active=app)
            with mock.patch.object(cad_module, "_COM_RETRY_SLEEP", 0):
                got_app, got_doc, own_doc, own_app = cad_module._open_cad_drawing(client, path)
            self.assertIs(doc, got_doc)
            self.assertEqual(3, len(app.Documents.open_calls), "应当重试到成功")
            cad_module._release_cad_drawing(got_app, got_doc, own_doc, own_app)


class CadBackupTests(unittest.TestCase):
    def test_backup_creates_isolated_file(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp), "drawing.dwg")
            with mock.patch.object(cad_module.isolation_workspace, "root", return_value=Path(tmp) / "isolation"):
                backup = cad_module._backup_drawing(path)
            self.assertEqual(Path(tmp) / "isolation" / "workspace" / "backups" / "cad", Path(backup).parent)
            self.assertTrue(Path(backup).is_file())
            self.assertIn("备份-", str(backup))
            self.assertEqual(path.read_bytes(), Path(backup).read_bytes())


class CadHeaderTests(unittest.TestCase):
    def test_reads_dwg_format_from_header(self):
        with TemporaryDirectory() as tmp:
            for header, label in (("AC1027", "AutoCAD 2013"), ("AC1032", "AutoCAD 2018+"),
                                  ("AC1018", "AutoCAD 2004")):
                path = _dwg_file(Path(tmp), f"{header}.dwg", header.encode())
                self.assertEqual(header, cad_module._dwg_header(path))
                self.assertEqual(label, cad_module._DWG_HEADER_LABELS[header])

    def test_missing_file_header_is_empty(self):
        self.assertEqual("", cad_module._dwg_header(Path("/不存在/x.dwg")))


class CadSerializationTests(unittest.TestCase):
    def test_numbers_flattens_com_arrays(self):
        self.assertEqual([1.0, 2.0, 3.0], cad_module._numbers((1, 2, 3)))
        self.assertEqual([], cad_module._numbers(None))
        self.assertEqual([5.0], cad_module._numbers(5))
        self.assertEqual([], cad_module._numbers(object()))


class CadToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_com_does_not_claim_foreground_or_show_takeover_notice(self):
        control = cad_module.system_control
        notices = []
        context = control.set_foreground_notice_callback(notices.append)
        self.addCleanup(control.reset_foreground_notice_callback, context)
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path))
            client = _CadClient(active=None, dispatch_doc=doc)
            # Background CAD must work even while another task holds the input lease.
            with mock.patch.object(control, "acquire_control", side_effect=AssertionError("COM must not acquire input control")), \
                 _registered_tools(client) as registry:
                result = await registry.call("cad_open_info", {"path": str(path)})
                self.assertEqual("AutoCAD 2013", result["dwg_format"])
                with self.assertRaises(RuntimeError):
                    await registry.call("cad_open_info", {"path": str(Path(tmp) / "missing.dwg")})
        self.assertEqual([], notices)

    async def test_open_info_reports_mm_and_meters(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            for insunits, expected in ((4, "毫米(mm)"), (6, "米(m)"), (0, "无单位"),
                                       (2, "英尺(ft)")):
                doc = _CadDocument(str(path), modelspace=[_CadEntity("AcDbLine")],
                                   variables={"INSUNITS": insunits})
                client = _CadClient(active=None, dispatch_doc=doc)
                with _registered_tools(client) as registry:
                    result = await registry.call("cad_open_info", {"path": str(path)})
                self.assertEqual(expected, result["unit"], f"insunits={insunits}")

    async def test_open_info_result_is_json_serializable(self):
        """EXTMIN/EXTMAX 是 COM 数组,直接进返回值会让工具执行失败。"""
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path), modelspace=[_CadEntity("AcDbLine")])
            client = _CadClient(active=None, dispatch_doc=doc)
            with _registered_tools(client) as registry:
                result = await registry.call("cad_open_info", {"path": str(path)})
            json.dumps(result, ensure_ascii=False)  # 炸了就是回归
            self.assertEqual([0.0, 0.0, 0.0], result["extents"]["min"])
            self.assertEqual([42000.0, 29700.0, 0.0], result["extents"]["max"])

    async def test_open_info_reports_dwg_format_from_header(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp), header=b"AC1032")
            doc = _CadDocument(str(path))
            client = _CadClient(active=None, dispatch_doc=doc)
            with _registered_tools(client) as registry:
                result = await registry.call("cad_open_info", {"path": str(path)})
            self.assertEqual("AutoCAD 2018+", result["dwg_format"])

    async def test_open_info_survives_missing_layer_properties(self):
        """某些 AutoCAD 版本上图层属性名不同;缺一个不该毁掉整份报告。"""
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            layers = [_CadLayer("0"), _CadLayer("WALL", drop=("LayerOn", "Freeze"))]
            doc = _CadDocument(str(path), layers=layers)
            client = _CadClient(active=None, dispatch_doc=doc)
            with _registered_tools(client) as registry:
                result = await registry.call("cad_open_info", {"path": str(path)})
            self.assertEqual(2, result["layer_count"])
            names = [lay["name"] for lay in result["layers"]]
            self.assertEqual(["0", "WALL"], names)
            self.assertNotIn("on", result["layers"][1])
            self.assertIn("on", result["layers"][0])

    async def test_open_info_skips_layout_blocks_and_counts_inserts(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            blocks = [_CadBlock("*Model_Space"), _CadBlock("*Paper_Space"),
                      _CadBlock("标高符号"), _CadBlock("门M1")]
            entities = [
                _CadEntity("AcDbBlockReference", name="标高符号"),
                _CadEntity("AcDbBlockReference", name="标高符号"),
                _CadEntity("AcDbBlockReference", name="标高符号"),
                _CadEntity("AcDbLine"),
            ]
            doc = _CadDocument(str(path), modelspace=entities, blocks=blocks)
            client = _CadClient(active=None, dispatch_doc=doc)
            with _registered_tools(client) as registry:
                result = await registry.call("cad_open_info", {"path": str(path)})
            got = {b["name"]: b["inserted"] for b in result["blocks"]}
            self.assertEqual({"标高符号": 3, "门M1": 0}, got, "系统块不该报出来")
            self.assertEqual(4, result["entity_total"])

    async def test_missing_file_reports_before_touching_com(self):
        client = _CadClient(active=None)
        with _registered_tools(client) as registry:
            for tool, args in (
                ("cad_open_info", {}),
                ("cad_read_entities", {}),
                ("cad_read_text", {}),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    await registry.call(tool, {"path": "/不存在/图纸.dwg", **args})
                self.assertIn("不存在", str(ctx.exception))
        self.assertEqual(0, client.dispatched, "文件都没有,不该去启动 AutoCAD")

    async def test_save_keeps_original_dwg_format(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp), header=b"AC1018")  # 设计院给的 2004 格式
            doc = _CadDocument(str(path), modelspace=[_CadEntity("AcDbText", text="旧日期")])
            client = _CadClient(active=None, dispatch_doc=doc)
            with mock.patch.object(cad_module, "_backup_drawing", return_value="x.备份"), \
                 _registered_tools(client) as registry:
                result = await registry.call("cad_replace_text", {
                    "path": str(path),
                    "replacements": [{"old": "旧日期", "new": "新日期"}],
                    "dry_run": False,
                })
            self.assertTrue(result["ok"])
            self.assertEqual([(str(path.resolve()), 24)], doc.saved_as)
            self.assertIn("2004", result["saved_as"])
            self.assertEqual(0, doc.save_called)

    async def test_save_falls_back_when_saveas_rejected(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp), header=b"AC1018")
            doc = _CadDocument(str(path), modelspace=[_CadEntity("AcDbText", text="旧")],
                               saveas_error=RuntimeError("不支持该格式"))
            client = _CadClient(active=None, dispatch_doc=doc)
            with mock.patch.object(cad_module, "_backup_drawing", return_value="x.备份"), \
                 _registered_tools(client) as registry:
                result = await registry.call("cad_replace_text", {
                    "path": str(path),
                    "replacements": [{"old": "旧", "new": "新"}],
                    "dry_run": False,
                })
            self.assertTrue(result["ok"])
            self.assertEqual(1, doc.save_called, "SaveAs 失败要退回 Save")
            self.assertIn("默认格式", result["saved_as"])

    async def test_read_entities_limits_and_reports_total(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            entities = [_CadEntity("AcDbLine", position=(float(i), 0.0)) for i in range(50000)]
            doc = _CadDocument(str(path), modelspace=entities)
            client = _CadClient(active=None, dispatch_doc=doc)
            with _registered_tools(client) as registry:
                result = await registry.call("cad_read_entities",
                                             {"path": str(path), "types": "LINE"})
            self.assertEqual(50000, result["total"])
            self.assertEqual(500, result["returned"])
            self.assertTrue(result["truncated"])
            json.dumps(result, ensure_ascii=False)

    async def test_replace_text_dry_run_default_true_only_reports(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path), modelspace=[
                _CadEntity("AcDbText", text="图名:旧"),
                _CadEntity("AcDbText", text="图名:旧"),
            ])
            client = _CadClient(active=None, dispatch_doc=doc)
            with _registered_tools(client) as registry:
                result = await registry.call("cad_replace_text", {
                    "path": str(path),
                    "replacements": [{"old": "旧", "new": "新"}],
                })
            self.assertTrue(result["dry_run"])
            self.assertEqual(2, result["changes"])
            self.assertEqual("图名:旧", doc.ModelSpace.entities[0].TextString)
            self.assertEqual([], doc.saved_as)

    async def test_dry_run_detail_is_json_serializable(self):
        """明细里绝不能装 COM 实体对象,否则工具执行必失败。"""
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path), modelspace=[
                _CadEntity("AcDbText", text="2025年3月", layer="标注"),
            ])
            client = _CadClient(active=None, dispatch_doc=doc)
            with _registered_tools(client) as registry:
                result = await registry.call("cad_replace_text", {
                    "path": str(path),
                    "replacements": [{"old": "2025年3月", "new": "2026年8月"}],
                })
            json.dumps(result, ensure_ascii=False)  # 炸了就是回归
            detail = result["detail"][0]
            self.assertEqual("2025年3月", detail["before"])
            self.assertEqual("2026年8月", detail["after"])
            self.assertEqual("标注", detail["layer"])

    async def test_dry_run_detail_is_capped(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            entities = [_CadEntity("AcDbText", text="旧") for _ in range(600)]
            doc = _CadDocument(str(path), modelspace=entities)
            client = _CadClient(active=None, dispatch_doc=doc)
            with _registered_tools(client) as registry:
                result = await registry.call("cad_replace_text", {
                    "path": str(path),
                    "replacements": [{"old": "旧", "new": "新"}],
                })
            self.assertEqual(600, result["changes"])
            self.assertEqual(cad_module._MAX_DETAIL, len(result["detail"]))
            self.assertTrue(result["detail_truncated"])

    async def test_replace_text_dry_run_false_modifies(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path), modelspace=[_CadEntity("AcDbText", text="图名:旧")])
            client = _CadClient(active=None, dispatch_doc=doc)
            with mock.patch.object(cad_module, "_backup_drawing", return_value="x.备份"), \
                 _registered_tools(client) as registry:
                result = await registry.call("cad_replace_text", {
                    "path": str(path),
                    "replacements": [{"old": "旧", "new": "新"}],
                    "dry_run": False,
                })
            self.assertFalse(result["dry_run"])
            self.assertEqual("图名:新", doc.ModelSpace.entities[0].TextString)

    async def test_refuses_to_modify_drawing_user_has_open(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path), modelspace=[_CadEntity("AcDbText", text="旧")])
            app = _CadApplication(docs=[doc])
            client = _CadClient(active=app)
            with _registered_tools(client) as registry:
                with self.assertRaises(RuntimeError) as ctx:
                    await registry.call("cad_replace_text", {
                        "path": str(path),
                        "replacements": [{"old": "旧", "new": "新"}],
                        "dry_run": False,
                    })
            self.assertIn("正开在你的 AutoCAD", str(ctx.exception))
            self.assertEqual("旧", doc.ModelSpace.entities[0].TextString)
            self.assertEqual(0, doc.closed, "用户的图纸不能被关")

    async def test_add_text_30_items_is_one_roundtrip(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path))
            client = _CadClient(active=None, dispatch_doc=doc)
            items = [{"text": f"标注{i}", "x": float(i), "y": 0.0} for i in range(30)]
            with mock.patch.object(cad_module, "_backup_drawing", return_value="x.备份"), \
                 _registered_tools(client) as registry:
                result = await registry.call("cad_add_text", {"path": str(path), "items": items})
            self.assertEqual(30, result["added"])
            self.assertEqual(1, client.dispatched)
            self.assertEqual(30, len(doc.ModelSpace.added))
            self.assertEqual(1, len(doc.saved_as))

    async def test_draw_circle_uses_com_point_and_saves_after_backup(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path))
            client = _CadClient(active=None, dispatch_doc=doc)
            with mock.patch.object(cad_module, "_backup_drawing", return_value="x.备份"), \
                 _registered_tools(client) as registry:
                result = await registry.call("cad_draw_circle", {
                    "path": str(path), "center": [12, 34], "radius": 20, "layer": "轮廓",
                })
            self.assertTrue(result["ok"])
            self.assertEqual([12.0, 34.0], result["center"])
            self.assertEqual(20.0, result["radius"])
            self.assertEqual(1, len(doc.ModelSpace.add_circle_calls))
            center, radius = doc.ModelSpace.add_circle_calls[0]
            self.assertIsInstance(center, _FakeVariant)
            self.assertEqual((12.0, 34.0, 0.0), _unwrap(center))
            self.assertEqual(20.0, radius)
            self.assertEqual(1, len(doc.saved_as))
            self.assertEqual("轮廓", doc.ModelSpace.added[0].Layer)

    async def test_draw_circle_defaults_to_workspace_with_live_snapshot_backup(self):
        for saved_before in (False, True):
            with self.subTest(saved_before=saved_before), TemporaryDirectory() as tmp:
                root = Path(tmp)
                workspace = root / "isolation" / "workspace"
                source = _dwg_file(root) if saved_before else None
                original_bytes = source.read_bytes() if source else None
                user_doc = _CadDocument(str(source)) if source else _NoNameDocument("")
                doc = _CadDocument("")
                app = _CadApplication(docs=[user_doc], open_result=doc)
                app.ActiveDocument = user_doc
                client = _CadClient(active=app)
                save_as = doc.SaveAs

                def snapshot(path, version=None):
                    save_as(path, version)
                    Path(path).write_bytes(b"AC1027-live-" + str(len(doc.ModelSpace.added)).encode())
                    doc.FullName = str(path)

                doc.SaveAs = snapshot
                def save_current():
                    doc.save_called += 1
                    Path(doc.saved_as[-1][0]).write_bytes(b"AC1027-live-" + str(len(doc.ModelSpace.added)).encode())
                doc.Save = save_current
                with mock.patch.object(cad_module.isolation_workspace, "root", return_value=root / "isolation"), \
                     _registered_tools(client) as registry:
                    result = await registry.call("cad_draw_circle", {"center": [0, 0], "radius": 333})
                target = Path(result["path"])
                backup = Path(result["backup"])
                self.assertEqual(workspace, target.parent)
                self.assertEqual(b"AC1027-live-1", target.read_bytes())
                self.assertEqual(b"AC1027-live-0", backup.read_bytes())
                self.assertEqual(workspace / "backups" / "cad", backup.parent)
                self.assertIn(str(target), result["summary"])
                self.assertEqual(1, doc.closed)
                self.assertEqual(0, user_doc.closed)
                self.assertEqual([], user_doc.saved_as)
                self.assertEqual([], user_doc.ModelSpace.added)
                self.assertEqual(1, len(doc.saved_as), "新副本只应 SaveAs 一次")
                self.assertEqual(1, doc.save_called)
                self.assertFalse(app.quit_called)
                if source:
                    self.assertEqual(original_bytes, source.read_bytes())
                    self.assertIsNone(doc.saved_as[0][1])

    async def test_draw_circle_explicit_output_and_preflight_failures(self):
        for scenario in ("absolute", "relative", "exists", "save-failed", "backup-failed"):
            with self.subTest(scenario=scenario), TemporaryDirectory() as tmp:
                root = Path(tmp)
                doc = _CadDocument("")
                app = _CadApplication(docs=[_CadDocument("user.dwg")], open_result=doc)
                app.ActiveDocument = app.Documents.docs[0]
                client = _CadClient(active=app)
                requested = "cad/custom.dwg" if scenario == "relative" else str(root / "chosen.dwg")
                expected = (root / "isolation" / "workspace" / requested
                            if scenario == "relative" else Path(requested))
                if scenario == "exists":
                    expected.write_bytes(b"keep me")
                save_as = doc.SaveAs

                def snapshot(path, version=None):
                    if scenario == "save-failed":
                        raise RuntimeError("disk full")
                    save_as(path, version)
                    Path(path).write_bytes(b"AC1027snapshot")
                    doc.FullName = str(path)

                doc.SaveAs = snapshot
                backup_fn = cad_module._backup_drawing
                with mock.patch.object(cad_module.isolation_workspace, "root", return_value=root / "isolation"), \
                     mock.patch("backend.modules.office.fs_module._WORKSPACE_ROOT", None), \
                     mock.patch.object(cad_module, "_backup_drawing", side_effect=(
                         RuntimeError("backup failed") if scenario == "backup-failed" else backup_fn
                     )), _registered_tools(client) as registry:
                    args = {"center": [1, 2], "radius": 3, "output_path": requested}
                    if scenario in ("exists", "save-failed", "backup-failed"):
                        with self.assertRaises((FileExistsError, RuntimeError)):
                            await registry.call("cad_draw_circle", args)
                        self.assertEqual([], doc.ModelSpace.added)
                        self.assertEqual(0, doc.save_called)
                    else:
                        result = await registry.call("cad_draw_circle", args)
                        self.assertEqual(str(expected), result["path"])
                        self.assertTrue(expected.is_file())
                if scenario == "exists":
                    self.assertEqual(b"keep me", expected.read_bytes())

    async def test_unsaved_circle_stays_open_for_task_owned_and_new_documents(self):
        for existing in (True, False):
            doc = _CadDocument("")
            app = _CadApplication(docs=[doc] if existing else [], open_result=doc)
            app.ActiveDocument = doc if existing else None
            client = _CadClient(active=app)
            with self.subTest(existing=existing), _registered_tools(client) as registry, \
                 mock.patch.object(cad_module, "_is_task_cad", return_value=True):
                result = await registry.call("cad_draw_circle", {"center": [0, 0], "radius": 3, "save": False})
            self.assertTrue(result["ok"])
            self.assertEqual(0, doc.closed)
            self.assertFalse(app.quit_called)
            self.assertEqual([], doc.saved_as)
            self.assertEqual(0, doc.save_called)
            self.assertEqual(1, len(doc.ModelSpace.add_circle_calls))

    async def test_failed_unsaved_circle_closes_only_new_document(self):
        for existing in (True, False):
            doc = _CadDocument("")
            doc.ModelSpace.AddCircle = mock.Mock(side_effect=RuntimeError("draw failed"))
            app = _CadApplication(docs=[doc] if existing else [], open_result=doc)
            app.ActiveDocument = doc if existing else None
            client = _CadClient(active=app)
            with self.subTest(existing=existing), _registered_tools(client) as registry, \
                 self.assertRaisesRegex(RuntimeError, "draw failed"):
                await registry.call("cad_draw_circle", {"center": [0, 0], "radius": 3, "save": False})
            self.assertEqual(0 if existing else 1, doc.closed)
            self.assertFalse(app.quit_called)

    async def test_draw_circle_save_false_uses_current_open_drawing_without_landing(self):
        doc = _CadDocument("当前图纸.dwg")
        app = _CadApplication(docs=[doc])
        app.ActiveDocument = doc
        client = _CadClient(active=app)
        with _registered_tools(client) as registry:
            result = await registry.call("cad_draw_circle", {
                "center": [1, 2], "radius": 3, "save": False,
            })
        self.assertFalse(result["saved_as"])
        self.assertEqual([], doc.saved_as)
        self.assertEqual(1, len(doc.ModelSpace.add_circle_calls))

    async def test_draw_circle_rejects_non_positive_radius_before_com(self):
        client = _CadClient(active=None)
        with _registered_tools(client) as registry:
            with self.assertRaises(ValueError):
                await registry.call("cad_draw_circle", {"center": [0, 0], "radius": 0})
        self.assertEqual(0, client.dispatched)

    async def test_backup_failure_refuses_modification(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path))
            client = _CadClient(active=None, dispatch_doc=doc)
            with mock.patch.object(cad_module, "_backup_drawing",
                                   side_effect=RuntimeError("磁盘满")), \
                 _registered_tools(client) as registry:
                with self.assertRaises(RuntimeError):
                    await registry.call("cad_add_text", {
                        "path": str(path), "items": [{"text": "x", "x": 0, "y": 0}],
                    })
            self.assertEqual([], doc.ModelSpace.added)
            self.assertEqual([], doc.saved_as)
            self.assertEqual(0, doc.save_called)

    async def test_radius_dimension_gets_default_leader_length(self):
        """LeaderLength=0 在真机上会被拒,必须自动给一个。"""
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path))
            client = _CadClient(active=None, dispatch_doc=doc)
            with mock.patch.object(cad_module, "_backup_drawing", return_value="x.备份"), \
                 _registered_tools(client) as registry:
                result = await registry.call("cad_add_dimensions", {
                    "path": str(path),
                    "items": [{"type": "radius", "p1": [0, 0], "p2": [100, 0]}],
                })
            self.assertEqual(1, result["added"], f"失败明细:{result.get('failed')}")
            self.assertEqual([("radial", 10.0)], doc.ModelSpace.dim_calls)

    async def test_one_bad_dimension_does_not_abort_the_batch(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path))
            client = _CadClient(active=None, dispatch_doc=doc)
            items = [
                {"type": "linear", "p1": [0, 0], "p2": [100, 0], "text_pos": [50, 20]},
                {"type": "radius", "p1": [0, 0], "p2": [0, 0]},  # 半径 0,LeaderLength 也 0
                {"type": "aligned", "p1": [0, 0], "p2": [50, 50], "text_pos": [25, 40]},
            ]
            with mock.patch.object(cad_module, "_backup_drawing", return_value="x.备份"), \
                 _registered_tools(client) as registry:
                result = await registry.call("cad_add_dimensions",
                                             {"path": str(path), "items": items})
            self.assertEqual(2, result["added"])
            self.assertEqual(1, len(result["failed"]))
            self.assertEqual(1, result["failed"][0]["index"])
            self.assertFalse(result["ok"])
            json.dumps(result, ensure_ascii=False)
            self.assertEqual(1, len(doc.saved_as), "成功的那两条仍要存盘")

    async def test_linear_dimension_passes_rotation(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path))
            client = _CadClient(active=None, dispatch_doc=doc)
            with mock.patch.object(cad_module, "_backup_drawing", return_value="x.备份"), \
                 _registered_tools(client) as registry:
                await registry.call("cad_add_dimensions", {
                    "path": str(path),
                    "items": [{"type": "linear", "p1": [0, 0], "p2": [100, 0],
                               "text_pos": [50, 20], "rotation": 1.5708}],
                })
            kind, p1, pos, rot = doc.ModelSpace.dim_calls[0]
            self.assertEqual("rotated", kind)
            self.assertEqual((0.0, 0.0, 0.0), p1)
            self.assertEqual((50.0, 20.0, 0.0), pos)
            self.assertAlmostEqual(1.5708, rot)


class CadGeometryTests(unittest.TestCase):
    """几何读取。真机上多段线只返回 [0,0] 占位坐标,是这批测试要钉死的回归。"""

    def test_polyline_position_falls_back_to_bbox_center(self):
        poly = _CadPolyline([0, 0, 6000, 0, 6000, 3000, 0, 3000])
        self.assertEqual([3000.0, 1500.0], cad_module._entity_position(poly))

    def test_polyline_geometry_returns_real_vertices(self):
        poly = _CadPolyline([0, 0, 6000, 0, 6000, 3000, 0, 3000],
                            length=18000.0, area=18000000.0)
        geo = cad_module._entity_geometry(poly, "AcDbPolyline")
        self.assertEqual([[0.0, 0.0], [6000.0, 0.0], [6000.0, 3000.0], [0.0, 3000.0]],
                         geo["vertices"])
        self.assertEqual(4, geo["vertex_count"])
        self.assertTrue(geo["closed"])
        self.assertEqual(18000.0, geo["length"])
        self.assertEqual({"min": [0.0, 0.0], "max": [6000.0, 3000.0]}, geo["bbox"])
        self.assertEqual([6000.0, 3000.0], geo["size"])

    def test_old_style_polyline_uses_three_value_stride(self):
        poly = _CadPolyline([0, 0, 0, 5000, 0, 0, 5000, 2500, 0], stride=3,
                            object_name="AcDb2dPolyline")
        geo = cad_module._entity_geometry(poly, "AcDb2dPolyline")
        self.assertEqual([[0.0, 0.0], [5000.0, 0.0], [5000.0, 2500.0]], geo["vertices"])

    def test_vertices_are_capped(self):
        flat = []
        for i in range(200):
            flat += [float(i), 0.0]
        poly = _CadPolyline(flat)
        geo = cad_module._entity_geometry(poly, "AcDbPolyline")
        self.assertEqual(cad_module._MAX_VERTICES, len(geo["vertices"]))
        self.assertEqual(200, geo["vertex_count"])
        self.assertTrue(geo["vertices_truncated"])

    def test_line_geometry_gives_endpoints(self):
        line = _CadEntity("AcDbLine", position=(0.0, 0.0), bbox=((0, 0), (3000, 0)))
        line.StartPoint = (0.0, 0.0, 0.0)
        line.EndPoint = (3000.0, 0.0, 0.0)
        line.Length = 3000.0
        geo = cad_module._entity_geometry(line, "AcDbLine")
        self.assertEqual([0.0, 0.0], geo["start"])
        self.assertEqual([3000.0, 0.0], geo["end"])
        self.assertEqual(3000.0, geo["length"])

    def test_circle_geometry_gives_center_and_radius(self):
        circle = _CadEntity("AcDbCircle", bbox=((-500, -500), (500, 500)))
        circle.Center = (0.0, 0.0, 0.0)
        circle.Radius = 500.0
        geo = cad_module._entity_geometry(circle, "AcDbCircle")
        self.assertEqual([0.0, 0.0], geo["center"])
        self.assertEqual(500.0, geo["radius"])

    def test_text_geometry_gives_height(self):
        text = _CadEntity("AcDbText", text="标高 3.600", position=(100.0, 200.0),
                          bbox=((100, 200), (900, 500)))
        text.Height = 300.0
        geo = cad_module._entity_geometry(text, "AcDbText")
        self.assertEqual([100.0, 200.0], geo["position"])
        self.assertEqual(300.0, geo["height"])

    def test_block_reference_geometry_gives_block_name(self):
        block = _CadEntity("AcDbBlockReference", position=(1000.0, 0.0),
                           name="标高符号", bbox=((900, -100), (1100, 100)))
        geo = cad_module._entity_geometry(block, "AcDbBlockReference")
        self.assertEqual("标高符号", geo["block_name"])
        self.assertEqual([1000.0, 0.0], geo["insertion"])

    def test_dynamic_bounding_box_uses_explicit_variant_outputs(self):
        def bounds(low, high):
            self.assertEqual((0x400C, 0x400C), (low.vt, high.vt))
            low.value, high.value = (1.0, 2.0, 3.0), (4.0, 5.0, 6.0)
        entity = SimpleNamespace(_oleobj_=object(), GetBoundingBox=bounds)
        with _registered_tools(_CadClient()):
            self.assertEqual({"min": [1.0, 2.0], "max": [4.0, 5.0]}, cad_module._bounding_box(entity))

    def test_entity_without_bounding_box_still_works(self):
        plain = _CadEntity("AcDbLine", position=(5.0, 6.0))  # bbox=None -> 抛错
        self.assertEqual([5.0, 6.0], cad_module._entity_position(plain))
        geo = cad_module._entity_geometry(plain, "AcDbLine")
        self.assertNotIn("bbox", geo)

    def test_area_filter_uses_bbox_overlap_not_just_center(self):
        """横穿查询范围的长墙线,中心点在范围外,但必须被选中——它正是要标注的那根。

        用例必须让"看包围盒"和"只看中心点"给出不同答案,否则测不出东西:
        墙线从 0 横到 20000,中心点在 10000;查询范围只框住左端 [0,2000],
        中心点落在范围外(按点筛会漏),但包围盒确实与范围相交(按盒筛能中)。
        """
        wall_box = {"min": [0.0, 0.0], "max": [20000.0, 100.0]}
        area = [0, -500, 2000, 500]      # 只框左端一小段
        center = [10000.0, 50.0]         # 中心点在范围外
        self.assertFalse(cad_module._in_area(center, area),
                         "前提检查:只看中心点时这根墙线会被漏掉")
        self.assertTrue(cad_module._in_area(center, area, wall_box),
                        "有包围盒时,横穿范围的墙线必须被选中")
        far_box = {"min": [50000.0, 0.0], "max": [60000.0, 100.0]}
        self.assertFalse(cad_module._in_area([55000.0, 50.0], area, far_box))

    def test_area_filter_without_bbox_falls_back_to_point(self):
        self.assertTrue(cad_module._in_area([100.0, 100.0], [0, 0, 200, 200]))
        self.assertFalse(cad_module._in_area([500.0, 100.0], [0, 0, 200, 200]))


class CadLockTests(unittest.TestCase):
    def test_lock_files_are_detected(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            self.assertEqual([], cad_module._lock_files(path))
            path.with_suffix(".dwl").write_text("锁")
            self.assertEqual(["施工图.dwl"], cad_module._lock_files(path))

    def test_check_writable_rejects_pre_existing_lock(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            with self.assertRaises(RuntimeError) as ctx:
                cad_module._check_writable(path, ["施工图.dwl"])
            self.assertIn("正被 AutoCAD 占用", str(ctx.exception))

    def test_check_writable_ignores_our_own_lock(self):
        """快照在打开之前拍,所以我们自己产生的锁不该让我们自我否决。

        注意这条只验函数本身。真正的 bug 在**调用点**(快照拍晚了),
        那个由 CadSaveFailureTests.test_our_own_lock_does_not_block_us 覆盖。
        """
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            path.with_suffix(".dwl").write_text("我们自己开图产生的锁")
            cad_module._check_writable(path, [])  # 打开前的快照是空的 -> 放行

    def test_check_writable_rejects_readonly(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            with mock.patch.object(cad_module.os, "access", return_value=False):
                with self.assertRaises(RuntimeError) as ctx:
                    cad_module._check_writable(path, [])
            self.assertIn("只读", str(ctx.exception))


class CadReadGeometryToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_entities_returns_polyline_vertices(self):
        """回归:真机上这里返回的是 position [0,0],没有顶点,没法据此标注。"""
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            poly = _CadPolyline([0, 0, 6000, 0, 6000, 3000, 0, 3000], layer="板房轮廓")
            doc = _CadDocument(str(path), modelspace=[poly])
            client = _CadClient(active=None, dispatch_doc=doc)
            with _registered_tools(client) as registry:
                result = await registry.call("cad_read_entities", {"path": str(path)})
            json.dumps(result, ensure_ascii=False)
            entity = result["entities"][0]
            self.assertEqual("板房轮廓", entity["layer"])
            self.assertEqual(4, entity["vertex_count"])
            self.assertEqual([[0.0, 0.0], [6000.0, 0.0], [6000.0, 3000.0], [0.0, 3000.0]],
                             entity["vertices"])
            self.assertEqual([3000.0, 1500.0], entity["position"],
                             "多段线的代表点必须是包围盒中心,不是 [0,0]")
            self.assertEqual([6000.0, 3000.0], entity["size"])

    async def test_read_entities_area_filter_catches_crossing_wall(self):
        """查询范围只框住墙线左端,墙线中心点在范围外——按点筛会漏,按包围盒才中。

        范围要故意避开中心点(墙线中心在 10000),否则两种判法答案一样,测不出东西。
        """
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            wall = _CadPolyline([0, 0, 20000, 0], layer="墙")      # 中心点 (10000, 0)
            far = _CadPolyline([50000, 0, 60000, 0], layer="墙")
            doc = _CadDocument(str(path), modelspace=[wall, far])
            client = _CadClient(active=None, dispatch_doc=doc)
            with _registered_tools(client) as registry:
                result = await registry.call("cad_read_entities", {
                    "path": str(path), "area": [0, -500, 2000, 500],  # 只框左端
                })
            self.assertEqual(1, result["total"], "横穿范围的墙线应当被选中,远处那条不该")
            self.assertEqual([[0.0, 0.0], [20000.0, 0.0]], result["entities"][0]["vertices"])

    async def test_read_entities_omits_empty_text_field(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path), modelspace=[_CadPolyline([0, 0, 100, 100])])
            client = _CadClient(active=None, dispatch_doc=doc)
            with _registered_tools(client) as registry:
                result = await registry.call("cad_read_entities", {"path": str(path)})
            self.assertNotIn("text", result["entities"][0], "没有文字就别占字段")


class CadSaveFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_failure_reports_plain_language_and_keeps_backup(self):
        """真机遇到过 -2145320861。不能只甩错误码,要说清可能原因、并说明原文件没坏。"""
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp), header=b"AC1027")
            doc = _CadDocument(str(path), modelspace=[_CadEntity("AcDbText", text="旧")],
                               saveas_error=RuntimeError("保存文档时出错 (-2145320861)"))

            def _boom():
                raise RuntimeError("保存文档时出错 (-2145320861)")
            doc.Save = _boom
            client = _CadClient(active=None, dispatch_doc=doc)
            with mock.patch.object(cad_module, "_backup_drawing", return_value="x.备份"), \
                 _registered_tools(client) as registry:
                with self.assertRaises(RuntimeError) as ctx:
                    await registry.call("cad_replace_text", {
                        "path": str(path),
                        "replacements": [{"old": "旧", "new": "新"}],
                        "dry_run": False,
                    })
            msg = str(ctx.exception)
            self.assertIn("存不回去", msg)
            self.assertIn("备份仍在", msg)
            self.assertIn("只读", msg)
            self.assertIn("-2145320861", msg, "原始错误码要留着,便于定位")

    async def test_locked_drawing_refuses_before_backup(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            path.with_suffix(".dwl").write_text("别处开着")
            doc = _CadDocument(str(path), modelspace=[_CadEntity("AcDbText", text="旧")])
            client = _CadClient(active=None, dispatch_doc=doc)
            backups = []
            with mock.patch.object(cad_module, "_backup_drawing",
                                   side_effect=lambda p: backups.append(p) or "x"), \
                 _registered_tools(client) as registry:
                with self.assertRaises(RuntimeError) as ctx:
                    await registry.call("cad_add_text", {
                        "path": str(path), "items": [{"text": "x", "x": 0, "y": 0}],
                    })
            self.assertIn("正被 AutoCAD 占用", str(ctx.exception))
            self.assertEqual([], backups, "锁着就别备份了,白占磁盘")

    async def test_our_own_lock_does_not_block_us(self):
        """真机行为:我们自己用 COM 打开图纸也会生成 .dwl。

        锁快照必须在打开之前拍。拍晚了就会把自己刚产生的锁当成"别处正开着",
        每一次改图都自我否决——这个 bug 在函数内部看不出来,只有走完整工具链才暴露。
        """
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            self.assertEqual([], cad_module._lock_files(path), "前提:一开始没有锁文件")
            doc = _CadDocument(str(path), modelspace=[_CadEntity("AcDbText", text="旧")])
            client = _CadClient(active=None, dispatch_doc=doc, lock_on_open=True)
            with mock.patch.object(cad_module, "_backup_drawing", return_value="x.备份"), \
                 _registered_tools(client) as registry:
                result = await registry.call("cad_replace_text", {
                    "path": str(path),
                    "replacements": [{"old": "旧", "new": "新"}],
                    "dry_run": False,
                })
            self.assertTrue(result["ok"])
            self.assertEqual(1, result["changes"])
            self.assertEqual(["施工图.dwl"], cad_module._lock_files(path),
                             "假 AutoCAD 应当在打开时生成 .dwl,否则测不出快照时机")

    async def test_dry_run_works_on_locked_drawing(self):
        """预演只读不写,别人开着也该能看。"""
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            path.with_suffix(".dwl").write_text("别处开着")
            doc = _CadDocument(str(path), modelspace=[_CadEntity("AcDbText", text="旧")])
            client = _CadClient(active=None, dispatch_doc=doc)
            with _registered_tools(client) as registry:
                result = await registry.call("cad_replace_text", {
                    "path": str(path),
                    "replacements": [{"old": "旧", "new": "新"}],
                })
            self.assertEqual(1, result["changes"])
            self.assertTrue(result["dry_run"])


class CadResolveStrategyTests(unittest.TestCase):
    """39.1 四层兜底 + 39.2 不吞错 + 39.3 人话错误。"""

    def setUp(self):
        cad_module._RESOLVED_STRATEGY = None
        cad_module._GEN_PY_CLEARED = False

    def tearDown(self):
        cad_module._RESOLVED_STRATEGY = None
        cad_module._GEN_PY_CLEARED = False

    def test_a_late_succeeds_without_fallback(self):
        doc = _CadDocument("d:\\a.dwg")
        app = _CadApplication(docs=[doc])
        client = _CadClient(active=app)
        resolved, strategy, own = cad_module._resolve_cad_app(client, prefer_running=True)
        self.assertIs(app, resolved)
        self.assertEqual("late", strategy)
        self.assertFalse(own)
        self.assertEqual(0, client.ensure_calls)
        self.assertEqual(0, client.rebuild_calls, "A 直接成功,不该碰 gen_py")

    def test_b_application_falls_back(self):
        # ROT 里挂的是 Document 而不是 Application:.Application 才能回到应用对象
        good_app = _CadApplication(docs=[])
        broken = _NoDocumentsApp(application=good_app)
        client = _CadClient(active=broken)
        resolved, strategy, own = cad_module._resolve_cad_app(client, prefer_running=True)
        self.assertIs(good_app, resolved)
        self.assertEqual("application", strategy)
        self.assertEqual(0, client.ensure_calls)

    def test_c_rebuild_clears_gen_py(self):
        broken = _NoDocumentsApp()  # 无 Application
        client = _CadClient(active=broken, gen_py_path="/tmp/fake_gen_py")
        with mock.patch.object(cad_module.shutil, "rmtree") as rmtree, \
             mock.patch.object(cad_module.os.path, "isdir", return_value=True):
            resolved, strategy, own = cad_module._resolve_cad_app(client, prefer_running=True)
        self.assertEqual("rebuild", strategy)
        self.assertFalse(own, "GetActiveObject 能拿到实例,own 必须 False(40.7)")
        rmtree.assert_called_once(), "清缓存必须真的被调用了"
        self.assertEqual(1, client.rebuild_calls)

    def test_d_early_falls_back(self):
        broken = _NoDocumentsApp()
        good_app = _CadApplication(docs=[])
        client = _CadClient(active=broken, dispatch_error=RuntimeError("无效的类字符串"),
                            early_app=good_app)
        resolved, strategy, own = cad_module._resolve_cad_app(client, prefer_running=True)
        self.assertIs(good_app, resolved)
        self.assertEqual("early", strategy)
        self.assertFalse(own, "GetActiveObject 能拿到实例,own 必须 False(40.7)")
        self.assertEqual(1, client.ensure_calls)

    def test_all_fail_dispatch_reports_lt(self):
        """43.3:Dispatch 类未注册(LT)时,文案要说 AutoCAD LT,不再一次性倒三种可能。"""
        broken = _NoDocumentsApp()
        client = _CadClient(active=broken, dispatch_error=RuntimeError("无效的类字符串"),
                            early_error=RuntimeError("无效的类字符串"),
                            gen_py_path="/tmp/gen_py")
        with self.assertRaises(RuntimeError) as ctx:
            cad_module._resolve_cad_app(client, prefer_running=True)
        self.assertIn("AutoCAD LT", str(ctx.exception))
        self.assertNotIn("gen_py", str(ctx.exception))

    def test_strategy_is_cached(self):
        doc = _CadDocument("d:\\a.dwg")
        app = _CadApplication(docs=[doc])
        client = _CadClient(active=app)
        with mock.patch.object(cad_module, "_probe", wraps=cad_module._probe) as probe:
            first, strat1, _ = cad_module._resolve_cad_app(client, prefer_running=True)
            second, strat2, _ = cad_module._resolve_cad_app(client, prefer_running=True)
        self.assertEqual("late", strat1)
        self.assertEqual("late", strat2)
        self.assertIs(first, second)
        probe.assert_called_once(), "第二次调用应走缓存,不再重跑完整探测(清缓存很慢)"

    def test_rebuild_own_app_false_when_running(self):
        """40.7:用户在跑(GetActiveObject 能拿到)时,rebuild 的 own 必须 False。"""
        cad_module._RESOLVED_STRATEGY = None
        cad_module._GEN_PY_CLEARED = False
        running = _NoDocumentsApp()          # 用户的坏 wrapper
        fixed = _CadApplication(docs=[])     # Dispatch 附着后拿到的可用对象
        client = _CadClient(active=running, dispatch_result=fixed)
        with mock.patch.object(cad_module.shutil, "rmtree"), \
             mock.patch.object(cad_module.os.path, "isdir", return_value=True):
            app, strategy, own = cad_module._resolve_cad_app(client, prefer_running=True)
        self.assertEqual("rebuild", strategy)
        self.assertIs(fixed, app)
        self.assertFalse(own, "用户 AutoCAD 在跑,rebuild 的 own 不能是 True")

    def test_early_own_app_false_when_running(self):
        cad_module._RESOLVED_STRATEGY = None
        cad_module._GEN_PY_CLEARED = False
        running = _NoDocumentsApp()
        fixed = _CadApplication(docs=[])
        client = _CadClient(active=running, dispatch_error=RuntimeError("无效的类字符串"),
                            early_app=fixed)
        app, strategy, own = cad_module._resolve_cad_app(client, prefer_running=True)
        self.assertEqual("early", strategy)
        self.assertIs(fixed, app)
        self.assertFalse(own, "用户 AutoCAD 在跑,early 的 own 不能是 True")

    def test_resolve_own_app_true_when_late_dispatches_then_rebuild(self):
        """42.9:late 先 Dispatch 起坏实例(进 ROT),rebuild 清缓存后收尾,own 仍必须 True。

        旧实现把快照放在 rebuild 自己的 Dispatch 之前,此时 ROT 里已经有 late 起的
        实例,会误判 own=False(泄漏 acad.exe)。入口快照才能回答"是不是我们启动的"。
        """
        cad_module._RESOLVED_STRATEGY = None
        cad_module._GEN_PY_CLEARED = False
        broken = _NoDocumentsApp()          # late 的 Dispatch 起的坏实例(gen_py 坏)
        fixed = _CadApplication(docs=[])    # rebuild 清缓存后 Dispatch 返回的可用实例
        client = _CadClient(active=None, dispatch_results=[broken, fixed])
        with mock.patch.object(cad_module.shutil, "rmtree"), \
             mock.patch.object(cad_module.os.path, "isdir", return_value=True):
            app, strategy, own = cad_module._resolve_cad_app(client, prefer_running=True)
        self.assertEqual("rebuild", strategy)
        self.assertIs(fixed, app)
        self.assertTrue(own, "late 先起的坏实例是我们自己 Dispatch 的,own 必须是 True")

    def test_early_own_app_true_when_not_running(self):
        cad_module._RESOLVED_STRATEGY = None
        cad_module._GEN_PY_CLEARED = False
        fixed = _CadApplication(docs=[])
        client = _CadClient(active=None, dispatch_error=RuntimeError("无效的类字符串"),
                            early_app=fixed)  # 没在跑
        app, strategy, own = cad_module._resolve_cad_app(client, prefer_running=True)
        self.assertEqual("early", strategy)
        self.assertIs(fixed, app)
        self.assertTrue(own, "没在跑,early 起的实例 own 必须是 True")

    def test_rebuild_clears_gen_py_once_across_calls(self):
        """40.8:rebuild 的"清缓存"是一次性修复,后续调用只取对象,不重复清。"""
        broken = _NoDocumentsApp()
        fixed = _CadApplication(docs=[])
        client = _CadClient(active=broken, dispatch_result=fixed)
        with mock.patch.object(cad_module.shutil, "rmtree") as rmtree, \
             mock.patch.object(cad_module.os.path, "isdir", return_value=True):
            for _ in range(3):
                app, strat, own = cad_module._resolve_cad_app(client, prefer_running=True)
                self.assertEqual("rebuild", strat)
        rmtree.assert_called_once(), "三次调用只该清一次缓存"
        self.assertEqual(1, client.rebuild_calls)

    def test_scan_documents_error_stops_before_open(self):
        """39.2:Documents 取不到必须停在枚举,不能吞掉继续走 .Open。

        用 Documents 访问计数当证据:错误从 _scan 抛出来,访问 1 次就该停;
        若被 except 吞掉再走 .Open,会访问 2 次。
        """
        broken = _NoDocumentsApp()
        client = _CadClient(active=None)
        with mock.patch.object(cad_module, "_resolve_cad_app",
                               return_value=(broken, "late", True)), mock.patch("time.sleep"):
            with self.assertRaises(AttributeError):
                cad_module._open_cad_drawing(client, Path("d:\\a.dwg"))
        self.assertEqual(cad_module._COM_RETRY, broken.documents_accesses,
                         "仅重试读取 Documents，不应改走 Open")


class CadDynamicBindingTests(unittest.TestCase):
    def setUp(self):
        for name in ("_RESOLVED_STRATEGY", "_GEN_PY_CLEARED"):
            self.addCleanup(setattr, cad_module, name, getattr(cad_module, name))
        cad_module._RESOLVED_STRATEGY = None
        cad_module._GEN_PY_CLEARED = False

    def _dynamic_client(self, *, running=True):
        app = _CadApplication(docs=[])
        client = _CadClient(active=_NoDocumentsApp() if running else None,
                            dispatch_result=_NoDocumentsApp())
        raw = mock.Mock()
        raw.QueryInterface.return_value = raw
        get_active = mock.Mock(return_value=raw)
        if not running:
            get_active.side_effect = RuntimeError("no running CAD")
        client.pythoncom = SimpleNamespace(GetActiveObject=get_active, IID_IDispatch="IDispatch")
        client.dynamic = SimpleNamespace(CDispatch=object, DumbDispatch=mock.Mock(return_value=app))
        return client, app, raw

    def test_dynamic_recovers_broken_wrapper_without_clearing_cache(self):
        client, expected, raw = self._dynamic_client()
        with mock.patch.object(cad_module, "_clear_gen_py") as clear:
            app, strategy, own = cad_module._resolve_cad_app(client)
        self.assertIs(expected, app)
        self.assertEqual("dynamic", strategy)
        self.assertFalse(own)
        client.dynamic.DumbDispatch.assert_called_once_with(raw, createClass=mock.ANY)
        raw.QueryInterface.assert_called_once_with("IDispatch")
        clear.assert_not_called()
        self.assertEqual(0, client.ensure_calls)
        self.assertEqual(0, client.rebuild_calls)

    def test_dynamic_strategy_is_reused_without_cache_rebuild(self):
        client, expected, raw = self._dynamic_client()
        for _ in range(2):
            app, strategy, own = cad_module._resolve_cad_app(client)
            self.assertIs(expected, app)
            self.assertEqual("dynamic", strategy)
            self.assertFalse(own)
        self.assertEqual(2, client.dynamic.DumbDispatch.call_count)
        self.assertEqual(0, client.ensure_calls)
        self.assertEqual(0, client.rebuild_calls)

    def test_raw_running_object_preserves_user_ownership_when_wrapper_raises(self):
        client, expected, raw = self._dynamic_client()
        client.GetActiveObject = mock.Mock(side_effect=AttributeError("broken gen_py wrapper"))
        client.dispatch_error = AttributeError("broken generated module")
        app, strategy, own = cad_module._resolve_cad_app(client)
        self.assertIs(expected, app)
        self.assertEqual("dynamic", strategy)
        self.assertFalse(own, "Wrapper failure must not make the user's running CAD our own")
        cad_module._release_cad_drawing(app, None, False, own)
        self.assertFalse(app.quit_called)

    def test_cleanup_can_close_owned_instance_even_if_generated_wrapper_raises(self):
        client, expected, raw = self._dynamic_client()
        client.GetActiveObject = mock.Mock(side_effect=AttributeError("broken gen_py wrapper"))
        self.assertIsNone(cad_module._cleanup_started_instance(client, had_running=False))
        self.assertTrue(expected.quit_called)

    def test_cleanup_never_wraps_or_closes_users_running_instance(self):
        client, expected, raw = self._dynamic_client()
        client.GetActiveObject = mock.Mock(side_effect=AttributeError("broken gen_py wrapper"))
        self.assertIsNone(cad_module._cleanup_started_instance(client, had_running=True))
        client.dynamic.DumbDispatch.assert_not_called()
        self.assertFalse(expected.quit_called)

    def test_dynamic_cold_start_preserves_owned_instance_flag(self):
        client, expected, raw = self._dynamic_client(running=False)
        app, strategy, own = cad_module._resolve_cad_app(client)
        self.assertIs(expected, app)
        self.assertEqual("dynamic", strategy)
        self.assertTrue(own)
        client.dynamic.DumbDispatch.assert_called_once_with("AutoCAD.Application", createClass=mock.ANY)
        raw.QueryInterface.assert_not_called()

    def test_nested_dynamic_objects_use_same_no_cache_wrapper(self):
        client, expected, raw = self._dynamic_client()
        cad_module._dynamic_cad_object(client, raw)
        wrapper = client.dynamic.DumbDispatch.call_args.kwargs["createClass"]
        instance = wrapper()
        child = object()
        self.assertIs(expected, instance._wrap_dispatch_(child, "Documents", "unused-clsid"))
        client.dynamic.DumbDispatch.assert_called_with(child, "Documents", createClass=wrapper)

    def test_dynamic_member_lookup_retries_busy_without_hiding_missing_members(self):
        client, _, raw = self._dynamic_client()
        cad_module._dynamic_cad_object(client, raw)
        wrapper = client.dynamic.DumbDispatch.call_args.kwargs["createClass"]
        instance = wrapper()
        instance._oleobj_ = SimpleNamespace(GetIDsOfNames=mock.Mock(side_effect=[_BUSY, 42]))
        with mock.patch("time.sleep") as sleep:
            self.assertEqual(42, instance.__AttrToID__("Count"))
        sleep.assert_called_once()
        instance._oleobj_.GetIDsOfNames.side_effect = AttributeError("missing member")
        with mock.patch("time.sleep") as sleep, self.assertRaisesRegex(AttributeError, "missing member"):
            instance.__AttrToID__("Missing")
        sleep.assert_not_called()

    def test_dynamic_busy_error_is_not_cache_damage(self):
        client, expected, raw = self._dynamic_client()
        client.dynamic.DumbDispatch.side_effect = _BUSY
        app, own, error, nature = cad_module._run_strategy(client, "dynamic", True, True)
        self.assertIsNone(app)
        self.assertFalse(own)
        self.assertEqual("busy", nature)
        self.assertIn("纯动态绑定", error)

    def test_bad_error_keeps_strategy_details_without_instructing_cache_deletion(self):
        message = cad_module._cad_env_error("/tmp/gen_py", ["dynamic(失败: sample exception)"], "bad")
        self.assertIn("sample exception", message)
        self.assertIn("不能仅据此", message)
        self.assertNotIn("删掉", message)


class CadDocumentsErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_documents_item_error_propagates_not_swallowed(self):
        """39.2 回归(40.9 重写):Count 可取、Item 抛 AttributeError 时,
        错误必须从 Item 抛出来,不能被吞掉改从 Open 再抛。

        原测试的 client 四条策略全失败,在 _resolve_cad_app 就抛了,根本没走到
        _scan,assertNotIn("打开图纸") 是白捡的。这条让 Count 可用、Item 抛错,
        钉死"错误从 Item 传播"。
        """
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            app = _CadApplication(docs=[])
            app.Documents = _DocumentsWithFailingItem()
            client = _CadClient(active=app)
            with _registered_tools(client) as registry:
                with self.assertRaises(AttributeError) as ctx:
                    await registry.call("cad_open_info", {"path": str(path)})
        msg = str(ctx.exception)
        self.assertIn("Documents.Item", msg, "错误应从 Item 抛出,不是从 Open")


class CadVariantPointTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_text_point_is_variant(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path))
            client = _CadClient(active=None, dispatch_doc=doc)
            with mock.patch.object(cad_module, "_backup_drawing", return_value="x.备份"), \
                 _registered_tools(client) as registry:
                await registry.call("cad_add_text", {
                    "path": str(path),
                    "items": [{"text": "标注", "x": 10, "y": 20, "height": 3}],
                })
        _text, point, height = doc.ModelSpace.add_text_calls[0]
        self.assertIsInstance(point, _FakeVariant)
        self.assertEqual(0x2005, point.vt)  # VT_ARRAY(0x2000) | VT_R8(5)
        self.assertEqual([10.0, 20.0, 0.0], point.value)
        self.assertEqual(3.0, height, "字高是裸 float,不是 VARIANT")

    async def test_dimension_points_are_variant_scalars_plain(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path))
            client = _CadClient(active=None, dispatch_doc=doc)
            items = [
                {"type": "aligned", "p1": [0, 0], "p2": [100, 0], "text_pos": [50, 20]},
                {"type": "radius", "p1": [0, 0], "p2": [100, 0]},
                {"type": "angular", "p1": [0, 0], "p2": [100, 0],
                 "vertex": [50, 50], "text_pos": [50, 20]},
                {"type": "linear", "p1": [0, 0], "p2": [100, 0],
                 "text_pos": [50, 20], "rotation": 1.5708},
            ]
            with mock.patch.object(cad_module, "_backup_drawing", return_value="x.备份"), \
                 _registered_tools(client) as registry:
                result = await registry.call("cad_add_dimensions",
                                             {"path": str(path), "items": items})
        self.assertEqual(4, result["added"], result.get("failed"))
        kinds = [call[0] for call in doc.ModelSpace.add_dim_calls]
        self.assertEqual(["aligned", "radial", "angular", "rotated"], kinds)
        for call in doc.ModelSpace.add_dim_calls:
            kind = call[0]
            if kind == "aligned":
                _, p1, p2, pos = call
                for point in (p1, p2, pos):
                    self.assertIsInstance(point, _FakeVariant, f"{kind} 的点参必须是 VARIANT")
            elif kind == "radial":
                _, center, chord, leader = call
                self.assertIsInstance(center, _FakeVariant)
                self.assertIsInstance(chord, _FakeVariant)
                self.assertIsInstance(leader, float, "LeaderLength 是裸 float,不包 VARIANT")
            elif kind == "angular":
                _, vertex, p1, p2, pos = call
                for point in (vertex, p1, p2, pos):
                    self.assertIsInstance(point, _FakeVariant, f"{kind} 的点参必须是 VARIANT")
            elif kind == "rotated":
                _, p1, p2, pos, rot = call
                for point in (p1, p2, pos):
                    self.assertIsInstance(point, _FakeVariant, f"{kind} 的点参必须是 VARIANT")
                self.assertIsInstance(rot, float, "rotation 是裸 float,不包 VARIANT")


class CadDrawingToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_layers_and_basic_geometry_use_com_arrays_and_backup(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path), layers=[_CadLayer("0")])
            client = _CadClient(active=None, dispatch_doc=doc)
            with mock.patch.object(cad_module, "_backup_drawing", return_value="x.备份"), \
                 _registered_tools(client) as registry:
                listed = await registry.call("cad_list_layers", {"path": str(path)})
                created = await registry.call("cad_create_layer", {
                    "path": str(path), "name": "墙", "color": 1, "locked": True,
                })
                lines = await registry.call("cad_draw_line", {
                    "path": str(path), "items": [{"start": [0, 0], "end": [100, 0], "layer": "墙"}],
                })
                polylines = await registry.call("cad_draw_polyline", {
                    "path": str(path), "items": [{"points": [[0, 0], [100, 0], [100, 50]], "closed": True}],
                })
                rectangles = await registry.call("cad_draw_rectangle", {
                    "path": str(path), "items": [{"p1": [0, 0], "p2": [100, 50]}],
                })
                mtexts = await registry.call("cad_add_mtext", {
                    "path": str(path), "items": [{"text": "说明", "insertion": [10, 20], "height": 5}],
                })
        self.assertEqual(["0"], [layer["name"] for layer in listed["layers"]])
        self.assertTrue(created["created"])
        self.assertEqual(1, lines["added"])
        self.assertEqual(1, polylines["added"])
        self.assertEqual(1, rectangles["added"])
        self.assertEqual(1, mtexts["added"])
        self.assertIsInstance(doc.ModelSpace.add_polyline_raw_calls[0], _FakeVariant)
        self.assertEqual([0.0, 0.0, 100.0, 0.0, 100.0, 50.0],
                         list(doc.ModelSpace.add_polyline_calls[0]))
        self.assertIsInstance(doc.ModelSpace.add_line_calls[0][0], _FakeVariant)
        self.assertIsInstance(doc.ModelSpace.add_mtext_calls[0][0], _FakeVariant)
        self.assertEqual(5.0, doc.ModelSpace.added[-1].Height)

    async def test_geometry_validation_is_reported_without_saving_bad_item(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path))
            client = _CadClient(active=None, dispatch_doc=doc)
            with mock.patch.object(cad_module, "_backup_drawing", return_value="x.备份"), \
                 _registered_tools(client) as registry:
                result = await registry.call("cad_draw_line", {
                    "path": str(path), "items": [{"start": [1, 1], "end": [1, 1]}],
                })
        self.assertEqual(0, result["added"])
        self.assertEqual(1, len(result["failed"]))
        self.assertEqual([], doc.ModelSpace.add_line_calls)
        self.assertEqual(1, len(doc.saved_as))

    async def test_arc_ellipse_point_and_verify_tools(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path), modelspace=[_CadEntity("AcDbLine", layer="墙")])
            client = _CadClient(active=None, dispatch_doc=doc)
            with mock.patch.object(cad_module, "_backup_drawing", return_value="x.备份"), \
                 _registered_tools(client) as registry:
                arc = await registry.call("cad_draw_arc", {
                    "path": str(path),
                    "items": [{"center": [0, 0], "radius": 50, "start_angle": 0, "end_angle": 1.57}],
                })
                ellipse = await registry.call("cad_draw_ellipse", {
                    "path": str(path),
                    "items": [{"center": [10, 20], "major_axis": [100, 0], "ratio": 0.5}],
                })
                point = await registry.call("cad_draw_point", {
                    "path": str(path), "items": [{"point": [5, 6, 7]}],
                })
                verified = await registry.call("cad_verify_drawing", {
                    "path": str(path), "sample_limit": 2,
                })
        self.assertEqual(1, arc["added"])
        self.assertEqual(1, ellipse["added"])
        self.assertEqual(1, point["added"])
        self.assertEqual(4, verified["entity_total"])
        self.assertEqual(2, len(verified["sample"]))
        self.assertTrue(verified["sample_truncated"])
        self.assertIsInstance(doc.ModelSpace.add_arc_calls[0][0], _FakeVariant)
        self.assertIsInstance(doc.ModelSpace.add_ellipse_calls[0][0], _FakeVariant)
        self.assertIsInstance(doc.ModelSpace.add_ellipse_calls[0][1], _FakeVariant)
        self.assertIsInstance(doc.ModelSpace.add_point_calls[0], _FakeVariant)

    async def test_arc_and_ellipse_validation_skips_invalid_items(self):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path))
            client = _CadClient(active=None, dispatch_doc=doc)
            with mock.patch.object(cad_module, "_backup_drawing", return_value="x.备份"), \
                 _registered_tools(client) as registry:
                arc = await registry.call("cad_draw_arc", {
                    "path": str(path), "items": [{"center": [0, 0], "radius": 0,
                                                     "start_angle": 0, "end_angle": 1}],
                })
                ellipse = await registry.call("cad_draw_ellipse", {
                    "path": str(path), "items": [{"center": [0, 0], "major_axis": [0, 0], "ratio": 2}],
                })
        self.assertEqual(0, arc["added"])
        self.assertEqual(1, len(arc["failed"]))
        self.assertEqual(0, ellipse["added"])
        self.assertEqual(1, len(ellipse["failed"]))



class CadEntityEditTests(unittest.IsolatedAsyncioTestCase):
    TOOL_ARGS = {
        "cad_move_entities": {"offset": [10, 20, 30]},
        "cad_copy_entities": {"offset": [10, 20]},
        "cad_rotate_entities": {"base_point": [1, 1], "angle": math.pi / 2},
        "cad_scale_entities": {"base_point": [1, 1, 0], "factor": 2},
        "cad_mirror_entities": {"p1": [0, 0], "p2": [0, 10]},
        "cad_erase_entities": {},
        "cad_move_entities_to_layer": {"target_layer": "目标"},
        "cad_change_entity_properties": {"properties": {"color": 1}},
    }

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = _dwg_file(Path(self.temp.name))
        self.original = self.path.read_bytes()
        self.source = _CadEditableEntity("AcDbLine", layer="墙", position=(2, 3, 0))
        self.other = _CadEditableEntity("AcDbText", layer="注释", text="门窗", position=(99, 99))
        self.doc = _CadDocument(str(self.path), modelspace=[self.source, self.other],
                                layers=[_CadLayer("0"), _CadLayer("墙"), _CadLayer("注释"), _CadLayer("目标")])
        self.doc.Linetypes = _CadCollection([_CadStyle("Continuous"), _CadStyle("DASHED")])
        for entity in self.doc.ModelSpace.entities:
            entity._space = self.doc.ModelSpace
        self.doc.Close = mock.Mock(wraps=self.doc.Close)

    def args(self, name, **kwargs):
        return {"path": str(self.path), "layer": "墙", **self.TOOL_ARGS[name], **kwargs}

    @contextmanager
    def tools(self, **client_args):
        client = _CadClient(active=None, dispatch_doc=self.doc, **client_args)
        with mock.patch.object(cad_module, "_backup_drawing", return_value="原图.备份.dwg") as backup, \
             _registered_tools(client) as registry:
            yield registry, backup, client

    async def test_all_eight_default_to_read_only_preview(self):
        with self.tools() as (registry, backup, client):
            for name in self.TOOL_ARGS:
                with self.subTest(tool=name):
                    result = await registry.call(name, self.args(name))
                    self.assertTrue(result["dry_run"])
                    self.assertEqual(1, result["matched"])
                    self.assertEqual(0, result["changed"])
                    self.assertEqual("A1", result["preview"][0]["handle"])
                    self.assertIsNone(result["backup"])
                    self.assertIsNone(result["saved_as"])
                    self.assertIn("未修改", result["summary"])
                    json.dumps(result, ensure_ascii=False, allow_nan=False)
                    self.assertTrue(registry.get(name).side_effect)
                    schema = registry.get(name).parameters
                    self.assertTrue(schema["properties"]["dry_run"]["default"])
            backup.assert_not_called()
        self.assertEqual([], self.source.calls)
        self.assertEqual(256, self.source.Color)
        self.assertEqual("墙", self.source.Layer)
        self.assertEqual(2, self.doc.ModelSpace.Count)
        self.assertEqual([], self.doc.saved_as)
        self.assertEqual(0, self.doc.save_called)
        self.assertEqual(self.original, self.path.read_bytes())

    async def test_all_eight_require_actual_boolean_for_dry_run(self):
        with self.tools() as (registry, backup, client):
            for name in self.TOOL_ARGS:
                for value in (None, 0, 1, "false", "true", [], {}):
                    with self.subTest(tool=name, value=value), self.assertRaisesRegex(ValueError, "dry_run"):
                        await registry.call(name, self.args(name, dry_run=value))
            backup.assert_not_called()
            self.assertEqual(0, client.dispatched)

    async def test_empty_or_malformed_filters_are_rejected_before_open(self):
        bad_filters = [
            {}, {"layer": " "}, {"layer": None}, {"types": "*"}, {"types": "LINE,"},
            {"types": 12}, {"text": " "}, {"text": 12}, {"area": []},
            {"area": [0, 0, 1]}, {"area": [0, 0, "x", 1]}, {"area": [0, 0, float("nan"), 1]},
            {"area": [0, 0, float("inf"), 1]}, {"area": [0, False, 1, 1]},
        ]
        with self.tools() as (registry, backup, client):
            for name in self.TOOL_ARGS:
                for filters in bad_filters:
                    with self.subTest(tool=name, filters=filters), self.assertRaises(ValueError):
                        await registry.call(name, self.args(name, **{"layer": "", "dry_run": False, **filters}))
            backup.assert_not_called()
            self.assertEqual(0, client.dispatched)

    async def test_filter_intersection_type_alias_bbox_and_text(self):
        crossed = _CadEditableEntity("AcDbLine", layer="墙", text="Door A", position=(-100, 2),
                                      bbox=((-100, 2), (100, 2)))
        polyline = _CadPolyline([0, 0, 10, 10], layer="墙")
        self.doc.ModelSpace.entities.extend([crossed, polyline])
        with self.tools() as (registry, backup, _):
            result = await registry.call("cad_move_entities", self.args("cad_move_entities",
                types="LINE", area=[10, 10, 0, 0], text="Door"))
            self.assertEqual(1, result["matched"])
            self.assertEqual("Door A", result["preview"][0]["text"])
            self.assertEqual([0.0, 0.0, 10.0, 10.0], result["filters"]["area"])
            for types, count in (("LINE", 2), ("AcDbLine", 2), ("POLYLINE", 1), ("Text", 0), ("LIN", 0)):
                result = await registry.call("cad_erase_entities", self.args("cad_erase_entities", types=types))
                self.assertEqual(count, result["matched"], types)
            result = await registry.call("cad_erase_entities", self.args("cad_erase_entities", text="door"))
            self.assertEqual(0, result["matched"])
            backup.assert_not_called()

    async def test_area_never_uses_fabricated_origin_for_unknown_entity(self):
        self.doc.ModelSpace.entities = [SimpleNamespace(ObjectName="AcDbProxyEntity", Layer="墙")]
        with self.tools() as (registry, backup, _):
            result = await registry.call("cad_erase_entities", self.args("cad_erase_entities", area=[-1, -1, 1, 1], dry_run=False))
            self.assertEqual(0, result["matched"])
            backup.assert_not_called()
        self.assertEqual([], self.doc.saved_as)

    async def test_move_uses_variant_coordinates_and_backup_before_change(self):
        with self.tools() as (registry, backup, client):
            backup.side_effect = lambda path: self.assertEqual([], self.source.calls) or "原图.备份.dwg"
            result = await registry.call("cad_move_entities", self.args("cad_move_entities", dry_run=False))
            backup.assert_called_once_with(self.path)
        self.assertEqual((12.0, 23.0, 30.0), self.source.StartPoint)
        method, start, end = self.source.calls[0]
        self.assertEqual("Move", method)
        for point in (start, end):
            self.assertIsInstance(point, _FakeVariant)
            self.assertEqual(0x2005, point.vt)
        self.assertEqual((0.0, 0.0, 0.0), _unwrap(start))
        self.assertEqual((10.0, 20.0, 30.0), _unwrap(end))
        self.assertEqual([], self.other.calls)
        self.assertEqual(1, result["changed"])
        self.assertFalse(result["dry_run"])
        self.assertEqual("原图.备份.dwg", result["backup"])
        self.assertIn("AutoCAD 2013", result["saved_as"])
        self.assertIn("原图.备份.dwg", result["summary"])
        self.assertEqual([(str(self.path), 60)], self.doc.saved_as)
        self.doc.Close.assert_called_once_with(False)
        self.assertTrue(client.dispatch_app.quit_called)
        json.dumps(result, ensure_ascii=False, allow_nan=False)

    async def test_copy_snapshot_retains_originals_and_moves_only_new_entities(self):
        self.other.Layer = "墙"
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_copy_entities", self.args("cad_copy_entities", dry_run=False))
        self.assertEqual(2, result["changed"])
        self.assertEqual(4, self.doc.ModelSpace.Count)
        self.assertEqual((2, 3, 0), self.source.StartPoint)
        self.assertEqual((12.0, 23.0, 0.0), self.doc.ModelSpace.entities[2].StartPoint)
        self.assertEqual((109.0, 119.0, 0.0), self.doc.ModelSpace.entities[3].StartPoint)
        self.assertEqual([("Copy",)], self.source.calls)
        self.assertEqual(1, len(self.doc.saved_as))

    async def test_rotate_uses_radians_about_supplied_base(self):
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_rotate_entities", self.args("cad_rotate_entities", dry_run=False))
        self.assertAlmostEqual(-1, self.source.StartPoint[0])
        self.assertAlmostEqual(2, self.source.StartPoint[1])
        _, base, angle = self.source.calls[0]
        self.assertIsInstance(base, _FakeVariant)
        self.assertEqual((1, 1, 0), _unwrap(base))
        self.assertEqual(math.pi / 2, angle)
        self.assertEqual(1, result["changed"])

    async def test_scale_uses_positive_factor_about_supplied_base(self):
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_scale_entities", self.args("cad_scale_entities", dry_run=False))
        self.assertEqual((3, 5, 0), self.source.StartPoint)
        self.assertEqual("ScaleEntity", self.source.calls[0][0])
        self.assertIsInstance(self.source.calls[0][1], _FakeVariant)
        self.assertEqual(2, self.source.calls[0][2])
        self.assertEqual(1, result["changed"])

    async def test_mirror_defaults_to_retaining_source(self):
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_mirror_entities", self.args("cad_mirror_entities", dry_run=False))
        self.assertEqual(3, self.doc.ModelSpace.Count)
        self.assertEqual((2, 3, 0), self.source.StartPoint)
        self.assertEqual((-2, 3, 0), self.doc.ModelSpace.entities[-1].StartPoint)
        _, p1, p2 = self.source.calls[0]
        self.assertIsInstance(p1, _FakeVariant)
        self.assertIsInstance(p2, _FakeVariant)
        self.assertFalse(result["parameters"]["erase_source"])
        self.assertEqual(1, result["changed"])

    async def test_mirror_can_explicitly_erase_source_after_copy(self):
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_mirror_entities", self.args("cad_mirror_entities", erase_source=True, dry_run=False))
        self.assertEqual(2, self.doc.ModelSpace.Count)
        self.assertNotIn(self.source, self.doc.ModelSpace.entities)
        self.assertEqual(["Mirror", "Delete"], [call[0] for call in self.source.calls])
        self.assertTrue(result["parameters"]["erase_source"])

    async def test_erase_snapshot_does_not_skip_entities_as_count_shrinks(self):
        self.other.Layer = "墙"
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_erase_entities", self.args("cad_erase_entities", dry_run=False))
        self.assertEqual(2, result["changed"])
        self.assertEqual(0, self.doc.ModelSpace.Count)
        self.assertEqual([("Delete",)], self.source.calls)
        self.assertEqual([("Delete",)], self.other.calls)

    async def test_move_to_layer_and_change_all_supported_properties(self):
        properties = {"layer": "目标", "color": 3, "linetype": "DASHED", "linetype_scale": 2,
                      "lineweight": 25, "visible": False}
        with self.tools() as (registry, backup, _):
            preview = await registry.call("cad_change_entity_properties", self.args("cad_change_entity_properties", properties=properties))
            self.assertEqual(256, preview["preview"][0]["properties_before"]["color"])
            self.assertEqual(256, self.source.Color)
            moved = await registry.call("cad_move_entities_to_layer", self.args("cad_move_entities_to_layer", dry_run=False))
            self.assertEqual("目标", self.source.Layer)
            changed = await registry.call("cad_change_entity_properties", self.args("cad_change_entity_properties", layer="目标", properties=properties, dry_run=False))
            self.assertEqual(2, backup.call_count)
        for key, value in properties.items():
            self.assertEqual(value, getattr(self.source, cad_module._ENTITY_PROPERTIES[key]))
        self.assertEqual("墙", moved["preview"][0]["properties_before"]["layer"])
        self.assertEqual(1, changed["changed"])
        self.assertEqual(256, self.other.Color)
        json.dumps(changed, ensure_ascii=False, allow_nan=False)

    async def test_invalid_transforms_are_rejected_before_open(self):
        cases = [
            ("cad_move_entities", {"offset": [0, 0]}),
            ("cad_move_entities", {"offset": [1]}),
            ("cad_copy_entities", {"offset": [1, 2, 3, 4]}),
            ("cad_copy_entities", {"offset": [1, float("nan")]}),
            ("cad_rotate_entities", {"angle": float("inf")}),
            ("cad_rotate_entities", {"angle": "1"}),
            ("cad_rotate_entities", {"base_point": None}),
            ("cad_scale_entities", {"factor": 0}), ("cad_scale_entities", {"factor": -1}),
            ("cad_scale_entities", {"factor": True}),
            ("cad_mirror_entities", {"p2": [0, 0]}),
            ("cad_mirror_entities", {"p2": [0, 0, 2]}),
            ("cad_mirror_entities", {"p2": [1, 1, 2]}),
            ("cad_mirror_entities", {"erase_source": "false"}),
        ]
        with self.tools() as (registry, backup, client):
            for name, args in cases:
                with self.subTest(tool=name, args=args), self.assertRaises(ValueError):
                    await registry.call(name, self.args(name, dry_run=False, **args))
            backup.assert_not_called()
            self.assertEqual(0, client.dispatched)

    async def test_invalid_property_values_are_rejected_before_open(self):
        cases = [None, {}, {"Delete": True}, {"color": -1}, {"color": 257}, {"color": True},
                 {"color": 1.5}, {"color": "2"}, {"visible": 1}, {"visible": "false"},
                 {"linetype_scale": 0}, {"linetype_scale": float("nan")}, {"layer": " "},
                 {"linetype": ""}, {"lineweight": 26}, {"lineweight": True}, {"lineweight": 25.0}]
        with self.tools() as (registry, backup, client):
            for properties in cases:
                with self.subTest(properties=properties), self.assertRaises(ValueError):
                    await registry.call("cad_change_entity_properties", self.args("cad_change_entity_properties", properties=properties, dry_run=False))
            backup.assert_not_called()
            self.assertEqual(0, client.dispatched)

    async def test_missing_target_layer_or_unloaded_linetype_never_saves(self):
        with self.tools() as (registry, backup, _):
            for name, args in (("cad_move_entities_to_layer", {"target_layer": "不存在"}),
                               ("cad_change_entity_properties", {"properties": {"layer": "不存在"}}),
                               ("cad_change_entity_properties", {"properties": {"linetype": "NOT_LOADED"}})):
                with self.subTest(tool=name), self.assertRaises(ValueError):
                    await registry.call(name, self.args(name, dry_run=False, **args))
            backup.assert_not_called()
        self.assertEqual([], self.doc.saved_as)

    async def test_no_matches_never_backup_or_save(self):
        with self.tools() as (registry, backup, _):
            for name in self.TOOL_ARGS:
                result = await registry.call(name, self.args(name, types="CIRCLE", dry_run=False))
                self.assertEqual(0, result["matched"])
                self.assertEqual(0, result["changed"])
                self.assertIsNone(result["backup"])
                self.assertIsNone(result["saved_as"])
            backup.assert_not_called()
        self.assertEqual([], self.doc.saved_as)
        self.assertEqual([], self.source.calls)

    async def test_all_eight_refuse_user_open_drawing_but_allow_preview(self):
        app = _CadApplication(docs=[self.doc])
        client = _CadClient(active=app)
        with _registered_tools(client) as registry, mock.patch.object(cad_module, "_backup_drawing") as backup:
            for name in self.TOOL_ARGS:
                with self.subTest(tool=name), self.assertRaisesRegex(RuntimeError, "正开在你的 AutoCAD"):
                    await registry.call(name, self.args(name, dry_run=False))
                result = await registry.call(name, self.args(name))
                self.assertTrue(result["dry_run"])
            backup.assert_not_called()
        self.doc.Close.assert_not_called()
        self.assertFalse(app.quit_called)
        self.assertEqual([], self.source.calls)

    async def test_all_eight_refuse_preexisting_file_lock_but_allow_preview(self):
        self.path.with_suffix(".dwl2").write_text("占用", encoding="utf-8")
        with self.tools() as (registry, backup, _):
            for name in self.TOOL_ARGS:
                with self.subTest(tool=name), self.assertRaisesRegex(RuntimeError, "锁文件"):
                    await registry.call(name, self.args(name, dry_run=False))
                result = await registry.call(name, self.args(name))
                self.assertTrue(result["dry_run"])
            backup.assert_not_called()
        self.assertEqual([], self.source.calls)
        self.assertEqual([], self.doc.saved_as)

    async def test_all_eight_refuse_readonly_drawing(self):
        with self.tools() as (registry, backup, _), mock.patch.object(cad_module.os, "access", return_value=False):
            for name in self.TOOL_ARGS:
                with self.subTest(tool=name), self.assertRaisesRegex(RuntimeError, "只读"):
                    await registry.call(name, self.args(name, dry_run=False))
            backup.assert_not_called()
        self.assertEqual([], self.source.calls)

    async def test_all_eight_stop_on_backup_failure_before_mutation(self):
        with self.tools() as (registry, backup, _):
            backup.side_effect = RuntimeError("备份失败")
            for name in self.TOOL_ARGS:
                with self.subTest(tool=name), self.assertRaisesRegex(RuntimeError, "备份失败"):
                    await registry.call(name, self.args(name, dry_run=False))
        self.assertEqual([], self.source.calls)
        self.assertEqual(256, self.source.Color)
        self.assertEqual("墙", self.source.Layer)
        self.assertEqual([], self.doc.saved_as)
        self.assertEqual(8, self.doc.closed)

    async def test_own_lock_created_on_open_is_not_treated_as_preexisting(self):
        with self.tools(lock_on_open=True) as (registry, backup, _):
            result = await registry.call("cad_move_entities", self.args("cad_move_entities", dry_run=False))
            backup.assert_called_once()
        self.assertEqual(1, result["changed"])
        self.assertTrue(self.path.with_suffix(".dwl").exists())

    async def test_locked_source_or_target_layer_refuses_before_backup(self):
        source = cad_module._find_layer(self.doc, "墙")
        target = cad_module._find_layer(self.doc, "目标")
        with self.tools() as (registry, backup, _):
            source.Lock = True
            for name in self.TOOL_ARGS:
                with self.subTest(tool=name), self.assertRaisesRegex(RuntimeError, "锁定"):
                    await registry.call(name, self.args(name, dry_run=False))
            source.Lock, target.Lock = False, True
            with self.assertRaisesRegex(RuntimeError, "锁定"):
                await registry.call("cad_move_entities_to_layer", self.args("cad_move_entities_to_layer", dry_run=False))
            backup.assert_not_called()
        self.assertEqual([], self.source.calls)

    async def test_missing_file_fails_for_all_eight(self):
        with self.tools() as (registry, backup, client):
            for name in self.TOOL_ARGS:
                with self.subTest(tool=name), self.assertRaisesRegex(RuntimeError, "图纸不存在"):
                    await registry.call(name, self.args(name, path=str(self.path.with_name("missing.dwg")), dry_run=False))
            self.assertEqual(0, client.dispatched)
            backup.assert_not_called()

    async def test_selection_com_error_is_not_silently_treated_as_empty(self):
        with self.tools() as (registry, backup, _), mock.patch.object(self.doc.ModelSpace, "Item", side_effect=RuntimeError("读取失败")):
            with self.assertRaisesRegex(RuntimeError, "读取失败"):
                await registry.call("cad_erase_entities", self.args("cad_erase_entities", dry_run=False))
            backup.assert_not_called()
        self.assertEqual([], self.doc.saved_as)
        self.doc.Close.assert_called_once_with(False)

    async def test_mid_batch_failure_never_saves_partial_changes(self):
        self.other.Layer = "墙"
        self.other.Move = mock.Mock(side_effect=RuntimeError("变换失败"))
        with self.tools() as (registry, backup, _):
            with self.assertRaisesRegex(RuntimeError, "备份:原图.备份.dwg"):
                await registry.call("cad_move_entities", self.args("cad_move_entities", dry_run=False))
            backup.assert_called_once()
        self.assertEqual("Move", self.source.calls[0][0])
        self.assertEqual([], self.doc.saved_as)
        self.assertEqual(0, self.doc.save_called)
        self.doc.Close.assert_called_once_with(False)
        self.assertEqual(self.original, self.path.read_bytes())

    async def test_copy_failure_after_duplication_does_not_save_or_delete_original(self):
        original_copy = self.source._copy
        def broken_copy_move():
            result = original_copy()
            result.Move = mock.Mock(side_effect=RuntimeError("移动副本失败"))
            return result
        self.source.Copy = broken_copy_move
        with self.tools() as (registry, _, _):
            with self.assertRaisesRegex(RuntimeError, "移动副本失败"):
                await registry.call("cad_copy_entities", self.args("cad_copy_entities", dry_run=False))
        self.assertIn(self.source, self.doc.ModelSpace.entities)
        self.assertEqual([], self.doc.saved_as)
        self.doc.Close.assert_called_once_with(False)

    async def test_mirror_failure_never_deletes_source(self):
        self.source.Mirror = mock.Mock(side_effect=RuntimeError("镜像失败"))
        with self.tools() as (registry, _, _):
            with self.assertRaisesRegex(RuntimeError, "镜像失败"):
                await registry.call("cad_mirror_entities", self.args("cad_mirror_entities", erase_source=True, dry_run=False))
        self.assertEqual([], self.source.calls)
        self.assertIn(self.source, self.doc.ModelSpace.entities)
        self.assertEqual([], self.doc.saved_as)

    async def test_busy_transform_retries_without_repeating_whole_batch(self):
        self.source.Move = mock.Mock(side_effect=[_BUSY, None])
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_move_entities", self.args("cad_move_entities", dry_run=False))
        self.assertEqual(2, self.source.Move.call_count)
        self.assertEqual(1, result["changed"])

    async def test_save_failure_reports_backup_and_closes_without_saving(self):
        self.doc.saveas_error = RuntimeError("SaveAs failed")
        self.doc.Save = mock.Mock(side_effect=RuntimeError("Save failed"))
        with self.tools() as (registry, backup, _):
            with self.assertRaisesRegex(RuntimeError, "原图.备份.dwg"):
                await registry.call("cad_move_entities", self.args("cad_move_entities", dry_run=False))
            backup.assert_called_once()
        self.doc.Close.assert_called_once_with(False)
        self.assertEqual(self.original, self.path.read_bytes())

    async def test_preview_limit_does_not_limit_execution(self):
        self.doc.ModelSpace.entities = [
            _CadEditableEntity("AcDbLine", layer="墙", position=(i, 0))
            for i in range(cad_module._MAX_DETAIL + 5)
        ]
        for entity in self.doc.ModelSpace.entities:
            entity._space = self.doc.ModelSpace
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_erase_entities", self.args("cad_erase_entities", dry_run=False))
        self.assertEqual(cad_module._MAX_DETAIL, len(result["preview"]))
        self.assertTrue(result["truncated"])
        self.assertEqual(cad_module._MAX_DETAIL + 5, result["changed"])
        self.assertEqual(0, self.doc.ModelSpace.Count)


class _CadEngineeringEntity(_CadEditableEntity):
    """Only implements real ActiveX APIs; no invented Trim/Fillet/Join methods."""
    def __init__(self, start=(0, 0, 0), end=(10, 0, 0), object_name="AcDbLine", layer="编辑", handle="E1"):
        super().__init__(object_name, layer=layer, position=start)
        self.StartPoint, self.EndPoint = tuple(start), tuple(end)
        self.Handle = handle
        self.HasAttributes = False
        self.Thickness = 0.0
        self.offset_result = "auto"
        self.explode_result = "auto"

    def __setattr__(self, name, value):
        if name in ("StartPoint", "EndPoint") and isinstance(value, _FakeVariant):
            self.calls.append(("set", name, value))
            value = _unwrap(value)
        super().__setattr__(name, value)

    def Offset(self, distance):
        self.calls.append(("Offset", distance))
        if self.offset_result != "auto":
            return self.offset_result
        result = self._copy()
        result.StartPoint = tuple(p + (distance if i == 1 else 0) for i, p in enumerate(self.StartPoint))
        result.EndPoint = tuple(p + (distance if i == 1 else 0) for i, p in enumerate(self.EndPoint))
        return (result,)

    def Explode(self):
        self.calls.append(("Explode",))
        if self.explode_result != "auto":
            return self.explode_result
        first, second = self._copy(), self._copy()
        first.ObjectName = second.ObjectName = "AcDbLine"
        return (first, second)


class _CadEngineeringPolyline(_CadPolyline):
    def __setattr__(self, name, value):
        if name in ("Coordinates", "Normal") and isinstance(value, _FakeVariant):
            value = _unwrap(value)
        super().__setattr__(name, value)


class _CadEngineeringModelSpace(_CadModelSpace):
    def AddLightWeightPolyline(self, points):
        self.add_polyline_raw_calls.append(points)
        points = _unwrap(points)
        self.add_polyline_calls.append(points)
        entity = _CadEngineeringPolyline(points, closed=False)
        self.added.append(entity)
        return entity


class CadEngineeringEditTests(unittest.IsolatedAsyncioTestCase):
    TOOL_ARGS = {
        "cad_offset": {"distance": 2},
        "cad_trim": {"boundary_start": [5, -5], "boundary_end": [5, 5]},
        "cad_extend": {"boundary_start": [20, -5], "boundary_end": [20, 5]},
        "cad_fillet": {"radius": 2, "ends": ["end", "start"]},
        "cad_chamfer": {"distance1": 2, "distance2": 3, "ends": ["end", "start"]},
        "cad_join_entities": {},
        "cad_explode": {},
    }

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = _dwg_file(Path(self.temp.name))
        self.original = self.path.read_bytes()
        self.configure("cad_offset")

    def configure(self, name, entities=None):
        if entities is None:
            first = _CadEngineeringEntity()
            entities = [first]
            if name in ("cad_fillet", "cad_chamfer", "cad_join_entities"):
                entities.append(_CadEngineeringEntity(start=(10, 0, 0), end=(10, 10, 0), handle="E2"))
            if name == "cad_explode":
                first.ObjectName = "AcDbPolyline"
        self.doc = _CadDocument(str(self.path), modelspace=entities,
                                layers=[_CadLayer("0"), _CadLayer("编辑"), _CadLayer("其他")])
        self.doc.ModelSpace = _CadEngineeringModelSpace(entities)
        for entity in entities:
            entity._space = self.doc.ModelSpace
        self.doc.Close = mock.Mock(wraps=self.doc.Close)
        return entities

    def args(self, name, **kwargs):
        return {"path": str(self.path), "layer": "编辑", **self.TOOL_ARGS[name], **kwargs}

    @contextmanager
    def tools(self, active=None, **kwargs):
        client = _CadClient(active=active, dispatch_doc=self.doc, **kwargs)
        with mock.patch.object(cad_module, "_backup_drawing", return_value="工程.备份.dwg") as backup, \
             _registered_tools(client) as registry:
            yield registry, backup, client

    def assert_unsaved(self):
        self.assertEqual([], self.doc.saved_as)
        self.assertEqual(0, self.doc.save_called)
        self.assertEqual(self.original, self.path.read_bytes())

    async def test_all_seven_default_to_preview_without_mutation(self):
        for name in self.TOOL_ARGS:
            with self.subTest(tool=name):
                sources = self.configure(name)
                before = [(e.StartPoint, e.EndPoint) for e in sources]
                with self.tools() as (registry, backup, _):
                    result = await registry.call(name, self.args(name))
                    backup.assert_not_called()
                    self.assertTrue(registry.get(name).side_effect)
                    self.assertTrue(registry.get(name).parameters["properties"]["dry_run"]["default"])
                self.assertTrue(result["dry_run"])
                self.assertEqual(len(sources), result["matched"])
                self.assertEqual(0, result["changed"])
                self.assertIsNone(result["backup"])
                self.assertIsNone(result["saved_as"])
                json.dumps(result, ensure_ascii=False, allow_nan=False)
                self.assertTrue(all(not e.calls for e in sources))
                self.assertEqual(before, [(e.StartPoint, e.EndPoint) for e in sources])
                self.assertEqual(len(sources), self.doc.ModelSpace.Count)
                self.assert_unsaved()
                self.doc.Close.assert_called_once_with(False)

    async def test_all_seven_execute_with_backup_original_format_and_summary(self):
        for name in self.TOOL_ARGS:
            with self.subTest(tool=name):
                sources = self.configure(name)
                with self.tools() as (registry, backup, _):
                    backup.side_effect = lambda p: self.assertTrue(all(not e.calls for e in sources)) or "工程.备份.dwg"
                    result = await registry.call(name, self.args(name, dry_run=False))
                    backup.assert_called_once_with(self.path)
                self.assertFalse(result["dry_run"])
                self.assertTrue(result["ok"])
                self.assertEqual(len(sources), result["changed"])
                self.assertEqual("工程.备份.dwg", result["backup"])
                self.assertEqual([(str(self.path), cad_module._DWG_HEADER_TO_SAVETYPE["AC1027"])], self.doc.saved_as)
                self.assertIn("已保存", result["summary"])
                json.dumps(result, ensure_ascii=False, allow_nan=False)
                self.doc.Close.assert_called_once_with(False)

    async def test_invalid_numbers_and_options_fail_before_com(self):
        bad = [("cad_offset", {"distance": x}) for x in (0, True, None, "2", float("nan"), float("inf"))]
        bad += [("cad_fillet", {"radius": x}) for x in (0, -1, True, None, float("inf"))]
        bad += [("cad_chamfer", {"distance1": x}) for x in (0, -1, False, None)]
        bad += [("cad_join_entities", {"tolerance": x}) for x in (0, -1, True, float("nan"))]
        bad += [("cad_explode", {"erase_source": x}) for x in (0, None, "false")]
        bad += [("cad_trim", {"end": x}) for x in (None, "END", 1, [])]
        bad += [("cad_fillet", {"ends": x}) for x in ([], "end", ["start"], ["foo", "end"])]
        bad += [("cad_extend", {"boundary_start": x}) for x in ([1], [0, True], [0, float("inf")])]
        bad += [("cad_trim", {"boundary_end": [5, -5]})]
        with self.tools() as (registry, backup, client):
            for name, kwargs in bad:
                with self.subTest(tool=name, kwargs=kwargs), self.assertRaises(ValueError):
                    await registry.call(name, self.args(name, **kwargs))
            backup.assert_not_called()
            self.assertEqual(0, client.dispatched)

    async def test_all_seven_reject_non_boolean_and_empty_filters_before_com(self):
        with self.tools() as (registry, backup, client):
            for name in self.TOOL_ARGS:
                for value in (0, None, "false", [], {}):
                    with self.subTest(tool=name, value=value), self.assertRaisesRegex(ValueError, "dry_run"):
                        await registry.call(name, self.args(name, dry_run=value))
                with self.assertRaisesRegex(ValueError, "筛选"):
                    await registry.call(name, self.args(name, layer=""))
            backup.assert_not_called()
            self.assertEqual(0, client.dispatched)

    async def test_all_seven_no_match_do_not_save_or_backup(self):
        for name in self.TOOL_ARGS:
            self.configure(name)
            with self.tools() as (registry, backup, _):
                result = await registry.call(name, self.args(name, layer="不存在", dry_run=False))
                self.assertEqual(0, result["matched"])
                self.assertEqual(0, result["changed"])
                backup.assert_not_called()
                self.assert_unsaved()

    async def test_all_seven_reject_missing_file(self):
        with self.tools() as (registry, backup, client):
            for name in self.TOOL_ARGS:
                with self.assertRaisesRegex(RuntimeError, "图纸不存在"):
                    await registry.call(name, self.args(name, path=str(self.path.with_name("missing.dwg")), dry_run=False))
            backup.assert_not_called()
            self.assertEqual(0, client.dispatched)

    async def test_all_seven_protect_users_open_drawing(self):
        for name in self.TOOL_ARGS:
            sources = self.configure(name)
            app = _CadApplication(docs=[self.doc])
            with self.tools(active=app) as (registry, backup, _):
                with self.assertRaisesRegex(RuntimeError, "正开在你的 AutoCAD"):
                    await registry.call(name, self.args(name, dry_run=False))
                backup.assert_not_called()
            self.assertTrue(all(not e.calls for e in sources))
            self.doc.Close.assert_not_called()
            self.assertFalse(app.quit_called)
            self.assert_unsaved()

    async def test_all_seven_user_open_preview_keeps_drawing_open(self):
        for name in self.TOOL_ARGS:
            self.configure(name)
            app = _CadApplication(docs=[self.doc])
            with self.tools(active=app) as (registry, backup, _):
                result = await registry.call(name, self.args(name))
                self.assertEqual(0, result["changed"])
                backup.assert_not_called()
            self.doc.Close.assert_not_called()
            self.assertFalse(app.quit_called)

    async def test_all_seven_preexisting_locks_stop_before_backup(self):
        for name in self.TOOL_ARGS:
            sources = self.configure(name)
            for suffix in (".dwl", ".dwl2"):
                lock = self.path.with_suffix(suffix)
                lock.write_text("existing", encoding="utf-8")
                try:
                    with self.tools() as (registry, backup, _):
                        with self.assertRaisesRegex(RuntimeError, "锁文件"):
                            await registry.call(name, self.args(name, dry_run=False))
                        backup.assert_not_called()
                    self.assertTrue(all(not e.calls for e in sources))
                    self.assert_unsaved()
                finally:
                    lock.unlink()

    async def test_all_seven_readonly_stop_before_backup(self):
        for name in self.TOOL_ARGS:
            sources = self.configure(name)
            with self.tools() as (registry, backup, _), mock.patch.object(cad_module.os, "access", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "只读"):
                    await registry.call(name, self.args(name, dry_run=False))
                backup.assert_not_called()
            self.assertTrue(all(not e.calls for e in sources))
            self.assert_unsaved()

    async def test_all_seven_locked_layers_stop_before_backup(self):
        for name in self.TOOL_ARGS:
            sources = self.configure(name)
            self.doc.Layers.items[1].Lock = True
            with self.tools() as (registry, backup, _):
                with self.assertRaisesRegex(RuntimeError, "锁定"):
                    await registry.call(name, self.args(name, dry_run=False))
                backup.assert_not_called()
            self.assertTrue(all(not e.calls for e in sources))
            self.assert_unsaved()

    async def test_all_seven_backup_failure_prevents_mutations(self):
        for name in self.TOOL_ARGS:
            sources = self.configure(name)
            with self.tools() as (registry, backup, _):
                backup.side_effect = RuntimeError("备份失败")
                with self.assertRaisesRegex(RuntimeError, "备份失败"):
                    await registry.call(name, self.args(name, dry_run=False))
            self.assertTrue(all(not e.calls for e in sources))
            self.assert_unsaved()
            self.doc.Close.assert_called_once_with(False)

    async def test_all_seven_save_failures_report_backup(self):
        for name in self.TOOL_ARGS:
            self.configure(name)
            self.doc.saveas_error = RuntimeError("SaveAs failed")
            self.doc.Save = mock.Mock(side_effect=RuntimeError("Save failed"))
            with self.tools() as (registry, _, _):
                with self.assertRaisesRegex(RuntimeError, "工程.备份.dwg"):
                    await registry.call(name, self.args(name, dry_run=False))
            self.doc.Close.assert_called_once_with(False)
            self.assertEqual(self.original, self.path.read_bytes())

    async def test_offset_keeps_original_and_passes_signed_distance(self):
        source = self.doc.ModelSpace.entities[0]
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_offset", self.args("cad_offset", distance=-2, dry_run=False))
        self.assertEqual([("Offset", -2.0)], source.calls)
        self.assertEqual((0, 0, 0), source.StartPoint)
        self.assertIn(source, self.doc.ModelSpace.entities)
        self.assertEqual(1, result["created"])
        self.assertEqual(2, self.doc.ModelSpace.Count)
        self.assertEqual((0, -2, 0), self.doc.ModelSpace.entities[1].StartPoint)

    async def test_offset_rejects_missing_com_method_during_preview(self):
        source = self.doc.ModelSpace.entities[0]
        source.Offset = None
        with self.tools() as (registry, backup, _):
            with self.assertRaisesRegex(RuntimeError, "不支持 Offset"):
                await registry.call("cad_offset", self.args("cad_offset"))
            backup.assert_not_called()
        self.assert_unsaved()

    async def test_offset_and_explode_reject_empty_or_invalid_results(self):
        for name, attr in (("cad_offset", "offset_result"), ("cad_explode", "explode_result")):
            for value in (None, [], (), "invalid", 1):
                self.configure(name)
                source = self.doc.ModelSpace.entities[0]
                setattr(source, attr, value)
                with self.tools() as (registry, _, _):
                    with self.assertRaisesRegex(RuntimeError, "无法验证"):
                        await registry.call(name, self.args(name, dry_run=False))
                self.assertIn(source, self.doc.ModelSpace.entities)
                self.assert_unsaved()

    async def test_offset_failure_on_second_entity_discards_partial_batch(self):
        sources = self.configure("cad_offset", [_CadEngineeringEntity(), _CadEngineeringEntity(handle="E2")])
        sources[1].Offset = mock.Mock(side_effect=RuntimeError("unsupported version"))
        with self.tools() as (registry, _, _):
            with self.assertRaisesRegex(RuntimeError, "AutoCAD COM Offset.*未保存"):
                await registry.call("cad_offset", self.args("cad_offset", dry_run=False))
        self.assertEqual(3, self.doc.ModelSpace.Count)
        self.assert_unsaved()
        self.doc.Close.assert_called_once_with(False)

    async def test_busy_offset_retries_only_current_call(self):
        source = self.doc.ModelSpace.entities[0]
        offset = source.Offset
        def busy_once(distance):
            if source.Offset.call_count == 1:
                raise _BUSY
            return offset(distance)
        source.Offset = mock.Mock(side_effect=busy_once)
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_offset", self.args("cad_offset", dry_run=False))
        self.assertEqual(2, source.Offset.call_count)
        self.assertEqual(1, result["changed"])

    async def test_trim_end_and_start_change_only_named_endpoint(self):
        for end, expected in (("end", ((0, 0, 0), (5, 0, 0))), ("start", ((5, 0, 0), (10, 0, 0)))):
            source = self.configure("cad_trim")[0]
            with self.tools() as (registry, _, _):
                result = await registry.call("cad_trim", self.args("cad_trim", end=end, dry_run=False))
            self.assertEqual(expected, (source.StartPoint, source.EndPoint))
            self.assertEqual([5, 0, 0], result["preview"][0]["endpoint_after"])
            self.assertIsInstance(source.calls[0][2], _FakeVariant)

    async def test_extend_end_and_start(self):
        for end, x in (("end", 20), ("start", -5)):
            source = self.configure("cad_extend")[0]
            with self.tools() as (registry, _, _):
                await registry.call("cad_extend", self.args("cad_extend", end=end,
                                    boundary_start=[x, -5], boundary_end=[x, 5], dry_run=False))
            self.assertEqual((x, 0, 0), source.EndPoint if end == "end" else source.StartPoint)

    async def test_boundary_geometry_rejects_parallel_outside_and_wrong_direction(self):
        cases = [
            ("cad_trim", {"boundary_start": [0, 1], "boundary_end": [10, 1]}),
            ("cad_trim", {"boundary_start": [5, 1], "boundary_end": [5, 5]}),
            ("cad_trim", {"boundary_start": [20, -5], "boundary_end": [20, 5]}),
            ("cad_extend", {"boundary_start": [5, -5], "boundary_end": [5, 5]}),
            ("cad_extend", {"end": "start"}),
            ("cad_trim", {"boundary_start": [5, -5, 1], "boundary_end": [5, 5, 1]}),
        ]
        for name, args in cases:
            self.configure(name)
            with self.tools() as (registry, backup, _), self.assertRaises(ValueError):
                await registry.call(name, self.args(name, dry_run=False, **args))
            backup.assert_not_called()
            self.assert_unsaved()

    async def test_five_geometry_tools_reject_unsupported_and_3d_lines_before_backup(self):
        for name in ("cad_trim", "cad_extend", "cad_fillet", "cad_chamfer", "cad_join_entities"):
            for invalid in ("type", "3d", "zero", "nan", "thick"):
                sources = self.configure(name)
                if invalid == "type":
                    sources[0].ObjectName = "AcDbArc"
                elif invalid == "3d":
                    sources[0].EndPoint = (10, 0, 1)
                elif invalid == "zero":
                    sources[0].EndPoint = sources[0].StartPoint
                elif invalid == "thick":
                    sources[0].Thickness = 1.0
                else:
                    sources[0].EndPoint = (float("nan"), 0, 0)
                with self.tools() as (registry, backup, _), self.assertRaises(ValueError):
                    await registry.call(name, self.args(name, dry_run=False))
                backup.assert_not_called()
                self.assert_unsaved()

    async def test_fillet_right_angle_creates_tangent_minor_arc(self):
        first, second = self.configure("cad_fillet")
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_fillet", self.args("cad_fillet", dry_run=False))
        center, radius, start, end = self.doc.ModelSpace.add_arc_calls[0]
        self.assertIsInstance(center, _FakeVariant)
        self.assertAlmostEqual(8, _unwrap(center)[0])
        self.assertAlmostEqual(2, _unwrap(center)[1])
        self.assertEqual(2, radius)
        self.assertAlmostEqual(math.pi / 2, (end - start) % math.tau)
        self.assertAlmostEqual(8, first.EndPoint[0])
        self.assertAlmostEqual(2, second.StartPoint[1])
        self.assertEqual("编辑", self.doc.ModelSpace.added[0].Layer)
        self.assertEqual(1, result["created"])

    async def test_fillet_oblique_lines_and_reversed_order(self):
        for reverse in (False, True):
            sources = [_CadEngineeringEntity(start=(-20, 0, 7), end=(0, 0, 7)),
                       _CadEngineeringEntity(start=(0, 0, 7), end=(10, 10, 7), handle="E2")]
            if reverse:
                sources.reverse()
            self.configure("cad_fillet", sources)
            ends = ["start", "end"] if reverse else ["end", "start"]
            with self.tools() as (registry, _, _):
                await registry.call("cad_fillet", self.args("cad_fillet", ends=ends, dry_run=False))
            center, radius, start, end = self.doc.ModelSpace.add_arc_calls[0]
            center = _unwrap(center)
            self.assertEqual(7, center[2])
            arc_points = [[center[0] + radius * math.cos(a), center[1] + radius * math.sin(a)] for a in (start, end)]
            tangents = [e.StartPoint if side == "start" else e.EndPoint for e, side in zip(sources, ends)]
            for tangent in tangents:
                self.assertTrue(any(math.dist(tangent[:2], p) < 1e-8 for p in arc_points))
            self.assertLess((end - start) % math.tau, math.pi)

    async def test_chamfer_sets_distances_from_corner(self):
        first, second = self.configure("cad_chamfer")
        with self.tools() as (registry, _, _):
            await registry.call("cad_chamfer", self.args("cad_chamfer", dry_run=False))
        self.assertEqual((8, 0, 0), first.EndPoint)
        self.assertEqual((10, 3, 0), second.StartPoint)
        start, end = self.doc.ModelSpace.add_line_calls[0]
        self.assertEqual((8, 0, 0), _unwrap(start))
        self.assertEqual((10, 3, 0), _unwrap(end))
        self.assertEqual("编辑", self.doc.ModelSpace.added[0].Layer)

    async def test_corner_cardinality_and_oversized_values_reject_before_backup(self):
        for name in ("cad_fillet", "cad_chamfer"):
            for count in (1, 3):
                sources = self.configure(name)
                entities = sources[:count] if count == 1 else sources + [_CadEngineeringEntity(handle="E3")]
                self.configure(name, entities)
                with self.tools() as (registry, backup, _), self.assertRaisesRegex(ValueError, "两条"):
                    await registry.call(name, self.args(name, dry_run=False))
                backup.assert_not_called()
            self.configure(name)
            kwargs = {"radius": 100} if name == "cad_fillet" else {"distance2": 100}
            with self.tools() as (registry, backup, _), self.assertRaisesRegex(ValueError, "过大"):
                await registry.call(name, self.args(name, dry_run=False, **kwargs))
            backup.assert_not_called()

    async def test_corner_creation_error_does_not_change_sources(self):
        for name, method in (("cad_fillet", "AddArc"), ("cad_chamfer", "AddLine")):
            sources = self.configure(name)
            with self.tools() as (registry, _, _), mock.patch.object(self.doc.ModelSpace, method, side_effect=RuntimeError("接口不支持")):
                with self.assertRaisesRegex(RuntimeError, "AutoCAD COM.*接口不支持"):
                    await registry.call(name, self.args(name, dry_run=False))
            self.assertTrue(all(not e.calls for e in sources))
            self.assert_unsaved()
            self.doc.Close.assert_called_once_with(False)

    async def test_endpoint_write_failure_discards_partial_changes(self):
        class RejectEndpoint(_CadEngineeringEntity):
            def __setattr__(self, name, value):
                if name == "EndPoint" and isinstance(value, _FakeVariant):
                    raise RuntimeError("endpoint rejected")
                super().__setattr__(name, value)
        source = RejectEndpoint()
        self.configure("cad_trim", [source])
        with self.tools() as (registry, _, _), self.assertRaisesRegex(RuntimeError, "endpoint rejected"):
            await registry.call("cad_trim", self.args("cad_trim", dry_run=False))
        self.assert_unsaved()

    async def test_join_orders_reversed_segments_and_preserves_elevation(self):
        sources = [_CadEngineeringEntity(start=(10, 10, 9), end=(10, 0, 9)),
                   _CadEngineeringEntity(start=(0, 0, 9), end=(10, 0, 9), handle="E2")]
        self.configure("cad_join_entities", sources)
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_join_entities", self.args("cad_join_entities", dry_run=False))
        polyline = self.doc.ModelSpace.added[0]
        self.assertEqual((10, 10, 10, 0, 0, 0), polyline.Coordinates)
        self.assertEqual(9, polyline.Elevation)
        self.assertEqual((0, 0, 1), _unwrap(polyline.Normal))
        self.assertFalse(polyline.Closed)
        self.assertEqual(2, result["erased"])
        self.assertEqual([], self.doc.ModelSpace.entities)

    async def test_join_closed_loop(self):
        sources = [_CadEngineeringEntity(start=(0, 0, 0), end=(10, 0, 0)),
                   _CadEngineeringEntity(start=(10, 10, 0), end=(0, 0, 0), handle="E2"),
                   _CadEngineeringEntity(start=(10, 0, 0), end=(10, 10, 0), handle="E3")]
        self.configure("cad_join_entities", sources)
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_join_entities", self.args("cad_join_entities", dry_run=False))
        self.assertTrue(result["closed"])
        self.assertTrue(self.doc.ModelSpace.added[0].Closed)
        self.assertEqual(6, len(self.doc.ModelSpace.added[0].Coordinates))

    async def test_join_tolerance_snaps_near_endpoints(self):
        self.configure("cad_join_entities", [
            _CadEngineeringEntity(), _CadEngineeringEntity(start=(10.000001, 0, 0), end=(10, 10, 0), handle="E2")])
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_join_entities", self.args("cad_join_entities", tolerance=1e-4, dry_run=False))
        self.assertEqual([[0, 0, 0], [10, 0, 0], [10, 10, 0]], result["vertices"])

    async def test_join_rejects_disconnected_branched_and_overlapping_lines(self):
        cases = [
            [((0, 0, 0), (10, 0, 0))],
            [((0, 0, 0), (10, 0, 0)), ((20, 0, 0), (30, 0, 0))],
            [((0, 0, 0), (10, 0, 0)), ((10, 0, 0), (20, 0, 0)), ((10, 0, 0), (10, 10, 0))],
            [((0, 0, 0), (10, 0, 0)), ((10, 0, 0), (0, 0, 0))],
            [((0, 0, 0), (10, 0, 0)), ((10, 0, 0), (5, 0, 0))],
        ]
        for case in cases:
            sources = [_CadEngineeringEntity(start=a, end=b, handle=f"E{i}") for i, (a, b) in enumerate(case)]
            self.configure("cad_join_entities", sources)
            with self.tools() as (registry, backup, _), self.assertRaises(ValueError):
                await registry.call("cad_join_entities", self.args("cad_join_entities", dry_run=False))
            backup.assert_not_called()
            self.assert_unsaved()

    async def test_join_rejects_different_style_or_elevation(self):
        for attr, value in (("Color", 1), ("Layer", "其他"), ("EndPoint", (10, 10, 1))):
            sources = self.configure("cad_join_entities")
            setattr(sources[1], attr, value)
            with self.tools() as (registry, backup, _), self.assertRaises(ValueError):
                await registry.call("cad_join_entities", self.args("cad_join_entities", layer="", types="LINE", dry_run=False))
            backup.assert_not_called()

    async def test_join_creation_failure_keeps_all_sources(self):
        sources = self.configure("cad_join_entities")
        with self.tools() as (registry, _, _), mock.patch.object(self.doc.ModelSpace, "AddLightWeightPolyline", side_effect=AttributeError("missing")):
            with self.assertRaisesRegex(RuntimeError, "不支持轻量多段线"):
                await registry.call("cad_join_entities", self.args("cad_join_entities", dry_run=False))
        self.assertEqual(sources, self.doc.ModelSpace.entities)
        self.assertTrue(all(not e.calls for e in sources))
        self.assert_unsaved()

    async def test_explode_replaces_source_only_after_nonempty_results(self):
        source = self.configure("cad_explode")[0]
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_explode", self.args("cad_explode", dry_run=False))
        self.assertEqual([("Explode",), ("Delete",)], source.calls)
        self.assertEqual(2, result["created"])
        self.assertEqual(1, result["erased"])
        self.assertNotIn(source, self.doc.ModelSpace.entities)

    async def test_explode_can_keep_original(self):
        source = self.configure("cad_explode")[0]
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_explode", self.args("cad_explode", erase_source=False, dry_run=False))
        self.assertIn(source, self.doc.ModelSpace.entities)
        self.assertEqual(0, result["erased"])
        self.assertEqual(3, self.doc.ModelSpace.Count)

    async def test_explode_rejects_xrefs_compared_and_attributed_blocks(self):
        for object_name in ("AcDbExternalReference", "AcDbComparedReference", "AcDbBlockReference"):
            source = self.configure("cad_explode")[0]
            source.ObjectName = object_name
            source.Name = "块"
            source.HasAttributes = True
            block = _CadBlock("块")
            block.IsXRef = False
            self.doc.Blocks.Item = mock.Mock(return_value=block)
            with self.tools() as (registry, backup, _), self.assertRaisesRegex(ValueError, "不支持分解"):
                await registry.call("cad_explode", self.args("cad_explode", dry_run=False))
            backup.assert_not_called()
            self.assertEqual([], source.calls)

    async def test_plain_block_explode_checks_block_definition_for_isxref(self):
        source = self.configure("cad_explode")[0]
        source.ObjectName = "AcDbBlockReference"
        source.Name = "普通块"
        block = _CadBlock("普通块")
        block.IsXRef = False
        self.doc.Blocks.Item = mock.Mock(return_value=block)
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_explode", self.args("cad_explode", dry_run=False))
        self.assertEqual(2, result["created"])
        self.doc.Blocks.Item.assert_called_once_with("普通块")

    async def test_explode_missing_method_is_clear_error_before_backup(self):
        source = self.configure("cad_explode")[0]
        source.Explode = None
        with self.tools() as (registry, backup, _), self.assertRaisesRegex(RuntimeError, "不支持 Explode"):
            await registry.call("cad_explode", self.args("cad_explode", dry_run=False))
        backup.assert_not_called()
        self.assert_unsaved()


    async def test_destructive_second_batch_tools_are_marked_dangerous(self):
        with self.tools() as (registry, _, _):
            for name in ("cad_join_entities", "cad_explode"):
                self.assertTrue(registry.get(name).danger)

    async def test_generated_original_or_duplicate_handles_are_rejected(self):
        for name, attr in (("cad_offset", "offset_result"), ("cad_explode", "explode_result")):
            for mode in ("source", "duplicate", "missing_handle"):
                source = self.configure(name)[0]
                one = SimpleNamespace(ObjectName="AcDbLine", Handle="N1")
                two = SimpleNamespace(ObjectName="AcDbLine", Handle="n1")
                result = (source,) if mode == "source" else (one, two)
                if mode == "missing_handle":
                    result = (SimpleNamespace(ObjectName="AcDbLine", Handle=None),)
                setattr(source, attr, result)
                with self.tools() as (registry, _, _), self.assertRaisesRegex(RuntimeError, "Handle"):
                    await registry.call(name, self.args(name, dry_run=False))
                self.assertIn(source, self.doc.ModelSpace.entities)
                self.assert_unsaved()
                self.doc.Close.assert_called_once_with(False)

    async def test_silent_endpoint_write_is_not_reported_as_success(self):
        class SilentEndpoint(_CadEngineeringEntity):
            def __setattr__(self, name, value):
                if name == "EndPoint" and isinstance(value, _FakeVariant):
                    return
                super().__setattr__(name, value)
        self.configure("cad_trim", [SilentEndpoint()])
        with self.tools() as (registry, _, _), self.assertRaisesRegex(RuntimeError, "写入后不一致"):
            await registry.call("cad_trim", self.args("cad_trim", dry_run=False))
        self.assert_unsaved()

    async def test_join_source_delete_failure_discards_generated_polyline(self):
        sources = self.configure("cad_join_entities")
        sources[1].Delete = mock.Mock(side_effect=RuntimeError("delete rejected"))
        with self.tools() as (registry, _, _), self.assertRaisesRegex(RuntimeError, "delete rejected"):
            await registry.call("cad_join_entities", self.args("cad_join_entities", dry_run=False))
        self.assertEqual(1, len(self.doc.ModelSpace.added))
        self.assert_unsaved()
        self.doc.Close.assert_called_once_with(False)

    async def test_explode_delete_failure_does_not_save(self):
        source = self.configure("cad_explode")[0]
        source.Delete = mock.Mock(side_effect=RuntimeError("delete rejected"))
        with self.tools() as (registry, _, _), self.assertRaisesRegex(RuntimeError, "delete rejected"):
            await registry.call("cad_explode", self.args("cad_explode", dry_run=False))
        self.assert_unsaved()
        self.doc.Close.assert_called_once_with(False)

    async def test_second_batch_preview_cap_does_not_limit_execution(self):
        sources = [_CadEngineeringEntity(start=(i, 0, 0), end=(i + 1, 0, 0), handle=f"E{i}")
                   for i in range(cad_module._MAX_DETAIL + 3)]
        self.configure("cad_offset", sources)
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_offset", self.args("cad_offset", dry_run=False))
        self.assertEqual(cad_module._MAX_DETAIL, len(result["preview"]))
        self.assertTrue(result["truncated"])
        self.assertTrue(result["entities_truncated"])
        self.assertEqual(len(sources), result["changed"])
        self.assertEqual(len(sources), result["created"])
        self.assertEqual(2 * len(sources), self.doc.ModelSpace.Count)

    async def test_join_rejects_multiple_disconnected_closed_loops(self):
        sources = []
        for offset in (0, 100):
            points = [(offset, 0, 0), (offset + 10, 0, 0), (offset + 10, 10, 0)]
            for a, b in zip(points, points[1:] + points[:1]):
                sources.append(_CadEngineeringEntity(start=a, end=b, handle=f"E{len(sources)}"))
        self.configure("cad_join_entities", sources)
        with self.tools() as (registry, backup, _), self.assertRaisesRegex(ValueError, "不连通"):
            await registry.call("cad_join_entities", self.args("cad_join_entities", dry_run=False))
        backup.assert_not_called()
        self.assert_unsaved()

    async def test_corner_rejects_parallel_lines_before_backup(self):
        for name in ("cad_fillet", "cad_chamfer"):
            self.configure(name, [_CadEngineeringEntity(), _CadEngineeringEntity(start=(0, 10, 0), end=(10, 10, 0), handle="E2")])
            with self.tools() as (registry, backup, _), self.assertRaisesRegex(ValueError, "平行"):
                await registry.call(name, self.args(name, dry_run=False))
            backup.assert_not_called()
            self.assert_unsaved()


class CadToolDescriptionTests(unittest.TestCase):
    def test_all_cad_tools_warn_no_launch_no_screenshot(self):
        """42.5:所有 cad_* 工具的 description 都要点明"不需要 launch_app/截屏点界面"。"""
        client = _CadClient(active=None)
        with _registered_tools(client) as registry:
            for name in cad_module.EXPOSED_TOOLS:
                tool = registry.get(name)
                self.assertIsNotNone(tool, f"{name} 未注册")
                self.assertIn("不需要", tool.description, f"{name} 的 description 缺引导")


class CadModifySummaryTests(unittest.TestCase):
    def test_summary_reflects_own_app_false(self):
        """42.7:图纸是用户开着的(own_app=False),summary 必须说'未被关闭',不许写死'已关闭'。"""
        summary = cad_module._modify_summary("SaveAs(AutoCAD 2013)", "/tmp/x.备份-1.dwg", own_app=False)
        self.assertIn("未被关闭", summary)
        self.assertNotIn("已关闭", summary)

    def test_summary_reflects_own_app_true(self):
        summary = cad_module._modify_summary("SaveAs(AutoCAD 2013)", "/tmp/x.备份-1.dwg", own_app=True)
        self.assertIn("已关闭", summary)


class CadAppUsableTests(unittest.TestCase):
    def test_localized_rpc_rejection_is_classified_as_busy(self):
        error = "(-2147418111, '被呼叫方拒绝接收呼叫。', None, None)"
        self.assertTrue(cad_module._is_busy_error(RuntimeError(error)))
        self.assertEqual("busy", cad_module._error_nature(error))

    def test_busy_startup_is_retried(self):
        calls = []

        class App:
            @property
            def Documents(self):
                calls.append(1)
                if len(calls) < 3:
                    raise _BUSY
                return _CadDocuments(docs=[])

        with mock.patch.object(cad_module, "_COM_RETRY_SLEEP", 0):
            usable, nature = cad_module._app_usable(App())
        self.assertTrue(usable)
        self.assertEqual(3, len(calls), "忙要退避重试消化掉")

    def test_cold_start_budget_waits_longer(self):
        calls = []

        class App:
            @property
            def Documents(self):
                calls.append(1)
                if len(calls) < 20:
                    raise _BUSY
                return _CadDocuments(docs=[])

        with mock.patch.object(cad_module, "_COLD_START_SLEEP", 0):
            usable, _nature = cad_module._app_usable(App(), cold_start=True)
        self.assertTrue(usable)
        self.assertEqual(20, len(calls), "冷启动要等到第 20 次才好(60 秒预算)")

    def test_busy_startup_does_not_rebuild(self):
        """43.1 最要紧回归:启动期被拒不能误删 gen_py。"""
        cad_module._RESOLVED_STRATEGY = None
        cad_module._GEN_PY_CLEARED = False

        class BusyDocs:
            def __init__(self):
                self.calls = 0

            @property
            def Count(self):
                self.calls += 1
                if self.calls < 3:
                    raise _BUSY
                return 0

        class App:
            Documents = BusyDocs()

        client = _CadClient(active=App())
        with mock.patch.object(cad_module, "_COM_RETRY_SLEEP", 0), \
             mock.patch.object(cad_module.shutil, "rmtree") as rmtree, \
             mock.patch.object(cad_module.os.path, "isdir", return_value=True):
            _app, strategy, _own = cad_module._resolve_cad_app(client, prefer_running=True)
        self.assertEqual("late", strategy)
        rmtree.assert_not_called(), "启动期被拒不该误删 gen_py"


class CadProbeCleanupTests(unittest.TestCase):
    def test_busy_mixed_with_wrapper_errors_does_not_rebuild_or_clear_cache(self):
        outcomes = iter([
            (None, False, "Documents lookup failed", "bad"),
            (None, False, "Documents lookup failed", "bad"),
            (None, False, "Application failed: (-2147418111, '被呼叫方拒绝接收呼叫。', None, None)", "busy"),
            (None, False, "EnsureDispatch failed", "other"),
        ])
        with mock.patch.object(cad_module, "_run_strategy", side_effect=lambda *_args: next(outcomes)) as run, \
             mock.patch.object(cad_module, "_cleanup_started_instance", return_value=None), \
             mock.patch.object(cad_module, "_gen_py_path", return_value="/tmp/gen_py"), \
             mock.patch.object(cad_module.shutil, "rmtree") as rmtree:
            with self.assertRaisesRegex(RuntimeError, "启动太慢") as ctx:
                cad_module._probe(mock.Mock(), True, True)
        self.assertEqual(4, run.call_count, "rebuild must not run when any strategy reports busy")
        self.assertIn("启动太慢", str(ctx.exception))
        rmtree.assert_not_called()

    def test_probe_failure_quits_started_instance(self):
        """44.3:探测全败 + 没在跑(had_running=False)→ 我们启动的实例要 Quit。"""
        cad_module._RESOLVED_STRATEGY = None
        cad_module._GEN_PY_CLEARED = False
        busy = _BusyStartupApp(None)
        client = _CadClient(active=None, dispatch_results=[busy])
        with mock.patch.object(cad_module, "_COLD_START_SLEEP", 0), \
             mock.patch.object(cad_module.shutil, "rmtree"), \
             mock.patch.object(cad_module.os.path, "isdir", return_value=True):
            with self.assertRaises(RuntimeError):
                cad_module._resolve_cad_app(client, prefer_running=True)
        self.assertTrue(busy.quit_called, "我们自己启动的实例要 Quit")

    def test_probe_failure_does_not_quit_users_instance(self):
        cad_module._RESOLVED_STRATEGY = None
        cad_module._GEN_PY_CLEARED = False
        busy = _BusyStartupApp(None)
        client = _CadClient(active=busy)  # 用户实例(had_running=True)
        with mock.patch.object(cad_module, "_COM_RETRY_SLEEP", 0), \
             mock.patch.object(cad_module.shutil, "rmtree"), \
             mock.patch.object(cad_module.os.path, "isdir", return_value=True):
            with self.assertRaises(RuntimeError):
                cad_module._resolve_cad_app(client, prefer_running=True)
        self.assertFalse(busy.quit_called, "用户的实例绝不能 Quit")

    def test_unavailable_does_not_open_rebuild_gate(self):
        """44.2:对象是 None/拿不到(unavailable)不算坏,不给 rebuild(清 gen_py)开门。"""
        cad_module._RESOLVED_STRATEGY = None
        cad_module._GEN_PY_CLEARED = False
        client = _CadClient(active=None, dispatch_results=[None, None, None, None])
        with mock.patch.object(cad_module, "_COM_RETRY_SLEEP", 0), \
             mock.patch.object(cad_module, "_COLD_START_SLEEP", 0), \
             mock.patch.object(cad_module.shutil, "rmtree") as rmtree, \
             mock.patch.object(cad_module.os.path, "isdir", return_value=True):
            with self.assertRaises(RuntimeError):
                cad_module._resolve_cad_app(client, prefer_running=True)
        rmtree.assert_not_called(), "unavailable 不算坏,不该清 gen_py"


class CadProgressCallbackTests(unittest.TestCase):
    def test_cold_start_reports_progress(self):
        """44.5:冷启动等待期间要回调进度(前端可见)。"""
        messages = []
        cad_module.set_progress_callback(messages.append)
        try:
            calls = []

            class App:
                @property
                def Documents(self):
                    calls.append(1)
                    if len(calls) < 10:
                        raise _BUSY
                    return _CadDocuments(docs=[])

            with mock.patch.object(cad_module, "_COLD_START_SLEEP", 0):
                usable, _nature = cad_module._app_usable(App(), cold_start=True)
            self.assertTrue(usable)
        finally:
            cad_module.set_progress_callback(None)
        self.assertTrue(messages, "冷启动等待要回调进度")
        self.assertTrue(any("启动" in m for m in messages))


class CadColdStartEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def _open_info(self, reject_times):
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            doc = _CadDocument(str(path))
            busy = _BusyStartupApp(reject_times, open_doc=doc)
            client = _CadClient(active=None, dispatch_results=[busy])
            progress = []
            cad_module.set_progress_callback(progress.append)
            try:
                with mock.patch.object(cad_module, "_COLD_START_SLEEP", 0), \
                     mock.patch.object(cad_module.shutil, "rmtree") as rmtree, \
                     mock.patch.object(cad_module.os.path, "isdir", return_value=True), \
                     _registered_tools(client) as registry:
                    result = await registry.call("cad_open_info", {"path": str(path)})
            finally:
                cad_module.set_progress_callback(None)
            return result, busy, rmtree, progress

    async def test_success_rejects_2(self):
        result, busy, rmtree, progress = await self._open_info(2)
        self.assertIn("unit", result)
        self.assertTrue(busy.quit_called, "收尾要 Quit 我们启动的实例")
        rmtree.assert_not_called(), "冷启动成功不该删 gen_py"
        self.assertTrue(progress, "等待期间要回调进度")

    async def test_success_rejects_10(self):
        result, busy, rmtree, progress = await self._open_info(10)
        self.assertIn("unit", result)
        self.assertTrue(busy.quit_called)
        rmtree.assert_not_called()

    async def test_success_rejects_29(self):
        result, busy, rmtree, progress = await self._open_info(29)
        self.assertIn("unit", result)
        self.assertTrue(busy.quit_called)
        rmtree.assert_not_called()

    async def test_fail_always_busy_exceeds_budget(self):
        """44.6:永远忙(超 60 秒预算)→ 失败,不删 gen_py,且我们启动的实例被 Quit。"""
        with TemporaryDirectory() as tmp:
            path = _dwg_file(Path(tmp))
            busy = _BusyStartupApp(None)
            client = _CadClient(active=None, dispatch_results=[busy])
            with mock.patch.object(cad_module, "_COLD_START_SLEEP", 0), \
                 mock.patch.object(cad_module.shutil, "rmtree") as rmtree, \
                 mock.patch.object(cad_module.os.path, "isdir", return_value=True), \
                 _registered_tools(client) as registry:
                with self.assertRaises(RuntimeError) as ctx:
                    await registry.call("cad_open_info", {"path": str(path)})
        self.assertIn("启动太慢", str(ctx.exception))
        self.assertTrue(busy.quit_called, "失败也要 Quit 我们启动的实例")
        rmtree.assert_not_called(), "全忙不能删 gen_py"


class CadEnvErrorWordingTests(unittest.TestCase):
    def test_busy_wording_no_lt(self):
        msg = cad_module._cad_env_error("/tmp/gen_py", [], "busy")
        self.assertIn("启动太慢", msg)
        self.assertNotIn("AutoCAD LT", msg, "没启动完不能被说成 LT")

    def test_dispatch_wording_mentions_lt(self):
        msg = cad_module._cad_env_error("/tmp/gen_py", [], "dispatch")
        self.assertIn("AutoCAD LT", msg)

    def test_bad_wording_mentions_gen_py(self):
        msg = cad_module._cad_env_error("/tmp/gen_py", [], "bad")
        self.assertIn("gen_py", msg)
        self.assertIn("/tmp/gen_py", msg)


class _DeliveryEntity(_CadEntity):
    sequence = 100

    def __init__(self, kind, **kwargs):
        super().__init__(kind, **kwargs)
        _DeliveryEntity.sequence += 1
        self.Handle = format(self.sequence, "X")


class _DeliveryHatch:
    def __init__(self, events):
        object.__setattr__(self, "_ready", False)
        object.__setattr__(self, "_events", events)

    def __getattribute__(self, name):
        if not name.startswith("_") and name != "AppendOuterLoop" and not object.__getattribute__(self, "_ready"):
            raise AssertionError("Must AppendOuterLoop before ANY other Hatch access")
        return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        if not object.__getattribute__(self, "_ready"):
            raise AssertionError("Must AppendOuterLoop before setting Hatch properties")
        object.__setattr__(self, name, value)

    def AppendOuterLoop(self, loop):
        assert isinstance(loop, _FakeVariant) and loop.vt == 0x2009 and len(loop.value) == 1
        self._events.append(("AppendOuterLoop", loop))
        object.__setattr__(self, "_ready", True)
        self.ObjectName, self.Handle, self.Area = "AcDbHatch", "F1", 100.0

    def Evaluate(self):
        self._events.append(("Evaluate",))


class _DeliveryBlock(_CadCollection):
    def __init__(self, name="门", items=(), xref=False, layout=False):
        super().__init__(items)
        self.Name, self.IsXRef, self.IsLayout = name, xref, layout
        self.calls = []

    def AddAttribute(self, height, mode, prompt, insertion, tag, value):
        assert isinstance(insertion, _FakeVariant) and insertion.vt == 0x2005
        self.calls.append((height, mode, prompt, insertion, tag, value))
        entity = _DeliveryEntity("AcDbAttributeDefinition", text=value, position=_unwrap(insertion))
        entity.Height, entity.Mode, entity.PromptString, entity.TagString = height, mode, prompt, tag
        self.items.append(entity)
        return entity


class _DeliveryModelSpace(_CadModelSpace):
    def __init__(self, entities):
        super().__init__(entities)
        self.events = []

    def AddMText(self, insertion, width, text):
        entity = super().AddMText(insertion, width, text)
        entity.Handle = "F2"
        return entity

    def AddHatch(self, pattern_type, pattern, associative):
        assert pattern_type == 1 and associative is True, "Predefined requires enum 1"
        self.events.append(("AddHatch", pattern_type, pattern, associative))
        entity = _DeliveryHatch(self.events)
        self.added.append(entity)
        return entity

    def AddLeader(self, points, annotation, leader_type):
        assert leader_type == 2 and isinstance(annotation, _CadMText), "LineWithArrow is 2"
        assert isinstance(points, _FakeVariant) and points.vt == 0x2005
        self.events.append(("AddLeader", points, annotation, leader_type))
        entity = _DeliveryEntity("AcDbLeader")
        self.added.append(entity)
        return entity

    def AddMLeader(self, points, leader_index):
        assert leader_index == 0 and isinstance(leader_index, int)
        assert isinstance(points, _FakeVariant) and points.vt == 0x2005
        self.events.append(("AddMLeader", points, leader_index))
        entity = _DeliveryEntity("AcDbMLeader")
        self.added.append(entity)
        return entity

    def InsertBlock(self, insertion, name, sx, sy, sz, rotation):
        assert isinstance(insertion, _FakeVariant) and insertion.vt == 0x2005
        self.events.append(("InsertBlock", insertion, name, sx, sy, sz, rotation))
        entity = _DeliveryEntity("AcDbBlockReference", name=name, position=_unwrap(insertion))
        entity.HasAttributes = False
        self.added.append(entity)
        return entity


class _DeliveryPlotConfiguration:
    def __init__(self, name, config="None", model=False, collection=None):
        self.Name, self.ModelType = name, model
        self._config, self._collection = config, collection
        self.settings = {"paper": "A3", "scale": 0.01, "rotation": 1}
        self.assignments, self.copy_calls = [], []
        self.deleted = False

    @property
    def ConfigName(self):
        return self._config

    @ConfigName.setter
    def ConfigName(self, value):
        if str(value).casefold() in ("none", ""):
            raise AssertionError("Autodesk forbids assigning ConfigName=None")
        self.assignments.append(value)
        self._config = value
        self.settings = {"paper": "A4", "scale": 1.0, "rotation": 0}
        self.PlotType, self.UseStandardScale, self.StandardScale = 1, True, 0
        self.CenterPlot, self.PlotOrigin, self.ViewToPlot = False, (0.0, 0.0), ""
        self.window = ((0.0, 0.0), (1.0, 1.0))

    @property
    def PaperUnits(self):
        return 1

    @property
    def CanonicalMediaName(self):
        return self.settings["paper"]

    @CanonicalMediaName.setter
    def CanonicalMediaName(self, value):
        self.settings["paper"] = value

    @property
    def PlotRotation(self):
        return self.settings["rotation"]

    @PlotRotation.setter
    def PlotRotation(self, value):
        self.settings["rotation"] = value

    PlotType = 5
    UseStandardScale = False
    StandardScale = 0
    CenterPlot = False
    @property
    def PlotOrigin(self):
        return getattr(self, "_plot_origin", (0.0, 0.0))

    @PlotOrigin.setter
    def PlotOrigin(self, value):
        self._plot_origin = _unwrap(value)

    ViewToPlot = "view1"

    def GetCustomScale(self):
        return (self.settings["scale"], 1.0)

    def SetCustomScale(self, numerator, denominator):
        self.settings["scale"] = numerator / denominator

    def GetWindowToPlot(self):
        return getattr(self, "window", ((1.0, 2.0), (30.0, 40.0)))

    def SetWindowToPlot(self, first, second):
        self.window = (_unwrap(first), _unwrap(second))

    def CopyFrom(self, other):
        assert isinstance(other, _DeliveryPlotConfiguration)
        self.copy_calls.append(other.Name)
        self._config, self.settings = other.ConfigName, dict(other.settings)
        for attr in ("PlotType", "UseStandardScale", "StandardScale", "CenterPlot", "PlotOrigin", "ViewToPlot"):
            setattr(self, attr, getattr(other, attr))
        self.window = other.GetWindowToPlot()

    def Delete(self):
        self.deleted = True
        if self._collection is not None:
            self._collection.items.remove(self)

    def RefreshPlotDeviceInfo(self):
        pass

    def GetPlotDeviceNames(self):
        return ("DWG To PDF.pc3", "printer.pc3")


class _DeliveryConfigurations(_CadCollection):
    def __init__(self):
        super().__init__([])
        self.created = []

    def Add(self, name, model_type=False):
        config = _DeliveryPlotConfiguration(name, model=model_type, collection=self)
        self.items.append(config)
        self.created.append(config)
        return config


def _pdf_pc3_bytes(plain=b'custom{\n 0{\n  name="View_New_File\n  value=TRUE\n }\n}\n'):
    packed = zlib.compress(plain)
    return (b"PIAFILEVERSION_2.0,PC3VER1,compress\r\npmzlibcodec"
            + struct.pack("<III", zlib.adler32(packed), len(plain), len(packed)) + packed)


class _DeliveryPlot:
    def __init__(self, doc):
        self.doc, self.calls = doc, []
        self.result, self.payload, self.error = True, b"%PDF-1.7\nverified\n%%EOF\n", None
        self.rename_output, self.on_plot = False, None

    def SetLayoutsToPlot(self, *args):
        raise AssertionError("Do not call SetLayoutsToPlot; it changes output naming")

    def PlotToFile(self, path, plot_config):
        assert self.doc.activated and self.doc.GetVariable("BACKGROUNDPLOT") == 0
        assert Path(plot_config).is_absolute() and Path(plot_config).is_file()
        assert b'value=FALSE' in zlib.decompress(Path(plot_config).read_bytes()[60:])
        self.silent_config = plot_config
        self.calls.append((path, self.doc.ActiveLayout.Name, self.doc.ActiveLayout.ConfigName))
        if self.on_plot:
            self.on_plot()
        if self.error:
            raise self.error
        if self.payload is not None:
            output = Path(path)
            if self.rename_output:
                output = output.with_name("drawing-Layout1.pdf")
            output.write_bytes(self.payload)
        return self.result


class _DeliveryDocument(_CadDocument):
    def __init__(self, full_name):
        super().__init__(full_name)
        circle = _DeliveryEntity("AcDbCircle", position=(0, 0, 3))
        circle.Handle, circle.Area, circle.Normal, circle.Center = "C1", 100.0, (0, 0, 1), (0, 0, 3)
        self.ModelSpace = _DeliveryModelSpace([circle])
        self.ActiveLayer = self.Layers.Item(0)
        self.Blocks = _CadCollection([_DeliveryBlock()])
        self.Layouts = _CadCollection([_DeliveryPlotConfiguration("Model", model=True), _DeliveryPlotConfiguration("图纸1")])
        self.ActiveLayout = self.Layouts.Item(0)
        self.PlotConfigurations = _DeliveryConfigurations()
        self.Plot = _DeliveryPlot(self)
        self.activated, self.variable_calls = False, []
        self._variables["BACKGROUNDPLOT"] = 3
        self.output_payload = "normal"

    def Activate(self):
        self.activated = True

    def SetVariable(self, name, value):
        self.variable_calls.append((name, value))
        self._variables[name] = value

    def SaveAs(self, path, version=None):
        super().SaveAs(path, version)
        output = Path(path)
        if output.resolve() == Path(self.FullName).resolve() or self.output_payload == "missing":
            return
        if self.output_payload == "invalid":
            output.write_bytes(b"invalid output")
            return
        if output.suffix == ".dxf":
            header = {13: "AC1015", 25: "AC1018", 37: "AC1021", 49: "AC1024", 61: "AC1027", 65: "AC1032"}[version]
            output.write_bytes(f"  0\r\nSECTION\r\n  2\r\nHEADER\r\n  9\r\n$ACADVER\r\n  1\r\n{header}\r\n  0\r\nENDSEC\r\n  0\r\nEOF\r\n".encode())
        else:
            header = {12: "AC1015", 24: "AC1018", 36: "AC1021", 48: "AC1024", 60: "AC1027", 64: "AC1032"}[version]
            output.write_bytes(header.encode() + b" verified-dwg")


class CadDeliveryTests(unittest.IsolatedAsyncioTestCase):
    MODIFY_ARGS = {
        "cad_hatch": {"boundary_handle": "C1"},
        "cad_add_leader": {"points": [[0, 0], [10, 20]], "text": "引线说明"},
        "cad_add_multileader": {"points": [[0, 0], [10, 20]], "text": "多重引线"},
        "cad_insert_block": {"block_name": "门", "insertion": [10, 20]},
        "cad_add_attribute": {"block_name": "门", "height": 3, "mode": 4, "prompt": "编号", "insertion": [1, 2], "tag": "id", "value": "D-01"},
    }
    OUTPUTS = {"cad_save_as": ".dwg", "cad_export_dxf": ".dxf", "cad_export_pdf": ".pdf"}

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = _dwg_file(Path(self.temp.name))
        self.original = self.path.read_bytes()
        self.doc = _DeliveryDocument(str(self.path))
        self.pc3 = Path(self.temp.name) / "DWG To PDF.pc3"
        self.pc3.write_bytes(_pdf_pc3_bytes())
        self.target = Path(self.temp.name) / "成品.dwg"

    def args(self, tool, **kwargs):
        args = {"path": str(self.path), **self.MODIFY_ARGS.get(tool, {})}
        if tool in self.OUTPUTS:
            args["output_path"] = str(self.target.with_suffix(self.OUTPUTS[tool]))
        return {**args, **kwargs}

    @contextmanager
    def tools(self, active=None, **kwargs):
        self.doc.Application = SimpleNamespace(Preferences=SimpleNamespace(
            Files=SimpleNamespace(PrinterConfigPath=self.temp.name)))
        client = _CadClient(active=active, dispatch_doc=self.doc, **kwargs)
        backups = []

        def backup(path):
            dest = Path(self.temp.name) / f"backup-{len(backups)}{path.suffix}"
            dest.write_bytes(path.read_bytes())
            backups.append(dest)
            return str(dest)

        with mock.patch.object(cad_module, "_backup_drawing", side_effect=backup) as save_backup, \
             _registered_tools(client) as registry:
            yield registry, save_backup, client, backups

    def assert_unsaved(self):
        self.assertEqual([], self.doc.saved_as)
        self.assertEqual(0, self.doc.save_called)
        self.assertEqual(self.original, self.path.read_bytes())

    def assert_pdf_restored(self):
        self.assertEqual(3, self.doc.GetVariable("BACKGROUNDPLOT"))
        self.assertIs(self.doc.Layouts.Item(0), self.doc.ActiveLayout)
        for layout in self.doc.Layouts.items:
            self.assertEqual("None", layout.ConfigName)
            self.assertEqual({"paper": "A3", "scale": 0.01, "rotation": 1}, layout.settings)
            self.assertNotIn("None", layout.assignments)
        self.assertEqual([], self.doc.PlotConfigurations.items)
        self.assertTrue(all(x.deleted for x in self.doc.PlotConfigurations.created))
        self.assert_unsaved()

    async def test_five_modifiers_default_preview_and_explicit_overwrite(self):
        for tool in self.MODIFY_ARGS:
            with self.subTest(tool=tool), self.tools() as (registry, backup, client, _):
                result = await registry.call(tool, self.args(tool))
                self.assertTrue(result["dry_run"])
                self.assertEqual(0, result["changed"])
                self.assertIsNone(result["backup"])
                backup.assert_not_called()
                self.assertEqual([], self.doc.ModelSpace.added)
                self.assertEqual([], self.doc.Blocks.Item(0).calls)
                self.assert_unsaved()
                json.dumps(result, allow_nan=False)
            with self.tools() as (registry, backup, client, _):
                with self.assertRaisesRegex(ValueError, "overwrite"):
                    await registry.call(tool, self.args(tool, dry_run=False))
                self.assertEqual(0, client.dispatched)
                backup.assert_not_called()

    async def test_flags_require_booleans_for_all_writes(self):
        for tool in [*self.MODIFY_ARGS, *self.OUTPUTS]:
            for kw in ({"dry_run": 0}, {"overwrite": 1}, {"dry_run": "false"}, {"overwrite": "true"}):
                with self.subTest(tool=tool, kw=kw), self.tools() as (registry, backup, client, _):
                    with self.assertRaisesRegex(ValueError, "布尔"):
                        await registry.call(tool, self.args(tool, **kw))
                    self.assertEqual(0, client.dispatched)
                    backup.assert_not_called()

    async def test_hatch_enum_dispatch_loop_before_properties_and_evaluate(self):
        with self.tools() as (registry, backup, _, backups):
            result = await registry.call("cad_hatch", self.args("cad_hatch", dry_run=False, overwrite=True, pattern="ANSI31", scale=2, angle=0.5))
        events = self.doc.ModelSpace.events
        self.assertEqual(("AddHatch", 1, "ANSI31", True), events[0])
        self.assertEqual("AppendOuterLoop", events[1][0])
        self.assertEqual(0x2009, events[1][1].vt)
        self.assertIs(self.doc.ModelSpace.entities[0], events[1][1].value[0])
        self.assertEqual(("Evaluate",), events[2])
        hatch = self.doc.ModelSpace.added[0]
        self.assertEqual((3, 2, 0.5, "0"), (hatch.Elevation, hatch.PatternScale, hatch.PatternAngle, hatch.Layer))
        self.assertEqual(1, result["added"])
        self.assertEqual(self.original, backups[0].read_bytes())
        backup.assert_called_once_with(self.path)
        self.assertEqual((str(self.path), 60), self.doc.saved_as[0])
        json.dumps(result, allow_nan=False)

    async def test_hatch_closed_polyline_and_bad_boundaries(self):
        polyline = _CadPolyline([0, 0, 10, 0, 10, 10, 0, 10], area=100)
        polyline.Handle, polyline.Normal, polyline.Elevation = "C1", (0, 0, 1), 5
        self.doc.ModelSpace.entities = [polyline]
        with self.tools() as (registry, _, _, _):
            result = await registry.call("cad_hatch", self.args("cad_hatch", dry_run=False, overwrite=True))
        self.assertEqual(1, result["added"])
        self.assertEqual(5, self.doc.ModelSpace.added[0].Elevation)
        self.doc = _DeliveryDocument(str(self.path))
        open_poly = _CadPolyline([0, 0, 10, 10], closed=False)
        open_poly.Handle = "C1"
        cases = [open_poly, SimpleNamespace(Handle="C1", ObjectName="AcDbLine"),
                 SimpleNamespace(Handle="C1", ObjectName="AcDbCircle", Area=0),
                 SimpleNamespace(Handle="C1", ObjectName="AcDbCircle", Area=1, Normal=(1, 0, 0))]
        for boundary in cases:
            with self.subTest(boundary=boundary), self.tools() as (registry, backup, _, _):
                self.doc.ModelSpace.entities = [boundary]
                with self.assertRaises(ValueError):
                    await registry.call("cad_hatch", self.args("cad_hatch", dry_run=False, overwrite=True))
                backup.assert_not_called()
                self.assert_unsaved()

    async def test_hatch_no_positive_area_never_saves(self):
        def bad(hatch):
            hatch.Area = 0
        with self.tools() as (registry, backup, _, _), mock.patch.object(_DeliveryHatch, "Evaluate", bad):
            with self.assertRaisesRegex(RuntimeError, "填充面积"):
                await registry.call("cad_hatch", self.args("cad_hatch", dry_run=False, overwrite=True))
        backup.assert_called_once()
        self.assert_unsaved()

    async def test_leader_enum_2_mtext_height_and_double_safearray(self):
        with self.tools() as (registry, _, _, _):
            result = await registry.call("cad_add_leader", self.args("cad_add_leader", dry_run=False, overwrite=True, height=5))
        call = self.doc.ModelSpace.events[0]
        self.assertEqual("AddLeader", call[0])
        self.assertEqual(2, call[3])
        self.assertEqual([0, 0, 0, 10, 20, 0], call[1].value)
        self.assertEqual(0x2005, call[1].vt)
        self.assertEqual(5, call[2].Height)
        self.assertFalse(hasattr(call[2], "TextHeight"))
        self.assertEqual(2, result["added"])
        json.dumps(result, allow_nan=False)

    async def test_mleader_index_zero_and_textheight(self):
        with self.tools() as (registry, _, _, _):
            result = await registry.call("cad_add_multileader", self.args("cad_add_multileader", dry_run=False, overwrite=True, height=4))
        call = self.doc.ModelSpace.events[0]
        self.assertEqual("AddMLeader", call[0])
        self.assertEqual(0, call[2])
        entity = self.doc.ModelSpace.added[0]
        self.assertEqual((2, "多重引线", 4), (entity.ContentType, entity.TextString, entity.TextHeight))
        self.assertEqual(1, result["added"])

    async def test_mleader_com_tuple_return_keeps_entity_and_discards_index(self):
        original = self.doc.ModelSpace.AddMLeader
        with self.tools() as (registry, _, _, _), mock.patch.object(
            self.doc.ModelSpace, "AddMLeader", side_effect=lambda *args: (original(*args), 0)
        ):
            result = await registry.call("cad_add_multileader", self.args(
                "cad_add_multileader", dry_run=False, overwrite=True, height=4))
        entity = self.doc.ModelSpace.added[0]
        self.assertEqual((2, "多重引线", 4), (entity.ContentType, entity.TextString, entity.TextHeight))
        self.assertEqual(1, result["added"])
        self.assertEqual(1, len(self.doc.saved_as))

    async def test_mleader_invalid_or_ambiguous_return_never_saves(self):
        for returned in (None, (None, 0), (0,),
                         (_DeliveryEntity("AcDbLine"), 0),
                         (_DeliveryEntity("AcDbMLeader"), _DeliveryEntity("AcDbMLeader"), 0)):
            with self.subTest(returned=returned), self.tools() as (registry, _, _, _), mock.patch.object(
                self.doc.ModelSpace, "AddMLeader", return_value=returned
            ):
                with self.assertRaisesRegex(RuntimeError, "唯一有效"):
                    await registry.call("cad_add_multileader", self.args(
                        "cad_add_multileader", dry_run=False, overwrite=True))
            self.assert_unsaved()

    async def test_mtext_fake_forbids_textheight_and_old_tool_uses_height(self):
        with self.assertRaisesRegex(AttributeError, "Height"):
            _CadMText("AcDbMText").TextHeight = 4
        with self.tools() as (registry, _, _, _):
            result = await registry.call("cad_add_mtext", {"path": str(self.path), "items": [{"text": "说明", "insertion": [0, 0], "height": 6}]})
        self.assertEqual(1, result["added"])
        self.assertEqual([], result["failed"])
        self.assertEqual(6, self.doc.ModelSpace.added[0].Height)

    async def test_insert_block_exact_signature(self):
        with self.tools() as (registry, _, _, _):
            result = await registry.call("cad_insert_block", self.args("cad_insert_block", dry_run=False, overwrite=True, scale=[2, 3, 4], rotation=0.5))
        call = self.doc.ModelSpace.events[0]
        self.assertEqual("InsertBlock", call[0])
        self.assertEqual([10, 20, 0], call[1].value)
        self.assertEqual(("门", 2, 3, 4, 0.5), call[2:])
        self.assertEqual(1, self.doc.Blocks.Count)
        self.assertEqual(1, result["added"])

    async def test_attribute_block_definition_signature_schema_tag(self):
        with self.tools() as (registry, backup, _, _):
            result = await registry.call("cad_add_attribute", self.args("cad_add_attribute", dry_run=False, overwrite=True, mode=12))
            schema = registry.get("cad_add_attribute").parameters
        call = self.doc.Blocks.Item(0).calls[0]
        self.assertEqual((3, 12, "编号"), call[:3])
        self.assertEqual([1, 2, 0], call[3].value)
        self.assertEqual(("ID", "D-01"), call[4:])
        self.assertTrue({"block_name", "height", "mode", "prompt", "insertion", "tag", "value"} <= set(schema["required"]))
        self.assertEqual(1, result["added"])
        self.assertIn("不自动同步", result["note"])
        self.assertEqual([], self.doc.ModelSpace.added)
        backup.assert_called_once()

    async def test_duplicate_attribute_never_overwrites(self):
        block = self.doc.Blocks.Item(0)
        block.items.append(SimpleNamespace(ObjectName="AcDbAttributeDefinition", TagString="ID"))
        with self.tools() as (registry, backup, _, _), self.assertRaisesRegex(ValueError, "已存在"):
            await registry.call("cad_add_attribute", self.args("cad_add_attribute", dry_run=False, overwrite=True))
        backup.assert_not_called()
        self.assertEqual([], block.calls)
        self.assert_unsaved()

    async def test_block_rejects_xref_layout_missing_anonymous(self):
        for tool in ("cad_insert_block", "cad_add_attribute"):
            for name, xref, layout in (("门", True, False), ("门", False, True), ("*U1", False, False), ("a|门", False, False), ("无", False, False)):
                self.doc.Blocks = _CadCollection([_DeliveryBlock(xref=xref, layout=layout)])
                with self.subTest(tool=tool, name=name), self.tools() as (registry, backup, _, _):
                    with self.assertRaises(ValueError):
                        await registry.call(tool, self.args(tool, block_name=name, dry_run=False, overwrite=True))
                    backup.assert_not_called()
                    self.assert_unsaved()


    async def test_input_validation_rejects_before_com(self):
        cases = {
            "cad_hatch": [{"scale": 0}, {"angle": float("inf")}, {"pattern": ""}, {"boundary_handle": ""}],
            "cad_add_leader": [{"points": [[0, 0]]}, {"points": [[0, 0], [0, 0]]}, {"height": -1}, {"width": 0}, {"text": ""}],
            "cad_add_multileader": [{"height": float("nan")}, {"text": ""}, {"points": [[True, 0], [1, 1]]}],
            "cad_insert_block": [{"insertion": [float("nan"), 0]}, {"scale": [1, 2]}, {"scale": [1, 0, 1]}, {"rotation": False}],
            "cad_add_attribute": [{"height": 0}, {"mode": 64}, {"mode": -1}, {"mode": True}, {"mode": 1.5}, {"tag": "a b"}, {"tag": "x!"}, {"tag": "x\x00"}, {"value": ""}, {"insertion": []}],
        }
        for tool, cases in cases.items():
            for kwargs in cases:
                with self.subTest(tool=tool, kwargs=kwargs), self.tools() as (registry, backup, client, _):
                    with self.assertRaises(ValueError):
                        await registry.call(tool, self.args(tool, **kwargs))
                    self.assertEqual(0, client.dispatched)
                    backup.assert_not_called()

    async def test_modifiers_refuse_locked_or_missing_layer(self):
        for tool in self.MODIFY_ARGS:
            with self.subTest(tool=tool), self.tools() as (registry, backup, _, _):
                self.doc.ActiveLayer.Lock = True
                with self.assertRaisesRegex(ValueError, "锁定"):
                    await registry.call(tool, self.args(tool, dry_run=False, overwrite=True))
                self.doc.ActiveLayer.Lock = False
                if tool != "cad_add_attribute":
                    with self.assertRaisesRegex(ValueError, "不存在"):
                        await registry.call(tool, self.args(tool, layer="不存在", dry_run=False, overwrite=True))
                backup.assert_not_called()
                self.assert_unsaved()

    async def test_all_writes_reject_user_open_source(self):
        for tool in [*self.MODIFY_ARGS, *self.OUTPUTS]:
            app = _CadApplication(docs=[self.doc])
            with self.subTest(tool=tool), self.tools(active=app) as (registry, backup, _, _):
                with self.assertRaisesRegex(RuntimeError, "AutoCAD"):
                    await registry.call(tool, self.args(tool, dry_run=False, overwrite=True))
                self.assertFalse(app.quit_called)
                backup.assert_not_called()
                self.assert_unsaved()
                self.assertEqual(0, self.doc.closed)

    async def test_all_writes_reject_source_locks(self):
        self.path.with_suffix(".dwl").write_text("external lock")
        for tool in [*self.MODIFY_ARGS, *self.OUTPUTS]:
            with self.subTest(tool=tool), self.tools() as (registry, backup, _, _):
                with self.assertRaisesRegex(RuntimeError, "占用"):
                    await registry.call(tool, self.args(tool, dry_run=False, overwrite=True))
                backup.assert_not_called()
                self.assert_unsaved()
        self.assertEqual([], self.doc.ModelSpace.added)
        self.assertEqual([], self.doc.Plot.calls)

    async def test_all_writes_require_writable_source_and_backup(self):
        for tool in [*self.MODIFY_ARGS, *self.OUTPUTS]:
            with self.subTest(tool=tool), self.tools() as (registry, backup, _, _):
                with mock.patch.object(cad_module.os, "access", return_value=False), self.assertRaisesRegex(RuntimeError, "只读"):
                    await registry.call(tool, self.args(tool, dry_run=False, overwrite=True))
                backup.assert_not_called()
                backup.side_effect = OSError("备份失败")
                with self.assertRaisesRegex((OSError, RuntimeError), "备份失败"):
                    await registry.call(tool, self.args(tool, dry_run=False, overwrite=True))
                self.assertEqual([], self.doc.ModelSpace.added)
                self.assertEqual([], self.doc.Blocks.Item(0).calls)
                self.assertEqual([], self.doc.Plot.calls)
                self.assert_unsaved()

    async def test_read_entities_exposes_stable_handle(self):
        with self.tools() as (registry, backup, _, _):
            result = await registry.call("cad_read_entities", {"path": str(self.path)})
        self.assertEqual("C1", result["entities"][0]["handle"])
        backup.assert_not_called()

    async def test_read_blocks_definitions_attributes_references_and_truncation(self):
        block = self.doc.Blocks.Item(0)
        for tag in ("A", "B"):
            block.AddAttribute(3, 4, "提示", _FakeVariant(0x2005, [1, 2, 0]), tag, "D1")
            ref = _DeliveryEntity("AcDbBlockReference", name="门", position=(10, 20, 0))
            ref.HasAttributes = True
            self.doc.ModelSpace.entities.append(ref)
        self.doc.Blocks.items.extend([_DeliveryBlock("窗"), _DeliveryBlock("*Model_Space", layout=True)])
        with self.tools() as (registry, backup, _, _):
            result = await registry.call("cad_read_blocks", {"path": str(self.path), "limit": 1})
        self.assertTrue(result["blocks_truncated"])
        self.assertTrue(result["references_truncated"])
        self.assertTrue(result["blocks"][0]["attributes_truncated"])
        self.assertEqual((2, 2, 2), (result["block_total"], result["reference_total"], result["blocks"][0]["attribute_total"]))
        attribute = result["blocks"][0]["attributes"][0]
        self.assertEqual(("A", "D1", 3, 4), (attribute["tag"], attribute["value"], attribute["height"], attribute["mode"]))
        backup.assert_not_called()
        self.assert_unsaved()
        json.dumps(result, allow_nan=False)

    async def test_read_blocks_filter_bad_limit_missing_file_and_user_ownership(self):
        app = _CadApplication(docs=[self.doc])
        with self.tools(active=app) as (registry, backup, _, _):
            for limit in (True, 0, 501, 1.5):
                with self.assertRaises(ValueError):
                    await registry.call("cad_read_blocks", {"path": str(self.path), "limit": limit})
            with self.assertRaisesRegex(RuntimeError, "不存在"):
                await registry.call("cad_read_blocks", {"path": str(self.path) + "missing"})
            result = await registry.call("cad_read_blocks", {"path": str(self.path), "block_name": "无"})
            self.assertEqual(0, result["block_total"])
        self.assertEqual(0, self.doc.closed)
        self.assertFalse(app.quit_called)
        backup.assert_not_called()

    async def test_outputs_default_preview_no_com_or_directories(self):
        for tool, suffix in self.OUTPUTS.items():
            target = self.target.parent / "new-folder" / ("x" + suffix)
            with self.subTest(tool=tool), self.tools() as (registry, backup, client, _):
                result = await registry.call(tool, self.args(tool, output_path=str(target)))
                self.assertTrue(result["dry_run"])
                backup.assert_not_called()
                self.assertEqual(0, client.dispatched)
                self.assertFalse(target.parent.exists())
                json.dumps(result, allow_nan=False)

    async def test_outputs_default_refuse_existing_target(self):
        for tool, suffix in self.OUTPUTS.items():
            target = self.target.with_suffix(suffix)
            target.write_bytes(b"existing target")
            with self.subTest(tool=tool), self.tools() as (registry, backup, client, _):
                with self.assertRaisesRegex(FileExistsError, "默认不覆盖"):
                    await registry.call(tool, self.args(tool, dry_run=False))
                backup.assert_not_called()
                self.assertEqual(0, client.dispatched)
                self.assertEqual(b"existing target", target.read_bytes())

    async def test_dwg_and_dxf_all_supported_version_pairs(self):
        for header, dwg, dxf in (("AC1015", 12, 13), ("AC1018", 24, 25), ("AC1021", 36, 37), ("AC1024", 48, 49), ("AC1027", 60, 61), ("AC1032", 64, 65)):
            self.path.write_bytes(header.encode() + b"dwg")
            for tool, expected in (("cad_save_as", dwg), ("cad_export_dxf", dxf)):
                target = self.target.with_name(header).with_suffix(self.OUTPUTS[tool])
                with self.subTest(header=header, tool=tool), self.tools() as (registry, _, client, backups):
                    result = await registry.call(tool, self.args(tool, output_path=str(target), dry_run=False))
                self.assertEqual(expected, self.doc.saved_as[-1][1])
                self.assertNotEqual(str(target), self.doc.saved_as[-1][0])
                self.assertEqual(self.path.read_bytes(), backups[0].read_bytes())
                self.assertTrue(target.is_file())
                self.assertGreater(result["size"], 0)
                self.assertEqual(0, self.doc.save_called)
                self.assertTrue(client.dispatch_app.quit_called)
                self.assertEqual([], list(target.parent.glob(".xiaoa-cad-*")))
                json.dumps(result, allow_nan=False)

    async def test_outputs_use_live_documents_when_autocad_count_is_stale(self):
        for tool, suffix in self.OUTPUTS.items():
            app = _CadApplication()
            app.Documents = _StaleCadDocuments(open_result=self.doc)
            with self.subTest(tool=tool), self.tools(active=app) as (registry, backup, _, _), \
                 mock.patch.object(cad_module, "_dynamic_cad_object", side_effect=lambda _client, raw: raw):
                result = await registry.call(tool, self.args(tool, dry_run=False))
            self.assertTrue(result["ok"])
            self.assertTrue(self.target.with_suffix(suffix).is_file())
            backup.assert_called_once_with(self.path)
            self.assertEqual(self.original, self.path.read_bytes())
            self.assertFalse(app.quit_called)

    async def test_stale_count_still_protects_user_open_target(self):
        for tool, suffix in self.OUTPUTS.items():
            target = self.target.with_suffix(suffix)
            target.write_bytes(b"user drawing")
            user_doc = _CadDocument(str(target))
            app = _CadApplication()
            app.Documents = _StaleCadDocuments(docs=[user_doc], open_result=self.doc)
            with self.subTest(tool=tool), self.tools(active=app) as (registry, backup, _, _), \
                 mock.patch.object(cad_module, "_dynamic_cad_object", side_effect=lambda _client, raw: raw):
                with self.assertRaisesRegex(RuntimeError, "正开"):
                    await registry.call(tool, self.args(tool, dry_run=False, overwrite=True))
            backup.assert_not_called()
            self.assertEqual(b"user drawing", target.read_bytes())
            self.assertEqual(0, user_doc.closed)
            self.assertFalse(app.quit_called)

    async def test_unknown_header_never_guesses_save_version(self):
        self.path.write_bytes(b"AC0000whatever")
        for tool in ("cad_save_as", "cad_export_dxf"):
            with self.subTest(tool=tool), self.tools() as (registry, backup, client, _), self.assertRaisesRegex(ValueError, "拒绝"):
                await registry.call(tool, self.args(tool, dry_run=False))
            backup.assert_not_called()
            self.assertEqual(0, client.dispatched)

    async def test_same_source_hardlink_and_target_locks_refused(self):
        with self.tools() as (registry, backup, client, _):
            with self.assertRaisesRegex(ValueError, "原图相同"):
                await registry.call("cad_save_as", self.args("cad_save_as", output_path=str(self.path), overwrite=True, dry_run=False))
            import os
            os.link(self.path, self.target)
            with self.assertRaisesRegex(ValueError, "原图相同"):
                await registry.call("cad_save_as", self.args("cad_save_as", overwrite=True, dry_run=False))
            self.target.unlink()
            self.target.with_suffix(".dwl2").write_text("external lock")
            with self.assertRaisesRegex(RuntimeError, "锁文件"):
                await registry.call("cad_save_as", self.args("cad_save_as", dry_run=False, overwrite=True))
        self.assertEqual(0, client.dispatched)
        backup.assert_not_called()

    async def test_wrong_extensions_empty_path_and_directory_refused(self):
        for tool in self.OUTPUTS:
            with self.subTest(tool=tool), self.tools() as (registry, backup, client, _):
                for output in ("", str(self.target.with_suffix(".txt"))):
                    with self.assertRaises(ValueError):
                        await registry.call(tool, self.args(tool, output_path=output, dry_run=False))
                directory = self.target.with_suffix(self.OUTPUTS[tool])
                directory.mkdir()
                with self.assertRaises(ValueError):
                    await registry.call(tool, self.args(tool, output_path=str(directory), overwrite=True, dry_run=False))
                directory.rmdir()
                backup.assert_not_called()
                self.assertEqual(0, client.dispatched)


    async def test_output_open_target_preserves_user_document(self):
        self.target.write_bytes(b"old")
        user_doc = _CadDocument(str(self.target))
        app = _CadApplication(docs=[user_doc], open_result=self.doc)
        with self.tools(active=app) as (registry, backup, _, _), self.assertRaisesRegex(RuntimeError, "正开"):
            await registry.call("cad_save_as", self.args("cad_save_as", dry_run=False, overwrite=True))
        backup.assert_not_called()
        self.assertEqual(0, user_doc.closed)
        self.assertFalse(app.quit_called)
        self.assertEqual(b"old", self.target.read_bytes())

    async def test_explicit_overwrite_backs_up_old_target(self):
        for tool, suffix in self.OUTPUTS.items():
            target = self.target.with_suffix(suffix)
            target.write_bytes(b"old bytes")
            with self.subTest(tool=tool), self.tools() as (registry, backup, _, backups):
                result = await registry.call(tool, self.args(tool, dry_run=False, overwrite=True))
            self.assertEqual(2, backup.call_count)
            self.assertEqual(self.original, backups[0].read_bytes())
            self.assertEqual(b"old bytes", backups[1].read_bytes())
            self.assertEqual(str(backups[1]), result["target_backup"])
            self.assertNotEqual(b"old bytes", target.read_bytes())
            self.assertEqual(self.original, self.path.read_bytes())

    async def test_saveas_failure_never_falls_back_to_save_or_changes_target(self):
        for tool in ("cad_save_as", "cad_export_dxf"):
            target = self.target.with_suffix(self.OUTPUTS[tool])
            target.write_bytes(b"old")
            self.doc.saveas_error = RuntimeError("unsupported SaveAs version")
            with self.subTest(tool=tool), self.tools() as (registry, _, client, _):
                with self.assertRaisesRegex(RuntimeError, "未发布"):
                    await registry.call(tool, self.args(tool, dry_run=False, overwrite=True))
            self.assertEqual(b"old", target.read_bytes())
            self.assertEqual(0, self.doc.save_called)
            self.assertTrue(client.dispatch_app.quit_called)
            self.assertEqual([], list(target.parent.glob(".xiaoa-cad-*")))

    async def test_missing_or_invalid_generated_file_never_claims_success(self):
        for tool, suffix in self.OUTPUTS.items():
            for behavior in ("missing", "invalid"):
                target = self.target.with_suffix(suffix)
                target.write_bytes(b"old")
                self.doc.output_payload = behavior
                self.doc.Plot.payload = None if behavior == "missing" else b"not a PDF"
                with self.subTest(tool=tool, behavior=behavior), self.tools() as (registry, _, _, _):
                    with self.assertRaisesRegex(RuntimeError, "输出|PDF|DXF"):
                        await registry.call(tool, self.args(tool, dry_run=False, overwrite=True))
                self.assertEqual(b"old", target.read_bytes())
                self.assertEqual(self.original, self.path.read_bytes())

    async def test_atomic_no_overwrite_on_concurrent_target_creation(self):
        target = self.target.with_suffix(".pdf")
        self.doc.Plot.on_plot = lambda: target.write_bytes(b"concurrent file")
        with self.tools() as (registry, _, _, _), self.assertRaises(FileExistsError):
            await registry.call("cad_export_pdf", self.args("cad_export_pdf", dry_run=False))
        self.assertEqual(b"concurrent file", target.read_bytes())

    async def test_target_backup_failure_preserves_existing_target(self):
        self.target.write_bytes(b"old")
        with self.tools() as (registry, backup, _, _):
            real = backup.side_effect
            def fail_target(p):
                if p != self.path:
                    raise OSError("目标备份失败")
                return real(p)
            backup.side_effect = fail_target
            with self.assertRaisesRegex(OSError, "目标备份失败"):
                await registry.call("cad_save_as", self.args("cad_save_as", dry_run=False, overwrite=True))
        self.assertEqual(b"old", self.target.read_bytes())
        self.assertEqual(self.original, self.path.read_bytes())

    def test_silent_pc3_preserves_source_and_other_settings(self):
        original = self.pc3.read_bytes()
        app = SimpleNamespace(Preferences=SimpleNamespace(Files=SimpleNamespace(PrinterConfigPath=self.temp.name)))
        output = Path(self.temp.name) / "export"
        output.mkdir()
        target = cad_module._silent_pdf_pc3(app, self.pc3.name, output)
        self.assertEqual(original, self.pc3.read_bytes())
        data = target.read_bytes()
        checksum, length, packed_length = struct.unpack("<III", data[48:60])
        self.assertEqual((checksum, length, packed_length),
                         (zlib.adler32(data[60:]), len(zlib.decompress(data[60:])), len(data[60:])))
        self.assertEqual(zlib.decompress(original[60:]).replace(b'value=TRUE', b'value=FALSE'),
                         zlib.decompress(data[60:]))

    async def test_unknown_or_corrupt_pdf_driver_never_plots_or_publishes(self):
        for data in (b'invalid', _pdf_pc3_bytes(b'custom{}'), _pdf_pc3_bytes()[:-2],
                     _pdf_pc3_bytes(b'name="View_New_File\nvalue=TRUE\n' * 2)):
            with self.subTest(data=data):
                self.doc = _DeliveryDocument(str(self.path))
                self.pc3.write_bytes(data)
                with self.tools() as (registry, _, _, _), self.assertRaisesRegex(RuntimeError, "PC3|预览"):
                    await registry.call("cad_export_pdf", self.args("cad_export_pdf", dry_run=False))
                self.assertEqual([], self.doc.Plot.calls)
                self.assertFalse(self.target.with_suffix(".pdf").exists())
                self.assertEqual(self.original, self.path.read_bytes())
                self.assert_pdf_restored()

    async def test_pdf_active_foreground_exact_name_and_restores_none_config(self):
        with self.tools() as (registry, _, _, _):
            result = await registry.call("cad_export_pdf", self.args("cad_export_pdf", dry_run=False, layout_name="图纸1"))
        self.assertTrue(result["ok"])
        self.assertEqual(1, len(self.doc.Plot.calls))
        path, layout, device = self.doc.Plot.calls[0]
        self.assertEqual("成品.pdf", Path(path).name)
        self.assertEqual(("图纸1", "DWG To PDF.pc3"), (layout, device))
        self.assertEqual([("BACKGROUNDPLOT", 0), ("BACKGROUNDPLOT", 3)], self.doc.variable_calls)
        self.assert_pdf_restored()

    async def test_pdf_preserves_paper_scale_and_rotation_during_plot(self):
        original = dict(self.doc.Layouts.Item(1).settings)
        def check_settings():
            self.assertEqual(original, self.doc.ActiveLayout.settings)
        self.doc.Plot.on_plot = check_settings
        with self.tools() as (registry, _, _, _):
            await registry.call("cad_export_pdf", self.args("cad_export_pdf", dry_run=False, layout_name="图纸1"))
        self.assert_pdf_restored()

    async def test_pdf_retains_standard_scale_window_and_view(self):
        for plot_type in (3, 4):
            self.doc = _DeliveryDocument(str(self.path))
            layout = self.doc.Layouts.Item(1)
            layout.PlotType, layout.UseStandardScale, layout.StandardScale = plot_type, True, 16
            layout.PlotOrigin, layout.ViewToPlot = (3.0, 4.0), "construction"
            def check_settings():
                actual = self.doc.ActiveLayout
                self.assertEqual(plot_type, actual.PlotType)
                self.assertTrue(actual.UseStandardScale)
                self.assertEqual(16, actual.StandardScale)
                self.assertEqual((3.0, 4.0), actual.PlotOrigin)
                if plot_type == 3:
                    self.assertEqual("construction", actual.ViewToPlot)
                else:
                    self.assertEqual(((1.0, 2.0), (30.0, 40.0)), actual.GetWindowToPlot())
            self.doc.Plot.on_plot = check_settings
            with self.subTest(plot_type=plot_type), self.tools() as (registry, _, _, _):
                await registry.call("cad_export_pdf", self.args("cad_export_pdf", dry_run=False, overwrite=True, layout_name="图纸1"))
            self.assert_pdf_restored()

    async def test_pdf_dynamic_scale_and_window_use_explicit_output_parameters(self):
        original_scale = _DeliveryPlotConfiguration.GetCustomScale
        original_window = _DeliveryPlotConfiguration.GetWindowToPlot
        calls = []
        def output(config, method, outputs, variant_type):
            values = method(config)
            if not outputs:
                return values
            self.assertEqual([variant_type, variant_type], [item.vt for item in outputs])
            for item, value in zip(outputs, values):
                item.value = value
            calls.append(variant_type)
        self.doc.Layouts.Item(1).PlotType = 4
        with self.tools() as (registry, _, _, _), \
             mock.patch.object(_DeliveryPlotConfiguration, "_oleobj_", object(), create=True), \
             mock.patch.object(_DeliveryPlotConfiguration, "GetCustomScale",
                               lambda config, *args: output(config, original_scale, args, 0x4005)), \
             mock.patch.object(_DeliveryPlotConfiguration, "GetWindowToPlot",
                               lambda config, *args: output(config, original_window, args, 0x400C)):
            await registry.call("cad_export_pdf", self.args("cad_export_pdf", dry_run=False, layout_name="图纸1"))
        self.assertEqual([0x4005, 0x4005, 0x400C, 0x400C], calls)
        self.assert_pdf_restored()

    async def test_pdf_silent_media_and_scale_setters_never_publish(self):
        for setting in ("paper", "scale"):
            self.doc = _DeliveryDocument(str(self.path))
            with self.subTest(setting=setting), self.tools() as (registry, _, _, _):
                if setting == "paper":
                    patch = mock.patch.object(_DeliveryPlotConfiguration, "CanonicalMediaName",
                        property(lambda obj: obj.settings["paper"], lambda obj, value: None))
                else:
                    patch = mock.patch.object(_DeliveryPlotConfiguration, "SetCustomScale", lambda *args: None)
                with patch, self.assertRaisesRegex(RuntimeError, "未保留"):
                    await registry.call("cad_export_pdf", self.args("cad_export_pdf", dry_run=False, layout_name="图纸1"))
            self.assertEqual([], self.doc.Plot.calls)
            self.assertFalse(self.target.with_suffix(".pdf").exists())
            self.assert_pdf_restored()

    async def test_pdf_incompatible_paper_fails_without_publishing(self):
        def reject_media(layout, value):
            raise RuntimeError("PDF device cannot print this paper size")
        with self.tools() as (registry, _, _, _), mock.patch.object(
                _DeliveryPlotConfiguration, "CanonicalMediaName",
                property(lambda obj: obj.settings["paper"], reject_media)):
            with self.assertRaisesRegex(RuntimeError, "保留打印设置|paper size"):
                await registry.call("cad_export_pdf", self.args("cad_export_pdf", dry_run=False, layout_name="图纸1"))
        self.assertEqual([], self.doc.Plot.calls)
        self.assertFalse(self.target.with_suffix(".pdf").exists())
        self.assert_pdf_restored()

    async def test_pdf_plot_false_error_and_renamed_output_restore_and_fail(self):
        for behavior in ("false", "raise", "rename"):
            self.doc = _DeliveryDocument(str(self.path))
            self.doc.Plot.result = behavior != "false"
            self.doc.Plot.error = RuntimeError("plot failed") if behavior == "raise" else None
            self.doc.Plot.rename_output = behavior == "rename"
            with self.subTest(behavior=behavior), self.tools() as (registry, _, _, _), self.assertRaises(RuntimeError):
                await registry.call("cad_export_pdf", self.args("cad_export_pdf", dry_run=False, layout_name="图纸1"))
            self.assertFalse(self.target.with_suffix(".pdf").exists())
            self.assert_pdf_restored()

    async def test_pdf_missing_device_restores_all_settings(self):
        with self.tools() as (registry, _, _, _), self.assertRaisesRegex(RuntimeError, "未安装"):
            await registry.call("cad_export_pdf", self.args("cad_export_pdf", dry_run=False, plot_config="missingPDF.pc3", layout_name="图纸1"))
        self.assertEqual([], self.doc.Plot.calls)
        self.assert_pdf_restored()

    async def test_pdf_missing_layout_no_global_changes(self):
        with self.tools() as (registry, _, _, _), self.assertRaisesRegex(RuntimeError, "布局不存在"):
            await registry.call("cad_export_pdf", self.args("cad_export_pdf", dry_run=False, layout_name="not-found"))
        self.assert_pdf_restored()

    async def test_pdf_restore_failure_does_not_publish(self):
        layout = self.doc.Layouts.Item(1)
        with self.tools() as (registry, _, _, _), mock.patch.object(layout, "CopyFrom", side_effect=RuntimeError("restore failed")):
            with self.assertRaisesRegex(RuntimeError, "恢复失败"):
                await registry.call("cad_export_pdf", self.args("cad_export_pdf", dry_run=False, layout_name="图纸1"))
        self.assertEqual(3, self.doc.GetVariable("BACKGROUNDPLOT"))
        self.assertIs(self.doc.Layouts.Item(0), self.doc.ActiveLayout)
        self.assertTrue(self.doc.PlotConfigurations.created[0].deleted)
        self.assertFalse(self.target.with_suffix(".pdf").exists())

    async def test_pdf_snapshot_failure_does_not_plot(self):
        with self.tools() as (registry, _, client, _), mock.patch.object(self.doc.PlotConfigurations, "Add", side_effect=AttributeError("unsupported Add")):
            with self.assertRaisesRegex(RuntimeError, "不支持"):
                await registry.call("cad_export_pdf", self.args("cad_export_pdf", dry_run=False))
        self.assertEqual([], self.doc.Plot.calls)
        self.assert_pdf_restored()
        self.assertTrue(client.dispatch_app.quit_called)

    async def test_invalid_pc3_rejected_without_com(self):
        for config in ("printer.pc3", "PDF.txt", "", None):
            with self.subTest(config=config), self.tools() as (registry, _, client, _), self.assertRaises(ValueError):
                await registry.call("cad_export_pdf", self.args("cad_export_pdf", dry_run=False, plot_config=config))
            self.assertEqual(0, client.dispatched)

    async def test_unsupported_methods_and_null_returns_never_save(self):
        for tool, obj, method in (
            ("cad_hatch", self.doc.ModelSpace, "AddHatch"),
            ("cad_add_leader", self.doc.ModelSpace, "AddLeader"),
            ("cad_add_multileader", self.doc.ModelSpace, "AddMLeader"),
            ("cad_insert_block", self.doc.ModelSpace, "InsertBlock"),
            ("cad_add_attribute", self.doc.Blocks.Item(0), "AddAttribute"),
        ):
            self.doc.ModelSpace.added.clear()
            with self.subTest(tool=tool), self.tools() as (registry, backup, _, _), mock.patch.object(obj, method, None):
                with self.assertRaisesRegex(RuntimeError, "不支持"):
                    await registry.call(tool, self.args(tool, dry_run=False, overwrite=True))
                backup.assert_not_called()
                self.assert_unsaved()
            with self.tools() as (registry, backup, _, _), mock.patch.object(obj, method, return_value=None):
                with self.assertRaisesRegex(RuntimeError, "未保存|不支持"):
                    await registry.call(tool, self.args(tool, dry_run=False, overwrite=True))
                backup.assert_called_once()
                self.assert_unsaved()

    async def test_runtime_com_error_closes_and_discards_unsaved_changes(self):
        with self.tools() as (registry, backup, client, _), mock.patch.object(self.doc.ModelSpace, "AddMLeader", side_effect=RuntimeError("unsupported property")):
            with self.assertRaisesRegex(RuntimeError, "未保存"):
                await registry.call("cad_add_multileader", self.args("cad_add_multileader", dry_run=False, overwrite=True))
        backup.assert_called_once()
        self.assertEqual(1, self.doc.closed)
        self.assertTrue(client.dispatch_app.quit_called)
        self.assert_unsaved()

    async def test_new_tools_never_quit_users_application_and_accept_own_locks(self):
        for tool in ("cad_add_multileader", "cad_export_pdf"):
            self.doc = _DeliveryDocument(str(self.path))
            app = _CadApplication(open_result=self.doc)
            with self.subTest(tool=tool), self.tools(active=app) as (registry, _, _, _):
                await registry.call(tool, self.args(tool, dry_run=False, overwrite=True))
            self.assertFalse(app.quit_called)
            self.assertEqual(1, self.doc.closed)
            lock = self.path.with_suffix(".dwl")
            if lock.exists():
                lock.unlink()
            self.doc = _DeliveryDocument(str(self.path))
            with self.tools(lock_on_open=True) as (registry, _, _, _):
                result = await registry.call(tool, self.args(tool, dry_run=False, overwrite=True))
            self.assertTrue(result["ok"])
            lock.unlink()


class _DimensionStyle:
    """DimStyle exposes CopyFrom, NOT TextHeight or other dimension properties."""
    def __init__(self, name, values=None):
        self.Name = name
        self.values = dict(values or {
            "TextHeight": 2.5, "ArrowheadSize": 2.5, "ScaleFactor": 1.0, "LinearScaleFactor": 1.0,
            "TextGap": 0.5, "ExtensionLineOffset": 0.6, "ExtensionLineExtend": 1.0,
            "PrimaryUnitsPrecision": 2, "TextColor": 256, "DimensionLineColor": 256,
            "ExtensionLineColor": 256, "TextStyle": "Standard", "TextPrefix": "", "TextSuffix": "",
        })
        self.copy_calls = []

    def __setattr__(self, key, value):
        if key not in ("Name", "values", "copy_calls", "CopyFrom"):
            raise AttributeError("DimStyle only supports CopyFrom, not " + key)
        object.__setattr__(self, key, value)

    def CopyFrom(self, source):
        assert isinstance(source, (_DimensionStyle, _DimensionEntity))
        self.copy_calls.append(source)
        self.values = dict(source.values)


class _DimensionStyles(_CadCollection):
    def Add(self, name):
        style = _DimensionStyle(name)
        self.items.append(style)
        return style


class _DimensionEntity(_CadEntity):
    def __init__(self, space, kind, position):
        super().__init__(kind, position=position)
        self._space = space
        self.StyleName = space.doc.ActiveDimStyle.Name
        self.Handle = "D" + str(space.Count + 1)

    def __setattr__(self, key, value):
        if key == "StyleName":
            style = next(x for x in self._space.doc.DimStyles.items if x.Name.casefold() == value.casefold())
            object.__setattr__(self, "values", dict(style.values))
        if key in self.__dict__.get("values", {}):
            self.values[key] = value
        else:
            object.__setattr__(self, key, value)

    def __getattr__(self, key):
        if key in self.values:
            return self.values[key]
        raise AttributeError(key)

    def Delete(self):
        if self in self._space.added:
            self._space.added.remove(self)
        else:
            self._space.entities.remove(self)


class _DimensionSpace(_CadModelSpace):
    def __init__(self, doc):
        super().__init__([])
        self.doc = doc

    def dimension(self, kind, *args):
        for arg in args:
            if isinstance(arg, _FakeVariant):
                assert arg.vt == 0x2005 and len(arg.value) == 3
        self.add_dim_calls.append((kind, *args))
        point = next(_unwrap(arg) for arg in args if isinstance(arg, _FakeVariant))
        entity = _DimensionEntity(self, kind, point)
        self.added.append(entity)
        return entity

    def AddDimDiametric(self, first, second, leader):
        assert type(leader) is float and leader > 0
        return self.dimension("AcDbDiametricDimension", first, second, leader)

    def AddDimArc(self, center, first, second, point):
        return self.dimension("AcDbArcDimension", center, first, second, point)

    def AddDimOrdinate(self, point, end, use_x):
        assert type(use_x) is bool
        return self.dimension("AcDbOrdinateDimension", point, end, use_x)

    def AddDimRotated(self, first, second, point, rotation):
        assert type(rotation) is float
        return self.dimension("AcDbRotatedDimension", first, second, point, rotation)


class CadExtendedDimensionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = _dwg_file(Path(self.temp.name))
        self.doc = _CadDocument(str(self.path))
        self.doc.DimStyles = _DimensionStyles([_DimensionStyle("ISO-25"), _DimensionStyle("施工")])
        self.doc.ActiveDimStyle = self.doc.DimStyles.Item(0)
        self.doc.ModelSpace = _DimensionSpace(self.doc)
        self.doc.Layers.items.append(_CadLayer("尺寸"))
        self.defaults = {"diameter": {"p1": [-10, 0], "p2": [10, 0]},
                         "arc_length": {"center": [0, 0], "p1": [10, 0], "p2": [0, 10], "text_pos": [15, 15]},
                         "ordinate": {"p1": [10, 20], "text_pos": [30, 20], "axis": "x"},
                         "baseline": {"p1": [0, 0], "points": [[10, 0], [20, 0]], "text_pos": [0, -10], "spacing": 5}}

    def item(self, kind, **kwargs):
        return {"type": kind, **self.defaults[kind], **kwargs}

    @contextmanager
    def tools(self, active=None):
        client = _CadClient(active=active, dispatch_doc=self.doc)
        with mock.patch.object(cad_module, "_backup_drawing", return_value="原图.备份.dwg") as backup, _registered_tools(client) as registry:
            yield registry, backup, client

    async def dimensions(self, items, **kwargs):
        with self.tools() as (registry, _, _):
            return await registry.call("cad_add_dimensions", {"path": str(self.path), "items": items, **kwargs})

    def assert_unsaved(self):
        self.assertEqual([], self.doc.saved_as)
        self.assertEqual(0, self.doc.save_called)

    async def test_diameter_two_endpoints_leader_and_named_style(self):
        result = await self.dimensions([self.item("diameter", layer="尺寸", style_name="施工", text_pos=[15, 5])])
        self.assertTrue(result["ok"])
        kind, a, b, leader = self.doc.ModelSpace.add_dim_calls[0]
        self.assertEqual("AcDbDiametricDimension", kind)
        self.assertEqual((-10, 0, 0), _unwrap(a))
        self.assertEqual((10, 0, 0), _unwrap(b))
        self.assertEqual(2.0, leader)
        entity = self.doc.ModelSpace.added[0]
        self.assertEqual("施工", entity.StyleName)
        self.assertEqual("尺寸", entity.Layer)
        self.assertEqual((15, 5, 0), _unwrap(entity.TextPosition))
        self.assertEqual("", entity.TextOverride)

    async def test_arc_length_four_wcs_points(self):
        result = await self.dimensions([self.item("arc_length")])
        self.assertEqual(1, result["added"])
        call = self.doc.ModelSpace.add_dim_calls[0]
        self.assertEqual("AcDbArcDimension", call[0])
        self.assertEqual([(0, 0, 0), (10, 0, 0), (0, 10, 0), (15, 15, 0)], [_unwrap(x) for x in call[1:]])

    async def test_ordinate_x_y_boolean_not_text_override(self):
        result = await self.dimensions([self.item("ordinate"), self.item("ordinate", axis="y")])
        self.assertEqual(2, result["added"])
        self.assertEqual([True, False], [x[-1] for x in self.doc.ModelSpace.add_dim_calls])
        self.assertTrue(all(e.TextOverride == "" for e in self.doc.ModelSpace.added))

    async def test_baseline_common_origin_and_spacing_both_sides(self):
        result = await self.dimensions([self.item("baseline"), self.item("baseline", text_pos=[0, 10])])
        self.assertEqual(4, result["added"])
        calls = self.doc.ModelSpace.add_dim_calls
        self.assertEqual([(0, 0, 0)] * 4, [_unwrap(x[1]) for x in calls])
        self.assertEqual([(0, -10, 0), (0, -15, 0), (0, 10, 0), (0, 15, 0)], [_unwrap(x[3]) for x in calls])

    async def test_baseline_rotated_and_nonzero_elevation(self):
        result = await self.dimensions([self.item("baseline", p1=[0, 0, 5], points=[[0, 10, 5], [0, 20, 5]],
                                                text_pos=[-10, 0, 5], rotation=math.pi/2)])
        self.assertEqual(2, result["added"])
        self.assertAlmostEqual(-15, _unwrap(self.doc.ModelSpace.add_dim_calls[1][3])[0])
        self.assertEqual(5, _unwrap(self.doc.ModelSpace.add_dim_calls[1][3])[2])

    async def test_dimension_preview_no_creates_backups_or_saves(self):
        with self.tools() as (registry, backup, _):
            result = await registry.call("cad_add_dimensions", {"path": str(self.path), "dry_run": True,
                "items": [self.item(k) for k in self.defaults]})
        self.assertEqual(5, result["planned"])
        self.assertEqual(0, result["added"])
        self.assertEqual([0, -15, 0], result["preview"][-1]["arguments"][1][2])
        self.assertEqual([], self.doc.ModelSpace.added)
        backup.assert_not_called()
        self.assert_unsaved()
        json.dumps(result, allow_nan=False)

    async def test_invalid_geometry_and_types_never_create_or_save(self):
        invalid = [self.item("diameter", p2=[-10, 0]), self.item("diameter", leader_length=0),
                   self.item("diameter", p2=[10, 0, 1]), self.item("arc_length", p2=[0, 20]),
                   self.item("arc_length", text_pos=[0, 0]), self.item("arc_length", p1=[float("nan"), 0]),
                   self.item("ordinate", axis="z"), self.item("ordinate", text_pos=[10, 20]),
                   self.item("baseline", spacing=0), self.item("baseline", points=[[10, 0], [10, 5]]),
                   self.item("baseline", text_pos=[0, 0]), self.item("baseline", points=[[0, 10]]),
                   {"type": "typo", "p1": [0, 0]}, "not an object"]
        with self.tools() as (registry, backup, _):
            result = await registry.call("cad_add_dimensions", {"path": str(self.path), "items": invalid})
        self.assertEqual(len(invalid), len(result["failed"]))
        self.assertFalse(result["ok"])
        backup.assert_not_called()
        self.assert_unsaved()

    async def test_bad_style_layer_or_unsupported_api_are_reported(self):
        items = [self.item("diameter", style_name="missing"), self.item("ordinate", layer="missing"), self.item("arc_length")]
        with self.tools() as (registry, backup, _), mock.patch.object(self.doc.ModelSpace, "AddDimArc", None):
            result = await registry.call("cad_add_dimensions", {"path": str(self.path), "items": items})
        self.assertEqual(3, len(result["failed"]))
        backup.assert_not_called()
        self.assert_unsaved()

    async def test_batch_valid_items_preserved_with_invalid_item(self):
        result = await self.dimensions([self.item("diameter"), self.item("arc_length", p2=[0, 30])])
        self.assertEqual(1, result["added"])
        self.assertFalse(result["ok"])
        self.assertEqual(1, len(self.doc.saved_as))

    async def test_com_failure_or_null_discards_entire_batch(self):
        for value in (RuntimeError("busy unsupported"), None):
            with self.subTest(value=value), self.tools() as (registry, backup, _):
                patch = (mock.patch.object(self.doc.ModelSpace, "AddDimArc", side_effect=value) if value else
                         mock.patch.object(self.doc.ModelSpace, "AddDimArc", return_value=None))
                with patch, self.assertRaisesRegex(RuntimeError, "失败|未返回"):
                    await registry.call("cad_add_dimensions", {"path": str(self.path), "items": [self.item("diameter"), self.item("arc_length")]})
            backup.assert_called_once()
            self.assert_unsaved()

    async def test_top_level_invalid_inputs_rejected_before_com(self):
        for extra in ({"items": []}, {"items": {}}, {"dry_run": 0}):
            with self.subTest(extra=extra), self.tools() as (registry, backup, client), self.assertRaises(ValueError):
                await registry.call("cad_add_dimensions", {"path": str(self.path), "items": [self.item("diameter")], **extra})
            self.assertEqual(0, client.dispatched)
            backup.assert_not_called()

    async def test_user_open_doc_and_locked_layer_not_modified(self):
        app = _CadApplication(docs=[self.doc])
        with self.tools(active=app) as (registry, backup, _), self.assertRaisesRegex(RuntimeError, "正开"):
            await registry.call("cad_add_dimensions", {"path": str(self.path), "items": [self.item("diameter")]})
        self.assertEqual(0, self.doc.closed)
        self.assertFalse(app.quit_called)
        backup.assert_not_called()
        self.doc.Layers.Item(1).Lock = True
        result = await self.dimensions([self.item("diameter", layer="尺寸")])
        self.assertFalse(result["ok"])
        self.assert_unsaved()

    async def test_list_styles_readonly_and_truncated(self):
        with self.tools() as (registry, backup, _):
            result = await registry.call("cad_list_dimstyles", {"path": str(self.path), "limit": 1})
        self.assertEqual("ISO-25", result["active"])
        self.assertEqual(2, result["total"])
        self.assertTrue(result["truncated"])
        backup.assert_not_called()
        self.assert_unsaved()
        json.dumps(result)

    async def test_style_default_previews_do_not_modify(self):
        for name, args in (("cad_create_dimstyle", {"style_name": "新样式", "properties": {"text_height": 3}}),
                           ("cad_update_dimstyle", {"style_name": "施工", "properties": {"text_height": 4}}),
                           ("cad_set_dimstyle", {"style_name": "施工"})):
            with self.subTest(name=name), self.tools() as (registry, backup, _):
                result = await registry.call(name, {"path": str(self.path), **args})
            self.assertTrue(result["dry_run"])
            backup.assert_not_called()
            self.assert_unsaved()
            self.assertEqual(2, self.doc.DimStyles.Count)
            self.assertEqual([], self.doc.ModelSpace.added)
            self.assertEqual("ISO-25", self.doc.ActiveDimStyle.Name)

    async def test_create_style_copies_base_sets_properties_and_deletes_proxy(self):
        properties = {"text_height": 3.5, "arrow_size": 2, "overall_scale": 50, "measurement_scale": 0.001,
                      "text_gap": 0.7, "extension_offset": 0.2, "extension_extend": 1.2, "precision": 3,
                      "text_color": 1, "dimension_color": 2, "extension_color": 3, "text_style": "Standard",
                      "prefix": "D=", "suffix": " mm"}
        with self.tools() as (registry, backup, _):
            result = await registry.call("cad_create_dimstyle", {"path": str(self.path), "style_name": "新样式",
                "base_style": "施工", "properties": properties, "dry_run": False, "overwrite": True})
        self.assertTrue(result["ok"])
        style = self.doc.DimStyles.Item(2)
        self.assertEqual(3.5, style.values["TextHeight"])
        self.assertEqual(50.0, style.values["ScaleFactor"])
        self.assertEqual("D=", style.values["TextPrefix"])
        self.assertEqual(3, style.values["PrimaryUnitsPrecision"])
        self.assertEqual([], self.doc.ModelSpace.added)
        self.assertEqual("ISO-25", self.doc.ActiveDimStyle.Name)
        backup.assert_called_once()

    async def test_create_clone_without_properties_uses_no_entity(self):
        with self.tools() as (registry, _, _):
            await registry.call("cad_create_dimstyle", {"path": str(self.path), "style_name": "Clone", "dry_run": False, "overwrite": True})
        self.assertEqual(self.doc.DimStyles.Item(0).values, self.doc.DimStyles.Item(2).values)
        self.assertEqual([], self.doc.ModelSpace.add_dim_calls)

    async def test_update_style_preserves_active_and_other_properties(self):
        self.doc.DimStyles.Item(1).values["TextSuffix"] = "m"
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_update_dimstyle", {"path": str(self.path), "style_name": "施工",
                "properties": {"text_height": 5}, "dry_run": False, "overwrite": True})
        self.assertTrue(result["ok"])
        self.assertEqual(5, self.doc.DimStyles.Item(1).values["TextHeight"])
        self.assertEqual("m", self.doc.DimStyles.Item(1).values["TextSuffix"])
        self.assertEqual("ISO-25", self.doc.ActiveDimStyle.Name)
        self.assertEqual([], self.doc.ModelSpace.added)

    async def test_set_current_style_only_affects_current(self):
        with self.tools() as (registry, _, _):
            result = await registry.call("cad_set_dimstyle", {"path": str(self.path), "style_name": "施工", "dry_run": False, "overwrite": True})
        self.assertEqual("施工", self.doc.ActiveDimStyle.Name)
        self.assertEqual("ISO-25", result["previous_style"])
        self.assertEqual([], self.doc.ModelSpace.added)

    async def test_style_input_validation_and_duplicate_rejection(self):
        bad = [{"text_height": 0}, {"precision": 9}, {"precision": True}, {"text_height": float("nan")},
               {"unknown": 1}, {}, {"arrow_size": -1}, {"overall_scale": -1}, {"overall_scale": 0}, {"text_height": "3"}, {"prefix": None}]
        for properties in bad:
            with self.subTest(properties=properties), self.tools() as (registry, backup, client), self.assertRaises(ValueError):
                await registry.call("cad_update_dimstyle", {"path": str(self.path), "style_name": "施工", "properties": properties})
            self.assertEqual(0, client.dispatched)
            backup.assert_not_called()
        for name, args in (("cad_create_dimstyle", {"style_name": "ISO-25"}),
                           ("cad_set_dimstyle", {"style_name": "missing"}),
                           ("cad_update_dimstyle", {"style_name": "施工", "properties": {"text_style": "missing"}})):
            with self.tools() as (registry, backup, _), self.assertRaises(ValueError):
                await registry.call(name, {"path": str(self.path), **args})
            backup.assert_not_called()
        self.assert_unsaved()

    async def test_explicit_overwrite_required_and_proxy_cleanup_failure_never_saves(self):
        with self.tools() as (registry, backup, client), self.assertRaises(ValueError):
            await registry.call("cad_set_dimstyle", {"path": str(self.path), "style_name": "施工", "dry_run": False})
        backup.assert_not_called()
        self.assertEqual(0, client.dispatched)
        with self.tools() as (registry, backup, _), mock.patch.object(_DimensionEntity, "Delete", side_effect=RuntimeError("delete failed")):
            with self.assertRaisesRegex(RuntimeError, "失败"):
                await registry.call("cad_update_dimstyle", {"path": str(self.path), "style_name": "施工",
                    "properties": {"text_height": 3}, "dry_run": False, "overwrite": True})
        self.assert_unsaved()

    async def test_apply_style_only_selected_dimensions_and_preview(self):
        entity = self.doc.ModelSpace.AddDimRotated(_FakeVariant(0x2005, [0,0,0]), _FakeVariant(0x2005, [10,0,0]), _FakeVariant(0x2005, [0,5,0]), 0.0)
        entity.Layer = "尺寸"
        text = _CadEntity("AcDbText", layer="尺寸")
        self.doc.ModelSpace.entities.append(text)
        for dry_run in (True, False):
            with self.tools() as (registry, backup, _):
                result = await registry.call("cad_apply_dimstyle", {"path": str(self.path), "style_name": "施工", "layer": "尺寸", "dry_run": dry_run})
            self.assertEqual(1, result["matched"])
            self.assertEqual("ISO-25" if dry_run else "施工", entity.StyleName)
            if dry_run:
                backup.assert_not_called()
        self.assertNotIn("StyleName", text.__dict__)

    async def test_apply_style_requires_filter_and_respects_locked_layers(self):
        with self.tools() as (registry, _, client), self.assertRaises(ValueError):
            await registry.call("cad_apply_dimstyle", {"path": str(self.path), "style_name": "施工"})
        self.assertEqual(0, client.dispatched)
        entity = self.doc.ModelSpace.AddDimRotated(_FakeVariant(0x2005, [0,0,0]), _FakeVariant(0x2005, [10,0,0]), _FakeVariant(0x2005, [0,5,0]), 0.0)
        entity.Layer = "尺寸"
        self.doc.Layers.Item(1).Lock = True
        with self.tools() as (registry, backup, _), self.assertRaisesRegex(RuntimeError, "锁"):
            await registry.call("cad_apply_dimstyle", {"path": str(self.path), "style_name": "施工", "layer": "尺寸", "dry_run": False})
        backup.assert_not_called()
        self.assert_unsaved()


class _Solid3D:
    """Strict ActiveX contract fake with independently computed box intersections."""
    def __init__(self, space, kind="AcDb3dSolid", center=(0., 0., 0.), size=(2., 4., 6.), volume=48.):
        self.space, self.ObjectName = space, kind
        space.sequence += 1
        self.Handle = format(space.sequence, "X")
        self.Layer, self.Volume, self.Area = "0", volume, 4.
        self.Thickness = 0.0
        self.center, self.size = list(center), list(size)
        self.Closed = True
        self.calls = []
        space.entities.append(self)

    @property
    def Centroid(self):
        return tuple(self.center[:2])  # official ActiveX contract has XY only

    def GetBoundingBox(self):
        return tuple(c-s/2 for c,s in zip(self.center,self.size)), tuple(c+s/2 for c,s in zip(self.center,self.size))

    def Copy(self):
        self.calls.append(("Copy",))
        entity = _Solid3D(self.space, self.ObjectName, self.center, self.size, self.Volume)
        entity.Layer, entity.Area, entity.Closed = self.Layer, self.Area, self.Closed
        return entity

    def Delete(self):
        self.calls.append(("Delete",))
        self.space.entities.remove(self)

    def CheckInterference(self, other, create):
        assert create is True
        self.calls.append(("CheckInterference", other.Handle))
        a, b = self.GetBoundingBox(), other.GetBoundingBox()
        low, high = [max(x,y) for x,y in zip(a[0],b[0])], [min(x,y) for x,y in zip(a[1],b[1])]
        sizes = [max(0.,hi-lo) for lo,hi in zip(low,high)]
        volume = math.prod(sizes)
        if volume == 0:
            return None, False
        return _Solid3D(self.space, center=[(x+y)/2 for x,y in zip(low,high)], size=sizes, volume=volume), True

    def Boolean(self, operation, other):
        self.calls.append(("Boolean", operation))
        a, b = self.GetBoundingBox(), other.GetBoundingBox()
        overlap = math.prod(max(0., min(x,y)-max(u,v)) for x,y,u,v in zip(a[1],b[1],a[0],b[0]))
        self.Volume = {0:self.Volume+other.Volume-overlap, 1:overlap, 2:self.Volume-overlap}[operation]
        other.Delete()  # AutoCAD may consume the operand

    def Rotate3D(self, start, end, angle):
        assert isinstance(start, _FakeVariant) and start.vt == 0x2005
        assert isinstance(end, _FakeVariant) and type(angle) is float
        self.calls.append(("Rotate3D", start, end, angle))

    def Mirror3D(self, first, second, third):
        assert all(isinstance(p,_FakeVariant) and p.vt == 0x2005 for p in (first,second,third))
        self.calls.append(("Mirror3D", first, second, third))
        return self.Copy()

    def SliceSolid(self, p1, p2, p3, negative):
        assert negative is True
        assert all(isinstance(p,_FakeVariant) and p.vt == 0x2005 for p in (p1,p2,p3))
        self.calls.append(("SliceSolid", p1, p2, p3, negative))
        self.Volume /= 2
        half = self.Copy()
        half.center[2] -= 1
        self.center[2] += 1
        return half


class _SolidSpace(_CadModelSpace):
    def __init__(self):
        super().__init__([])
        self.sequence, self.solid_calls = 100, []
        self.region_count, self.interference_mode = 1, "normal"

    def primitive(self, method, center, size, volume, *params):
        assert isinstance(center, _FakeVariant) and center.vt == 0x2005 and len(center.value)==3
        self.solid_calls.append((method, center, *params))
        return _Solid3D(self, center=_unwrap(center), size=size, volume=volume)

    def AddBox(self, center, length, width, height):
        return self.primitive("AddBox",center,[length,width,height],length*width*height,length,width,height)

    def AddWedge(self, center, length, width, height):
        return self.primitive("AddWedge",center,[length,width,height],length*width*height/2,length,width,height)

    def AddCylinder(self, center, radius, height):
        return self.primitive("AddCylinder",center,[2*radius,2*radius,height],math.pi*radius**2*height,radius,height)

    def AddCone(self, center, radius, height):
        return self.primitive("AddCone",center,[2*radius,2*radius,height],math.pi*radius**2*height/3,radius,height)

    def AddSphere(self, center, radius):
        return self.primitive("AddSphere",center,[2*radius]*3,4*math.pi*radius**3/3,radius)

    def AddTorus(self, center, radius, tube):
        return self.primitive("AddTorus",center,[2*(radius+tube)]*2+[2*tube],2*math.pi**2*radius*tube**2,radius,tube)

    def AddRegion(self, curves):
        assert isinstance(curves,_FakeVariant) and curves.vt == 0x2009
        assert len(curves.value)==1 and curves.value[0].ObjectName in ("AcDbCircle","AcDbPolyline")
        curve = curves.value[0]
        self.solid_calls.append(("AddRegion",curves))
        regions = [_Solid3D(self,kind="AcDbRegion",volume=1) for i in range(self.region_count)]
        for region in regions:
            region.Area = curve.Area
        return tuple(regions)

    def AddExtrudedSolid(self, region, height, taper):
        assert region.ObjectName == "AcDbRegion" and type(height) is float and type(taper) is float
        self.solid_calls.append(("AddExtrudedSolid",region.Handle,height,taper))
        return _Solid3D(self,volume=region.Area*abs(height))

    def AddRevolvedSolid(self, region, point, direction, angle):
        assert region.ObjectName == "AcDbRegion" and type(angle) is float
        assert all(isinstance(p,_FakeVariant) and p.vt == 0x2005 for p in (point,direction))
        self.solid_calls.append(("AddRevolvedSolid",region.Handle,point,direction,angle))
        return _Solid3D(self,volume=10*angle)


class Cad3DTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=_dwg_file(Path(self.temp.name))
        self.doc=_CadDocument(str(self.path))
        self.doc.ModelSpace=_SolidSpace()
        self.space=self.doc.ModelSpace
        self.doc.Layers.items.append(_CadLayer("三维"))
        self.first=_Solid3D(self.space)
        self.second=_Solid3D(self.space,center=(1,0,0))
        self.profile=_Solid3D(self.space,kind="AcDbCircle",size=(2,2,0))
        self.args_by_tool={
            "cad_create_3d_solid":{"kind":"box","center":[10,20,30],"length":2,"width":4,"height":6},
            "cad_extrude":{"profile_handle":self.profile.Handle,"height":5},
            "cad_revolve":{"profile_handle":self.profile.Handle,"axis_point":[2,0,0],"axis_direction":[0,2,0]},
            "cad_boolean":{"target_handle":self.first.Handle,"tool_handle":self.second.Handle,"operation":"subtract"},
            "cad_rotate_3d":{"handles":[self.first.Handle],"axis_start":[0,0,0],"axis_end":[1,2,3],"angle":math.pi/2},
            "cad_mirror_3d":{"handles":[self.first.Handle],"plane_p1":[0,0,0],"plane_p2":[1,0,0],"plane_p3":[0,1,0]},
            "cad_slice_solid":{"handle":self.first.Handle,"plane_p1":[0,0,0],"plane_p2":[1,0,0],"plane_p3":[0,1,0]},
        }

    @contextmanager
    def tools(self, active=None):
        client=_CadClient(active=active,dispatch_doc=self.doc)
        with mock.patch.object(cad_module,"_backup_drawing",return_value="原图.备份.dwg") as backup, _registered_tools(client) as registry:
            yield registry,backup,client

    def args(self,name,execute=False,**extra):
        return {"path":str(self.path),**self.args_by_tool.get(name,{}),
                **({"dry_run":False,"overwrite":True} if execute else {}),**extra}

    def assert_unsaved(self):
        self.assertEqual([],self.doc.saved_as)
        self.assertEqual(0,self.doc.save_called)

    async def test_all_writes_default_preview_without_creating_or_modifying(self):
        with self.tools() as (registry,backup,_):
            for name in self.args_by_tool:
                with self.subTest(name=name):
                    result=await registry.call(name,self.args(name))
                    self.assertTrue(result["dry_run"])
                    json.dumps(result,allow_nan=False)
                    self.assertEqual(3,self.space.Count)
        self.assertEqual([],self.space.solid_calls)
        self.assertEqual([],self.first.calls)
        backup.assert_not_called()
        self.assert_unsaved()

    async def test_primitives_real_api_signature_and_positive_volume(self):
        cases=[("box",{"length":2,"width":4,"height":6},48),
               ("wedge",{"length":2,"width":4,"height":6},24),
               ("cylinder",{"radius":2,"height":6},24*math.pi),
               ("cone",{"radius":2,"height":6},8*math.pi),
               ("sphere",{"radius":2},32*math.pi/3),
               ("torus",{"radius":5,"tube_radius":2},40*math.pi**2)]
        with self.tools() as (registry,backup,_):
            for kind,params,volume in cases:
                with self.subTest(kind=kind):
                    result=await registry.call("cad_create_3d_solid",{"path":str(self.path),"kind":kind,"center":[10,20,30],
                        "layer":"三维","dry_run":False,"overwrite":True,**params})
                    record=result["entities"][0]
                    self.assertAlmostEqual(volume,record["volume"])
                    self.assertEqual("三维",record["layer"])
                    self.assertEqual([10,20],record["centroid"])
                    self.assertEqual((10,20,30),_unwrap(self.space.solid_calls[-1][1]))
                    self.assertEqual(3,len(record["bbox"]["min"]))
                    json.dumps(result,allow_nan=False)
        self.assertEqual(6,backup.call_count)

    async def test_read_solids_filters_limits_and_preserves_user_doc(self):
        self.second.Layer="三维"
        app=_CadApplication(docs=[self.doc])
        with self.tools(active=app) as (registry,backup,_):
            result=await registry.call("cad_read_solids",self.args("cad_read_solids",limit=1))
            filtered=await registry.call("cad_read_solids",self.args("cad_read_solids",layer="三维"))
        self.assertEqual(2,result["total"])
        self.assertTrue(result["truncated"])
        self.assertEqual(self.second.Handle,filtered["entities"][0]["handle"])
        self.assertEqual(0,self.doc.closed)
        self.assertFalse(app.quit_called)
        backup.assert_not_called()
        self.assert_unsaved()

    async def test_extrude_converts_copied_profile_and_cleans_temporary_region(self):
        with self.tools() as (registry,backup,_):
            result=await registry.call("cad_extrude",self.args("cad_extrude",True,height=-5,taper_angle=0.1))
        self.assertEqual(20,result["entities"][0]["volume"])
        self.assertTrue(result["profile_preserved"])
        self.assertIn(self.profile,self.space.entities)
        self.assertEqual(4,self.space.Count)
        self.assertFalse(any(e.ObjectName=="AcDbRegion" for e in self.space.entities))
        self.assertEqual(-5.,self.space.solid_calls[-1][2])
        self.assertEqual(0.1,self.space.solid_calls[-1][3])
        backup.assert_called_once()

    async def test_revolve_normalized_direction_not_endpoint(self):
        with self.tools() as (registry,_,_):
            result=await registry.call("cad_revolve",self.args("cad_revolve",True))
        method,region,point,direction,angle=self.space.solid_calls[-1]
        self.assertEqual("AddRevolvedSolid",method)
        self.assertEqual((2,0,0),_unwrap(point))
        self.assertEqual((0,1,0),_unwrap(direction))
        self.assertEqual(math.tau,angle)
        self.assertEqual(4,self.space.Count)

    async def test_existing_region_needs_no_addregion(self):
        self.profile.ObjectName="AcDbRegion"
        with self.tools() as (registry,_,_), mock.patch.object(self.space,"AddRegion",None):
            result=await registry.call("cad_extrude",self.args("cad_extrude",True))
        self.assertTrue(result["ok"])
        self.assertEqual(4,self.space.Count)
        self.assertIn(self.profile,self.space.entities)

    async def test_boolean_all_three_operations_and_operand_consumption(self):
        for operation,expected,enum in (("union",72,0),("intersection",24,1),("subtract",24,2)):
            with self.subTest(operation=operation), self.tools() as (registry,_,_):
                result=await registry.call("cad_boolean",self.args("cad_boolean",True,operation=operation))
            self.assertEqual(expected,result["entities"][0]["volume"])
            self.assertEqual(24,result["intersection_volume"])
            entity=next(e for e in self.space.entities if e.Handle==result["entities"][0]["handle"])
            self.assertIn(("Boolean",enum),entity.calls)
            self.assertEqual(48,self.first.Volume)
            self.assertEqual(48,self.second.Volume)
            self.assertIn(self.first,self.space.entities)
            entity.Delete()
            self.assertEqual(3,self.space.Count)

    async def test_boolean_deletes_sources_only_after_success_when_requested(self):
        with self.tools() as (registry,_,_):
            result=await registry.call("cad_boolean",self.args("cad_boolean",True,keep_sources=False))
        self.assertEqual(2,result["erased"])
        self.assertNotIn(self.first,self.space.entities)
        self.assertNotIn(self.second,self.space.entities)
        self.assertEqual(2,self.space.Count)

    async def test_disjoint_subtract_and_intersection_refuse_to_save(self):
        self.second.center[0]=100
        for op in ("subtract","intersection"):
            with self.subTest(op=op),self.tools() as (registry,_,_),self.assertRaisesRegex(RuntimeError,"没有正体积"):
                await registry.call("cad_boolean",self.args("cad_boolean",True,operation=op))
            self.assert_unsaved()
            self.assertIn(self.first,self.space.entities)
            self.assertIn(self.second,self.space.entities)

    async def test_boolean_silent_noop_does_not_report_success(self):
        with self.tools() as (registry,_,_),mock.patch.object(_Solid3D,"Boolean",lambda *args:None):
            with self.assertRaisesRegex(RuntimeError,"体积"):
                await registry.call("cad_boolean",self.args("cad_boolean",True,keep_sources=False))
        self.assertIn(self.first,self.space.entities)
        self.assertIn(self.second,self.space.entities)
        self.assert_unsaved()

    async def test_rotate_arbitrary_axis_and_mirror_preserves_source(self):
        with self.tools() as (registry,_,_):
            rotated=await registry.call("cad_rotate_3d",self.args("cad_rotate_3d",True))
            mirrored=await registry.call("cad_mirror_3d",self.args("cad_mirror_3d",True))
        self.assertEqual(1,rotated["changed"])
        self.assertEqual(48,rotated["entities"][0]["volume"])
        self.assertNotEqual(self.first.Handle,mirrored["entities"][0]["handle"])
        self.assertIn(self.first,self.space.entities)
        self.assertEqual(("Copy",),self.first.calls[-1])

    async def test_slice_positive_negative_and_both(self):
        for side,count in (("positive",1),("negative",1),("both",2)):
            with self.subTest(side=side),self.tools() as (registry,_,_):
                result=await registry.call("cad_slice_solid",self.args("cad_slice_solid",True,side=side))
            self.assertEqual(count,result["added"])
            self.assertEqual([24]*count,[e["volume"] for e in result["entities"]])
            self.assertEqual(48,self.first.Volume)
            self.assertIn(self.first,self.space.entities)
            for record in result["entities"]:
                next(e for e in self.space.entities if e.Handle==record["handle"]).Delete()
            self.assertEqual(3,self.space.Count)

    async def test_slice_null_wrong_volume_and_failed_cuts_never_save(self):
        with self.tools() as (registry,_,_),mock.patch.object(_Solid3D,"SliceSolid",return_value=None):
            with self.assertRaisesRegex(RuntimeError,"3DSolid"):
                await registry.call("cad_slice_solid",self.args("cad_slice_solid",True,keep_source=False))
        self.assertIn(self.first,self.space.entities)
        self.assert_unsaved()

    async def test_invalid_parameters_rejected_before_com(self):
        cases=[("cad_create_3d_solid",{"height":0}),("cad_create_3d_solid",{"center":[float("nan"),0,0]}),
               ("cad_create_3d_solid",{"height":True}),("cad_create_3d_solid",{"radius":1}),
               ("cad_create_3d_solid",{"kind":"torus","radius":1,"tube_radius":2,"length":None,"width":None,"height":None}),
               ("cad_extrude",{"height":0}),("cad_extrude",{"taper_angle":math.pi/2}),
               ("cad_revolve",{"axis_direction":[0,0,0]}),("cad_revolve",{"angle":7}),
               ("cad_boolean",{"tool_handle":self.first.Handle}),("cad_boolean",{"keep_sources":1}),
               ("cad_rotate_3d",{"handles":[]}),("cad_rotate_3d",{"angle":math.tau}),
               ("cad_rotate_3d",{"axis_end":[0,0,0]}),("cad_mirror_3d",{"plane_p3":[2,0,0]}),
               ("cad_slice_solid",{"side":"wrong"}),("cad_slice_solid",{"keep_source":1})]
        for name,extra in cases:
            with self.subTest(name=name,extra=extra),self.tools() as (registry,backup,client),self.assertRaises(ValueError):
                await registry.call(name,self.args(name,True,**extra))
            self.assertEqual(0,client.dispatched)
            backup.assert_not_called()
        self.assert_unsaved()

    async def test_overwrite_and_boolean_flags_require_explicit_values(self):
        for name in self.args_by_tool:
            for extra in ({"dry_run":False},{"dry_run":0},{"overwrite":1}):
                with self.subTest(name=name,extra=extra),self.tools() as (registry,backup,client),self.assertRaises(ValueError):
                    await registry.call(name,self.args(name,**extra))
                backup.assert_not_called()
                self.assertEqual(0,client.dispatched)

    async def test_missing_handle_wrong_type_locked_layer_and_user_doc(self):
        for name,extra in (("cad_boolean",{"target_handle":"FFFF"}),
                           ("cad_boolean",{"target_handle":self.profile.Handle}),
                           ("cad_extrude",{"profile_handle":self.first.Handle})):
            with self.tools() as (registry,backup,_),self.assertRaises(ValueError):
                await registry.call(name,self.args(name,True,**extra))
            backup.assert_not_called()
        self.doc.Layers.Item(0).Lock=True
        with self.tools() as (registry,backup,_),self.assertRaises(ValueError):
            await registry.call("cad_rotate_3d",self.args("cad_rotate_3d",True))
        backup.assert_not_called()
        closed_before = self.doc.closed
        app=_CadApplication(docs=[self.doc])
        with self.tools(active=app) as (registry,backup,_),self.assertRaisesRegex(RuntimeError,"正开"):
            await registry.call("cad_create_3d_solid",self.args("cad_create_3d_solid",True))
        backup.assert_not_called()
        self.assertEqual(closed_before,self.doc.closed)
        self.assertFalse(app.quit_called)
        self.assert_unsaved()

    async def test_bad_region_conversion_refuses_to_save_and_preserves_profile(self):
        self.space.region_count=2
        with self.tools() as (registry,_,_),self.assertRaisesRegex(RuntimeError,"唯一有效面域"):
            await registry.call("cad_extrude",self.args("cad_extrude",True))
        self.assertIn(self.profile,self.space.entities)
        self.assert_unsaved()

    async def test_unsupported_and_null_api_result_never_save(self):
        for method,value in (("AddBox",None),("AddExtrudedSolid",None)):
            name="cad_create_3d_solid" if method=="AddBox" else "cad_extrude"
            with self.subTest(method=method),self.tools() as (registry,backup,_),mock.patch.object(self.space,method,return_value=None):
                with self.assertRaisesRegex(RuntimeError,"未创建|未返回"):
                    await registry.call(name,self.args(name,True))
            backup.assert_called_once()
            self.assert_unsaved()
        with self.tools() as (registry,backup,_),mock.patch.object(self.space,"AddBox",None):
            with self.assertRaisesRegex(RuntimeError,"不支持"):
                await registry.call("cad_create_3d_solid",self.args("cad_create_3d_solid",True))
        backup.assert_not_called()

    async def test_bad_volume_fails_without_saving(self):
        self.first.Volume=float("nan")
        with self.tools() as (registry,backup,_),self.assertRaises(ValueError):
            await registry.call("cad_rotate_3d",self.args("cad_rotate_3d",True))
        backup.assert_not_called()
        self.assert_unsaved()

    async def test_cache_free_com_binding_uses_explicit_byref_outputs(self):
        originals = {}
        copy_method = _Solid3D.Copy
        def copy_with_raw(entity):
            clone = copy_method(entity)
            clone._oleobj_ = object()
            originals[clone._oleobj_] = clone
            return clone
        def wrap(client, raw):
            entity = originals[raw]
            def bbox(low, high):
                self.assertEqual(0x400C,low.vt)
                self.assertEqual(0x400C,high.vt)
                low.value,high.value = entity.GetBoundingBox()
            def interference(other, create, flag):
                self.assertEqual(0x400B,flag.vt)
                solid, intersects = entity.CheckInterference(other, create)
                flag.value = intersects
                return solid
            return SimpleNamespace(GetBoundingBox=bbox,CheckInterference=interference)
        with self.tools() as (registry,_,_),mock.patch.object(_Solid3D,"Copy",copy_with_raw),mock.patch.object(cad_module,"_dynamic_cad_object",side_effect=wrap):
            result=await registry.call("cad_boolean",self.args("cad_boolean",True))
        self.assertEqual(24,result["entities"][0]["volume"])
        self.assertEqual(4,self.space.Count)

    async def test_disjoint_union_is_valid_and_cleans_operands(self):
        self.second.center[0]=100
        with self.tools() as (registry,_,_):
            result=await registry.call("cad_boolean",self.args("cad_boolean",True,operation="union"))
        self.assertEqual(96,result["entities"][0]["volume"])
        self.assertEqual(0,result["intersection_volume"])
        self.assertEqual(4,self.space.Count)

    async def test_profiles_reject_open_polyline_and_nonzero_thickness(self):
        for kind,closed,thickness in (("AcDbPolyline",False,0),("AcDbCircle",True,1)):
            self.profile.ObjectName,self.profile.Closed,self.profile.Thickness=kind,closed,thickness
            with self.subTest(kind=kind),self.tools() as (registry,backup,_),self.assertRaises(ValueError):
                await registry.call("cad_extrude",self.args("cad_extrude",True))
            backup.assert_not_called()
        self.assert_unsaved()

    async def test_primitive_null_existing_or_wrong_geometry_cannot_succeed(self):
        for result in (None,self.first):
            with self.subTest(result=result),self.tools() as (registry,_,_),mock.patch.object(self.space,"AddBox",return_value=result):
                with self.assertRaisesRegex(RuntimeError,"未创建"):
                    await registry.call("cad_create_3d_solid",self.args("cad_create_3d_solid",True))
            self.assert_unsaved()
        method=self.space.AddBox
        def wrong(*args):
            solid=method(*args)
            solid.Volume=12
            return solid
        with self.tools() as (registry,_,_),mock.patch.object(self.space,"AddBox",side_effect=wrong):
            with self.assertRaisesRegex(RuntimeError,"体积与请求"):
                await registry.call("cad_create_3d_solid",self.args("cad_create_3d_solid",True))
        self.assert_unsaved()

    async def test_partial_bbox_and_invalid_read_limits_are_not_fabricated(self):
        with self.tools() as (registry,_,_),mock.patch.object(self.first,"GetBoundingBox",return_value=((0,0),(1,1))):
            with self.assertRaisesRegex(RuntimeError,"XYZ"):
                await registry.call("cad_read_solids",self.args("cad_read_solids"))
        for limit in (0,501,True,"10"):
            with self.subTest(limit=limit),self.tools() as (registry,_,client),self.assertRaises(ValueError):
                await registry.call("cad_read_solids",self.args("cad_read_solids",limit=limit))
            self.assertEqual(0,client.dispatched)
        self.assert_unsaved()

    async def test_slice_discard_source_after_valid_two_halves(self):
        with self.tools() as (registry,_,_):
            result=await registry.call("cad_slice_solid",self.args("cad_slice_solid",True,keep_source=False))
        self.assertEqual(1,result["erased"])
        self.assertNotIn(self.first,self.space.entities)
        self.assertEqual(48,sum(r["volume"] for r in result["entities"]))

    async def test_save_failure_keeps_backup_and_reports_no_success(self):
        self.doc.saveas_error=RuntimeError("cannot SaveAs")
        with self.tools() as (registry,backup,_),mock.patch.object(self.doc,"Save",side_effect=RuntimeError("cannot Save")):
            with self.assertRaisesRegex(RuntimeError,"备份"):
                await registry.call("cad_create_3d_solid",self.args("cad_create_3d_solid",True))
        backup.assert_called_once()
        self.assertEqual(1,self.doc.closed)
        self.assertEqual([],self.doc.saved_as)


class CadLiveStartupRegressionTests(unittest.TestCase):
    def test_missing_type_information_during_owned_cold_start_is_retried(self):
        class StartupApp:
            calls = 0
            @property
            def Documents(self):
                self.calls += 1
                if self.calls < 4:
                    raise AttributeError("AutoCAD.Application.Documents")
                return SimpleNamespace(Count=0)
        app = StartupApp()
        with mock.patch.object(cad_module, "_COLD_START_SLEEP", 0):
            usable, nature = cad_module._app_usable(app, cold_start=True)
        self.assertTrue(usable)
        self.assertEqual("ok", nature)
        self.assertEqual(4, app.calls)


if __name__ == "__main__":
    unittest.main()
