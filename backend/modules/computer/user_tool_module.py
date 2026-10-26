"""Persistent user-created tools executed by built-in Windows PowerShell."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from backend.core.process import hidden_subprocess_kwargs
from backend.core import isolation_workspace
from backend.core.tools import Tool, ToolRegistry, with_risk_confirmation

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_MANIFEST = "tool.json"
_SCRIPT = "tool.ps1"
_JSON_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "gb18030", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _powershell() -> str:
    candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    return str(candidate) if candidate.exists() else "powershell.exe"


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            **hidden_subprocess_kwargs(),
        )
        if process.poll() is None:
            process.kill()
    else:
        process.kill()


def _validate_schema(schema: Any, path: str = "parameters") -> None:
    if not isinstance(schema, dict):
        raise ValueError(f"{path} 必须是 JSON Schema 对象。")
    if not schema:
        return
    schema_type = schema.get("type")
    if schema_type not in _JSON_TYPES:
        raise ValueError(f"{path}.type 不支持: {schema_type}")
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise ValueError(f"{path}.enum 必须是数组。")
    if schema_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ValueError(f"{path}.properties/required 格式错误。")
        if any(not isinstance(item, str) or item not in properties for item in required):
            raise ValueError(f"{path}.required 中存在未声明参数。")
        for name, child in properties.items():
            _validate_schema(child, f"{path}.{name}")
    elif schema_type == "array" and "items" in schema:
        _validate_schema(schema["items"], f"{path}.items")


def _validate_arguments(schema: dict[str, Any], value: Any, path: str = "参数") -> None:
    if not schema:
        return
    schema_type = schema.get("type")
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(schema_type, False)
    if not valid:
        raise ValueError(f"{path} 类型应为 {schema_type}。")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} 不在允许值中。")
    if schema_type == "object":
        properties = schema.get("properties", {})
        missing = [name for name in schema.get("required", []) if name not in value]
        if missing:
            raise ValueError(f"缺少必填参数: {', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            extras = [name for name in value if name not in properties]
            if extras:
                raise ValueError(f"存在未声明参数: {', '.join(extras)}")
        for name, item in value.items():
            if name in properties:
                _validate_arguments(properties[name], item, f"{path}.{name}")
    elif schema_type == "array" and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _validate_arguments(schema["items"], item, f"{path}[{index}]")
    elif schema_type in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} 不能小于 {schema['minimum']}。")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} 不能大于 {schema['maximum']}。")
    elif schema_type == "string":
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValueError(f"{path} 长度不足。")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValueError(f"{path} 长度超限。")


class UserToolManager:
    def __init__(self, root: Path, registry: ToolRegistry):
        self.root = Path(root)
        self.registry = registry
        self.history_root = self.root / "_history"
        self.root.mkdir(parents=True, exist_ok=True)
        self.history_root.mkdir(parents=True, exist_ok=True)
        self._loaded: set[str] = set()

    @staticmethod
    def _validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
        name = str(manifest.get("name") or "")
        description = str(manifest.get("description") or "").strip()
        parameters = manifest.get("parameters")
        if not _NAME_RE.fullmatch(name):
            raise ValueError("工具名必须是3-64位小写字母、数字、下划线,且以字母开头。")
        if not description or len(description) > 500:
            raise ValueError("工具描述不能为空且最多500字。")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ValueError("parameters 必须是 type=object 的 JSON Schema。")
        _validate_schema(parameters)
        properties = parameters.get("properties", {})
        required = parameters.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ValueError("parameters.properties/required 格式错误。")
        if any(item not in properties for item in required):
            raise ValueError("required 中存在未声明的参数。")
        timeout = max(1, min(int(manifest.get("timeout", 120)), 300))
        return {
            "name": name,
            "description": description,
            "parameters": parameters,
            "side_effect": bool(manifest.get("side_effect", True)),
            "timeout": timeout,
            "version": max(1, int(manifest.get("version", 1))),
            "enabled": bool(manifest.get("enabled", True)),
        }

    @staticmethod
    def _ps_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def _validate_script(self, path: Path) -> None:
        if path.stat().st_size > 100_000:
            raise ValueError("工具脚本不能超过100KB。")
        command = (
            "$tokens=$null;$errors=$null;"
            f"[System.Management.Automation.Language.Parser]::ParseFile({self._ps_quote(str(path))},"
            "[ref]$tokens,[ref]$errors)|Out-Null;"
            "if($errors.Count -gt 0){$errors|ForEach-Object{$_.Message};exit 1}"
        )
        result = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, timeout=20,
            **hidden_subprocess_kwargs(),
        )
        if result.returncode:
            raise ValueError("PowerShell 语法错误: " + _decode(result.stdout + result.stderr)[:2000])

    @staticmethod
    def _minimal_env() -> dict[str, str]:
        """只给脚本 OS 运行必需的环境变量,**故意不带** API key / GITHUB_TOKEN /
        云凭据等敏感项。之前是 dict(os.environ) 全量透传,等于把所有密钥交给
        模型现造的脚本——最容易被造出来的东西反而拿到最多密钥,已收窄。
        """
        allowed = (
            "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "TEMP", "TMP",
            "USERNAME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE", "PROGRAMFILES", "PROGRAMDATA", "APPDATA", "LOCALAPPDATA",
            "PSModulePath", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "no_proxy",
        )
        return {key: os.environ[key] for key in allowed if os.environ.get(key)}

    def _run_script(
        self, script: Path, arguments: dict[str, Any], timeout: int, *, test: bool = False,
    ) -> str:
        env = self._minimal_env()
        env["XIAOA_TOOL_ARGS"] = json.dumps(arguments or {}, ensure_ascii=False)
        env["XIAOA_TOOL_HOME"] = str(script.parent)
        env["XIAOA_WORKSPACE"] = str(isolation_workspace.workspace())
        for name in ("TMP", "TEMP", "TMPDIR"):
            env[name] = str(isolation_workspace.temporary())
        env["XIAOA_TOOL_TEST"] = "1" if test else "0"
        process = subprocess.Popen(
            [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            cwd=env["XIAOA_WORKSPACE"], env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            **hidden_subprocess_kwargs(),
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_tree(process)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            raise TimeoutError(f"工具执行超过 {timeout} 秒。") from exc
        output = _decode((stdout or b"") + (stderr or b"")).strip()
        if process.returncode:
            raise RuntimeError(f"工具退出码 {process.returncode}: {output[:4000]}")
        return output[:10000] or "执行成功。"

    def _load_manifest(self, directory: Path) -> tuple[dict[str, Any], Path]:
        manifest_path, script_path = directory / _MANIFEST, directory / _SCRIPT
        if not manifest_path.is_file() or not script_path.is_file():
            raise ValueError("缺少 tool.json 或 tool.ps1。")
        manifest = self._validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
        if directory.name != manifest["name"]:
            raise ValueError("目录名必须与工具名一致。")
        self._validate_script(script_path)
        return manifest, script_path

    def _handler(self, name: str, script: Path, timeout: int, parameters: dict[str, Any]):
        async def run(**kwargs):
            _validate_arguments(parameters, kwargs)
            return await asyncio.to_thread(self._run_script, script, kwargs, timeout)
        run.__name__ = name
        return run

    def reload(self) -> dict[str, Any]:
        for name in self._loaded:
            self.registry.unregister(name)
        self._loaded.clear()
        loaded, errors = [], []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or directory.name.startswith("_"):
                continue
            try:
                manifest, script = self._load_manifest(directory)
                if not manifest["enabled"]:
                    continue
                name = manifest["name"]
                if self.registry.get(name) is not None:
                    raise ValueError("工具名与内置工具冲突。")
                self.registry.register(Tool(
                    name=name,
                    description=with_risk_confirmation(manifest["description"]),
                    parameters=manifest["parameters"],
                    handler=self._handler(name, script, manifest["timeout"], manifest["parameters"]),
                    side_effect=manifest["side_effect"],
                    danger=True,
                ))
                self._loaded.add(name)
                loaded.append(name)
            except Exception as e:
                errors.append(f"{directory.name}: {e}")
        return {"loaded": loaded, "errors": errors}

    def _save_history(self, tool_dir: Path, version: int) -> None:
        if not tool_dir.exists():
            return
        destination = self.history_root / tool_dir.name / f"v{version}-{time.time_ns()}"
        destination.mkdir(parents=True, exist_ok=False)
        for filename in (_MANIFEST, _SCRIPT):
            source = tool_dir / filename
            if source.exists():
                shutil.copy2(source, destination / filename)

    def create(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        script: str,
        test_args: dict[str, Any] | None = None,
        side_effect: bool = True,
        timeout: int = 120,
    ) -> dict[str, Any]:
        if not isinstance(script, str) or not script.strip():
            raise ValueError("工具脚本不能为空。")
        existing = self.registry.get(name)
        if existing is not None and name not in self._loaded:
            raise ValueError("不能覆盖内置工具。")
        tool_dir = self.root / name
        old_version = 0
        if (tool_dir / _MANIFEST).exists():
            old = json.loads((tool_dir / _MANIFEST).read_text(encoding="utf-8"))
            old_version = int(old.get("version", 1))
        manifest = self._validate_manifest({
            "name": name, "description": description, "parameters": parameters,
            "side_effect": side_effect, "timeout": timeout,
            "version": old_version + 1, "enabled": True,
        })
        staging = Path(tempfile.mkdtemp(prefix=f"._{name}-", dir=self.root))
        try:
            (staging / _MANIFEST).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            (staging / _SCRIPT).write_text(script, encoding="utf-8-sig")
            self._validate_script(staging / _SCRIPT)
            _validate_arguments(manifest["parameters"], test_args or {})
            test_output = self._run_script(
                staging / _SCRIPT, test_args or {}, manifest["timeout"], test=True,
            )
            if tool_dir.exists():
                self._save_history(tool_dir, old_version)
            tool_dir.mkdir(parents=True, exist_ok=True)
            os.replace(staging / _SCRIPT, tool_dir / _SCRIPT)
            os.replace(staging / _MANIFEST, tool_dir / _MANIFEST)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        result = self.reload()
        if name not in result["loaded"]:
            raise RuntimeError("工具写入成功但加载失败: " + "; ".join(result["errors"]))
        return {"name": name, "version": manifest["version"], "test_output": test_output}

    def list_tools(self) -> list[dict[str, Any]]:
        rows = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or directory.name.startswith("_"):
                continue
            try:
                manifest = self._validate_manifest(json.loads(
                    (directory / _MANIFEST).read_text(encoding="utf-8")
                ))
                manifest["loaded"] = manifest["name"] in self._loaded
                rows.append(manifest)
            except Exception as e:
                rows.append({"name": directory.name, "loaded": False, "error": str(e)})
        return rows

    def disable(self, name: str, enabled: bool = False) -> bool:
        path = self.root / name / _MANIFEST
        if not path.is_file():
            return False
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["enabled"] = bool(enabled)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self.reload()
        return True

    def rollback(self, name: str) -> dict[str, Any]:
        tool_dir = self.root / name
        history = self.history_root / name
        versions = sorted(
            (p for p in history.iterdir() if p.is_dir()),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        ) if history.exists() else []
        if not versions:
            raise ValueError("没有可回滚的历史版本。")
        current = json.loads((tool_dir / _MANIFEST).read_text(encoding="utf-8"))
        self._save_history(tool_dir, int(current.get("version", 1)))
        selected = versions[0]
        shutil.copy2(selected / _SCRIPT, tool_dir / _SCRIPT)
        shutil.copy2(selected / _MANIFEST, tool_dir / _MANIFEST)
        result = self.reload()
        if name not in result["loaded"]:
            raise RuntimeError("回滚后加载失败: " + "; ".join(result["errors"]))
        manifest = json.loads((tool_dir / _MANIFEST).read_text(encoding="utf-8"))
        return {"name": name, "version": manifest.get("version")}


def register_tools(registry: ToolRegistry, root: Path) -> UserToolManager:
    manager = UserToolManager(root, registry)

    @registry.tool(
        name="tool_create",
        description=(
            "创建或升级一个可长期复用的 PowerShell 工具。只为重复工作创建;一次性任务用现有工具。"
            "脚本从 $env:XIAOA_TOOL_ARGS 读取 JSON 参数,向标准输出返回结果。创建时会先语法检查和自测;"
            "自测环境 $env:XIAOA_TOOL_TEST=1,有副作用时必须只做无害验证。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "parameters": {"type": "object", "description": "OpenAI 工具参数 JSON Schema"},
                "script": {"type": "string", "description": "完整 PowerShell 脚本"},
                "test_args": {"type": "object", "description": "发布前自测参数"},
                "side_effect": {"type": "boolean"},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 300},
            },
            "required": ["name", "description", "parameters", "script", "test_args"],
        },
        side_effect=True,
    )
    async def tool_create(
        name: str, description: str, parameters: dict, script: str,
        test_args: dict, side_effect: bool = True, timeout: int = 120,
    ) -> str:
        result = await asyncio.to_thread(
            manager.create, name=name, description=description, parameters=parameters,
            script=script, test_args=test_args, side_effect=side_effect, timeout=timeout,
        )
        return json.dumps(result, ensure_ascii=False)

    @registry.tool(
        name="tool_list",
        description="列出小A自己创建的持久化工具及其版本、启用和加载状态。",
        parameters={"type": "object", "properties": {}},
    )
    async def tool_list() -> str:
        return json.dumps(await asyncio.to_thread(manager.list_tools), ensure_ascii=False)

    @registry.tool(
        name="tool_reload",
        description="重新扫描并加载 XiaoA-data/tools 中的用户工具。",
        parameters={"type": "object", "properties": {}},
        side_effect=True,
        danger=True,
    )
    async def tool_reload() -> str:
        return json.dumps(await asyncio.to_thread(manager.reload), ensure_ascii=False)

    @registry.tool(
        name="tool_enable",
        description="启用或停用一个用户工具,不删除历史版本。",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}, "enabled": {"type": "boolean"}},
            "required": ["name", "enabled"],
        },
        side_effect=True,
        danger=True,
    )
    async def tool_enable(name: str, enabled: bool) -> str:
        ok = await asyncio.to_thread(manager.disable, name, enabled)
        return "已更新。" if ok else "没有找到该用户工具。"

    @registry.tool(
        name="tool_rollback",
        description="把一个用户工具回滚到上一历史版本。",
        parameters={
            "type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"],
        },
        side_effect=True,
    )
    async def tool_rollback(name: str) -> str:
        return json.dumps(await asyncio.to_thread(manager.rollback, name), ensure_ascii=False)

    manager.reload()
    return manager
