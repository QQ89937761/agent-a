"""Hub failover: one first-model request per configured provider, no live APIs."""
import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from backend.core.config import (Config, LLMConfig, LLMProviderConfig, PROVIDERS_ENV,
    PROVIDER_API_KEYS_ENV, apply_env_overrides, provider_runtime_env, subprocess_env_without_secrets)
from backend.core.llm import LLMClient, LLMError, ProviderFailoverClient, runtime_llm


def cfg():
    return LLMConfig(base_url="https://first.invalid/v1", api_key="secret-first", chat_model="first-a", models=["first-a", "first-b"], providers=[
        LLMProviderConfig(id="second", base_url="https://second.invalid/v1", api_key="secret-second", models=["second-a", "second-b"], protocol="responses"),
        LLMProviderConfig(id="third", base_url="https://third.invalid/v1", api_key="secret-third", models=["third-a", "third-b"], protocol="anthropic"),
        LLMProviderConfig(id="fourth", base_url="https://fourth.invalid/v1", api_key="secret-fourth", models=["fourth-a"]),
    ])


def success(protocol, stream=False):
    if not stream:
        data = ({"choices": [{"message": {"role": "assistant", "content": "完成"}}]} if protocol == "openai" else
                {"output": [{"type": "message", "content": [{"type": "output_text", "text": "完成"}]}]} if protocol == "responses" else
                {"content": [{"type": "text", "text": "完成"}], "stop_reason": "end_turn"})
        return httpx.Response(200, json=data)
    if protocol == "openai":
        lines = ['data: '+json.dumps({"choices": [{"delta": {"content": "完成"}}]}), 'data: [DONE]']
    elif protocol == "responses":
        lines = ['event: response.output_text.delta', 'data: '+json.dumps({"type":"response.output_text.delta", "delta":"完成"}),
                 'event: response.completed', 'data: '+json.dumps({"type":"response.completed", "response": {"output": [{"type":"message", "content":[{"type":"output_text", "text":"完成"}]}]}})]
    else:
        lines = ['event: content_block_delta', 'data: '+json.dumps({"delta":{"type":"text_delta", "text":"完成"}}),
                 'event: message_stop', 'data: '+json.dumps({"type":"message_stop"})]
    return httpx.Response(200, text='\n\n'.join(lines)+'\n\n', headers={"content-type":"text/event-stream"})


