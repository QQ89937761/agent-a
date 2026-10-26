"""统一测试入口:发现并运行 backend 下的单元测试。

用法(项目根目录):
    python -m backend.run_tests [--strict]

特点(为了能在开发机 Windows 和 CI Linux 两端都跑):
  - 只采集 test_*.py(live_check.py 不是 test_ 前缀,连真网关花钱的检查不会被误跑);
  - 某个测试模块因缺依赖(如未装 fastapi)或非 Windows 而无法导入时,
    记为"跳过"并继续,而不是让整轮直接失败——这样缺依赖的环境也能跑其余用例;
  - 退出码:有失败/错误返回 1,全过返回 0,方便 CI 判定。
"""
from __future__ import annotations

import argparse
import importlib
import sys
import unittest
from pathlib import Path


def _configure_test_stdio() -> None:
    """Keep diagnostic output Unicode-safe on Windows CI consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


_configure_test_stdio()

_OPTIONAL_TEST_DEPENDENCIES = {
    "PIL", "docx", "fastapi", "httpx", "openpyxl", "pydantic", "tomli",
    "uvicorn", "websockets",
}


def build_suite() -> tuple[unittest.TestSuite, list[str]]:
    backend_dir = Path(__file__).resolve().parent
    project_root = backend_dir.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    skipped: list[str] = []
    for path in sorted(backend_dir.glob("test_*.py")):
        module = f"backend.{path.stem}"
        try:
            imported = importlib.import_module(module)
            suite.addTests(loader.loadTestsFromModule(imported))
        except ModuleNotFoundError as exc:  # 缺依赖/平台不符导致导入失败 → 跳过
            missing_root = str(exc.name or "").split(".", 1)[0]
            if missing_root not in _OPTIONAL_TEST_DEPENDENCIES:
                raise
            skipped.append(f"{module}: {type(exc).__name__}: {exc}")
    return suite, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the backend unit test suite.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail when any test module cannot be imported",
    )
    args = parser.parse_args(argv)

    suite, skipped = build_suite()
    if skipped:
        print("以下测试模块被跳过(导入失败,通常是缺依赖或需 Windows):")
        for item in skipped:
            print(f"  - {item}")
        print()
        if args.strict:
            print("严格模式不允许跳过测试模块。")
            return 1
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
