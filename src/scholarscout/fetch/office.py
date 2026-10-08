"""Office 文档（.docx/.xlsx/.pptx）抽正文 —— 只用标准库解 zip + XML，不引新依赖。

为什么自己做：这三种格式本质就是 zip 里的 XML，抽出**文字**只要几十行；引 python-docx /
openpyxl / python-pptx 会往运行期依赖白名单里塞三个大包（还连带 lxml 等），而产品要的只是
正文文本 —— 样式、公式、图表、批注一概不需要。

口径：抽不到就如实说抽不到（返回 ok=False + 原因），**不返回空字符串冒充"提取成功"** ——
"这份 xlsx 里没有文字" 和 "这个文件根本打不开" 是两件不同的事，调用方要能区分。
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

#: 认识这三种（扩展名 → 抽取器）
OFFICE_EXTENSIONS = ("docx", "xlsx", "pptx")

_SHEET_RE = re.compile(r"^xl/worksheets/sheet\d+\.xml$")
_SLIDE_RE = re.compile(r"^ppt/slides/slide\d+\.xml$")


def _open(content: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(content))


def _slide_index(name: str) -> int:
    match = re.search(r"(\d+)\.xml$", name)
    return int(match.group(1)) if match else 0


def docx_text(content: bytes) -> tuple[bool, str, str]:
    """Word：按段落抽字（表格单元格里的文字也在 w:t 里，同样会被带出来）。"""
    try:
        with _open(content) as archive:
            xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError, OSError, ValueError) as exc:
        return False, "", f"不是有效的 .docx（{type(exc).__name__}）"
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        return False, "", f"word/document.xml 解析失败：{exc}"
    lines: list[str] = []
    for paragraph in root.iter(f"{_W}p"):
        chunk = "".join(node.text or "" for node in paragraph.iter(f"{_W}t"))
        if chunk.strip():
            lines.append(chunk.strip())
    return True, "\n".join(lines), f"docx：{len(lines)} 段"


def xlsx_text(content: bytes) -> tuple[bool, str, str]:
    """Excel：共享字符串表 + 每张工作表，按行输出（单元格用制表符分隔），只保留非空行。"""
    try:
        with _open(content) as archive:
            names = archive.namelist()
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in root.iter(f"{_S}si"):
                    shared.append("".join(node.text or "" for node in item.iter(f"{_S}t")))
            sheets = sorted((n for n in names if _SHEET_RE.match(n)), key=_slide_index)
            lines: list[str] = []
            for name in sheets:
                lines.append(f"# {name.rsplit('/', 1)[-1]}")
                root = ET.fromstring(archive.read(name))
                for row in root.iter(f"{_S}row"):
                    cells: list[str] = []
                    for cell in row.iter(f"{_S}c"):
                        value = ""
                        inline = cell.find(f"{_S}is")
                        raw = cell.find(f"{_S}v")
                        if cell.get("t") == "s" and raw is not None and raw.text:
                            try:
                                value = shared[int(raw.text)]
                            except (ValueError, IndexError):
                                value = raw.text
                        elif inline is not None:
                            value = "".join(node.text or "" for node in inline.iter(f"{_S}t"))
                        elif raw is not None:
                            value = raw.text or ""
                        cells.append(value.replace("\n", " "))
                    line = "\t".join(cells).rstrip()
                    if line.strip():
                        lines.append(line)
    except (zipfile.BadZipFile, KeyError, OSError, ValueError) as exc:
        return False, "", f"不是有效的 .xlsx（{type(exc).__name__}）"
    except ET.ParseError as exc:
        return False, "", f"工作表解析失败：{exc}"
    return True, "\n".join(lines), f"xlsx：{len(sheets)} 张表"


def pptx_text(content: bytes) -> tuple[bool, str, str]:
    """PowerPoint：按幻灯片序号抽文字框内容。"""
    try:
        with _open(content) as archive:
            slides = sorted((n for n in archive.namelist() if _SLIDE_RE.match(n)), key=_slide_index)
            lines: list[str] = []
            for name in slides:
                root = ET.fromstring(archive.read(name))
                lines.append(f"# 第 {_slide_index(name)} 页")
                for paragraph in root.iter(f"{_A}p"):
                    chunk = "".join(node.text or "" for node in paragraph.iter(f"{_A}t"))
                    if chunk.strip():
                        lines.append(chunk.strip())
    except (zipfile.BadZipFile, KeyError, OSError, ValueError) as exc:
        return False, "", f"不是有效的 .pptx（{type(exc).__name__}）"
    except ET.ParseError as exc:
        return False, "", f"幻灯片解析失败：{exc}"
    return True, "\n".join(lines), f"pptx：{len(slides)} 页"


def office_text(extension: str, content: bytes) -> tuple[bool, str, str]:
    """按扩展名分发。不认识的扩展名如实返回失败，不猜。"""
    handlers = {"docx": docx_text, "xlsx": xlsx_text, "pptx": pptx_text}
    handler = handlers.get(extension.lower().lstrip("."))
    if handler is None:
        return False, "", f"不支持的 Office 扩展名：{extension}"
    return handler(content)
