"""文件系统工具模块(跨平台)。

提供列目录、读文本、写文本等基础能力。

权限设计(2026-08-15 调整):
- **读**(列目录/读文本)不限目录。理由:小A 就是要帮用户看自己的资料;
  而且把读锁死也挡不住什么——它照样能用 run_command 的 dir/type 读到,
  那种限制只是让它绕远路、浪费步数、还刷一堆红色报错,是"假安全"。
- **写**只允许在用户自己的常用目录(桌面/文档/各盘的工作目录等),
  并明确拦住系统关键目录(Windows、Program Files、System32…),防止误伤系统。
"""
from __future__ import annotations

import os
from pathlib import Path

from backend.core.tools import ToolRegistry
from backend.core import isolation_workspace
from backend.modules.computer.code_module import _reject_protected
from backend.modules.office.text_reading import TEXT_PAGE_CHARS, text_page

# 相对路径的落脚点(不再作为"只能在这里面"的牢笼)
_WORKSPACE_ROOT: Path | None = None

# 写操作禁区:系统关键目录,误写会弄坏系统
_WRITE_FORBIDDEN = [
    "c:/windows", "c:/program files", "c:/program files (x86)",
    "c:/programdata", "/etc", "/usr", "/bin", "/sbin", "/boot", "/sys", "/proc",
]


def set_workspace_root(path: str) -> None:
    global _WORKSPACE_ROOT
    _WORKSPACE_ROOT = Path(path).resolve()
    _WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)


def _resolve(path: str) -> Path:
    """解析路径。相对路径挂到工作目录下,绝对路径原样用。"""
    p = Path(path)
    if not p.is_absolute():
        p = (_WORKSPACE_ROOT or isolation_workspace.workspace()) / p
    return p.resolve()


def _resolve_for_write(path: str) -> Path:
    """写操作:解析并检查是否落在系统禁区。

    注意:原始字符串和解析后的路径都要查。因为在非 Windows 上,
    "C:/Windows/..." 不会被解析成 c:/ 开头的路径(会被当相对路径),
    只查解析结果会漏掉——所以两个都过一遍禁区表。
    """
    p = _resolve(path)
    for candidate in (str(path), str(p)):
        low = candidate.replace("\\", "/").lower()
        for bad in _WRITE_FORBIDDEN:
            if low == bad or low.startswith(bad + "/"):
                raise PermissionError(f"这是系统目录,不能往这里写: {candidate}")
    return p


def register_tools(registry: ToolRegistry) -> None:
    @registry.tool(
        name="workspace_info",
        description="返回当前执行电脑的隔离工作区路径。用户未指定保存位置时，文档、图片、脚本及过程文件全部存到这里；不要猜用户名或文档/桌面路径。",
        parameters={"type": "object", "properties": {}},
    )
    async def workspace_info() -> dict[str, str]:
        return {"path": str(_WORKSPACE_ROOT or isolation_workspace.workspace())}

    @registry.tool(
        name="fs_list_dir",
        description="列出某个目录下的文件和子目录。",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "目录路径"}},
            "required": ["path"],
        },
    )
    async def fs_list_dir(path: str) -> list[str]:
        p = _resolve(path)
        if not p.exists():
            return [f"路径不存在: {p}"]
        if not p.is_dir():
            return [f"不是目录: {p}"]
        try:
            return sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())
        except PermissionError:
            return [f"没有权限读取这个目录: {p}"]

    @registry.tool(
        name="fs_read_text",
        description=(
            f"分页读取文本文件，每次最多 {TEXT_PAGE_CHARS} 字符。首次省略 offset 或传 0；"
            "结果若标记未读完，必须用返回的下一页 offset 继续调用，直到已读完，"
            "不能把单页说成全文。若结果提示内容已存至 ref，先读取该 ref 的完整结果。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "description": "字符偏移量，翻页时使用上次返回的下一页 offset"},
            },
            "required": ["path"],
        },
    )
    async def fs_read_text(path: str, offset: int = 0) -> str:
        p = _resolve(path)
        if not p.exists():
            return f"文件不存在: {p}"
        if p.is_dir():
            return f"这是个目录不是文件,用 fs_list_dir 看内容: {p}"
        guides = {**dict.fromkeys((".xlsx", ".xlsm", ".xls", ".xlsb"), "excel_list_sheets / excel_read_all / excel_read_range"),
                  **dict.fromkeys((".docx", ".docm", ".doc"), "word_read_text / word_list_paragraphs；修改可用 word_find_replace / word_apply_replacements / word_insert_paragraphs / word_append_paragraphs / word_append_blocks"),
                  **dict.fromkeys((".pptx", ".pptm", ".ppt"), "ppt_read_text（pptx；旧格式请先另存）"),
                  ".pdf": "pdf_list_pages / pdf_read_text / pdf_extract_images / pdf_render_pages", ".dwg": "CAD 读取工具"}
        if p.suffix.lower() in guides:
            raise ValueError(f"该文件为二进制文档({p.suffix})，不能用 fs_read_text 按纯文本读取；请改用 {guides[p.suffix.lower()]}")
        try:
            return text_page(p.read_text(encoding="utf-8", errors="replace"), offset)
        except PermissionError:
            return f"没有权限读取: {p}"

    @registry.tool(
        name="fs_write_text",
        description="把文本写入文件(会覆盖同名文件)。危险操作,执行前会请求确认。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        side_effect=True,
        danger=True,
    )
    async def fs_write_text(path: str, content: str) -> str:
        p = _resolve_for_write(path)   # 会拦住系统目录
        _reject_protected(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"已写入 {len(content)} 字符到 {p}"
