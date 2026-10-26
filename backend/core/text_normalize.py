"""中文文本里的半角标点规范化。

模型写中文内容时经常混用英文半角标点(逗号、分号、冒号、括号、波浪号),
生成的文档和小A 的回复里都会出现(实测一份 4 页方案:半角逗号 81 个、
中文逗号 0 个;分号/冒号/括号同样全是半角)。

这里做**保守**替换:只有标点紧邻的上下文确实是中文(或数字范围)时才换全角,
避免误伤数字(1,000)、URL(http://x)、代码(a, b)、时间(12:30)、英文缩写。
替换是幂等的:全角标点不会再被处理。
"""
from __future__ import annotations

import re

_HALF_TO_FULL = {",": "，", ";": "；", ":": "：", "?": "？", "!": "！"}
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ffff]")
# 中文标点也算"中文语境":不然 "（90日历天）,完成" 里那个逗号会因为前面是刚换出来的
# "）" 而漏替换。
_CN_PUNCT = "，。、；：？！“”‘’（）《》【】…—～·"


def _is_cjk(char: str) -> bool:
    return bool(char) and bool(_CJK_RE.match(char))


def _is_cn(char: str) -> bool:
    """中文语境:汉字或中文标点。"""
    return _is_cjk(char) or (bool(char) and char in _CN_PUNCT)


def _is_digit(char: str) -> bool:
    return bool(char) and char.isdigit()


def _is_ascii_letter(char: str) -> bool:
    return bool(char) and char.isascii() and char.isalpha()


def _last_nonspace(out: list[str]) -> str:
    """已输出部分的最后一个非空白字符(看替换后的结果,不看原文)。"""
    for char in reversed(out):
        if not char.isspace():
            return char
    return ""


def _next_nonspace(chars: list[str], index: int) -> str:
    """原文里 index 之后第一个非空白字符。"""
    i = index + 1
    while i < len(chars):
        if not chars[i].isspace():
            return chars[i]
        i += 1
    return ""


def _cn_before_token(out: list[str]) -> bool:
    """往前跳过数字/字母/点组成的"单位串",看是否落在中文语境上。

    处理 "15.5m,8m 幅度" "2200t,模板" 这类:逗号紧邻的是字母,但整句是中文。
    纯英文句子里往前找不到中文,所以不会被误改。
    """
    i = len(out) - 1
    while i >= 0 and out[i].isspace():
        i -= 1
    # 注意:str.isalnum() 对中文也返回 True,必须先排除中文,否则会把中文一起跳过
    while i >= 0 and not _is_cn(out[i]) and (out[i].isalnum() or out[i] == "."):
        i -= 1
    while i >= 0 and out[i].isspace():
        i -= 1
    return i >= 0 and _is_cn(out[i])


def normalize_cn_punctuation(text: str) -> str:
    """把中文语境里的半角标点换成全角;非中文上下文原样保留。"""
    if not text:
        return text
    # Protect technical spans before context-sensitive punctuation conversion.
    protected = set()
    pattern = r"[A-Za-z][A-Za-z0-9+.-]*://[^\s，；。！？<>]+|[A-Za-z]:[\\/][^\s，；。！？<>]+|[A-Za-z_][A-Za-z0-9_.]*\([^\x80-\U0010ffff]*?\)"
    for match in re.finditer(pattern, text):
        protected.update(range(match.start(), match.end()))
    chars = list(text)
    out: list[str] = []
    for index, char in enumerate(chars):
        if index in protected:
            out.append(char)
            continue
        prev = _last_nonspace(out)  # 看替换后的结果:半角 ")" 刚换成 "）" 也算中文语境
        nxt = _next_nonspace(chars, index)
        # 中文语境:紧邻的是中文,或往前跳过单位串(15.5m)后能落在中文上
        in_cn = _is_cn(prev) or _cn_before_token(out)
        if char in _HALF_TO_FULL:
            # 千分位(1,000)和时间(12:30)前后都是数字,不能动
            if _is_digit(prev) and _is_digit(nxt):
                out.append(char)
                continue
            # 代码/英文标识符(w:tbl、a,b):标点夹在两个 ASCII 字母之间,不动
            if _is_ascii_letter(prev) and _is_ascii_letter(nxt):
                out.append(char)
                continue
            if in_cn:
                out.append(_HALF_TO_FULL[char])
                continue
        if char == "(" and in_cn:
            out.append("（")
            continue
        if char == ")" and in_cn:
            out.append("）")
            continue
        if char == "~":
            # 范围号:中文旁边,或数字之间(7~10 天),都该是中文波浪号
            if _is_cn(prev) or _is_cn(nxt) or (_is_digit(prev) and _is_digit(nxt)):
                out.append("～")
                continue
        out.append(char)
    return "".join(out)
