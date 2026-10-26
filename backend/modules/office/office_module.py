"""Office 工具：常见格式直接只读解析，编辑与旧格式使用 COM。

未安装 Office/COM 也注册文件读取工具；不因读取而启动 Office。
用户已打开的目标优先读取当前内容，不保存、不关闭其应用实例。
"""
from __future__ import annotations

import sys
import asyncio
import functools
import os
import time
from pathlib import Path
from typing import Any

from backend.core.tools import ToolRegistry, with_risk_confirmation
from backend.modules.office.fs_module import _resolve, _resolve_for_write
from backend.modules.office.text_reading import TEXT_PAGE_CHARS, text_page
from backend.modules.office.office_reading import (read_excel_file, read_word_file, supports_file_read, select_sheet, list_excel_sheets, read_ppt_file, read_pdf_file, list_pdf_pages, extract_pdf_images, render_pdf_pages)

_IS_WINDOWS = sys.platform == "win32"


def _office_owner_files(path: Path, *, word: bool = False) -> set[Path]:
    owners = {path.with_name(f"~${path.name}")}  # WPS / Excel
    if word and len(path.stem) > 2:
        owners.add(path.with_name(f"~${path.name[2:]}"))  # Microsoft Word
    return owners


def _check_office_owner_files(path: Path, *, word: bool = False) -> None:
    if any(owner.exists() for owner in _office_owner_files(path, word=word)):
        # ponytail: COM 看不到 WPS 会话时不猜测磁盘值，也不覆盖未保存内容。
        # 占用标记可能残留；不能仅因 GetActiveObject 失败就当作已关闭。
        raise RuntimeError(
            f"检测到 Excel/Word/WPS 文件占用标记，无法安全访问当前内容或写入。"
            f"请先在应用中保存并关闭此文件后重试；若已关闭，请确认占用标记是否残留: {path}"
        )


_WORD_PROGIDS = ("Word.Application", "KWPS.Application", "WPS.Application")
_EXCEL_PROGIDS = ("Excel.Application", "KET.Application", "ET.Application")


def _retry_com_busy(call):
    for attempt in range(4):
        try:
            return call()
        except Exception as exc:
            code = getattr(exc, "hresult", None)
            if code is None and exc.args:
                code = exc.args[0]
            if code not in {-2147418111, -2147417846} or attempt == 3:
                raise
            time.sleep(0.1 * (attempt + 1))


def _find_open(client, path, progids, collection):
    for progid in progids:
        try:
            app = _retry_com_busy(lambda: client.GetActiveObject(progid))
            items = getattr(app, collection)
            for index in range(1, _retry_com_busy(lambda: items.Count) + 1):
                item = _retry_com_busy(lambda: items.Item(index))
                if Path(str(_retry_com_busy(lambda: item.FullName))).resolve() == path:
                    return app, item, False
        except Exception:
            continue
    return None


def _dispatch_office(client, progids):
    if client is None:
        raise RuntimeError("此格式需要本机 Office/WPS COM；建议先另存为 xlsx/docx")
    error = None
    for progid in progids:
        try:
            return _retry_com_busy(lambda: client.DispatchEx(progid))
        except Exception as exc:
            error = exc
    raise RuntimeError(f"未找到可用的 Office/WPS COM 应用: {error}") from error


def _find_open_word_document(client: Any, path: Path) -> tuple[Any, Any, bool] | None:
    return _find_open(client, path, _WORD_PROGIDS, "Documents")


def _saved_warning(path, *, word=False):
    if any(owner.exists() for owner in _office_owner_files(path, word=word)):
        return "[提示：检测到 ~$ 占用标记，以下仅为磁盘已保存版本，可能不含 WPS/Office 中未保存的修改]"
    return ""


def _file_read_result(path, data, *, word=False):
    warning = _saved_warning(path, word=word)
    return {"warning": warning, "data": data} if warning else data


def _com_sheet(workbook, sheet=None):
    if not sheet:
        return _retry_com_busy(lambda: workbook.Worksheets(1))
    for name in dict.fromkeys((sheet, sheet.strip())):
        try:
            return _retry_com_busy(lambda: workbook.Worksheets(name))
        except Exception:
            pass
    names = [_retry_com_busy(lambda: workbook.Worksheets(i).Name)
             for i in range(1, workbook.Worksheets.Count + 1)]
    raise ValueError(f"工作表「{sheet}」不存在；当前文件包含的工作表：{names}")


def _check_closed(client, path, *, word=False):
    _check_office_owner_files(path, word=word)
    if client is not None:
        opened = (_find_open_word_document if word else _find_open_excel_workbook)(client, path)
        if opened is not None:
            raise RuntimeError(f"目标文件已打开，请先确认并保存后关闭再操作: {path}")


def _open_word_document(
    client: Any, path: Path, *, read_only: bool = False
) -> tuple[Any, Any, bool]:
    opened = _find_open_word_document(client, path)
    if opened is not None:
        return opened

    # create_word_doc starts WPS asynchronously. Give its COM session time to
    # appear, but never mistake a missing COM object for an unoccupied file.
    owner_files = _office_owner_files(path, word=True)
    if any(owner.exists() for owner in owner_files):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            time.sleep(0.1)
            opened = _find_open_word_document(client, path)
            if opened is not None:
                return opened

    _check_office_owner_files(path, word=True)
    word = _dispatch_office(client, _WORD_PROGIDS)
    word.Visible = False
    word.DisplayAlerts = 0
    try:
        return word, _retry_com_busy(lambda: word.Documents.Open(str(path), ReadOnly=read_only)), True
    except Exception:
        try:
            word.Quit()
        except Exception:
            pass
        raise


