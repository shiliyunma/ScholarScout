"""Office 抽取器的口径测试：真的能从 zip+XML 里读出文字，抽不到如实报错。

测试自己造最小的 .docx/.xlsx/.pptx（就是几个 XML 打进一个 zip）—— 不依赖任何样本文件，
也不依赖外部库。这样这个测试在任何机器上都能跑，且"改坏了"会立刻红。
"""

from __future__ import annotations

import io
import zipfile

from scholarscout.fetch import office

DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>第一章 总论</w:t></w:r></w:p>
    <w:p><w:r><w:t>行内</w:t></w:r><w:r><w:t>分片也要拼起来</w:t></w:r></w:p>
    <w:p/>
  </w:body>
</w:document>
"""

SHARED_STRINGS = """<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <si><t>姓名</t></si>
  <si><t>分数</t></si>
  <si><t>张三</t></si>
</sst>
"""

SHEET = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>
    <row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>95</v></c></row>
    <row r="3"><c r="A3"/><c r="B3"/></row>
  </sheetData>
</worksheet>
"""

SLIDE_1 = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld><p:spTree><p:sp><p:txBody>
    <a:p><a:r><a:t>封面：ScholarScout</a:t></a:r></a:p>
  </p:txBody></p:sp></p:spTree></p:cSld>
</p:sld>
"""

SLIDE_2 = SLIDE_1.replace("封面：ScholarScout", "第二页：证据与结论")


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def test_docx_reads_paragraphs_and_joins_runs() -> None:
    ok, text, detail = office.docx_text(_zip({"word/document.xml": DOCUMENT_XML}))
    assert ok is True
    assert "第一章 总论" in text
    assert "行内分片也要拼起来" in text  # 同一段里分开的 w:t 要拼成一段
    assert "\n\n" not in text  # 空段落不进正文
    assert "段" in detail


def test_xlsx_reads_shared_strings_and_numbers() -> None:
    ok, text, _ = office.xlsx_text(
        _zip({"xl/sharedStrings.xml": SHARED_STRINGS, "xl/worksheets/sheet1.xml": SHEET})
    )
    assert ok is True
    assert "姓名\t分数" in text
    assert "张三\t95" in text
    assert "# sheet1.xml" in text


def test_pptx_reads_slides_in_order() -> None:
    ok, text, _ = office.pptx_text(
        _zip({"ppt/slides/slide1.xml": SLIDE_1, "ppt/slides/slide2.xml": SLIDE_2})
    )
    assert ok is True
    assert text.index("封面") < text.index("第二页")


def test_broken_file_reports_failure_instead_of_empty_text() -> None:
    """打不开就报"打不开"，不能返回空字符串假装提取成功。"""
    ok, text, detail = office.docx_text(b"this is not a zip at all")
    assert ok is False and text == ""
    assert "不是有效的" in detail
    ok, text, detail = office.xlsx_text(b"PK\x03\x04garbage")
    assert ok is False and text == "" and detail


def test_unknown_extension_is_not_guessed() -> None:
    ok, _text, detail = office.office_text("doc", b"whatever")
    assert ok is False and "不支持" in detail


def test_empty_document_is_ok_but_empty() -> None:
    """内容为空的文档：ok=True 但 text 为空 —— 这是"文档确实没字"，不是"读失败"。"""
    ok, text, _ = office.docx_text(
        _zip(
            {
                "word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:body/></w:document>'
            }
        )
    )
    assert ok is True and text == ""
