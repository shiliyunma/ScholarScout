---
name: report-generation
description: "Generate formatted .docx academic/technical reports from markdown + PlantUML diagrams + Python data charts. Covers the full pipeline: design interview, PlantUML rendering, data chart generation, and python-docx building from .doc templates."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [report, docx, plantuml, academic, experimental, word]
    related_skills: [plan, architecture-diagram, design-md]
---

# Report Generation Skill

Generate formatted Word (.docx) reports for academic/technical/experimental writing. The pipeline is: **Markdown source** → **PlantUML diagrams** → **Python data charts** → **python-docx build from template**.

## When to Use

Use this skill when the user asks you to write a formatted report with:
- Structured sections (Background, Problem, Experiment, Analysis, Conclusion)
- Embedded diagrams (architecture, workflow, breakdown trees)
- Data-driven charts (bar charts, time series, comparisons from CSV data)
- Formatted Word output (.docx) with proper fonts and styles

## Prerequisites

```bash
# Check python-docx — NOTE: Hermes venv Python (3.11.5) may lack pip.
# If `pip3` not found, use the system Python at:
#   /c/Users/Lenovo/AppData/Local/Programs/Python/Python312/python.exe -m pip
# Or test directly:
#   python -c "import docx; print('OK')" 2>&1
#   /c/Users/Lenovo/AppData/Local/Programs/Python/Python312/python.exe -c "import docx; print('OK')" 2>&1
pip3 show python-docx 2>&1 | head -3

# Check Java (needed for PlantUML)
java -version 2>&1 | head -1

# Check PlantUML jar (download if missing)
PU_JAR="/c/Users/Lenovo/plantuml/plantuml-mit-1.2026.5.jar"
java -jar "$PU_JAR" -version 2>&1 || (
  mkdir -p "$(dirname "$PU_JAR")"
  curl -L -o "$PU_JAR" \
    "https://repo1.maven.org/maven2/net/sourceforge/plantuml/plantuml-mit/1.2026.5/plantuml-mit-1.2026.5.jar"
)
```

## Step-by-Step Pipeline

### Step 1: Requirements Gathering (Design Interview)

For complex reports with multiple artifacts, use the **Design Interview** pattern from the `plan` skill:
- One question at a time
- Walk the decision tree: format → template → scope → diagrams → data sources → output
- For each question, recommend an answer with reasoning
- Explore codebase/template/data before asking

Typical decision branches:
| Branch | Questions to ask |
|--------|-----------------|
| **Format** | .docx or .doc? Keep intermediate .md? |
| **Template** | Existing .doc template? Extract formatting? |
| **Scope** | Which data to include/exclude? Narrative arc? |
| **Diagrams** | PlantUML (architecture/flow) vs matplotlib (data-driven)? |
| **Data sources** | CSV files, sub-directories, existing charts? |
| **Output** | File name? Location? Overwrite or new? |

### Step 2: Write Markdown Source

Write the complete report in markdown first. Include:

```markdown
# Report Title

## 一、章节名

### 1.1 子章节

正文内容...

![图表标题](relative/path/to/image.png)
> 图1：图表描述
```

**Image reference convention:**
- `plantuml_output/` — for PlantUML-rendered diagrams
- `python_output/` — for Python-generated data charts  
- `chart_results/` — for existing charts

### Step 3: Render PlantUML Diagrams

Create `.puml` files in a `plantuml_src/` directory, then render to PNG.

**Key techniques:**

```bash
# Create puml file
write_file path="plantuml_src/diagram.puml"

# Render (CRITICAL: avoid Chinese path in input)
mkdir -p /tmp/plantuml_work
cp plantuml_src/*.puml /tmp/plantuml_work/
java -jar "$PU_JAR" -tpng -charset UTF-8 /tmp/plantuml_work/*.puml -o /tmp/plantuml_work/
cp /tmp/plantuml_work/*.png plantuml_output/
```

