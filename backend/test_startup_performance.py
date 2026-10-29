"""启动不抢占模型资源；后台维护错峰且可取消。"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.api import server
from backend.core.config import MemoryConfig
from backend.modules.memory import memory_module


class WindowedLoggingTests(unittest.TestCase):
    def test_windowed_entry_uses_parent_pipes_with_line_buffering(self):
        import ast
        import os
        import sys
        from types import SimpleNamespace
        source = Path(__file__).resolve().parents[1] / "xiaoa_backend.py"
        node = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body
                    if isinstance(node, ast.FunctionDef) and node.name == "_ensure_stdio")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "pipe.log"
            with destination.open("wb") as output:
                winapi = SimpleNamespace(GetCurrentProcess=lambda: 1,
                    GetStdHandle=lambda identifier: output.fileno(),
                    DuplicateHandle=lambda *args: output.fileno(), DUPLICATE_SAME_ACCESS=2)
                streams = SimpleNamespace(stdin=object(), stdout=None, stderr=None)
                namespace = {"os": SimpleNamespace(name="nt", O_WRONLY=os.O_WRONLY),
                             "sys": streams, "Path": Path}
                with patch.dict(sys.modules, {"_winapi": winapi,
                        "msvcrt": SimpleNamespace(open_osfhandle=lambda handle, flags: os.dup(handle))}):
                    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
                    namespace["_ensure_stdio"]()
                try:
                    streams.stdout.write("[startup] 中文日志\n")
                    streams.stderr.write("stderr\n")
                    self.assertIn("[startup] 中文日志", destination.read_text(encoding="utf-8"))
                    self.assertIn("stderr", destination.read_text(encoding="utf-8"))
                finally:
                    streams.stdout.close()
                    streams.stderr.close()

    def test_windowed_reader_recovers_inherited_stdin(self):
        import ast
        import os
        import sys
        from types import SimpleNamespace
        source = Path(__file__).resolve().parents[1] / "xiaoa_backend.py"
        node = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body
                    if isinstance(node, ast.FunctionDef) and node.name == "_ensure_stdio")
        with tempfile.TemporaryFile() as pipe:
            pipe.write(b'{"id":1}\n')
            pipe.seek(0)
            streams = SimpleNamespace(stdin=None, stdout=sys.stdout, stderr=sys.stderr)
            winapi = SimpleNamespace(GetCurrentProcess=lambda: 1,
                GetStdHandle=lambda identifier: pipe.fileno() if identifier == -10 else 0,
                DuplicateHandle=lambda *args: pipe.fileno(), DUPLICATE_SAME_ACCESS=2)
            namespace = {"os": SimpleNamespace(name="nt", O_RDONLY=os.O_RDONLY),
                         "sys": streams, "Path": Path}
            with patch.dict(sys.modules, {"_winapi": winapi,
                    "msvcrt": SimpleNamespace(open_osfhandle=lambda handle, flags: os.dup(handle))}):
                exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
                namespace["_ensure_stdio"]()
            try:
                self.assertEqual(streams.stdin.readline(), '{"id":1}\n')
            finally:
                streams.stdin.close()

    def test_windowed_entry_falls_back_when_parent_handles_are_missing_or_invalid(self):
        import ast
        import os
        import sys
        from types import SimpleNamespace
        source = Path(__file__).resolve().parents[1] / "xiaoa_backend.py"
        node = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body
                    if isinstance(node, ast.FunctionDef) and node.name == "_ensure_stdio")
        for handle in (None, 0, -1, 123):
            with self.subTest(handle=handle), tempfile.TemporaryDirectory() as directory:
                duplicate = MagicMock(side_effect=TypeError("handle must be int, not None")
                                      if handle is None else OSError("invalid parent pipe"))
                winapi = SimpleNamespace(GetCurrentProcess=lambda: 1,
                    GetStdHandle=lambda identifier: handle, DuplicateHandle=duplicate,
                    DUPLICATE_SAME_ACCESS=2)
                streams = SimpleNamespace(stdin=object(), stdout=None, stderr=None)
                namespace = {"os": SimpleNamespace(name="nt", O_WRONLY=os.O_WRONLY,
                    environ={"XIAOA_HOME": directory}), "sys": streams, "Path": Path}
                with patch.dict(sys.modules, {"_winapi": winapi, "msvcrt": SimpleNamespace()}):
                    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
                    namespace["_ensure_stdio"]()
                try:
                    streams.stdout.write("startup fallback\n")
                    streams.stderr.write("error fallback\n")
                    self.assertIs(streams.stdout, streams.stderr)
                    self.assertEqual("startup fallback\nerror fallback\n",
                        (Path(directory) / "backend.log").read_text(encoding="utf-8"))
                    self.assertEqual(2 if handle == 123 else 0, duplicate.call_count)
                finally:
                    streams.stdout.close()


class LazyVectorStartupTests(unittest.TestCase):
    def test_startup_and_maintenance_do_not_load_a_model(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(memory_module, "make_default_embedder") as load:
            memory = memory_module.MemoryModule(MemoryConfig(), Path(directory), None)
            memory.refresh_projects()
            memory.list_memories()
            self.assertFalse(memory.vector_ready)
            self.assertFalse(memory._vector_loading)
            load.assert_not_called()

    def test_first_recall_keeps_keywords_available_and_loads_only_once(self):
        started, release, finished = (threading.Event() for _ in range(3))

        class Embedder:
            dim = 2
            def embed(self, texts):
                return [[1.0, float(len(text))] for text in texts]

        def load(_):
            started.set()
            if not release.wait(5):
                raise RuntimeError("test did not release the model loader")
            return Embedder()

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(memory_module, "make_default_embedder", side_effect=load) as model:
            memory = memory_module.MemoryModule(MemoryConfig(), Path(directory), None)
            memory._backend.add("启动时仍能查询项目记忆")
            original = memory._backend.set_embedder
            def backfill(*args):
                try:
                    return original(*args)
                finally:
                    finished.set()
            memory._backend.set_embedder = backfill
            try:
                self.assertIn("项目记忆", asyncio.run(memory.recall("项目记忆")))
                self.assertTrue(started.wait(2))
                self.assertFalse(memory.vector_ready)
                with ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(lambda _: memory._ensure_vectors(), range(20)))
                self.assertEqual(model.call_count, 1)
            finally:
                release.set()
                self.assertTrue(finished.wait(5))
                deadline = time.monotonic() + 2
                while memory._vector_loading and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertTrue(memory.vector_ready)
            self.assertEqual(len(memory.search("项目记忆")), 1)
            self.assertEqual(model.call_count, 1)

    def test_disabled_embeddings_never_create_a_loader(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(memory_module, "make_default_embedder") as load:
            memory = memory_module.MemoryModule(MemoryConfig(embed_model=""), Path(directory), None)
            memory.add("纯关键词记忆")
            self.assertIn("纯关键词记忆", asyncio.run(memory.recall("关键词记忆")))
            load.assert_not_called()

    def test_failed_model_keeps_keywords_and_is_not_retried_each_query(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(memory_module, "make_default_embedder", side_effect=RuntimeError("offline")) as load:
            memory = memory_module.MemoryModule(MemoryConfig(), Path(directory), None)
            memory.add("模型失败也保留的项目记忆")
            deadline = time.monotonic() + 2
            while memory._vector_loading and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(memory.vector_ready)
            self.assertEqual(len(memory.search("项目记忆")), 1)
            self.assertEqual(len(memory.search("项目记忆")), 1)
            self.assertEqual(load.call_count, 1)


class StartupMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_maintenance_is_deferred_serial_and_then_becomes_periodic(self):
        events = []
        async def sleep(delay):
            events.append(("wait", delay))
        async def periodic():
            events.append("periodic")
        memory = MagicMock()
        memory.refresh_projects.side_effect = lambda: events.append("projects") or []
        with patch.object(server.asyncio, "sleep", side_effect=sleep), \
                patch.object(server.isolation_workspace, "cleanup", side_effect=lambda *_: events.append("isolation")), \
                patch.object(server, "_cleanup_attachments", side_effect=lambda: events.append("attachments")), \
                patch.object(server, "_cleanup_stale_update_packages", side_effect=lambda: events.append("old-updates")), \
                patch.object(server, "MEMORY", memory), \
                patch.object(server, "_isolation_cleanup_loop", side_effect=periodic):
            await server._startup_cache_cleanup()
        self.assertEqual(events, [("wait", 30), "isolation", ("wait", 5), "attachments", ("wait", 5), "old-updates", ("wait", 5), "projects", "periodic"])

    async def test_exit_during_delay_does_not_start_disk_scans(self):
        with patch.object(server.asyncio, "sleep", side_effect=asyncio.CancelledError), \
                patch.object(server.isolation_workspace, "cleanup") as clean, \
                patch.object(server, "_cleanup_attachments") as attachments:
            with self.assertRaises(asyncio.CancelledError):
                await server._startup_cache_cleanup()
            clean.assert_not_called()
            attachments.assert_not_called()

    async def test_scan_error_does_not_disable_the_remaining_maintenance(self):
        with patch.object(server.asyncio, "sleep", new=AsyncMock()), \
                patch.object(server.isolation_workspace, "cleanup", side_effect=OSError("busy")), \
                patch.object(server, "_cleanup_attachments") as attachments, \
                patch.object(server, "MEMORY", None), \
                patch.object(server, "_isolation_cleanup_loop", new=AsyncMock()) as periodic:
            await server._startup_cache_cleanup()
            attachments.assert_called_once()
            periodic.assert_awaited_once()


class MemorySyncPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def run_loop(self, role, fingerprints, *, upload_errors=None, passes=3):
        from backend.core.config import Config, RoleConfig
        waits = []
        async def sleep(seconds):
            waits.append(seconds)
            if len(waits) > passes:
                raise asyncio.CancelledError
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / "snapshot"
            backup.mkdir()
            cfg = Config(role=RoleConfig(mode=role))
            with patch.object(server, "CFG", cfg), \
                    patch.object(server, "_sleep_or_wake", side_effect=sleep), \
                    patch.object(server, "_memory_source_fingerprint", side_effect=fingerprints), \
                    patch.object(server, "_run_memory_upload_once", new=AsyncMock(side_effect=upload_errors)) as upload, \
                    patch.object(server, "_run_memory_backup_once", new=AsyncMock(return_value=backup)) as write:
                with self.assertRaises(asyncio.CancelledError):
                    await server._memory_backup_loop()
                return waits, upload.call_count, write.call_count, write.call_args_list

    async def test_standalone_skips_unchanged_and_backs_up_changes(self):
        waits, uploads, backups, _ = await self.run_loop("standalone", ["a", "a", "a", "b", "b"])
        self.assertEqual(waits[0], 120)
        self.assertEqual(uploads, 0)
        self.assertEqual(backups, 2)

    async def test_worker_skips_upload_but_always_fetches_cloud_changes(self):
        _, uploads, backups, calls = await self.run_loop("worker", ["a", "a", "a", "a"])
        self.assertEqual(uploads, 1)
        self.assertEqual(backups, 3)
        self.assertTrue(all(call.kwargs == {"skip_unchanged": True} for call in calls))

    async def test_failed_upload_is_retried_without_new_local_changes(self):
        _, uploads, backups, _ = await self.run_loop("worker", ["a", "a", "a", "a"],
                                                    upload_errors=[OSError("offline"), {}])
        self.assertEqual(uploads, 2)
        self.assertEqual(backups, 2)

    async def test_write_during_upload_is_not_marked_as_uploaded(self):
        _, uploads, backups, _ = await self.run_loop("worker", ["a", "b", "b", "b", "b"])
        self.assertEqual(uploads, 2)
        self.assertEqual(backups, 3)

    async def test_identical_cloud_merge_keeps_real_source_fingerprint_stable(self):
        from backend.core.config import Config
        from backend.modules.memory.account_memory import WeixinMemories, CATALOG_FILE
        from backend.modules.memory.memory_module import MemoryModule
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config()
            cfg.memory.data_dir = str(root / "memory")
            cfg.memory.embed_model = ""
            memory = MemoryModule(cfg.memory, cfg.memory_data_dir, lambda: None)
            private = WeixinMemories(root, cfg.memory, lambda: None)
            accounts = {"test": {"ilink_user_id": "test-user"}, "other": {"ilink_user_id": "other-user"}}
            private.register_accounts(accounts)
            private.for_account("test", accounts).add("private snapshot regression")
            with patch.object(server, "CFG", cfg), patch.object(server, "MEMORY", memory), \
                    patch.object(server, "_weixin_memories", return_value=private), \
                    patch.object(server, "_HISTORY_PATH", root / "history.json"):
                files = await server._collect_memory_export_files(include_shared=False)
                before = server._memory_source_fingerprint(include_shared=False)
                await server._merge_memory_snapshot_into_runtime(files)
                self.assertEqual(before, server._memory_source_fingerprint(include_shared=False))
                # An actual cloud ownership change must still apply and invalidate the fingerprint.
                manifest = json.loads(files[CATALOG_FILE])
                manifest["owner"] = "user:other-user"
                files[CATALOG_FILE] = json.dumps(manifest).encode()
                await server._merge_memory_snapshot_into_runtime(files)
                after = server._memory_source_fingerprint(include_shared=False)
                self.assertNotEqual(before, after)
                self.assertEqual(private.owner(), "user:other-user")
                await server._merge_memory_snapshot_into_runtime(files)
                self.assertEqual(after, server._memory_source_fingerprint(include_shared=False))

    async def test_equal_downloads_do_not_write_duplicate_backup(self):
        from backend.core.config import Config
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "saved"
            target.mkdir()
            cfg = Config()
            with patch.object(server, "CFG", cfg), \
                    patch.object(server, "_LAST_MEMORY_BACKUP", [None, None]), \
                    patch.object(server, "_collect_memory_export_files", new=AsyncMock(return_value={"memory.sqlite3": b"snapshot"})) as collect, \
                    patch.object(server, "_write_memory_backup", return_value=target) as write:
                await server._run_memory_backup_once(skip_unchanged=True)
                await server._run_memory_backup_once(skip_unchanged=True)
                self.assertEqual(write.call_count, 1)
                collect.return_value = {"memory.sqlite3": b"new snapshot"}
                await server._run_memory_backup_once(skip_unchanged=True)
                self.assertEqual(write.call_count, 2)
                await server._run_memory_backup_once()  # Explicit backups still execute.
                self.assertEqual(write.call_count, 3)


class MemoryFingerprintTests(unittest.TestCase):
    def test_wal_private_catalog_json_history_and_shared_changes_are_detected(self):
        from types import SimpleNamespace
        from backend.core.config import Config
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = SimpleNamespace(root=root / "weixin-memory", catalog=root / "weixin-memory" / "bindings.sqlite3",
                                      shared=root / "shared-memory.sqlite3")
            cfg = Config()
            cfg.memory.data_dir = str(root / "memory")
            history = root / "history.json"
            with patch.object(server, "CFG", cfg), patch.object(server, "_weixin_memories", return_value=private), \
                    patch.object(server, "_HISTORY_PATH", history):
                for path in (root / "memory" / "memory.sqlite3-wal", root / "memory" / "memory.sqlite3-shm",
                             root / "memory" / "simple_memory.json", root / "memory" / "vector_memory.jsonl",
                             history, private.catalog, private.root / "accounts" / ("a" * 64) / "memory.sqlite3",
                             root / "shared-libraries.sqlite3", root / "shared-libraries" / "test.sqlite3"):
                    before = server._memory_source_fingerprint(include_shared=True)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"changed")
                    self.assertNotEqual(before, server._memory_source_fingerprint(include_shared=True), str(path))
                    before = server._memory_source_fingerprint(include_shared=True)
                    path.write_bytes(b"another size")
                    self.assertNotEqual(before, server._memory_source_fingerprint(include_shared=True), str(path))
                    before = server._memory_source_fingerprint(include_shared=True)
                    path.unlink()
                    self.assertNotEqual(before, server._memory_source_fingerprint(include_shared=True), str(path))


class OldUpdateCleanupTests(unittest.TestCase):
    def test_only_expired_flat_update_workspaces_are_removed(self):
        import os
        from backend.core import isolation_workspace as isolation
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("xiaoa-update-old", "xiaoa-update-new", "xiaoa-update-active", "xiaoa-update-nested", "unrelated"):
                folder = root / "tmp" / name
                folder.mkdir(parents=True)
                (folder / "XiaoA.exe").write_bytes(b"payload")
                if name == "xiaoa-update-nested":
                    (folder / "unknown").mkdir()
                if name != "xiaoa-update-new":
                    for file in folder.iterdir():
                        os.utime(file, (1, 1))
                    os.utime(folder, (1, 1))
            with patch.object(isolation, "root", return_value=root), \
                    patch.object(isolation, "_active_runtime_root", return_value=None):
                self.assertEqual(isolation.cleanup_stale_updates((root / "tmp" / "xiaoa-update-active",)), 1)
            self.assertFalse((root / "tmp" / "xiaoa-update-old").exists())
            for name in ("xiaoa-update-new", "xiaoa-update-active", "xiaoa-update-nested", "unrelated"):
                self.assertTrue((root / "tmp" / name / "XiaoA.exe").is_file())

    def test_pending_update_suppresses_cleanup(self):
        for stage in ("downloading", "staged", "restarting", "cancelling", "replacing"):
            with self.subTest(stage=stage), patch.object(server, "_UPDATE_JOB", {"stage": stage}), \
                    patch.object(server.isolation_workspace, "cleanup_stale_updates") as clean:
                server._cleanup_stale_update_packages()
                clean.assert_not_called()


if __name__ == "__main__":
    unittest.main()
