# python-docx Format Specification

Reference for Chinese academic report formatting in .docx.

## Page Setup (A4)

| Property | Value | python-docx |
|----------|-------|-------------|
| Page width | 21.0 cm (A4) | `section.page_width = Mm(210)` |
| Page height | 29.7 cm | `section.page_height = Mm(297)` |
| Top margin | 2.54 cm | `section.top_margin = Cm(2.54)` |
| Bottom margin | 2.54 cm | `section.bottom_margin = Cm(2.54)` |
| Left margin | 3.17 cm | `section.left_margin = Cm(3.17)` |
| Right margin | 3.17 cm | `section.right_margin = Cm(3.17)` |

## Font Size Mapping

| 字号 | English | Point | pt value | python-docx |
|------|---------|-------|----------|-------------|
| 三号 | 16pt | 16 | 16 | `Pt(16)` |
| 四号 | 14pt | 14 | 14 | `Pt(14)` |
| 小四 | 12pt | 12 | 12 | `Pt(12)` |
| 五号 | 10.5pt | 10.5 | 11 (rounded) | `Pt(10.5)` |

## Style Mapping

| Element | Font CN | Font EN | Size | Bold | Align | Spacing |
|---------|---------|---------|------|------|-------|---------|
| 封面标题 | 宋体 | Times New Roman | 16pt | Yes | Center | Before=0, After=0 |
| 一级标题 (#) | 宋体 | Times New Roman | 14pt | Yes | Left | Before=6pt, After=3pt |
| 二级标题 (##) | 宋体 | Times New Roman | 13pt | Yes | Left | Before=6pt, After=3pt |
| 正文 | 宋体 | Times New Roman | 12pt | No | Justify | Line=1.5, Indent=0.74cm |
| 代码/TNR | 宋体 | Courier New | 10.5pt | No | Left | Single |
| 表格表头 | 宋体 | Times New Roman | 10.5pt | Yes | Center | Single |
| 表格正文 | 宋体 | Times New Roman | 10.5pt | No | Left | Single |
| 图表标题 | 宋体 | Times New Roman | 10.5pt | No | Center | Before=3pt |

## Dual Font Implementation

```python
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

def set_run_font(run, font_cn='宋体', font_en='Times New Roman', size=12, bold=False):
    """Set both Chinese and Western font on a run."""
    run.font.name = font_en
    run.font.size = Pt(size)
    run.bold = bold
    rpr = run._element.get_or_add_rPr()
    # East-Asian font
    ea = rpr.find(qn('w:rFonts'))
    if ea is None:
        ea = OxmlElement('w:rFonts')
        rpr.insert(0, ea)
    ea.set(qn('w:eastAsia'), font_cn)
```

## Line Spacing

```python
from docx.shared import Pt
from docx.enum.text import WD_LINE_SPACING

pf = paragraph.paragraph_format
pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
pf.line_spacing = 1.5
```

## Table Grid Style

```python
table = doc.add_table(rows=1, cols=4)
table.style = 'Table Grid'
# Header row: bold + center
for cell in table.rows[0].cells:
    for p in cell.paragraphs:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for run in p.runs:
            run.bold = True
```

## Image Handling

```python
from docx.shared import Cm
# Insert with fixed width (height auto-scales)
doc.add_picture('path/to/image.png', width=Cm(15))
# Insert with both dimensions
doc.add_picture('path/to/image.png', width=Cm(12), height=Cm(8))

# Add caption below image
cap = doc.add_paragraph('图X: 标题')
cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
```

## Page Break

```python
doc.add_page_break()
```
