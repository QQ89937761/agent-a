# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules


root = Path(SPECPATH).parent
sys.path.insert(0, str(root))
from deploy.release_files import release_data_files

datas = release_data_files(root)

binaries = []
# lark-oapi ships an incomplete gray/test namespace (missing generated model files).
# It is not used by XiaoA; excluding it keeps PyInstaller analysis focused on the
# production SDK modules instead of reporting a false missing-import warning.
hiddenimports = collect_submodules(
    "lark_oapi",
    filter=lambda name: not name.startswith("lark_oapi.api.gray_test_open_sg"),
    on_error="ignore",
)
# These channel modules are imported at runtime from the FastAPI lifespan and
# therefore need to be declared explicitly for the frozen backend.
hiddenimports += [
    "backend.modules.chat.feishu_module",
    "backend.modules.chat.weixin_clawbot",
]

for package in ("fastembed", "paramiko", "cryptography", "certifi", "qrcode", "uiautomation", "pypdf", "pymupdf"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

a = Analysis(
    [str(root / "xiaoa_backend.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "_tkinter", "tcl", "tk", "PIL.ImageTk"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="xiaoa-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="xiaoa-backend",
)
