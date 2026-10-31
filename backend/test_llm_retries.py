from __future__ import annotations

import asyncio
import json
import unittest

import httpx
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.core.config import LLMConfig
from backend.core.llm import LLMClient, LLMError


class _FakeHttpClient:
    def __init__(self, statuses: list[int]):
        self.statuses = list(statuses)
        self.calls = 0

    async def post(self, _path: str, json: dict):
        del json
        status = self.statuses[min(self.calls, len(self.statuses) - 1)]
        self.calls += 1
        if status == 200:
            return SimpleNamespace(
                status_code=200, text="ok",
                json=lambda: {"choices": [{"message": {"role": "assistant", "content": "完成"}}]},
            )
        return SimpleNamespace(status_code=status, text="temporary failure")


class _FakeStreamResponse:
    def __init__(self, lines: list[str], status_code: int = 200, body: bytes = b""):
        self.lines = lines
        self.status_code = status_code
        self.body = body

    async def aread(self):
        return self.body

    async def aiter_lines(self):
        for line in self.lines:
            yield line


class _FakeStreamContext:
    def __init__(self, response: _FakeStreamResponse):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return None


class _FakeStreamHttpClient:
    def __init__(self, responses: list[_FakeStreamResponse]):
        self.responses = list(responses)
        self.calls = 0

    def stream(self, _method: str, _path: str, json: dict):
        del json
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return _FakeStreamContext(response)


def _sse_delta(delta: dict) -> str:
    return "data: " + json.dumps({"choices": [{"delta": delta}]})


class LLMRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_stream_does_not_execute_partial_tool_arguments(self):
        client = LLMClient.__new__(LLMClient)
        client.cfg = LLMConfig(chat_model='test-model')
        client._client = _FakeStreamHttpClient([
            _FakeStreamResponse([
                _sse_delta({'tool_calls': [{'index': 0, 'function': {'name': 'launch_app', 'arguments': '{'}}]}),
                'data: ' + json.dumps({'error': {'code': 'overloaded', 'message': 'busy'}}),
            ]),
            _FakeStreamResponse([_sse_delta({'content': '完成'}), 'data: [DONE]']),
        ])
        with patch('backend.core.llm.asyncio.sleep', new=AsyncMock()):
            result = [event async for event in client.chat_stream_tools([])]
        self.assertEqual('完成', result[-1][1]['content'])
        self.assertNotIn('tool_calls', result[-1][1])
        self.assertEqual(2, client._client.calls)

    async def test_gateway_error_inside_http_200_is_reported_and_not_empty_retry(self):
        client = LLMClient.__new__(LLMClient)
        client.cfg = LLMConfig(chat_model='test-model')
        client._client = _FakeStreamHttpClient([_FakeStreamResponse([
            'data: ' + json.dumps({'error': {'code': 'INVALID_MODEL_ID', 'message': 'Invalid model'}}),
        ])])
        with patch('backend.core.llm.asyncio.sleep', new=AsyncMock()) as sleep:
            with self.assertRaises(LLMError) as caught:
                _ = [event async for event in client.chat_stream_tools([])]
        self.assertEqual('config', caught.exception.kind)
        self.assertIn('INVALID_MODEL_ID', str(caught.exception))
        self.assertEqual(1, client._client.calls)
        sleep.assert_not_awaited()

    async def test_http_200_service_error_keeps_reason_after_retries(self):
        client = LLMClient.__new__(LLMClient)
        client.cfg = LLMConfig(chat_model='test-model')
        client._client = _FakeStreamHttpClient([_FakeStreamResponse([
            'data: ' + json.dumps({'error': {'code': 'overloaded', 'message': 'Upstream temporarily overloaded'}}),
        ])])
        with patch('backend.core.llm.asyncio.sleep', new=AsyncMock()):
            with self.assertRaisesRegex(LLMError, 'Upstream temporarily overloaded'):
                _ = [event async for event in client.chat_stream_tools([])]
        self.assertEqual(5, client._client.calls)

    async def test_non_object_stream_chunk_and_usage_tail_do_not_break_valid_reply(self):
        client = LLMClient.__new__(LLMClient)
        client.cfg = LLMConfig(chat_model='test-model')
        client._client = _FakeStreamHttpClient([_FakeStreamResponse([
            'data: null', _sse_delta({'content': '完成'}),
            'data: ' + json.dumps({'choices': [], 'usage': {'total_tokens': 1}}), 'data: [DONE]',
        ])])
        result = [event async for event in client.chat_stream_tools([])]
        self.assertEqual('完成', result[-1][1]['content'])
        self.assertEqual(1, client._client.calls)

    async def test_transient_gateway_failure_retries_up_to_five_attempts(self):
        client = LLMClient.__new__(LLMClient)
        client.cfg = LLMConfig(chat_model="test-model")
        client._client = _FakeHttpClient([503, 503, 503, 503, 200])
        with patch("backend.core.llm.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await client.chat([{"role": "user", "content": "test"}])
        self.assertEqual("完成", result["content"])
        self.assertEqual(5, client._client.calls)
        self.assertEqual([3.0, 6.0, 12.0, 20.0], [call.args[0] for call in sleep.await_args_list])

    async def test_fifth_transient_failure_is_reported(self):
        client = LLMClient.__new__(LLMClient)
        client.cfg = LLMConfig(chat_model="test-model")
        client._client = _FakeHttpClient([502])
        with patch("backend.core.llm.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(LLMError, "连续 5 次失败"):
                await client.chat([{"role": "user", "content": "test"}])
        self.assertEqual(5, client._client.calls)

    async def test_empty_stream_retries_then_returns_text(self):
        client = LLMClient.__new__(LLMClient)
        client.cfg = LLMConfig(chat_model="test-model")
        client._client = _FakeStreamHttpClient([
            _FakeStreamResponse(["data: not-json", 'data: {"choices": []}', "data: [DONE]"]),
            _FakeStreamResponse([_sse_delta({"content": "完成"}), "data: [DONE]"]),
        ])
        with patch("backend.core.llm.asyncio.sleep", new=AsyncMock()) as sleep:
            events = [
                event async for event in client.chat_stream_tools(
                    [{"role": "user", "content": "test"}]
                )
            ]
        sleep.assert_awaited_once_with(3.0)
        retry = events.pop(0)
        self.assertEqual("retry", retry[0])
        self.assertEqual(2, retry[1]["attempt"])
        self.assertEqual(5, retry[1]["max_attempts"])
        self.assertEqual(200, retry[1]["status_code"])
        self.assertTrue(retry[1]["empty_stream"])
        self.assertGreaterEqual(retry[1]["elapsed_ms"], 0)
        self.assertIn("2/5", retry[1]["text"])
        self.assertEqual([("delta", "完成"), ("done", {
            "role": "assistant", "content": "完成",
        })], events)
        self.assertEqual(2, client._client.calls)

    async def test_stream_attempt_logs_are_complete_and_exclude_gateway_body(self):
        client = LLMClient.__new__(LLMClient)
        client.cfg = LLMConfig(chat_model="test-model")
        client._client = _FakeStreamHttpClient([
            _FakeStreamResponse([], status_code=503, body=b"private-gateway-body"),
            _FakeStreamResponse([_sse_delta({"content": "完成"}), "data: [DONE]"]),
        ])
        with patch("backend.core.llm.asyncio.sleep", new=AsyncMock()), \
                self.assertLogs("backend.core.llm", level="INFO") as logs:
            events = [event async for event in client.chat_stream_tools([])]
        self.assertEqual("retry", events[0][0])
        self.assertEqual(503, events[0][1]["status_code"])
        self.assertFalse(events[0][1]["empty_stream"])
        attempts = [line for line in logs.output if "LLM stream attempt" in line]
        self.assertEqual(2, len(attempts))
        self.assertIn("status=503", attempts[0])
        self.assertIn("status=200", attempts[1])
        self.assertTrue(all("elapsed_ms=" in line for line in attempts))
        self.assertNotIn("private-gateway-body", "\n".join(logs.output))

    async def test_visible_partial_stream_never_retries_silently(self):
        class InterruptedResponse(_FakeStreamResponse):
            async def aiter_lines(self):
                yield _sse_delta({"content": "已收到的半截回复"})
                raise httpx.ReadError("connection interrupted")
        client = LLMClient.__new__(LLMClient)
        client.cfg = LLMConfig(chat_model="test-model")
        client._client = _FakeStreamHttpClient([InterruptedResponse([])])
        events = []
        with patch("backend.core.llm.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaisesRegex(LLMError, "回复中断"):
                async for event in client.chat_stream_tools([{"role": "user", "content": "test"}]):
                    events.append(event)
        self.assertEqual([("delta", "已收到的半截回复")], events)
        self.assertEqual(1, client._client.calls)
        sleep.assert_not_awaited()

    async def test_timeout_is_not_mislabeled_as_empty_and_backoff_can_be_cancelled(self):
        class TimeoutResponse(_FakeStreamResponse):
            async def aiter_lines(self):
                raise httpx.ReadTimeout("private-timeout-detail")
                yield ""
        client = LLMClient.__new__(LLMClient)
        client.cfg = LLMConfig(chat_model="test-model")
        client._client = _FakeStreamHttpClient([TimeoutResponse([])])
        events = []
        with patch("backend.core.llm.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)), \
                self.assertLogs("backend.core.llm", level="INFO") as logs:
            with self.assertRaises(asyncio.CancelledError):
                async for event in client.chat_stream_tools([]):
                    events.append(event)
        self.assertEqual(1, client._client.calls)
        self.assertEqual(["retry"], [kind for kind, _ in events])
        self.assertFalse(events[0][1]["empty_stream"])
        self.assertEqual("ReadTimeout", events[0][1]["error_type"])
        self.assertIn("error_type=ReadTimeout", "\n".join(logs.output))
        self.assertNotIn("private-timeout-detail", "\n".join(logs.output))

    async def test_reasoning_only_stream_retries(self):
        client = LLMClient.__new__(LLMClient)
        client.cfg = LLMConfig(chat_model="test-model")
        client._client = _FakeStreamHttpClient([
            _FakeStreamResponse([_sse_delta({"reasoning_content": "先分析"}), "data: [DONE]"]),
            _FakeStreamResponse([_sse_delta({"content": "完成"}), "data: [DONE]"]),
        ])
        with patch("backend.core.llm.asyncio.sleep", new=AsyncMock()):
            events = [
                event async for event in client.chat_stream_tools(
                    [{"role": "user", "content": "test"}]
                )
            ]
        self.assertEqual("完成", events[-1][1]["content"])
        self.assertEqual(2, client._client.calls)

    async def test_tool_call_without_text_is_valid(self):
        client = LLMClient.__new__(LLMClient)
        client.cfg = LLMConfig(chat_model="test-model")
        client._client = _FakeStreamHttpClient([_FakeStreamResponse([
            _sse_delta({"tool_calls": [{
                "index": 0, "id": "call_1",
                "function": {"name": "read_file", "arguments": "{\"path\":"},
            }]}),
            _sse_delta({"tool_calls": [{
                "index": 0, "function": {"arguments": "\"a.txt\"}"},
            }]}),
            "data: [DONE]",
        ])])
        events = [
            event async for event in client.chat_stream_tools(
                [{"role": "user", "content": "test"}]
            )
        ]
        self.assertEqual(1, len(events))
        self.assertEqual({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{\"path\":\"a.txt\"}"},
            }],
        }, events[0][1])
        self.assertEqual("done", events[0][0])

    async def test_exhausted_empty_stream_is_service_error(self):
        client = LLMClient.__new__(LLMClient)
        client.cfg = LLMConfig(chat_model="test-model")
        client._client = _FakeStreamHttpClient([_FakeStreamResponse([])])
        with patch("backend.core.llm.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(LLMError) as ctx:
                _ = [
                    event async for event in client.chat_stream_tools(
                        [{"role": "user", "content": "test"}]
                    )
                ]
        self.assertEqual("service", ctx.exception.kind)
        self.assertIn("空回复", str(ctx.exception))
        self.assertEqual(5, client._client.calls)


class LLMErrorClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_4xx_classifies_as_config(self):
        # 能力不匹配/余额/模型名/密钥等 4xx 都是配置问题(用户能自己修)
        for status in (400, 401, 402, 404):
            client = LLMClient.__new__(LLMClient)
            client.cfg = LLMConfig(chat_model="test-model")
            client._client = _FakeHttpClient([status])
            with self.assertRaises(LLMError) as ctx:
                await client.chat([{"role": "user", "content": "test"}])
            self.assertEqual("config", ctx.exception.kind, f"status={status}")

    async def test_5xx_classifies_as_service(self):
        client = LLMClient.__new__(LLMClient)
        client.cfg = LLMConfig(chat_model="test-model")
        client._client = _FakeHttpClient([501])  # 非重试的 5xx → 真故障
        with self.assertRaises(LLMError) as ctx:
            await client.chat([{"role": "user", "content": "test"}])
        self.assertEqual("service", ctx.exception.kind)

    async def test_429_retries_then_service(self):
        # 429 限流是瞬时过载,归服务类:在 _RETRIABLE_STATUS 里重试,耗尽后报错
        client = LLMClient.__new__(LLMClient)
        client.cfg = LLMConfig(chat_model="test-model")
        client._client = _FakeHttpClient([429])
        with patch("backend.core.llm.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(LLMError) as ctx:
                await client.chat([{"role": "user", "content": "test"}])
        self.assertEqual("service", ctx.exception.kind)


if __name__ == "__main__":
    unittest.main()