class HubFailoverTests(unittest.IsolatedAsyncioTestCase):
    async def make(self, handler, config=None):
        client = ProviderFailoverClient(config or cfg())
        self.addAsyncCleanup(client.aclose)
        for child in client._providers:
            child._client = httpx.AsyncClient(base_url=child.cfg.base_url, headers=(
                {"x-api-key": child.cfg.api_key} if child.cfg.protocol == "anthropic" else {"Authorization": "Bearer "+child.cfg.api_key}),
                transport=httpx.MockTransport(handler))
        return client

    async def test_third_provider_success_exact_order_first_models_and_own_protocol_keys(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.host != "third.invalid":
                return httpx.Response(503, text="busy")
            return success("anthropic")
        client = await self.make(handler)
        result = await client.chat([{"role":"user", "content":"test"}], model="ignored-other-model", tools=[{"type":"function", "function":{"name":"query", "parameters":{"type":"object", "properties":{}}}}])
        self.assertEqual("完成", result["content"])
        self.assertEqual(["first.invalid","second.invalid","third.invalid"], [r.url.host for r in requests])
        self.assertEqual(["first-a","second-a","third-a"], [json.loads(r.content)["model"] for r in requests])
        self.assertEqual(["/v1/chat/completions","/v1/responses","/v1/messages"], [r.url.path for r in requests])
        self.assertEqual("Bearer secret-second", requests[1].headers["authorization"])
        self.assertNotIn("secret-first", str(requests[1].headers))
        self.assertEqual("secret-third", requests[2].headers["x-api-key"])

    async def test_success_stops_chain_and_next_call_starts_at_first(self):
        hosts = []
        def handler(request):
            hosts.append(request.url.host)
            return success("responses") if request.url.host == "second.invalid" else httpx.Response(401)
        client = await self.make(handler)
        for _ in range(2):
            self.assertEqual("完成", (await client.chat([]))["content"])
        self.assertEqual(["first.invalid", "second.invalid"]*2, hosts)

    async def test_all_fail_exact_three_requests_no_internal_sleep_and_safe_error(self):
        hosts = []
        def handler(request):
            hosts.append(request.url.host)
            raise httpx.ConnectError("secret-first unavailable", request=request)
        client = await self.make(handler)
        with patch("backend.core.llm.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaisesRegex(LLMError, "网关失败.*3 个厂商") as failure:
                await client.chat([])
        sleep.assert_not_awaited()
        self.assertEqual(3, len(hosts))
        self.assertNotIn("secret-first", str(failure.exception))
        self.assertNotIn("invalid", str(failure.exception))

    async def test_stream_switches_before_output_and_preserves_final_reply(self):
        hosts = []
        def handler(request):
            hosts.append(request.url.host)
            return success("responses", True) if request.url.host == "second.invalid" else httpx.Response(503)
        client = await self.make(handler)
        events = [event async for event in client.chat_stream_tools([])]
        self.assertEqual(["first.invalid","second.invalid"], hosts)
        self.assertEqual("retry", events[0][0])
        self.assertTrue(events[0][1]["provider_switch"])
        self.assertEqual(3, events[0][1]["max_attempts"])
        self.assertEqual("完成", events[-1][1]["content"])
        self.assertEqual("完成", ''.join(data for kind,data in events if kind == "delta"))

    async def test_all_stream_protocols_can_be_final_provider(self):
        for index, protocol in enumerate(("openai", "responses", "anthropic")):
            with self.subTest(protocol=protocol):
                hosts = []
                def handler(request):
                    hosts.append(request.url.host)
                    return success(protocol, True) if len(hosts) == index+1 else httpx.Response(502)
                client = await self.make(handler)
                events = [event async for event in client.chat_stream_tools([])]
                self.assertEqual(index+1, len(hosts))
                self.assertEqual("完成", events[-1][1]["content"])

    async def test_partial_stream_never_switches_or_replays(self):
        hosts = []
        def handler(request):
            hosts.append(request.url.host)
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"半截"}}]}\n\ndata: {"error":{"message":"upstream interrupted"}}\n\n')
        client = await self.make(handler)
        events = []
        with self.assertRaisesRegex(LLMError, "未自动切换"):
            async for event in client.chat_stream_tools([]):
                events.append(event)
        self.assertEqual(["first.invalid"], hosts)
        self.assertEqual([("delta","半截")], events)

    async def test_empty_response_and_malformed_json_switch_without_second_model(self):
        hosts = []
        def handler(request):
            hosts.append(request.url.host)
            if len(hosts) == 1:
                return httpx.Response(200, json={"choices":[{"message":{"role":"assistant","content":""}}]})
            if len(hosts) == 2:
                return httpx.Response(200, text="not-json")
            return success("anthropic")
        client = await self.make(handler)
        self.assertEqual("完成", (await client.chat([]))["content"])
        self.assertEqual(3, len(hosts))

    async def test_tool_fragments_before_failure_never_execute_partial_calls(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.host == "first.invalid":
                return httpx.Response(200, text='data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"delete_file","arguments":"{"}}]}}]}\n\ndata: {"error":{"message":"unavailable"}}\n\n')
            return success("responses", True)
        client = await self.make(handler)
        events = [event async for event in client.chat_stream_tools([])]
        self.assertNotIn("tool_calls", events[-1][1])
        self.assertEqual("完成", events[-1][1]["content"])
        self.assertEqual(2, len(requests))

    async def test_provider_specific_responses_history_only_replayed_to_same_provider(self):
        requests = []
        def handler(request):
            requests.append(request)
            return success("responses") if request.url.host == "second.invalid" else httpx.Response(503)
        client = await self.make(handler)
        messages = [{"role": "user", "content": "test"}]
        message = await client.chat(messages)
        self.assertEqual(client._provider_tags[1], message["_failover_provider"])
        self.assertTrue(message["_responses_output"])
        message["_responses_output"].append({"type":"reasoning", "encrypted_content":"opaque-for-second"})
        messages += [message, {"role":"user", "content":"continue"}]
        self.assertEqual("完成", (await client.chat(messages))["content"])
        primary = json.loads(requests[2].content)
        secondary = json.loads(requests[3].content)
        self.assertNotIn("_failover_provider", json.dumps(primary))
        self.assertNotIn("opaque-for-second", json.dumps(primary))
        self.assertIn("opaque-for-second", json.dumps(secondary))
        # A different Responses provider also must not receive opaque state.
        portable = client._messages_for_provider(messages, 2)
        self.assertNotIn("_responses_output", portable[1])
        self.assertEqual("完成", portable[1]["content"])
        self.assertIn("_responses_output", message)  # Never mutate saved history.

    async def test_agent_tool_round_retries_from_first_and_executes_tool_once(self):
        from backend.core.agent import Agent
        from backend.core.config import AgentConfig
        from backend.core.tools import Tool, ToolRegistry
        requests, executed = [], []
        def handler(request):
            requests.append(request)
            if request.url.host == "first.invalid":
                return httpx.Response(503)
            if len(requests) == 2:
                output = [{"type":"reasoning", "summary":[], "encrypted_content":"opaque-second"},
                          {"type":"function_call", "call_id":"call_query", "name":"query", "arguments":"{}"}]
                return httpx.Response(200, text='event: response.completed\ndata: '+json.dumps({
                    "type":"response.completed", "response":{"output":output}})+'\n\n')
            return success("responses", True)
        client = await self.make(handler)
        registry = ToolRegistry()
        async def query():
            executed.append("query")
            return {"ok":True}
        registry.register(Tool("query", "read", {"type":"object", "properties":{}}, query))
        history = [{"role":"assistant", "content":"prior", "_responses_output":[{
            "type":"reasoning", "summary":[], "encrypted_content":"prior-opaque-second"}],
            "_failover_provider":client._provider_tags[1]}]
        events = [event async for event in Agent(client, registry, AgentConfig()).run("查询", history=history)]
        self.assertFalse(any(event.type == "error" for event in events))
        self.assertEqual(["query"], executed)
        self.assertEqual(["first.invalid", "second.invalid"]*2, [r.url.host for r in requests])
        primary = json.loads(requests[2].content)
        secondary = json.loads(requests[3].content)
        self.assertNotIn("opaque-second", json.dumps(primary))
        self.assertNotIn("_failover_provider", json.dumps(primary))
        self.assertIn("prior-opaque-second", json.dumps(secondary))
        self.assertTrue(any(item.get("type") == "reasoning" and item.get("encrypted_content") == "opaque-second"
                            for item in secondary["input"]))
        self.assertEqual(["call_query"], [item["call_id"] for item in secondary["input"]
                                        if item.get("type") == "function_call_output"])

    async def test_nonobject_upstream_json_is_failure_not_crash(self):
        hosts = []
        def handler(request):
            hosts.append(request.url.host)
            return httpx.Response(200, json=[]) if len(hosts) == 1 else success("responses")
        client = await self.make(handler)
        self.assertEqual("完成", (await client.chat([]))["content"])
        self.assertEqual(["first.invalid", "second.invalid"], hosts)

    async def test_cancel_never_contacts_backup(self):
        hosts = []
        def handler(request):
            hosts.append(request.url.host)
            raise asyncio.CancelledError()
        client = await self.make(handler)
        with self.assertRaises(asyncio.CancelledError):
            await client.chat([])
        self.assertEqual(["first.invalid"], hosts)

    async def test_missing_backups_no_duplicate_requests_and_local_policy_unchanged(self):
        config = cfg()
        config.providers = []
        hosts = []
        def handler(request):
            hosts.append(request.url.host)
            return httpx.Response(503)
        client = await self.make(handler, config)
        with self.assertRaisesRegex(LLMError, "1 个厂商"):
            await client.chat([])
        self.assertEqual(1, len(hosts))
        self.assertIs(type(runtime_llm(config, hub=False)), LLMClient)
        self.assertIs(type(runtime_llm(config, hub=True)), ProviderFailoverClient)
        self.assertEqual(5, LLMClient(config)._attempt_limit())


class ProviderSyncTests(unittest.TestCase):
    def test_roundtrip_order_protocol_models_and_separate_secrets(self):
        from deploy.update_hub_env import update_environment
        import shlex
        original = cfg()
        values = provider_runtime_env(original.providers)
        self.assertNotIn("secret-", values[PROVIDERS_ENV])
        self.assertNotIn("fourth", values[PROVIDERS_ENV])
        updated = update_environment("AGENT_ROLE=hub\n", values)
        shell_values = dict(line.split("=", 1) for line in shlex.split(updated))
        with patch.dict(os.environ, shell_values, clear=True):
            loaded = apply_env_overrides(Config())
            self.assertEqual(original.providers[:2], loaded.llm.providers)
            self.assertNotIn(PROVIDER_API_KEYS_ENV, subprocess_env_without_secrets())
        with patch.dict(os.environ, provider_runtime_env([]), clear=True):
            loaded = apply_env_overrides(Config(llm=original))
            self.assertEqual([], loaded.llm.providers)

    def test_runtime_env_update_uses_installed_python_not_bare_system_python(self):
        from backend.core.cloud_deploy import _apply_hub_environment
        client = MagicMock()
        _apply_hub_environment(client, "sudo -n ", provider_runtime_env(cfg().providers))
        command = client.run.call_args.args[0]
        self.assertIn("/opt/xiaoa/.venv/bin/python /opt/xiaoa/deploy/update_hub_env.py", command)
        self.assertNotIn("secret-", command)

    def test_invalid_provider_metadata_fails_without_leaking_payload(self):
        for invalid in ('secret-json', '{}', '[{"id":"secret-id","api_key":"secret-key"}]',
                        '[{"id":"x","base_url":"https://x","models":"not-list"}]'):
            with self.subTest(invalid=invalid), patch.dict(os.environ, {PROVIDERS_ENV: invalid}, clear=True):
                with self.assertRaisesRegex(ValueError, "格式无效") as caught:
                    apply_env_overrides(Config())
                self.assertNotIn("secret-", str(caught.exception))

    def test_remote_update_validates_json_without_echoing_secrets(self):
        from deploy.update_hub_env import update_environment
        for name, value in ((PROVIDERS_ENV, "secret-invalid"), (PROVIDERS_ENV, "{}"),
                            (PROVIDER_API_KEYS_ENV, "[]"), (PROVIDER_API_KEYS_ENV, '{"x":123}')):
            with self.subTest(name=name, value=value):
                with self.assertRaises(ValueError) as caught:
                    update_environment("AGENT_ROLE=hub\n", {name: value})
                self.assertNotIn("secret-invalid", str(caught.exception))


class ProviderServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_sync_includes_backups_and_hub_cannot_bypass_chain(self):
        from types import SimpleNamespace
        from backend.api import server
        config = Config(llm=cfg())
        config.role.mode = "hub"
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        client = runtime_llm(config.llm, hub=True)
        self.addAsyncCleanup(client.aclose)
        with patch.object(server, "CFG", config), patch.object(server, "LLM", client), \
                patch("backend.core.cloud_deploy.sync_runtime_settings") as sync:
            self.assertTrue((await server.api_cloud_runtime(request))["ok"])
            values = sync.call_args.args[1]
            self.assertEqual(provider_runtime_env(config.llm.providers)[PROVIDERS_ENV], values[PROVIDERS_ENV])
            self.assertEqual("secret-third", json.loads(values[PROVIDER_API_KEYS_ENV])["third"])
            selected, owned, _, _ = server._llm_for_provider("third")
            self.assertIs(client, selected)
            self.assertFalse(owned)


if __name__ == "__main__":
    unittest.main()
