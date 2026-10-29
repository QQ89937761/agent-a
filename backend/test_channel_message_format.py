from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.modules.chat.message_format import format_channel_text as render


class ChannelMessageFormatTests(unittest.TestCase):
    def test_screenshot_foundation_values_and_headings(self):
        text = r"""### 一、正式施工图阶段（首轮阅读数据）
针对**1#厂房钢结构主体**的基础说明：
* **基础底相对标高**：一般基础底标高记录为 **-3.000 m**（相对室内地坪 $\pm0.000$）。
* **基础深度调整说明**：实际埋深约为 $3.0\text{ m}\sim 4.0\text{ m}$。
* **持力层与承载力**：地基承载力特征值 $f_{ak}$：**$400\text{ kPa}$**。
* **承台厚度**：$h = 900\text{ mm}$（即承台底约为 -1.300 m）。"""
        output = render(text)
        self.assertTrue(output.startswith("一、正式施工图阶段（首轮阅读数据）"))
        for expected in ("1#厂房钢结构主体", "-3.000 m", "±0.000", "3.0 m～ 4.0 m",
                         "fₐₖ：400 kPa", "h = 900 mm", "-1.300 m"):
            self.assertIn(expected, output)
        for marker in ("###", "**", "$", r"\text", "{"):
            self.assertNotIn(marker, output)

    def test_math_groups_units_scripts_and_double_escaped_commands(self):
        cases = {
            r"**$67\%$**": "67%", r"$67\\%$": "67%",
            r"$(546294.03,\, 2393385.00)$": "(546294.03,  2393385.00)",
            r"$23,971.34\text{ m}^2$": "23,971.34 m²",
            r"\(H \approx 0.75 \times \sqrt{400}\)": "H ≈ 0.75 × √(400)",
            r"\[\frac{a+b}{c}\]": "(a+b)/(c)",
            r"$$\frac{1}{\frac{2}{3}}$$": "(1)/((2)/(3))",
            r"$x_i^2 + x_{ABC}$": "xᵢ² + x_(ABC)",
            r"${x+y}^2$": "(x+y)²", r"$> 4\\text{ m}$": "> 4 m",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(expected, render(source))

    def test_table_becomes_labelled_rows_including_code_and_line_breaks(self):
        source = "| **能量 $(E)$** | 深度 $(H)$ | 备注 |\n|---|:---:|---|\n| 1000 kN·m | 5～6 m | `a|b` |\n| 2000 kN·m | 6～7 m | 第一遍<br>第二遍 |"
        self.assertEqual("能量 (E)：1000 kN·m\n深度 (H)：5～6 m\n备注：a|b\n\n"
                         "能量 (E)：2000 kN·m\n深度 (H)：6～7 m\n备注：第一遍\n第二遍", render(source))

    def test_code_paths_links_currency_and_ordinary_text_are_preserved(self):
        for source in ("价格 $5 和 $10，合计 $15", "Cost $5 and $10", "$PATH and $HOME",
                       r"转义 \$5 和 \$10", r"D:\text\a__b__\file.md", "https://example.test/a__b__?cost=$5",
                       "收到，正在处理。", "", "用户文本 \x00XIAOACODE0\x00"):
            with self.subTest(source=source):
                self.assertEqual(source, render(source))
        self.assertEqual(r"$x^2$ **literal**", render(r"`$x^2$ **literal**`"))
        self.assertEqual('# heading\n**literal** $x^2$\n', render('```text\n# heading\n**literal** $x^2$\n```'))
        self.assertEqual('标题\n· 重点\n引用', render('## 标题\n* **重点**\n> 引用'))

    def test_unsupported_unfinished_or_overly_nested_math_keeps_source(self):
        for source in (r"$\unknown{x}$", r"$$\begin{matrix}a&b\\c&d\end{matrix}$$", "$x^{",
                       "$" + "x" * 8193 + "$", "$" + "{" * 40 + "x" + "}" * 40 + "$"):
            with self.subTest(length=len(source)):
                self.assertEqual(source, render(source))


class ChannelMessageDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_weixin_formats_actual_payload_without_changing_routing(self):
        from backend.modules.chat import weixin_clawbot as weixin
        with patch.object(weixin, "_post", new=AsyncMock(return_value={})) as post:
            await weixin.send_message("user-a", r"### 厚度 **$h=900\text{ mm}$**", "ctx-a", "bot-a")
        args, kwargs = post.await_args
        self.assertEqual("bot-a", kwargs["account_id"])
        message = args[1]["msg"]
        self.assertEqual("user-a", message["to_user_id"])
        self.assertEqual("ctx-a", message["context_token"])
        self.assertEqual("厚度 h=900 mm", message["item_list"][0]["text_item"]["text"])

    async def test_feishu_formats_actual_payload_for_chat_and_reminder(self):
        from backend.modules.chat import feishu_module as feishu
        # Stub only the external SDK boundary; exercise the real send_message.
        api = MagicMock()
        request = api.CreateMessageRequest.builder.return_value
        request.receive_id_type.return_value = request
        request.request_body.return_value = request
        body = api.CreateMessageRequestBody.builder.return_value
        for name in ("receive_id", "msg_type", "content"):
            getattr(body, name).return_value = body
        client = MagicMock()
        client.im.v1.message.create.return_value.success.return_value = True
        with patch.dict("sys.modules", {"lark_oapi": MagicMock(), "lark_oapi.api.im.v1": api}), \
             patch.object(feishu, "_get_client", return_value=client):
            for id_type in ("chat_id", "open_id"):
                await feishu.send_message("target-a", r"### 基础 **$\pm0.000$**", receive_id_type=id_type)
                request.receive_id_type.assert_called_with(id_type)
                body.receive_id.assert_called_with("target-a")
                body.msg_type.assert_called_with("text")
                self.assertEqual({"text": "基础 ±0.000"}, json.loads(body.content.call_args.args[0]))
        self.assertEqual(2, client.im.v1.message.create.call_count)


if __name__ == "__main__":
    unittest.main()
