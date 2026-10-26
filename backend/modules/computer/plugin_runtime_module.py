"""Versioned, isolated Python/Node plugins exposed as XiaoA tools."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any

from backend.core.process import hidden_subprocess_kwargs
from backend.core import isolation_workspace
from backend.core.tools import Tool, ToolRegistry, with_risk_confirmation
from backend.modules.computer.user_tool_module import (
    _NAME_RE, _validate_arguments, _validate_schema,
)

_MANIFEST = "plugin.json"
_STATE = "state.json"
_VERSION = ".xiaoa-version.json"
_MAX_SOURCE_BYTES = 20 * 1024 * 1024
_MAX_FILES = 200
_MAX_INPUT_BYTES = 1024 * 1024
_MAX_OUTPUT_BYTES = 1024 * 1024
_PYTHON_DEP = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9][A-Za-z0-9._+!-]*$"
)
_NODE_NAME = re.compile(r"^(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._-]*$")
_NODE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$")
_NPM_REGISTRY_KEY = re.compile(r"^(?:registry|@[a-z0-9._-]+:registry)$", re.IGNORECASE)
_ALLOWED_KEYS = {
    "name", "description", "runtime", "entry", "parameters", "dependencies",
    "test_args", "side_effect", "timeout",
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name} 不是有效的 UTF-8 JSON。") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} 必须是 JSON 对象。")
    return value


def _safe_entry(value: Any, runtime: str) -> str:
    entry = str(value or "").replace("\\", "/")
    path = PurePosixPath(entry)
    expected = ".py" if runtime == "python" else ".js"
    if (
        not entry or path.is_absolute() or any(part in ("", ".", "..") for part in path.parts)
        or ":" in path.parts[0] or path.suffix.lower() != expected
    ):
        raise ValueError(f"entry 必须是插件目录内的 {expected} 文件。")
    return entry


def _minimal_env(home: Path, *, install: bool = False) -> dict[str, str]:
    """Keep OS essentials, deliberately omit XiaoA/API/cloud credentials."""
    allowed = ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "TEMP", "TMP")
    env = {key: os.environ[key] for key in allowed if os.environ.get(key)}
    proxy_keys = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
    env.update({key: os.environ[key] for key in proxy_keys if os.environ.get(key)})
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "XIAOA_PLUGIN_HOME": str(home), "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1", "NO_COLOR": "1",
    })
    if not install:
        return env

    # Installers may use the user's network route and registry mirror, but plugin
    # code still runs with the isolated environment above.
    install_keys = (
        "NPM_CONFIG_REGISTRY", "npm_config_registry",
        "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST", "PIP_CONFIG_FILE",
    )
    env.update({key: os.environ[key] for key in install_keys if os.environ.get(key)})
    user_home_value = os.getenv("USERPROFILE") or os.getenv("HOME")
    user_home = Path(user_home_value) if user_home_value else None
    configured_npmrc = os.getenv("NPM_CONFIG_USERCONFIG")
    npmrc = Path(configured_npmrc) if configured_npmrc else (user_home / ".npmrc" if user_home else None)
    if npmrc and npmrc.is_file():
        try:
            registry_lines = []
            for line in npmrc.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = line.strip()
                key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
                if _NPM_REGISTRY_KEY.fullmatch(key):
                    registry_lines.append(stripped)
            if registry_lines:
                safe_npmrc = home / ".npmrc.registry"
                safe_npmrc.write_text("\n".join(registry_lines) + "\n", encoding="utf-8")
                env["NPM_CONFIG_USERCONFIG"] = str(safe_npmrc)
        except (OSError, UnicodeError):
            pass
    pip_configs = [
        Path(os.environ["APPDATA"]) / "pip" / "pip.ini" if os.getenv("APPDATA") else None,
        Path(os.environ["XDG_CONFIG_HOME"]) / "pip" / "pip.conf"
        if os.getenv("XDG_CONFIG_HOME") else None,
        user_home / ".config" / "pip" / "pip.conf" if user_home else None,
        user_home / ".pip" / "pip.conf" if user_home else None,
    ]
    if "PIP_CONFIG_FILE" not in env:
        config = next((path for path in pip_configs if path and path.is_file()), None)
        if config:
            env["PIP_CONFIG_FILE"] = str(config)
    return env


class PluginRuntimeManager:
    """Install immutable plugin versions and register only their active version."""

    def __init__(
        self, root: Path, registry: ToolRegistry, *,
        python_executable: str | None = None, node_executable: str | None = None,
        npm_executable: str | None = None,
        install_timeout: int = 300,
    ):
        self.root = Path(root)
        self.registry = registry
        self.runtime_error = os.environ.get("XIAOA_PLUGIN_RUNTIME_ERROR", "")
        # A frozen backend executable is not a Python interpreter. Portable builds
        # pass the separately bundled runtimes through these environment variables.
        fallback_python = None if getattr(sys, "frozen", False) else sys.executable
        self.python_executable = (
            python_executable or os.environ.get("XIAOA_PLUGIN_PYTHON")
            or fallback_python or shutil.which("python3") or shutil.which("python")
        )
        self.node_executable = (
            node_executable or os.environ.get("XIAOA_PLUGIN_NODE") or shutil.which("node")
        )
        self.npm_executable = (
            npm_executable or os.environ.get("XIAOA_PLUGIN_NPM")
            or shutil.which("npm.cmd" if os.name == "nt" else "npm")
        )
        self.install_timeout = max(30, min(int(install_timeout), 900))
        self.staging_root = self.root / "_staging"
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self._loaded: set[str] = set()

    @staticmethod
    def validate_manifest(raw: dict[str, Any]) -> dict[str, Any]:
        extras = sorted(set(raw) - _ALLOWED_KEYS)
        if extras:
            raise ValueError("plugin.json 包含不支持的字段: " + ", ".join(extras))
        name = str(raw.get("name") or "")
        description = str(raw.get("description") or "").strip()
        runtime = str(raw.get("runtime") or "").lower()
        parameters = raw.get("parameters")
        if not _NAME_RE.fullmatch(name):
            raise ValueError("插件名必须是3-64位小写字母、数字、下划线，且以字母开头。")
        if not description or len(description) > 500:
            raise ValueError("插件描述不能为空且最多500字。")
        if runtime not in {"python", "node"}:
            raise ValueError("runtime 只能是 python 或 node。")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ValueError("parameters 必须是 type=object 的 JSON Schema。")
        _validate_schema(parameters)
        entry = _safe_entry(raw.get("entry"), runtime)
        dependencies = raw.get("dependencies", [] if runtime == "python" else {})
        if runtime == "python":
            if not isinstance(dependencies, list) or any(
                not isinstance(item, str) or not _PYTHON_DEP.fullmatch(item)
                for item in dependencies
            ):
                raise ValueError("Python dependencies 必须是固定版本数组，例如 requests==2.32.3。")
            dependencies = list(dict.fromkeys(dependencies))
        else:
            if not isinstance(dependencies, dict) or any(
                not isinstance(name_, str) or not _NODE_NAME.fullmatch(name_)
                or not isinstance(version, str) or not _NODE_VERSION.fullmatch(version)
                for name_, version in dependencies.items()
            ):
                raise ValueError("Node dependencies 必须是固定版本对象，例如 {\"lodash\":\"4.17.21\"}。")
            dependencies = dict(sorted(dependencies.items()))
        test_args = raw.get("test_args", {})
        if not isinstance(test_args, dict):
            raise ValueError("test_args 必须是对象。")
        _validate_arguments(parameters, test_args)
        return {
            "name": name, "description": description, "runtime": runtime, "entry": entry,
            "parameters": parameters, "dependencies": dependencies, "test_args": test_args,
            "side_effect": bool(raw.get("side_effect", True)),
            "timeout": max(1, min(int(raw.get("timeout", 120)), 300)),
        }

    def inspect_directory(self, source: Path) -> dict[str, Any]:
        source = Path(source).resolve()
        manifest = self.validate_manifest(_read_json(source / _MANIFEST))
        entry = (source / Path(*PurePosixPath(manifest["entry"]).parts)).resolve()
        if source not in entry.parents or not entry.is_file():
            raise ValueError("plugin.json 指定的入口文件不存在。")
        files, total = [], 0
        for path in source.rglob("*"):
            if path.is_symlink():
                raise ValueError("插件不能包含符号链接。")
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            if any(part in {".venv", "node_modules", "_staging"} for part in relative.parts):
                raise ValueError("插件源码不能自带运行环境或 node_modules。")
            total += path.stat().st_size
            files.append(str(relative).replace("\\", "/"))
            if len(files) > _MAX_FILES or total > _MAX_SOURCE_BYTES:
                raise ValueError("插件源码超过 200 个文件或 20MB 限制。")
        return {"manifest": manifest, "files": sorted(files), "source_bytes": total}

    def _plugin_dir(self, name: str) -> Path:
        if not _NAME_RE.fullmatch(str(name or "")):
            raise ValueError("插件名称格式错误。")
        return self.root / name

    def _state(self, name: str) -> dict[str, Any]:
        return _read_json(self._plugin_dir(name) / _STATE)

    @staticmethod
    def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _version_dir(plugin_dir: Path, version: int) -> Path:
        return plugin_dir / "versions" / f"v{version}"

    def _python(self, version_dir: Path) -> Path:
        relative = "Scripts/python.exe" if os.name == "nt" else "bin/python"
        return version_dir / ".venv" / relative

    def _run_install(self, command: list[str], cwd: Path, env: dict[str, str]) -> None:
        result = subprocess.run(
            command, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=self.install_timeout,
            **hidden_subprocess_kwargs(),
        )
        output = ((result.stdout or b"") + (result.stderr or b""))[:20_000].decode(
            "utf-8", errors="replace",
        )
        if result.returncode:
            raise RuntimeError(f"插件依赖安装失败({result.returncode}): {output}")

    def _prepare_environment(self, version_dir: Path, manifest: dict[str, Any]) -> None:
        if self.runtime_error:
            raise RuntimeError(self.runtime_error)
        env = _minimal_env(version_dir, install=True)
        if manifest["runtime"] == "python":
            if not self.python_executable:
                raise RuntimeError("没有找到插件 Python 运行时，无法安装 Python 插件。")
            self._run_install([
                str(self.python_executable), "-m", "venv", "--without-pip",
                str(version_dir / ".venv"),
            ], version_dir, env)
            if manifest["dependencies"]:
                site_packages = version_dir / ".venv" / (
                    "Lib/site-packages" if os.name == "nt"
                    else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
                )
                self._run_install([
                    str(self.python_executable), "-m", "pip", "install", "--no-input",
                    "--disable-pip-version-check", "--only-binary=:all:",
                    "--target", str(site_packages),
                    *manifest["dependencies"],
                ], version_dir, env)
            return
        if not self.node_executable:
            raise RuntimeError("没有找到 Node.js，无法安装 Node 插件。")
        dependencies = manifest["dependencies"]
        if not dependencies:
            return
        if not self.npm_executable:
            raise RuntimeError("没有找到 npm，无法安装 Node 插件依赖。")
        env_dir = version_dir / ".node-env"
        env_dir.mkdir()
        (env_dir / "package.json").write_text(json.dumps({
            "name": f"xiaoa-plugin-{manifest['name']}", "private": True,
            "dependencies": dependencies,
        }, indent=2), encoding="utf-8")
        self._run_install([
            self.npm_executable, "install", "--ignore-scripts", "--omit=dev",
            "--no-audit", "--no-fund",
        ], env_dir, env)

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **hidden_subprocess_kwargs(),
            )
        else:
            process.kill()

    def _execute(self, version_dir: Path, manifest: dict[str, Any], arguments: dict[str, Any]) -> Any:
        if self.runtime_error:
            raise RuntimeError(self.runtime_error)
        _validate_arguments(manifest["parameters"], arguments)
        raw_input = json.dumps(arguments, ensure_ascii=False).encode("utf-8")
        if len(raw_input) > _MAX_INPUT_BYTES:
            raise ValueError("插件输入超过 1MB 限制。")
        code_dir = version_dir / "code"
        entry = code_dir.joinpath(*PurePosixPath(manifest["entry"]).parts)
        env = _minimal_env(version_dir)
        env["XIAOA_WORKSPACE"] = str(isolation_workspace.workspace())
        env["XIAOA_PLUGIN_HOME"] = str(code_dir)
        for name in ("TMP", "TEMP", "TMPDIR"):
            env[name] = str(isolation_workspace.temporary())
        if manifest["runtime"] == "python":
            # -I ignores PYTHONUTF8/PYTHONIOENCODING; the JSON pipe is UTF-8.
            command = [str(self._python(version_dir)), "-I", "-X", "utf8", str(entry)]
        else:
            command = [str(self.node_executable), str(entry)]
            node_path = version_dir / ".node-env" / "node_modules"
            if node_path.exists():
                env["NODE_PATH"] = str(node_path)
        process = subprocess.Popen(
            command, cwd=env["XIAOA_WORKSPACE"], env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            **hidden_subprocess_kwargs(),
        )
        overflow = threading.Event()
        output, errors = bytearray(), bytearray()

        def read_bounded(stream, destination: bytearray, limit: int) -> None:
            while chunk := stream.read(65_536):
                destination.extend(chunk)
                if len(destination) > limit:
                    overflow.set(); self._terminate(process); return

        def write_input() -> None:
            try:
                process.stdin.write(raw_input); process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        threads = [
            threading.Thread(target=read_bounded, args=(process.stdout, output, _MAX_OUTPUT_BYTES), daemon=True),
            threading.Thread(target=read_bounded, args=(process.stderr, errors, 20_000), daemon=True),
            threading.Thread(target=write_input, daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            process.wait(timeout=manifest["timeout"])
        except subprocess.TimeoutExpired as exc:
            self._terminate(process); process.wait(timeout=10)
            raise TimeoutError(f"插件执行超过 {manifest['timeout']} 秒。") from exc
        finally:
            for thread in threads:
                thread.join(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        if overflow.is_set():
            raise RuntimeError("插件输出超过限制（stdout 1MB / stderr 20KB）。")
        raw_output, raw_error = bytes(output), bytes(errors)
        error = raw_error.decode("utf-8", errors="replace")
        if process.returncode:
            raise RuntimeError(f"插件退出码 {process.returncode}: {error}")
        try:
            return json.loads(raw_output.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("插件 stdout 必须只输出一个 UTF-8 JSON 值。" + (f" stderr: {error}" if error else "")) from exc

    def _copy_source(self, source: Path, destination: Path) -> None:
        def ignored(_directory: str, names: list[str]) -> set[str]:
            return {name for name in names if name in {".git", ".venv", "node_modules", "_staging"}}
        shutil.copytree(source, destination, ignore=ignored)

    def install_from_directory(
        self, source: Path, source_meta: dict[str, Any], *, update_name: str | None = None,
    ) -> dict[str, Any]:
        inspected = self.inspect_directory(source)
        manifest = inspected["manifest"]
        name = manifest["name"]
        if update_name and update_name != name:
            raise ValueError("GitHub 插件名与已安装插件不一致。")
        plugin_dir = self._plugin_dir(name)
        state_path = plugin_dir / _STATE
        if update_name is None and state_path.exists():
            raise ValueError("插件已存在，请使用 update 更新。")
        if self.registry.get(name) is not None and name not in self._loaded:
            raise ValueError("插件名与内置或其他工具冲突。")
        old_state = self._state(name) if state_path.exists() else {}
        version = max(
            [int(path.name[1:]) for path in (plugin_dir / "versions").glob("v[0-9]*")
             if path.name[1:].isdigit()] or [0]
        ) + 1
        staging = Path(tempfile.mkdtemp(prefix=f"{name}-v{version}-", dir=self.staging_root))
        try:
            self._copy_source(Path(source), staging / "code")
            self._prepare_environment(staging, manifest)
            metadata = {
                "version": version, "installed_at": int(time.time()),
                "source_url": str(source_meta.get("source_url") or ""),
                "source_commit": source_meta.get("commit"),
                "repository": str(source_meta.get("repository") or ""),
                "manifest": manifest,
            }
            self._write_json_atomic(staging / _VERSION, metadata)
            test_output = self._execute(staging, manifest, manifest["test_args"])
            destination = self._version_dir(plugin_dir, version)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, destination)
            state = {
                "name": name, "active_version": version,
                "enabled": bool(old_state.get("enabled", True)),
                "source_url": metadata["source_url"], "source_commit": metadata["source_commit"],
                "repository": metadata["repository"],
            }
            self._write_json_atomic(state_path, state)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        result = self.reload()
        if state["enabled"] and name not in result["loaded"]:
            raise RuntimeError("插件写入成功但加载失败: " + "; ".join(result["errors"]))
        return {
            "name": name, "version": version, "enabled": state["enabled"],
            "runtime": manifest["runtime"], "test_output": test_output,
            "source_url": state["source_url"], "repository": state["repository"],
            "commit": state["source_commit"],
        }

    def _active(self, name: str) -> tuple[dict[str, Any], Path, dict[str, Any]]:
        state = self._state(name)
        version_dir = self._version_dir(self._plugin_dir(name), int(state["active_version"]))
        metadata = _read_json(version_dir / _VERSION)
        manifest = self.validate_manifest(metadata.get("manifest") or {})
        return state, version_dir, manifest

    def _handler(self, name: str, version_dir: Path, manifest: dict[str, Any]):
        async def run(**kwargs):
            result = await asyncio.to_thread(self._execute, version_dir, manifest, kwargs)
            return json.dumps(result, ensure_ascii=False)
        run.__name__ = name
        return run

    def reload(self) -> dict[str, list[str]]:
        for name in self._loaded:
            self.registry.unregister(name)
        self._loaded.clear()
        loaded, errors = [], []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or directory.name.startswith("_"):
                continue
            try:
                state, version_dir, manifest = self._active(directory.name)
                if not state.get("enabled", True):
                    continue
                if self.registry.get(manifest["name"]) is not None:
                    raise ValueError("插件名与内置或其他工具冲突。")
                self.registry.register(Tool(
                    name=manifest["name"], description=with_risk_confirmation(manifest["description"]),
                    parameters=manifest["parameters"],
                    handler=self._handler(manifest["name"], version_dir, manifest),
                    side_effect=manifest["side_effect"],
                    danger=True,
                ))
                self._loaded.add(manifest["name"]); loaded.append(manifest["name"])
            except Exception as exc:
                errors.append(f"{directory.name}: {exc}")
        return {"loaded": loaded, "errors": errors}

    def list_plugins(self) -> list[dict[str, Any]]:
        rows = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or directory.name.startswith("_"):
                continue
            try:
                state, _version_dir, manifest = self._active(directory.name)
                versions = sorted(
                    int(path.name[1:]) for path in (directory / "versions").glob("v[0-9]*")
                    if path.name[1:].isdigit()
                )
                rows.append({
                    "name": manifest["name"], "description": manifest["description"],
                    "runtime": manifest["runtime"], "version": int(state["active_version"]),
                    "versions": versions, "enabled": bool(state.get("enabled", True)),
                    "loaded": manifest["name"] in self._loaded,
                    "source": str(state.get("source_url") or "本地"),
                    "repository": str(state.get("repository") or ""),
                })
            except Exception as exc:
                rows.append({"name": directory.name, "loaded": False, "error": str(exc)})
        return rows

    def enable(self, name: str, enabled: bool) -> bool:
        state_path = self._plugin_dir(name) / _STATE
        if not state_path.is_file():
            return False
        state = _read_json(state_path); state["enabled"] = bool(enabled)
        self._write_json_atomic(state_path, state); self.reload()
        return True

    def rollback(self, name: str) -> dict[str, Any]:
        plugin_dir = self._plugin_dir(name); state = self._state(name)
        current = int(state["active_version"])
        previous = sorted(
            (int(path.name[1:]) for path in (plugin_dir / "versions").glob("v[0-9]*")
             if path.name[1:].isdigit() and int(path.name[1:]) < current),
            reverse=True,
        )
        if not previous:
            raise ValueError("没有可回滚的历史版本。")
        state["active_version"] = previous[0]
        metadata = _read_json(self._version_dir(plugin_dir, previous[0]) / _VERSION)
        for key in ("source_url", "source_commit", "repository"):
            state[key] = metadata.get(key)
        self._write_json_atomic(plugin_dir / _STATE, state); self.reload()
        return {"name": name, "version": previous[0]}
