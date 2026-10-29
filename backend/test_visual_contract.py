"""Global visual response contract, MIME and diagnostics (no live desktop input)."""
import io
import json
import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from PIL import Image
from backend.core.llm import LLMClient
from backend.core.config import AgentConfig, LLMConfig
from backend.core.agent import Agent, SYSTEM_PROMPT
from backend.core.tools import Tool, ToolRegistry
from backend.test_conversation_approval import OneCallLLM
from backend.modules.computer.computer_module import locate_in_image, parse_coords


class VisualContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_desktop_guidance_does_not_treat_windows_taskbar_as_target_app(self):
        self.assertIn("Shell_TrayWnd是Windows任务栏，不是目标应用窗口", SYSTEM_PROMPT)
        self.assertIn("结合返回的全窗口清单和桌面图定位托盘/主窗口", SYSTEM_PROMPT)

    async def test_pixels_and_explicit_normalized_units_without_guessing_or_clamping(self):
        for answer, expected, invalid in (
            ('{"x":690,"y":850}', None, True),
            ('{"x":690,"y":850,"coordinate_space":"normalized_1000"}', (660, 664), False),
            ('{"x":0,"y":0}', (0, 0), False),
            ('{"x":956,"y":781}', (956, 781), False),
            ('{"x":957,"y":781}', None, True),
            ('{"x":-2,"y":20}', None, True),
            ('{"x":1001,"y":500,"coordinate_space":"normalized_1000"}', None, True),
            ('{"x":10,"y":20,"coordinate_space":"desktop"}', None, True),
            ('{"x":-1,"y":-1}', None, False),
            ('{"found":false}', None, True),
            ('null', None, True),
            ('{"x":true,"y":20}', None, True),
            ('{"x":NaN,"y":20}', None, True),
            ('{"x":Infinity,"y":20}', None, True),
            ('{"x":10.9,"y":20}', None, True),
            ('{"x":"10","y":20}', None, True),
            ('x=10.9,y=20', None, True),
        ):
            with self.subTest(answer=answer):
                llm = SimpleNamespace(vision=AsyncMock(return_value=answer))
                diagnostic = {}
                result = await locate_in_image(llm, b'image', 'target', 957, 782, diagnostics=diagnostic)
                self.assertEqual(expected, result)
                self.assertEqual(invalid, bool(diagnostic))
                llm.vision.assert_awaited_once()
                self.assertIn('y范围0到781', llm.vision.call_args.args[0])
        self.assertIsNone(parse_coords('null'))

    async def test_agent_exposes_failure_crop_and_mapping_for_next_model_turn(self):
        class InspectLLM(OneCallLLM):
            async def chat_stream_tools(self, messages, *args, **kwargs):
                self.seen = json.loads(json.dumps(messages))
                async for event in super().chat_stream_tools(messages, *args, **kwargs):
                    yield event

        result = {"ok": False, "status": "visual_coordinate_invalid", "sent": False,
                  "path": "failure.jpg", "_observation_image": "c2FtZQ==",
                  "coordinate_mapping": {"origin": {"x": 100, "y": 80},
                                         "observation_size": {"width": 957, "height": 782}}}
        async def click():
            return dict(result)
        registry = ToolRegistry()
        registry.register(Tool("click_element", "click", {"type": "object"}, click, side_effect=True))
        llm = InspectLLM("click_element")
        with tempfile.TemporaryDirectory() as directory:
            agent = Agent(llm, registry, AgentConfig(), result_dir=Path(directory))
            with self.assertLogs("backend.core.agent.timing", level="INFO") as timing:
                events = [event async for event in agent.run("点击目标控件", approval_mode="full")]
        self.assertIn("name=click_element ok=False elapsed_ms=", str(timing.output))
        self.assertNotIn("点击目标控件", str(timing.output))
        self.assertNotIn("failure.jpg", str(timing.output))
        metadata = json.loads(next(m["content"] for m in llm.seen if m.get("role") == "tool"))
        self.assertEqual(result["coordinate_mapping"], metadata["coordinate_mapping"])
        self.assertNotIn("_observation_image", metadata)
        self.assertTrue(any("data:image/jpeg;base64,c2FtZQ==" in str(m) for m in llm.seen))
        self.assertFalse(next(event.data["ok"] for event in events if event.type == "tool_result"))
        self.assertTrue(any(event.type == "screenshot" for event in events))

    async def test_all_vision_calls_use_actual_jpeg_or_png_mime_and_preserve_override(self):
        client = LLMClient(LLMConfig())
        client.chat = AsyncMock(return_value={'content':'ok'})
        for image_format, expected in (('JPEG', 'jpeg'), ('PNG', 'png')):
            buffer = io.BytesIO()
            Image.new('RGB', (12, 12), 'white').save(buffer, format=image_format)
            with self.assertLogs('backend.core.llm', level='INFO') as logs:
                await client.vision('private content not for logs', buffer.getvalue())
            payload = client.chat.call_args.args[0][0]['content'][1]['image_url']['url']
            self.assertTrue(payload.startswith(f'data:image/{expected};base64,'))
            self.assertNotIn('private content', str(logs.output))
            self.assertIn('elapsed_ms=', str(logs.output))
        await client.vision('text', b'webp-data', image_format='webp')
        self.assertTrue(client.chat.call_args.args[0][0]['content'][1]['image_url']['url'].startswith('data:image/webp;'))


if __name__ == '__main__':
    unittest.main()