**Pitfalls:**
- ❌ PlantUML path with Chinese characters (`大三下/`) → garbled error
- ✅ Copy puml files to `/tmp/plantuml_work/` (no Chinese), render there, copy back
- ❌ `|#LightBlue|` pipe syntax in activity diagrams → error on line N
- ✅ Use `partition "name" { }` blocks instead of pipe tables
- ❌ `#` at line start inside activity diagram body → invalid syntax
- ✅ Use PlantUML comments with `'` (single quote) not `#`
- ❌ `note right:` placed AFTER a `}` closing a partition → parsing error
- ✅ Place notes BEFORE the `}` or use separate `note` blocks

### Step 4: Generate Python Data Charts

For data-driven charts (bar charts, time series, comparisons), use matplotlib:

```python
import matplotlib.pyplot as plt
import csv, os

# Read CSV data
with open('data.csv') as f:
    reader = csv.DictReader(f)
    data = list(reader)

# Generate chart
fig, ax = plt.subplots(figsize=(10, 6))
ax.bar(x, y)
plt.tight_layout()
plt.savefig('python_output/chart_name.png', dpi=150, bbox_inches='tight')
plt.close()
```

**Font settings for Chinese labels:**
```python
plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False
```

### Step 5: Build .docx from Template

Use python-docx to convert markdown content to formatted Word document.

**Template handling on Windows (.doc format):**

```python
import win32com.client

# Convert .doc to .docx (python-docx cannot read .doc format)
word = win32com.client.Dispatch('Word.Application')
word.Visible = False
doc = word.Documents.Open(r'D:\path\template.doc')
doc.SaveAs(r'D:\path\template.docx', FileFormat=16)  # 16 = wdFormatDocumentDefault
doc.Close()
word.Quit()
```

**Template cleanup technique:**

After loading a template, **delete existing paragraph elements** rather than clearing their text:

```python
# ❌ BAD: leaves empty paragraph elements, creates visible blank lines
for p in doc.paragraphs:
    if p.text.strip():
        for run in p.runs:
            run.text = ''

# ✅ GOOD: removes all template paragraph elements entirely
for p in list(doc.paragraphs):
    p._element.getparent().remove(p._element)
```

The `list()` wrapper is required because modifying `doc.paragraphs` during iteration invalidates the iterator. Removing the XML element directly ensures no residual empty paragraphs remain in the document.

**Key python-docx formatting functions:**

```python
from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn, nsdecls
from docx.oxml import parse_xml

def set_font(run, name_cn='宋体', name_en='Times New Roman', size=Pt(12), bold=False):
    """双字体设置（中/英文不同字体）"""
    run.font.name = name_en
    run.font.size = size
    run.font.bold = bold
    r = run._element
    rPr = r.find(qn('w:rPr'))
    if rPr is None:
        rPr = parse_xml(f'<w:rPr {nsdecls("w")}></w:rPr>')
        r.insert(0, rPr)
    rFonts = rPr.find(qn('w:rFonts'))
    if rFonts is None:
        rFonts = parse_xml(f'<w:rFonts {nsdecls("w")}></w:rFonts>')
        rPr.insert(0, rFonts)
    rFonts.set(qn('w:eastAsia'), name_cn)
```

**Formatting specs:**
| Element | Size | Font (CN/EN) | Line Spacing | Special |
|---------|------|-------------|-------------|---------|
| 一级标题 | 14pt | 宋体 | 1.0 | Bold, 段前12pt |
| 二级标题 | 13pt | 宋体 | 1.0 | Bold, 段前8pt |
| 正文 | 12pt | 宋体/TNR | 1.5 | 首行缩进0.74cm |
| 图标题 | 10.5pt | 宋体/TNR | 1.0 | 居中 |
| 表格 | 9pt | 宋体/TNR | — | 居中, 表头加粗+灰底 |

**Pitfalls (Python string quoting):**

