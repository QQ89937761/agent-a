"""Shared paging helpers for long text-reading tools."""
from __future__ import annotations


TEXT_PAGE_CHARS = 20000


def text_page(text: str, offset: int = 0) -> str:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset 必须是非负整数")

    total = len(text)
    if offset > total:
        raise ValueError(f"offset 超出文件长度（{total} 字符）")
    end = min(offset + TEXT_PAGE_CHARS, total)
    page = text[offset:end]
    complete = end >= total
    status = "已读完" if complete else f"未读完；请继续读取，下一页 offset={end}"
    page_range = f"{offset + 1}-{end}" if page else "无内容"
    return (
        f"[文本分页] 总字符数={total}；本页范围={page_range}；"
        f"状态={status}\n{page}"
    )
