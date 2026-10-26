"""Unified personal, conversation and project memory facade."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from backend.core.config import MemoryConfig
from .embedder import make_default_embedder
from .vector_memory import VectorMemory


class MemoryModule:
    def __init__(self, cfg: MemoryConfig, data_dir: Path, llm, *, embedder_factory=None):
        self._embedder_factory = embedder_factory or make_default_embedder
        self.cfg = cfg
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._vector_loading = False
        self._vector_started = False
        self._vector_load_lock = threading.Lock()
        self._backend = self._init_backend(llm)

    def _init_backend(self, llm):
        backend = VectorMemory(
            None, llm, self.data_dir,
            batch_turns=getattr(self.cfg, "batch_turns", 5),
            flush_seconds=getattr(self.cfg, "flush_seconds", 180),
            project_roots=getattr(self.cfg, "project_roots", ()),
            max_project_files=getattr(self.cfg, "max_project_files", 5000),
        )
        print("[memory] SQLite 关键词记忆已就绪")
        return backend

    def _ensure_vectors(self) -> None:
        # ponytail: 首次实际使用记忆再加载 ONNX；空闲启动仅需 SQLite，不占用模型内存。
        model_name = str(getattr(self.cfg, "embed_model", "") or "").strip()
        if not model_name:
            return
        with self._vector_load_lock:
            if self._vector_started:
                return
            self._vector_started = self._vector_loading = True

        def load_vectors() -> None:
            try:
                embedder = self._embedder_factory(model_name)
                if embedder is not None:
                    updated = self._backend.set_embedder(embedder, model_name)
                    print(f"[memory] 本地向量已就绪({embedder.dim} 维,补齐 {updated} 条)")
            except Exception as error:
                print(f"[memory] 本地向量初始化失败，继续使用关键词记忆: {error}")
            finally:
                self._vector_loading = False

        try:
            threading.Thread(target=load_vectors, daemon=True).start()
        except Exception as error:
            with self._vector_load_lock:
                self._vector_started = self._vector_loading = False
            print(f"[memory] 无法启动向量加载线程，继续使用关键词记忆: {error}")

    async def recall(self, query: str, limit: int = 5) -> str:
        self._ensure_vectors()
        items = await self._backend_recall(query, limit)
        if not items:
            return ""
        return "\n".join(f"- {it}" for it in items)

    async def _backend_recall(self, query: str, limit: int):
        result = self._backend.recall(query, limit)
        if hasattr(result, "__await__"):
            return await result
        return result

    async def remember(self, messages: list[dict[str, Any]]) -> None:
        self._ensure_vectors()
        result = self._backend.remember(messages)
        if hasattr(result, "__await__"):
            await result

    async def flush(self) -> None:
        """把攒着还没提炼的对话立刻提炼掉(定时调 + 退出前调,防丢)。"""
        fn = getattr(self._backend, "flush", None)
        if fn is None:
            return
        try:
            result = fn()
            if hasattr(result, "__await__"):
                await result
        except Exception as e:
            print(f"[memory] flush 失败: {e}")

    @property
    def pending_count(self) -> int:
        return int(getattr(self._backend, "pending_count", 0) or 0)

    def search(self, query: str, namespace: str = "", limit: int = 8,
               source_prefix: str = "") -> list[dict[str, Any]]:
        self._ensure_vectors()
        return self._backend.search(
            query, namespace=namespace, limit=limit, source_prefix=source_prefix,
        )

    def add(self, text: str, namespace: str = "personal", pinned: bool = False) -> int:
        self._ensure_vectors()
        return self._backend.add(text, namespace=namespace, pinned=pinned)

    def list_memories(self, namespace: str = "personal", limit: int = 20) -> list[dict[str, Any]]:
        return self._backend.list_memories(namespace=namespace, limit=limit)

    def forget(self, memory_id: int) -> bool:
        return self._backend.forget(memory_id)

    def pin(self, memory_id: int, pinned: bool = True) -> bool:
        return self._backend.pin(memory_id, pinned=pinned)

    def update(self, memory_id: int, text: str) -> bool:
        self._ensure_vectors()
        return self._backend.update(memory_id, text)

    def index_project(self, root: str, max_files: int | None = None) -> dict[str, Any]:
        self._ensure_vectors()
        return self._backend.index_project(root, max_files=max_files)

    def refresh_projects(self) -> list[dict[str, Any]]:
        return self._backend.refresh_projects()

    def project_roots(self) -> list[str]:
        return self._backend.project_roots()

    def project_status(self) -> list[dict[str, Any]]:
        return self._backend.project_status()

    def remove_project(self, root: str) -> bool:
        return self._backend.remove_project(root)

    def export_snapshot(self) -> bytes:
        return self._backend.export_snapshot()

    def import_snapshot(self, snapshot: bytes) -> dict[str, int]:
        """把外部记忆快照合并进本库(worker → hub 同步用)。回退后端不支持时报错。"""
        backend = self._backend
        if not hasattr(backend, "import_snapshot"):
            raise RuntimeError("当前记忆后端不支持导入快照")
        return backend.import_snapshot(snapshot)

    @property
    def vector_ready(self) -> bool:
        return self._backend.embedder is not None and not self._vector_loading


class _JsonBackend:
    """极简回退:全文关键词匹配。仅在本地嵌入器都不可用时使用。"""

    def __init__(self, path: Path):
        self.path = path
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    def _load(self) -> list[str]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def recall(self, query: str, limit: int = 5) -> list[str]:
        items = self._load()
        q = query.lower()
        scored = [it for it in items if any(w in it.lower() for w in q.split())]
        return scored[:limit]

    def remember(self, messages: list[dict[str, Any]]) -> None:
        items = self._load()
        text = " | ".join(f"{m['role']}: {m['content']}" for m in messages if m.get("content"))
        items.append(text)
        self.path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