def _release_word_document(word: Any, doc: Any, owned: bool) -> None:
    if not owned:
        return
    try:
        doc.Close(SaveChanges=False)
    except Exception:
        pass
    try:
        word.Quit()
    except Exception:
        # WPS often exits its COM server as soon as the last document closes.
        pass


def _find_open_excel_workbook(client: Any, path: Path) -> tuple[Any, Any, bool] | None:
    return _find_open(client, path, _EXCEL_PROGIDS, "Workbooks")


def _open_excel_workbook(
    client: Any, path: Path, *, create: bool = False, read_only: bool = False
) -> tuple[Any, Any, bool]:
    opened = _find_open_excel_workbook(client, path)
    if opened is not None:
        return opened

    _check_office_owner_files(path)
    excel = _dispatch_office(client, _EXCEL_PROGIDS)
    excel.Visible = False
    excel.DisplayAlerts = False
    try:
        if create:
            workbook = excel.Workbooks.Add()
        elif read_only:
            workbook = _retry_com_busy(lambda: excel.Workbooks.Open(str(path), ReadOnly=True, UpdateLinks=0))
        else:
            workbook = _retry_com_busy(lambda: excel.Workbooks.Open(str(path)))
        return excel, workbook, True
    except Exception:
        try:
            excel.Quit()
        except Exception:
            pass
        raise


def _release_excel_workbook(excel: Any, workbook: Any, owned: bool) -> None:
    if not owned:
        return
    try:
        workbook.Close(SaveChanges=False)
    except Exception:
        pass
    try:
        excel.Quit()
    except Exception:
        pass


def _edit_excel_file(client, path, sheet, *, cells=None, values=None):
    from openpyxl import Workbook, load_workbook
    from openpyxl.utils.cell import range_boundaries
    from backend.modules.office.office_files import atomic_office_save, validate_plain_ooxml, save_excel_preserving_names
    _check_closed(client, path)
    validate_plain_ooxml(path)
    if cells is not None:
        if not isinstance(cells, dict):
            raise ValueError("cells 必须是单元格到值的映射")
        for address, value in cells.items():
            bounds = range_boundaries(address)
            if (any(index is None for index in bounds)
                    or not 1 <= bounds[0] <= bounds[2] <= 16384
                    or not 1 <= bounds[1] <= bounds[3] <= 1048576):
                raise ValueError(f"无效单元格地址: {address}")
            width, height = bounds[2] - bounds[0] + 1, bounds[3] - bounds[1] + 1
            if width * height > 1:
                if (not isinstance(value, list) or len(value) != height
                        or any(not isinstance(row, list) or len(row) != width for row in value)):
                    raise ValueError(f"区域 {address} 需要 {height} 行 {width} 列的二维数组")
    elif not isinstance(values, list) or not values or len(values) > 16384:
        raise ValueError("values 必须是非空数组，最多 16384 列")
    if values is not None and not path.exists():
        raise FileNotFoundError(path)
    wb = load_workbook(path) if path.exists() else Workbook()
    try:
        ws = select_sheet(wb, sheet)
        if cells is not None:
            for address, value in cells.items():
                left, top, right, bottom = range_boundaries(address)
                if left == right and top == bottom:
                    ws.cell(top, left).value = value
                else:
                    for row_index, row in enumerate(value, top):
                        for column, item in enumerate(row, left):
                            ws.cell(row_index, column).value = item
            result = f"已写入 {len(cells)} 个单元格到 {path}"
        else:
            last = max((cell.row for row in ws for cell in row if cell.value is not None), default=0)
            if last >= 1048576:
                raise ValueError("工作表已达最大行数")
            for column, value in enumerate(values, 1):
                ws.cell(last + 1, column, value)
            result = f"已在第 {last + 1} 行追加 {len(values)} 列数据"
        backup = atomic_office_save(path, lambda temporary: save_excel_preserving_names(path, wb, temporary), overwrite=True,
                                   check=lambda: _check_closed(client, path))
        return result + (f"；原文件备份: {backup}" if backup else "")
    finally:
        wb.close()



def _edit_word_file(client, path, find, replace, *, save=True):
    from docx import Document
    from backend.modules.office.office_files import atomic_office_save, validate_plain_ooxml
    _check_closed(client, path, word=True)
    validate_plain_ooxml(path)
    doc = Document(str(path))
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    replaced_count = 0
    changed_parts = {}
    boundaries = {ns + tag for tag in ("tab", "br", "cr", "drawing", "pict")}
    for part in doc.part.package.parts:
        root = getattr(part, "element", None)
        if root is None:
            continue
        for paragraph in root.iter(ns + "p"):
            # Break matching at tabs, line breaks and objects: adjacent text
            # XML nodes are not necessarily adjacent visible text. Never flatten runs.
            groups = [[]]
            for node in paragraph.iter():
                if node.tag in boundaries:
                    groups.append([])
                elif node.tag == ns + "t" and next(node.iterancestors(ns + "p"), None) is paragraph:
                    groups[-1].append(node)
            for nodes in groups:
                text = "".join(node.text or "" for node in nodes)
                if find not in text:
                    continue
                if any(list(paragraph.iter(ns + tag)) for tag in ("fldChar", "instrText", "fldSimple")):
                    raise ValueError("匹配段落含域代码，无法保真替换，未保存；请在文档应用中修改")
                import re
                offsets = []
                cursor = 0
                for node in nodes:
                    offsets.append(cursor)
                    cursor += len(node.text or "")
                matches = list(re.finditer(re.escape(find), text))
                replaced_count += len(matches)
                for match in reversed(matches):
                    start, end = match.span()
                    first = max(i for i, offset in enumerate(offsets) if offset <= start)
                    last = max(i for i, offset in enumerate(offsets) if offset < end)
                    prefix = (nodes[first].text or "")[:start - offsets[first]]
                    suffix = (nodes[last].text or "")[end - offsets[last]:]
                    nodes[first].text = prefix + replace + (suffix if first == last else "")
                    for i in range(first + 1, last):
                        nodes[i].text = ""
                    if last != first:
                        nodes[last].text = suffix
                for node in nodes:
                    node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                changed_parts[str(part.partname).lstrip("/")] = part
    if not replaced_count:
        return f"未在文档中找到「{find}」，未修改: {path}"
    def save_changed_parts(temporary):
        from zipfile import ZipFile
        # Serialize only edited text parts. Keep all other parts, relationships,
        # embedded objects and WPS metadata byte-for-byte, including orphan parts.
        with ZipFile(path) as source, ZipFile(temporary, "w") as output:
            output.comment = source.comment
            for info in source.infolist():
                part = changed_parts.get(info.filename)
                output.writestr(info, part.blob if part is not None else source.read(info))
    if save:
        backup = atomic_office_save(path, save_changed_parts, overwrite=True,
                                   check=lambda: _check_closed(client, path, word=True))
    else:
        # 工作副本只用于组合多条规则；不创建“备份副本”，也不把中间文件当作交付物。
        import os
        import tempfile
        fd, temporary = tempfile.mkstemp(prefix=".xiaoa-word-stage-", suffix=path.suffix, dir=path.parent)
        os.close(fd)
        try:
            save_changed_parts(temporary)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        backup = None
    return f"已把 {replaced_count} 处「{find}」替换为「{replace}」；原文件备份: {backup}" if save else f"已把 {replaced_count} 处「{find}」替换到工作副本"


