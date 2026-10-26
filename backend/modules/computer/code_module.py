"""Pragmatic code-workbench tools for reading, editing, Git and verification."""
from __future__ import annotations

import hashlib
import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.core.config import subprocess_env_without_secrets
from backend.core.process import hidden_subprocess_kwargs
from backend.core.tools import ToolRegistry
from backend.core import isolation_workspace


_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "dist", "build", "__pycache__"}
_MAX_COMMAND = 2_000
_MAX_OUTPUT = 100_000
_MAX_PROCESSES = 4
_MAX_CHECKPOINT_FILES = 2_000
_MAX_CHECKPOINT_BYTES = 200 * 1024 * 1024


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _path(raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
        raise ValueError("路径不能为空或包含 NUL 字符")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = isolation_workspace.workspace() / path
    return path.resolve()


def _protected_roots() -> list[Path]:
    """不允许写工具随意写入的目录。挡的是两类事:
      1. 持久化/自启动(启动目录、计划任务)——让代码开机就跑,危险且隐蔽;
      2. 自我篡改(小A 自己的 backend、已发布的工具脚本、审计库)——工具改写
         自己的代码或审计,等于绕过一切防线。
    通用编码用途(在别的项目里读写文件)不受影响,这里只列真正敏感的根。
    """
    roots: list[Path] = []

    def _add(p: str | os.PathLike | None) -> None:
        if not p:
            return
        try:
            roots.append(Path(p).expanduser().resolve())
        except (OSError, ValueError, RuntimeError):
            pass

    # 小A 自身代码、配置与扩展(篡改即绕过防线)
    try:
        from backend.core.config import app_home, _project_root
        project, home = _project_root(), app_home()
        for relative in (
            ".git", "backend", "frontend", "deploy",
            "xiaoa_backend.py", "start_backend.bat", "start_xiaoa.bat", "小A.exe",
        ):
            _add(project / relative)
        for relative in ("tools", "plugins", "skills", "config.toml", "ui_token.txt"):
            _add(home / relative)
        _add(home / "data" / "memory")
        _add(home / "data" / "audit")
        _add(home / "data" / "backup")
        _add(home / "data" / "channel_lock.json")
        _add(home / "model-cache")
    except Exception:
        pass

    # Windows 系统与自启动
    if os.name == "nt":
        windir = os.getenv("WINDIR") or r"C:\Windows"
        _add(windir)                                       # 含 System32
        appdata = os.getenv("APPDATA")
        if appdata:
            _add(Path(appdata) / r"Microsoft\Windows\Start Menu\Programs\Startup")
        programdata = os.getenv("PROGRAMDATA")
        if programdata:
            _add(Path(programdata) / r"Microsoft\Windows\Start Menu\Programs\StartUp")
    return roots


def _reject_protected(path: Path) -> None:
    """若目标落在受保护根内(或就是该根),抛错拒绝。"""
    resolved = path if path.is_absolute() else path.resolve()
    for root in _protected_roots():
        if resolved == root or root in resolved.parents:
            raise ValueError(
                f"拒绝写入受保护位置(系统目录/自启动/小A自身):{resolved}。"
                "这类路径可能被用于持久化或篡改小A本身,已阻止。"
            )


def _inside(root: Path, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"路径必须位于项目目录内:{raw}") from exc
    return candidate


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(tmp_name, path.stat().st_mode)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _transaction_apply(changes: dict[Path, bytes | None]) -> None:
    """Apply prepared files as one best-effort transaction with rollback."""
    temps: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    installed: list[Path] = []
    try:
        for path, data in changes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            if data is None:
                continue
            fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".new", dir=path.parent)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if path.exists():
                os.chmod(name, path.stat().st_mode)
            temps[path] = Path(name)

        for path in changes:
            if path.exists():
                if not path.is_file() or path.is_symlink():
                    raise ValueError(f"只允许修改普通文件:{path}")
                fd, backup_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".bak", dir=path.parent)
                os.close(fd)
                os.unlink(backup_name)
                backup = Path(backup_name)
                os.replace(path, backup)
                backups[path] = backup
            if path in temps:
                os.replace(temps[path], path)
            installed.append(path)
    except Exception:
        for path in reversed(installed):
            try:
                if path.exists():
                    path.unlink()
                backup = backups.get(path)
                if backup and backup.exists():
                    os.replace(backup, path)
            except OSError:
                pass
        for path, backup in backups.items():
            if backup.exists() and not path.exists():
                try:
                    os.replace(backup, path)
                except OSError:
                    pass
        raise
    finally:
        for item in list(temps.values()) + list(backups.values()):
            try:
                item.unlink()
            except FileNotFoundError:
                pass


