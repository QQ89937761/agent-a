"""Chrome/Edge 专用控制器：通过本机 CDP 读取和操作真实网页结构。"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import shutil
import subprocess
import time
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
try:
    import psutil
except ImportError:  # Linux CI does not install Windows-only process inspection.
    psutil = None
import websockets

from backend.core.tools import ToolRegistry
from backend.core import isolation_workspace
from backend.core.process import hidden_subprocess_kwargs


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _browser_executable() -> Path | None:
    configured = os.getenv("XIAOA_BROWSER_PATH", "").strip()
    candidates = [Path(configured)] if configured else []
    for env_name, suffix in (
        ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
        ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
        ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
        ("PROGRAMFILES", "Microsoft/Edge/Application/msedge.exe"),
        ("PROGRAMFILES(X86)", "Microsoft/Edge/Application/msedge.exe"),
    ):
        root = os.getenv(env_name, "").strip()
        if root:
            candidates.append(Path(root) / suffix)
    for command in (
        "chrome", "msedge", "chromium", "chromium-browser",
        "google-chrome", "google-chrome-stable",
    ):
        found = shutil.which(command)
        if found:
            candidates.append(Path(found))
    for path in ("/snap/bin/chromium", "/usr/bin/chromium", "/usr/bin/chromium-browser"):
        candidates.append(Path(path))
    return next((path for path in candidates if path.is_file()), None)


def _profile_debug_port(profile_dir: Path, executable: Path) -> int | None:
    """Only reconnect the same executable using XiaoA's exact private profile."""
    if psutil is None:
        return None
    profile = profile_dir.resolve()
    for process in psutil.process_iter(["exe", "cmdline"]):
        try:
            info = process.info
            if not info.get("exe") or Path(info["exe"]).resolve() != executable.resolve():
                continue
            args = info.get("cmdline") or []
            directory = next((arg.split("=", 1)[1] for arg in args if arg.startswith("--user-data-dir=")), "")
            port = next((arg.split("=", 1)[1] for arg in args if arg.startswith("--remote-debugging-port=")), "")
            if directory and Path(directory.strip('"')).resolve() == profile and port.isdigit() and 1024 <= int(port) <= 65535:
                return int(port)
        except (psutil.Error, OSError, ValueError):
            continue
    return None


def _browser_launch_flags(platform_name: str | None = None) -> list[str]:
    """Linux 云端无显示器；Windows 保持有头并保留登录态。"""
    if (platform_name or os.name) == "nt":
        return ["--start-minimized"]
    return ["--headless=new", "--no-sandbox", "--disable-dev-shm-usage"]


