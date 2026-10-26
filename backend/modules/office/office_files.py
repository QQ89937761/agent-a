"""Backed-up atomic Office output; never launch an application to save OOXML."""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path

from backend.core import isolation_workspace


def atomic_office_save(path: Path, save, *, overwrite: bool = False, check=None) -> Path | None:
    if path.exists() and not overwrite:
        raise ValueError(f"目标文件已存在，为避免覆盖未写入: {path}")
    if check:
        check()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".xiaoa-office-", suffix=path.suffix, dir=path.parent)
    os.close(fd)
    temp = Path(temporary)
    backup = None
    try:
        save(str(temp))  # serialization failure must leave the original untouched
        if check:
            check()
        if path.exists() and not overwrite:
            raise ValueError(f"目标文件已存在，为避免覆盖未写入: {path}")
        if overwrite:
            if path.exists():
                folder = isolation_workspace.root() / "backups" / "office"
                folder.mkdir(parents=True, exist_ok=True)
                backup = folder / f"{path.stem}-{uuid.uuid4().hex}{path.suffix}"
                shutil.copy2(path, backup)
            os.replace(temp, path)
        else:
            # Atomic no-clobber publish, including a concurrently-created destination.
            os.link(temp, path)
        return backup
    finally:
        temp.unlink(missing_ok=True)


def validate_plain_ooxml(path: Path) -> None:
    """Fail closed for packages our editors cannot reliably round-trip.

    No COM fallback: a misleading extension must not change the authorized route.
    """
    from zipfile import ZipFile
    from xml.etree import ElementTree as ET
    from backend.modules.office.excel_formulas import _formula_needs_confirmation

    if not path.exists():
        return
    with ZipFile(path) as archive:
        for name in archive.namelist():
            lower = name.lower()
            if any(part in lower for part in ("vbaproject", "externallinks/", "activex/", "connections.xml")):
                raise ValueError("文件含宏、外链或嵌入对象，未修改；请另存为普通无宏文档后重试")
            if lower.startswith("_xmlsignatures/") or (path.suffix.lower() == ".xlsx" and
                    any(part in lower for part in ("drawings/", "embeddings/", "slicers/", "pivot", "querytables/", "threadedcomments/", "customxml/"))):
                raise ValueError("文件含签名或复杂对象，无法保证原样保留，未修改；请先另存简化副本")
            if lower == "[content_types].xml" and b"macroenabled" in archive.read(name).lower():
                raise ValueError("文件内容为宏格式，与普通 OOXML 扩展名不符，未修改")
            if path.suffix.lower() == ".xlsx" and lower.endswith(".xml") and lower.startswith("xl/"):
                root = ET.fromstring(archive.read(name))
                for node in root.iter():
                    kind = node.tag.rsplit("}", 1)[-1]
                    if kind == "f" and node.get("t") not in {None, "normal", "shared"}:
                        raise ValueError("文件含特殊公式，无法安全保真修改；请先另存简化副本")
                    # Names include print areas/titles and AutoFilter; shared followers
                    # have no text. openpyxl expands shared formulas on load.
                    if kind in {"f", "definedName"} and node.text and _formula_needs_confirmation("=" + node.text):
                        raise ValueError("文件含外部或未知公式，未修改；请先在表格应用中确认并移除风险公式")


def save_excel_preserving_names(path: Path, workbook, temporary: str) -> None:
    """Cell edits must not normalize/drop WPS print areas or reserved names."""
    from io import BytesIO
    from zipfile import ZipFile
    from lxml import etree as ET

    if not path.exists():
        workbook.save(temporary)
        return
    generated = BytesIO()
    workbook.save(generated)
    parser = ET.XMLParser(resolve_entities=False, no_network=True)
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with ZipFile(path) as original, ZipFile(generated) as source, ZipFile(temporary, "w") as output:
        old = ET.fromstring(original.read("xl/workbook.xml"), parser).find(ns + "definedNames")
        for info in source.infolist():
            content = source.read(info)
            if info.filename == "xl/workbook.xml" and old is not None:
                root = ET.fromstring(content, parser)
                current = root.find(ns + "definedNames")
                if current is not None:
                    root.replace(current, old)
                else:
                    # definedNames belongs before calcPr in the workbook schema.
                    calc = root.find(ns + "calcPr")
                    root.insert(root.index(calc) if calc is not None else len(root), old)
                content = ET.tostring(root)
            output.writestr(info, content)
