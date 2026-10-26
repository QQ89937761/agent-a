"""Reply drafting must keep memory lookup private and group-safe."""
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from backend.api import server


class _Memory:
    def __init__(self):
        self.calls = []

    async def recall(self, query, limit=5):
        self.calls.append((query, limit))
        return "- 火箭基地区项目"


class _LLM:
    def __init__(self):
        self.prompt = ""
        self.model = None

    async def chat(self, messages, temperature=0.4, model=None):
        self.prompt = messages[0]["content"]
        self.model = model
        return {"content": "正在火箭基地区项目上班。"}


class ReplyDraftTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_redraft_explicitly_searches_local_memory(self):
        memory = _Memory()
        llm = _LLM()
        guidance = "你说我在项目上班，哪个项目你查下记忆"
        info = {
            "source": "feishu", "sender": "朱镇平", "incoming": "有没有空",
            "reply_target": "朱镇平", "is_group": False,
        }
        with patch.object(server, "MEMORY", memory), patch.object(server, "LLM", llm):
            draft = await server._compose_reply_draft(info, guidance)

        self.assertEqual("正在火箭基地区项目上班。", draft)
        self.assertEqual([(guidance, 8)], memory.calls)
        self.assertIn("火箭基地区项目", llm.prompt)
        self.assertIn("内部操作说明", llm.prompt)

    async def test_group_redraft_never_reads_personal_memory(self):
        memory = _Memory()
        llm = _LLM()
        info = {
            "source": "feishu", "sender": "同事", "incoming": "项目进度？",
            "reply_target": "工作群", "is_group": True,
        }
        with patch.object(server, "MEMORY", memory), patch.object(server, "LLM", llm):
            await server._compose_reply_draft(info, "查下记忆")

        self.assertEqual([], memory.calls)
        self.assertIn("不读取全局长期记忆", llm.prompt)

    def test_memory_lookup_requires_explicit_wording(self):
        self.assertTrue(server._memory_requested("哪个项目你查下记忆"))
        self.assertFalse(server._memory_requested("语气正式一点"))

    async def test_image_reply_uses_vision_model(self):
        llm = _LLM()
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "message.png"
            image_path.write_bytes(b"test-image")
            info = {
                "source": "feishu", "sender": "Xiao A", "incoming": "[图片]",
                "reply_target": "Xiao A", "is_group": False,
                "image_path": str(image_path),
            }
            with patch.object(server, "LLM", llm):
                await server._compose_reply_draft(info)

        self.assertEqual(server.CFG.llm.chat_model, llm.model)
        self.assertIsInstance(llm.prompt, list)
        self.assertEqual("image_url", llm.prompt[1]["type"])


if __name__ == "__main__":
    unittest.main()
