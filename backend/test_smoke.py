"""冒烟测试:不连真网关,用假 LLM 验证 中枢→工具→观察→回复 的闭环 + 记忆降级。

跑法(在 desktop-agent 目录下):
    python -m backend.test_smoke
"""
import asyncio
import io
import tempfile
import unittest
import os
import shutil
import subprocess
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path

from backend.core.agent import Agent
from backend.core.config import AgentConfig
from backend.core.tools import ToolRegistry
from backend.modules.office import fs_module
from backend import run_tests


class FakeLLM:
    """脚本化的假 LLM:第一轮要求调用 fs_write_text,第二轮给出总结。"""

    def __init__(self):
        self.turn = 0

    async def chat(self, messages, tools=None, temperature=0.3, model=None, **kwargs):
        self.turn += 1
        if self.turn == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "fs_write_text",
                            "arguments": '{"path": "memo.txt", "content": "今天的待办:写方案"}',
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": "已经帮你在工作目录创建 memo.txt 并写入待办。"}

    async def chat_stream_tools(self, messages, tools=None, temperature=0.3, model=None, **kwargs):
        msg = await self.chat(messages, tools=tools, temperature=temperature, model=model)
        if msg.get("content"):
            yield "delta", msg["content"]
        yield "done", msg


async def _run_smoke():
    temp_dir = tempfile.TemporaryDirectory(prefix="xiaoa-smoke-")
    tmp = Path(temp_dir.name)
    fs_module.set_workspace_root(str(tmp))
    reg = ToolRegistry()
    fs_module.register_tools(reg)
    print("注册的工具:", reg.names())

    confirmations = []

    async def confirm_fn(name, args):
        confirmations.append((name, args))
        return True  # 自动允许

    agent = Agent(FakeLLM(), reg, AgentConfig(confirm_before_action=True), confirm_fn=confirm_fn)

    events = []
    async for ev in agent.run("在工作目录新建 memo.txt 写上今天的待办"):
        events.append(ev.type)
        print(f"  event: {ev.type}  {ev.data}")

    # 断言
    assert "tool_call" in events, "应有工具调用"
    assert "confirm_needed" in events, "覆盖写文件前应请求用户确认"
    assert confirmations == [("fs_write_text", {
        "path": "memo.txt", "content": "今天的待办:写方案",
    })], "确认回调应收到准确的工具名和参数"
    assert (tmp / "memo.txt").read_text(encoding="utf-8") == "今天的待办:写方案", "文件内容应写入"
    assert events[-1] == "done", "最后应为 done"
    print("\nSMOKE PASS: agent loop, tool dispatch, policy, and file write.")
    temp_dir.cleanup()



class SmokeTests(unittest.TestCase):
    def test_agent_loop_file_write(self):
        asyncio.run(_run_smoke())


class TestRunnerTests(unittest.TestCase):
    def test_syntax_errors_are_not_silently_skipped(self):
        with patch.object(run_tests.importlib, "import_module", side_effect=SyntaxError("broken test")):
            with self.assertRaises(SyntaxError):
                run_tests.build_suite()

    def test_internal_import_typos_are_not_silently_skipped(self):
        error = ModuleNotFoundError("No module named backend.typo", name="backend.typo")
        with patch.object(run_tests.importlib, "import_module", side_effect=error):
            with self.assertRaises(ModuleNotFoundError):
                run_tests.build_suite()

    def test_module_import_skip_fails_in_strict_mode(self):
        skipped = ["backend.test_example: ModuleNotFoundError: No module named 'PIL'"]
        with patch.object(
            run_tests,
            "build_suite",
            return_value=(unittest.TestSuite(), skipped),
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = run_tests.main(["--strict"])

        self.assertEqual(exit_code, 1)
        self.assertIn("PIL", output.getvalue())


class DeploymentScriptTests(unittest.TestCase):
    @staticmethod
    def _script(root: Path, name: str) -> Path:
        script = root / "deploy" / name
        if not script.is_file():
            raise AssertionError(f"missing deployment script: {script}")
        return script

    @staticmethod
    def _bash():
        return None if os.name == "nt" else shutil.which("bash")

    def test_preflight_rejects_invalid_or_reserved_port(self):
        root = Path(__file__).resolve().parents[1]
        script = self._script(root, "preflight.sh")
        bash = self._bash()
        if not bash:
            self.assertIn("端口必须是 1-65535", script.read_text(encoding="utf-8"))
            self.assertIn("80", script.read_text(encoding="utf-8"))
            return
        for value in ("bad", "0", "65536", "80", "443"):
            result = subprocess.run([bash, str(script), value], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, value)

    def test_preflight_reports_port_check(self):
        root = Path(__file__).resolve().parents[1]
        script = self._script(root, "preflight.sh")
        bash = self._bash()
        if not bash:
            self.assertIn("端口 $PORT:", script.read_text(encoding="utf-8"))
            return
        result = subprocess.run([bash, str(script), "21115"], capture_output=True, text=True)
        self.assertIn("端口 21115:", result.stdout)

    def test_postcheck_runs_service_checks_without_private_host_baseline(self):
        root = Path(__file__).resolve().parents[1]
        script = self._script(root, "postcheck.sh").read_text(encoding="utf-8")
        self.assertIn('diagnose_endpoint.sh', script)
        self.assertIn('"https://127.0.0.1:$PORT/health"', script)
        self.assertNotIn("ZHXA", script)
        self.assertNotIn("BASELINE_DIR", script)

    def test_firewall_diagnosis_has_three_independent_layers(self):
        root = Path(__file__).resolve().parents[1]
        script = self._script(root, "diagnose_endpoint.sh").read_text(encoding="utf-8")
        for marker in ("服务层:", "监听层:", "防火墙层:", "云厂商安全组"):
            self.assertIn(marker, script)
        self.assertIn("0.0.0.0", script)
        self.assertIn("ufw", script)
        self.assertIn("firewall-cmd", script)
        self.assertIn("iptables", script)

    def test_deploy_hub_supports_zero_arguments_and_private_credential_file(self):
        root = Path(__file__).resolve().parents[1]
        script_path = self._script(root, "deploy_hub.sh")
        script = script_path.read_text(encoding="utf-8")
        self.assertIn('PUBLIC_IP="${1:-}"', script)
        self.assertIn('PORT="${2:-}"', script)
        self.assertIn("detect_public_ip", script)
        self.assertIn('while [ "$PORT" -le 65535 ] && port_in_use', script)
        self.assertIn("检测到已有部署，继续使用端口", script)
        self.assertIn('CREDENTIAL_FILE="$APP_DIR/data/connection-string.txt"', script)
        self.assertIn('chmod 600 "$CREDENTIAL_FILE"', script)
        self.assertIn("configure_firewall", script)
        self.assertIn("ufw", script)
        self.assertIn("firewall-cmd", script)
        self.assertIn("iptables", script)
        self.assertNotIn("网页登录密码（请单独保存）", script)

        bash = self._bash()
        if bash:
            result = subprocess.run([bash, "-n", str(script_path)], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
