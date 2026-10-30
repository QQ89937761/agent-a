"""任务成品和过程文件的统一默认目录回归。"""
import base64
import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
from backend.core import isolation_workspace
from backend.core.agent import Agent
from backend.core.config import AgentConfig
from backend.core.tools import Tool, ToolRegistry
from backend.modules.cad import cad_module
from backend.modules.computer import browser_module, code_module, computer_module, system_control, user_tool_module
from backend.modules.computer.operation_audit import redact_parameters
from backend.modules.computer.plugin_runtime_module import PluginRuntimeManager
from backend.modules.office import docgen_module, fs_module


class OutputWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xiaoa-workspace-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        patcher = patch.object(isolation_workspace, "root", return_value=self.root / "isolation")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(fs_module, "_WORKSPACE_ROOT", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.workspace = isolation_workspace.workspace()

    async def test_documents_and_scripts_default_to_workspace(self):
        self.assertEqual(self.workspace / "helper.py", fs_module._resolve("helper.py"))
        self.assertEqual(self.workspace / "helper.py", code_module._path("helper.py"))
        self.assertEqual(self.workspace / "new.docx", docgen_module._resolve_path(None, "new.docx"))
        registry = ToolRegistry()
        docgen_module.register_tools(registry)
        with patch.object(docgen_module, "_open_file", create=True):
            await registry.call("create_word_doc", {"path": "result.docx", "title": "测试"})
            await registry.call("create_excel_file", {"path": "result.xlsx", "headers": ["测试"]})
        self.assertTrue((self.workspace / "result.docx").is_file())
        self.assertTrue((self.workspace / "result.xlsx").is_file())
        explicit = self.root / "chosen.docx"
        self.assertEqual(explicit, docgen_module._resolve_path(str(explicit), "new.docx"))
        self.assertEqual(explicit, code_module._path(str(explicit)))
        fs_module.register_tools(registry)
        info = await registry.call("workspace_info", {})
        self.assertEqual(str(self.workspace), info["path"])

    async def test_omitted_document_paths_are_saved_without_overwriting_previous_tasks(self):
        registry = ToolRegistry()
        docgen_module.register_tools(registry)
        with patch.object(docgen_module, "_open_file", create=True):
            for _ in range(2):
                await registry.call("create_word_doc", {"title": "测试"})
                await registry.call("create_excel_file", {"headers": ["测试"]})
        self.assertEqual(2, len(list(self.workspace.glob("*.docx"))))
        self.assertEqual(2, len(list(self.workspace.glob("*.xlsx"))))
        self.assertEqual(self.workspace, docgen_module._resolve_path("  ", "blank.docx").parent)
        fs_module.register_tools(registry)
        await registry.call("fs_write_text", {"path": "scripts/helper.py", "content": "pass"})
        self.assertEqual("pass", (self.workspace / "scripts/helper.py").read_text())
        explicit = self.root / "chosen" / "result.docx"
        with patch.object(docgen_module, "_open_file", create=True):
            await registry.call("create_word_doc", {"path": str(explicit)})
        self.assertTrue(explicit.is_file())

    async def test_browser_screenshots_are_saved_in_workspace_and_keep_original_bytes(self):
        controller = browser_module.BrowserController(profile_dir=self.root / "profile", port=1986)
        controller._target = AsyncMock(return_value={"id": "tab"})
        image = io.BytesIO()
        Image.new("RGB", (1920, 1080), "white").save(image, format="PNG")
        payload = image.getvalue()
        controller._cdp = AsyncMock(return_value={"data": base64.b64encode(payload).decode()})
        first = await controller.screenshot()
        second = await controller.screenshot()
        self.assertNotEqual(first["path"], second["path"])
        for result in (first, second):
            saved = Path(result["path"])
            self.assertEqual(self.workspace / "screenshots", saved.parent)
            self.assertEqual(payload, saved.read_bytes())
            with Image.open(saved) as opened:
                self.assertEqual((1920, 1080), opened.size)
        controller._cdp.return_value = {"data": "invalid image"}
        with self.assertRaises(ValueError):
            await controller.screenshot()
        self.assertEqual(2, len(list((self.workspace / "screenshots").glob("*.png"))))

    async def test_command_default_cwd_and_explicit_cwd(self):
        registry = ToolRegistry()
        with patch.object(system_control, "_IS_WINDOWS", True):
            system_control.register_tools(registry)
        command = subprocess.list2cmdline([sys.executable, "-c", "from pathlib import Path; Path('intermediate.txt').write_text('ok')"])
        result = await registry.call("run_command", {"command": command})
        self.assertNotIn("失败", result)
        self.assertEqual("ok", (self.workspace / "intermediate.txt").read_text())
        with patch.object(system_control, "_run_console_command", return_value=SimpleNamespace(stdout=b"ok", stderr=b"", returncode=0)) as run:
            await registry.call("run_command", {"command": "echo ok", "cwd": str(self.root)})
        self.assertEqual(str(self.root), run.call_args.args[1])

    async def test_code_commands_also_allow_omitted_cwd(self):
        registry = ToolRegistry()
        processes = code_module.register_tools(registry)
        command = subprocess.list2cmdline([sys.executable, "-c", "from pathlib import Path; Path('code-output.txt').write_text('ok')"])
        with patch.object(code_module, "_reject_protected"):
            result = await registry.call("code_run", {"command": command})
            self.assertIn("退出码 0", result)
            self.assertTrue((self.workspace / "code-output.txt").is_file())
            with patch.object(processes, "start", return_value={"ok": True}) as start:
                await registry.call("code_process_start", {"command": "echo test"})
            start.assert_called_once_with("echo test", self.workspace)

    def test_powershell_tool_cwd_and_resource_home(self):
        manager = user_tool_module.UserToolManager(self.root / "tools", ToolRegistry())
        script = self.root / "tools" / "tool.ps1"
        process = Mock(returncode=0)
        process.communicate.return_value = (b"ok", b"")
        with patch.object(user_tool_module.subprocess, "Popen", return_value=process) as popen:
            self.assertEqual("ok", manager._run_script(script, {}, 10))
        env = popen.call_args.kwargs["env"]
        self.assertEqual(str(self.workspace), popen.call_args.kwargs["cwd"])
        self.assertEqual(str(script.parent), env["XIAOA_TOOL_HOME"])
        self.assertEqual(str(self.workspace), env["XIAOA_WORKSPACE"])
        self.assertEqual(str(isolation_workspace.temporary()), env["TEMP"])

    def test_python_plugin_process_outputs_and_temps_stay_in_workspace(self):
        manager = PluginRuntimeManager(self.root / "plugins", ToolRegistry())
        version = self.root / "plugins" / "test" / "v1"
        code = version / "code"
        code.mkdir(parents=True)
        (code / "main.py").write_text(
            "import json,os,tempfile;from pathlib import Path;"
            "Path('plugin-output.txt').write_text('ok');"
            "print(json.dumps({'cwd':os.getcwd(),'temp':tempfile.gettempdir(),'home':os.environ['XIAOA_PLUGIN_HOME']}))",
            encoding="utf-8",
        )
        manifest = {"parameters": {"type": "object", "properties": {}}, "entry": "main.py", "runtime": "python", "timeout": 10}
        with patch.object(manager, "_python", return_value=Path(sys.executable)):
            result = manager._execute(version, manifest, {})
        self.assertEqual(str(self.workspace), result["cwd"])
        self.assertEqual(str(code), result["home"])
        self.assertEqual(isolation_workspace.temporary(), Path(result["temp"]))
        self.assertTrue((self.workspace / "plugin-output.txt").is_file())
        self.assertFalse((code / "plugin-output.txt").exists())

    async def test_browser_downloads_configured_once_and_fail_closed(self):
        controller = browser_module.BrowserController(profile_dir=self.root / "profile", port=1986)
        controller._http = AsyncMock(return_value={"webSocketDebuggerUrl": "ws://127.0.0.1/browser/session"})
        controller._available = AsyncMock(return_value=True)
        controller._cdp_request = AsyncMock()
        socket = SimpleNamespace(close_code=None, close=AsyncMock())
        with patch.object(browser_module.websockets, "connect", new=AsyncMock(return_value=socket)) as connect:
            await controller.ensure_browser()
            await controller.ensure_browser()
            connect.assert_awaited_once()
            controller._cdp_request.assert_awaited_once()
            target, request_id, method, params, timeout = controller._cdp_request.call_args.args
            self.assertIs(socket, target)
            self.assertEqual("Browser.setDownloadBehavior", method)
            self.assertEqual(str(self.workspace / "downloads"), params["downloadPath"])
            controller._download_session = ""
            controller._cdp_request.side_effect = RuntimeError("CDP failed")
            with self.assertRaisesRegex(RuntimeError, "CDP failed"):
                await controller.ensure_browser()
            self.assertEqual("", controller._download_session)
            self.assertIsNone(controller._download_socket)
            socket.close.assert_awaited()

    def test_screenshots_keep_original_for_user_and_readable_budgeted_observation(self):
        image = Image.new("RGB", (1920, 1080), "white")
        result = computer_module.screenshot_result(image, "截图")
        full = base64.b64decode(result["_display_image"])
        small = base64.b64decode(result["_observation_image"])
        self.assertEqual((1920, 1080), Image.open(io.BytesIO(full)).size)
        self.assertEqual((1280, 720), Image.open(io.BytesIO(small)).size)
        self.assertEqual({"width": 1280, "height": 720}, result["observation_size"])
        self.assertIn("观察图 1280x720", result["text"])
        self.assertLessEqual(len(small), computer_module._SCREENSHOT_MAX_BYTES)
        saved = Path(result["path"])
        self.assertEqual(self.workspace / "screenshots", saved.parent)
        self.assertEqual(full, saved.read_bytes())
        self.assertEqual(result["_display_image"], redact_parameters(result, skip_media=True)["_display_image"])
        with patch.object(computer_module, "compress_screenshot", return_value=(None, 1920, 1080)):
            result = computer_module.screenshot_result(image, "截图")
        self.assertNotIn("_observation_image", result)
        self.assertTrue(Path(result["path"]).is_file())

    async def test_agent_displays_hd_but_only_passes_small_image_to_model(self):
        registry = ToolRegistry()
        async def shot():
            return {"text": "截图", "_display_image": "SElHSFJFUw==", "_observation_image": "TE9XUkVT", "path": str(self.workspace / "shot.jpg")}
        registry.register(Tool(name="screen_capture", description="截图", parameters={"type": "object", "properties": {}}, handler=shot))
        class LLM:
            def __init__(self):
                self.inputs = []
            async def chat_stream_tools(self, messages, **kwargs):
                self.inputs.append(copy.deepcopy(messages))
                if len(self.inputs) == 1:
                    yield "done", {"role": "assistant", "content": None, "tool_calls": [{"id": "shot", "type": "function", "function": {"name": "screen_capture", "arguments": "{}"}}]}
                else:
                    yield "done", {"role": "assistant", "content": "完成"}
        llm = LLM()
        events = [event async for event in Agent(llm, registry, AgentConfig(max_steps=3)).run("截图")]
        self.assertEqual("SElHSFJFUw==", next(e.data["image"] for e in events if e.type == "screenshot"))
        self.assertIn("TE9XUkVT", json.dumps(llm.inputs[-1]))
        self.assertNotIn("SElHSFJFUw==", json.dumps(llm.inputs[-1]))

    def test_cleanup_preserves_all_workspace_outputs_and_empty_directories(self):
        document = self.workspace / "final.docx"
        document.write_bytes(b"finished document")
        empty = self.workspace / "next-task"
        empty.mkdir()
        disposable = isolation_workspace.temporary() / "cache.bin"
        disposable.write_bytes(b"temporary")
        with patch.object(isolation_workspace, "normalize_max_mb", return_value=0):
            result = isolation_workspace.cleanup()
        self.assertTrue(document.is_file())
        self.assertTrue(empty.is_dir())
        self.assertFalse(disposable.exists())
        self.assertEqual(len(b"finished document"), result["used_bytes"])

    def test_cleanup_preserves_live_frozen_runtime(self):
        runtime = self.root / "isolation" / "tmp" / "runtime"
        backend = runtime / "resources" / "backend"
        backend.mkdir(parents=True)
        (backend / "xiaoa-backend.exe").write_bytes(b"backend")
        payload = runtime / "resources" / "backend" / "_internal" / "server-payload" / "backend" / "core"
        payload.mkdir(parents=True)
        live_module = payload / "cloud_jobs.py"
        live_module.write_bytes(b"live payload")
        disposable = isolation_workspace.temporary() / "old-cache.bin"
        disposable.write_bytes(b"temporary")
        with patch.object(isolation_workspace.sys, "executable", str(backend / "xiaoa-backend.exe")), \
             patch.object(isolation_workspace, "normalize_max_mb", return_value=0):
            result = isolation_workspace.cleanup()
        self.assertTrue(live_module.exists())
        self.assertFalse(disposable.exists())
        self.assertEqual((backend / "xiaoa-backend.exe").stat().st_size + live_module.stat().st_size, result["used_bytes"])

    def test_cad_backups_are_unique_and_not_automatically_deleted(self):
        drawing = self.root / "drawing.dwg"
        drawing.write_bytes(b"AC1032original")
        first = Path(cad_module._backup_drawing(drawing))
        second = Path(cad_module._backup_drawing(drawing))
        self.assertNotEqual(first, second)
        self.assertEqual(self.workspace / "backups" / "cad", first.parent)
        self.assertEqual(drawing.read_bytes(), first.read_bytes())
        disposable = isolation_workspace.temporary() / "cache.bin"
        disposable.write_bytes(b"temporary")
        with patch.object(isolation_workspace, "normalize_max_mb", return_value=0):
            isolation_workspace.cleanup()
        self.assertTrue(first.is_file())
        self.assertTrue(second.is_file())
        self.assertFalse(disposable.exists())
        with patch.object(cad_module.shutil, "copy2", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                cad_module._backup_drawing(drawing)


if __name__ == "__main__":
    unittest.main()
