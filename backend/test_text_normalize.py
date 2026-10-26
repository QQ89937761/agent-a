"""中文半角标点规范化的回归(小A 回复与生成文档都会用到)。"""
from __future__ import annotations

import unittest

from backend.core.text_normalize import normalize_cn_punctuation


class NormalizeCnPunctuationTests(unittest.TestCase):
    def test_chinese_context_halfwidth_becomes_fullwidth(self):
        text = "确认了:正文里有 81 个半角逗号,顺手把范围号的 ~ 也换掉(共 15 对括号)。"
        got = normalize_cn_punctuation(text)
        self.assertIn("确认了：", got)
        self.assertIn("半角逗号，", got)
        self.assertIn("（共", got)
        self.assertIn("括号）。", got)
        self.assertIn("～", got)
        self.assertNotIn(",", got)

    def test_does_not_touch_pure_english_and_numbers(self):
        text = "price 1,000; see http://x.com/a:b; call f(a, b); at 12:30"
        self.assertEqual(text, normalize_cn_punctuation(text))

    def test_code_identifier_colon_untouched(self):
        """小A 回复里会提到 w:tbl 这种代码标识符,里面的冒号不能动。"""
        text = "说明正则误匹配了 w:tbl 这类标签，把 XML 内部也改了。"
        self.assertEqual(text, normalize_cn_punctuation(text))

    def test_unit_suffix_comma_in_chinese_sentence(self):
        """逗号紧邻的是单位字母(15.5m),但整句是中文 → 要换。"""
        text = "主臂25.3m,8m幅度额定起重量5.0t,钢筋捆按1.5~2.5t计。"
        got = normalize_cn_punctuation(text)
        self.assertNotIn(",", got)
        self.assertIn("25.3m，8m", got)
        self.assertIn("5.0t，钢筋捆", got)
        self.assertIn("1.5～2.5t", got)

    def test_idempotent(self):
        text = "确认了：正文里有 81 个半角逗号，顺手换掉（共 15 对括号）。"
        self.assertEqual(text, normalize_cn_punctuation(text))

    def test_number_range_gets_fullwidth_tilde(self):
        self.assertEqual("养护 7～10 天", normalize_cn_punctuation("养护 7~10 天"))

    def test_english_tilde_untouched(self):
        self.assertEqual("a~b", normalize_cn_punctuation("a~b"))

    def test_empty(self):
        self.assertEqual("", normalize_cn_punctuation(""))


if __name__ == "__main__":
    unittest.main()