class BrowserController:
    """Small CDP client for the dedicated XiaoA browser profile."""

    def __init__(self, profile_dir: Path, port: int | None = None) -> None:
        self.profile_dir = Path(profile_dir)
        configured = str(port) if port is not None else os.getenv("XIAOA_BROWSER_DEBUG_PORT", "").strip()
        self.port = int(configured) if configured else _free_loopback_port()
        if not 1024 <= self.port <= 65535:
            raise ValueError("浏览器调试端口必须在 1024-65535")
        self.endpoint = f"http://127.0.0.1:{self.port}"
        self._active_target = ""
        self._download_session = ""
        self._download_socket: Any = None
        self._download_lock = asyncio.Lock()
        self._refs: dict[str, dict[str, Any]] = {}
        self._launch_lock = asyncio.Lock()
        self._process: subprocess.Popen | None = None

    async def _http(self, method: str, path: str, timeout: float = 5.0) -> Any:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.request(method, self.endpoint + path)
            response.raise_for_status()
            return response.json() if response.content else {}

    async def _available(self) -> bool:
        try:
            data = await self._http("GET", "/json/version", 1.0)
            return bool(data.get("webSocketDebuggerUrl"))
        except Exception:
            return False

    async def _configure_downloads(self) -> None:
        version = await self._http("GET", "/json/version")
        socket_url = str(version.get("webSocketDebuggerUrl") or "")
        if not socket_url:
            raise RuntimeError("无法设置隔离下载目录：浏览器控制通道不存在")
        async with self._download_lock:
            if (socket_url == self._download_session and self._download_socket is not None
                    and self._download_socket.close_code is None):
                return
            if self._download_socket is not None:
                await self._download_socket.close()
            self._download_socket = None
            self._download_session = ""
            directory = isolation_workspace.workspace() / "downloads"
            directory.mkdir(parents=True, exist_ok=True)
            # Chromium resets this session-scoped setting when its CDP socket closes.
            # Keep the socket alive so files never fall back to ordinary Downloads.
            socket = await websockets.connect(socket_url, open_timeout=15.0)
            try:
                await self._cdp_request(socket, 1, "Browser.setDownloadBehavior", {
                    "behavior": "allow", "downloadPath": str(directory),
                }, 15.0)
            except BaseException:
                await socket.close()
                raise
            self._download_socket = socket
            self._download_session = socket_url

    async def ensure_browser(self) -> dict[str, Any]:
        if await self._available():
            await self._configure_downloads()
            return {"connected": True, "port": self.port, "profile": str(self.profile_dir)}
        async with self._launch_lock:
            if await self._available():
                await self._configure_downloads()
                return {"connected": True, "port": self.port, "profile": str(self.profile_dir)}
            executable = _browser_executable()
            if executable is None:
                raise RuntimeError("没有找到 Chrome 或 Edge")
            # A backend restart can leave the dedicated profile running on its old port.
            # Never reconnect the user's ordinary browser, or launch repeatedly after a timeout.
            old_port = await asyncio.to_thread(_profile_debug_port, self.profile_dir, executable)
            if old_port is not None:
                self.port = old_port
                self.endpoint = f"http://127.0.0.1:{self.port}"
            running = self._process is not None and self._process.poll() is None
            if old_port is None and not running:
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                self._process = subprocess.Popen(
                    [
                        str(executable),
                        f"--remote-debugging-port={self.port}",
                        "--remote-debugging-address=127.0.0.1",
                        f"--user-data-dir={self.profile_dir}",
                        "--no-first-run",
                        "--no-default-browser-check",
                        *_browser_launch_flags(),
                        "about:blank",
                    ],
                    cwd=str(isolation_workspace.workspace()),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **hidden_subprocess_kwargs(new_process_group=True),
                )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if await self._available():
                    await self._configure_downloads()
                    return {
                        "connected": True,
                        "browser": executable.name,
                        "port": self.port,
                        "profile": str(self.profile_dir),
                    }
                exit_code = self._process.poll() if self._process else None
                if exit_code is not None and exit_code != 0:
                    raise RuntimeError(
                        f"浏览器进程启动失败（退出码 {exit_code}），请检查 Chromium 安装和启动参数"
                    )
                # A zero exit can be Chromium handing off to its existing profile owner.
                if exit_code == 0 and old_port is None:
                    old_port = await asyncio.to_thread(_profile_debug_port, self.profile_dir, executable)
                    if old_port is not None:
                        self.port = old_port
                        self.endpoint = f"http://127.0.0.1:{self.port}"
                await asyncio.sleep(0.2)
            raise RuntimeError("专用浏览器控制通道在 30 秒内没有就绪；稍后重试会复用同一浏览器，不会重复启动。若持续失败，请检查浏览器调试策略或启动环境")

    async def list_tabs(self) -> list[dict[str, Any]]:
        await self.ensure_browser()
        raw = await self._http("GET", "/json")
        return [
            {
                "id": str(item.get("id") or ""),
                "title": str(item.get("title") or "")[:300],
                "url": str(item.get("url") or "")[:2000],
                "type": item.get("type"),
                "webSocketDebuggerUrl": item.get("webSocketDebuggerUrl"),
                "active": str(item.get("id") or "") == self._active_target,
            }
            for item in raw if item.get("type") == "page" and item.get("webSocketDebuggerUrl")
        ]

    async def _new_tab(self, url: str = "about:blank") -> dict[str, Any]:
        await self.ensure_browser()
        return await self._http("PUT", "/json/new?" + quote(url, safe=""))

    async def _target(self, target_id: str = "") -> dict[str, Any]:
        tabs = await self.list_tabs()
        wanted = target_id or self._active_target
        target = next((item for item in tabs if item["id"] == wanted), None)
        if target_id and target is None:
            raise ValueError(f"浏览器标签页不存在或已关闭：{target_id}")
        if target is None and tabs:
            target = tabs[0]
        if target is None:
            created = await self._new_tab()
            target = {
                "id": str(created.get("id") or ""),
                "title": str(created.get("title") or ""),
                "url": str(created.get("url") or ""),
                "webSocketDebuggerUrl": created.get("webSocketDebuggerUrl"),
            }
        if not target.get("webSocketDebuggerUrl"):
            raise RuntimeError("浏览器标签页没有可用的控制通道")
        self._active_target = target["id"]
        return target

    async def close(self) -> None:
        process, self._process = self._process, None
        self._active_target = ""
        async with self._download_lock:
            if self._download_socket is not None:
                await self._download_socket.close()
            self._download_socket = None
            self._download_session = ""
        self._refs.clear()
        if process is None or process.poll() is not None:
            return

        def stop() -> None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                    **hidden_subprocess_kwargs(),
                )
            else:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

        await asyncio.to_thread(stop)

    async def _cdp(
        self, target: dict[str, Any], method: str, params: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        async with websockets.connect(
            target["webSocketDebuggerUrl"], open_timeout=timeout, max_size=16 * 1024 * 1024,
        ) as socket:
            return await self._cdp_request(socket, 1, method, params, timeout)

    async def _cdp_request(
        self, socket: Any, request_id: int, method: str,
        params: dict[str, Any] | None = None, timeout: float = 15.0,
    ) -> dict[str, Any]:
        """在同一个 CDP 会话中完成一次请求。"""
        await socket.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"浏览器操作超时：{method}")
            payload = json.loads(await asyncio.wait_for(socket.recv(), remaining))
            if payload.get("id") != request_id:
                continue
            if payload.get("error"):
                raise RuntimeError(str(payload["error"].get("message") or payload["error"]))
            return payload.get("result") or {}

    async def _evaluate(self, target: dict[str, Any], expression: str) -> Any:
        result = await self._cdp(target, "Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        remote = result.get("result") or {}
        if remote.get("subtype") == "error":
            raise RuntimeError(str(remote.get("description") or "网页执行失败"))
        return remote.get("value")

    async def open(self, url: str, new_tab: bool = False) -> dict[str, Any]:
        url = str(url or "").strip()
        if not url:
            raise ValueError("浏览器地址不能为空")
        if not urlsplit(url).scheme or ("://" not in url and url.partition(":")[2].split("/", 1)[0].isdigit()):
            url = "https://" + url
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https", "file"}:
            raise ValueError("浏览器仅支持 HTTP、HTTPS 或本地 file:// 地址")
        if parsed.scheme == "file":
            if parsed.netloc not in {"", "localhost"} or not parsed.path.startswith("/"):
                raise ValueError("file:// 仅支持本机绝对路径")
        elif not parsed.hostname:
            raise ValueError("网页地址缺少主机名")
        if new_tab:
            created = await self._new_tab("about:blank")
            target = await self._target(str(created.get("id") or ""))
        else:
            target = await self._target()
        await self._cdp(target, "Page.navigate", {"url": url})
        self._refs.clear()
        deadline = time.monotonic() + 15
        state = "loading"
        current = ""
        last_error = ""
        while time.monotonic() < deadline:
            try:
                state = str(await self._evaluate(target, "document.readyState"))
                current = str(await self._evaluate(target, "location.href"))
                if state in {"interactive", "complete"} and current not in {"", "about:blank"}:
                    break
            except Exception as exc:
                last_error = str(exc)
            await asyncio.sleep(0.2)
        if current.startswith("chrome-error://"):
            return {
                "ok": False,
                "target_id": target["id"],
                "url": url,
                "ready_state": state,
                "error": "目标网页不可达，可能是网络、DNS 或站点拒绝访问",
            }
        if state not in {"interactive", "complete"} or current in {"", "about:blank"}:
            detail = f"：{last_error}" if last_error else ""
            return {
                "ok": False,
                "target_id": target["id"],
                "url": url,
                "ready_state": state,
                "error": f"网页在 15 秒内未完成加载{detail}",
            }
        return {"ok": True, "target_id": target["id"], "url": url, "ready_state": state}

    async def snapshot(self, target_id: str = "", limit: int = 220) -> dict[str, Any]:
        target = await self._target(target_id)
        tree = await self._cdp(target, "Accessibility.getFullAXTree")
        meta = await self._evaluate(target, "({title:document.title,url:location.href,text:(document.body?.innerText||'').slice(0,6000)})")
        roles = {
            "button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox",
            "listbox", "option", "menuitem", "tab", "switch", "slider", "heading", "img",
            "row", "cell",
        }
        self._refs.clear()
        elements = []
        for node in tree.get("nodes") or []:
            role = str((node.get("role") or {}).get("value") or "")
            name = str((node.get("name") or {}).get("value") or "").strip()
            backend_id = node.get("backendDOMNodeId")
            if role not in roles or not name or not backend_id:
                continue
            ref = f"B{len(elements) + 1}"
            item = {"ref": ref, "role": role, "name": name[:500]}
            self._refs[ref] = {"target_id": target["id"], "backend_id": int(backend_id), **item}
            elements.append(item)
            if len(elements) >= max(20, min(int(limit), 500)):
                break
        return {
            "ok": True,
            "target_id": target["id"],
            "title": str((meta or {}).get("title") or "")[:500],
            "url": str((meta or {}).get("url") or "")[:2000],
            "text": str((meta or {}).get("text") or ""),
            "elements": elements,
        }

    async def _call_ref_once(
        self, target: dict[str, Any], saved: dict[str, Any], function: str,
        arguments: list[dict[str, Any]] | None = None,
    ) -> Any:
        """解析和执行必须共用一个 CDP 会话，否则 objectId 会立即失效。"""
        async with websockets.connect(
            target["webSocketDebuggerUrl"], open_timeout=15.0, max_size=16 * 1024 * 1024,
        ) as socket:
            resolved = await self._cdp_request(
                socket, 1, "DOM.resolveNode", {"backendNodeId": saved["backend_id"]},
            )
            object_id = ((resolved.get("object") or {}).get("objectId"))
            if not object_id:
                raise RuntimeError("网页元素没有可用的对象引用")
            result = await self._cdp_request(socket, 2, "Runtime.callFunctionOn", {
                "objectId": object_id,
                "functionDeclaration": function,
                "arguments": arguments or [],
                "returnByValue": True,
                "awaitPromise": True,
            })
            return ((result.get("result") or {}).get("value"))

    async def _call_ref(self, ref: str, function: str, arguments: list[dict[str, Any]] | None = None) -> Any:
        saved = self._refs.get(ref)
        if not saved:
            raise ValueError(f"网页元素引用 {ref} 已失效，请重新读取页面")
        target = await self._target(saved["target_id"])
        for attempt in range(2):
            try:
                return await self._call_ref_once(target, saved, function, arguments)
            except RuntimeError as exc:
                message = str(exc).lower()
                if "could not find object with given id" in message and attempt == 0:
                    await asyncio.sleep(0)
                    continue
                if "could not find node with given id" in message:
                    raise ValueError(f"网页已刷新，元素引用 {ref} 已失效，请重新读取页面") from exc
                raise

    async def click(self, ref: str = "", text: str = "") -> dict[str, Any]:
        if ref:
            value = await self._call_ref(ref, """function(){
                let e=this; while(e && !e.matches?.('button,a,input,select,textarea,[role=button],[role=link],[tabindex]')) e=e.parentElement;
                e=e||this; e.scrollIntoView({block:'center'}); e.click();
                return {clicked:true,tag:e.tagName,label:(e.innerText||e.getAttribute?.('aria-label')||'').trim().slice(0,300)};
            }""")
        else:
            wanted = str(text or "").strip()
            if not wanted:
                raise ValueError("ref 和 text 至少提供一个")
            target = await self._target()
            value = await self._evaluate(target, f"""(() => {{
              const wanted={json.dumps(wanted, ensure_ascii=False)}.toLowerCase();
              const visible=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);
              const label=e=>(e.innerText||e.value||e.getAttribute('aria-label')||e.title||'').trim();
              const all=[...document.querySelectorAll('button,a,input[type=button],input[type=submit],[role=button],[role=link],[role=tab],[role=menuitem]')].filter(visible);
              const e=all.find(x=>label(x).toLowerCase()===wanted)||all.find(x=>label(x).toLowerCase().includes(wanted));
              if(!e) return {{clicked:false,error:'未找到元素'}};
              e.scrollIntoView({{block:'center'}}); e.click(); return {{clicked:true,tag:e.tagName,label:label(e).slice(0,300)}};
            }})()""")
        if not value or not value.get("clicked"):
            return {"ok": False, "error": (value or {}).get("error", "点击失败")}
        return {"ok": True, **value}

    async def type_text(self, value: str, ref: str = "", label: str = "", submit: bool = False) -> dict[str, Any]:
        if len(value) > 50_000:
            raise ValueError("单次网页输入不能超过 50000 个字符")
        function = """function(value){
          let e=this; if(!e.matches?.('input,textarea,[contenteditable=true]')) e=e.querySelector?.('input,textarea,[contenteditable=true]');
          if(!e) return {typed:false,error:'引用不是输入框'}; e.focus();
          if(e.isContentEditable) e.textContent=value;
          else { const p=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype; const s=Object.getOwnPropertyDescriptor(p,'value')?.set; s?s.call(e,value):e.value=value; }
          e.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:value})); e.dispatchEvent(new Event('change',{bubbles:true}));
          return {typed:true,tag:e.tagName};
        }"""
        if ref:
            result = await self._call_ref(ref, function, [{"value": value}])
            target = await self._target(self._refs[ref]["target_id"])
        else:
            wanted = str(label or "").strip()
            if not wanted:
                raise ValueError("ref 和 label 至少提供一个")
            target = await self._target()
            result = await self._evaluate(target, f"""(() => {{
              const wanted={json.dumps(wanted, ensure_ascii=False)}.toLowerCase(), value={json.dumps(value, ensure_ascii=False)};
              const all=[...document.querySelectorAll('input,textarea,[contenteditable=true]')];
              const name=e=>(e.getAttribute('aria-label')||e.placeholder||e.name||e.id||'').trim().toLowerCase();
              let e=all.find(x=>name(x)===wanted)||all.find(x=>name(x).includes(wanted));
              if(!e){{ const l=[...document.querySelectorAll('label')].find(x=>x.innerText.trim().toLowerCase().includes(wanted)); e=l?.control; }}
              if(!e) return {{typed:false,error:'未找到输入框'}}; e.focus();
              if(e.isContentEditable)e.textContent=value; else {{const p=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;const s=Object.getOwnPropertyDescriptor(p,'value')?.set;s?s.call(e,value):e.value=value;}}
              e.dispatchEvent(new InputEvent('input',{{bubbles:true,inputType:'insertText',data:value}}));e.dispatchEvent(new Event('change',{{bubbles:true}}));return {{typed:true,tag:e.tagName}};
            }})()""")
        if not result or not result.get("typed"):
            return {"ok": False, "error": (result or {}).get("error", "输入失败")}
        if submit:
            await self._cdp(target, "Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
            await self._cdp(target, "Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
        return {"ok": True, "characters": len(value), "submitted": submit, **result}

    async def select(self, option: str, ref: str) -> dict[str, Any]:
        result = await self._call_ref(ref, """function(wanted){
          let e=this; if(e.tagName!=='SELECT') e=e.querySelector?.('select'); if(!e)return {selected:false,error:'引用不是下拉框'};
          const q=String(wanted).toLowerCase(); const o=[...e.options].find(x=>x.text.trim().toLowerCase()===q||x.value.toLowerCase()===q)||[...e.options].find(x=>x.text.trim().toLowerCase().includes(q));
          if(!o)return {selected:false,error:'没有该选项'}; e.value=o.value; e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return {selected:true,label:o.text,value:o.value};
        }""", [{"value": option}])
        return {"ok": bool(result and result.get("selected")), **(result or {})}

    async def upload(self, path: str, ref: str) -> dict[str, Any]:
        file_path = Path(path).expanduser().resolve()
        if not file_path.is_file():
            raise ValueError(f"文件不存在：{file_path}")
        saved = self._refs.get(ref)
        if not saved:
            raise ValueError(f"网页元素引用 {ref} 已失效，请重新读取页面")
        target = await self._target(saved["target_id"])
        await self._cdp(target, "DOM.setFileInputFiles", {
            "files": [str(file_path)], "backendNodeId": saved["backend_id"],
        })
        return {"ok": True, "file": str(file_path), "ref": ref}

    async def scroll(self, amount: int = 700) -> dict[str, Any]:
        amount = max(-5000, min(int(amount), 5000))
        target = await self._target()
        value = await self._evaluate(target, f"scrollBy({{top:{amount},behavior:'instant'}}); ({{x:scrollX,y:scrollY}})")
        return {"ok": True, "amount": amount, **(value or {})}

    async def wait(self, text: str = "", url_contains: str = "", timeout: float = 10.0) -> dict[str, Any]:
        if not text and not url_contains:
            raise ValueError("text 和 url_contains 至少提供一个")
        target = await self._target()
        timeout = max(0.2, min(float(timeout), 30.0))
        deadline = time.monotonic() + timeout
        last = {}
        while time.monotonic() < deadline:
            last = await self._evaluate(target, "({url:location.href,text:(document.body?.innerText||'').slice(0,100000)})") or {}
            text_ok = not text or text in str(last.get("text") or "")
            url_ok = not url_contains or url_contains in str(last.get("url") or "")
            if text_ok and url_ok:
                return {"ok": True, "url": str(last.get("url") or ""), "matched": True}
            await asyncio.sleep(0.25)
        return {"ok": False, "matched": False, "timeout": timeout, "url": str(last.get("url") or "")}

    async def screenshot(self) -> dict[str, Any]:
        target = await self._target()
        result = await self._cdp(target, "Page.captureScreenshot", {"format": "png", "fromSurface": True})
        image = str(result.get("data") or "")
        if not image:
            raise RuntimeError("浏览器没有返回截图")
        payload = base64.b64decode(image, validate=True)
        directory = isolation_workspace.workspace() / "screenshots"
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="xiaoa-web-", suffix=".png", dir=directory, delete=False) as file:
            file.write(payload)
            saved_path = file.name
        return {"ok": True, "_observation_image": image, "path": saved_path,
                "text": f"已截取当前网页,保存到 {saved_path}。"}

    async def tabs(self, action: str = "list", target_id: str = "") -> dict[str, Any]:
        if action == "list":
            tabs = await self.list_tabs()
            return {"ok": True, "count": len(tabs), "tabs": tabs}
        if not target_id:
            raise ValueError("激活或关闭标签页需要 target_id")
        if action == "activate":
            await self._http("GET", "/json/activate/" + quote(target_id, safe=""))
            self._active_target = target_id
            return {"ok": True, "active_target": target_id}
        if action == "close":
            await self._http("GET", "/json/close/" + quote(target_id, safe=""))
            if self._active_target == target_id:
                self._active_target = ""
            self._refs.clear()
            return {"ok": True, "closed_target": target_id}
        raise ValueError("action 只能是 list/activate/close")


def register_tools(registry: ToolRegistry, controller: BrowserController) -> None:
    @registry.tool(
        name="browser_open",
        description="默认用小A独立 Chrome/Edge 打开网页，使用独立登录资料和下载目录；首次在 Windows 最小化启动，网页结构操作不占鼠标键盘。登录状态会保留；普通网页和自动化任务都优先用它，而不是 open_url。",
        parameters={"type": "object", "properties": {
            "url": {"type": "string"}, "new_tab": {"type": "boolean"},
        }, "required": ["url"]}, side_effect=True,
    )
    async def browser_open(url: str, new_tab: bool = False) -> dict[str, Any]:
        return await controller.open(url, new_tab)

    @registry.tool(
        name="browser_snapshot",
        description="读取当前网页的标题、地址、正文和带 B1/B2 引用的按钮、链接、输入框等结构。页面变化后应重新读取。",
        parameters={"type": "object", "properties": {
            "target_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 20, "maximum": 500},
        }},
    )
    async def browser_snapshot(target_id: str = "", limit: int = 220) -> dict[str, Any]:
        return await controller.snapshot(target_id, limit)

    @registry.tool(
        name="browser_click",
        description="按 browser_snapshot 返回的 ref 精确点击网页元素；没有 ref 时可按可见文字点击。",
        parameters={"type": "object", "properties": {
            "ref": {"type": "string"}, "text": {"type": "string"},
        }}, side_effect=True,
    )
    async def browser_click(ref: str = "", text: str = "") -> dict[str, Any]:
        return await controller.click(ref, text)

    @registry.tool(
        name="browser_type",
        description="向网页输入框输入文字，可按 ref 或标签/占位文字定位；submit=true 时再按回车。",
        parameters={"type": "object", "properties": {
            "value": {"type": "string"}, "ref": {"type": "string"}, "label": {"type": "string"},
            "submit": {"type": "boolean"},
        }, "required": ["value"]}, side_effect=True,
    )
    async def browser_type(value: str, ref: str = "", label: str = "", submit: bool = False) -> dict[str, Any]:
        return await controller.type_text(value, ref, label, submit)

    @registry.tool(
        name="browser_select",
        description="选择网页下拉框选项。先读取页面，再传下拉框 ref 和选项文字。",
        parameters={"type": "object", "properties": {
            "ref": {"type": "string"}, "option": {"type": "string"},
        }, "required": ["ref", "option"]}, side_effect=True,
    )
    async def browser_select(ref: str, option: str) -> dict[str, Any]:
        return await controller.select(option, ref)

    @registry.tool(
        name="browser_upload",
        description="向网页文件选择框上传本机文件。先读取页面取得文件框 ref。涉及隐私文件时系统仍会要求确认。",
        parameters={"type": "object", "properties": {
            "ref": {"type": "string"}, "path": {"type": "string"},
        }, "required": ["ref", "path"]}, side_effect=True,
    )
    async def browser_upload(ref: str, path: str) -> dict[str, Any]:
        return await controller.upload(path, ref)

    @registry.tool(
        name="browser_scroll",
        description="在当前网页内滚动，正数向下、负数向上。",
        parameters={"type": "object", "properties": {
            "amount": {"type": "integer", "minimum": -5000, "maximum": 5000},
        }}, side_effect=True,
    )
    async def browser_scroll(amount: int = 700) -> dict[str, Any]:
        return await controller.scroll(amount)

    @registry.tool(
        name="browser_wait",
        description="等待网页出现指定文字或地址包含指定内容，用于加载和提交后的可靠验证。",
        parameters={"type": "object", "properties": {
            "text": {"type": "string"}, "url_contains": {"type": "string"},
            "timeout": {"type": "number", "minimum": 0.2, "maximum": 30},
        }},
    )
    async def browser_wait(text: str = "", url_contains: str = "", timeout: float = 10.0) -> dict[str, Any]:
        return await controller.wait(text, url_contains, timeout)

    @registry.tool(
        name="browser_tabs",
        description="列出、激活或关闭小A专用浏览器标签页。先 action=list；激活/关闭时使用返回的 target_id。",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "activate", "close"]},
            "target_id": {"type": "string"},
        }}, side_effect=True,
    )
    async def browser_tabs(action: str = "list", target_id: str = "") -> dict[str, Any]:
        return await controller.tabs(action, target_id)

    @registry.tool(
        name="browser_screenshot",
        description="截取当前网页给小A查看。仅在网页结构不足、失败排查或最终验收时使用，不要逐步截图。",
        parameters={"type": "object", "properties": {}},
    )
    async def browser_screenshot() -> dict[str, Any]:
        return await controller.screenshot()
