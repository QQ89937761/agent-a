"""AutoCAD COM 真机诊断:逐步探测,每步单独报错,不中断。

为什么要单独一个脚本:cad 工具跑在小A 后端里,任何一步失败都只看到一句
"工具执行失败"。这个脚本把每一步拆开,直接在命令行告诉你哪一步、什么错误,
贴回来就能定位到具体代码行。

用法(在仓库根目录下):
    xiaoa-venv\\Scripts\\python.exe tools\\cad_doctor.py                     # 只探测环境
    xiaoa-venv\\Scripts\\python.exe tools\\cad_doctor.py <drawing-path>   # 连图纸一起探

只读。不改、不存、不关你已经开着的 AutoCAD。
"""
from __future__ import annotations

import os
import shutil
import sys
import traceback
from pathlib import Path

PROGID = "AutoCAD.Application"
_ok = 0
_fail = 0


def step(label: str, fn, *, optional: bool = False):
    """跑一步,打印结果,失败不中断。返回值供后续步骤用;失败返回 None。"""
    global _ok, _fail
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001
        _fail += 1
        mark = "跳过" if optional else "失败"
        print(f"[{mark}] {label}")
        print(f"       {type(exc).__name__}: {exc}")
        tb = traceback.format_exc().strip().splitlines()
        if len(tb) > 3:
            print(f"       {tb[-3].strip()}")
        return None
    _ok += 1
    shown = repr(value)
    if len(shown) > 160:
        shown = shown[:160] + "…"
    print(f"[ OK ] {label} -> {shown}")
    return value


def _code_bug_note(exc: Exception) -> str:
    """环境探测脚本自己的代码缺陷(NameError/AttributeError/ImportError)要说明,
    别让它伪装成"被探测环境(AutoCAD/pywin32)的故障"。"""
    if isinstance(exc, (NameError, AttributeError, ImportError)):
        return "       ⚠ 这是脚本自身的 bug(缺 import / 拼写错),不是 AutoCAD 的问题。"
    return ""