def _run(args: list[str], cwd: Path, timeout: int = 30, limit: int = 40_000) -> tuple[int, str, bool]:
    completed = subprocess.run(
        args, cwd=str(cwd), capture_output=True, timeout=timeout,
        **hidden_subprocess_kwargs(),
    )
    text = _decode((completed.stdout or b"") + (completed.stderr or b""))
    truncated = len(text) > limit
    return completed.returncode, text[:limit], truncated


def _git(repo: Path, args: list[str], timeout: int = 30, limit: int = 40_000) -> tuple[int, str, bool]:
    if not repo.is_dir():
        raise ValueError(f"项目目录不存在:{repo}")
    if not shutil.which("git"):
        raise RuntimeError("系统未安装 Git")
    code, text, truncated = _run(["git", "-C", str(repo), *args], repo, timeout, limit)
    if code != 0:
        raise RuntimeError(text.strip() or f"Git 退出码 {code}")
    return code, text, truncated


def _repo_root(raw: str) -> Path:
    start = _path(raw)
    if start.is_file():
        start = start.parent
    _, text, _ = _git(start, ["rev-parse", "--show-toplevel"])
    return _path(text.strip())


def _search_fallback(root: Path, pattern: str, ext: str, max_results: int) -> list[str]:
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    hits: list[str] = []
    suffix = ext if not ext or ext.startswith(".") else "." + ext
    scanned = 0
    for path in root.rglob("*"):
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if not path.is_file() or path.is_symlink() or (suffix and path.suffix.lower() != suffix.lower()):
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            scanned += 1
            if scanned > 5_000:
                break
            for number, line in enumerate(_decode(path.read_bytes()).splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{_relative(root, path)}:{number}:{line.strip()[:300]}")
                    if len(hits) >= max_results:
                        return hits
        except OSError:
            continue
    return hits


def _search(root: Path, pattern: str, ext: str, max_results: int) -> tuple[list[str], str]:
    rg = shutil.which("rg")
    if not rg:
        return _search_fallback(root, pattern, ext, max_results), "python"
    args = [
        rg, "--line-number", "--no-heading", "--color", "never", "--max-filesize", "2M",
        "--glob", "!.git/**", "--glob", "!node_modules/**", "--glob", "!.venv/**",
        "--glob", "!venv/**", "--glob", "!dist/**", "--glob", "!build/**",
    ]
    if ext:
        args += ["--glob", f"*{ext if ext.startswith('.') else '.' + ext}"]
    args += [pattern, "."]
    try:
        completed = subprocess.run(
            args, cwd=str(root), capture_output=True, timeout=20,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return _search_fallback(root, pattern, ext, max_results), "python"
    if completed.returncode not in (0, 1):
        return _search_fallback(root, pattern, ext, max_results), "python"
    lines = _decode(completed.stdout or b"").splitlines()
    return lines[:max_results], "rg"


def _discover_project_root(start: Path) -> Path:
    try:
        return _repo_root(str(start))
    except Exception:
        current = start if start.is_dir() else start.parent
        for candidate in (current, *current.parents):
            if any((candidate / marker).exists() for marker in ("pyproject.toml", "package.json", "Cargo.toml", "go.mod")):
                return candidate
        return current


def _all_instruction_files(root: Path) -> list[str]:
    files = []
    for item in root.rglob("AGENTS.md"):
        if any(part in _SKIP_DIRS for part in item.relative_to(root).parts):
            continue
        files.append(str(item))
        if len(files) >= 100:
            break
    return files


def _status_paths(repo: Path) -> set[str]:
    _, tracked_raw, tracked_truncated = _git(repo, ["diff", "--no-renames", "--name-only", "-z", "HEAD"], limit=2_000_000)
    _, untracked_raw, untracked_truncated = _git(repo, ["ls-files", "--others", "--exclude-standard", "-z"], limit=2_000_000)
    if tracked_truncated or untracked_truncated:
        raise ValueError("变更文件清单过大，拒绝创建或恢复检查点")
    return {p for p in (tracked_raw + untracked_raw).split("\0") if p}


def _head_paths(repo: Path) -> set[str]:
    _, raw, truncated = _git(repo, ["ls-tree", "-r", "--name-only", "-z", "HEAD"], limit=20_000_000)
    if truncated:
        raise ValueError("仓库文件清单过大，拒绝自动恢复")
    return {p for p in raw.split("\0") if p}


def _git_dir(repo: Path) -> Path:
    _, raw, _ = _git(repo, ["rev-parse", "--path-format=absolute", "--git-dir"])
    return _path(raw.strip())


def _checkpoint_dir(repo: Path, checkpoint_id: str) -> Path:
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}", checkpoint_id):
        raise ValueError("检查点编号格式无效")
    # Keep directory names short for portable Windows builds without long-path support.
    repo_key = hashlib.sha256(str(_git_dir(repo)).encode("utf-8")).hexdigest()[:24]
    return isolation_workspace.workspace() / "backups" / "code" / repo_key / checkpoint_id


def _create_checkpoint(repo: Path, label: str) -> dict[str, Any]:
    """同步构建检查点；调用方必须放在线程中，避免大文件读写阻塞事件循环。"""
    _, head, _ = _git(repo, ["rev-parse", "HEAD"], 30, 40_000)
    paths = sorted(_status_paths(repo))
    if len(paths) > _MAX_CHECKPOINT_FILES:
        raise ValueError(f"变更文件超过 {_MAX_CHECKPOINT_FILES} 个，拒绝创建检查点")
    checkpoint_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
    destination = _checkpoint_dir(repo, checkpoint_id)
    staging = destination.with_name(destination.name + ".tmp")
    try:
        staging.mkdir(parents=True, exist_ok=False)
        files_dir = staging / "files"
        files_dir.mkdir()
        items, total = [], 0
        for rel in paths:
            path = _inside(repo, rel)
            if path.exists() and (not path.is_file() or path.is_symlink()):
                raise ValueError(f"检查点不支持链接或特殊文件:{rel}")
            exists, blob = path.is_file(), ""
            if exists:
                data = path.read_bytes()
                total += len(data)
                if total > _MAX_CHECKPOINT_BYTES:
                    raise ValueError("检查点内容超过 200MB")
                blob = hashlib.sha256(rel.encode("utf-8")).hexdigest()
                (files_dir / blob).write_bytes(data)
            items.append({"path": rel, "exists": exists, "blob": blob})
        index_path = _git_dir(repo) / "index"
        if index_path.with_name("index.lock").exists():
            raise ValueError("Git 正在进行其他操作(index.lock 存在)，稍后再创建检查点")
        if not index_path.is_file():
            raise ValueError("Git index 不存在")
        (staging / "index.bin").write_bytes(index_path.read_bytes())
        manifest = {
            "id": checkpoint_id, "created_at": datetime.now(timezone.utc).isoformat(),
            "label": str(label)[:120], "repo": str(repo), "head": head.strip(), "files": items,
        }
        (staging / "manifest.json").write_text(_json(manifest), encoding="utf-8")
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, destination)
        return {
            "ok": True, "checkpoint_id": checkpoint_id, "files": len(items),
            "bytes": total, "head": head.strip(),
        }
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_show_bytes(repo: Path, rel: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{rel}"], capture_output=True, timeout=30,
        **hidden_subprocess_kwargs(),
    )
    if completed.returncode != 0:
        raise RuntimeError(f"无法读取 HEAD 中的文件:{rel}")
    return completed.stdout


