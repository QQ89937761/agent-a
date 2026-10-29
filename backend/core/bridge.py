"""云↔电脑连接桥。

场景:小A 拆成两半——
  hub(云端常开)负责接待和记忆;worker(你电脑)负责操作软件。
家庭宽带的电脑没有固定公网地址,所以由 **worker 主动拨号连 hub**(WebSocket),
hub 需要操作软件时,顺着这条已建立的连接把任务下发给 worker。

协议(JSON over WebSocket):
  worker 连上后先发:  {"type":"hello", "token": "..."}
  设置页只测试握手:    {"type":"hello", "token": "...", "probe": true}
  hub 校验通过回:      {"type":"welcome"}          校验失败回 {"type":"deny"} 并断开
  hub 派活:            {"type":"task", "id": "..", "tool": "launch_app", "args": {...}}
  worker 回结果:       {"type":"result", "id": "..", "ok": true, "result": "..."}
  两端心跳:            {"type":"ping"} / {"type":"pong"}

本文件只实现 worker 侧(拨号+执行+重连);hub 侧的接入点在 server.py 的 WebSocket 路由。
"""
from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import hashlib
import ssl
import secrets
import time
from typing import Any, Callable, Awaitable

# 执行一个工具调用,返回结果字符串。由 server 注入(用本机 ToolRegistry)。
ToolRunner = Callable[[str, dict[str, Any]], Awaitable[Any]]
ToolProvider = Callable[[], list[dict[str, Any]]]
ToolReceiver = Callable[[list[dict[str, Any]]], None]


def direct_websocket_options(connect, **options: Any) -> dict[str, Any]:
    """Build websocket options without inheriting the machine's HTTP proxy.

    websockets 15 auto-detects the Windows system proxy; the hub endpoint is a
    pinned self-signed TLS connection and must be reached directly. Older
    supported websockets releases don't expose the ``proxy`` keyword, so keep
    the option conditional for compatibility.
    """
    try:
        if "proxy" in inspect.signature(connect).parameters:
            options["proxy"] = None
    except (TypeError, ValueError):
        pass
    return options


def pinned_ssl_context() -> ssl.SSLContext:
    """为自签证书建立 TLS 通道；握手后必须再做精确指纹校验。"""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # 自签证书不在系统 CA 中；安全性由 verify_peer_fingerprint 的硬匹配提供。
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def hub_ssl_context(fingerprint: str = "") -> ssl.SSLContext:
    """New connections validate CA, SAN (including IP) and expiry on every handshake."""
    return pinned_ssl_context() if fingerprint else ssl.create_default_context()


def verify_peer_fingerprint(ws: Any, expected: str) -> None:
    """拒绝不是连接串中那张证书的 TLS 对端。"""
    ssl_object = getattr(getattr(ws, "transport", None), "get_extra_info", lambda *_: None)("ssl_object")
    certificate = ssl_object.getpeercert(binary_form=True) if ssl_object else None
    actual = hashlib.sha256(certificate or b"").hexdigest().lower()
    wanted = str(expected).replace(":", "").strip().lower()
    if not hmac.compare_digest(actual, wanted):
        raise ssl.SSLError("云端证书指纹不匹配")