def _com_available() -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        import win32com.client  # noqa: F401
        return True
    except ImportError:
        return False


def _check_closed_for_save(path: Path, *, word: bool = False) -> None:
    if not _com_available():
        _check_closed(None, path, word=word)
        return
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        _check_closed(win32com.client, path, word=word)
    finally:
        pythoncom.CoUninitialize()


def register_tools(registry: ToolRegistry) -> None:
    com_available = _com_available()
    client = None
    if com_available:
        import win32com.client  # type: ignore
        client = win32com.client

    def _com_async(fn):
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            def call():
                if not com_available:
                    return fn(*args, **kwargs)
                import pythoncom
                pythoncom.CoInitialize()
                try:
                    return fn(*args, **kwargs)
                finally:
                    pythoncom.CoUninitialize()
            return await asyncio.to_thread(call)
        return wrapped

    # ---------- Excel:读取区域 ----------
    @registry.tool(
        name="excel_read_range",
        description=("读取 Excel 指定工作表的单元格区域。单个单元格返回一个值，多格返回二维数组。"
                     "xlsx/xlsm 等直接读取已保存值，不执行宏、外链或公式；公式无缓存返回空，缓存可能过期。"
                     "已打开目标在 COM 可访问时读取当前值，否则附提示读取磁盘已保存版本；旧格式需 COM 和对话级一次授权。"),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "xlsx 文件绝对路径"},
                "sheet": {"type": "string", "description": "工作表名,默认第一个"},
                "cell_range": {"type": "string", "description": "如 A1:D20"},
            },
            "required": ["path", "cell_range"],
        },
    )
    @_com_async
    def excel_read_range(path: str, cell_range: str, sheet: str | None = None) -> Any:
        p = _resolve(path)
        # 与中枢按同一原始 path 选择解析链路；解析失败不回退到未授权 COM。
        # 不打开应用即可读取保存数据；已打开目标优先保留用户的未保存视图。
        opened = _find_open_excel_workbook(client, p) if client is not None else None
        if opened is None and supports_file_read("excel_read_range", {"path": path}):
            return _file_read_result(p, read_excel_file(p, sheet, cell_range))
        if client is None:
            raise RuntimeError("此格式需要本机 Excel/WPS 和 COM；常见 xlsx/xlsm 可直接读取")
        excel, wb, owned = opened or _open_excel_workbook(client, p, read_only=True)
        try:
            ws = _com_sheet(wb, sheet)
            return ws.Range(cell_range).Value
        finally:
            _release_excel_workbook(excel, wb, owned)

    # ---------- Excel:读取整张已用区域 ----------
    @registry.tool(
        name="excel_read_all",
        description=("读取 Excel 某个工作表的全部已用区域（无需指定范围），单格返回一个值，多格返回二维数组。"
                     "xlsx/xlsm 等直接读取已保存值，不执行宏、外链或公式；公式无缓存返回空，缓存可能过期。"
                     "已打开目标在 COM 可访问时读取当前值，否则附提示读取磁盘已保存版本；旧格式需 COM 和对话级一次授权。"),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "xlsx 绝对路径"},
                "sheet": {"type": "string", "description": "工作表名,默认第一个"},
            },
            "required": ["path"],
        },
    )
    @_com_async
    def excel_read_all(path: str, sheet: str | None = None) -> Any:
        p = _resolve(path)
        opened = _find_open_excel_workbook(client, p) if client is not None else None
        if opened is None and supports_file_read("excel_read_all", {"path": path}):
            return _file_read_result(p, read_excel_file(p, sheet))
        if client is None:
            raise RuntimeError("此格式需要本机 Excel/WPS 和 COM；常见 xlsx/xlsm 可直接读取")
        excel, wb, owned = opened or _open_excel_workbook(client, p, read_only=True)
        try:
            ws = _com_sheet(wb, sheet)
            return ws.UsedRange.Value
        finally:
            _release_excel_workbook(excel, wb, owned)

    # ---------- Word:读取全文 ----------
    @registry.tool(
        name="word_read_text",
        description=(
            f"分页读取 Word 文档文字，每次最多 {TEXT_PAGE_CHARS} 字符。首次省略 offset 或传 0；"
            "结果若标记未读完，必须用返回的下一页 offset 继续调用，直到已读完，"
            "不能把单页说成全文。若结果提示内容已存至 ref，先读取该 ref 的完整结果。"
            "docx/docm 直接读取已保存正文和表格，不启动 Word，不执行宏或外链。"
            "已打开目标在 COM 可访问时读取当前正文，否则附提示读取磁盘已保存版本；旧格式需 COM 和对话级一次授权。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "docx 绝对路径"},
                "offset": {"type": "integer", "minimum": 0, "description": "字符偏移量，翻页时使用上次返回的下一页 offset"},
            },
            "required": ["path"],
        },
    )
    @_com_async
    def word_read_text(path: str, offset: int = 0) -> str:
        p = _resolve(path)
        opened = _find_open_word_document(client, p) if client is not None else None
        if opened is None and supports_file_read("word_read_text", {"path": path}):
            return (_saved_warning(p, word=True) + "\n" if _saved_warning(p, word=True) else "") + text_page(read_word_file(p), offset)
        if client is None:
            raise RuntimeError("此格式需要本机 Word/WPS 和 COM；常见 docx/docm 可直接读取")
        word, doc, owned = opened or _open_word_document(client, p, read_only=True)
        try:
            text = doc.Content.Text
            return text_page(text, offset)
        finally:
            _release_word_document(word, doc, owned)

    @registry.tool(
        name="word_list_paragraphs",
        description="列出现有 .docx 正文段落的当前序号、样式和文字，便于按真实标题定位插入位置；返回全部段落的分页结果，不读取表格内段落。",
        parameters={"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
        }, "required": ["path"]},
    )
    @_com_async
    def word_list_paragraphs(path: str, offset: int = 0, limit: int = 200):
        from docx import Document
        p = _resolve(path)
        if not p.is_file():
            raise FileNotFoundError(f"Word 文件不存在: {p}")
        if p.suffix.lower() != ".docx":
            raise ValueError("段落索引目前支持普通 .docx")
        if (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
                or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500):
            raise ValueError("offset 必须非负，limit 必须为 1 到 500")
        _check_closed(client, p, word=True)
        paragraphs = Document(str(p)).paragraphs
        end = min(offset + limit, len(paragraphs))
        return {
            "path": str(p), "total": len(paragraphs), "offset": offset,
            "next_offset": end if end < len(paragraphs) else None,
            "paragraphs": [
                {"index": index, "style": paragraph.style.name or "", "text": paragraph.text}
                for index, paragraph in enumerate(paragraphs[offset:end], offset)
            ],
        }

    @registry.tool(name="excel_list_sheets", description="列出 Excel 的工作表名称和已用行列；优先读取活动文档，否则读取磁盘已保存版本。",
                   parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
    @_com_async
    def excel_list_sheets(path: str):
        p = _resolve(path)
        opened = _find_open_excel_workbook(client, p) if client else None
        if opened is None and supports_file_read("excel_list_sheets", {"path": path}):
            return _file_read_result(p, list_excel_sheets(p))
        excel, wb, owned = opened or _open_excel_workbook(client, p, read_only=True)
        try:
            sheets = []
            for i in range(1, wb.Worksheets.Count + 1):
                ws = wb.Worksheets(i)
                sheets.append({"name": ws.Name, "max_row": ws.UsedRange.Row + ws.UsedRange.Rows.Count - 1,
                               "max_column": ws.UsedRange.Column + ws.UsedRange.Columns.Count - 1})
            return {"path": str(p), "sheets": sheets}
        finally:
            _release_excel_workbook(excel, wb, owned)

    @registry.tool(name="ppt_read_text", description="分页读取 pptx 幻灯片正文、表格和备注，不启动 Office，不执行宏。",
                   parameters={"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}}, "required": ["path"]})
    async def ppt_read_text(path: str, offset: int = 0):
        if not supports_file_read("ppt_read_text", {"path": path}):
            raise ValueError("仅支持 pptx；请先把旧格式或宏格式演示文稿另存为 pptx")
        return await asyncio.to_thread(lambda: text_page(read_ppt_file(_resolve(path)), offset))

    # ---------- PDF:读取与图片提取 ----------
    @registry.tool(
        name="pdf_read_text",
        description=(
            f"分页读取 PDF 全文，每次最多 {TEXT_PAGE_CHARS} 字符；自动逐页提取文字，"
            "不要求用户手动导出页面。扫描型 PDF 若没有文字层，会明确返回空文字并提示需要 OCR。"
        ),
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "PDF 绝对路径"},
            "offset": {"type": "integer", "minimum": 0},
        }, "required": ["path"]},
    )
    async def pdf_read_text(path: str, offset: int = 0):
        p = _resolve(path)
        if not p.is_file():
            raise FileNotFoundError(f"PDF 文件不存在: {p}")
        if p.suffix.lower() != ".pdf":
            raise ValueError(f"目标不是 PDF 文件: {p}")
        result = await asyncio.to_thread(read_pdf_file, p)
        if not result or not any(line.strip() and not line.startswith("【第") for line in result.splitlines()):
            result += "\n[该 PDF 没有可提取的文字层；如需识别扫描页，请使用 OCR 工具或提供可搜索版 PDF]"
        return text_page(result, offset)

    @registry.tool(
        name="pdf_list_pages",
        description="检查 PDF 是否真实存在并返回页数，避免把不存在的路径继续当成已处理。",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    async def pdf_list_pages(path: str):
        p = _resolve(path)
        if not p.is_file():
            raise FileNotFoundError(f"PDF 文件不存在: {p}")
        return await asyncio.to_thread(list_pdf_pages, p)

    @registry.tool(
        name="pdf_extract_images",
        description=with_risk_confirmation(
            "提取 PDF 内嵌图片到指定目录并返回真实文件路径；不要求用户手动逐页导出。"
            "只提取 PDF 已包含的图片，不对扫描页自动 OCR。"
        ),
        parameters={"type": "object", "properties": {
            "path": {"type": "string"}, "output_dir": {"type": "string"},
        }, "required": ["path", "output_dir"]}, side_effect=True, danger=True,
    )
    async def pdf_extract_images(path: str, output_dir: str):
        p = _resolve(path)
        if not p.is_file():
            raise FileNotFoundError(f"PDF 文件不存在: {p}")
        target = _resolve_for_write(output_dir)
        return await asyncio.to_thread(extract_pdf_images, p, target)

    @registry.tool(
        name="pdf_render_pages",
        description=with_risk_confirmation(
            "把 PDF 指定页面渲染成 PNG 并返回真实路径；适用于扫描 PDF、图纸和需要视觉核对的页面，"
            "不要求用户手动截图或导出。"
        ),
        parameters={"type": "object", "properties": {
            "path": {"type": "string"},
            "output_dir": {"type": "string"},
            "pages": {"type": "array", "items": {"type": "integer", "minimum": 1}},
            "dpi": {"type": "integer", "minimum": 72, "maximum": 600, "default": 150},
        }, "required": ["path", "output_dir"]}, side_effect=True, danger=True,
    )
    async def pdf_render_pages(path: str, output_dir: str, pages: list[int] | None = None, dpi: int = 150):
        p = _resolve(path)
        if not p.is_file():
            raise FileNotFoundError(f"PDF 文件不存在: {p}")
        if p.suffix.lower() != ".pdf":
            raise ValueError(f"目标不是 PDF 文件: {p}")
        target = _resolve_for_write(output_dir)
        return await asyncio.to_thread(render_pdf_pages, p, target, pages=pages, dpi=dpi)

    # ---------- Excel:写入单元格 ----------
    @registry.tool(
        name="excel_write_cells",
        description=with_risk_confirmation(
            "向 Excel 指定单元格写入值并保存。目标文件已在 Excel/WPS 中打开时拒绝操作，"
            "避免覆盖用户未保存的内容。这是有副作用的写操作。"
            "普通 xlsx/docx 在进程内修改，备份后原子保存；旧格式使用 COM，可能触发已有宏、外链或事件，需对话授权。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "xlsx 文件绝对路径"},
                "sheet": {"type": "string"},
                "cells": {
                    "type": "object",
                    "description": "单元格到值的映射,如 {\"A1\": \"姓名\", \"B1\": 100}",
                },
            },
            "required": ["path", "cells"],
        },
        side_effect=True,
        danger=True,
    )
    @_com_async
    def excel_write_cells(path: str, cells: dict[str, Any], sheet: str | None = None) -> str:
        p = _resolve_for_write(path)
        if Path(path).suffix.lower() == ".xlsx":
            return _edit_excel_file(client, p, sheet, cells=cells)
        exists = p.exists()
        excel, wb, owned = _open_excel_workbook(client, p, create=not exists)
        try:
            if not owned:
                raise RuntimeError(f"目标 Excel 文件已打开，请先确认并保存后关闭再操作: {p}")
            if wb.ReadOnly:
                raise RuntimeError(f"目标 Excel 文件以只读方式打开，未修改或保存: {p}")
            ws = _com_sheet(wb, sheet)
            for addr, val in cells.items():
                ws.Range(addr).Value = val
            _retry_com_busy(lambda: wb.SaveAs(str(p)) if not exists else wb.Save())
            if not wb.Saved:
                raise RuntimeError(f"Excel 未确认保存成功，不报告写入成功: {p}")
            return f"已写入 {len(cells)} 个单元格到 {p}"
        finally:
            _release_excel_workbook(excel, wb, owned)

    # 说明:新建 Word 文档改用 docgen_module 的 create_word_doc(python-docx,
    # 不依赖微软 Word,WPS 也能打开),所以这里不再提供 COM 版的新建。

    # ---------- Excel:在末尾追加一行 ----------
    @registry.tool(
        name="excel_append_row",
        description=with_risk_confirmation(
            "在 Excel 工作表已有数据的下方追加一行并保存。目标文件已在 Excel/WPS 中打开时"
            "拒绝操作，避免覆盖用户未保存的内容。有副作用。"
            "普通 xlsx/docx 在进程内修改，备份后原子保存；旧格式使用 COM，可能触发已有宏、外链或事件，需对话授权。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "xlsx 绝对路径"},
                "sheet": {"type": "string"},
                "values": {"type": "array", "items": {}, "description": "一行的各列值,如 [\"张三\", 25, \"北京\"]"},
            },
            "required": ["path", "values"],
        },
        side_effect=True,
        danger=True,
    )
    @_com_async
    def excel_append_row(path: str, values: list, sheet: str | None = None) -> str:
        p = _resolve_for_write(path)
        if Path(path).suffix.lower() == ".xlsx":
            return _edit_excel_file(client, p, sheet, values=values)
        excel, wb, owned = _open_excel_workbook(client, p)
        try:
            if not owned:
                raise RuntimeError(f"目标 Excel 文件已打开，请先确认并保存后关闭再操作: {p}")
            if wb.ReadOnly:
                raise RuntimeError(f"目标 Excel 文件以只读方式打开，未修改或保存: {p}")
            ws = _com_sheet(wb, sheet)
            used = ws.UsedRange
            next_row = 1 if used.Row == 1 and used.Rows.Count == 1 and used.Value is None else used.Rows.Count + used.Row
            for col, val in enumerate(values, start=1):
                ws.Cells(next_row, col).Value = val
            _retry_com_busy(wb.Save)
            if not wb.Saved:
                raise RuntimeError(f"Excel 未确认保存成功，不报告追加成功: {p}")
            return f"已在第 {next_row} 行追加 {len(values)} 列数据"
        finally:
            _release_excel_workbook(excel, wb, owned)

    # ---------- Word:查找替换 ----------
    @registry.tool(
        name="word_find_replace",
        description=with_risk_confirmation(
            "在 Word 文档里把某段文字全部替换成另一段。若文档已在 Word/WPS 中打开，"
            "为避免混入用户未保存编辑而拒绝操作；请先保存并关闭文档。有副作用。"
            "普通 xlsx/docx 在进程内修改，备份后原子保存；旧格式使用 COM，可能触发已有宏、外链或事件，需对话授权。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "docx 绝对路径"},
                "find": {"type": "string", "description": "要查找的文字"},
                "replace": {"type": "string", "description": "替换成的文字"},
            },
            "required": ["path", "find", "replace"],
        },
        side_effect=True,
        danger=True,
    )
    @_com_async
    def word_find_replace(path: str, find: str, replace: str) -> str:
        p = _resolve_for_write(path)
        if not p.is_file():
            raise FileNotFoundError(f"Word 文件不存在: {p}")
        if not find:
            raise ValueError("查找文字 find 不能为空")
        if Path(path).suffix.lower() == ".docx":
            return _edit_word_file(client, p, find, replace)
        word, doc, owned = _open_word_document(client, p)
        try:
            if not owned:
                raise RuntimeError(f"目标 Word 文档已打开，请先保存并关闭后再操作: {p}")
            if doc.ReadOnly:
                raise RuntimeError(f"目标 Word 文档以只读方式打开，未修改或保存: {p}")
            rng = doc.Content
            rng.Find.Execute(find, False, False, False, False, False, True, 1, False, replace, 2)
            _retry_com_busy(doc.Save)
            if not doc.Saved:
                raise RuntimeError(f"Word 未确认保存成功，不报告替换成功: {p}")
            return f"已把「{find}」替换为「{replace}」"
        finally:
            _release_word_document(word, doc, owned)


    @registry.tool(
        name="word_apply_replacements",
        description=with_risk_confirmation(
            "对同一个普通 .docx 文档执行多组全文查找替换；每组替换覆盖正文、表格、页眉页脚和脚注等可编辑文字，"
            "不需要用户手工复制粘贴；未命中是正常结果，不会改动或保存文档。"
            "文档已在 Word/WPS 打开、目标不存在、包含宏/外链/复杂域代码时停止，不伪报完成。"
        ),
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "现有 .docx 绝对路径"},
            "replacements": {"type": "array", "items": {"type": "object", "properties": {
                "find": {"type": "string"}, "replace": {"type": "string"},
            }, "required": ["find", "replace"]}},
        }, "required": ["path", "replacements"]}, side_effect=True, danger=True,
    )
    @_com_async
    def word_apply_replacements(path: str, replacements: list[dict]) -> str:
        import shutil
        import tempfile
        from backend.modules.office.office_files import atomic_office_save
        p = _resolve_for_write(path)
        if not p.is_file():
            raise FileNotFoundError(f"Word 文件不存在: {p}")
        if p.suffix.lower() not in {".docx", ".docm", ".doc"}:
            raise RuntimeError("批量 Word 编辑只支持 .docx、.docm 或 .doc")
        if not isinstance(replacements, list) or not replacements:
            raise ValueError("replacements 必须是非空数组")
        results = []
        if p.suffix.lower() == ".docx":
            # 先在同目录工作副本中完成全部规则，最后只对原文档做一次原子替换。
            # 任一规则失败都不会留下“改了一半”的原文档，也不会把补充文档当成交付物。
            fd, temporary = tempfile.mkstemp(prefix=".xiaoa-word-", suffix=".working", dir=p.parent)
            os.close(fd)
            working = Path(temporary)
            try:
                shutil.copy2(p, working)
                changed = False
                for item in replacements:
                    if not isinstance(item, dict) or not str(item.get("find", "")):
                        raise ValueError("每组替换都必须包含非空 find 和 replace")
                    result = _edit_word_file(client, working, str(item["find"]), str(item.get("replace", "")), save=False)
                    results.append(result)
                    changed = changed or result.startswith("已把 ")
                if not changed:
                    return f"未找到任何待替换文字；原 Word 文档未修改、未保存: {p}\n" + "\n".join(results)
                backup = atomic_office_save(
                    p, lambda target: shutil.copyfile(working, target), overwrite=True,
                    check=lambda: _check_closed(client, p, word=True),
                )
                # 重新读取已保存的原文档；打开失败时不报告完成。
                verified = read_word_file(p)
                if not verified:
                    raise RuntimeError(f"原 Word 文档保存后无法重新读取，不能报告完成: {p}")
            finally:
                working.unlink(missing_ok=True)
            return f"已完成 {len(results)} 组全文替换，已直接修改原文档并重新读取核验: {p}；原文件备份: {backup}\n" + "\n".join(results)
        else:
            word, doc, owned = _open_word_document(client, p)
            try:
                if not owned:
                    raise RuntimeError(f"目标 Word 文档已打开，请先保存并关闭后再操作: {p}")
                if doc.ReadOnly:
                    raise RuntimeError(f"目标 Word 文档以只读方式打开，未修改或保存: {p}")
                total = 0
                for item in replacements:
                    if not isinstance(item, dict) or not str(item.get("find", "")):
                        raise ValueError("每组替换都必须包含非空 find 和 replace")
                    find, replace = str(item["find"]), str(item.get("replace", ""))
                    rng = doc.Content
                    matched = bool(rng.Find.Execute(find, False, False, False, False, False, True, 1, False, replace, 2))
                    if matched:
                        total += 1
                    results.append(f"已替换「{find}」" if matched else f"未找到「{find}」")
                if not total:
                    return f"未找到任何待替换文字；原 Word 文档未修改、未保存: {p}\n" + "\n".join(results)
                _retry_com_busy(doc.Save)
                if not doc.Saved:
                    raise RuntimeError(f"Word 未确认保存成功，不报告替换成功: {p}")
            finally:
                _release_word_document(word, doc, owned)
        return f"已完成 {len(results)} 组全文替换，已直接修改原文档并实际保存: {p}\n" + "\n".join(results)


    @registry.tool(
        name="word_append_paragraphs",
        description=with_risk_confirmation(
            "把一组正式段落直接追加到现有普通 .docx 文档末尾并保存，适合把依据 PDF 整理出的补充章节直接写入原文档，"
            "不再要求用户手工复制粘贴；这是对用户原文档的直接修改，不是生成替代文档。目标不存在、文档已打开或保存失败时停止并报告，不生成虚假的完成消息。"
        ),
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "现有 .docx 绝对路径"},
            "paragraphs": {"type": "array", "items": {"type": "string"}},
        }, "required": ["path", "paragraphs"]}, side_effect=True, danger=True,
    )
    @_com_async
    def word_append_paragraphs(path: str, paragraphs: list[str]) -> str:
        from docx import Document
        from backend.modules.office.office_files import atomic_office_save, validate_plain_ooxml
        p = _resolve_for_write(path)
        if not p.is_file():
            raise FileNotFoundError(f"Word 文件不存在: {p}")
        if p.suffix.lower() != ".docx":
            raise RuntimeError("追加段落目前只支持普通 .docx；旧格式请先另存为 .docx")
        if not isinstance(paragraphs, list) or not paragraphs or any(not isinstance(item, str) for item in paragraphs):
            raise ValueError("paragraphs 必须是非空字符串数组")
        _check_closed(client, p, word=True)
        validate_plain_ooxml(p)
        source = Document(str(p))
        for text in paragraphs:
            source.add_paragraph(text)
        backup = atomic_office_save(
            p, lambda temporary: source.save(temporary), overwrite=True,
            check=lambda: _check_closed(client, p, word=True),
        )
        verified = Document(str(p))
        try:
            actual = [item.text for item in verified.paragraphs]
        finally:
            del verified
        if actual[-len(paragraphs):] != paragraphs:
            raise RuntimeError(f"原 Word 文档保存后核验失败，不能报告完成: {p}")
        return f"已向原 Word 文档追加 {len(paragraphs)} 段并重新读取核验: {p}；原文件备份: {backup}"


    @registry.tool(
        name="word_insert_paragraphs",
        description=with_risk_confirmation(
            "把一组段落直接插入现有普通 .docx 原文档的指定正文位置并保存；保留原文档其余正文、表格和图片，"
            "保存后重新读取核验，不生成替代文档。"
        ),
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "现有 .docx 绝对路径"},
            "index": {"type": "integer", "minimum": 0, "description": "正文段落位置；0 表示文首，超过现有段落数表示文末"},
            "anchor_before": {"type": "string", "description": "精确匹配的正文段落，新内容插在其前；必须唯一"},
            "anchor_after": {"type": "string", "description": "精确匹配的正文段落，新内容插在其后；必须唯一"},
            "paragraphs": {"type": "array", "items": {"type": "string"}},
        }, "required": ["path", "paragraphs"], "anyOf": [
            {"required": ["index"]}, {"required": ["anchor_before"]}, {"required": ["anchor_after"]},
        ]}, side_effect=True, danger=True,
    )
    @_com_async
    def word_insert_paragraphs(path: str, paragraphs: list[str], index: int | None = None,
                               anchor_before: str | None = None, anchor_after: str | None = None) -> str:
        from docx import Document
        from backend.modules.office.office_files import atomic_office_save, validate_plain_ooxml
        p = _resolve_for_write(path)
        if not p.is_file():
            raise FileNotFoundError(f"Word 文件不存在: {p}")
        if p.suffix.lower() != ".docx":
            raise RuntimeError("插入段落目前只支持普通 .docx；旧格式请先另存为 .docx")
        selectors = sum((index is not None, anchor_before is not None, anchor_after is not None))
        if selectors != 1:
            raise ValueError("index、anchor_before、anchor_after 必须且只能指定一个")
        if index is not None and (isinstance(index, bool) or not isinstance(index, int) or index < 0):
            raise ValueError("index 必须是非负整数")
        if anchor_before is not None and (not isinstance(anchor_before, str) or not anchor_before.strip()):
            raise ValueError("anchor_before 必须是非空正文段落文字")
        if anchor_after is not None and (not isinstance(anchor_after, str) or not anchor_after.strip()):
            raise ValueError("anchor_after 必须是非空正文段落文字")
        if not isinstance(paragraphs, list) or not paragraphs or any(not isinstance(item, str) for item in paragraphs):
            raise ValueError("paragraphs 必须是非空字符串数组")
        _check_closed(client, p, word=True)
        validate_plain_ooxml(p)
        source = Document(str(p))
        if anchor_before is not None or anchor_after is not None:
            anchor = anchor_before or anchor_after
            matches = [i for i, paragraph in enumerate(source.paragraphs) if paragraph.text.strip() == anchor.strip()]
            if len(matches) != 1:
                return f"标题锚点匹配到 {len(matches)} 个正文段落；原 Word 文档未修改、未保存: {p}"
            index = matches[0] + (1 if anchor_after else 0)
        insertion_index = min(index, len(source.paragraphs))
        anchor = source.paragraphs[insertion_index] if insertion_index < len(source.paragraphs) else None
        if anchor is None:
            for text in paragraphs:
                source.add_paragraph(text)
        else:
            for text in paragraphs:
                anchor.insert_paragraph_before(text)
        backup = atomic_office_save(
            p, lambda temporary: source.save(temporary), overwrite=True,
            check=lambda: _check_closed(client, p, word=True),
        )
        verified = Document(str(p))
        actual = [item.text for item in verified.paragraphs]
        expected = paragraphs
        if actual[insertion_index:insertion_index + len(expected)] != expected:
            raise RuntimeError(f"原 Word 文档保存后核验失败，不能报告完成: {p}")
        return f"已向原 Word 文档第 {insertion_index} 段位置插入 {len(paragraphs)} 段并重新读取核验: {p}；原文件备份: {backup}"


    @registry.tool(
        name="word_append_blocks",
        description=with_risk_confirmation(
            "把章节、节标题、正文、条款、表格和图片按结构直接追加到现有普通 .docx 原文档末尾；"
            "复用项目 Word 排版规范，保存后重新读取核验，不生成替代文档。"
        ),
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "现有 .docx 绝对路径"},
            "blocks": {"type": "array", "items": {"type": "object", "description": (
                "type 可为 chapter、section、body、clause、table、figure、figure_caption；"
                "table 使用 caption、headers、rows；figure 使用 image_path 和 text"
            )}},
        }, "required": ["path", "blocks"]}, side_effect=True, danger=True,
    )
    @_com_async
    def word_append_blocks(path: str, blocks: list[dict]) -> str:
        from docx import Document
        from backend.modules.office.office_files import atomic_office_save, validate_plain_ooxml
        from backend.modules.office import word_style
        p = _resolve_for_write(path)
        if not p.is_file():
            raise FileNotFoundError(f"Word 文件不存在: {p}")
        if p.suffix.lower() != ".docx":
            raise RuntimeError("结构化追加目前只支持普通 .docx；旧格式请先另存为 .docx")
        if not isinstance(blocks, list) or not blocks or any(not isinstance(block, dict) for block in blocks):
            raise ValueError("blocks 必须是非空对象数组")
        _check_closed(client, p, word=True)
        validate_plain_ooxml(p)
        source = Document(str(p))

        def clean(value: object) -> str:
            from backend.core.text_normalize import normalize_cn_punctuation
            return normalize_cn_punctuation("" if value is None else str(value))

        def array(block: dict, name: str) -> list:
            value = block.get(name)
            if value is None:
                return []
            if not isinstance(value, list):
                raise ValueError(f"Word 结构化字段 {name} 必须是数组")
            return value

        expected_text = []
        for block in blocks:
            kind = str(block.get("type") or "body").lower()
            text = clean(block.get("text"))
            if text:
                expected_text.append(text)
            if kind == "chapter":
                word_style.add_chapter(source, text)
            elif kind == "section":
                word_style.add_section(source, text)
            elif kind == "clause":
                word_style.add_clause(source, text)
            elif kind == "table":
                headers = array(block, "headers")
                rows = array(block, "rows")
                if any(not isinstance(row, list) for row in rows):
                    raise ValueError("Word 表格字段 rows 必须是二维数组")
                word_style.add_table(
                    source,
                    [clean(item) for item in headers],
                    [[clean(item) for item in row] for row in rows],
                    caption=clean(block.get("caption")),
                )
                expected_text.extend(clean(item) for item in headers)
                expected_text.extend(clean(item) for row in rows for item in row)
            elif kind == "figure":
                image_path = _resolve(str(block.get("image_path") or block.get("path") or ""))
                if not image_path.is_file():
                    raise FileNotFoundError(f"图片不存在: {image_path}")
                from docx.enum.text import WD_ALIGN_PARAGRAPH
                from docx.image.image import Image
                from docx.shared import Cm
                info = Image.from_file(str(image_path))
                section = source.sections[-1]
                max_width = min(Cm(14.5), section.page_width - section.left_margin - section.right_margin)
                max_height = min(Cm(18), section.page_height - section.top_margin - section.bottom_margin)
                scale = min(1, max_width / info.width, max_height / info.height)
                paragraph = source.add_paragraph()
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                paragraph.add_run().add_picture(
                    str(image_path), width=int(info.width * scale), height=int(info.height * scale)
                )
                word_style.add_figure_caption(source, text or clean(block.get("caption")))
            elif kind == "figure_caption":
                word_style.add_figure_caption(source, text or clean(block.get("caption")))
            else:
                word_style.add_body(source, text)

        backup = atomic_office_save(
            p, lambda temporary: source.save(temporary), overwrite=True,
            check=lambda: _check_closed(client, p, word=True),
        )
        verified = read_word_file(p)
        missing = [text for text in expected_text if text and text not in verified]
        if missing:
            raise RuntimeError(f"原 Word 文档保存后核验缺少 {len(missing)} 项内容，不能报告完成: {p}")
        return f"已向原 Word 文档追加 {len(blocks)} 个结构块并重新读取核验: {p}；原文件备份: {backup}"


# 便于其他机器上开发时了解本模块暴露了哪些工具
EXPOSED_TOOLS = [
    "excel_read_range", "excel_write_cells", "excel_read_all", "excel_append_row",
    "word_read_text", "word_list_paragraphs", "word_find_replace", "excel_list_sheets", "ppt_read_text",
    "pdf_read_text", "pdf_list_pages", "pdf_extract_images", "pdf_render_pages",
    "word_apply_replacements", "word_append_paragraphs", "word_insert_paragraphs", "word_append_blocks",
]