def _restore_plan(repo: Path, manifest: dict[str, Any], checkpoint: Path) -> tuple[dict[Path, bytes | None], list[dict[str, Any]], str]:
    _, head, _ = _git(repo, ["rev-parse", "HEAD"])
    if head.strip() != manifest["head"]:
        raise ValueError("当前 Git 提交已变化，不能自动恢复此检查点")
    current = _status_paths(repo)
    saved = {item["path"]: item for item in manifest["files"]}
    head_paths = _head_paths(repo)
    affected = sorted(current | set(saved))
    if len(affected) > _MAX_CHECKPOINT_FILES:
        raise ValueError(f"恢复涉及超过 {_MAX_CHECKPOINT_FILES} 个文件，拒绝自动恢复")
    changes: dict[Path, bytes | None] = {}
    preview: list[dict[str, Any]] = []
    fingerprint: list[dict[str, Any]] = []
    total = 0
    for rel in affected:
        path = _inside(repo, rel)
        if path.is_file():
            total += path.stat().st_size
            if total > _MAX_CHECKPOINT_BYTES:
                raise ValueError("当前变更内容超过 200MB，拒绝自动恢复")
        fingerprint.append({
            "path": rel, "exists": path.is_file(),
            "sha256": _file_hash(path) if path.is_file() and not path.is_symlink() else None,
        })
        item = saved.get(rel)
        if item:
            target = (checkpoint / "files" / item["blob"]).read_bytes() if item["exists"] else None
        elif rel in head_paths:
            target = _git_show_bytes(repo, rel)
        else:
            target = None
        total += len(target or b"")
        if total > _MAX_CHECKPOINT_BYTES:
            raise ValueError("恢复内容超过 200MB，拒绝自动恢复")
        current_bytes = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        if current_bytes != target:
            changes[path] = target
            preview.append({"path": rel, "action": "delete" if target is None else "restore"})

    index_path = _git_dir(repo) / "index"
    index_target = (checkpoint / "index.bin").read_bytes()
    fingerprint.append({
        "path": "<git-index>", "exists": index_path.is_file(),
        "sha256": _file_hash(index_path) if index_path.is_file() else None,
    })
    if not index_path.is_file() or index_path.read_bytes() != index_target:
        changes[index_path] = index_target
        preview.append({"path": "<git-index>", "action": "restore"})
    token = hashlib.sha256(_json({
        "checkpoint": manifest["id"], "head": manifest["head"], "current": fingerprint,
    }).encode("utf-8")).hexdigest()[:24]
    return changes, preview, token