class WorkerBridge:
    """跑在电脑(worker)上:拨号连 hub,听 hub 派活,执行后回传结果,断线自动重连。"""

    def __init__(self, hub_url: str, token: str, worker_id: str, run_tool: ToolRunner,
                 get_tools: ToolProvider | None = None, tls_fingerprint: str = "",
                 on_notice: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
                 on_model_order: Callable[[list[dict]], Awaitable[None]] | None = None):
        self.hub_url = hub_url
        self.token = token
        self.worker_id = worker_id
        self.run_tool = run_tool
        self.get_tools = get_tools
        self.tls_fingerprint = tls_fingerprint
        self._on_notice = on_notice
        self._on_model_order = on_model_order
        self._stop = False
        self._ws = None
        self._desktop_stop = False
        self._drained = []
        self._epoch = ""

    async def run_forever(self, retry_base: float = 2.0, retry_max: float = 30.0) -> None:
        """一直保持与 hub 的连接;断了就退避重连。"""
        import websockets

        attempt = 0
        while not self._stop:
            try:
                connect_kwargs = direct_websocket_options(
                    websockets.connect, ping_interval=20, ping_timeout=20,
                )
                connect_kwargs["ssl"] = hub_ssl_context(self.tls_fingerprint)
                # 20 MB 原文件经 Base64/JSON 后会膨胀到约 27 MB，给协议头和元数据留余量。
                connect_kwargs["max_size"] = 32 * 1024 * 1024
                async with websockets.connect(self.hub_url, **connect_kwargs) as ws:
                    if self.tls_fingerprint:
                        verify_peer_fingerprint(ws, self.tls_fingerprint)
                    tools = self._tools()
                    media_tools = sorted(self._tool_names(tools) & {
                        "send_file_to_chat", "send_image_to_chat",
                    })
                    print(
                        "[bridge] 上报本机工具清单: "
                        f"{len(tools)} 个,媒体工具={','.join(media_tools) or '无'}",
                        flush=True,
                    )
                    await ws.send(json.dumps({
                        "type": "hello", "token": self.token, "worker_id": self.worker_id,
                        "tools": tools, "model_order_sync": self._on_model_order is not None,
                        "desktop_stop": 1, "desktop_drained": self._drained,
                    }))
                    reply = json.loads(await ws.recv())
                    if reply.get("type") != "welcome":
                        print(f"[bridge] hub 拒绝连接: {reply}")
                        await asyncio.sleep(retry_max)
                        continue
                    if self._on_model_order is not None and "model_order" in reply:
                        await self._receive_model_order(reply["model_order"])
                    self._desktop_stop = reply.get("desktop_stop") == 1
                    self._epoch = str(reply.get("desktop_epoch") or "")
                    self._ws = ws
                    print("[bridge] 已连上 hub,等待派活")
                    attempt = 0  # 连上就重置退避
                    await self._serve(ws)
            except Exception as e:
                if self._stop:
                    break
                attempt += 1
                delay = min(retry_base * attempt, retry_max)
                print(f"[bridge] 与 hub 断开({type(e).__name__}: {e}),{delay:.0f}s 后重连")
                await asyncio.sleep(delay)
            finally:
                self._ws = None

    async def _receive_model_order(self, providers) -> None:
        if self._on_model_order is None:
            return
        try:
            await self._on_model_order(providers)
        except Exception as exc:
            # A failed local disk write must not take desktop tool execution offline.
            print(f"[bridge] 模型顺序同步失败({type(exc).__name__})，保留本机设置；请检查磁盘后重连")

    async def _serve(self, ws) -> None:
        """收 hub 的任务,执行,回结果。"""
        async def heartbeat() -> None:
            ticks = 0
            while True:
                await asyncio.sleep(15)
                ticks += 1
                payload = {"type": "ping", "ts": int(time.time())}
                if ticks % 4 == 0:
                    payload = {"type": "capabilities", "tools": self._tools(), "ts": int(time.time())}
                await ws.send(json.dumps(payload))

        heartbeat_task = asyncio.create_task(heartbeat())
        tasks = {}
        stops = {}
        seen = set()
        desktop_ids = set()
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                t = msg.get("type")
                if t == "task":
                    tid = msg.get("id")
                    if not isinstance(tid, str) or not 1 <= len(tid) <= 160:
                        continue
                    if self._desktop_stop and not tid.startswith(self._epoch + "-t"):
                        continue  # A delayed task from an old connection cannot run again.
                    if tid in seen:
                        # An acknowledgement is never a terminal execution result.
                        await ws.send(json.dumps({"type": "duplicate", "id": tid}))
                        continue
                    if len(seen) >= 4096:
                        await ws.close()  # Bound dedup memory; the next connection has a new epoch.
                        break
                    seen.add(tid)
                    event = asyncio.Event() if msg.get("tool") == "desktop_execute" else None
                    if event is not None:
                        desktop_ids.add(tid)
                        stops[tid] = event
                    task = asyncio.create_task(self._do_task(ws, msg, event))
                    tasks[tid] = task
                    def finished(done, task_id=tid):
                        tasks.pop(task_id, None)
                        stops.pop(task_id, None)
                        if not done.cancelled():
                            done.exception()
                    task.add_done_callback(finished)
                elif t == "stop" and self._desktop_stop:
                    task_id = msg.get("id")
                    event = stops.get(task_id) if isinstance(task_id, str) else None
                    if event is not None:
                        event.set()
                        await ws.send(json.dumps({"type": "stopping", "id": msg["id"]}))
                elif t == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                elif t == "pong":
                    pass
                elif t == "model_order":
                    await self._receive_model_order(msg.get("providers"))
                elif t == "notice":
                    # 收到 hub 推来的提醒:只在本机展示,不执行工具、不回 result
                    if self._on_notice is not None:
                        try:
                            await self._on_notice(msg.get("payload") or {})
                        except Exception as e:
                            print(f"[bridge] 展示提醒失败: {e}")
        finally:
            heartbeat_task.cancel()
            pending = list(tasks.items())
            for event in stops.values():
                event.set()
            for tid, task in pending:
                if tid not in stops:
                    task.cancel()
            # Drain current desktop input before reconnecting; never replay its task.
            await asyncio.gather(heartbeat_task, *(task for _, task in pending), return_exceptions=True)
            self._drained = list(desktop_ids)

    async def _do_task(self, ws, msg: dict, stop_event=None) -> None:
        tid = msg.get("id")
        tool = msg.get("tool", "")
        args = msg.get("args", {}) or {}
        from backend.modules.computer.desktop_runtime import interrupt_check, request_stop
        check_token = interrupt_check.set(stop_event.is_set) if stop_event is not None else None
        stop_token = request_stop.set(stop_event.set) if stop_event is not None else None
        try:
            if stop_event is not None and stop_event.is_set():
                result = {"ok": False, "status": "interrupted", "completed": 0, "steps": []}
            elif (tool == "desktop_execute" and not self._desktop_stop
                    and len(args.get("actions", [])) > 1):
                result = {"ok": False, "status": "remote_single_step_required", "completed": 0}
            else:
                result = await self.run_tool(tool, args)
            ok = True
        except asyncio.CancelledError:
            result = {"ok": False, "status": "action_unconfirmed", "completed": None,
                      "note": "执行被取消，当前动作结果不确定；重新观察，不要重放。"}
            ok = False
        except Exception as e:
            result = f"执行失败: {e}"
            ok = False
        finally:
            if check_token is not None:
                interrupt_check.reset(check_token)
                request_stop.reset(stop_token)
        try:
            await ws.send(json.dumps(
                {"type": "result", "id": tid, "ok": ok, "result": result,
                 "tools": self._tools()}, ensure_ascii=False))
        except Exception as e:
            print(f"[bridge] 回传结果失败: {e}")

    def stop(self) -> None:
        self._stop = True

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def _tools(self) -> list[dict[str, Any]]:
        try:
            return self.get_tools() if self.get_tools is not None else []
        except Exception:
            return []

    @staticmethod
    def _tool_names(tools: list[dict[str, Any]]) -> set[str]:
        names: set[str] = set()
        for item in tools:
            schema = item.get("schema") if isinstance(item, dict) else None
            function = schema.get("function") if isinstance(schema, dict) else None
            name = function.get("name") if isinstance(function, dict) else ""
            if name:
                names.add(str(name))
        return names

    def status(self) -> dict[str, Any]:
        """Return safe local diagnostics without exposing credentials."""
        tools = self._tools()
        names = self._tool_names(tools)
        return {
            "connected": self.connected,
            "worker_id": self.worker_id,
            "tool_count": len(tools),
            "media_tools": sorted(names & {
                "send_file_to_chat", "send_image_to_chat",
            }),
        }


