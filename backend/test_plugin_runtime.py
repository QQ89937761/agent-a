"""Isolated executable plugin runtime tests."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import shutil
import subprocess
import sysconfig
from pathlib import Path
from unittest.mock import patch

from backend.core.tools import ToolRegistry
from backend.modules.computer.plugin_runtime_module import PluginRuntimeManager, _minimal_env


def _plugin(directory: Path, version: int, *, bad_dependencies=None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": "demo_plugin",
        "description": "隔离插件测试",
        "runtime": "python",
        "entry": "main.py",
        "parameters": {
            "type": "object",
            "properties": {
                "value": {"type": "integer"},
                "mode": {"type": "string"},
            },
            "required": ["value"],
            "additionalProperties": False,
        },
        "dependencies": [] if bad_dependencies is None else bad_dependencies,
        "test_args": {"value": 1},
        "side_effect": False,
        "timeout": 5,
    }
    (directory / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "main.py").write_text(
        "import json, os, sys\n"
        f"VERSION={version}\n"
        "args=json.load(sys.stdin)\n"
        "if args.get('mode') == 'big':\n"
        " print(json.dumps('x' * 1048576))\n"
        "else:\n"
        " print(json.dumps({'version': VERSION, 'value': args['value'], "
        "'secret': os.getenv('XIAOA_SECRET_DO_NOT_SHARE'), 'home': os.getenv('HOME')}))\n",
        encoding="utf-8",
    )
    return directory


def _node_plugin(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.json").write_text(json.dumps({
        "name": "node_plugin", "description": "Node 隔离插件测试",
        "runtime": "node", "entry": "main.js",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        "dependencies": {}, "test_args": {"text": "test"}, "side_effect": False,
    }), encoding="utf-8")
    (directory / "main.js").write_text(
        "let raw='';process.stdin.setEncoding('utf8');process.stdin.on('data',c=>raw+=c);"
        "process.stdin.on('end',()=>process.stdout.write(JSON.stringify({echo:JSON.parse(raw).text,secret:process.env.XIAOA_SECRET_DO_NOT_SHARE||null})));",
        encoding="utf-8",
    )
    return directory


class PluginRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_build_finds_stdlib_venv_when_python_is_a_virtual_environment(self):
        script = (Path(__file__).resolve().parents[1] / "deploy/prepare_plugin_runtimes.ps1").read_text(encoding="utf-8-sig")
        command = next(line for line in script.splitlines() if "$hostVenv = & python.exe" in line)
        probe = command.split('-c "', 1)[1].rsplit('"', 1)[0]
        with tempfile.TemporaryDirectory(prefix="xiaoa-build-venv-") as tmp:
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", tmp], check=True, capture_output=True, timeout=30)
            python = Path(tmp) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            result = subprocess.run([str(python), "-I", "-c", probe], check=True, capture_output=True, text=True, timeout=10)
        self.assertEqual(Path(result.stdout.strip()).resolve(), (Path(sysconfig.get_path("stdlib")) / "venv").resolve())

    def test_install_environment_uses_network_config_without_exposing_it_to_plugin(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-plugin-env-") as tmp:
            root = Path(tmp)
            home = root / "user"
            appdata = root / "appdata"
            home.mkdir()
            (home / ".npmrc").write_text(
                "registry=https://mirror.example/npm\n"
                "//mirror.example/npm/:_authToken=SECRET-TOKEN\n",
                encoding="utf-8",
            )
            pip_ini = appdata / "pip" / "pip.ini"
            pip_ini.parent.mkdir(parents=True)
            pip_ini.write_text("[global]\nindex-url=https://mirror.example/pypi\n", encoding="utf-8")
            values = {
                "USERPROFILE": str(home), "APPDATA": str(appdata),
                "HTTPS_PROXY": "http://proxy.example:8080", "NO_PROXY": "127.0.0.1",
                "NPM_CONFIG_REGISTRY": "https://mirror.example/npm",
                "PIP_INDEX_URL": "https://mirror.example/pypi/simple",
                "NPM_CONFIG_USERCONFIG": "", "PIP_CONFIG_FILE": "",
                "AGENT_LLM_API_KEY": "must-not-leak",
            }
            version_dir = root / "plugin"
            version_dir.mkdir()
            manager = PluginRuntimeManager(
                root / "plugins", ToolRegistry(), node_executable="node", npm_executable="npm",
            )
            with patch.dict(os.environ, values, clear=False), patch.object(
                manager, "_run_install",
            ) as run_install:
                manager._prepare_environment(version_dir, {
                    "runtime": "node", "name": "demo", "dependencies": {"demo": "1.0.0"},
                })
                install_env = run_install.call_args.args[2]
                runtime_env = _minimal_env(root / "plugin")
                # 临时目录退出即删,净化后的 .npmrc 内容必须在块内读出来
                safe_npmrc_path = install_env["NPM_CONFIG_USERCONFIG"]
                safe_npmrc_text = Path(safe_npmrc_path).read_text(encoding="utf-8")
                expected_safe_npmrc = str(version_dir / ".npmrc.registry")
                user_npmrc_path = str(home / ".npmrc")

        self.assertEqual(values["HTTPS_PROXY"], install_env["HTTPS_PROXY"])
        self.assertEqual(values["NPM_CONFIG_REGISTRY"], install_env["NPM_CONFIG_REGISTRY"])
        self.assertEqual(values["PIP_INDEX_URL"], install_env["PIP_INDEX_URL"])
        # 不再把用户 .npmrc 整个交出去,而是只提取 registry 行写成净化副本
        self.assertNotEqual(user_npmrc_path, safe_npmrc_path)
        self.assertEqual(expected_safe_npmrc, safe_npmrc_path)
        self.assertIn("registry=https://mirror.example/npm", safe_npmrc_text)
        self.assertNotIn("_authToken", safe_npmrc_text)
        self.assertNotIn("SECRET-TOKEN", safe_npmrc_text)
        self.assertEqual(str(pip_ini), install_env["PIP_CONFIG_FILE"])
        self.assertNotIn("AGENT_LLM_API_KEY", install_env)
        # 镜像源/配置文件只给安装期,插件运行期拿不到
        for key in ("NPM_CONFIG_REGISTRY", "PIP_INDEX_URL",
                    "NPM_CONFIG_USERCONFIG", "PIP_CONFIG_FILE"):
            self.assertNotIn(key, runtime_env)
        # 代理是运行期也要的:插件联网必须能走用户的网络路由,否则在需要代理的
        # 环境下必然失败。代理不是凭据,与 AGENT_* 密钥性质不同。
        self.assertEqual(values["HTTPS_PROXY"], runtime_env["HTTPS_PROXY"])
        self.assertEqual(values["NO_PROXY"], runtime_env["NO_PROXY"])
        # 但密钥在任何阶段都不许出现
        self.assertNotIn("AGENT_LLM_API_KEY", runtime_env)

    def test_runtime_environment_overrides_are_used(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-plugin-") as tmp:
            with patch.dict(os.environ, {
                "XIAOA_PLUGIN_PYTHON": r"C:\runtime\python.exe",
                "XIAOA_PLUGIN_NODE": r"C:\runtime\node.exe",
                "XIAOA_PLUGIN_NPM": r"C:\runtime\npm.cmd",
            }):
                manager = PluginRuntimeManager(Path(tmp) / "plugins", ToolRegistry())
            self.assertEqual(r"C:\runtime\python.exe", manager.python_executable)
            self.assertEqual(r"C:\runtime\node.exe", manager.node_executable)
            self.assertEqual(r"C:\runtime\npm.cmd", manager.npm_executable)

    def test_failed_bundled_provisioning_rejects_plugins_without_system_runtime_fallback(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-plugin-") as tmp, patch.dict(os.environ, {
            "XIAOA_PLUGIN_RUNTIME_ERROR": "插件运行环境初始化失败：拒绝访问",
        }):
            manager = PluginRuntimeManager(Path(tmp) / "plugins", ToolRegistry())
            with patch.object(manager, "_run_install") as install, patch("subprocess.Popen") as execute:
                for runtime in ("python", "node"):
                    with self.subTest(runtime=runtime):
                        with self.assertRaisesRegex(RuntimeError, "初始化失败"):
                            manager._prepare_environment(Path(tmp), {"runtime": runtime})
                        with self.assertRaisesRegex(RuntimeError, "初始化失败"):
                            manager._execute(Path(tmp), {"runtime": runtime}, {})
                install.assert_not_called()
                execute.assert_not_called()

    def test_python_dependencies_install_without_pip_in_plugin_venv(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-plugin-install-") as tmp:
            manager = PluginRuntimeManager(
                Path(tmp) / "plugins", ToolRegistry(), python_executable="bundled-python.exe",
            )
            version_dir = Path(tmp) / "version"
            version_dir.mkdir()
            with patch.object(manager, "_run_install") as run_install:
                manager._prepare_environment(version_dir, {
                    "runtime": "python", "name": "demo", "dependencies": ["requests==2.32.3"],
                })
            create_venv = run_install.call_args_list[0].args[0]
            install_dependencies = run_install.call_args_list[1].args[0]
            self.assertIn("--without-pip", create_venv)
            self.assertEqual("bundled-python.exe", install_dependencies[0])
            self.assertIn("--target", install_dependencies)
            site_packages = version_dir / ".venv" / (
                "Lib/site-packages" if os.name == "nt"
                else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
            )
            self.assertIn(str(site_packages), install_dependencies)
            self.assertIn("requests==2.32.3", install_dependencies)

    async def test_install_run_secret_filter_disable_update_and_rollback(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-plugin-") as tmp:
            base = Path(tmp)
            registry = ToolRegistry()
            manager = PluginRuntimeManager(base / "plugins", registry)
            with patch.dict(os.environ, {"XIAOA_SECRET_DO_NOT_SHARE": "must-not-leak"}):
                first = manager.install_from_directory(
                    _plugin(base / "source-one", 1),
                    {"source_url": "https://github.com/acme/demo", "repository": "acme/demo"},
                )
                self.assertEqual(1, first["version"])
                self.assertIn("demo_plugin", registry.names())
                description = registry.get("demo_plugin").description
                self.assertIn("风险审批模式", description)
                self.assertIn("执行前会请求确认", description)
                result = json.loads(await registry.call("demo_plugin", {"value": 7}))
                self.assertEqual({"version": 1, "value": 7}, {
                    "version": result["version"], "value": result["value"],
                })
                self.assertIsNone(result["secret"])
                self.assertIn("plugins", result["home"])

                self.assertTrue(manager.enable("demo_plugin", False))
                self.assertNotIn("demo_plugin", registry.names())
                self.assertTrue(manager.enable("demo_plugin", True))

                second = manager.install_from_directory(
                    _plugin(base / "source-two", 2),
                    {"source_url": "https://github.com/acme/demo", "repository": "acme/demo"},
                    update_name="demo_plugin",
                )
                self.assertEqual(2, second["version"])
                result = json.loads(await registry.call("demo_plugin", {"value": 8}))
                self.assertEqual(2, result["version"])
                self.assertEqual([1, 2], manager.list_plugins()[0]["versions"])

                self.assertEqual(1, manager.rollback("demo_plugin")["version"])
                result = json.loads(await registry.call("demo_plugin", {"value": 9}))
                self.assertEqual(1, result["version"])

    async def test_isolated_python_plugin_round_trips_utf8_json(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-plugin-utf8-") as tmp:
            base = Path(tmp)
            registry = ToolRegistry()
            manager = PluginRuntimeManager(base / "plugins", registry)
            source = _plugin(base / "source", 1)
            (source / "main.py").write_text(
                "import json,sys\n"
                "args=json.load(sys.stdin)\n"
                "assert sys.flags.utf8_mode == 1\n"
                "print(json.dumps({'mode':args.get('mode')},ensure_ascii=False))\n",
                encoding="utf-8",
            )
            manager.install_from_directory(source, {})
            text = "火箭项目：插件运行成功 🚀"
            result = json.loads(await registry.call("demo_plugin", {"value": 1, "mode": text}))
            self.assertEqual(text, result["mode"])

    async def test_manifest_dependency_and_protocol_limits(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-plugin-") as tmp:
            base = Path(tmp)
            registry = ToolRegistry()
            manager = PluginRuntimeManager(base / "plugins", registry)
            with self.assertRaisesRegex(ValueError, "固定版本"):
                manager.inspect_directory(_plugin(base / "bad", 1, bad_dependencies=["requests>=2"]))

            manager.install_from_directory(_plugin(base / "good", 1), {})
            with self.assertRaisesRegex(RuntimeError, "输出超过限制"):
                await registry.call("demo_plugin", {"value": 1, "mode": "big"})
            with self.assertRaisesRegex(ValueError, "未声明参数"):
                await registry.call("demo_plugin", {"value": 1, "unknown": True})

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    async def test_dependency_free_node_plugin_uses_json_protocol(self):
        with tempfile.TemporaryDirectory(prefix="xiaoa-plugin-") as tmp:
            base = Path(tmp); registry = ToolRegistry()
            manager = PluginRuntimeManager(base / "plugins", registry)
            with patch.dict(os.environ, {"XIAOA_SECRET_DO_NOT_SHARE": "must-not-leak"}):
                manager.install_from_directory(_node_plugin(base / "node-source"), {})
                result = json.loads(await registry.call("node_plugin", {"text": "hello"}))
            self.assertEqual({"echo": "hello", "secret": None}, result)


if __name__ == "__main__":
    unittest.main()