@dataclass
class _ManagedProcess:
    process_id: str
    command: str
    cwd: str
    proc: subprocess.Popen[bytes]
    created_at: float = field(default_factory=time.time)
    output: bytearray = field(default_factory=bytearray)
    base_cursor: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class ProcessManager:
    def __init__(self) -> None:
        self._items: dict[str, _ManagedProcess] = {}
        self._lock = threading.Lock()

    def _clean(self) -> None:
        finished = sorted(
            (item for item in self._items.values() if item.proc.poll() is not None),
            key=lambda item: item.created_at,
        )
        while len(self._items) > 16 and finished:
            item = finished.pop(0)
            self._close_streams(item)
            self._items.pop(item.process_id, None)

    @staticmethod
    def _close_streams(item: _ManagedProcess) -> None:
        for stream in (item.proc.stdin, item.proc.stdout):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    def start(self, command: str, cwd: Path) -> dict[str, Any]:
        if not command.strip() or len(command) > _MAX_COMMAND:
            raise ValueError(f"命令不能为空且不能超过 {_MAX_COMMAND} 字符")
        with self._lock:
            self._clean()
            active = sum(item.proc.poll() is None for item in self._items.values())
            if active >= _MAX_PROCESSES:
                raise RuntimeError(f"同时最多运行 {_MAX_PROCESSES} 个长任务")
            kwargs = hidden_subprocess_kwargs(new_process_group=True)
            proc = subprocess.Popen(
                command, shell=True, cwd=str(cwd), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                env=subprocess_env_without_secrets(), **kwargs,
            )
            process_id = uuid.uuid4().hex[:12]
            item = _ManagedProcess(process_id, command, str(cwd), proc)
            self._items[process_id] = item
            threading.Thread(target=self._read_output, args=(item,), daemon=True).start()
        return {"process_id": process_id, "pid": proc.pid, "status": "running", "cursor": 0}

    @staticmethod
    def _read_output(item: _ManagedProcess) -> None:
        assert item.proc.stdout is not None
        for chunk in iter(lambda: item.proc.stdout.read(4096), b""):
            with item.lock:
                item.output.extend(chunk)
                excess = len(item.output) - _MAX_OUTPUT
                if excess > 0:
                    del item.output[:excess]
                    item.base_cursor += excess

    def get(self, process_id: str) -> _ManagedProcess:
        with self._lock:
            item = self._items.get(process_id)
        if not item:
            raise ValueError("长任务编号不存在或已清理")
        return item

    def poll(self, process_id: str, cursor: int, max_chars: int) -> dict[str, Any]:
        item = self.get(process_id)
        with item.lock:
            dropped = cursor < item.base_cursor
            start = max(cursor, item.base_cursor) - item.base_cursor
            chunk = bytes(item.output[start:start + max_chars])
            next_cursor = max(cursor, item.base_cursor) + len(chunk)
        return {
            "process_id": process_id,
            "status": "running" if item.proc.poll() is None else "finished",
            "exit_code": item.proc.poll(), "output": _decode(chunk),
            "cursor": next_cursor, "output_dropped": dropped,
        }

    def write(self, process_id: str, text: str, newline: bool) -> dict[str, Any]:
        item = self.get(process_id)
        if item.proc.poll() is not None or item.proc.stdin is None:
            raise RuntimeError("长任务已经结束")
        data = (text + ("\n" if newline else "")).encode("utf-8")
        item.proc.stdin.write(data)
        item.proc.stdin.flush()
        return {"process_id": process_id, "written": len(data)}

    def stop(self, process_id: str) -> dict[str, Any]:
        item = self.get(process_id)
        if item.proc.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(item.proc.pid), "/T", "/F"],
                    capture_output=True, timeout=10, **hidden_subprocess_kwargs(),
                )
            else:
                try:
                    os.killpg(item.proc.pid, signal.SIGTERM)
                    item.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(item.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            item.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            item.proc.kill()
        self._close_streams(item)
        return {"process_id": process_id, "status": "stopped", "exit_code": item.proc.poll()}

    def stop_all(self) -> None:
        with self._lock:
            process_ids = list(self._items)
        for process_id in process_ids:
            try:
                self.stop(process_id)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                pass


def _detect_commands(root: Path) -> list[dict[str, str]]:
    commands: list[dict[str, str]] = []
    package = root / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
            runner = "pnpm" if (root / "pnpm-lock.yaml").exists() else "yarn" if (root / "yarn.lock").exists() else "npm run"
            for name in ("test", "check", "lint", "build", "typecheck"):
                if isinstance(scripts.get(name), str):
                    command = f"{runner} {name}" if runner != "yarn" else f"yarn {name}"
                    commands.append({"kind": "test" if name == "test" else "build" if name == "build" else "check", "command": command, "source": "package.json"})
        except (OSError, ValueError):
            pass
    if (root / "pyproject.toml").is_file() or (root / "pytest.ini").is_file() or (root / "tests").is_dir():
        commands.append({"kind": "test", "command": "python -m pytest", "source": "Python project"})
    elif any(root.glob("test*.py")):
        commands.append({"kind": "test", "command": "python -m unittest discover", "source": "Python tests"})
    if (root / "Cargo.toml").is_file():
        commands += [{"kind": "test", "command": "cargo test", "source": "Cargo.toml"}, {"kind": "check", "command": "cargo check", "source": "Cargo.toml"}]
    if (root / "go.mod").is_file():
        commands.append({"kind": "test", "command": "go test ./...", "source": "go.mod"})
    if (root / "pom.xml").is_file():
        commands.append({"kind": "test", "command": "mvn test", "source": "pom.xml"})
    if (root / "gradlew.bat").is_file() or (root / "gradlew").is_file():
        wrapper = ".\\gradlew.bat" if os.name == "nt" else "./gradlew"
        commands.append({"kind": "test", "command": f"{wrapper} test", "source": "Gradle wrapper"})
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in commands:
        if item["command"] not in seen:
            seen.add(item["command"])
            unique.append(item)
    return unique


def register_tools(registry: ToolRegistry) -> ProcessManager:
    processes = ProcessManager()

    @registry.tool(name="code_read", description="读取代码或文本文件并附行号。可指定起始行和行数。", parameters={"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 2000}}, "required": ["path"]})
    async def code_read(path: str, offset: int = 1, limit: int = 2000) -> str:
        try:
            p = _path(path)
            if not p.is_file() or p.is_symlink():
                return f"文件不存在或不是普通文件:{path}"
            if p.stat().st_size > 20_000_000:
                return "文件超过20MB，请按更小范围或使用专门工具处理"
            lines = _decode(p.read_bytes()).splitlines()
            start = max(0, int(offset) - 1)
            chunk = lines[start:start + max(1, min(int(limit), 2000))]
            return "\n".join(f"{start + i + 1}\t{line}" for i, line in enumerate(chunk)) or "(空文件)"
        except Exception as exc:
            return f"读取失败:{exc}"

    @registry.tool(name="code_write", description="原子方式新建或覆盖一个代码/文本文件。", parameters={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}, side_effect=True)
    async def code_write(path: str, content: str) -> str:
        try:
            p = _path(path)
            _reject_protected(p)
            if p.exists() and (not p.is_file() or p.is_symlink()):
                return f"只允许写普通文件:{p}"
            _atomic_write(p, content.encode("utf-8"))
            return f"已原子写入 {len(content)} 字符到 {p}"
        except Exception as exc:
            return f"写入失败:{exc}"

    @registry.tool(name="code_edit", description="精确替换文件中的唯一文本片段；找不到或不唯一时不会写入。", parameters={"type": "object", "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["path", "old_string", "new_string"]}, side_effect=True)
    async def code_edit(path: str, old_string: str, new_string: str) -> str:
        try:
            p = _path(path)
            _reject_protected(p)
            if not p.is_file() or p.is_symlink():
                return f"文件不存在或不是普通文件:{path}"
            if not old_string:
                return "old_string 不能为空"
            text = _decode(p.read_bytes())
            count = text.count(old_string)
            if count == 0:
                return "没找到要替换的内容，请先 code_read 查看原文。"
            if count > 1:
                return f"要替换的内容出现了 {count} 次，请增加上下文使其唯一。"
            _atomic_write(p, text.replace(old_string, new_string).encode("utf-8"))
            return f"已修改 {p}(替换 1 处)"
        except Exception as exc:
            return f"修改失败:{exc}"

    @registry.tool(name="code_apply_edits", description="一次校验并提交多个文件的精确替换；任一替换失败则所有文件都保持不变。", parameters={"type": "object", "properties": {"root": {"type": "string"}, "edits": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "object", "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["path", "old_string", "new_string"], "additionalProperties": False}}}, "required": ["root", "edits"]}, side_effect=True)
    async def code_apply_edits(root: str, edits: list[dict[str, str]]) -> str:
        try:
            base = _path(root)
            if not base.is_dir() or not 1 <= len(edits) <= 50:
                raise ValueError("项目目录无效或修改数量不在 1-50 范围内")
            grouped: dict[Path, list[dict[str, str]]] = {}
            for edit in edits:
                grouped.setdefault(_inside(base, edit.get("path", "")), []).append(edit)
            if len(grouped) > 20:
                raise ValueError("一次最多修改 20 个文件")
            changes: dict[Path, bytes | None] = {}
            applied = 0
            for path, file_edits in grouped.items():
                _reject_protected(path)
                if not path.is_file() or path.is_symlink():
                    raise ValueError(f"文件不存在或不是普通文件:{path}")
                if path.stat().st_size > 20_000_000:
                    raise ValueError(f"文件超过20MB:{path}")
                text = _decode(path.read_bytes())
                for edit in file_edits:
                    old, new = edit.get("old_string", ""), edit.get("new_string", "")
                    if not old:
                        raise ValueError(f"old_string 不能为空:{_relative(base, path)}")
                    count = text.count(old)
                    if count != 1:
                        raise ValueError(f"{_relative(base, path)} 的目标出现 {count} 次，应为 1 次")
                    text = text.replace(old, new)
                    applied += 1
                changes[path] = text.encode("utf-8")
            _transaction_apply(changes)
            return _json({"ok": True, "files": len(changes), "edits": applied, "paths": [_relative(base, p) for p in changes]})
        except Exception as exc:
            return _json({"ok": False, "error": str(exc)})

    @registry.tool(name="code_search", description="在项目中搜索关键字或正则；优先使用 rg，并自动忽略依赖和构建目录。", parameters={"type": "object", "properties": {"directory": {"type": "string"}, "pattern": {"type": "string"}, "ext": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 200}}, "required": ["directory", "pattern"]})
    async def code_search(directory: str, pattern: str, ext: str = "", max_results: int = 100) -> str:
        try:
            root = _path(directory)
            if not root.is_dir() or not pattern or len(pattern) > 2_000:
                raise ValueError("目录或搜索内容无效")
            maximum = max(1, min(int(max_results), 200))
            hits, engine = await asyncio.to_thread(_search, root, pattern, ext, maximum)
            return _json({"engine": engine, "count": len(hits), "truncated": len(hits) >= maximum, "matches": hits})
        except Exception as exc:
            return _json({"error": str(exc), "matches": []})

    @registry.tool(name="code_project_instructions", description="发现并读取当前路径适用的 AGENTS.md，同时列出项目内其他 AGENTS.md。", parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
    async def code_project_instructions(path: str) -> str:
        try:
            start = _path(path)
            current = start if start.is_dir() else start.parent
            root = await asyncio.to_thread(_discover_project_root, current)
            chain = [root]
            if current != root:
                rel_parts = current.relative_to(root).parts
                chain += [root.joinpath(*rel_parts[:i]) for i in range(1, len(rel_parts) + 1)]
            applicable, total = [], 0
            for directory in chain:
                instruction = directory / "AGENTS.md"
                if instruction.is_file() and not instruction.is_symlink():
                    content = _decode(instruction.read_bytes())[:32_000]
                    remaining = max(0, 64_000 - total)
                    content = content[:remaining]
                    total += len(content)
                    applicable.append({"path": str(instruction), "content": content})
            all_files = await asyncio.to_thread(_all_instruction_files, root)
            return _json({"project_root": str(root), "applicable": applicable, "all_instruction_files": all_files})
        except Exception as exc:
            return _json({"error": str(exc)})

    @registry.tool(name="code_git_status", description="结构化返回 Git 分支和工作区状态，不修改仓库。", parameters={"type": "object", "properties": {"repo": {"type": "string"}}, "required": ["repo"]})
    async def code_git_status(repo: str) -> str:
        try:
            root = await asyncio.to_thread(_repo_root, repo)
            _, raw, _ = await asyncio.to_thread(
                _git, root, ["status", "--porcelain=v1", "--branch", "-z"], 30, 2_000_000,
            )
            records, branch, files, index = raw.split("\0"), "", [], 0
            while index < len(records):
                record, index = records[index], index + 1
                if not record:
                    continue
                if record.startswith("## "):
                    branch = record[3:]
                    continue
                status, path = record[:2], record[3:]
                item = {"index": status[0], "worktree": status[1], "path": path}
                if status[0] in "RC" and index < len(records):
                    item["original_path"], index = records[index], index + 1
                files.append(item)
            return _json({"repo": str(root), "branch": branch, "clean": not files, "files": files})
        except Exception as exc:
            return _json({"error": str(exc)})

    @registry.tool(name="code_git_diff", description="结构化返回 Git diff；可查看暂存区并限制到一个项目内路径。", parameters={"type": "object", "properties": {"repo": {"type": "string"}, "staged": {"type": "boolean"}, "path": {"type": "string"}}, "required": ["repo"]})
    async def code_git_diff(repo: str, staged: bool = False, path: str = "") -> str:
        try:
            root = await asyncio.to_thread(_repo_root, repo)
            args = ["diff", "--no-ext-diff", "--no-color"] + (["--cached"] if staged else [])
            if path:
                target = _inside(root, path)
                args += ["--", _relative(root, target)]
            _, diff, truncated = await asyncio.to_thread(_git, root, args, 30, 60_000)
            return _json({"repo": str(root), "staged": bool(staged), "truncated": truncated, "diff": diff})
        except Exception as exc:
            return _json({"error": str(exc)})

    @registry.tool(name="code_git_log", description="结构化返回最近 Git 提交记录。", parameters={"type": "object", "properties": {"repo": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, "required": ["repo"]})
    async def code_git_log(repo: str, limit: int = 10) -> str:
        try:
            root = await asyncio.to_thread(_repo_root, repo)
            count = max(1, min(int(limit), 50))
            _, raw, _ = await asyncio.to_thread(
                _git, root, ["log", f"-{count}", "--pretty=format:%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1e"],
                30, 40_000,
            )
            commits = []
            for row in raw.split("\x1e"):
                fields = row.strip().split("\x1f")
                if len(fields) == 5:
                    commits.append(dict(zip(("hash", "short_hash", "author", "date", "subject"), fields)))
            return _json({"repo": str(root), "commits": commits})
        except Exception as exc:
            return _json({"error": str(exc)})

    @registry.tool(name="code_checkpoint_create", description="在隔离工作区创建当前工作区安全检查点，保存已修改、已删除和未跟踪文件及暂存状态。", parameters={"type": "object", "properties": {"repo": {"type": "string"}, "label": {"type": "string", "maxLength": 120}}, "required": ["repo"]}, side_effect=True)
    async def code_checkpoint_create(repo: str, label: str = "") -> str:
        try:
            root = await asyncio.to_thread(_repo_root, repo)
            return _json(await asyncio.to_thread(_create_checkpoint, root, label))
        except Exception as exc:
            return _json({"ok": False, "error": str(exc)})

    @registry.tool(name="code_checkpoint_restore", description="两步恢复 Git 检查点。首次不传令牌只返回影响清单；确认后传回令牌，状态有变化则拒绝恢复。", parameters={"type": "object", "properties": {"repo": {"type": "string"}, "checkpoint_id": {"type": "string"}, "confirm_token": {"type": "string"}}, "required": ["repo", "checkpoint_id"]}, side_effect=True, danger=True)
    async def code_checkpoint_restore(repo: str, checkpoint_id: str, confirm_token: str = "") -> str:
        try:
            root = await asyncio.to_thread(_repo_root, repo)
            checkpoint = await asyncio.to_thread(_checkpoint_dir, root, checkpoint_id)
            # 兼容旧版本的安全检查点；新检查点只写隔离工作区。
            if not checkpoint.is_dir():
                git_dir = await asyncio.to_thread(_git_dir, root)
                checkpoint = git_dir / "xiaoa-checkpoints" / checkpoint_id
            manifest = json.loads((checkpoint / "manifest.json").read_text(encoding="utf-8"))
            if _path(manifest["repo"]) != root:
                raise ValueError("检查点不属于当前项目")
            git_dir = await asyncio.to_thread(_git_dir, root)
            if (git_dir / "index.lock").exists():
                raise ValueError("Git 正在进行其他操作(index.lock 存在)，稍后再恢复")
            changes, preview, token = await asyncio.to_thread(_restore_plan, root, manifest, checkpoint)
            if not confirm_token:
                return _json({"ok": True, "preview": True, "changes": preview, "confirm_token": token})
            if not re.fullmatch(r"[0-9a-f]{24}", confirm_token) or confirm_token != token:
                raise ValueError("确认令牌已失效；文件或暂存区可能发生变化，请重新预览")
            await asyncio.to_thread(_transaction_apply, changes)
            for path, data in changes.items():
                if data is None and path.parent != git_dir:
                    parent = path.parent
                    while parent != root:
                        try:
                            parent.rmdir()
                        except OSError:
                            break
                        parent = parent.parent
            return _json({"ok": True, "restored": checkpoint_id, "changes": preview})
        except Exception as exc:
            return _json({"ok": False, "error": str(exc)})

    @registry.tool(name="code_process_start", description="在隔离工作区启动可持续读取输出的长命令；仅用户指定目录时填写 cwd，同时最多4个。", parameters={"type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}}, "required": ["command"]}, side_effect=True, danger=True)
    async def code_process_start(command: str, cwd: str = "") -> str:
        try:
            directory = _path(cwd) if cwd else isolation_workspace.workspace()
            if not directory.is_dir():
                raise ValueError(f"目录不存在:{cwd}")
            return _json(processes.start(command, directory))
        except Exception as exc:
            return _json({"ok": False, "error": str(exc)})

    @registry.tool(name="code_process_poll", description="读取长命令从 cursor 开始的新输出和运行状态。", parameters={"type": "object", "properties": {"process_id": {"type": "string"}, "cursor": {"type": "integer", "minimum": 0}, "max_chars": {"type": "integer", "minimum": 1, "maximum": 12000}}, "required": ["process_id"]})
    async def code_process_poll(process_id: str, cursor: int = 0, max_chars: int = 6000) -> str:
        try:
            return _json(processes.poll(process_id, max(0, int(cursor)), max(1, min(int(max_chars), 12000))))
        except Exception as exc:
            return _json({"ok": False, "error": str(exc)})

    @registry.tool(name="code_process_write", description="向仍在运行的长命令标准输入写入文本。", parameters={"type": "object", "properties": {"process_id": {"type": "string"}, "text": {"type": "string", "maxLength": 12000}, "newline": {"type": "boolean"}}, "required": ["process_id", "text"]}, side_effect=True)
    async def code_process_write(process_id: str, text: str, newline: bool = True) -> str:
        try:
            if len(text) > 12_000:
                raise ValueError("单次输入不能超过12000字符")
            return _json(processes.write(process_id, text, bool(newline)))
        except Exception as exc:
            return _json({"ok": False, "error": str(exc)})

    @registry.tool(name="code_process_stop", description="停止一个长命令及其子进程。", parameters={"type": "object", "properties": {"process_id": {"type": "string"}}, "required": ["process_id"]}, side_effect=True, danger=True)
    async def code_process_stop(process_id: str) -> str:
        try:
            return _json(await asyncio.to_thread(processes.stop, process_id))
        except Exception as exc:
            return _json({"ok": False, "error": str(exc)})

    @registry.tool(name="code_detect_commands", description="读取项目清单并给出可用的测试、检查和构建命令，不执行命令。", parameters={"type": "object", "properties": {"root": {"type": "string"}}, "required": ["root"]})
    async def code_detect_commands(root: str) -> str:
        try:
            directory = _path(root)
            if not directory.is_dir():
                raise ValueError(f"目录不存在:{root}")
            return _json({"root": str(directory), "commands": _detect_commands(directory)})
        except Exception as exc:
            return _json({"error": str(exc), "commands": []})

    @registry.tool(name="code_verify", description="执行一条测试/检查/构建命令；未指定时从项目清单选择首个测试或检查命令。", parameters={"type": "object", "properties": {"root": {"type": "string"}, "command": {"type": "string"}, "timeout": {"type": "integer", "minimum": 1, "maximum": 900}}, "required": ["root"]}, side_effect=True, danger=True)
    async def code_verify(root: str, command: str = "", timeout: int = 120) -> str:
        try:
            directory = _path(root)
            if not directory.is_dir():
                raise ValueError(f"目录不存在:{root}")
            if not command:
                detected = _detect_commands(directory)
                if not detected:
                    return _json({"ok": False, "error": "未识别到验证命令，请显式提供 command"})
                command = detected[0]["command"]
            if len(command) > _MAX_COMMAND:
                raise ValueError("命令过长")
            seconds = max(1, min(int(timeout), 900))
            try:
                completed = await asyncio.to_thread(
                    subprocess.run, command, shell=True, cwd=str(directory),
                    capture_output=True, timeout=seconds,
                    env=subprocess_env_without_secrets(),
                    **hidden_subprocess_kwargs(),
                )
                output = _decode((completed.stdout or b"") + (completed.stderr or b""))
                return _json({"ok": completed.returncode == 0, "command": command, "exit_code": completed.returncode, "truncated": len(output) > 12_000, "output": output[:12_000]})
            except subprocess.TimeoutExpired as exc:
                output = _decode((exc.stdout or b"") + (exc.stderr or b""))
                return _json({"ok": False, "command": command, "timeout": seconds, "output": output[:12_000]})
        except Exception as exc:
            return _json({"ok": False, "error": str(exc)})

    @registry.tool(name="code_run", description="兼容工具：默认在隔离工作区执行短命令；仅用户指定目录时填写 cwd，长任务请使用 code_process_start。", parameters={"type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}, "timeout": {"type": "integer", "minimum": 1, "maximum": 900}}, "required": ["command"]}, side_effect=True, danger=True)
    async def code_run(command: str, cwd: str = "", timeout: int = 120) -> str:
        result = json.loads(await code_verify(cwd or str(isolation_workspace.workspace()), command, timeout))
        if "exit_code" in result:
            return f"[退出码 {result['exit_code']}]\n" + (result.get("output", "").strip() or "(无输出)")
        if "timeout" in result:
            return f"命令超时({result['timeout']}秒)\n" + result.get("output", "")
        return f"执行失败:{result.get('error', '未知错误')}"

    return processes
