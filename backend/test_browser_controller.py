"""浏览器专用控制器的无浏览器协议单测。"""
from __future__ import annotations

import unittest
import os
import asyncio
import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.tools import ToolRegistry
from backend.modules.computer import browser_module
from backend.modules.computer.browser_module import BrowserController, register_tools


class FakeBrowser(BrowserController):
    def __init__(self) -> None:
        super().__init__(profile_dir=".")
        self.calls = []
        self.target = {"id": "tab-1", "webSocketDebuggerUrl": "ws://test"}

    async def ensure_browser(self):
        return {"connected": True}

    async def list_tabs(self):
        return [{**self.target, "title": "测试页", "url": "https://example.test", "type": "page"}]

    async def _target(self, target_id: str = ""):
        return self.target

    async def _cdp(self, target, method, params=None, timeout=15.0):
        self.calls.append((method, params or {}))
        if method == "Accessibility.getFullAXTree":
            return {"nodes": [{
                "role": {"value": "button"}, "name": {"value": "登录"},
                "backendDOMNodeId": 42,
            }]}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "object-1"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": {"clicked": True, "typed": True}}}
        return {}

    async def _call_ref_once(self, target, saved, function, arguments=None):
        self.calls.append(("same_session_ref_call", {"backend_id": saved["backend_id"]}))
        return {"clicked": True, "typed": True}

    async def _evaluate(self, target, expression):
        if "document.title" in expression:
            return {"title": "测试页", "url": "https://example.test", "text": "登录"}
        return {"clicked": True, "typed": True}


class UnreachableBrowser(FakeBrowser):
    async def _evaluate(self, target, expression):
        if expression == "document.readyState":
            return "complete"
        if expression == "location.href":
            return "chrome-error://chromewebdata/"
        return await super()._evaluate(target, expression)


class StaleOnceBrowser(FakeBrowser):
    def __init__(self) -> None:
        super().__init__()
        self.ref_attempts = 0

    async def _call_ref_once(self, target, saved, function, arguments=None):
        self.ref_attempts += 1
        if self.ref_attempts == 1:
            raise RuntimeError("Could not find object with given id")
        return {"clicked": True}


class BrowserControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tab_actions_accept_plain_text_and_update_state(self):
        for action, body in [("activate", "Target activated"), ("close", "Target is closing")]:
            with self.subTest(action=action):
                browser = BrowserController(Path("."))
                browser._active_target = "tab-1"
                browser._refs["B1"] = {"target_id": "tab-1"}

                def respond(request):
                    self.assertEqual("GET", request.method)
                    self.assertEqual(f"/json/{action}/tab-1", request.url.path)
                    return browser_module.httpx.Response(200, text=body)

                client = browser_module.httpx.AsyncClient(
                    transport=browser_module.httpx.MockTransport(respond), trust_env=False,
                )
                with patch.object(browser_module.httpx, "AsyncClient", return_value=client):
                    result = await browser.tabs(action, "tab-1")
                self.assertTrue(result["ok"])
                self.assertEqual("tab-1" if action == "activate" else "", browser._active_target)
                if action == "close":
                    self.assertEqual({}, browser._refs)

    async def test_json_endpoints_still_reject_invalid_json(self):
        browser = BrowserController(Path("."))
        client = browser_module.httpx.AsyncClient(transport=browser_module.httpx.MockTransport(
            lambda request: browser_module.httpx.Response(200, text="unexpected text"),
        ), trust_env=False)
        with patch.object(browser_module.httpx, "AsyncClient", return_value=client):
            with self.assertRaises(json.JSONDecodeError):
                await browser._http("GET", "/json/version")

    async def test_failed_close_does_not_clear_active_target_or_refs(self):
        browser = BrowserController(Path("."))
        browser._active_target = "tab-1"
        browser._refs["B1"] = {"target_id": "tab-1"}
        client = browser_module.httpx.AsyncClient(transport=browser_module.httpx.MockTransport(
            lambda request: browser_module.httpx.Response(404, text="No such target id"),
        ), trust_env=False)
        with patch.object(browser_module.httpx, "AsyncClient", return_value=client):
            with self.assertRaises(browser_module.httpx.HTTPStatusError):
                await browser.tabs("close", "tab-1")
        self.assertEqual("tab-1", browser._active_target)
        self.assertIn("B1", browser._refs)

    async def test_cdp_timeouts_include_method_and_duration(self):
        browser = BrowserController(Path("."))
        socket = SimpleNamespace(send=AsyncMock(), recv=AsyncMock(side_effect=TimeoutError()))
        with self.assertRaisesRegex(TimeoutError, r"Page.captureScreenshot.*15"):
            await browser._cdp_request(socket, 1, "Page.captureScreenshot")
        socket.recv.assert_awaited_once()
        with patch.object(browser_module.time, "monotonic", side_effect=[0, 16]):
            with self.assertRaisesRegex(TimeoutError, r"Page.captureScreenshot.*15"):
                await browser._cdp_request(socket, 2, "Page.captureScreenshot")

    async def test_screenshot_retries_timeout_using_viewport_capture(self):
        with TemporaryDirectory() as tmp:
            browser = FakeBrowser()
            payload = b"test screenshot bytes"
            browser._cdp = AsyncMock(side_effect=[TimeoutError(), {
                "data": base64.b64encode(payload).decode(),
            }])
            with patch.object(browser_module.isolation_workspace, "workspace", return_value=Path(tmp)):
                result = await browser.screenshot()
            self.assertTrue(result["ok"])
            self.assertEqual(payload, Path(result["path"]).read_bytes())
            self.assertEqual([True, False], [call.args[2]["fromSurface"] for call in browser._cdp.await_args_list])

    async def test_repeated_screenshot_timeout_is_actionable_and_bounded(self):
        browser = FakeBrowser()
        browser._cdp = AsyncMock(side_effect=TimeoutError())
        with self.assertRaisesRegex(TimeoutError, "截图超时.*browser_snapshot"):
            await browser.screenshot()
        self.assertEqual(2, browser._cdp.await_count)

    async def test_screenshot_does_not_retry_cancellation_or_other_errors(self):
        for error in [asyncio.CancelledError(), RuntimeError("Target closed")]:
            with self.subTest(error=type(error).__name__):
                browser = FakeBrowser()
                browser._cdp = AsyncMock(side_effect=error)
                with self.assertRaises(type(error)):
                    await browser.screenshot()
                browser._cdp.assert_awaited_once()

    async def test_download_session_survives_tool_calls_and_reconnects(self):
        with TemporaryDirectory() as tmp:
            browser = BrowserController(Path(tmp) / "profile")
            url = "ws://127.0.0.1:54321/devtools/browser/test"
            browser._http = AsyncMock(return_value={"webSocketDebuggerUrl": url})
            browser._cdp_request = AsyncMock(return_value={})
            first = SimpleNamespace(close_code=None, close=AsyncMock())
            second = SimpleNamespace(close_code=None, close=AsyncMock())
            with patch.object(browser_module.websockets, "connect", new=AsyncMock(side_effect=[first, second])) as connect, \
                 patch.object(browser_module.isolation_workspace, "workspace", return_value=Path(tmp)):
                await browser._configure_downloads()
                await browser._configure_downloads()
                connect.assert_awaited_once()
                first.close.assert_not_awaited()
                browser._cdp_request.assert_awaited_once_with(first, 1, "Browser.setDownloadBehavior", {
                    "behavior": "allow", "downloadPath": str(Path(tmp) / "downloads"),
                }, 15.0)
                first.close_code = 1006
                await browser._configure_downloads()
                self.assertEqual(2, connect.await_count)
                self.assertIs(browser._download_socket, second)
                await browser.close()
            first.close.assert_awaited_once()
            second.close.assert_awaited_once()
            self.assertIsNone(browser._download_socket)

    async def test_failed_download_setup_closes_socket_and_allows_retry(self):
        with TemporaryDirectory() as tmp:
            browser = BrowserController(Path(tmp) / "profile")
            browser._http = AsyncMock(return_value={"webSocketDebuggerUrl": "ws://test"})
            browser._cdp_request = AsyncMock(side_effect=RuntimeError("download setup failed"))
            socket = SimpleNamespace(close_code=None, close=AsyncMock())
            with patch.object(browser_module.websockets, "connect", new=AsyncMock(return_value=socket)), \
                 patch.object(browser_module.isolation_workspace, "workspace", return_value=Path(tmp)):
                with self.assertRaisesRegex(RuntimeError, "download setup failed"):
                    await browser._configure_downloads()
            socket.close.assert_awaited_once()
            self.assertIsNone(browser._download_socket)
            self.assertEqual("", browser._download_session)

    @unittest.skipUnless(browser_module.psutil is not None, "psutil is Windows-only in the release requirements")
    def test_profile_reconnect_only_matches_exact_browser_and_profile(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe, profile = root / "chrome.exe", root / "private-profile"
            ordinary = SimpleNamespace(info={"exe": str(exe), "cmdline": [
                f"--user-data-dir={root / 'user-profile'}", "--remote-debugging-port=54321"]})
            other_exe = SimpleNamespace(info={"exe": str(root / "other.exe"), "cmdline": [
                f"--user-data-dir={profile}", "--remote-debugging-port=54321"]})
            matching = SimpleNamespace(info={"exe": str(exe), "cmdline": [
                f"--user-data-dir={profile}", "--remote-debugging-port=54322"]})
            with patch.object(browser_module.psutil, "process_iter", return_value=[ordinary, other_exe]):
                self.assertIsNone(browser_module._profile_debug_port(profile, exe))
            with patch.object(browser_module.psutil, "process_iter", return_value=[ordinary, matching]):
                self.assertEqual(54322, browser_module._profile_debug_port(profile, exe))
            matching.info["cmdline"][-1] = "--remote-debugging-port=99999"
            with patch.object(browser_module.psutil, "process_iter", return_value=[matching]):
                self.assertIsNone(browser_module._profile_debug_port(profile, exe))

    async def test_reconnects_old_private_port_without_relaunch_or_taking_ownership(self):
        browser = BrowserController(Path("private-profile"), port=54321)
        browser._available = AsyncMock(side_effect=[False, False, True])
        browser._configure_downloads = AsyncMock()
        with patch.object(browser_module, "_browser_executable", return_value=Path("chrome.exe")), \
             patch.object(browser_module, "_profile_debug_port", return_value=54322), \
             patch.object(browser_module.subprocess, "Popen") as launch:
            info = await browser.ensure_browser()
        self.assertEqual(54322, info["port"])
        self.assertEqual("http://127.0.0.1:54322", browser.endpoint)
        self.assertIsNone(browser._process)
        launch.assert_not_called()
        browser._configure_downloads.assert_awaited_once()

    async def test_cold_start_can_connect_after_twelve_seconds(self):
        with TemporaryDirectory() as tmp:
            browser = BrowserController(Path(tmp) / "profile", port=54321)
            browser._available = AsyncMock(side_effect=[False, False, False, True])
            browser._configure_downloads = AsyncMock()
            process = MagicMock()
            process.poll.return_value = None
            clock = SimpleNamespace(monotonic=MagicMock(side_effect=[0, 0, 13]))
            with patch.object(browser_module, "time", clock), \
                 patch.object(browser_module.asyncio, "sleep", new=AsyncMock()), \
                 patch.object(browser_module, "_browser_executable", return_value=Path("chrome.exe")), \
                 patch.object(browser_module, "_profile_debug_port", return_value=None), \
                 patch.object(browser_module.isolation_workspace, "workspace", return_value=Path(tmp)), \
                 patch.object(browser_module.subprocess, "Popen", return_value=process) as launch:
                info = await browser.ensure_browser()
            self.assertTrue(info["connected"])
            launch.assert_called_once()

    async def test_timeout_retry_does_not_launch_a_second_live_browser(self):
        with TemporaryDirectory() as tmp:
            browser = BrowserController(Path(tmp) / "profile", port=54321)
            browser._available = AsyncMock(return_value=False)
            process = MagicMock()
            process.poll.return_value = None
            clock = SimpleNamespace(monotonic=MagicMock(side_effect=[0, 31, 40, 71]))
            with patch.object(browser_module, "time", clock), \
                 patch.object(browser_module, "_browser_executable", return_value=Path("chrome.exe")), \
                 patch.object(browser_module, "_profile_debug_port", return_value=None), \
                 patch.object(browser_module.isolation_workspace, "workspace", return_value=Path(tmp)), \
                 patch.object(browser_module.subprocess, "Popen", return_value=process) as launch:
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, "30 秒"):
                        await browser.ensure_browser()
            launch.assert_called_once()
            self.assertIs(browser._process, process)

    async def test_nonzero_exit_reports_browser_start_failure(self):
        with TemporaryDirectory() as tmp:
            browser = BrowserController(Path(tmp) / "profile", port=54321)
            browser._available = AsyncMock(return_value=False)
            process = MagicMock()
            process.poll.return_value = 7
            with patch.object(browser_module, "_browser_executable", return_value=Path("chrome.exe")), \
                 patch.object(browser_module, "_profile_debug_port", return_value=None), \
                 patch.object(browser_module.isolation_workspace, "workspace", return_value=Path(tmp)), \
                 patch.object(browser_module.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "退出码 7"):
                    await browser.ensure_browser()

    async def test_zero_exit_handoff_recovers_existing_profile_port(self):
        with TemporaryDirectory() as tmp:
            browser = BrowserController(Path(tmp) / "profile", port=54321)
            browser._available = AsyncMock(side_effect=[False, False, False, True])
            browser._configure_downloads = AsyncMock()
            process = MagicMock()
            process.poll.return_value = 0
            with patch.object(browser_module, "_browser_executable", return_value=Path("chrome.exe")), \
                 patch.object(browser_module, "_profile_debug_port", side_effect=[None, 54322]), \
                 patch.object(browser_module.asyncio, "sleep", new=AsyncMock()), \
                 patch.object(browser_module.isolation_workspace, "workspace", return_value=Path(tmp)), \
                 patch.object(browser_module.subprocess, "Popen", return_value=process):
                info = await browser.ensure_browser()
            self.assertEqual(54322, info["port"])

    async def test_local_file_url_is_not_prefixed_with_https(self):
        browser = FakeBrowser()
        url = "file:///D:/资料/方案.pdf"
        browser._evaluate = AsyncMock(side_effect=["complete", url, {"url": url, "title": "方案"}])
        result = await browser.open(url)
        self.assertTrue(result["ok"])
        self.assertIn(("Page.navigate", {"url": url}), browser.calls)

    async def test_unsupported_and_remote_file_urls_fail_before_opening_browser(self):
        for url in ("", "javascript:alert(1)", "data:text/html,hello", "ftp://example.test", "file://server/share/test.pdf", "https://"):
            with self.subTest(url=url):
                browser = FakeBrowser()
                with self.assertRaises(ValueError):
                    await browser.open(url)
                self.assertFalse(browser.calls)

    def test_linux_chromium_candidate_and_headless_flags(self):
        with TemporaryDirectory() as tmp:
            executable = Path(tmp) / "chromium"
            executable.write_text("", encoding="utf-8")

            def which(command):
                return str(executable) if command == "chromium" else None

            with patch.dict(os.environ, {"XIAOA_BROWSER_PATH": "", "PROGRAMFILES": "",
                                         "PROGRAMFILES(X86)": "", "LOCALAPPDATA": ""}, clear=False), \
                 patch.object(browser_module.shutil, "which", side_effect=which), \
                 patch.object(Path, "is_file", return_value=True):
                self.assertEqual(executable, browser_module._browser_executable())
        self.assertEqual(["--start-minimized"], browser_module._browser_launch_flags("nt"))
        self.assertEqual(
            ["--headless=new", "--no-sandbox", "--disable-dev-shm-usage"],
            browser_module._browser_launch_flags("posix"),
        )

    async def test_snapshot_refs_and_click_are_structured(self):
        browser = FakeBrowser()
        snapshot = await browser.snapshot()
        self.assertEqual("B1", snapshot["elements"][0]["ref"])
        self.assertEqual("登录", snapshot["elements"][0]["name"])
        result = await browser.click(ref="B1")
        self.assertTrue(result["ok"])
        self.assertIn("same_session_ref_call", [name for name, _ in browser.calls])

    async def test_browser_open_reports_chrome_error_page(self):
        result = await UnreachableBrowser().open("https://example.test")
        self.assertFalse(result["ok"])
        self.assertIn("不可达", result["error"])

    async def test_stale_object_id_is_retried_once(self):
        browser = StaleOnceBrowser()
        await browser.snapshot()
        result = await browser.click(ref="B1")
        self.assertTrue(result["ok"])
        self.assertEqual(2, browser.ref_attempts)

    async def test_tools_are_registered_in_one_browser_group(self):
        registry = ToolRegistry()
        register_tools(registry, FakeBrowser())
        self.assertEqual({
            "browser_open", "browser_snapshot", "browser_click", "browser_type",
            "browser_select", "browser_upload", "browser_scroll", "browser_wait",
            "browser_tabs", "browser_screenshot",
        }, set(registry.names()))


if __name__ == "__main__":
    unittest.main()
