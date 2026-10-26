from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from backend.core.tools import ToolRegistry
from backend.modules.computer.tool_market_module import ToolMarketManager
from backend.modules.computer.user_tool_module import UserToolManager


def _archive(script: str = "Write-Output 'ok'", *, subdir: str = "") -> bytes:
    manifest = {
        "name": "github_tool",
        "description": "GitHub 测试工具",
        "parameters": {"type": "object", "properties": {}},
        "test_args": {},
        "side_effect": False,
        "timeout": 10,
    }
    prefix = "demo-main/" + (subdir.strip("/") + "/" if subdir else "")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr(prefix + "tool.json", json.dumps(manifest, ensure_ascii=False))
        bundle.writestr(prefix + "tool.ps1", script)
    return output.getvalue()


class _Response(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _Opener:
    def __init__(self, archive: bytes):
        self.archive = archive
        self.urls: list[str] = []

    def __call__(self, request, timeout=0):
        url = request.full_url
        self.urls.append(url)
        if "api.github.com" in url:
            return _Response(json.dumps({"sha": "a" * 40}).encode())
        return _Response(self.archive)


class ToolMarketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xiaoa-market-test-")
        self.root = Path(self.temp.name)
        self.manager = UserToolManager(self.root, ToolRegistry())
        self.script_check = patch.object(self.manager, "_validate_script")
        self.script_run = patch.object(self.manager, "_run_script", return_value="self-test ok")
        self.script_check.start()
        self.script_run.start()

    def tearDown(self):
        self.script_check.stop()
        self.script_run.stop()
        self.temp.cleanup()

    def test_inspect_tree_subdirectory(self):
        opener = _Opener(_archive(subdir="tools/github_tool"))
        market = ToolMarketManager(self.manager, opener=opener)
        result = market.inspect("https://github.com/acme/demo/tree/main/tools/github_tool")
        self.assertTrue(result["ok"])
        self.assertEqual("github_tool", result["name"])
        self.assertEqual("main", result["ref"])
        self.assertEqual("tools/github_tool", result["subdir"])
        self.assertEqual("a" * 40, result["commit"])
        self.assertIn("/zip/main", opener.urls[0])

    def test_rejects_zip_slip_and_file_limit(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as bundle:
            bundle.writestr("demo-main/../outside.txt", "bad")
        with self.assertRaisesRegex(ValueError, "不安全路径"):
            ToolMarketManager(self.manager, opener=_Opener(output.getvalue())).inspect(
                "https://github.com/acme/demo"
            )

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as bundle:
            bundle.writestr("demo-main/tool.json", "{}")
            bundle.writestr("demo-main/tool.ps1", "")
            bundle.writestr("demo-main/README.md", "")
        with self.assertRaisesRegex(ValueError, "文件数量"):
            ToolMarketManager(self.manager, opener=_Opener(output.getvalue()), max_files=2).inspect(
                "https://github.com/acme/demo"
            )

    def test_rejects_oversized_download(self):
        with self.assertRaisesRegex(ValueError, "下载内容超过限制"):
            ToolMarketManager(
                self.manager, opener=_Opener(b"x" * 30), max_download_bytes=20,
            ).inspect("https://github.com/acme/demo")

    def test_install_update_records_source_and_history(self):
        opener = _Opener(_archive("Write-Output 'v1'"))
        market = ToolMarketManager(self.manager, opener=opener)
        installed = market.install("https://github.com/acme/demo")
        self.assertEqual(1, installed["version"])
        manifest_path = self.root / "github_tool" / "tool.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual("https://github.com/acme/demo", manifest["source_url"])
        self.assertEqual("HEAD", manifest["source_ref"])
        self.assertEqual("a" * 40, manifest["source_commit"])

        opener.archive = _archive("Write-Output 'v2'")
        updated = market.update("github_tool")
        self.assertEqual(2, updated["version"])
        self.assertIn("v2", (self.root / "github_tool" / "tool.ps1").read_text(encoding="utf-8-sig"))
        self.assertTrue(any((self.root / "_history" / "github_tool").iterdir()))

    def test_failed_update_keeps_installed_tool(self):
        opener = _Opener(_archive("Write-Output 'stable'"))
        market = ToolMarketManager(self.manager, opener=opener)
        market.install("https://github.com/acme/demo")
        script_path = self.root / "github_tool" / "tool.ps1"
        before = script_path.read_bytes()

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as bundle:
            bundle.writestr("demo-main/tool.json", "not-json")
            bundle.writestr("demo-main/tool.ps1", "Write-Output 'broken'")
        opener.archive = output.getvalue()
        with self.assertRaisesRegex(ValueError, "tool.json"):
            market.update("github_tool")
        self.assertEqual(before, script_path.read_bytes())
        self.assertEqual(1, json.loads((self.root / "github_tool" / "tool.json").read_text())["version"])
        with self.assertRaisesRegex(ValueError, "名称格式"):
            market.update("../outside", "https://github.com/acme/demo")


if __name__ == "__main__":
    unittest.main()
