"""Unified GitHub extension integration tests without network access."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from backend.core.tools import ToolRegistry
from backend.modules.computer.extension_module import ExtensionManager, register_tools
from backend.modules.computer.skill_module import SkillManager
from backend.modules.computer.user_tool_module import UserToolManager


def _skill_archive(body: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr(
            "demo-main/SKILL.md",
            "---\nname: github-skill\ndescription: GitHub 测试技能\n---\n\n" + body,
        )
    return output.getvalue()


def _plugin_archive() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr("demo-main/plugin.json", json.dumps({
            "name": "github_plugin", "description": "GitHub 插件测试",
            "runtime": "python", "entry": "main.py",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            "dependencies": [], "test_args": {"text": "test"}, "side_effect": False,
        }))
        bundle.writestr(
            "demo-main/main.py",
            "import json,sys\na=json.load(sys.stdin)\nprint(json.dumps({'echo':a['text']}))\n",
        )
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

    def __call__(self, request, timeout=0):
        if "/commits/" in request.full_url:
            return _Response(json.dumps({"sha": "b" * 40}).encode())
        return _Response(self.archive)


class ExtensionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_install_through_github_manager(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-extensions-") as tmp:
            root = Path(tmp); registry = ToolRegistry()
            manager = ExtensionManager(
                UserToolManager(root / "tools", registry), SkillManager(root / "skills"),
            )
            manager.market._opener = _Opener(_plugin_archive())
            inspected = manager.inspect("https://github.com/acme/demo", "plugin")
            self.assertEqual("python", inspected["manifest"]["runtime"])
            installed = manager.install("https://github.com/acme/demo", "plugin")
            self.assertEqual(1, installed["version"])
            self.assertEqual("plugin", installed["kind"])
            result = json.loads(await registry.call("github_plugin", {"text": "hello"}))
            self.assertEqual({"echo": "hello"}, result)
            self.assertEqual("github_plugin", manager.list_extensions()["plugins"][0]["name"])

    async def test_skill_install_update_list_and_rollback(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-extensions-") as tmp:
            root = Path(tmp)
            registry = ToolRegistry()
            tools = UserToolManager(root / "tools", registry)
            skills = SkillManager(root / "skills")
            manager = ExtensionManager(tools, skills)
            opener = _Opener(_skill_archive("VERSION ONE"))
            manager.market._opener = opener

            installed = manager.install("https://github.com/acme/demo", "skill")
            self.assertEqual(1, installed["version"])
            self.assertIn("VERSION ONE", skills.active_prompt())
            listed = manager.list_extensions()["skills"][0]
            self.assertEqual("https://github.com/acme/demo", listed["source"])
            self.assertEqual(1, listed["version"])

            opener.archive = _skill_archive("VERSION TWO")
            updated = manager.update("github-skill", "skill")
            self.assertEqual(2, updated["version"])
            self.assertIn("VERSION TWO", skills.active_prompt())

            manager.rollback("skill", "github-skill")
            self.assertIn("VERSION ONE", skills.active_prompt())
            self.assertNotIn("VERSION TWO", skills.active_prompt())
            self.assertEqual(1, manager.list_extensions()["skills"][0]["version"])

    async def test_extension_tools_are_registered(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-extensions-") as tmp:
            registry = ToolRegistry()
            manager = ExtensionManager(
                UserToolManager(Path(tmp) / "tools", registry),
                SkillManager(Path(tmp) / "skills"),
            )
            register_tools(registry, manager)
            self.assertTrue(registry.get("extension_inspect").side_effect)
            self.assertTrue({
                "extension_search", "extension_inspect", "extension_install",
                "extension_update", "extension_list", "extension_enable",
                "extension_rollback",
            }.issubset(registry.names()))
            result = json.loads(await registry.call("extension_list", {}))
            self.assertEqual({"tools": [], "skills": [], "plugins": []}, result)


if __name__ == "__main__":
    unittest.main()
