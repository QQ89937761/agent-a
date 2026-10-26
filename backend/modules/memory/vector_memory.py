"""SQLite-backed personal and project memory with optional local embeddings."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from array import array
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .embedder import Embedder

_EXTRACT_PROMPT = """从下面对话中提取值得长期记住的稳定事实。
只保留用户偏好、身份、长期项目、常用路径、规则和重要数据。
排除一次性状态、操作流水和重复事实。没有就回复“无”。
每条一行，不要编号。

对话:
{convo}
"""

_TRIVIAL_MEMORY_TEXT = {
    "好", "好的", "好吧", "嗯", "嗯嗯", "收到", "知道了", "明白了", "谢谢", "感谢",
    "确认", "继续", "行", "可以", "没问题", "ok", "okay", "thanks", "thank you",
}
_MEMORY_ENTITY_RE = re.compile(
    r"(?:\b\d{1,3}(?:\.\d{1,3}){3}\b|\b\d{2,5}\s*端口\b|"
    r"https?://\S+|(?:[A-Za-z]:[\\/]|/)\S+)", re.IGNORECASE,
)
_MEMORY_FORCE_RE = re.compile(r"记住|别忘|以后|每次|always", re.IGNORECASE)

_TEXT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css",
    ".json", ".jsonl", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".csv", ".sql",
    ".ps1", ".bat", ".cmd", ".java", ".go", ".rs", ".c", ".h", ".cpp", ".hpp",
}
_DOCUMENT_EXTENSIONS = {".docx", ".xlsx"}
_SKIP_DIRS = {
    ".git", ".svn", ".hg", ".venv", "venv", "node_modules", "dist", "build", "work",
    "__pycache__", ".idea", ".vscode", "XiaoA-data",
}


class VectorMemory:
    """One durable store for personal facts, conversation snippets and projects."""

    # history 快照只保留最近这么多条。快照每批写、从不被业务删除,若不封顶会无限增长:
    # 一是把库撑大,二是新近快照挤占检索候选池(recent pool 按 updated_at DESC 取),
    # 把索引更早、updated_at 更旧的 project chunk 挤出候选窗口 → 项目检索悄悄查不到还不报错。
    _HISTORY_RETENTION = 4000

    def __init__(
        self,
        embedder: Embedder | None,
        llm,
        data_dir: Path,
        batch_turns: int = 5,
        flush_seconds: int = 180,
        project_roots: Iterable[str] = (),
        max_project_files: int = 5000,
    ):
        self.embedder = embedder
        self._get_llm = llm if callable(llm) else lambda: llm
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "memory.sqlite3"
        self.batch_turns = max(int(batch_turns), 1)
        self.flush_seconds = max(int(flush_seconds), 0)
        self.max_project_files = max(int(max_project_files), 1)
        self._embed_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: list[list[dict[str, Any]]] = []
        self._last_flush = time.time()
        self._init_db()
        self._migrate_legacy()
        for root in project_roots:
            try:
                self.add_project_root(root)
            except Exception as e:
                print(f"[memory] 跳过项目目录 {root}: {e}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _db(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    namespace TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    source_path TEXT NOT NULL DEFAULT '',
                    source_ref TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL,
                    vector BLOB,
                    importance REAL NOT NULL DEFAULT 0.5,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memories_namespace
                    ON memories(namespace, active, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_memories_source
                    ON memories(source_path, active);
                CREATE INDEX IF NOT EXISTS idx_memories_hash
                    ON memories(namespace, content_hash, active);
                CREATE TABLE IF NOT EXISTS project_files (
                    path TEXT PRIMARY KEY,
                    root_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    indexed_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_project_files_root ON project_files(root_path);
                CREATE TABLE IF NOT EXISTS projects (
                    root_path TEXT PRIMARY KEY,
                    added_at INTEGER NOT NULL,
                    last_indexed_at INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS memory_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

    @staticmethod
    def _pack_vector(vec: list[float] | None) -> bytes | None:
        return array("f", vec).tobytes() if vec else None

    @staticmethod
    def _unpack_vector(raw: bytes | None) -> list[float]:
        if not raw:
            return []
        values = array("f")
        values.frombytes(raw)
        return list(values)

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na and nb else 0.0

    def _embed(self, texts: list[str]) -> list[list[float] | None]:
        if not texts or self.embedder is None:
            return [None] * len(texts)
        try:
            with self._embed_lock:
                return list(self.embedder.embed(texts))
        except Exception as e:
            print(f"[memory] 向量化失败,本次使用关键词检索: {e}")
            return [None] * len(texts)

    def set_embedder(self, embedder: Embedder, model_name: str) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT value FROM memory_meta WHERE key='embedding_model'"
            ).fetchone()
            if row is None or row["value"] != model_name:
                conn.execute("UPDATE memories SET vector=NULL WHERE active=1")
                conn.execute(
                    "INSERT OR REPLACE INTO memory_meta(key, value) VALUES('embedding_model', ?)",
                    (model_name,),
                )
        self.embedder = embedder
        updated = 0
        while True:
            with self._db() as conn:
                rows = conn.execute(
                    "SELECT id, text FROM memories WHERE active=1 AND vector IS NULL LIMIT 64"
                ).fetchall()
            if not rows:
                return updated
            vectors = self._embed([row["text"] for row in rows])
            pairs = [(self._pack_vector(vector), int(row["id"]))
                     for row, vector in zip(rows, vectors) if vector]
            if not pairs:
                return updated
            with self._db() as conn:
                conn.executemany("UPDATE memories SET vector=? WHERE id=?", pairs)
            updated += len(pairs)

    def _insert(
        self,
        conn: sqlite3.Connection,
        *,
        namespace: str,
        kind: str,
        text: str,
        vector: list[float] | None = None,
        source_path: str = "",
        source_ref: str = "",
        importance: float = 0.5,
        pinned: bool = False,
    ) -> int | None:
        text = text.strip()
        if not text:
            return None
        content_hash = self._hash(text)
        existing = conn.execute(
            """SELECT id FROM memories WHERE namespace=? AND content_hash=?
               AND source_path=? AND source_ref=? AND active=1 LIMIT 1""",
            (namespace, content_hash, source_path, source_ref),
        ).fetchone()
        if existing:
            return int(existing["id"])
        now = int(time.time())
        cur = conn.execute(
            """INSERT INTO memories
               (namespace, kind, text, source_path, source_ref, content_hash, vector,
                importance, pinned, active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                namespace, kind, text, source_path, source_ref, content_hash,
                self._pack_vector(vector), max(0.0, min(float(importance), 1.0)),
                1 if pinned else 0, now, now,
            ),
        )
        return int(cur.lastrowid)

    def _migrate_legacy(self) -> None:
        with self._db() as conn:
            if conn.execute("SELECT 1 FROM memory_meta WHERE key='legacy_v1_migrated'").fetchone():
                return
            vector_path = self.data_dir / "vector_memory.jsonl"
            if vector_path.exists():
                for line in vector_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        rec = json.loads(line)
                        text = str(rec.get("text") or "").strip()
                        vec = rec.get("vec") if isinstance(rec.get("vec"), list) else None
                        if text:
                            self._insert(
                                conn, namespace="personal",
                                kind=str(rec.get("kind") or "fact"), text=text, vector=vec,
                            )
                    except Exception:
                        continue
            simple_path = self.data_dir / "simple_memory.json"
            if simple_path.exists():
                try:
                    items = json.loads(simple_path.read_text(encoding="utf-8"))
                except Exception:
                    items = []
                texts = [str(item).strip() for item in items if str(item).strip()]
                vectors = self._embed(texts)
                for text, vec in zip(texts, vectors):
                    self._insert(conn, namespace="personal", kind="legacy", text=text, vector=vec)
            conn.execute(
                "INSERT OR REPLACE INTO memory_meta(key, value) VALUES('legacy_v1_migrated', ?)",
                (str(int(time.time())),),
            )

    @staticmethod
    def _worth_extracting(messages: list[dict[str, Any]]) -> bool:
        user_text = " ".join(
            m.get("content", "") for m in messages
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        ).strip()
        normalized = re.sub(r"[\s\u3000，。！？,.!?、:：;；]+", "", user_text).lower()
        if len(normalized) <= 4 or normalized in _TRIVIAL_MEMORY_TEXT:
            return False
        # Facts and operational details are more reliable than a hand-maintained
        # signal-word list; collect non-trivial user statements by default.
        return True

    @staticmethod
    def _should_flush_now(messages: list[dict[str, Any]]) -> bool:
        user_text = " ".join(
            str(m.get("content") or "") for m in messages
            if m.get("role") == "user"
        ).strip()
        return bool(_MEMORY_FORCE_RE.search(user_text) or _MEMORY_ENTITY_RE.search(user_text))

    async def extract_facts(self, messages: list[dict[str, Any]]) -> list[str]:
        llm = self._get_llm()
        if llm is None:
            return []
        convo = "\n".join(f"{m['role']}: {m['content']}" for m in messages if m.get("content"))
        msg = await llm.chat(
            [{"role": "user", "content": _EXTRACT_PROMPT.format(convo=convo)}],
            temperature=0.1,
        )
        text = (msg.get("content") or "").strip()
        facts = [line.strip("-• \t") for line in text.splitlines() if line.strip()]
        return [fact for fact in facts if fact.lower() not in ("无", "none", "空")]

    async def remember(self, messages: list[dict[str, Any]]) -> list[str]:
        if not self._worth_extracting(messages):
            return []
        with self._pending_lock:
            self._pending.append(list(messages))
            enough = len(self._pending) >= self.batch_turns
        timed_out = self.flush_seconds > 0 and time.time() - self._last_flush >= self.flush_seconds
        immediate = self._should_flush_now(messages)
        return await self.flush() if enough or timed_out or immediate else []

    async def flush(self) -> list[str]:
        with self._pending_lock:
            if not self._pending:
                return []
            batch, self._pending = self._pending, []
            self._last_flush = time.time()

        # 先无条件落对话快照:即便事实提炼失败,这几轮对话也不会丢。
        # 快照按 0.98 高阈值去重,所以事实重试导致的重复写入会被自动挡掉。
        snippets = [self._make_snippet(turn) for turn in batch]
        snippets = [snippet for snippet in snippets if len(snippet) >= 18]
        if snippets:
            def persist_snippets() -> None:
                snippet_vectors = self._embed(snippets)
                with self._db() as conn:
                    for snippet, vec in zip(snippets, snippet_vectors):
                        if not self._is_duplicate(conn, vec, namespaces=("history",), threshold=0.98):
                            self._insert(
                                conn, namespace="history", kind="conversation", text=snippet, vector=vec,
                            )
                    # 写完这批快照顺手裁剪,把 history 封在保留上限内,不让它无限堆积挤掉 project 索引。
                    self._prune_history(conn)

            await asyncio.to_thread(persist_snippets)

        flat = [message for turn in batch for message in turn]
        try:
            facts = await self.extract_facts(flat)
        except Exception as e:
            # 事实提炼失败:把整批放回待办开头,下轮重试;不要吞掉。
            with self._pending_lock:
                self._pending[:0] = batch
            print(f"[memory] 事实提炼失败,已把 {len(batch)} 轮对话放回队列等待重试: {e}")
            return []

        def persist_facts() -> list[str]:
            vectors = self._embed(facts)
            new_facts: list[str] = []
            with self._db() as conn:
                for fact, vec in zip(facts, vectors):
                    if self._is_duplicate(conn, vec, namespaces=("personal",)):
                        continue
                    self._insert(conn, namespace="personal", kind="fact", text=fact, vector=vec)
                    new_facts.append(fact)
            return new_facts

        return await asyncio.to_thread(persist_facts)

    def _is_duplicate(
        self,
        conn: sqlite3.Connection,
        vec: list[float] | None,
        threshold: float = 0.93,
        namespaces: tuple[str, ...] = ("personal",),
    ) -> bool:
        # 只跟指定命名空间比对。事实(personal)不要跟 history 快照比:
        # 快照本身就包含刚提炼出来的事实,拿它当对照会把正确的新事实误判成重复丢掉。
        if not vec or not namespaces:
            return False
        placeholders = ",".join("?" for _ in namespaces)
        rows = conn.execute(
            f"SELECT vector FROM memories WHERE namespace IN ({placeholders}) AND active=1",
            tuple(namespaces),
        )
        return any(self._cosine(vec, self._unpack_vector(row["vector"])) >= threshold for row in rows)

    def _prune_history(self, conn: sqlite3.Connection) -> int:
        # 只留最近 _HISTORY_RETENTION 条 history 快照,超出的旧快照直接硬删(含向量),
        # 避免 history 无限膨胀挤占 project 检索。只动 history 命名空间,不碰 personal/project。
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE namespace='history' AND active=1"
        ).fetchone()
        total = int(row["n"]) if row else 0
        if total <= self._HISTORY_RETENTION:
            return 0
        # 找到"第 N 新"那条的 id 作为分界,删掉比它更旧的(updated_at 更小、或同刻 id 更小)。
        cutoff = conn.execute(
            """SELECT id, updated_at FROM memories
               WHERE namespace='history' AND active=1
               ORDER BY updated_at DESC, id DESC LIMIT 1 OFFSET ?""",
            (self._HISTORY_RETENTION - 1,),
        ).fetchone()
        if cutoff is None:
            return 0
        cur = conn.execute(
            """DELETE FROM memories
               WHERE namespace='history' AND active=1
                 AND (updated_at < ? OR (updated_at = ? AND id < ?))""",
            (cutoff["updated_at"], cutoff["updated_at"], cutoff["id"]),
        )
        return cur.rowcount

    @staticmethod
    def _make_snippet(messages: list[dict[str, Any]], max_len: int = 500) -> str:
        parts = []
        for message in messages:
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            role = {"user": "我", "assistant": "小A"}.get(message.get("role"), message.get("role"))
            parts.append(f"{role}:{content.strip()}")
        return " | ".join(parts)[:max_len]

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def add(self, text: str, namespace: str = "personal", pinned: bool = False) -> int:
        namespace = namespace if namespace in ("personal", "history", "project") else "personal"
        vector = self._embed([text])[0]
        with self._db() as conn:
            memory_id = self._insert(
                conn, namespace=namespace, kind="manual", text=text, vector=vector, pinned=pinned,
                importance=0.9 if pinned else 0.6,
            )
        return int(memory_id or 0)

    def list_memories(self, namespace: str = "personal", limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        where, args = "active=1", []
        if namespace:
            where += " AND namespace=?"
            args.append(namespace)
        with self._db() as conn:
            rows = conn.execute(
                f"""SELECT id, namespace, kind, text, source_path, source_ref, pinned,
                           importance, created_at, updated_at
                    FROM memories WHERE {where}
                    ORDER BY pinned DESC, updated_at DESC LIMIT ?""",
                (*args, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def forget(self, memory_id: int) -> bool:
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE memories SET active=0, updated_at=? WHERE id=? AND active=1",
                (int(time.time()), int(memory_id)),
            )
        return cur.rowcount > 0

    def pin(self, memory_id: int, pinned: bool = True) -> bool:
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE memories SET pinned=?, updated_at=? WHERE id=? AND active=1",
                (1 if pinned else 0, int(time.time()), int(memory_id)),
            )
        return cur.rowcount > 0

    def update(self, memory_id: int, text: str) -> bool:
        text = text.strip()
        if not text:
            raise ValueError("记忆内容不能为空。")
        vector = self._embed([text])[0]
        with self._db() as conn:
            cur = conn.execute(
                """UPDATE memories SET text=?, content_hash=?, vector=?, updated_at=?
                   WHERE id=? AND active=1 AND namespace!='project'""",
                (
                    text, self._hash(text), self._pack_vector(vector), int(time.time()),
                    int(memory_id),
                ),
            )
        return cur.rowcount > 0

    @staticmethod
    def _lexical_score(query: str, text: str) -> float:
        query, text = query.lower().strip(), text.lower()
        if not query:
            return 0.0
        if query in text:
            return 1.0
        terms = [term for term in query.replace("/", " ").replace("\\", " ").split() if term]
        term_score = sum(1 for term in terms if term in text) / len(terms) if terms else 0.0
        compact_query = "".join(query.split())
        compact_text = "".join(text.split())
        if len(compact_query) < 2:
            return term_score
        pairs = {compact_query[i:i + 2] for i in range(len(compact_query) - 1)}
        pair_score = sum(1 for pair in pairs if pair in compact_text) / len(pairs)
        return max(term_score, pair_score * 0.85)

    # 候选集上限:粗筛后拿去逐行算余弦的行数,压到几百级别而不是全表两万行。
    _CANDIDATE_LEXICAL_POOL = 400
    _CANDIDATE_RECENT_POOL = 200

    @staticmethod
    def _query_patterns(query: str) -> list[str]:
        """把查询拆成给 SQL LIKE 用的粗筛模式:整串 + 分词 + 紧凑串的二元组。"""
        low = query.lower()
        patterns: dict[str, None] = {low: None}
        for term in low.replace("/", " ").replace("\\", " ").split():
            if term:
                patterns[term] = None
        compact = "".join(low.split())
        for i in range(len(compact) - 1):
            patterns[compact[i:i + 2]] = None
            if len(patterns) >= 24:  # 二元组封顶,避免 OR 子句爆炸
                break
        return list(patterns)

    @staticmethod
    def _recency_score(updated_at: int, now: int, half_life_days: float = 30.0) -> float:
        """updated_at 越新越接近 1;半衰期 30 天。用于让同主题里新事实压过旧事实。"""
        age = max(0, now - int(updated_at or 0))
        return 0.5 ** (age / (half_life_days * 86400.0))

    def _gather_candidates(
        self, conn: sqlite3.Connection, query: str, where: str, args: list,
    ) -> list[sqlite3.Row]:
        """SQL 粗筛:词法命中的一批 + 高重要度/新近的一批,合并去重,压到几百行。"""
        cols = ("id, namespace, kind, text, source_path, source_ref, vector, "
                "importance, pinned, updated_at")
        patterns = self._query_patterns(query)
        like_clause = " OR ".join("LOWER(text) LIKE ?" for _ in patterns)
        like_args = [f"%{p}%" for p in patterns]
        lexical_rows = conn.execute(
            f"""SELECT {cols} FROM memories
                WHERE {where} AND ({like_clause})
                ORDER BY pinned DESC, importance DESC, updated_at DESC LIMIT ?""",
            (*args, *like_args, self._CANDIDATE_LEXICAL_POOL),
        ).fetchall()
        # 兜底:纯语义命中(与查询无字面重叠)和 pinned/新近条目,靠这批保证进候选。
        recent_rows = conn.execute(
            f"""SELECT {cols} FROM memories
                WHERE {where}
                ORDER BY pinned DESC, importance DESC, updated_at DESC LIMIT ?""",
            (*args, self._CANDIDATE_RECENT_POOL),
        ).fetchall()
        merged: dict[int, sqlite3.Row] = {}
        for row in list(lexical_rows) + list(recent_rows):
            merged[int(row["id"])] = row
        return list(merged.values())

    def search(
        self,
        query: str,
        *,
        namespace: str = "",
        limit: int = 5,
        source_prefix: str = "",
    ) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        limit = max(1, min(int(limit), 30))
        where, args = "active=1", []
        if namespace:
            where += " AND namespace=?"
            args.append(namespace)
        if source_prefix:
            where += " AND source_path LIKE ?"
            args.append(str(Path(source_prefix).resolve()) + "%")
        with self._db() as conn:
            rows = self._gather_candidates(conn, query, where, args)
        qvec = self._embed([query])[0]
        now = int(time.time())
        scored = []
        for row in rows:
            lexical = self._lexical_score(query, row["text"])
            semantic = self._cosine(qvec, self._unpack_vector(row["vector"])) if qvec else 0.0
            if lexical <= 0 and semantic < 0.25:
                continue
            recency = self._recency_score(row["updated_at"], now)
            score = (semantic * 0.78 + lexical * 0.18
                     + float(row["importance"]) * 0.03 + recency * 0.06)
            score += 0.05 if row["pinned"] else 0.0
            item = dict(row)
            item.pop("vector", None)
            item["score"] = round(score, 4)
            scored.append(item)
        scored.sort(key=lambda item: (item["score"], item["updated_at"]), reverse=True)
        return scored[:limit]

    @staticmethod
    def _format_result(item: dict[str, Any]) -> str:
        if item["namespace"] == "project":
            source = item.get("source_path") or "未知文件"
            ref = item.get("source_ref") or ""
            return f"[项目 {source}{':' + ref if ref else ''}] {item['text']}"
        label = "往事" if item["namespace"] == "history" else "记忆"
        return f"[{label} #{item['id']}] {item['text']}"

    async def recall(self, query: str, limit: int = 5) -> list[str]:
        # search 会跑 SQL 粗筛 + 逐行余弦,是同步重活。挪到线程里跑,
        # 别在事件循环上阻塞——每轮对话 agent.py 都会 await 这个函数。
        items = await asyncio.to_thread(self.search, query, limit=limit + 2)
        results = [self._format_result(item) for item in items]
        # Include not-yet-flushed turns so a fact is usable in the same session.
        pending = await asyncio.to_thread(self._pending_matches, query, limit)
        for item in pending:
            if item not in results:
                results.append(item)
        return results[:max(1, int(limit))]

    def _pending_matches(self, query: str, limit: int) -> list[str]:
        with self._pending_lock:
            turns = list(self._pending)
        scored: list[tuple[float, str]] = []
        for turn in turns:
            text = self._make_snippet(turn)
            score = self._lexical_score(query, text)
            if score > 0:
                scored.append((score, f"[待办记忆] {text}"))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [text for _score, text in scored[:max(1, int(limit))]]

    def add_project_root(self, root: str | os.PathLike) -> Path:
        path = Path(root).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"项目目录不存在: {path}")
        with self._db() as conn:
            existing = [Path(row["root_path"]) for row in conn.execute(
                "SELECT root_path FROM projects"
            )]
            for current in existing:
                if path == current:
                    return path
                if path.is_relative_to(current) or current.is_relative_to(path):
                    raise ValueError(f"项目目录与已有索引重叠: {current}")
            conn.execute(
                "INSERT OR IGNORE INTO projects(root_path, added_at) VALUES(?, ?)",
                (str(path), int(time.time())),
            )
        return path

    def project_roots(self) -> list[str]:
        with self._db() as conn:
            return [row["root_path"] for row in conn.execute(
                "SELECT root_path FROM projects ORDER BY added_at"
            )]

    def project_status(self) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                """SELECT p.root_path, p.added_at, p.last_indexed_at,
                          COUNT(DISTINCT f.path) AS file_count,
                          COUNT(m.id) AS chunk_count
                   FROM projects p
                   LEFT JOIN project_files f ON f.root_path=p.root_path
                   LEFT JOIN memories m ON m.source_path=f.path
                       AND m.namespace='project' AND m.active=1
                   GROUP BY p.root_path, p.added_at, p.last_indexed_at
                   ORDER BY p.added_at"""
            ).fetchall()
        return [dict(row) for row in rows]

    def remove_project(self, root: str | os.PathLike) -> bool:
        root_path = str(Path(root).expanduser().resolve())
        with self._db() as conn:
            paths = [row["path"] for row in conn.execute(
                "SELECT path FROM project_files WHERE root_path=?", (root_path,)
            )]
            for path in paths:
                conn.execute(
                    "DELETE FROM memories WHERE namespace='project' AND source_path=?", (path,)
                )
            conn.execute("DELETE FROM project_files WHERE root_path=?", (root_path,))
            cur = conn.execute("DELETE FROM projects WHERE root_path=?", (root_path,))
        return cur.rowcount > 0

    @staticmethod
    def _decode_text(path: Path) -> str:
        raw = path.read_bytes()
        for encoding in ("utf-8", "utf-8-sig", "gb18030", "gbk"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _chunk_lines(lines: list[str], max_chars: int = 1200) -> list[tuple[str, str]]:
        chunks, current, start = [], [], 1
        size = 0
        for number, line in enumerate(lines, 1):
            if current and size + len(line) + 1 > max_chars:
                chunks.append((f"L{start}-L{number - 1}", "\n".join(current).strip()))
                current, start, size = [], number, 0
            if len(line) > max_chars:
                if current:
                    chunks.append((f"L{start}-L{number - 1}", "\n".join(current).strip()))
                    current, size = [], 0
                for offset in range(0, len(line), max_chars):
                    chunks.append((f"L{number}", line[offset:offset + max_chars]))
                start = number + 1
                continue
            current.append(line)
            size += len(line) + 1
        if current:
            chunks.append((f"L{start}-L{start + len(current) - 1}", "\n".join(current).strip()))
        return [(ref, text) for ref, text in chunks if text]

    def _read_chunks(self, path: Path) -> list[tuple[str, str]]:
        suffix = path.suffix.lower()
        if suffix in _TEXT_EXTENSIONS:
            return self._chunk_lines(self._decode_text(path).splitlines())
        if suffix == ".docx":
            from docx import Document

            paragraphs = [paragraph.text for paragraph in Document(path).paragraphs]
            return self._chunk_lines(paragraphs)
        if suffix == ".xlsx":
            from openpyxl import load_workbook

            workbook = load_workbook(path, read_only=True, data_only=True)
            chunks = []
            try:
                for sheet in workbook.worksheets:
                    lines = ["\t".join("" if value is None else str(value) for value in row)
                             for row in sheet.iter_rows(values_only=True)]
                    for ref, text in self._chunk_lines(lines):
                        chunks.append((f"{sheet.title}!{ref}", text))
            finally:
                workbook.close()
            return chunks
        return []

    def index_project(self, root: str | os.PathLike, max_files: int | None = None) -> dict[str, Any]:
        root_path = self.add_project_root(root)
        max_files = max(1, min(int(max_files or self.max_project_files), 20000))
        indexed = unchanged = removed = failed = scanned = 0
        seen: set[str] = set()
        # 整个扫描复用一条连接:不再每文件 connect 一次(5000 文件=5000 次开库)。
        # 写操作逐文件显式 commit,SELECT 在 WAL 下不阻塞;失败按文件回滚,不留半截状态。
        conn = self._connect()
        try:
            for current_root, dirs, files in os.walk(root_path):
                dirs[:] = [directory for directory in dirs if directory not in _SKIP_DIRS]
                for filename in files:
                    if scanned >= max_files:
                        break
                    path = Path(current_root) / filename
                    if path.suffix.lower() not in _TEXT_EXTENSIONS | _DOCUMENT_EXTENSIONS:
                        continue
                    try:
                        stat = path.stat()
                        if stat.st_size > 2_000_000:
                            continue
                        scanned += 1
                        resolved = str(path.resolve())
                        seen.add(resolved)
                        previous = conn.execute(
                            "SELECT content_hash, mtime_ns, size FROM project_files WHERE path=?",
                            (resolved,),
                        ).fetchone()
                        # 真增量:先比 mtime_ns + size。没变就跳过,连文件都不读、更不算哈希。
                        if (previous is not None
                                and int(previous["mtime_ns"]) == stat.st_mtime_ns
                                and int(previous["size"]) == stat.st_size):
                            unchanged += 1
                            continue
                        # mtime/size 变了才读盘算 hash 确认内容是否真变(有的文件被 touch 但内容没动)。
                        digest = hashlib.sha256(path.read_bytes()).hexdigest()
                        if previous is not None and previous["content_hash"] == digest:
                            # 内容没变、只是元数据漂移:回填 mtime/size,下次就能靠元数据直接跳过。
                            conn.execute(
                                """UPDATE project_files SET mtime_ns=?, size=?, indexed_at=?
                                   WHERE path=?""",
                                (stat.st_mtime_ns, stat.st_size, int(time.time()), resolved),
                            )
                            conn.commit()
                            unchanged += 1
                            continue
                        chunks = self._read_chunks(path)
                        vectors = self._embed([text for _, text in chunks])
                        conn.execute(
                            "DELETE FROM memories WHERE namespace='project' AND source_path=?",
                            (resolved,),
                        )
                        for (ref, text), vector in zip(chunks, vectors):
                            self._insert(
                                conn, namespace="project", kind="chunk", text=text, vector=vector,
                                source_path=resolved, source_ref=ref, importance=0.55,
                            )
                        conn.execute(
                            """INSERT INTO project_files(path, root_path, content_hash, mtime_ns, size, indexed_at)
                               VALUES(?, ?, ?, ?, ?, ?)
                               ON CONFLICT(path) DO UPDATE SET content_hash=excluded.content_hash,
                               mtime_ns=excluded.mtime_ns, size=excluded.size, indexed_at=excluded.indexed_at,
                               root_path=excluded.root_path""",
                            (resolved, str(root_path), digest, stat.st_mtime_ns, stat.st_size, int(time.time())),
                        )
                        conn.commit()
                        indexed += 1
                    except Exception as e:
                        conn.rollback()
                        failed += 1
                        print(f"[memory] 索引失败 {path}: {e}")
                if scanned >= max_files:
                    break
            old_paths = [row["path"] for row in conn.execute(
                "SELECT path FROM project_files WHERE root_path=?", (str(root_path),)
            )]
            for old_path in old_paths:
                if old_path in seen or Path(old_path).exists():
                    continue
                conn.execute("DELETE FROM memories WHERE namespace='project' AND source_path=?", (old_path,))
                conn.execute("DELETE FROM project_files WHERE path=?", (old_path,))
                removed += 1
            conn.execute(
                "UPDATE projects SET last_indexed_at=? WHERE root_path=?",
                (int(time.time()), str(root_path)),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "root": str(root_path), "scanned": scanned, "indexed": indexed,
            "unchanged": unchanged, "removed": removed, "failed": failed,
            "limited": scanned >= max_files,
        }

    def refresh_projects(self) -> list[dict[str, Any]]:
        results = []
        for root in self.project_roots():
            try:
                results.append(self.index_project(root))
            except Exception as e:
                results.append({"root": root, "error": str(e)})
        return results

    def export_snapshot(self) -> bytes:
        snapshot = self.data_dir / "memory-export.sqlite3"
        source = self._connect()
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        try:
            return snapshot.read_bytes()
        finally:
            snapshot.unlink(missing_ok=True)

    def import_snapshot(self, snapshot: bytes) -> dict[str, int]:
        """把外部记忆快照按 (namespace, content_hash, source_path, source_ref) 去重合并进本库。

        用于 worker 把本机记忆同步给 hub(云端做真相源)。只合 active 记忆,向量原样复制
        (两端同一个嵌入模型)。返回 {"added": n, "skipped": n}。本库已有的一条都不删。
        """
        import tempfile

        handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        try:
            handle.write(snapshot)
            handle.close()
            source = sqlite3.connect(handle.name)
            source.row_factory = sqlite3.Row
            try:
                rows = source.execute(
                    "SELECT namespace, kind, text, source_path, source_ref, content_hash, "
                    "vector, importance, pinned, created_at, updated_at "
                    "FROM memories WHERE active=1"
                ).fetchall()
            finally:
                source.close()
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass

        added = 0
        skipped = 0
        with self._db() as conn:
            for row in rows:
                existing = conn.execute(
                    "SELECT id FROM memories WHERE namespace=? AND content_hash=? "
                    "AND source_path=? AND source_ref=? AND active=1 LIMIT 1",
                    (row["namespace"], row["content_hash"], row["source_path"], row["source_ref"]),
                ).fetchone()
                if existing:
                    skipped += 1
                    continue
                conn.execute(
                    "INSERT INTO memories (namespace, kind, text, source_path, source_ref, "
                    "content_hash, vector, importance, pinned, active, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (
                        row["namespace"], row["kind"], row["text"], row["source_path"],
                        row["source_ref"], row["content_hash"], row["vector"],
                        row["importance"], row["pinned"], row["created_at"], row["updated_at"],
                    ),
                )
                added += 1
        return {"added": added, "skipped": skipped}
