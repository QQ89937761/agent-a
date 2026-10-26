"""集中管理小A运行期间产生的临时文件和工作缓存。"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

from backend.core.config import app_home


DEFAULT_MAX_MB = 1024
MIN_MAX_MB = 128
MAX_MAX_MB = 10 * 1024


def root() -> Path:
    """隔离区固定在应用数据目录，避免设置页传入任意路径造成误清理。"""
    return (app_home() / "data" / "isolation").resolve()


def prepare() -> Path:
    path = root()
    (path / "workspace").mkdir(parents=True, exist_ok=True)
    (path / "tmp").mkdir(parents=True, exist_ok=True)
    return path


def workspace() -> Path:
    """用户未指定保存位置时，所有任务产物共用这个默认目录。"""
    return prepare() / "workspace"


def temporary() -> Path:
    """任务临时文件的固定入口，不依赖系统临时目录或进程当前目录。"""
    return prepare() / "tmp"


def configure_process_temp() -> Path:
    """让 Python 临时文件和子进程临时目录默认落到隔离区。"""
    path = temporary()
    os.environ["XIAOA_WORKSPACE"] = str(workspace())
    for name in ("TMP", "TEMP", "TMPDIR"):
        os.environ[name] = str(path)
    # tempfile caches the first resolved directory, so update it explicitly.
    tempfile.tempdir = str(path)
    return path


def _active_runtime_root() -> Path | None:
    """Return the current frozen app root so cleanup cannot corrupt a live runtime."""
    try:
        current = Path(sys.executable).resolve().parent
        while current != current.parent:
            if current.name.lower() == "resources":
                root = current.parent
                backend = current / "backend" / "xiaoa-backend.exe"
                return root if backend.is_file() else None
            current = current.parent
    except OSError:
        pass
    return None


def cleanup_stale_updates(protected: tuple[Path, ...] = ()) -> int:
    """Remove only old, flat updater workspaces inside the fixed temp root."""
    temp_root = (root() / "tmp").resolve()
    protected = tuple(path.resolve() for path in protected)
    active = _active_runtime_root()
    deleted = 0
    if not temp_root.is_dir():
        return deleted
    for directory in temp_root.glob("xiaoa-update-*"):
        try:
            resolved = directory.resolve()
            if (directory.is_symlink() or directory.is_junction() or
                    resolved.parent != temp_root or not directory.is_dir() or
                    any(path == resolved or path.is_relative_to(resolved) for path in protected) or
                    (active is not None and (active == resolved or active.is_relative_to(resolved)))):
                continue
            entries = list(directory.iterdir())
            # Updater workspaces are flat; unknown nested trees are never followed.
            if any(item.is_symlink() or not item.is_file() or item.resolve().parent != resolved for item in entries):
                continue
            newest = max([directory.stat().st_mtime] + [item.stat().st_mtime for item in entries])
            if time.time() - newest <= 24 * 60 * 60:
                continue
            for item in entries:
                item.unlink()
            directory.rmdir()
            deleted += 1
        except OSError:
            continue  # An updater may still hold the EXE open.
    return deleted


def normalize_max_mb(value: int | float | str | None) -> int:
    try:
        number = int(value) if value is not None else DEFAULT_MAX_MB
    except (TypeError, ValueError):
        number = DEFAULT_MAX_MB
    return max(MIN_MAX_MB, min(number, MAX_MAX_MB))


def _iter_files(path: Path):
    if not path.exists():
        return
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                yield item
        except OSError:
            continue


def usage_bytes(path: Path | None = None) -> int:
    total = 0
    for item in _iter_files(path or root()):
        try:
            total += item.stat().st_size
        except OSError:
            continue
    return total


def cleanup(max_mb: int | float | str | None = None) -> dict[str, int]:
    """只清理临时文件和日志；工作区成品与安全备份永不自动删除。"""
    path = prepare()
    limit = normalize_max_mb(max_mb) * 1024 * 1024
    current = usage_bytes(path)
    deleted_files = 0
    deleted_bytes = 0
    if current <= limit:
        return {
            "used_bytes": current, "limit_bytes": limit,
            "deleted_files": 0, "deleted_bytes": 0,
        }

    candidates = []
    workspace_root = path / "workspace"
    runtime_root = _active_runtime_root()
    protected = (workspace_root, path / "backups", runtime_root)
    for item in _iter_files(path):
        if any(candidate is not None and (item == candidate or item.is_relative_to(candidate))
               for candidate in protected):
            continue
        try:
            stat = item.stat()
            candidates.append((stat.st_mtime, item, stat.st_size))
        except OSError:
            continue
    candidates.sort(key=lambda entry: entry[0])
    for _, item, size in candidates:
        if current <= limit:
            break
        try:
            item.unlink()
        except OSError:
            continue
        current -= size
        deleted_files += 1
        deleted_bytes += size

    # Remove empty directories, but keep the two stable entry points.
    for directory in sorted((item for item in path.rglob("*") if item.is_dir()),
                            key=lambda item: len(item.parts), reverse=True):
        if directory == path / "tmp" or any(candidate is not None and
                (directory == candidate or directory.is_relative_to(candidate))
                for candidate in protected):
            continue
        try:
            directory.rmdir()
        except OSError:
            pass
    return {
        "used_bytes": max(0, current), "limit_bytes": limit,
        "deleted_files": deleted_files, "deleted_bytes": deleted_bytes,
    }


def status(max_mb: int | float | str | None = None) -> dict[str, object]:
    path = prepare()
    limit_mb = normalize_max_mb(max_mb)
    used = usage_bytes(path)
    return {
        "path": str(path),
        "max_mb": limit_mb,
        "used_mb": round(used / 1024 / 1024, 2),
        "used_bytes": used,
        "limit_bytes": limit_mb * 1024 * 1024,
    }
