---
name: ppt-pdf-slides-to-rag
description: 将 PPT / PDF 幻灯片（尤其是中文 PowerPoint 导出 PDF）经 OCR 提取文本、清洗后导入 RAG 向量库。
---

## 适用场景

PowerPoint 演示文稿导出为 PDF 时，中文文本经常丢失（pdftotext/PyPDF2 只能提取英文和数字，中文字符完全缺失）。本 skill 用 OCR 重新识别——**关键是用 pagesegmode 6（单文本块）和不用 --clean**，否则幻灯片背景干扰会导致大量乱码。

## 快捷脚本

项目中有一个可直接复用的脚本（也挂在本 skill 下）：

```bash
# 单文件
scripts/ocr_pdf_slides.sh "D:/path/to/slides.pdf"           # 输出到 pdf 同目录
scripts/ocr_pdf_slides.sh "D:/path/to/slides.pdf" clean_docs/ # 指定输出目录

# 批量处理整个目录
scripts/ocr_pdf_slides.sh "D:/期末复习资料/" clean_docs/

# 脚本会：OCR → 提取文本 → 清理临时文件 → 自动跑 clean_ocr.py 清洗
```

脚本路径：`E:\RAGLearn\scripts\ocr_pdf_slides.sh`

也作为 skill 的 linked file 可用：`skill_view(name='ppt-pdf-slides-to-rag', file_path='scripts/ocr_pdf_slides.sh')`

## 工作流程

### Step 0: 判断文件类型

```bash
# PPT 文件 → 用 win32com
# PPTX 文件 → 用 python-pptx
# PDF 文件（含 PPT 导出的 PDF）→ 走 OCR 流程
```

### Step 1: PPT 转换（win32com）

```python
import win32com.client
powerpoint = win32com.client.Dispatch("PowerPoint.Application")
prs = powerpoint.Presentations.Open(src_path)
texts = []
for slide in prs.Slides:
    for shape in slide.Shapes:
        if shape.HasTextFrame:
            t = shape.TextFrame.TextRange.Text.strip()
            if t: texts.append(t)
prs.Close()
# 写入 .txt
powerpoint.Quit()
```

注意：PowerPoint COM 拒绝 `Visible = False`，窗口会短暂闪烁，安全忽略。

### Step 2: PDF OCR（OCRmyPDF + Tesseract）

**关键参数（踩坑经验）：**

| 参数 | 值 | 原因 |
|------|:--:|------|
| `--language` | `chi_sim+eng` | 中英双语 |
| `--force-ocr` | — | 覆盖已有但缺中文的文本层 |
| `--tesseract-oem` | `1` | LSTM 引擎，CJK 效果好 |
| `--oversample` | `300` | 300 DPI 足够，450 无额外收益 |
| `--tesseract-pagesegmode` | **`6`** | **单文本块（幻灯片最佳）** |
| `--clean` | **不使用** | unpaper 损坏幻灯片文本 |

```bash
ocrmypdf \
  --language chi_sim+eng \
  --force-ocr \
  --tesseract-oem 1 \
  --oversample 300 \
  --tesseract-pagesegmode 6 \
  "input.pdf" "output_ocr.pdf"
```

### Step 3: 提取 OCR 文本

```python
from PyPDF2 import PdfReader
reader = PdfReader(ocr_pdf_path)
text = "\n\n".join(
    (page.extract_text() or "") for page in reader.pages
)
```

### Step 4: 清洗 OCR 文本

```bash
# 项目自带的 clean_ocr.py 会处理：多余空格、重复标点、孤立符号
python scripts/clean_ocr.py --output clean_docs/
```

### Step 5: 导入向量库

```bash
# 只导入新增文件（用绝对路径避免 relative_to 错误）
.venv/Scripts/python.exe ingest.py \
  "E:/RAGLearn/clean_docs/数据库实现_xxx.txt" \
  "E:/RAGLearn/clean_docs/数据库实现_yyy.txt"
```

## 已知坑

1. **pagesegmode 切换陷阱**：幻灯片用 `--tesseract-pagesegmode 3`（自动）会产生大量乱码。**必须用 `6`（单文本块）**。纯文本 PDF（非幻灯片）则可以用 `3`。

2. **`--clean` 有害**：`--clean` 调用 unpaper 做背景清理，对纸质扫描件有用，但对数字幻灯片（有渐变背景、装饰线条）会**严重损坏文字**。

3. **`force-ocr` 必须**：这些 PDF 已有文本层（虽然缺中文），不加 `--force-ocr` 会报 `PriorOcrFoundError`。

4. **`relative_to` 路径陷阱**：`ingest.py` 的 `fp.relative_to(DOCS_DIR)` 要求传入的路径在 DOCS_DIR 下。传相对路径时可能匹配失败，**始终用绝对路径**。

5. **Python 输出缓冲**：subprocess.run 在后台运行时 stdout 被缓冲，看不到实时进度。用 `sys.stdout.reconfigure(line_buffering=True)` 解决。

6. **OCR 不是完美的**：中文识别率约 85-90%，部分字符会被误识别（如"数据"→"数据"、丢偏旁等），但配合 bge-large-zh 嵌入 + reranker，RAG 检索效果足够好。
