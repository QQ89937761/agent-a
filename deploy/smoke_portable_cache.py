"""Compile and run the real portable shell against a tiny inert payload (Windows only)."""
from __future__ import annotations

import ctypes
import os
import shutil
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    if os.name != "nt":
        raise RuntimeError("Portable cache smoke requires Windows")
    cache = Path(os.environ["LOCALAPPDATA"]) / "electron-builder" / "Cache"
    compiler = next(cache.glob("nsis-*/**/Bin/makensis.exe"))
    plugins = next(cache.glob("nsis-resources-*/**/plugins/x86-unicode"))
    templates = ROOT / "frontend/node_modules/app-builder-lib/templates/nsis"
    (ROOT / "work").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="portable cache smoke-", dir=ROOT / "work") as folder:
        root = Path(folder).resolve()
        payload = root / "payload"
        payload.mkdir()
        # This inert NSIS executable only appends its actual runtime path; it never starts XiaoA.
        child = root / "child.nsi"
        child.write_text(f'''Unicode true
RequestExecutionLevel user
SilentInstall silent
OutFile "{payload / 'XiaoA.exe'}"
Section
  FileOpen $0 "$EXEDIR\\runs.txt" a
  FileSeek $0 0 END
  FileWrite $0 "$EXEDIR$\\r$\\n"
  FileClose $0
SectionEnd
''', encoding="utf-8")

        def compile_script(source: Path) -> None:
            result = subprocess.run([str(compiler), "-V2", "-INPUTCHARSET", "UTF8", str(source)], capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)

        compile_script(child)
        for name in ("resources/app.asar", "resources/backend/xiaoa-backend.exe",
                     "resources/backend/_internal/python312.dll", "icudtl.dat"):
            path = payload / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"inert smoke payload")
        transformed = subprocess.check_output(["node", "-e", '''
const fs=require('fs'); const {persistentPortableScript}=require('./build_electron');
process.stdout.write(persistentPortableScript(fs.readFileSync('node_modules/app-builder-lib/templates/nsis/portable.nsi','utf8')));
'''], cwd=ROOT / "frontend").decode("utf-8")
        # Keep fallback writes and old-cache cleanup inside this isolated fixture.
        macro = root / "portable-cache.nsh"
        macro.write_text((ROOT / "frontend/portable-cache.nsh").read_text(encoding="utf-8")
                         .replace("$LOCALAPPDATA", str(root / "fallback-home")), encoding="utf-8")
        transformed = transformed.replace(str(ROOT / "frontend/portable-cache.nsh"), str(macro))
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        caches = []
        for build in ("smoke-build-one", "smoke-build-two"):
            exe = root / (build + ".exe")
            source = root / (build + ".nsi")
            source.write_text(f'''Unicode true
!include LogicLib.nsh
!include WinMessages.nsh
!addincludedir "{templates}"
!addincludedir "{templates / 'include'}"
!addplugindir /x86-unicode "{plugins}"
!include StdUtils.nsh
!define PRODUCT_NAME "XiaoA Cache Smoke"
!define PRODUCT_FILENAME "XiaoA"
!define APP_FILENAME "XiaoA"
!define VERSION "0.0.0"
!define UNPACK_DIR_NAME "{build}"
!define REQUEST_EXECUTION_LEVEL "user"
!define APP_DIR_64 "{payload}"
OutFile "{exe}"
LangString win7Required 1033 "Windows 7 required"
{transformed}
''', encoding="utf-8")
            compile_script(source)
            destination = root / "XiaoA-data/runtimes" / ("app-0.0.0-" + build)

            def run_shell() -> None:
                result = subprocess.run([str(exe), "--cache-smoke"], cwd=root, timeout=30,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode:
                    raise RuntimeError(f"Portable shell failed: {result.returncode}")
                assert (destination / ".xiaoa-ready").is_file()
                assert (destination / "runs.txt").is_file(), "quoted executable path must launch, including spaces"

            # A live launcher's handle protects its cache, even after extraction
            # releases mutex ownership. Simulate that exact lifetime here.
            active = kernel.CreateMutexW(None, False, "Local\\XiaoA-cache-app-0.0.0-smoke-build-one") if caches else None
            try:
                run_shell()
                if caches:
                    assert active and caches[0].is_dir(), "active old build must survive cleanup"
            finally:
                if active:
                    kernel.CloseHandle(active)
            saved = destination / "resources/app.asar"
            before = saved.stat().st_mtime_ns
            run_shell()
            assert saved.stat().st_mtime_ns == before, "second run must not extract again"
            assert len((destination / "runs.txt").read_text().splitlines()) == 2
            # A marker alone cannot hide a missing backend/runtime.
            (destination / "resources/backend/xiaoa-backend.exe").unlink()
            run_shell()
            assert (destination / "resources/backend/xiaoa-backend.exe").is_file()
            # An interrupted extraction (no marker) must rebuild the cache.
            (destination / ".xiaoa-ready").unlink()
            run_shell()
            caches.append(destination)
        assert caches[0] != caches[1] and not caches[0].exists() and caches[1].is_dir()
        # Concurrent double-clicks must serialize extraction, not destroy each other's files.
        (caches[-1] / ".xiaoa-ready").unlink()
        processes = [subprocess.Popen([str(exe)], cwd=root, creationflags=subprocess.CREATE_NO_WINDOW)
                     for _ in range(2)]
        assert all(process.wait(timeout=30) == 0 for process in processes)
        assert (caches[-1] / ".xiaoa-ready").is_file()
        # A file in place of XiaoA-data makes the normal runtime path unwritable.
        fallback_launch = root / "fallback launch"
        fallback_launch.mkdir()
        (fallback_launch / "XiaoA-data").write_bytes(b"blocked directory")
        fallback_exe = fallback_launch / exe.name
        shutil.copy2(exe, fallback_exe)
        fallback = root / "fallback-home/xiaoa-portable/app-0.0.0-smoke-build-two"
        for _ in range(2):
            result = subprocess.run([str(fallback_exe)], cwd=root, timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            assert result.returncode == 0 and (fallback / ".xiaoa-ready").is_file()
        assert len((fallback / "runs.txt").read_text().splitlines()) == 2
        # Exercise the actual electron-builder hook/API as well as the template.
        # Prepackaged inert payload avoids rebuilding/starting the real application.
        builder_output = root / "builder output"
        subprocess.run(["node", "-e", """
const {installPortableCacheHook}=require('./build_electron');
installPortableCacheHook();
const {build,Platform,Arch}=require('electron-builder');
build({projectDir:process.cwd(),prepackaged:process.argv[1],publish:'never',
 targets:Platform.WINDOWS.createTarget('portable',Arch.x64),
 config:{productName:'XiaoA',directories:{output:process.argv[2]},
 win:{signAndEditExecutable:false},portable:{artifactName:'builder-smoke.exe',useZip:true}}})
 .catch(error=>{console.error(error);process.exitCode=1;});
""", str(payload), str(builder_output)], cwd=ROOT / "frontend", check=True, timeout=120)
        builder_exe = builder_output / "builder-smoke.exe"
        subprocess.run([str(builder_exe)], cwd=root, check=True, timeout=30,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        built_cache, = (builder_output / "XiaoA-data/runtimes").glob("app-*")
        saved = built_cache / "resources/app.asar"
        before = saved.stat().st_mtime_ns
        subprocess.run([str(builder_exe)], cwd=root, check=True, timeout=30,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        assert saved.stat().st_mtime_ns == before
        assert len((built_cache / "runs.txt").read_text().splitlines()) == 2
        print("PORTABLE_CACHE_SMOKE_PASSED: reuse, missing-file repair, interrupted extraction, build isolation, concurrent launch, active-cache protection, inactive cleanup, fallback reuse, electron-builder integration")


if __name__ == "__main__":
    main()
