"""工具注册与调度。

每个能力模块(Office、电脑操作、聊天、记忆…)把自己的能力注册成 Tool。
中枢通过 OpenAI function-calling 的 schema 把这些工具暴露给模型,
模型决定调哪个、传什么参数,再由这里派发执行。

有副作用的工具标 side_effect=True 供展示与审计；固有高风险工具另标 danger=True。
"""
from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


_RISK_CONFIRMATION_NOTICE = "（危险操作；风险审批模式下执行前会请求确认。）"
_CONVERSATION_APPROVAL_NOTICE = "同一对话最多请求一次危险操作授权；批准后复用，拒绝或中止后不再重复弹窗，可继续无需授权的步骤。"


def with_risk_confirmation(description: str) -> str:
    text = str(description).strip()
    if "执行前会请求确认" not in text:
        text += _RISK_CONFIRMATION_NOTICE
    elif "风险审批模式" not in text:
        text = text.replace("执行前会请求确认", "风险审批模式下执行前会请求确认")
    if _CONVERSATION_APPROVAL_NOTICE not in text:
        text += _CONVERSATION_APPROVAL_NOTICE
    return text


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    handler: Callable[..., Awaitable[Any]]
    side_effect: bool = False  # 会改变系统状态(用于前端展示区分)
    danger: bool = False  # 固有高风险；风险审批模式下执行前确认

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._lock = threading.RLock()

    def register(self, tool: Tool) -> None:
        with self._lock:
            if tool.name in self._tools:
                raise ValueError(f"工具重名: {tool.name}")
            self._tools[tool.name] = tool

    def tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        *,
        side_effect: bool = False,
        danger: bool = False,
    ):
        """装饰器写法,方便模块声明工具。"""

        def deco(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
            self.register(
                Tool(
                    name=name,
                    description=description,
                    parameters=parameters,
                    handler=fn,
                    side_effect=side_effect,
                    danger=danger,
                )
            )
            return fn

        return deco

    def get(self, name: str) -> Tool | None:
        with self._lock:
            return self._tools.get(name)

    def unregister(self, name: str) -> None:
        with self._lock:
            self._tools.pop(name, None)

    def all_schemas(self) -> list[dict[str, Any]]:
        with self._lock:
            tools = tuple(self._tools.values())
        return [tool.to_openai_schema() for tool in tools]

    def capabilities(self) -> list[dict[str, Any]]:
        with self._lock:
            tools = tuple(self._tools.values())
        return [
            {
                "schema": tool.to_openai_schema(),
                "side_effect": tool.side_effect,
                "danger": tool.danger,
            }
            for tool in tools
        ]

    def names(self) -> list[str]:
        with self._lock:
            return list(self._tools)

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        with self._lock:
            tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"未知工具: {name}")
        # 容错:有些模型(如 opus)调无参/少参工具时会塞多余占位参数(如 raw_arguments),
        # 按 handler 的真实签名过滤,除非 handler 显式接受 **kwargs。
        sig = inspect.signature(tool.handler)
        accepts_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if not accepts_var_kw:
            allowed = set(sig.parameters.keys())
            arguments = {k: v for k, v in (arguments or {}).items() if k in allowed}
        result = tool.handler(**arguments)
        if inspect.isawaitable(result):
            result = await result
        return result
