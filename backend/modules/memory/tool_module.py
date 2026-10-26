"""Agent tools for explicit memory management and project indexing."""
from __future__ import annotations

import asyncio
import json
from typing import Callable

from backend.core.tools import ToolRegistry
from .memory_module import MemoryModule

MemoryGetter = Callable[[], MemoryModule | None]
SharedContextGetter = Callable[[], dict | None]
SharedAction = Callable[..., object]


def _memory(get_memory: MemoryGetter) -> MemoryModule:
    memory = get_memory()
    if memory is None:
        raise RuntimeError("记忆模块还在初始化,请稍后再试。")
    return memory


def register_memory_tools(registry: ToolRegistry, get_memory: MemoryGetter) -> None:
    @registry.tool(
        name="memory_search",
        description="主动搜索长期记忆。适合查用户偏好、规则、历史决定和已记录事实。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "namespace": {
                    "type": "string",
                    "enum": ["", "personal", "history", "project"],
                    "description": "留空搜索全部记忆",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    )
    async def memory_search(query: str, namespace: str = "", limit: int = 8) -> str:
        memory = _memory(get_memory)
        rows = await asyncio.to_thread(
            lambda: memory.search(query, namespace=namespace, limit=limit)
        )
        return json.dumps(rows, ensure_ascii=False) if rows else "没有找到相关记忆。"

    @registry.tool(
        name="memory_add",
        description="明确记录一条需要长期保留的事实、偏好、规则或决定。",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "pinned": {"type": "boolean", "description": "重要规则可固定"},
            },
            "required": ["text"],
        },
        side_effect=True,
    )
    async def memory_add(text: str, pinned: bool = False) -> str:
        memory_id = await asyncio.to_thread(_memory(get_memory).add, text, "personal", pinned)
        return f"已记录长期记忆 #{memory_id}。"

    @registry.tool(
        name="memory_list",
        description="列出最近的长期记忆,用于检查小A目前记住了什么。",
        parameters={
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string", "enum": ["", "personal", "history", "project"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    )
    async def memory_list(namespace: str = "personal", limit: int = 20) -> str:
        rows = await asyncio.to_thread(_memory(get_memory).list_memories, namespace, limit)
        return json.dumps(rows, ensure_ascii=False) if rows else "当前没有记忆。"

    @registry.tool(
        name="memory_forget",
        description="按记忆编号停用一条错误或过期记忆。先用 memory_list/search 找编号。",
        parameters={
            "type": "object",
            "properties": {"memory_id": {"type": "integer"}},
            "required": ["memory_id"],
        },
        side_effect=True,
    )
    async def memory_forget(memory_id: int) -> str:
        ok = await asyncio.to_thread(_memory(get_memory).forget, memory_id)
        return "已忘记。" if ok else "没有找到这条有效记忆。"

    @registry.tool(
        name="memory_pin",
        description="固定或取消固定一条重要记忆。固定记忆检索时优先。",
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
                "pinned": {"type": "boolean"},
            },
            "required": ["memory_id", "pinned"],
        },
        side_effect=True,
    )
    async def memory_pin(memory_id: int, pinned: bool) -> str:
        ok = await asyncio.to_thread(_memory(get_memory).pin, memory_id, pinned)
        return "已更新固定状态。" if ok else "没有找到这条有效记忆。"

    @registry.tool(
        name="memory_update",
        description="修改一条已有的长期记忆。先用 memory_list/search 找到编号。项目索引内容应修改原文件后重建索引。",
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
                "text": {"type": "string"},
            },
            "required": ["memory_id", "text"],
        },
        side_effect=True,
    )
    async def memory_update(memory_id: int, text: str) -> str:
        ok = await asyncio.to_thread(_memory(get_memory).update, memory_id, text)
        return "已更新记忆。" if ok else "没有找到可修改的长期记忆。"


def register_project_tools(registry: ToolRegistry, get_memory: MemoryGetter) -> None:
    @registry.tool(
        name="project_index",
        description=(
            "把一个项目目录增量加入项目记忆。支持常见代码/文本、Word 和 Excel;"
            "重复执行只更新发生变化的文件。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "项目目录完整路径"},
                "max_files": {"type": "integer", "minimum": 1, "maximum": 20000},
            },
            "required": ["path"],
        },
        side_effect=True,
    )
    async def project_index(path: str, max_files: int | None = None) -> str:
        result = await asyncio.to_thread(_memory(get_memory).index_project, path, max_files)
        return json.dumps(result, ensure_ascii=False)

    @registry.tool(
        name="project_search",
        description="搜索已建立索引的项目内容,返回原文件路径和行号/工作表位置。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string", "description": "可选,只搜这个项目目录"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    )
    async def project_search(query: str, path: str = "", limit: int = 8) -> str:
        memory = _memory(get_memory)
        rows = await asyncio.to_thread(
            lambda: memory.search(
                query, namespace="project", limit=limit, source_prefix=path,
            )
        )
        return json.dumps(rows, ensure_ascii=False) if rows else "项目记忆中没有找到相关内容。"

    @registry.tool(
        name="project_list",
        description="列出已经加入项目记忆的根目录、文件数、片段数和最后索引时间。",
        parameters={"type": "object", "properties": {}},
    )
    async def project_list() -> str:
        rows = await asyncio.to_thread(_memory(get_memory).project_status)
        return json.dumps(rows, ensure_ascii=False) if rows else "还没有建立项目索引。"

    @registry.tool(
        name="project_remove",
        description="移除一个项目目录及其索引片段，不删除项目原文件。",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "项目根目录完整路径"}},
            "required": ["path"],
        },
        side_effect=True,
    )
    async def project_remove(path: str) -> str:
        ok = await asyncio.to_thread(_memory(get_memory).remove_project, path)
        return "已移除项目索引，原文件未删除。" if ok else "没有找到这个项目索引。"


def register_shared_tools(registry: ToolRegistry, get_context: SharedContextGetter, action_handler: SharedAction) -> None:
    """Expose natural-language project memory operations after a library is activated."""

    @registry.tool(
        name="shared_memory_action",
        description=(
            "在已激活的项目共享记忆库中查询、列出或维护资料。先理解用户意图再调用；"
            "只有章程指定的本库管理员可以保存、修改、删除，后端会再次校验身份。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["search", "list", "save", "update", "delete"]},
                "query": {"type": "string"},
                "content": {"type": "string"},
                "memory_id": {"type": "integer", "minimum": 1},
            },
            "required": ["action"],
        },
        side_effect=True,
    )
    async def shared_memory_action(action: str, query: str = "", content: str = "", memory_id: int = 0) -> str:
        if not get_context():
            return "当前对话没有激活项目共享记忆库，不能访问项目资料。"
        result = action_handler(action=action, query=query, content=content, memory_id=memory_id)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)
