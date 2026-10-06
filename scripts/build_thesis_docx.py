"""把 docs/*.md 按常规学位论文格式拼成一篇 .docx。

用法（用 uv 跑，避免往项目 venv 里塞新依赖）：
    uv run --with python-docx python run/build_thesis_docx.py
输出：ScholarScout-毕业论文.docx（项目根，已 gitignore）

格式约定（常规中文学位论文）：
  A4；上下 2.5cm / 左 3.0cm / 右 2.5cm；正文宋体小四(12pt) 1.5 倍行距 首行缩进 0.74cm；
  章标题黑体三号居中（另起一页）、节标题黑体四号、小节黑体小四；
  表格用三线表（五号 10.5pt，固定列宽，不缩进）；代码/ASCII 图用等宽 8.5pt；
  页脚居中页码；目录用 Word 域（打开后 Ctrl+A → F9 更新）。

本文件是"构建脚本"不是"论文内容"：内容永远以 docs/*.md 为准，改内容不要改这里。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OUT = ROOT / "ScholarScout-毕业论文.docx"

BODY_FONT_EA = "宋体"
HEAD_FONT_EA = "黑体"
MONO_FONT = "Consolas"
WEST_FONT = "Times New Roman"
TABLE_FONT_PT = 10.5  # 五号
BODY_PT = 12  # 小四
INDENT_CM = 0.74  # 两个字符（12pt 正文）

#: 章节顺序：论文里的章 → 对应的 markdown
CHAPTERS: list[tuple[str, Path]] = [
    ("第 1 章 绪论", DOCS / "09-论文第1章-绪论.md"),
    ("第 2 章 相关技术与文献综述", DOCS / "11-论文第2章-相关技术与文献综述.md"),
    ("第 3 章 系统设计", DOCS / "07-论文第3章-系统设计.md"),
    ("第 4 章 系统实现", DOCS / "08-论文第4章-系统实现.md"),
    ("第 5 章 实验与评估", DOCS / "06-论文第5章-实验与评估.md"),
    ("第 6 章 总结与展望", DOCS / "10-论文第6章-总结与展望.md"),
]

#: 工作笔记式的开头引用块不进论文（它们是给协作者看的，不是论文正文）
META_MARKERS = ("写作纪律", "写作说明", "本章数字全部来自", "写作约定")


# --------------------------------------------------------------------- 字体工具
def set_run_font(
    run,
    *,
    ea: str = BODY_FONT_EA,
    size: float = BODY_PT,
    bold: bool = False,
    mono: bool = False,
    color: str | None = None,
) -> None:
    """中英文字体要分别设：只设 `run.font.name` 只管西文（坑）。"""
    run.font.size = Pt(size)
    run.bold = bold
    run.font.name = MONO_FONT if mono else WEST_FONT
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.insert(0, fonts)
    fonts.set(qn("w:eastAsia"), "Consolas" if mono else ea)
    if color:
        run.font.color.rgb = RGBColor.from_string(color)


INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`)")


def add_inline(
    paragraph, text: str, *, ea: str = BODY_FONT_EA, size: float = BODY_PT, base_bold: bool = False
) -> None:
    """把 `**粗**` 与 `` `代码` `` 落成不同 run，其余按正文字体。"""
    for chunk in INLINE.split(text):
        if not chunk:
            continue
        if chunk.startswith("**") and chunk.endswith("**"):
            set_run_font(paragraph.add_run(chunk[2:-2]), ea=ea, size=size, bold=True)
        elif chunk.startswith("`") and chunk.endswith("`"):
            set_run_font(paragraph.add_run(chunk[1:-1]), size=size - 1.0, mono=True)
        else:
            set_run_font(paragraph.add_run(chunk), ea=ea, size=size, bold=base_bold)


def body_paragraph(doc, text: str, *, indent: bool = True):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
    pf.space_before = Pt(0)
    pf.space_after = Pt(3)
    if indent:
        pf.first_line_indent = Cm(INDENT_CM)
    add_inline(p, text)
    return p


def heading(doc, text: str, level: int, *, new_page: bool = False):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.SINGLE
    if new_page:
        # 用"段前分页"而不是插入分页符：内容刚好占满整页时，
        # 显式分页符会多出一张空白页（实测 Abstract 后出现过空白页）。
        pf.page_break_before = True
    if level == 1:
        pf.space_before = Pt(0)
        pf.space_after = Pt(18)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        add_inline(p, text, ea=HEAD_FONT_EA, size=16, base_bold=True)
    elif level == 2:
        pf.space_before = Pt(12)
        pf.space_after = Pt(6)
        add_inline(p, text, ea=HEAD_FONT_EA, size=14, base_bold=True)
    else:
        pf.space_before = Pt(8)
        pf.space_after = Pt(4)
        add_inline(p, text, ea=HEAD_FONT_EA, size=12, base_bold=True)
    # 让 Word 的目录域能认出这些标题
    p.style = doc.styles[
        "Heading 1" if level == 1 else ("Heading 2" if level == 2 else "Heading 3")
    ]
    for run in p.runs:
        run.font.name = WEST_FONT
        # Word 内置标题样式默认是蓝色，学位论文要用黑色（显式设 run 级颜色覆盖样式）
        run.font.color.rgb = RGBColor(0, 0, 0)
    return p


# --------------------------------------------------------------------- 三线表
def _border(element, tag: str, size: int, val: str = "single") -> None:
    node = OxmlElement(f"w:{tag}")
    node.set(qn("w:val"), val)
    node.set(qn("w:sz"), str(size))
    node.set(qn("w:space"), "0")
    node.set(qn("w:color"), "000000")
    element.append(node)


def three_line_table(table) -> None:
    """三线表：顶线与底线粗、表头下细线，无竖线。"""
    tbl = table._tbl
    pr = tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    _border(borders, "top", 12)
    _border(borders, "bottom", 12)
    _border(borders, "left", 0, "none")
    _border(borders, "right", 0, "none")
    _border(borders, "insideH", 0, "none")
    _border(borders, "insideV", 0, "none")
    pr.append(borders)
    for cell in table.rows[0].cells:
        tc_pr = cell._tc.get_or_add_tcPr()
        cell_borders = OxmlElement("w:tcBorders")
        _border(cell_borders, "bottom", 6)
        tc_pr.append(cell_borders)


def set_col_widths(table, widths_cm: list[float]) -> None:
    """固定布局 + 显式列宽（只设 cell.width 会被忽略）。"""
    table.autofit = False
    for row in table.rows:
        for index, width in enumerate(widths_cm):
            if index < len(row.cells):
                row.cells[index].width = Cm(width)


def add_table(doc, rows: list[list[str]]) -> None:
    cols = max(len(row) for row in rows)
    table = doc.add_table(rows=0, cols=cols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for r_index, row in enumerate(rows):
        cells = table.add_row().cells
        for c_index in range(cols):
            text = row[c_index] if c_index < len(row) else ""
            cell = cells[c_index]
            cell.text = ""
            p = cell.paragraphs[0]
            pf = p.paragraph_format
            pf.line_spacing_rule = WD_LINE_SPACING.SINGLE
            pf.space_before = Pt(1)
            pf.space_after = Pt(1)
            add_inline(p, text, size=TABLE_FONT_PT, base_bold=(r_index == 0))
            if r_index == 0:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    three_line_table(table)
    # 列宽按内容字符数粗分，总宽 15.5cm（A4 减去左右页边距）
    weights = []
    for c_index in range(cols):
        longest = max((len(row[c_index]) if c_index < len(row) else 0) for row in rows)
        weights.append(max(longest, 6))
    total = sum(weights)
    set_col_widths(table, [15.5 * w / total for w in weights])
    doc.add_paragraph().paragraph_format.space_after = Pt(3)


# --------------------------------------------------------------------- Markdown
def strip_meta_quote(blocks: list[list[str]]) -> list[list[str]]:
    """去掉"给协作者看的"引用块（论文正文里不该出现）。

    注意不能只看第一个块：章文件的第一块是章标题，元笔记是**第二块** ——
    早期版本只查 blocks[0]，结果元笔记整段漏进了 docx。
    """
    return [
        block
        for block in blocks
        if not (
            block and block[0].startswith(">") and any(m in "".join(block) for m in META_MARKERS)
        )
    ]


def split_blocks(markdown: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    in_code = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            current.append(line)
            if not in_code:
                blocks.append(current)
                current = []
            continue
        if in_code:
            current.append(line)
            continue
        if not line.strip():
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)
    return blocks


def render_markdown(doc, markdown: str, *, first_chapter: bool) -> None:
    blocks = strip_meta_quote(split_blocks(markdown))
    for _block_index, block in enumerate(blocks):
        head = block[0]
        if head.startswith("```"):
            text = "\n".join(block[1:-1] if block[-1].startswith("```") else block[1:])
            p = doc.add_paragraph()
            pf = p.paragraph_format
            pf.line_spacing_rule = WD_LINE_SPACING.SINGLE
            pf.space_before = Pt(6)
            pf.space_after = Pt(6)
            pf.left_indent = Cm(0.4)
            for i, line in enumerate(text.splitlines()):
                run = p.add_run(("\n" if i else "") + line)
                set_run_font(run, size=8.5, mono=True)
            continue
        if head.startswith("|"):
            rows: list[list[str]] = []
            for line in block:
                if re.match(r"^\|[\s:\-|]+\|$", line):
                    continue  # 分隔行
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                rows.append(cells)
            if rows:
                add_table(doc, rows)
            continue
        if head.startswith("#"):
            level = len(head) - len(head.lstrip("#"))
            text = head.lstrip("#").strip()
            if level == 1:
                # 每章都必须新起一页；第一章也不例外（目录页已不再带尾部分页符）
                heading(doc, text, 1, new_page=True)
            else:
                heading(doc, text, min(level, 3))
            continue
        if head.startswith(">"):
            text = " ".join(line.lstrip("> ").strip() for line in block)
            p = doc.add_paragraph()
            pf = p.paragraph_format
            pf.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
            pf.left_indent = Cm(0.74)
            pf.space_before = Pt(3)
            pf.space_after = Pt(6)
            add_inline(p, text, ea="楷体", size=BODY_PT - 0.5)
            continue
        if head.startswith("---"):
            continue
        if re.match(r"^([-*]|\d+\.)\s+", head):
            for line in block:
                text = re.sub(r"^([-*]|\d+\.)\s+", "", line).strip()
                p = doc.add_paragraph(
                    style="List Bullet" if not line[0].isdigit() else "List Number"
                )
                pf = p.paragraph_format
                pf.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
                pf.space_after = Pt(2)
                add_inline(p, text)
            continue
        body_paragraph(doc, " ".join(line.strip() for line in block))


# --------------------------------------------------------------------- 页面与页码
def setup_page(section) -> None:
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.5)
    section.bottom_margin = Cm(2.5)
    section.left_margin = Cm(3.0)
    section.right_margin = Cm(2.5)


def add_page_number_footer(section) -> None:
    p = section.footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instr)
    run._r.append(end)
    set_run_font(run, size=10.5)


def add_toc_field(doc) -> None:
    p = doc.add_paragraph()
    run = p.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = 'TOC \\o "1-3" \\h \\z \\u'
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    placeholder = OxmlElement("w:t")
    placeholder.text = "【目录：在 Word 中 Ctrl+A 后按 F9 生成】"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for node in (begin, instr, separate, placeholder, end):
        run._r.append(node)
    set_run_font(run, size=10.5, color="808080")


# --------------------------------------------------------------------- 前置页
def add_cover(doc) -> None:
    for _ in range(3):
        doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    add_inline(p, "本科毕业设计（论文）", ea=HEAD_FONT_EA, size=18, base_bold=True)
    for _ in range(2):
        doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    add_inline(
        p,
        "ScholarScout：面向开放世界学术调研的学习辅助智能体",
        ea=HEAD_FONT_EA,
        size=20,
        base_bold=True,
    )
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    add_inline(p, "——两级反幻觉度量与成本可控的桌面实现", size=14)
    for _ in range(4):
        doc.add_paragraph()
    items = [
        ("学院", "【待填】"),
        ("专业", "【待填】"),
        ("班级 / 学号", "【待填】"),
        ("姓名", "【待填】"),
        ("指导教师", "【待填】"),
        ("完成日期", "2026 年 10 月"),
    ]
    table = doc.add_table(rows=len(items), cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for row, (key, value) in zip(table.rows, items, strict=False):
        for cell, text, bold in ((row.cells[0], key + "：", False), (row.cells[1], value, False)):
            cell.text = ""
            p = cell.paragraphs[0]
            p.alignment = (
                WD_ALIGN_PARAGRAPH.CENTER if cell is row.cells[1] else WD_ALIGN_PARAGRAPH.RIGHT
            )
            p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
            add_inline(p, text, size=BODY_PT, base_bold=bold)
    set_col_widths(table, [4.0, 7.0])


def add_abstract_pages(doc) -> None:
    text = (DOCS / "12-论文摘要与关键词.md").read_text(encoding="utf-8")
    blocks = split_blocks(text)
    for block in blocks:
        head = block[0]
        if head.startswith("# "):
            continue
        if head.startswith("## "):
            title_text = head.lstrip("#").strip()
            if "中文" in title_text:
                heading(doc, "摘　要", 1, new_page=True)
            else:
                heading(doc, "Abstract", 1, new_page=True)
            continue
        joined = " ".join(line.strip() for line in block)
        p = body_paragraph(doc, joined)
        if joined.startswith("**关键词**") or joined.startswith("**Keywords**"):
            p.paragraph_format.first_line_indent = Cm(0)
            p.paragraph_format.space_before = Pt(12)


def add_toc_page(doc) -> None:
    heading(doc, "目　录", 1, new_page=True)
    add_toc_field(doc)


def add_references(doc) -> None:
    path = DOCS / "13-论文参考文献.md"
    if not path.exists():
        return
    heading(doc, "参考文献", 1, new_page=True)
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
        pf.space_after = Pt(2)
        pf.left_indent = Cm(0.85)
        pf.first_line_indent = Cm(-0.85)  # 悬挂缩进，参考文献常规排法
        add_inline(p, text)


def add_thanks(doc) -> None:
    heading(doc, "致　谢", 1, new_page=True)
    body_paragraph(
        doc,
        "【待填】此处致谢指导教师、同学与家人。本文的实验与代码在本机 Windows 环境下完成，"
        "感谢开源社区提供的 arXiv、OpenAlex、SQLite/FTS5、pywebview 等基础工具。",
    )


def main() -> int:
    missing = [str(path) for _, path in CHAPTERS if not path.exists()]
    if missing:
        print("缺少这些章节文件，先补齐再构建：")
        for item in missing:
            print("  - " + item)
        return 1

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = WEST_FONT
    style.font.size = Pt(BODY_PT)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT_EA)
    setup_page(doc.sections[0])
    add_page_number_footer(doc.sections[0])

    add_cover(doc)
    add_abstract_pages(doc)
    add_toc_page(doc)

    for index, (_title, path) in enumerate(CHAPTERS):
        markdown = path.read_text(encoding="utf-8")
        render_markdown(doc, markdown, first_chapter=(index == 0))

    add_references(doc)
    add_thanks(doc)

    doc.save(OUT)
    print(f"已生成：{OUT}")
    print(f"章节 {len(CHAPTERS)} 章；字数（近似）{len(doc.paragraphs)} 段")
    return 0


if __name__ == "__main__":
    sys.exit(main())
