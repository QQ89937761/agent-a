import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from backend.core.config import LLMConfig
from backend.core.llm import LLMClient, _anthropic_request, _normalize_anthropic_message


class AnthropicAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_native_endpoint_headers_and_normalized_response(self):
        seen = {}
        def handler(request):
            seen["request"] = request
            return httpx.Response(200, json={"content": [{"type": "text", "text": "hello"}]})
        client = LLMClient(LLMConfig(protocol="anthropic", base_url="https://api.anthropic.com", api_key="secret", chat_model="claude-test"))
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=client.cfg.base_url,
            headers={"x-api-key": client.cfg.api_key, "anthropic-version": "2023-06-01"})
        try:
            result = await client.chat([{"role": "system", "content": "rules"}, {"role": "user", "content": "hi"}])
            request = seen["request"]
            body = json.loads(request.content)
            self.assertEqual(request.url.path, "/v1/messages")
            self.assertEqual(request.headers["x-api-key"], "secret")
            self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
            self.assertEqual(body["system"][0]["text"], "rules")
            self.assertEqual(body["max_tokens"], 4096)
            self.assertEqual(result["content"], "hello")
        finally:
            await client.aclose()

    async def test_list_models_uses_protocol_path_and_normalizes_catalog(self):
        seen = {}

        def handler(request):
            seen["request"] = request
            return httpx.Response(200, json={"data": [
                {"id": "model-b"}, {"id": "model-a"}, {"id": "model-b"},
            ]})

        client = LLMClient(LLMConfig(
            protocol="anthropic", base_url="https://api.anthropic.com", api_key="secret",
        ))
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=client.cfg.base_url,
        )
        try:
            self.assertEqual(["model-b", "model-a"], await client.list_models())
            self.assertEqual("/v1/models", seen["request"].url.path)
        finally:
            await client.aclose()

    async def test_stream_retries_retriable_status_before_output(self):
        attempts = 0
        events = [
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": "ok"}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        stream_body = "".join(f"event: {kind}\ndata: {json.dumps(data)}\n\n" for kind, data in events)

        def handler(_request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, text="temporarily unavailable")
            return httpx.Response(200, text=stream_body, headers={"content-type": "text/event-stream"})

        client = LLMClient(LLMConfig(protocol="anthropic", base_url="https://api.anthropic.com",
                                     api_key="secret", chat_model="claude-test"))
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=client.cfg.base_url)
        try:
            with patch("backend.core.llm._retry_delay", return_value=0):
                result = [item async for item in client.chat_stream_tools([{"role": "user", "content": "hi"}])]
            self.assertEqual(attempts, 2)
            self.assertEqual(result[0][0], "retry")
            self.assertEqual(result[0][1]["attempt"], 2)
            self.assertEqual(result.pop(0)[1]["status_code"], 503)
            self.assertEqual(result, [("delta", "ok"), ("done", {"role": "assistant", "content": "ok"})])
        finally:
            await client.aclose()

    async def test_tools_and_stream(self):
        request = _anthropic_request([
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "lookup", "arguments": json.dumps({"q": "x"})}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "found"},
        ], [{"type": "function", "function": {"name": "lookup", "description": "look", "parameters": {"type": "object"}}}],
            "claude-test", 0, True)
        self.assertEqual(request["tools"][0]["input_schema"]["type"], "object")
        self.assertEqual(request["messages"][0]["content"][0]["type"], "tool_use")
        self.assertEqual(request["messages"][1]["content"][0]["type"], "tool_result")
        normalized = _normalize_anthropic_message({"content": [{"type": "tool_use", "id": "t1", "name": "lookup", "input": {"q": "x"}}]})
        self.assertEqual(json.loads(normalized["tool_calls"][0]["function"]["arguments"]), {"q": "x"})

        events = [
            ("message_start", {"type": "message_start", "message": {}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_stop", {"type": "message_stop"}),
        ]
        body = "".join(f"event: {kind}\ndata: {json.dumps(data)}\n\n" for kind, data in events).encode()
        def handler(_request):
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
        client = LLMClient(LLMConfig(protocol="anthropic", base_url="https://api.anthropic.com/v1", api_key="secret"))
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=client.cfg.base_url,
            headers={"x-api-key": "secret", "anthropic-version": "2023-06-01"})
        try:
            result = [item async for item in client.chat_stream_tools([{"role": "user", "content": "hi"}])]
            self.assertEqual(result, [("delta", "hi"), ("done", {"role": "assistant", "content": "hi"})])
        finally:
            await client.aclose()


if __name__ == "__main__":
    unittest.main()
