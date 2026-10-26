"""Verify the runtimes released by Electron by installing and executing both plugin types."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile


def verify_released_runtime(source: Path, runtime: Path) -> int:
    from deploy.verify_release import tree_hashes, verify_files

    files = tree_hashes(source)
    actual = tree_hashes(runtime)
    # Electron creates this local cache marker after copying the shipped runtime.
    # Ignore only the root marker, not other extra files or changed runtime bytes.
    actual.pop(".ready.json", None)
    return verify_files({name: source / name for name in files}, actual, "released plugin runtimes")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app_home", type=Path)
    parser.add_argument("resources", type=Path)
    args = parser.parse_args()
    app_home = args.app_home.resolve()
    resources = args.resources.resolve()
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    os.environ["XIAOA_HOME"] = str(app_home)
    from backend.core.tools import ToolRegistry
    from backend.modules.computer.plugin_runtime_module import PluginRuntimeManager

    source = resources / "plugin-runtime"
    metadata = json.loads((source / "runtime.json").read_text(encoding="utf-8"))
    runtime = app_home / "runtimes/plugin-runtime" / f"{metadata['python']}-{metadata['node']}"
    verify_released_runtime(source, runtime)

    # No network, third-party plugins or system Python/Node fallback.
    with tempfile.TemporaryDirectory(prefix="plugin-smoke-", dir=app_home) as temporary:
        probe = Path(temporary)
        registry = ToolRegistry()
        manager = PluginRuntimeManager(
            probe / "plugins", registry,
            python_executable=str(runtime / "python/python.exe"),
            node_executable=str(runtime / "node/node.exe"),
            npm_executable=str(runtime / "node/npm.cmd"),
        )
        for kind, entry, code, dependencies in (
            ("python", "main.py",
             "import json,sys,ssl,sqlite3,ctypes,venv\n"
             "args=json.load(sys.stdin)\nprint(json.dumps({'echo':args['text']},ensure_ascii=False))\n", []),
            ("node", "main.js",
             "let raw='';process.stdin.setEncoding('utf8');process.stdin.on('data',c=>raw+=c);"
             "process.stdin.on('end',()=>process.stdout.write(JSON.stringify({echo:JSON.parse(raw).text})));", {}),
        ):
            name = f"smoke_{kind}_plugin"
            fixture = probe / kind
            fixture.mkdir()
            (fixture / "plugin.json").write_text(json.dumps({
                "name": name, "description": "Bundled plugin runtime smoke",
                "runtime": kind, "entry": entry, "dependencies": dependencies,
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                "test_args": {"text": "install check"}, "side_effect": False,
            }), encoding="utf-8")
            (fixture / entry).write_text(code, encoding="utf-8")
            installed = manager.install_from_directory(fixture, {})
            if installed["test_output"] != {"echo": "install check"}:
                raise RuntimeError(f"{kind} plugin install check failed: {installed}")
            result = json.loads(asyncio.run(registry.call(name, {"text": "插件运行成功"})))
            if result != {"echo": "插件运行成功"}:
                raise RuntimeError(f"{kind} plugin execution failed: {result}")
            print(f"PASS bundled {kind}: isolated plugin install and execution", flush=True)
    print("PLUGIN_RUNTIME_SMOKE_PASSED", flush=True)


if __name__ == "__main__":
    main()
