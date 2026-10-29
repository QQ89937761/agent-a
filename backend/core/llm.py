"""统一的 OpenAI 兼容 LLM 客户端。

所有对大模型的调用都走这里,底层接你现有的中转网关(Grok2API / Agnes)。
支持:普通对话、带工具(function calling)的对话、多模态(图片)、流式。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import ssl
import sys
import time
from contextlib import aclosing
from typing import Any, AsyncIterator, Iterable

import httpx

from .config import LLMConfig


class LLMError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "service"):
        super().__init__(message)
        # kind: "config"=用户能自己修的配置问题(能力/余额/模型名/密钥),
        #       "service"=真故障(5xx/超时/连不上/空响应)。前端据此决定要不要上红点。
        self.kind = kind


# 这些状态码代表"网关此刻不行,过一下可能就好了",值得重试。
# 4xx(除429)是请求本身写错了,重试没意义。
_RETRIABLE_STATUS = (429, 500, 502, 503, 504)
_MAX_ATTEMPTS = 5
_LOGGER = logging.getLogger(__name__)


def _retry_delay(attempt: int) -> float:
    """首次请求后的四次退避：2s、4s、6s、8s；共 5 次尝试。"""
    return 2.0 * attempt


def _stream_retry(attempt: int, previous: dict) -> dict:
    status = previous.get("status_code")
    reason = ("网关返回空流" if previous.get("empty_stream") else
              f"网关返回 HTTP {status}" if status and status >= 400 else "网关响应中断或不可用")
    delay = _retry_delay(attempt)
    return {**previous, "attempt": attempt + 1, "max_attempts": _MAX_ATTEMPTS,
            "delay_ms": round(delay * 1000),
            "text": f"{reason}，{delay:g} 秒后重试（{attempt + 1}/{_MAX_ATTEMPTS}）。"}


def _stream_attempt(started: float, status: int | None, empty: bool, attempt: int, protocol: str,
                    error_type: str = "", max_attempts: int = _MAX_ATTEMPTS) -> dict:
    # Diagnostics never include prompts, credentials, URLs or raw gateway bodies.
    data = {"status_code": status, "elapsed_ms": max(0, round((time.perf_counter() - started) * 1000)),
            "empty_stream": empty, "error_type": error_type}
    _LOGGER.info("LLM stream attempt protocol=%s attempt=%d/%d status=%s elapsed_ms=%d empty_stream=%s error_type=%s",
                 protocol, attempt + 1, max_attempts, status, data["elapsed_ms"], empty, error_type or "none")
    return data


def _is_retriable_exc(e: Exception) -> bool:
    """连接层面的抖动(连不上、超时)可重试。"""
    return isinstance(e, (httpx.TransportError, httpx.TimeoutException))


def _extract_error_message(body) -> str:
    """从网关错误体里抠出 message/detail 原文；抠不到就用整个 body。"""
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace")
    elif isinstance(body, str):
        text = body
    else:
        text = str(body or "")
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            for key in ("message", "detail"):
                if error.get(key):
                    return str(error[key])
        for key in ("message", "detail"):
            if data.get(key):
                return str(data[key])
    return text


def _friendly_gateway_error(status: int, body) -> str:
    """把网关的英文报错转成可操作的人话(用户看到的不再是一坨 JSON)。"""
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace")
    else:
        text = str(body or "")
    if "INVALID_MODEL_ID" in text.upper():
        message = _extract_error_message(body)
        return (
            f"网关返回 {status}：当前模型 ID 无效或在此接口不可用。"
            "请在「模型服务」中从上游探测模型，选择可用的模型 ID 后重试。"
            f"（INVALID_MODEL_ID；网关原文：{message[:300]}）"
        )
    if "MODEL_CAPABILITY_NOT_SUPPORTED" in text.upper():
        message = _extract_error_message(body)
        low = message.lower()
        # 按 message 说的是哪种能力来定,不要见到这个 code 就一律说"不支持看图"。
        if any(kw in low for kw in ("vision", "image", "图")):
            return "当前模型不支持看图（视觉能力），请切换到支持视觉的模型后重试。"
        if any(kw in low for kw in ("tool", "function_call", "function call")):
            return "当前模型不支持工具调用（函数调用），请换一个支持工具调用的模型。"
        return f"当前模型不支持本次请求用到的某项能力（网关原文：{message[:300]}）"
    return f"网关返回 {status}: {text[:500]}"


def _anthropic_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content or ""
    blocks = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            blocks.append({"type": "text", "text": item.get("text", "")})
        elif item.get("type") == "image_url":
            url = (item.get("image_url") or {}).get("url", "")
            if url.startswith("data:") and ";base64," in url:
                media_type, data = url[5:].split(";base64,", 1)
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
    return blocks


def _anthropic_request(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                       model: str, temperature: float, stream: bool = False) -> dict[str, Any]:
    system, converted = [], []
    for message in messages:
        role, content = message.get("role"), message.get("content") or ""
        if role == "system":
            if isinstance(content, str) and content:
                system.append({"type": "text", "text": content})
        elif role == "tool":
            result = {"type": "tool_result", "tool_use_id": message.get("tool_call_id", ""),
                      "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)}
            if converted and converted[-1]["role"] == "user":
                converted[-1]["content"].append(result)
            else:
                converted.append({"role": "user", "content": [result]})
        elif role in ("user", "assistant"):
            blocks = _anthropic_content(content)
            if not isinstance(blocks, list):
                blocks = ([{"type": "text", "text": blocks}] if blocks else [])
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    arguments = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    arguments = {}
                blocks.append({"type": "tool_use", "id": call.get("id", ""),
                               "name": fn.get("name", ""), "input": arguments})
            converted.append({"role": role, "content": blocks or " "})
    payload: dict[str, Any] = {"model": model, "messages": converted, "max_tokens": 4096,
                               "temperature": temperature}
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = [{"name": item["function"]["name"],
                              "description": item["function"].get("description", ""),
                              "input_schema": item["function"].get("parameters") or {"type": "object"}}
                             for item in tools if item.get("type") == "function" and item.get("function")]
    if stream:
        payload["stream"] = True
    return payload


def _normalize_anthropic_message(data: dict[str, Any]) -> dict[str, Any]:
    text, calls = [], []
    for index, block in enumerate(data.get("content") or []):
        if block.get("type") == "text":
            text.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            calls.append({"id": block.get("id") or f"call_{index}", "type": "function",
                          "function": {"name": block.get("name", ""),
                                       "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False)}})
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text)}
    if calls:
        message["tool_calls"] = calls
    return message


def _chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in message.items() if key not in {"_responses_output", "_failover_provider"}}
            for message in messages]


def _responses_request(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                       model: str, reasoning_effort: str | None = None, *, stream: bool = False) -> dict:
    inputs = []
    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""
        native = message.get("_responses_output")
        if role == "assistant" and isinstance(native, list) and native:
            calls = {call["id"]: call["function"] for call in message.get("tool_calls") or []}
            for item in native:
                if item.get("type") == "function_call":
                    function = calls.get(item.get("call_id"))
                    if not function:
                        raise LLMError("Responses 历史工具调用缺少配对参数", kind="config")
                    inputs.append({**item, "name": function["name"], "arguments": function["arguments"]})
                else:
                    inputs.append(item)
            continue
        if role == "tool":
            inputs.append({"type": "function_call_output", "call_id": message.get("tool_call_id", ""),
                           "output": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)})
            continue
        if role not in {"system", "developer", "user", "assistant"}:
            raise LLMError(f"Responses 不支持消息角色：{role}", kind="config")
        blocks = []
        for part in content if isinstance(content, list) else [{"type": "text", "text": content}]:
            if part.get("type") == "text":
                blocks.append({"type": "output_text" if role == "assistant" else "input_text",
                               "text": part.get("text", "")})
            elif part.get("type") == "refusal" and role == "assistant":
                blocks.append({"type": "refusal", "refusal": part.get("refusal", "")})
            elif part.get("type") == "image_url":
                image = part.get("image_url") or {}
                blocks.append({"type": "input_image", "image_url": image.get("url", ""),
                               "detail": image.get("detail", "auto")})
            else:
                raise LLMError("Responses 收到不支持的消息内容类型", kind="config")
        if blocks and (content or not message.get("tool_calls")):
            inputs.append({"role": role, "content": blocks})
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            inputs.append({"type": "function_call", "call_id": call.get("id", ""),
                           "name": function.get("name", ""), "arguments": function.get("arguments") or "{}"})
    # Replay native output locally, without server-side conversation state.
    # Sampling is optional; omit temperature for reasoning-model compatibility.
    payload = {"model": model, "input": inputs, "store": False,
               "include": ["reasoning.encrypted_content"], "stream": stream}
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}
    if tools:
        payload["tools"] = [{"type": "function", **item["function"], "strict": False}
                            for item in tools if item.get("type") == "function"]
        payload["tool_choice"] = "auto"
    return payload


def _responses_error(data: dict) -> LLMError:
    error = data.get("error") or data
    code = str(error.get("code") or error.get("type") or "") if isinstance(error, dict) else ""
    config = any(word in code.upper() for word in
                 ("INVALID", "AUTH", "API_KEY", "NOT_FOUND", "QUOTA", "BALANCE", "PERMISSION"))
    message = error.get("message") or error.get("detail") if isinstance(error, dict) else error
    return LLMError("Responses 接口错误：" + str(message or error), kind="config" if config else "service")


def _normalize_responses_message(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get("output", []), list):
        raise LLMError("Responses 返回结构异常")
    if data.get("error"):
        raise _responses_error(data)
    if data.get("status") not in {None, "completed"}:
        reason = (data.get("incomplete_details") or {}).get("reason") or data["status"]
        raise LLMError(f"Responses 回复未完成：{reason}")
    text, calls, native = [], [], []
    call_ids = set()
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            raise LLMError("Responses output 项结构异常")
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    text.append(part.get("text", ""))
                elif part.get("type") == "refusal":
                    text.append(part.get("refusal", ""))
        elif kind == "function_call":
            arguments = item.get("arguments") or "{}"
            try:
                valid = isinstance(arguments, str) and isinstance(json.loads(arguments), dict)
            except (TypeError, ValueError):
                valid = False
            if (not item.get("call_id") or not item.get("name") or not valid
                    or item.get("status") not in {None, "completed"} or item["call_id"] in call_ids):
                raise LLMError("Responses 工具调用不完整或参数不是有效 JSON 对象，未执行工具")
            call_ids.add(item["call_id"])
            calls.append({"id": item["call_id"], "type": "function",
                          "function": {"name": item["name"], "arguments": arguments}})
        if kind in {"message", "function_call", "reasoning"}:
            # Function arguments have one canonical copy, so checkpoint redaction
            # and cancellation repair cannot leave a second, unsafe copy behind.
            native.append({key: item[key] for key in ("type", "id", "call_id", "status") if key in item}
                          if kind == "function_call" else item)
    message = {"role": "assistant", "content": "".join(text), "_responses_output": native}
    if calls:
        message["tool_calls"] = calls
    return message


async def _responses_events(response: httpx.Response):
    lines = []
    event_name = ""
    async for line in response.aiter_lines():
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            lines.append(line[5:].lstrip())
        elif not line:
            if lines:
                try:
                    event = json.loads("\n".join(lines))
                except ValueError:
                    event = None
                if isinstance(event, dict):
                    if event_name == "error":
                        event = {"type": "error", "error": event.get("error") or event}
                    yield event
            lines.clear()
            event_name = ""
    if lines:
        try:
            event = json.loads("\n".join(lines))
        except ValueError:
            event = None
        if isinstance(event, dict):
            if event_name == "error":
                event = {"type": "error", "error": event.get("error") or event}
            yield event


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._client: httpx.AsyncClient | None = None

    def _attempt_limit(self) -> int:
        # Ordinary/local clients retain their existing retry policy. Runtime hub
        # failover children get one attempt each, never 3 providers x 5 retries.
        return getattr(self, "max_attempts", _MAX_ATTEMPTS)

    def _http(self) -> httpx.AsyncClient:
        """Build lazily so a broken portable bundle can still start and report the problem."""
        if self._client is None:
            try:
                headers = (
                    {"x-api-key": self.cfg.api_key, "anthropic-version": "2023-06-01"}
                    if self.cfg.protocol == "anthropic"
                    else {"Authorization": f"Bearer {self.cfg.api_key}"}
                )
                self._client = httpx.AsyncClient(
                    base_url=self.cfg.base_url.rstrip("/"), headers=headers,
                    timeout=self.cfg.timeout, trust_env=False,
                    # Portable resources live in a temporary extraction directory.
                    # Windows already has a trusted CA store; keep verification on
                    # without requiring certifi to reopen a temporary PEM file.
                    verify=ssl.create_default_context() if sys.platform == "win32" else True,
                )
            except FileNotFoundError as exc:
                raise LLMError(
                    "模型服务初始化失败：TLS 证书文件不可用，请检查系统证书或重新下载完整的小A。"
                ) from exc
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _anthropic_path(self) -> str:
        return "/messages" if self.cfg.base_url.rstrip("/").endswith("/v1") else "/v1/messages"

    def _models_path(self) -> str:
        if self.cfg.protocol != "anthropic":
            return "/models"
        return "/models" if self.cfg.base_url.rstrip("/").endswith("/v1") else "/v1/models"

    async def list_models(self) -> list[str]:
        """读取上游模型目录；不把目录结果写入配置，供设置页选择。"""
        response = await self._http().get(self._models_path())
        if response.status_code >= 400:
            raise LLMError(f"上游模型目录返回 {response.status_code}: {response.text[:300]}", kind="config")
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMError("上游模型目录不是有效 JSON", kind="config") from exc
        items = payload.get("data", payload.get("models", payload)) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise LLMError("上游模型目录格式不受支持", kind="config")
        models: list[str] = []
        for item in items:
            if isinstance(item, str):
                model = item.strip()
            elif isinstance(item, dict):
                model = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
            else:
                model = ""
            if model and model not in models:
                models.append(model)
        if not models:
            raise LLMError("上游没有返回可用模型", kind="config")
        return models

    # ---------- 基础对话 ----------
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.3,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """一次非流式对话,返回原始 message 对象(可能含 tool_calls)。"""
        if self.cfg.protocol == "responses":
            payload = _responses_request(messages, tools, model or self.cfg.chat_model, reasoning_effort)
        elif self.cfg.protocol == "anthropic":
            payload = _anthropic_request(messages, tools, model or self.cfg.chat_model, temperature)
        else:
            payload = {"model": model or self.cfg.chat_model, "messages": _chat_messages(messages), "temperature": temperature}
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"
            if reasoning_effort:
                payload["reasoning_effort"] = reasoning_effort

        # 网关偶发 502/503/504 或连接抖动,重试几次再放弃(否则整轮任务白跑)。
        # 非流式是原子的:要么成功要么失败,没吐给用户任何东西,所以可以无条件重试。
        last_err: Exception | None = None
        for attempt in range(self._attempt_limit()):
            if attempt:
                await asyncio.sleep(_retry_delay(attempt))
            try:
                endpoint = ("/responses" if self.cfg.protocol == "responses" else
                            self._anthropic_path() if self.cfg.protocol == "anthropic" else "/chat/completions")
                resp = await self._http().post(endpoint, json=payload)
            except Exception as e:
                if not _is_retriable_exc(e):
                    raise
                last_err = LLMError(f"连不上网关({type(e).__name__}): {e}")
                continue
            if resp.status_code in _RETRIABLE_STATUS:
                last_err = LLMError(f"网关返回 {resp.status_code}: {resp.text[:300]}")
                continue
            if resp.status_code >= 400:
                # 4xx(除 429,其已被 _RETRIABLE_STATUS 重试)是请求本身的问题,重试无意义,直接抛
                kind = "config" if resp.status_code < 500 else "service"
                raise LLMError(_friendly_gateway_error(resp.status_code, resp.text), kind=kind)
            data = resp.json()
            if not isinstance(data, dict):
                raise LLMError("网关响应结构异常：应返回 JSON 对象")
            if self.cfg.protocol == "responses":
                message = _normalize_responses_message(data)
                if not message["content"].strip() and not message.get("tool_calls"):
                    last_err = LLMError("Responses 返回空回复")
                    continue
                return message
            if self.cfg.protocol == "anthropic":
                return _normalize_anthropic_message(data)
            try:
                return data["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as e:
                raise LLMError(f"网关响应结构异常: {json.dumps(data)[:500]}") from e
        raise LLMError(f"网关连续 {self._attempt_limit()} 次失败,最后一次:{last_err}")

    # ---------- 流式对话(给前端逐字显示) ----------
    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.3,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[str]:
        if self.cfg.protocol in {"anthropic", "responses"}:
            async for kind, value in self.chat_stream_tools(messages, model=model, temperature=temperature,
                                                          reasoning_effort=reasoning_effort):
                if kind == "delta":
                    yield value
            return
        payload = {
            "model": model or self.cfg.chat_model,
            "messages": _chat_messages(messages),
            "temperature": temperature,
            "stream": True,
        }
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        async with self._http().stream("POST", "/chat/completions", json=payload) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise LLMError(f"网关返回 {resp.status_code}: {body[:500]!r}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[len("data:"):].strip()
                if chunk == "[DONE]":
                    break
                try:
                    delta = json.loads(chunk)["choices"][0]["delta"]
                except (KeyError, IndexError, json.JSONDecodeError):
                    continue
                if content := delta.get("content"):
                    yield content

    # ---------- 流式 + 工具(给 agent 循环用) ----------
    async def _anthropic_stream(self, payload: dict[str, Any]):
        last_err: Exception | None = None
        previous = {}
        for attempt in range(self._attempt_limit()):
            if attempt:
                yield "retry", _stream_retry(attempt, previous)
                await asyncio.sleep(_retry_delay(attempt))
            texts: list[str] = []
            calls: dict[int, dict[str, str]] = {}
            event_type = ""
            yielded_any = False
            started = time.perf_counter()
            response_status = None
            completed = False
            received = False
            error_type = ""
            try:
                async with self._http().stream("POST", self._anthropic_path(), json=payload) as resp:
                    response_status = resp.status_code
                    if resp.status_code in _RETRIABLE_STATUS:
                        body = await resp.aread()
                        last_err = LLMError(_friendly_gateway_error(resp.status_code, body), kind="service")
                        continue
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        raise LLMError(_friendly_gateway_error(resp.status_code, body),
                                       kind="config" if resp.status_code < 500 else "service")
                    async for line in resp.aiter_lines():
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            try:
                                event = json.loads(line[5:].strip())
                            except ValueError:
                                continue
                            if event_type == "content_block_start":
                                block = event.get("content_block") or {}
                                if block.get("type") == "tool_use":
                                    calls[event.get("index", 0)] = {
                                        "id": block.get("id", ""), "name": block.get("name", ""), "args": ""
                                    }
                            elif event_type == "content_block_delta":
                                delta = event.get("delta") or {}
                                if delta.get("type") == "text_delta" and delta.get("text"):
                                    texts.append(delta["text"])
                                    yielded_any = True
                                    yield "delta", delta["text"]
                                elif delta.get("type") == "input_json_delta":
                                    calls.setdefault(event.get("index", 0), {"id": "", "name": "", "args": ""})["args"] += delta.get("partial_json", "")
                            elif event_type == "message_stop":
                                completed = True
                                message: dict[str, Any] = {"role": "assistant", "content": "".join(texts)}
                                if calls:
                                    message["tool_calls"] = []
                                    for index, slot in sorted(calls.items()):
                                        try:
                                            args = json.dumps(json.loads(slot["args"] or "{}"), ensure_ascii=False)
                                        except ValueError:
                                            args = "{}"
                                        message["tool_calls"].append({"id": slot["id"] or f"call_{index}", "type": "function",
                                            "function": {"name": slot["name"], "arguments": args}})
                                yield "done", message
                                return
                received = True
            except LLMError:
                raise
            except Exception as e:
                error_type = type(e).__name__
                if yielded_any or not _is_retriable_exc(e):
                    raise LLMError(f"Anthropic stream interrupted ({type(e).__name__}): {e}", kind="service") from e
                last_err = LLMError(f"Anthropic connection error ({type(e).__name__}): {e}", kind="service")
                continue
            finally:
                previous = _stream_attempt(started, response_status,
                                           response_status == 200 and received and not completed and not texts and not calls,
                                           attempt, "anthropic", error_type, self._attempt_limit())
            last_err = LLMError("Anthropic stream ended without message_stop", kind="service")
            if yielded_any:
                raise last_err
        raise LLMError(f"Anthropic stream failed after {self._attempt_limit()} attempts: {last_err}", kind="service")

    async def _responses_stream(self, payload: dict[str, Any]):
        last_err = None
        previous = {}
        for attempt in range(self._attempt_limit()):
            if attempt:
                yield "retry", _stream_retry(attempt, previous)
                await asyncio.sleep(_retry_delay(attempt))
            started = time.perf_counter()
            status = None
            yielded_any = False
            result = None
            items = {}
            stream_error = None
            error_type = ""
            try:
                async with self._http().stream("POST", "/responses", json=payload) as response:
                    status = response.status_code
                    if status in _RETRIABLE_STATUS:
                        last_err = LLMError(_friendly_gateway_error(status, await response.aread()))
                        continue
                    if status >= 400:
                        raise LLMError(_friendly_gateway_error(status, await response.aread()),
                                       kind="config" if status < 500 else "service")
                    async for event in _responses_events(response):
                        kind = ("error" if event.get("error") or event.get("object") == "error"
                                else event.get("type"))
                        index = event.get("output_index", 0)
                        if kind in {"response.output_item.added", "response.output_item.done"}:
                            if isinstance(event.get("item"), dict):
                                items[index] = event["item"]
                        elif kind in {"response.output_text.delta", "response.refusal.delta"}:
                            text = event.get("delta") or ""
                            if not isinstance(text, str) or not text:
                                continue
                            item = items.setdefault(index, {"type": "message", "role": "assistant", "content": []})
                            content_index = event.get("content_index", 0)
                            content = item.setdefault("content", [])
                            while len(content) <= content_index:
                                content.append({"type": "output_text", "text": ""})
                            refusal = kind == "response.refusal.delta"
                            part = content[content_index]
                            field = "refusal" if refusal else "text"
                            part["type"] = "refusal" if refusal else "output_text"
                            part[field] = part.get(field, "") + text
                            yielded_any = True
                            yield "delta", text
                        elif kind in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
                            slot = items.setdefault(index, {"type": "function_call"})
                            if kind.endswith(".done"):
                                slot["arguments"] = event.get("arguments", "")
                            else:
                                slot["arguments"] = slot.get("arguments", "") + event.get("delta", "")
                        elif kind == "response.completed":
                            result = event.get("response") or {}
                            if not result.get("output"):
                                result["output"] = [item for _, item in sorted(items.items())]
                            break
                        elif kind == "response.incomplete":
                            detail = event.get("response") or {}
                            detail["status"] = "incomplete"
                            _normalize_responses_message(detail)
                        elif kind in {"error", "response.failed"}:
                            stream_error = _responses_error(event.get("response") or event)
                            error_type = kind
                            if yielded_any or stream_error.kind == "config":
                                raise stream_error
                            last_err = stream_error
                            break
            except LLMError:
                raise
            except Exception as exc:
                error_type = type(exc).__name__
                if yielded_any or not _is_retriable_exc(exc):
                    raise LLMError(f"Responses 回复中断（{type(exc).__name__}）：{exc}") from exc
                last_err = LLMError(f"Responses 连接失败（{type(exc).__name__}）：{exc}")
                continue
            finally:
                output = (result or {}).get("output") or list(items.values())
                useful = any(isinstance(item, dict) and (
                    item.get("type") == "function_call" and item.get("name") or
                    item.get("type") == "message" and any(
                        part.get("text") or part.get("refusal") for part in item.get("content") or []
                    )) for item in output)
                previous = _stream_attempt(started, status,
                                           status == 200 and not useful and not error_type,
                                           attempt, "responses", error_type, self._attempt_limit())
            if stream_error:
                continue
            if result is None:
                last_err = LLMError("Responses 流在 response.completed 之前结束，未执行工具")
                if yielded_any:
                    raise last_err
                continue
            message = _normalize_responses_message(result)
            if not message["content"].strip() and not message.get("tool_calls"):
                last_err = LLMError("Responses 返回空回复")
                if yielded_any:
                    raise last_err
                continue
            yield "done", message
            return
        raise LLMError(f"Responses 连续 {self._attempt_limit()} 次失败，最后一次：{last_err}")

    async def chat_stream_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.3,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[tuple[str, Any]]:
        """流式对话,同时能拿到工具调用。产出事件:
            ("delta", "文字片段")  —— 边生成边吐,前端可逐字显示
            ("done",  message)    —— 结束时给出完整 message(含拼好的 tool_calls)
            ("retry", diagnostics) —— 下一次尝试前的状态通知，不进入模型历史

        为什么要自己拼:实测网关的 tool_calls 是**分片**返回的——
        第一片给 name 和 id,后面几片只给 arguments 的片段,靠 index 对应。
        必须按 index 把 arguments 字符串拼起来才能 json.loads。

        重试策略(与 chat() 的区别很重要):
        只在**还没吐出任何文字之前**才透明重试。一旦已经 yield 过 delta,
        重试会让用户看到重复的半截回答,那比报错更糟,所以只能把错误抛出去。
        好消息:HTTP 502 这类"网关此刻不在"的错误随响应头一起到达,
        必然发生在第一个 delta 之前,所以你遇到的 502 是能完全透明重试的。
        """
        if self.cfg.protocol == "responses":
            payload = _responses_request(messages, tools, model or self.cfg.chat_model, reasoning_effort, stream=True)
            async with aclosing(self._responses_stream(payload)) as stream:
                async for event in stream:
                    yield event
            return
        if self.cfg.protocol == "anthropic":
            payload = _anthropic_request(messages, tools, model or self.cfg.chat_model, temperature, stream=True)
            async with aclosing(self._anthropic_stream(payload)) as stream:
                async for event in stream:
                    yield event
            return
        payload: dict[str, Any] = {
            "model": model or self.cfg.chat_model, "messages": _chat_messages(messages),
            "temperature": temperature, "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort

        last_err: Exception | None = None
        previous = {}
        for attempt in range(self._attempt_limit()):
            if attempt:
                yield "retry", _stream_retry(attempt, previous)
                await asyncio.sleep(_retry_delay(attempt))
            yielded_any = False          # 本次尝试有没有吐过字
            content_parts: list[str] = []
            # index -> {"id","name","args"};按 index 累积分片
            tc_acc: dict[int, dict[str, Any]] = {}
            response_status: int | None = None
            saw_sse_data = False
            saw_done = False
            malformed_chunks = 0
            reasoning_chars = 0
            stream_error = None
            started = time.perf_counter()
            received = False
            error_type = ""
            try:
                async with self._http().stream(
                    "POST", "/chat/completions", json=payload
                ) as resp:
                    response_status = resp.status_code
                    if resp.status_code in _RETRIABLE_STATUS:
                        body = await resp.aread()
                        last_err = LLMError(f"网关返回 {resp.status_code}: {body[:300]!r}")
                        continue          # 还没吐字,安全重试
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        kind = "config" if resp.status_code < 500 else "service"
                        raise LLMError(_friendly_gateway_error(resp.status_code, body), kind=kind)

                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        saw_sse_data = True
                        chunk = line[len("data:"):].strip()
                        if chunk == "[DONE]":
                            saw_done = True
                            break
                        try:
                            event = json.loads(chunk)
                        except (ValueError, TypeError):
                            malformed_chunks += 1
                            continue
                        if not isinstance(event, dict):
                            malformed_chunks += 1
                            continue
                        if event.get('error'):
                            error = event['error']
                            code = str(error.get('code') or error.get('type') or '') if isinstance(error, dict) else ''
                            config_error = any(word in code.upper() for word in
                                               ('INVALID', 'AUTH', 'API_KEY', 'CAPABILITY', 'NOT_FOUND', 'QUOTA', 'BALANCE'))
                            message = _friendly_gateway_error(400 if config_error else response_status, chunk)
                            if config_error or yielded_any:
                                raise LLMError(message, kind='config' if config_error else 'service')
                            stream_error = LLMError(message, kind='service')
                            last_err = stream_error
                            break
                        choices = event.get('choices')
                        if not choices and event.get('usage'):
                            continue  # Legitimate usage-only tail from OpenAI-compatible gateways.
                        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                            malformed_chunks += 1
                            continue
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        if not isinstance(delta, dict):
                            malformed_chunks += 1
                            continue

                        reasoning = delta.get("reasoning_content")
                        if isinstance(reasoning, str):
                            reasoning_chars += len(reasoning)

                        text = delta.get("content")
                        if isinstance(text, str) and text:
                            content_parts.append(text)
                            yielded_any = True
                            yield ("delta", text)

                        for tc in delta.get("tool_calls") or []:
                            if not isinstance(tc, dict):
                                malformed_chunks += 1
                                continue
                            idx = tc.get("index", 0)
                            slot = tc_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if not isinstance(fn, dict):
                                malformed_chunks += 1
                                continue
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"]
                received = True
            except LLMError:
                raise                     # 4xx 之类,不重试
            except Exception as e:
                error_type = type(e).__name__
                if yielded_any or not _is_retriable_exc(e):
                    # 已经吐过字:不能偷偷重试(会出现重复的半截回答),如实抛出
                    raise LLMError(f"回复中断({type(e).__name__}): {e}") from e
                last_err = LLMError(f"连不上网关({type(e).__name__}): {e}")
                continue

            finally:
                previous = _stream_attempt(
                    started, response_status,
                    response_status == 200 and received and stream_error is None
                    and not "".join(content_parts).strip()
                    and not (tc_acc and all(slot.get("name") for slot in tc_acc.values())),
                    attempt, "openai", error_type, self._attempt_limit())

            if stream_error is not None:
                continue  # Discard even partial tool arguments from a failed attempt.

            # HTTP 200 不等于模型真的返回了可用结果。中转网关偶尔会给空流、
            # 只有 reasoning_content 的流，或损坏的 SSE；这些都可以安全重试，
            # 因为本次尝试没有给用户吐出可见正文。
            valid_tool_calls = bool(tc_acc) and all(
                bool(slot.get("name")) for slot in tc_acc.values()
            )
            has_visible_content = bool("".join(content_parts).strip())
            if not has_visible_content and not valid_tool_calls:
                reason = (
                    "网关只返回了推理片段，没有最终回复"
                    if reasoning_chars
                    else "网关返回空回复（HTTP 200，但没有文本或有效工具调用）"
                )
                last_err = LLMError(reason, kind="service")
                _LOGGER.warning(
                    "LLM 流式响应无有效内容 (%d/%d): status=%s, "
                    "sse_data=%s, done=%s, malformed=%d, reasoning_chars=%d",
                    attempt + 1,
                    self._attempt_limit(),
                    response_status,
                    saw_sse_data,
                    saw_done,
                    malformed_chunks,
                    reasoning_chars,
                )
                continue

            # 正常读完:把分片拼成标准 message
            msg: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(content_parts),
            }
            if valid_tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": slot["id"] or f"call_{idx}",
                        "type": "function",
                        "function": {"name": slot["name"], "arguments": slot["args"] or "{}"},
                    }
                    for idx, slot in sorted(tc_acc.items())
                ]
            yield ("done", msg)
            return

        raise LLMError(f"网关连续 {self._attempt_limit()} 次失败,最后一次:{last_err}")

    # ---------- 多模态:看屏幕截图 ----------
    async def vision(
        self,
        prompt: str,
        image_bytes: bytes,
        *,
        model: str | None = None,
        image_format: str | None = None,
    ) -> str:
        """给一张图 + 一段指令,返回文本。用于电脑操作模块理解屏幕。"""
        if image_format is None:
            image_format = "jpeg" if image_bytes.startswith(b"\xff\xd8\xff") else "png"
        b64 = base64.b64encode(image_bytes).decode()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/{image_format};base64,{b64}"},
                    },
                ],
            }
        ]
        started = time.perf_counter()
        try:
            msg = await self.chat(messages, model=model or self.cfg.chat_model)
            return msg.get("content", "") or ""
        finally:
            _LOGGER.info("Vision request elapsed_ms=%d image_format=%s image_bytes=%d",
                         round((time.perf_counter() - started) * 1000), image_format, len(image_bytes))

    # ---------- 嵌入(给记忆层用) ----------
    async def embed(self, texts: Iterable[str], *, model: str | None = None) -> list[list[float]]:
        payload = {"model": model or "text-embedding-3-small", "input": list(texts)}
        resp = await self._http().post("/embeddings", json=payload)
        if resp.status_code >= 400:
            raise LLMError(f"嵌入接口返回 {resp.status_code}: {resp.text[:300]}")
        return [item["embedding"] for item in resp.json()["data"]]


class ProviderFailoverClient(LLMClient):
    """Hub-only, three configured providers at most, first model once per call.

    No global 'healthy provider' state: concurrent requests remain independent,
    and every new model call starts with the first configured provider.
    """
    def __init__(self, cfg: LLMConfig):
        from dataclasses import replace
        super().__init__(cfg)
        primary = replace(cfg, providers=[], models=list(cfg.models))
        candidates = [primary] + [LLMConfig(
            base_url=item.base_url, api_key=item.api_key, protocol=item.protocol,
            chat_model=item.models[0] if item.models else "", models=list(item.models),
            timeout=cfg.timeout,
        ) for item in cfg.providers[:2]]
        self._provider_tags = [hashlib.sha256(json.dumps([
            item.base_url, item.protocol, item.chat_model,
        ]).encode()).hexdigest() for item in candidates]
        self._providers = [LLMClient(item) for item in candidates]
        for client in self._providers:
            client.max_attempts = 1

    def _failed(self) -> LLMError:
        return LLMError(
            f"网关失败：已按顺序尝试 {len(self._providers)} 个厂商的第一个模型，均未成功。"
            "请检查厂商连接、密钥、余额和模型配置。", kind="service")

    @staticmethod
    def _validate_reply(message):
        if not isinstance(message, dict) or not (str(message.get("content") or "").strip() or message.get("tool_calls")):
            raise LLMError("厂商返回空回复")
        return message

    def _messages_for_provider(self, messages, index):
        # Responses item IDs/encrypted reasoning belong to their originating
        # provider. Other providers receive portable text + paired tool calls.
        prepared = []
        for message in messages:
            source = message.get("_failover_provider")
            clean = {key: value for key, value in message.items() if key != "_failover_provider"}
            if source != self._provider_tags[index]:
                clean.pop("_responses_output", None)
            prepared.append(clean)
        return prepared

    def _mark_reply(self, message, index):
        if "_responses_output" in message:
            message["_failover_provider"] = self._provider_tags[index]
        return message

    async def chat(self, messages, *, model=None, **kwargs):
        for index, client in enumerate(self._providers):
            try:
                if not client.cfg.chat_model:
                    raise LLMError("厂商未配置首模型", kind="config")
                result = await client.chat(self._messages_for_provider(messages, index),
                                           model=client.cfg.chat_model, **kwargs)
                return self._mark_reply(self._validate_reply(result), index)
            except (LLMError, httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError, IndexError) as exc:
                # Never log URLs, API keys, raw responses, prompts or tool arguments.
                _LOGGER.warning("Hub provider attempt=%d/%d failed error_type=%s",
                                index + 1, len(self._providers), type(exc).__name__)
        raise self._failed()

    async def chat_stream_tools(self, messages, *, model=None, **kwargs):
        for index, client in enumerate(self._providers):
            emitted = False
            try:
                if not client.cfg.chat_model:
                    raise LLMError("厂商未配置首模型", kind="config")
                complete = False
                async with aclosing(client.chat_stream_tools(
                        self._messages_for_provider(messages, index), model=client.cfg.chat_model, **kwargs)) as stream:
                    async for kind, data in stream:
                        if kind == "delta" and data:
                            emitted = True
                        elif kind == "done":
                            self._mark_reply(self._validate_reply(data), index)
                            complete = emitted = True
                        yield kind, data
                if not complete:
                    raise LLMError("厂商流未完整结束")
                return
            except (LLMError, httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError, IndexError) as exc:
                if emitted:
                    raise LLMError("网关响应中断：已开始输出，未自动切换厂商以免重复回复。", kind="service") from None
                _LOGGER.warning("Hub stream provider attempt=%d/%d failed error_type=%s",
                                index + 1, len(self._providers), type(exc).__name__)
                if index + 1 < len(self._providers):
                    yield "retry", {"attempt": index + 2, "max_attempts": len(self._providers),
                        "delay_ms": 0, "provider_switch": True,
                        "text": f"第 {index + 1} 个厂商未成功，切换第 {index + 2} 个厂商的第一个模型"
                                f"（{index + 2}/{len(self._providers)}）。"}
        raise self._failed()

    async def chat_stream(self, messages, *, model=None, **kwargs):
        async for kind, data in self.chat_stream_tools(messages, **kwargs):
            if kind == "delta":
                yield data

    async def aclose(self):
        await asyncio.gather(super().aclose(), *(client.aclose() for client in self._providers))


def runtime_llm(cfg: LLMConfig, *, hub: bool) -> LLMClient:
    return ProviderFailoverClient(cfg) if hub else LLMClient(cfg)