def main() -> int:
    print("=" * 66)
    print("AutoCAD COM 诊断")
    print("=" * 66)
    print(f"Python: {sys.version.split()[0]}  ({sys.executable})")
    print(f"平台:   {sys.platform}")
    print()

    if sys.platform != "win32":
        print("这个脚本只能在 Windows 上跑。")
        return 2

    print("--- 1. 环境 ---")
    if step("import pythoncom", lambda: __import__("pythoncom").__name__) is None:
        print("\npywin32 没装好。装它:  xiaoa-venv\\Scripts\\pip.exe install pywin32")
        return 2
    client = step("import win32com.client", lambda: __import__("win32com.client", fromlist=["x"]))
    if client is None:
        return 2

    import pythoncom
    pythoncom.CoInitialize()
    try:
        print("\n--- 2. 连接 AutoCAD ---")
        app = step("GetActiveObject(已开的 AutoCAD)",
                   lambda: client.GetActiveObject(PROGID), optional=True)
        started_by_us = False
        if app is None:
            print("       (没有已开的 AutoCAD,下面尝试新起一个)")
            app = step("Dispatch(新起 AutoCAD)", lambda: client.Dispatch(PROGID))
            started_by_us = app is not None
        if app is None:
            print("\n连不上 AutoCAD。两个最常见原因:")
            print("  1) 装的是 AutoCAD LT —— LT 不带 ActiveX,无法被程序驱动,这条路走不通。")
            print("  2) AutoCAD 从没以管理员之外的身份运行过 —— 先手动打开一次 AutoCAD 再试。")
            return 2

        step("Application.Name", lambda: str(app.Name), optional=True)
        step("Application.Version", lambda: str(app.Version), optional=True)
        step("Documents.Count", lambda: int(app.Documents.Count))

        # Visible 只读不写。写它在 AutoCAD 上会抛错,这里正是要确认这一点。
        step("读 Application.Visible", lambda: bool(app.Visible), optional=True)

        print("\n--- 2.5 对象模型与类型库 ---")
        try:
            import win32api
            step("pywin32 位置", lambda: win32api.__file__)
        except Exception as exc:  # noqa: BLE001
            print(f"       win32api 导入失败:{exc}")

        try:
            gen_path = str(client.gencache.GetGeneratePath())
            print(f"[ OK ] gencache.GetGeneratePath() -> {gen_path}")
            print(f"       目录存在: {os.path.isdir(gen_path)}")
            if os.path.isdir(gen_path):
                subs = sorted(os.listdir(gen_path))
                print(f"       子目录数: {len(subs)}")
                for sub in subs:
                    print(f"         - {sub}")
        except Exception as exc:  # noqa: BLE001
            print(f"[失败] 读 gencache 路径: {exc}")
            note = _code_bug_note(exc)
            if note:
                print(note)

        def _usable(obj):
            try:
                obj.Documents.Count
                return True
            except Exception:
                return False

        print("       四条策略(逐条试,成功即停):")
        # A late
        if _usable(app):
            print("       [ OK ] A late: Documents 可取")
            print(f"              type(app) = {type(app)}")
            print(f"              type(app.Documents) = {type(app.Documents)}")
            print(f"              dir(app) 前40: {dir(app)[:40]}")
        else:
            print("       [失败] A late: 取不到 Documents")
            # B application
            try:
                app_b = app.Application
            except Exception as exc:  # noqa: BLE001
                print(f"       [失败] B application: .Application 取不到 {exc}")
            else:
                if _usable(app_b):
                    print("       [ OK ] B application: Documents 可取")
                    print(f"              type(app_b) = {type(app_b)}")
                    print(f"              type(app_b.Documents) = {type(app_b.Documents)}")
                    print(f"              dir(app_b) 前40: {dir(app_b)[:40]}")
                    app = app_b
                else:
                    print("       [失败] B application: Application 对象上仍取不到 Documents")
                    # C rebuild(有副作用,只在 A/B 都失败后才清)
                    print("       [提示] C rebuild 要清 gen_py 缓存,有副作用,现在执行:")
                    try:
                        if os.path.isdir(gen_path):
                            shutil.rmtree(gen_path, ignore_errors=True)
                            print(f"              (已清空 {gen_path})")
                        client.gencache.Rebuild()
                        app_c = client.Dispatch(PROGID)
                    except Exception as exc:  # noqa: BLE001
                        print(f"       [失败] C rebuild: {exc}")
                        note = _code_bug_note(exc)
                        if note:
                            print(note)
                    else:
                        if _usable(app_c):
                            print("       [ OK ] C rebuild: 清缓存后 Documents 可取")
                            app = app_c
                        else:
                            print("       [失败] C rebuild: 清缓存后仍取不到 Documents")
                            # D early
                            try:
                                app_d = client.gencache.EnsureDispatch(PROGID)
                            except Exception as exc:  # noqa: BLE001
                                print(f"       [失败] D early: EnsureDispatch {exc}")
                            else:
                                if _usable(app_d):
                                    print("       [ OK ] D early: 早绑定后 Documents 可取")
                                    app = app_d
                                else:
                                    print("       [失败] D early: 早绑定后仍取不到 Documents")
                                    print("       >>> 四条全失败。最可能是 gen_py 类型库缓存损坏;")
                                    print(f"          手动关掉 AutoCAD 和小A,删掉 {gen_path} 再重开。")

        print("\n--- 3. 已打开的图纸 ---")
        try:
            for i in range(int(app.Documents.Count)):
                doc_i = app.Documents.Item(i)
                try:
                    name = str(doc_i.FullName or "(未存盘)")
                except Exception as exc:  # noqa: BLE001
                    name = f"(读 FullName 失败: {exc})"
                print(f"       [{i}] {name}")
        except Exception as exc:  # noqa: BLE001
            print(f"       枚举失败: {exc}")

        target = sys.argv[1] if len(sys.argv) > 1 else ""
        if not target:
            print("\n没传图纸路径,只做了环境探测。")
            print("要探图纸,再跑一次并带上路径:")
            print("       xiaoa-venv\\Scripts\\python.exe tools\\cad_doctor.py <drawing-path>")
            print(f"\n小结:成功 {_ok} 步,失败/跳过 {_fail} 步。")
            return 0 if _fail == 0 else 1

        path = Path(target).resolve()
        print(f"\n--- 4. 图纸:{path} ---")
        if not path.is_file():
            print(f"[失败] 文件不存在:{path}")
            return 2
        step("DWG 文件头(前 6 字节)",
             lambda: open(path, "rb").read(6).decode("ascii", "ignore"))
        print("       AC1015=2000 AC1018=2004 AC1021=2007 AC1024=2010 AC1027=2013 AC1032=2018+")

        doc = None
        own_doc = False
        for i in range(int(app.Documents.Count)):
            d = app.Documents.Item(i)
            try:
                if d.FullName and Path(str(d.FullName)).resolve() == path:
                    doc = d
                    print("[ OK ] 这张图已经开在 AutoCAD 里,直接复用(不会动它)")
                    break
            except Exception:
                continue
        if doc is None:
            doc = step("Documents.Open", lambda: app.Documents.Open(str(path)))
            own_doc = doc is not None
        if doc is None:
            print("\n打不开图纸。如果 AutoCAD 弹了对话框(缺字体、缺外部参照),先手动点掉。")
            return 2

        try:
            print("\n--- 5. 系统变量 ---")
            step('GetVariable("INSUNITS") 单位',
                 lambda: int(doc.GetVariable("INSUNITS")))
            print("       0=无单位 1=英寸 2=英尺 4=毫米 5=厘米 6=米")
            step('GetVariable("EXTMIN") 图形范围左下',
                 lambda: [float(v) for v in doc.GetVariable("EXTMIN")])
            step('GetVariable("EXTMAX") 图形范围右上',
                 lambda: [float(v) for v in doc.GetVariable("EXTMAX")])
            step('GetVariable("ACADVER") CAD 版本',
                 lambda: str(doc.GetVariable("ACADVER")), optional=True)
            step('GetVariable("DWGVER") — 预期失败,确认它不是真变量',
                 lambda: str(doc.GetVariable("DWGVER")), optional=True)

            print("\n--- 6. 图层属性(最容易版本不兼容的地方) ---")
            layer_count = step("Layers.Count", lambda: int(doc.Layers.Count))
            if layer_count:
                layer = doc.Layers.Item(0)
                step("  Layer.Name", lambda: str(layer.Name))
                step("  Layer.Color", lambda: int(layer.Color), optional=True)
                step("  Layer.Linetype", lambda: str(layer.Linetype), optional=True)
                step("  Layer.Lineweight", lambda: int(layer.Lineweight), optional=True)
                step("  Layer.LayerOn", lambda: bool(layer.LayerOn), optional=True)
                step("  Layer.Freeze", lambda: bool(layer.Freeze), optional=True)
                step("  Layer.Lock", lambda: bool(layer.Lock), optional=True)
                names = []
                try:
                    for i in range(min(int(doc.Layers.Count), 40)):
                        names.append(str(doc.Layers.Item(i).Name))
                    print(f"       图层名:{', '.join(names)}")
                except Exception as exc:  # noqa: BLE001
                    print(f"       图层枚举失败:{exc}")

            print("\n--- 7. 块 / 样式 ---")
            step("Blocks.Count", lambda: int(doc.Blocks.Count), optional=True)
            step("Blocks.Item(0).Name", lambda: str(doc.Blocks.Item(0).Name), optional=True)
            step("DimStyles 名称", lambda: [str(doc.DimStyles.Item(i).Name)
                                            for i in range(int(doc.DimStyles.Count))],
                 optional=True)
            step("TextStyles 名称", lambda: [str(doc.TextStyles.Item(i).Name)
                                             for i in range(int(doc.TextStyles.Count))],
                 optional=True)

            print("\n--- 8. 模型空间实体(只读前 2000 个) ---")
            total = step("ModelSpace.Count", lambda: int(doc.ModelSpace.Count))
            if total:
                stats: dict[str, int] = {}
                texts = 0
                sample = None
                scanned = min(int(total), 2000)
                try:
                    for i in range(scanned):
                        ent = doc.ModelSpace.Item(i)
                        name = str(getattr(ent, "ObjectName", "?") or "?")
                        stats[name] = stats.get(name, 0) + 1
                        if "Text" in name:
                            texts += 1
                            if sample is None:
                                sample = str(getattr(ent, "TextString", ""))
                    print(f"[ OK ] 扫了 {scanned}/{total} 个实体")
                    for name, count in sorted(stats.items(), key=lambda kv: -kv[1])[:14]:
                        print(f"       {count:>7}  {name}")
                    if sample is not None:
                        print(f"       文字样例:{sample!r}(共 {texts} 个文字实体)")
                except Exception as exc:  # noqa: BLE001
                    print(f"[失败] 实体扫描在第 {i} 个中断:{type(exc).__name__}: {exc}")
        finally:
            if own_doc:
                try:
                    doc.Close(False)   # 我们开的,关掉;不保存
                    print("\n(已关闭本脚本打开的图纸,未做任何修改)")
                except Exception as exc:  # noqa: BLE001
                    print(f"\n(关闭图纸失败,请手动关:{exc})")
            if started_by_us:
                try:
                    app.Quit()
                    print("(已退出本脚本启动的 AutoCAD)")
                except Exception:
                    pass
    finally:
        pythoncom.CoUninitialize()

    print(f"\n{'=' * 66}")
    print(f"小结:成功 {_ok} 步,失败/跳过 {_fail} 步。")
    print("把上面整段贴回来,我按行定位。")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
