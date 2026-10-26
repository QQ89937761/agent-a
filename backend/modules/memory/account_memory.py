"""Persistent WeChat identity bindings and physically separate memory stores."""
from __future__ import annotations

from contextlib import contextmanager, closing
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
from typing import Callable

from backend.core.config import MemoryConfig
from .memory_module import MemoryModule
from .embedder import make_default_embedder
from .scoped_snapshot import prepare_scope, merge_scope

PRIVATE_FILE = re.compile(r"weixin-memory-([0-9a-f]{64})\.sqlite3\Z")
CATALOG_FILE = "weixin-memory-bindings.json"
SHARED_FILE = "shared-memory.sqlite3"


def snapshot_database(path: Path) -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "snapshot.sqlite3"
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
        return target.read_bytes()


class WeixinMemories:
    def __init__(self, home: Path, cfg: MemoryConfig, llm: Callable):
        self.root = home / "data" / "weixin-memory"
        self.catalog = self.root / "bindings.sqlite3"
        self.shared = home / "data" / SHARED_FILE
        self.cfg, self.llm = cfg, llm
        self._lock = threading.RLock()
        self._memories: dict[str, MemoryModule] = {}
        self._model_lock = threading.Lock()
        self._embedder = None
        self._embedder_loaded = False

    @contextmanager
    def _db(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.catalog, timeout=15)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS bindings (account TEXT PRIMARY KEY, identity TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            yield db

    @staticmethod
    def identity(account_id: str, config: dict) -> str:
        # iLink's authenticated user ID survives a fresh QR login with a new bot ID.
        user_id = str(config.get("ilink_user_id") or "").strip()
        return "user:" + user_id if user_id else "account:" + account_id

    def register_accounts(self, accounts: dict[str, dict]) -> None:
        with self._lock, self._db() as db:
            for account_id, config in accounts.items():
                identity = self.identity(account_id, config)
                previous = db.execute("SELECT identity FROM bindings WHERE account=?", (account_id,)).fetchone()
                if previous and previous[0] != identity:
                    raise ValueError("微信账号身份与已保存的记忆归属不一致，请在本机检查连接")
                db.execute("INSERT OR IGNORE INTO bindings VALUES (?, ?)", (account_id, identity))
                db.execute("INSERT OR IGNORE INTO settings VALUES ('owner', ?)", (identity,))

        if accounts:
            from .shared_libraries import ensure_catalog
            ensure_catalog(self.shared, self.owner(), [self.identity(key, cfg) for key, cfg in accounts.items()])

    def libraries(self) -> list[dict]:
        from .shared_libraries import list_libraries
        return list_libraries(self.shared)

    def manage_library(self, accounts: dict, values: dict) -> dict:
        from .shared_libraries import manage
        self.register_accounts(accounts)
        return manage(self.shared, accounts, self.owner(), values)

    def owner(self) -> str:
        with self._lock, self._db() as db:
            row = db.execute("SELECT value FROM settings WHERE key='owner'").fetchone()
            return row[0] if row else ""

    def is_owner(self, account_id: str, accounts: dict[str, dict]) -> bool:
        config = accounts.get(account_id)
        return bool(config is not None and self.identity(account_id, config) == self.owner())

    def transfer_owner(self, account_id: str, accounts: dict[str, dict]) -> None:
        if account_id not in accounts:
            raise ValueError("指定微信账号尚未连接")
        self.register_accounts(accounts)
        with self._lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('owner', ?)",
                       (self.identity(account_id, accounts[account_id]),))

        from .shared_libraries import legacy_owner
        legacy_owner(self.shared, self.identity(account_id, accounts[account_id]))

    def _shared_embedder(self, model: str):
        with self._model_lock:
            if not self._embedder_loaded:
                self._embedder = make_default_embedder(model)
                self._embedder_loaded = True
            return self._embedder

    def _get_store(self, key: str) -> MemoryModule:
        with self._lock:
            if key not in self._memories:
                memory = MemoryModule(
                    replace(self.cfg, project_roots=[]), self.root / "accounts" / key,
                    self.llm, embedder_factory=self._shared_embedder,
                )
                prepare_scope(memory, key)
                self._memories[key] = memory
            return self._memories[key]

    def for_account(self, account_id: str, accounts: dict[str, dict]) -> MemoryModule:
        if not account_id or account_id not in accounts:
            raise ValueError("无法确定当前微信账号，已停止访问长期记忆")
        self.register_accounts(accounts)
        key = sha256(self.identity(account_id, accounts[account_id]).encode()).hexdigest()
        return self._get_store(key)

    def active_stores(self) -> tuple[MemoryModule, ...]:
        with self._lock:
            return tuple(self._memories.values())

    def export_files(self, *, include_shared: bool = True) -> dict[str, bytes]:
        files = {}
        with self._lock:
            if self.catalog.exists():
                with self._db() as db:
                    manifest = {"version": 1, "owner": self.owner(),
                                "bindings": dict(db.execute("SELECT account, identity FROM bindings"))}
                files[CATALOG_FILE] = json.dumps(manifest, ensure_ascii=False).encode()
            for path in (self.root / "accounts").glob("*/memory.sqlite3"):
                if re.fullmatch(r"[0-9a-f]{64}", path.parent.name):
                    files[f"weixin-memory-{path.parent.name}.sqlite3"] = snapshot_database(path)
            if include_shared:
                from .shared_libraries import export_files
                files.update(export_files(self.shared))
        return files

    def import_files(self, files: dict[str, bytes], *, authoritative: bool = False) -> dict[str, int]:
        """Merge private stores by identity; only hub downloads restore legacy owner metadata."""
        added = skipped = 0
        if not any(name == CATALOG_FILE or name == SHARED_FILE or PRIVATE_FILE.fullmatch(name) for name in files):
            return {"added": 0, "skipped": 0}
        manifest = None
        if CATALOG_FILE in files:
            manifest = json.loads(files[CATALOG_FILE])
            bindings = manifest.get("bindings")
            if manifest.get("version") != 1 or not isinstance(bindings, dict):
                raise ValueError("微信记忆归属备份无效")
            if any(not isinstance(k, str) or not isinstance(v, str) or
                   not v.startswith(("user:", "account:")) or len(k) > 256 or len(v) > 512
                   for k, v in bindings.items()):
                raise ValueError("微信记忆身份备份无效")
            owner = manifest.get("owner", "")
            if not isinstance(owner, str) or (owner and owner not in bindings.values()):
                raise ValueError("共享库管理员备份无效")
        private = [(PRIVATE_FILE.fullmatch(name).group(1), data)
                   for name, data in files.items() if PRIVATE_FILE.fullmatch(name)]
        known = set()
        with self._lock, self._db() as db:
            known.update(sha256(row[0].encode()).hexdigest() for row in db.execute("SELECT identity FROM bindings"))
        if manifest:
            known.update(sha256(v.encode()).hexdigest() for v in manifest["bindings"].values())
        if any(key not in known for key, _ in private):
            raise ValueError("微信记忆备份缺少对应身份，不能合并到其他账号")
        if manifest:
            with self._lock, self._db() as db:
                for account, identity in manifest["bindings"].items():
                    previous = db.execute("SELECT identity FROM bindings WHERE account=?", (account,)).fetchone()
                    if previous and previous[0] != identity:
                        raise ValueError("备份中的微信账号归属冲突")
                    db.execute("INSERT OR IGNORE INTO bindings VALUES (?, ?)", (account, identity))
                # A trusted worker may seed a fresh installation, never replace an existing owner.
                if manifest["owner"]:
                    if authoritative:
                        db.execute("INSERT INTO settings VALUES ('owner', ?) "
                                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value "
                                   "WHERE settings.value != excluded.value", (manifest["owner"],))
                    else:
                        db.execute("INSERT OR IGNORE INTO settings VALUES ('owner', ?)", (manifest["owner"],))
        for key, snapshot in private:
            result = merge_scope(self._get_store(key), snapshot, key)
            added += result["added"]
            skipped += result["skipped"]
        # Project stores and ACLs are never merged by automatic private-memory sync.
        # Full project recovery is an explicit administrator operation, not an upload.
        return {"added": added, "skipped": skipped}
