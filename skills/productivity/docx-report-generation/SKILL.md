---
name: docx-report-generation
description: "Generate formatted Word (.docx) reports from Markdown with embedded diagrams (PlantUML) and data charts (matplotlib). Convert old .doc templates to .docx for format extraction."
version: 1.0.0
author: auto-generated
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [docx, word, report, plantuml, diagram, python-docx, writing]
    related_skills: [plan, architecture-diagram]
---

# docx-report-generation

Generate formatted Word documents from structured Markdown, with embedded PlantUML diagrams and data-driven matplotlib charts.

## Prerequisites

```
pip install python-docx           # .docx read/write
pip install matplotlib numpy      # data charts (optional)
# PlantUML JAR (17MB, one-time download):
mkdir -p ~/plantuml
curl -LO https://repo1.maven.org/maven2/net/sourceforge/plantuml/plantuml-mit/1.2026.5/plantuml-mit-1.2026.5.jar
ln -sf plantuml-mit-1.2026.5.jar ~/plantuml/plantuml.jar
java -jar ~/plantuml/plantuml.jar -version   # verify
# On Windows: win32com (for .doc→.docx conversion)
pip install pywin32
```

## Workflow Overview

```
┌─────────────────────┐     ┌──────────────────────┐
│  源文件: *.md         │────>│  python-docx 构建脚本 │
│  - 章节标题 & 正文    │     │  - 读取模板格式        │
│  - PlantUML 源码块   │     │  - 渲染 PlantUML→PNG  │
│  - 数据表格           │     │  - 生成 matplotlib 图  │
│  - 引用已有图片路径   │     │  - 插入图片 & 建表     │
└─────────────────────┘     │  - 输出 .docx          │
                            └──────────────────────┘
```

## Step-by-Step

### 1. Extract template formatting (if using a .doc template)

On Windows only — uses Word COM automation:

```python
import win32com.client
word = win32com.client.Dispatch('Word.Application')
word.Visible = False
doc = word.Documents.Open(r'D:\path\template.doc')
doc.SaveAs(r'D:\path\template_converted.docx', FileFormat=16)
doc.Close()
word.Quit()
```

Then read the converted .docx with python-docx to extract styles, headers, margins.

### 2. Render PlantUML to PNG

```python
import subprocess, os
PLANTUML_JAR = os.path.expanduser('~/plantuml/plantuml.jar')

def render_plantuml(puml_source: str, output_path: str):
    """Render PlantUML source string to PNG file."""
    proc = subprocess.run(
        ['java', '-jar', PLANTUML_JAR, '-tpng', '-pipe', '-charset', 'UTF-8'],
        input=puml_source.encode('utf-8'),
        capture_output=True, timeout=30
    )
    with open(output_path, 'wb') as f:
        f.write(proc.stdout)
```

### 3. Markdown → docx building rules

| Element | Format | python-docx |
|---------|--------|-------------|
| 一级标题 `#` | 宋体 14pt 加粗 段前6pt | `Heading 1` style |
| 二级标题 `##` | 宋体 13pt 加粗 | `Heading 2` style |
| 正文 | 宋体/TNR 12pt 1.5行距 首行缩进0.74cm | `Normal` + run settings |
| 代码块 | TNR 10.5pt 单倍行距 | `Normal` + monospace run |
| 表格 | Table Grid 表头加粗居中 | `doc.add_table()` |
| 图表标题 | 宋体 10.5pt 居中 | `Normal` + center align |
| 引用块 `>` | 楷体 左缩进1.48cm | `Normal` + italic + indent |

### 4. Python-docx formatting patterns

**Dual font (Chinese + English):**
```python
from docx.oxml.ns import qn
run = paragraph.add_run('中文 English')
run.font.name = 'Times New Roman'      # Western font
rpr = run._element.get_or_add_rPr()
ea = rpr.find(qn('w:rFonts'))
if ea is None:
    ea = OxmlElement('w:rFonts')
    rpr.insert(0, ea)
ea.set(qn('w:eastAsia'), '宋体')       # East Asian font
```

**First-line indent:**
```python
from docx.shared import Cm
pf = paragraph.paragraph_format
pf.first_line_indent = Cm(0.74)
```

**Spacing instead of blank paragraphs:**
```python
pf.space_before = Pt(6)
pf.space_after = Pt(3)
```

### 5. Insert existing images (chart_results/*.png)

```python
doc.add_picture('chart_results/01_各参数折线图.png', width=Cm(15))
# Add caption below
cap = doc.add_paragraph('图1: 各参数折线图')
cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
```

### 6. Build data tables from CSV

```python
from csv import DictReader
with open('data.csv') as f:
    reader = DictReader(f)
    table = doc.add_table(rows=1, cols=len(reader.fieldnames))
    table.style = 'Table Grid'
    # header row
    hdr = table.rows[0]
    for i, name in enumerate(reader.fieldnames):
        hdr.cells[i].text = name
    # data rows
    for row in reader:
        cells = table.add_row().cells
        for i, name in enumerate(reader.fieldnames):
            cells[i].text = row[name]
```

## Common Pitfalls