When embedding Chinese text with quotation marks into Python strings:
- ❌ `"...验证了"瓶颈不会消失"的结论..."` → SyntaxError (nested `"`)
- ✅ Use single quotes: `'...验证了《瓶颈不会消失》的结论...'`
- ✅ Use Unicode escapes: `'\u201c瓶颈不会消失\u201d'`
- ✅ Use triple-quoted strings: `"""...验证了"瓶颈不会消失"的结论..."""`
- ✅ Concatenate: `'...验证了' + '"瓶颈不会消失"' + '的结论...'`

**Pitfall (paragraph sparseness — one sentence per page):**

❌ `Building the docx with one `para()` call per sentence creates sparse documents` — each `doc.add_paragraph()` generates a separate Word paragraph. Even with `space_before/after=Pt(0)`, visual spacing between paragraphs compounds. The result is a document where each page has only 1-2 sentences.

✅ **Merged-paragraph approach:** Combine multiple sentences into a single `para()` call using Python string concatenation:

```python
# WRONG — each sentence is a separate paragraph, creates sparse pages
para(doc, '发现一：...')
para(doc, '发现二：...')
para(doc, '发现三：...')

# RIGHT — merge into fewer, denser paragraphs
para(doc, (
    '本实验的主要发现包括：'
    '发现一：...'
    '发现二：...'
    '发现三：...'
))
```

**Formatting specs for dense reports:**
| Element | Size | Line Spacing | Spacing Before/After | Special |
|---------|------|-------------|---------------------|---------|
| 一级标题 | 14pt | **1.0** | Pt(6)/Pt(2) | Bold |
| 二级标题 | 13pt | **1.0** | Pt(6)/Pt(2) | Bold |
| 正文 | 12pt | **1.0** | Pt(0)/Pt(0) | 首行缩进0.74cm |
| 图标题 | 10.5pt | 1.0 | Pt(0)/Pt(4) | 居中 |
| 表格 | 9pt | — | — | 居中, 表头加粗+灰底 |

**Critical:** Use `line_spacing=1.0` (not 1.5) for body text in multi-section reports. 1.5 line spacing is standard for academic papers, but combined with many small paragraphs creates excessive white space. If paragraphs are long (4-6 sentences each), 1.5 is fine; if they average 1-2 sentences, use 1.0.

### Step 6: Insert Images in docx

```python
def insert_image(doc, img_path, width_cm=14):
    """Insert image centered with proper dimensions."""
    if not os.path.exists(img_path):
        print(f'WARN: Image not found: {img_path}')
        return
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run()
    run.add_picture(img_path, width=Cm(width_cm))
```

### Step 7: Insert Tables in docx

```python
def insert_table(doc, headers, rows, caption_text=None):
    t = doc.add_table(rows=1 + len(rows), cols=len(headers))
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    # Header row with blue background
    for i, h in enumerate(headers):
        cell = t.rows[0].cells[i]
        cell.text = ''
        p = cell.paragraphs[0]
        run = p.add_run(str(h))
        set_font(run, size=Pt(9), bold=True)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        shading = parse_xml(f'<w:shd {nsdecls("w")} w:fill="D9E2F3" w:val="clear"/>')
        cell._tc.get_or_add_tcPr().append(shading)
    # Data rows
    for ri, row in enumerate(rows):
        for ci, val in enumerate(row):
            cell = t.rows[ri + 1].cells[ci]
            cell.text = ''
            p = cell.paragraphs[0]
            run = p.add_run(str(val))
            set_font(run, size=Pt(9))
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
```

## Verification Checklist

After docx generation, verify:
- [ ] File opens in Word without errors
- [ ] All images render correctly (count: N images embedded)
- [ ] Tables have proper headers and alignment
- [ ] Chinese text displays in 宋体
- [ ] English/numbers display in Times New Roman
- [ ] Page margins and line spacing match specs
- [ ] Header/footer from template preserved