class HubBridge:
    """跑在云端(hub)上:管理已连上的 worker,把操作类任务派下去并等结果回来。

    单用户场景只需管一个 worker(你自己的电脑)。多台可后续扩展成字典。
    """

    def __init__(
        self, token: str, on_tools: ToolReceiver | None = None,
        on_worker_offline: Callable[[], None] | None = None,
        get_model_order: Callable[[], list[dict]] | None = None,
    ):
        self.token = token
        self._ws = None                       # 当前在线的 worker 连接
        self._worker_id = ""
        self._connected_at = 0.0
        self._last_seen = 0.0
        self._pending: dict[str, asyncio.Future] = {}  # 任务 id -> 等结果的 future
        self._seq = 0
        self._send_lock = asyncio.Lock()
        self._on_tools = on_tools
        self._on_worker_offline = on_worker_offline
        self._get_model_order = get_model_order
        self._model_order_sync = False
        self._desktop_stop = False
        self._epoch = secrets.token_hex(8)
        self._unsettled: set[str] = set()
        self._desktop_tasks: set[str] = set()
        self._tool_count = 0
        self._cached_tool_count = 0
        self._tools_fingerprint = ""

    @property
    def worker_online(self) -> bool:
        return self._ws is not None

    @property
    def supports_desktop_stop(self) -> bool:
        return self._ws is not None and self._desktop_stop

    def status(self) -> dict[str, Any]:
        return {
            "desktop_stop": self.supports_desktop_stop,
            "unsettled_tasks": len(self._unsettled),
            "online": self.worker_online,
            "worker_id": self._worker_id,
            "connected_at": int(self._connected_at) if self._connected_at else None,
            "last_seen": int(self._last_seen) if self._last_seen else None,
            "pending_tasks": len(self._pending),
            "tool_count": self._tool_count,
            "cached_tool_count": self._cached_tool_count,
        }

    def _fail_pending(self, reason: str) -> None:
        self._unsettled.update(self._desktop_tasks)
        self._desktop_tasks.clear()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result({"ok": False, "result": reason, "offline": True})
        self._pending.clear()

    async def handle_worker(self, ws) -> None:
        """worker 拨进来时,server 的 WebSocket 路由调用这里。校验口令→接管收发。"""
        # 第一条必须是 hello + 正确 token
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
        except Exception:
            await ws.close(code=4003)
            return
        if not isinstance(first, dict):
            await ws.close(code=4003)
            return
        supplied = str(first.get("token") or "")
        if (first.get("type") != "hello" or not self.token
                or not hmac.compare_digest(supplied, self.token)):
            await ws.send_text(json.dumps({"type": "deny"}))
            await ws.close(code=4003)
            return
        if first.get("probe") is True:
            await ws.send_text(json.dumps({"type": "welcome"}))
            await ws.close()
            return
        worker_id = str(first.get("worker_id") or "unknown-worker")[:100]
        old_ws = self._ws
        if old_ws is not None and old_ws is not ws:
            self._ws = None
            self._fail_pending("worker 已被新连接替换")
            try:
                await old_ws.close(code=4002)
            except Exception:
                pass
        self._epoch = secrets.token_hex(8)
        welcome = {"type": "welcome"}
        self._desktop_stop = first.get("desktop_stop") == 1
        if self._desktop_stop:
            welcome["desktop_stop"] = 1
            welcome["desktop_epoch"] = self._epoch
        self._model_order_sync = first.get("model_order_sync") is True
        if self._model_order_sync and self._get_model_order is not None:
            welcome["model_order"] = self._get_model_order()
        await ws.send_text(json.dumps(welcome))
        self._ws = ws
        # Clear only exact task IDs the authenticated worker has finished draining.
        drained = first.get("desktop_drained")
        if self._desktop_stop and isinstance(drained, list):
            self._unsettled.difference_update(tid for tid in drained[:4096] if isinstance(tid, str))
        self._worker_id = worker_id
        self._connected_at = self._last_seen = time.time()
        self._receive_tools(first.get("tools"))
        print(f"[hub] worker 已上线: {worker_id}")
        try:
            while True:
                raw = await ws.receive_text()
                self._last_seen = time.time()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if self._ws is not ws:
                    break
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "result":
                    if not isinstance(msg.get("id"), str):
                        continue
                    self._unsettled.discard(msg.get("id"))
                    self._desktop_tasks.discard(msg.get("id"))
                    self._receive_tools(msg.get("tools"))
                    fut = self._pending.pop(msg.get("id"), None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif msg.get("type") == "pong":
                    pass
                elif msg.get("type") == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
                elif msg.get("type") == "capabilities":
                    self._receive_tools(msg.get("tools"))
        except Exception as e:
            print(f"[hub] worker 掉线: {type(e).__name__}")
        finally:
            if self._ws is ws:
                self._ws = None
                self._desktop_stop = False
                self._tool_count = 0
                self._tools_fingerprint = ""
                # 只清理由当前连接派出的任务。旧连接退出不能影响替代它的新连接。
                self._fail_pending("worker 掉线")
                if self._on_worker_offline is not None:
                    self._on_worker_offline()

    def _receive_tools(self, tools: Any) -> None:
        if not isinstance(tools, list):
            return
        tools = tools[:200]
        fingerprint = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if fingerprint == self._tools_fingerprint:
            return
        self._tools_fingerprint = fingerprint
        self._tool_count = len(tools)
        self._cached_tool_count = len(tools)
        if self._on_tools is not None:
            self._on_tools(tools)

    def set_cached_tool_count(self, count: int) -> None:
        self._cached_tool_count = max(0, int(count))

    async def dispatch(self, tool: str, args: dict, timeout: float = 120) -> dict:
        """把一个工具调用派给 worker,等它回结果。worker 不在线则立即返回离线。"""
        if self._ws is None:
            return {"ok": False, "result": "电脑现在不在线", "offline": True}
        from backend.modules.computer.desktop_runtime import interrupt_check
        desktop = tool == "desktop_execute"
        check = interrupt_check.get() if desktop else None
        if self._unsettled or self._desktop_tasks:
            return {"ok": False, "result": {"ok": False, "status": "remote_stop_unconfirmed",
                    "completed": None, "note": "上一远程任务尚未确认停止，暂不派发新操作；不要重复执行。"}}
        if check and check():
            return {"ok": True, "result": {"ok": False, "status": "interrupted", "completed": 0}}
        self._seq += 1
        tid = f"{self._epoch}-t{self._seq}"
        ws, can_stop = self._ws, self.supports_desktop_stop
        fut = asyncio.get_running_loop().create_future()
        self._pending[tid] = fut
        if desktop:
            self._desktop_tasks.add(tid)
        stopped = False

        async def stop():
            nonlocal stopped
            if desktop and not fut.done():
                self._unsettled.add(tid)
            if stopped or not (desktop and can_stop):
                return
            stopped = True
            try:
                async with asyncio.timeout(2):
                    async with self._send_lock:
                        await ws.send_text(json.dumps({"type": "stop", "id": tid}))
            except Exception:
                pass  # Unknown result stays quarantined until terminal result or a safe reconnect.

        try:
            async with asyncio.timeout(min(timeout, 5)):
                async with self._send_lock:
                    if ws is not self._ws:
                        self._pending.pop(tid, None)
                        self._desktop_tasks.discard(tid)
                        return {"ok": False, "result": "worker连接已变化，未派发", "offline": True}
                    await ws.send_text(json.dumps(
                        {"type": "task", "id": tid, "tool": tool, "args": args}, ensure_ascii=False))
        except asyncio.CancelledError:
            await stop()
            if not desktop:
                self._pending.pop(tid, None)
            raise
        except Exception as e:
            self._pending.pop(tid, None)
            await stop()
            return {"ok": False, "result": f"派活失败，执行情况未知: {e}", "offline": True}
        deadline = time.monotonic() + timeout
        try:
            while not fut.done():
                if not stopped and check and check():
                    await stop()
                    deadline = min(deadline, time.monotonic() + 5)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await stop()
                    if not desktop:
                        self._pending.pop(tid, None)
                    return {"ok": False, "result": {"ok": False,
                            "status": "remote_stop_unconfirmed" if desktop else "remote_timeout",
                            "completed": None, "note": "执行超时；已请求停止后续步骤，当前结果尚未确认，不要重放。"}}
                await asyncio.wait({fut}, timeout=min(.05, remaining))
            return fut.result()
        except asyncio.CancelledError:
            await stop()
            if not desktop:
                self._pending.pop(tid, None)
            raise

    async def sync_model_order(self) -> bool:
        """Only upgraded, authenticated workers receive non-secret model ordering."""
        if self._ws is None or not self._model_order_sync or self._get_model_order is None:
            return False
        async with self._send_lock:
            await self._ws.send_text(json.dumps({
                "type": "model_order", "providers": self._get_model_order(),
            }, ensure_ascii=False))
        return True

    async def notify(self, payload: dict[str, Any]) -> bool:
        """fire-and-forget 推一条提醒给 worker;worker 离线则静默跳过。

        复用 dispatch 的 _send_lock,避免和派活消息交错写坏同一个 WebSocket。
        """
        if self._ws is None:
            return False
        try:
            async with self._send_lock:
                await self._ws.send_text(json.dumps(
                    {"type": "notice", "payload": payload}, ensure_ascii=False))
            return True
        except Exception as e:
            print(f"[hub] 推送提醒失败: {e}")
            return False