1. **python-docx CANNOT read .doc** (old Word 97-2003 format). Use win32com to convert first, or start from scratch.
2. **Chinese fonts need XML-level `w:eastAsia` attribute.** Setting `run.font.name` alone only sets Western font.
3. **No empty paragraphs for spacing.** Use `space_before`/`space_after` instead.
4. **PlantUML output may be empty on error.** Always check subprocess return code and file size.
5. **matplotlib Chinese glyphs.** Some fonts (SimHei) lack Unicode minus (U+2212). Set `plt.rcParams['axes.unicode_minus'] = False`.
6. **Hermes venv Python has NO pip (Windows).** `which python` points at the Hermes venv (`.../hermes-agent/venv/Scripts/python`), where `python -m pip install ...` fails with "No module named pip" — and installing into Hermes's own venv is the wrong move anyway. **Run dependency-needing scripts with a Python that already has the package, or through uv — but see pitfall 14 first if the project path contains non-ASCII characters.**
   ```bash
   uv run --with python-docx python build_docx.py            # docx
   uv run --with matplotlib --with numpy python charts.py    # charts
   uv run --with pywin32 python doc2docx.py                  # .doc -> .docx via Word COM
   ```
   Verify before blaming your code: `uv run --with python-docx python -c "import docx; print('OK')"`. Use bare `python script.py` only for stdlib-only scripts. If uv is missing, fall back to a system Python that already has the package (`py -3.12 -c "import docx"`).
6. **Process output buffering in background tasks.** Don't rely on terminal preview for progress — check actual output files.
7. **Excel/CSV data sorting by string not number.** Values like "32m" sort after "128m" lexicographically. Parse to numeric first.
8. **python-docx 默认页面是 US Letter，不是 A4**（21.59×27.94cm）。Letter 比 A4 **矮 1.76cm** —— 在 Letter 上调好的排版换到 A4 会突然溢出；反过来会因为 Letter 更矮而**误以为必须用更小的字号**。先显式设 `sec.page_width/page_height = Cm(21.0)/Cm(29.7)` 再调字号，否则整轮试参白做。
9. **`cell.width` 单独设了没用**，LibreOffice/Word 会忽略它、按内容自动重排列宽，症状是**文字在奇怪的位置折行**。必须固定表格布局 + 显式列宽：`tbl.autofit = False`（python-docx 会写入 `tblLayout=fixed`），再把 `w:tblGrid` 下每个 `w:gridCol` 的 `w:w` 写成 twips（1cm = 567）。**先设列宽再合并单元格**，且合并范围要覆盖所有内容列。
10. **固定行距（`WD_LINE_SPACING.EXACTLY`）会把内嵌图片纵向裁掉**，只剩上半截。含图片的段落必须用 `WD_LINE_SPACING.SINGLE`，让行框自己长到图片高度。
11. **`space_before` 不接受负值**（`ValueError: value must be in range 0 to …`）。想把图片放到文字右侧，别用负偏移，改成**多开一列表格**让图片自然落在右侧单元格。
12. **LibreOffice `-env:UserInstallation` 的 URL 必须用正斜杠**（`file:///C:/Users/...`）。写成 native 反斜杠路径会**静默不产出 PDF** —— 命令返回 0，输出目录里却什么都没有。另备一条不带 `-env` 的 fallback，并在每次转换前清掉输出目录里旧的 `*.pdf`，否则会读到上一次的结果。
13. **含非 ASCII 路径的项目里用 `uv run` 会炸掉项目 venv**：不带 `--no-project` 时 uv 把当前项目当包做 editable 安装，写入含中文路径的 `.pth`；此后 `site.py` 用 GBK 读它 → **解释器启动即崩**（`UnicodeDecodeError: 'gbk' codec ...` in `site.py addpackage`），连 `python -c "print(1)"` 都跑不了。修复：删掉 `site-packages/_editable_impl_*.pth` 与 `__editable__*`/`*.dist-info`。要跑带依赖的脚本，**用系统里已装了该包的 Python**（先验证 `py -3.12 -c "import docx"`），并从项目目录外运行。
14. **分页用“段前分页”属性**（`paragraph_format.page_break_before = True`），**不要插分页符**（`doc.add_page_break()`）：内容恰好占满一页时，显式分页符会多出一张空白页（实测 Abstract 后出现过）。
15. **Word 内置 Heading 样式默认是蓝色**，学位论文要黑色 → 在 run 级显式写 `run.font.color.rgb = RGBColor(0,0,0)`，否则标题是蓝色（设了 `style='Heading 1'` 就会被带着走）。
16. **目录用 TOC 域**（`w:fldChar` + `w:instrText` = `TOC \o "1-3" \h \z \u`）：python-docx 不会算页码，Word 里 Ctrl+A → F9 生成；LibreOffice 转 PDF 时是否展开**不稳定**，别拿它当验收依据，要在交付说明里写清。
17. **ASCII 框线图**（`┌─┐│`）要单独跑一个段落：等宽字体、不缩进、约 8.5pt；行宽 ~70 字符在 A4（正文宽 15.5cm）内不会折行。
18. **给协作者看的工作笔记会漏进成品**：markdown 里那种“写作纪律/写作说明”引用块要在渲染时按内容标记过滤，且**不能只查首个块**（章文件的首块通常是标题）。

## Related Files

- `references/python-docx-format-spec.md` — detailed format specification table
