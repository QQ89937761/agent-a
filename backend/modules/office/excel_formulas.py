"""新建 XLSX 的公式边界；不适用于含宏/命名公式的既有工作簿。"""
from __future__ import annotations

import re
from typing import Any


def is_formula(value: Any) -> bool:
    # 与 openpyxl Cell._bind_value 一致；+、-、@ 前缀在此链路只是字符串。
    return isinstance(value, str) and len(value) > 1 and value.startswith("=")


def _formula_needs_confirmation(value: Any) -> bool:
    if not is_formula(value):
        return False
    try:
        from openpyxl.formula import Tokenizer
        from openpyxl.formula.tokenizer import TokenizerError
        from openpyxl.utils import FORMULAE
    except ImportError:
        return True
    try:
        tokens = Tokenizer(value).items
    except (TokenizerError, ValueError, IndexError):
        return True
    stack = []
    for token in tokens:
        if token.type == "FUNC" and token.subtype == "OPEN":
            name = token.value[:-1].upper()
            # 已安装库的内置函数表并非安全白名单：外部连接/动态引用单独审批。
            if (name not in FORMULAE or name.startswith("CUBE")
                    or name in {"RTD", "HYPERLINK", "INDIRECT"}):
                return True
        if token.type == "OPERAND" and token.subtype == "RANGE":
            reference = token.value
            # ponytail: 只豁免本表结构化引用；列标题里的 ! / | 也是文字。
            if re.fullmatch(r"[^\W\d]\w*\[(?:[^\[\]]|\[[^\[\]]*\])*\]", reference):
                continue
            if any(char in reference for char in "|\\/[]"):
                return True
            if "!" in reference:
                scope, target = reference.rsplit("!", 1)
                # 外部工作簿的命名范围可能没有 []，不能只查方括号。
                if re.search(r"\.(?:xls[bxm]?|xla[m]?|csv|ods)'?$", scope, re.IGNORECASE):
                    return True
                if not re.fullmatch(r"\$?[A-Z]+\$?[1-9][0-9]*(?::\$?[A-Z]+\$?[1-9][0-9]*)?"
                                    r"|\$?[A-Z]+:\$?[A-Z]+|\$?[1-9][0-9]*:\$?[1-9][0-9]*", target, re.IGNORECASE):
                    return True
        if token.type in {"FUNC", "PAREN", "ARRAY"}:
            if token.subtype == "OPEN":
                stack.append(token.type)
            elif token.subtype == "CLOSE":
                if not stack or stack.pop() != token.type:
                    return True
    # Tokenizer 分词不保证括号配平；无法确认的公式不冒充纯计算。
    return not tokens or bool(stack)


def new_workbook_needs_confirmation(headers: Any = None, rows: Any = None, sheets: Any = None) -> bool:
    if sheets is not None:
        return isinstance(sheets, list) and any(
            new_workbook_needs_confirmation(spec.get("headers"), spec.get("rows"))
            for spec in sheets if isinstance(spec, dict))
    # 形状错误会由创建工具在写文件前拒绝，不为无效请求消耗对话授权。
    if headers is not None and not isinstance(headers, list):
        return False
    if rows is not None and (not isinstance(rows, list)
                             or any(not isinstance(row, list) for row in rows)):
        return False
    return (any(_formula_needs_confirmation(value) for value in headers or [])
            or any(_formula_needs_confirmation(value) for row in rows or [] for value in row))


def new_workbook_execution_formulas(headers: Any = None, rows: Any = None, sheets: Any = None) -> list[str]:
    if isinstance(sheets, list):
        return [formula for spec in sheets if isinstance(spec, dict)
                for formula in new_workbook_execution_formulas(spec.get("headers"), spec.get("rows"))]
    """仅提取真正执行代码的公式，保留远程不可逆操作等原有硬保护。"""
    if (headers is not None and not isinstance(headers, list)) or (
        rows is not None and (not isinstance(rows, list)
                             or any(not isinstance(row, list) for row in rows))
    ):
        return []
    try:
        from openpyxl.formula import Tokenizer
        from openpyxl.formula.tokenizer import TokenizerError
    except ImportError:
        # 缺依赖时创建工具也无法执行；公式风险仍由上面的审批检查保守处理。
        return []

    result = []
    for group in [headers or [], *(rows or [])]:
        for value in group:
            if not is_formula(value):
                continue
            try:
                tokens = Tokenizer(value).items
            except (TokenizerError, ValueError, IndexError):
                # DDE 不被 Tokenizer 支持；只识别代码位置的管道，不扫描普通数据。
                if re.search(r"(?:^=|[,(+*/-])\s*(?:\w+|'[^']+')\s*\|", value):
                    result.append(value)
                continue
            if any(t.type == "FUNC" and t.subtype == "OPEN"
                   and t.value[:-1].upper() in {"EXEC", "CALL", "REGISTER", "REGISTER.ID"}
                   for t in tokens):
                result.append(value)
    return result
