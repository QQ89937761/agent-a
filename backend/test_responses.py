"""Responses conversion, SSE completion boundaries and configuration routing."""
from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import httpx

from backend.api import server
from backend.core.agent import Agent, _complete_tool_pairs
from backend.core.config import AgentConfig, Config, LLMConfig, LLMProviderConfig, apply_env_overrides, load_config
from backend.core.llm import LLMClient, LLMError, _normalize_responses_message, _responses_request
from backend.core.tools import Tool, ToolRegistry


def text_item(text="完成", phase="final_answer"):
    return {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed", "phase": phase,
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def call_item(call_id="call_1", arguments='{"value":1}'):
    return {"type": "function_call", "id": "fc_" + call_id, "call_id": call_id,
            "name": "query", "arguments": arguments, "status": "completed"}


def completed(*items):
    return {"id": "resp_1", "status": "completed", "output": list(items)}


def sse(*events):
    return "".join("event: " + event.get("type", "error") + "\ndata: " + json.dumps(event) + "\n\n"
                   for event in events)


def final_event(*items):
    return {"type": "response.completed", "response": completed(*items)}


class ResponsesTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler, protocol="responses"):
        client = LLMClient(LLMConfig(protocol=protocol, base_url="https://gateway.test/v1",
                                     api_key="private-key", chat_model="gpt-6.1-sol"))
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=client.cfg.base_url,
                                          headers={"Authorization": "Bearer private-key"})
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_chat_uses_responses_endpoint_and_stateless_reasoning(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=completed(text_item()))
        client = self.client(handler)
        result = await client.chat([{"role": "system", "content": "rule"}, {"role": "user", "content": "hi"}],
                                   reasoning_effort="high", temperature=0.3)
        body = json.loads(seen[0].content)
        self.assertEqual("/v1/responses", seen[0].url.path)
        self.assertEqual("Bearer private-key", seen[0].headers["authorization"])
        self.assertEqual("完成", result["content"])
        self.assertEqual({"effort": "high"}, body["reasoning"])
        self.assertEqual(["reasoning.encrypted_content"], body["include"])
        self.assertFalse(body["store"])
        self.assertFalse(body["stream"])
        self.assertNotIn("temperature", body)
        self.assertNotIn("messages", body)
        self.assertNotIn("previous_response_id", body)

    def test_request_preserves_system_order_images_and_flattens_non_strict_tools(self):
        messages = [{"role": "system", "content": "first"},
                    {"role": "user", "content": [{"type": "text", "text": "图片"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ==", "detail": "low"}}]},
                    {"role": "system", "content": "mid-loop reminder"}]
        tools = [{"type": "function", "function": {"name": "query", "description": "read",
                  "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}}}}]
        body = _responses_request(messages, tools, "test")
        self.assertEqual(["system", "user", "system"], [item["role"] for item in body["input"]])
        self.assertEqual("input_image", body["input"][1]["content"][1]["type"])
        self.assertEqual("low", body["input"][1]["content"][1]["detail"])
        self.assertEqual("query", body["tools"][0]["name"])
        self.assertFalse(body["tools"][0]["strict"])
        self.assertNotIn("function", body["tools"][0])

    def test_legacy_assistant_history_uses_output_content_types(self):
        body = _responses_request([
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
            {"role": "assistant", "content": [{"type": "refusal", "refusal": "cannot"}]},
            {"role": "user", "content": "continue"},
        ], None, "test")
        self.assertEqual(["input_text", "output_text", "refusal", "input_text"],
                         [item["content"][0]["type"] for item in body["input"]])
        self.assertEqual("cannot", body["input"][2]["content"][0]["refusal"])

    async def test_gateway_stream_errors_do_not_depend_on_type(self):
        cases = [
            'data: {"error":{"type":"invalid_request_error","message":"bad input"}}\n\n',
            'data: {"object":"error","type":"invalid_request_error","message":"bad input"}\n\n',
            'event: error\ndata: {"type":"invalid_request_error","message":"bad input"}\n\n',
            'event: error\ndata: {"code":"invalid_request_error","message":"bad input"}',
        ]
        for wire in cases:
            with self.subTest(wire=wire):
                count = 0
                def handler(request):
                    nonlocal count
                    count += 1
                    return httpx.Response(200, text=wire)
                client = self.client(handler)
                with patch("backend.core.llm.asyncio.sleep", new=AsyncMock()) as sleep:
                    with self.assertRaisesRegex(LLMError, "bad input") as result:
                        _ = [event async for event in client.chat_stream_tools([])]
                self.assertEqual("config", result.exception.kind)
                self.assertEqual(1, count)
                sleep.assert_not_awaited()

    def test_tool_pairing_uses_call_id_and_preserves_reasoning_and_phase(self):
        reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
        output = [reasoning, text_item("先查询", "commentary"), call_item()]
        message = _normalize_responses_message(completed(*output))
        self.assertEqual("call_1", message["tool_calls"][0]["id"])
        self.assertNotIn("arguments", message["_responses_output"][-1])
        messages = [message, {"role": "tool", "tool_call_id": "call_1", "content": "result"}]
        body = _responses_request(messages, None, "test")
        self.assertEqual(output, body["input"][:-1])
        self.assertEqual({"type": "function_call_output", "call_id": "call_1", "output": "result"}, body["input"][-1])
        repaired = _responses_request(_complete_tool_pairs([message]), None, "test")
        self.assertEqual("call_1", repaired["input"][-1]["call_id"])
        self.assertIn("结果未知", repaired["input"][-1]["output"])

    def test_old_chat_history_tool_calls_are_converted(self):
        message = {"role": "assistant", "content": None, "tool_calls": [{"id": "old_call", "type": "function",
                    "function": {"name": "query", "arguments": "{}"}}]}
        body = _responses_request([message, {"role": "tool", "tool_call_id": "old_call", "content": "ok"}], None, "test")
        self.assertEqual(["function_call", "function_call_output"], [item["type"] for item in body["input"]])

    async def test_stream_text_and_interleaved_tool_argument_fragments(self):
        one, two = call_item(), call_item("call_2", '{"value":2}')
        body = sse(
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "先查询"},
            {"type": "response.output_item.added", "output_index": 1, "item": {**one, "arguments": ""}},
            {"type": "response.output_item.added", "output_index": 2, "item": {**two, "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"value":'},
            {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '{"value":2}'},
            {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '1}'},
            final_event(text_item("先查询", "commentary"), one, two))
        client = self.client(lambda request: httpx.Response(200, text=body))
        events = [event async for event in client.chat_stream_tools([])]
        self.assertEqual(("delta", "先查询"), events[0])
        self.assertEqual(["call_1", "call_2"], [call["id"] for call in events[-1][1]["tool_calls"]])

    async def test_completed_without_output_uses_finished_items(self):
        body = sse({"type": "response.output_item.added", "output_index": 0, "item": {**call_item(), "arguments": ""}},
                   {"type": "response.function_call_arguments.done", "output_index": 0, "arguments": '{"value":1}'},
                   {"type": "response.completed", "response": {"status": "completed"}})
        client = self.client(lambda request: httpx.Response(200, text=body))
        result = [event async for event in client.chat_stream_tools([])]
        self.assertEqual('{"value":1}', result[-1][1]["tool_calls"][0]["function"]["arguments"])

    async def test_empty_stream_retries_and_emits_diagnostics(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, text=sse(final_event()) if len(requests) == 1 else sse(final_event(text_item())))
        client = self.client(handler)
        with patch("backend.core.llm.asyncio.sleep", new=AsyncMock()):
            events = [event async for event in client.chat_stream_tools([])]
        self.assertEqual("retry", events[0][0])
        self.assertEqual(2, events[0][1]["attempt"])
        self.assertTrue(events[0][1]["empty_stream"])
        self.assertEqual("完成", events[-1][1]["content"])

    async def test_partial_tool_stream_never_becomes_done_without_completion(self):
        body = sse({"type": "response.output_item.done", "output_index": 0, "item": call_item()})
        client = self.client(lambda request: httpx.Response(200, text=body))
        events = []
        with patch("backend.core.llm.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(LLMError):
                async for event in client.chat_stream_tools([]):
                    events.append(event)
        self.assertEqual(4, len(events))
        self.assertTrue(all(kind == "retry" for kind, _ in events))

    async def test_incomplete_and_malformed_tool_arguments_are_not_executed(self):
        for body in [sse({"type": "response.incomplete", "response": {"status": "incomplete",
                              "output": [call_item()], "incomplete_details": {"reason": "max_output_tokens"}}}),
                     sse(final_event(call_item(arguments='{"value":'))),
                     sse(final_event({**call_item(), "call_id": ""}))]:
            with self.subTest(body=body):
                client = self.client(lambda request: httpx.Response(200, text=body))
                with patch("backend.core.llm.asyncio.sleep", new=AsyncMock()) as sleep:
                    with self.assertRaises(LLMError):
                        _ = [event async for event in client.chat_stream_tools([])]
                    sleep.assert_not_awaited()

    async def test_partial_text_failure_is_reported_without_retry(self):
        body = sse({"type": "response.output_text.delta", "output_index": 0, "delta": "半截"},
                   {"type": "response.failed", "response": {"error": {"code": "server_error", "message": "busy"}}})
        client = self.client(lambda request: httpx.Response(200, text=body))
        events = []
        with patch("backend.core.llm.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaisesRegex(LLMError, "busy"):
                async for event in client.chat_stream_tools([]):
                    events.append(event)
            sleep.assert_not_awaited()
        self.assertEqual([("delta", "半截")], events)

    async def test_http_400_is_config_error_no_fallback_or_retry(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(400, json={"error": {"message": "bad request"}})
        client = self.client(handler)
        with patch("backend.core.llm.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(LLMError) as caught:
                _ = [event async for event in client.chat_stream_tools([])]
            self.assertEqual("config", caught.exception.kind)
            sleep.assert_not_awaited()
        self.assertEqual(["/v1/responses"], [request.url.path for request in seen])

    async def test_service_error_retries_and_discards_previous_tool_arguments(self):
        requests = []
        def handler(request):
            requests.append(request)
            body = (sse({"type": "response.output_item.added", "output_index": 0, "item": call_item()},
                        {"type": "error", "code": "server_error", "message": "busy"})
                    if len(requests) == 1 else sse(final_event(text_item())))
            return httpx.Response(200, text=body)
        client = self.client(handler)
        with patch("backend.core.llm.asyncio.sleep", new=AsyncMock()):
            events = [event async for event in client.chat_stream_tools([])]
        self.assertEqual("retry", events[0][0])
        self.assertNotIn("tool_calls", events[-1][1])

    async def test_chat_stream_and_vision_share_responses_conversion(self):
        requests = []
        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            if body.get("stream"):
                return httpx.Response(200, text=sse({"type": "response.output_text.delta", "delta": "完成"}, final_event(text_item())))
            return httpx.Response(200, json=completed(text_item()))
        client = self.client(handler)
        self.assertEqual(["完成"], [part async for part in client.chat_stream([], reasoning_effort="high")])
        self.assertEqual("完成", await client.vision("inspect", b"image"))
        self.assertEqual("high", requests[0]["reasoning"]["effort"])
        self.assertEqual("input_image", requests[1]["input"][0]["content"][1]["type"])

    async def test_agent_multi_tool_round_preserves_reasoning_and_executes_once(self):
        bodies, calls = [], []
        reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, text=sse(final_event(reasoning, call_item(), call_item("call_2", '{"value":2}')))
                                  if len(bodies) == 1 else sse(final_event(text_item())))
        registry = ToolRegistry()
        async def query(value):
            calls.append(value)
            return {"ok": True, "value": value}
        registry.register(Tool("query", "read", {"type": "object", "properties": {"value": {"type": "integer"}}}, query))
        events = [event async for event in Agent(self.client(handler), registry, AgentConfig()).run("查询")]
        self.assertEqual([1, 2], calls)
        self.assertEqual(2, len(bodies))
        native = bodies[1]["input"]
        self.assertIn(reasoning, native)
        self.assertEqual(["call_1", "call_2"], [item["call_id"] for item in native if item.get("type") == "function_call_output"])
        self.assertFalse(any(event.type == "error" for event in events))

    def test_checkpoint_redacts_arguments_without_corrupting_opaque_reasoning(self):
        opaque = "A" * 7000
        reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": opaque}
        message = _normalize_responses_message(completed(reasoning, call_item(arguments='{"api_key":"private-value"}')))
        saved = server._redact_resume_messages([message])
        self.assertNotIn("private-value", json.dumps(saved))
        replay = _responses_request(saved, None, "test")["input"]
        self.assertEqual(opaque, replay[0]["encrypted_content"])
        self.assertEqual("***", json.loads(replay[1]["arguments"])["api_key"])

    async def test_switching_back_to_chat_does_not_send_native_metadata(self):
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
        client = self.client(handler, protocol="openai")
        await client.chat([_normalize_responses_message(completed(text_item()))])
        self.assertNotIn("_responses_output", seen[0]["messages"][0])

    async def test_models_endpoint_and_backoff_cancellation(self):
        client = self.client(lambda request: httpx.Response(200, json={"data": [{"id": "model"}]}))
        self.assertEqual(["model"], await client.list_models())
        failing = self.client(lambda request: httpx.Response(503, text="busy"))
        with patch("backend.core.llm.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                _ = [event async for event in failing.chat_stream_tools([])]

    async def test_protocol_settings_round_trip_env_and_provider_selection(self):
        with patch.dict("os.environ", {"AGENT_LLM_PROTOCOL": "responses"}):
            self.assertEqual("responses", apply_env_overrides(Config()).llm.protocol)
        cfg = Config()
        cfg.llm.protocol = "responses"
        cfg.llm.providers = [LLMProviderConfig(id="second", protocol="responses", base_url="https://second.test/v1", models=["model"])]
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "CFG", cfg), \
                patch.object(server, "config_path", return_value=Path(directory) / "config.toml"):
            server._rewrite_config()
            loaded = load_config(Path(directory) / "config.toml")
            self.assertEqual("responses", loaded.llm.protocol)
            self.assertEqual("responses", loaded.llm.providers[0].protocol)
            client, owns, models, provider = server._llm_for_provider("second")
            self.assertEqual("responses", client.cfg.protocol)
            self.assertTrue(owns)
            await client.aclose()

    async def test_settings_api_saves_responses_for_default_and_custom_providers(self):
        cfg = Config()
        providers = [{"id": "default", "name": "Default", "protocol": "responses", "base_url": "https://gateway.test/v1",
                      "models": ["model"]},
                     {"id": "extra", "name": "Extra", "protocol": "responses", "base_url": "https://extra.test/v1",
                      "models": ["other"]}]
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "CFG", cfg), \
                patch.object(server, "LLM", None), patch.object(server, "_retire_llm", new=AsyncMock()), \
                patch.object(server.isolation_workspace, "cleanup"), \
                patch.object(server, "config_path", return_value=Path(directory) / "config.toml"):
            result = await server.api_save_settings(server.SettingsRequest(providers=providers))
            self.assertTrue(result["ok"], result)
            self.assertEqual(["responses", "responses"], [provider["protocol"] for provider in server._provider_settings()])
            loaded = load_config(Path(directory) / "config.toml")
            self.assertEqual("responses", loaded.llm.protocol)
            self.assertEqual("responses", loaded.llm.providers[0].protocol)
            await server.LLM.aclose()

    async def test_model_probe_and_discovery_keep_selected_protocol(self):
        seen = []
        class Client:
            def __init__(self, cfg):
                seen.append(cfg.protocol)
            async def chat(self, *args, **kwargs):
                return {"content": "OK"}
            async def list_models(self):
                return ["model"]
            async def aclose(self):
                pass
        with patch.object(server, "LLMClient", Client):
            test = await server.api_test_model(server.ModelTestRequest(base_url="https://test/v1", model="model", protocol="responses"))
            discovery = await server.api_discover_models(server.ModelDiscoveryRequest(base_url="https://test/v1", protocol="responses"))
        self.assertTrue(test["ok"])
        self.assertTrue(discovery["ok"])
        self.assertEqual(["responses", "responses"], seen)

    async def test_cloud_runtime_sync_retains_responses(self):
        cfg = Config()
        cfg.llm.protocol = "responses"
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        with patch.object(server, "CFG", cfg), patch("backend.core.cloud_deploy.sync_runtime_settings") as sync:
            self.assertTrue((await server.api_cloud_runtime(request))["ok"])
            self.assertEqual("responses", sync.call_args.args[1]["AGENT_LLM_PROTOCOL"])

    def test_checkpoint_disk_round_trip_preserves_protocol_state(self):
        opaque = "opaque" * 1500
        message = _normalize_responses_message(completed(
            {"type": "reasoning", "id": "rs", "summary": [], "encrypted_content": opaque}, call_item()))
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "_RESUME_STATES", {}), \
                patch.object(server, "_RESUME_PATH", Path(directory) / "resume.json"):
            state = server._save_resume_state("desktop", _complete_tool_pairs([message]), {}, "stopped")
            self.assertTrue(state["persisted"])
            reloaded = server._load_resume_states()["desktop"]["messages"]
            replay = _responses_request(reloaded, None, "test")["input"]
            self.assertEqual(opaque, replay[0]["encrypted_content"])
            self.assertEqual("call_1", replay[-1]["call_id"])

    async def test_refusal_is_displayed_and_reasoning_text_is_not(self):
        output = {"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "不能处理"}]}
        client = self.client(lambda request: httpx.Response(200, text=sse(
            {"type": "response.reasoning_summary_text.delta", "delta": "hidden-reasoning"},
            {"type": "response.refusal.delta", "delta": "不能处理"}, final_event(output))))
        events = [event async for event in client.chat_stream_tools([])]
        self.assertEqual(("delta", "不能处理"), events[0])
        self.assertEqual("不能处理", events[-1][1]["content"])
        self.assertNotIn("hidden-reasoning", json.dumps(events))

    async def test_interjection_after_text_reply_preserves_native_phase(self):
        pending, bodies = [], []
        reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
        def take():
            result = list(pending)
            pending.clear()
            return result
        def handler(request):
            bodies.append(json.loads(request.content))
            if len(bodies) == 1:
                pending.append("继续核对")
                return httpx.Response(200, text=sse(final_event(reasoning, text_item())))
            return httpx.Response(200, text=sse(final_event(text_item("核对完成"))))
        agent = Agent(self.client(handler), ToolRegistry(), AgentConfig(), interjection_fn=take)
        events = [event async for event in agent.run("查询")]
        self.assertEqual(2, len(bodies))
        self.assertIn(reasoning, bodies[1]["input"])
        self.assertTrue(any(item.get("phase") == "final_answer" for item in bodies[1]["input"]))
        self.assertFalse(any(event.type == "error" for event in events))

    async def test_real_http_sse_split_across_network_chunks(self):
        body = sse({"type": "response.output_text.delta", "output_index": 0, "delta": "中文"},
                   final_event(text_item("中文"))).encode("utf-8")
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                seen.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                for index in range(0, len(body), 7):
                    self.wfile.write(body[index:index + 7])
                    self.wfile.flush()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        client = LLMClient(LLMConfig(protocol="responses", base_url=f"http://127.0.0.1:{httpd.server_port}/v1",
                                     api_key="local-test-key", timeout=5))
        try:
            events = [event async for event in client.chat_stream_tools([{"role": "user", "content": "hi"}])]
            self.assertEqual(("delta", "中文"), events[0])
            self.assertEqual("中文", events[-1][1]["content"])
            self.assertEqual("/v1/responses", seen[0][0])
            self.assertTrue(seen[0][1]["stream"])
        finally:
            await client.aclose()
            await asyncio.to_thread(httpd.shutdown)
            httpd.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
