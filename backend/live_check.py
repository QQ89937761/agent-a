"""真实端到端检查:连真网关,跑完整中枢闭环。

**注意:这不是单元测试,会连真网关、真花钱。** 故意不叫 test_ 前缀,
避免被 unittest/pytest 自动采集误跑。需要时手动运行:
    python -m backend.live_check          # 完整 Agent 闭环
(在项目根目录、已有 config.toml 的前提下)
"""
import asyncio
import tempfile
from pathlib import Path

from backend.core.agent import Agent
from backend.core.config import apply_env_overrides, load_config
from backend.core.llm import LLMClient
from backend.core.tools import ToolRegistry
from backend.modules.office import fs_module
from backend.modules.memory.memory_module import MemoryModule


async def main():
    cfg = apply_env_overrides(load_config())
    print(f"网关: {cfg.llm.base_url}  模型: {cfg.llm.chat_model}")

    temp_dir = tempfile.TemporaryDirectory(prefix="xiaoa-live-")
    tmp = Path(temp_dir.name)
    fs_module.set_workspace_root(str(tmp))
    reg = ToolRegistry()
    fs_module.register_tools(reg)

    llm = LLMClient(cfg.llm)
    mem = MemoryModule(cfg.memory, cfg.memory_data_dir, llm)

    async def confirm_fn(name, args):
        print(f"  [确认] {name} {args} -> 自动允许")
        return True

    agent = Agent(llm, reg, cfg.agent, confirm_fn=confirm_fn, memory=mem)

    print("\n=== 任务: 明确指令,新建 todo.txt ===")
    task = ("用 fs_write_text 工具在工作目录创建文件 todo.txt,"
            "内容就是这三行:1. 写项目方案 2. 回复邮件 3. 测试 Agent。直接执行,不要反问。")
    async for ev in agent.run(task):
        print(f"  {ev.type}: {str(ev.data)[:200]}")

    f = tmp / "todo.txt"
    print(f"\n文件是否创建: {f.exists()}")
    if f.exists():
        print("内容:\n" + f.read_text(encoding="utf-8"))

    await llm.aclose()
    temp_dir.cleanup()
    print("\n✅ 真实闭环跑通" if f.exists() else "\n⚠ 文件未创建,看上面事件流排查")


if __name__ == "__main__":
    asyncio.run(main())
