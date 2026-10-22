---
name: ocr-and-documents
description: "Extract text from PDFs/scans (pymupdf, marker-pdf, MinerU) and OCR images from DOCX files (PaddleOCR). China-network friendly guidance."
version: 2.5.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [PDF, Documents, Research, Arxiv, Text-Extraction, OCR, DOCX, PaddleOCR]
    related_skills: [powerpoint]
---

# PDF & Document Extraction

This skill covers **PDFs**, **scanned documents**, and **image extraction from Office documents** (DOCX/PPTX) for OCR.

For DOCX text content: use `python-docx` (parses actual structure, far better than OCR).
For PPTX: see the `powerpoint` skill (uses `python-pptx` with full slide/notes support).
For **extracting images from DOCX and OCRing them** (e.g., terminal screenshots in reports): see the DOCX Image Extraction section below.

## Step 1: Remote URL Available?

If the document has a URL, **always try `web_extract` first**:

```
web_extract(urls=["https://arxiv.org/pdf/2402.03300"])
web_extract(urls=["https://example.com/report.pdf"])
```

This handles PDF-to-markdown conversion via Firecrawl with no local dependencies.

Only use local extraction when: the file is local, web_extract fails, or you need batch processing.

## Step 2: Choose Local Extractor

| Feature | pymupdf (~25MB) | marker-pdf (~3-5GB) | MinerU (~1-2GB) | Unstructured (~3-5GB) |
|---------|-----------------|---------------------|-----------------|----------------------|
| **Text-based PDF** | ✅ | ✅ | ✅ | ✅ |
| **Scanned PDF (OCR)** | ❌ | ✅ (90+ languages) | ✅ (109 languages, VLM+OCR) | ✅ |
| **Tables** | ✅ (basic) | ✅ (high accuracy) | ✅ (HTML format, high) | ✅ (basic) |
| **Equations / LaTeX** | ❌ | ✅ | ✅ | ❌ (Unicode approx) |
| **Code blocks** | ❌ | ✅ | ✅ | ✅ |
| **Forms** | ❌ | ✅ | ✅ | ❌ |
| **Headers/footers** | ❌ | ✅ | ✅ | ❌ |
| **Reading order** | ❌ | ✅ | ✅ | ✅ |
| **Images extraction** | ✅ (embedded) | ✅ (with context) | ✅ | ✅ |
| **DOCX/PPTX/XLSX** | ❌ | ❌ | ✅ (native parse) | ✅ (basic) |
| **Markdown output** | ✅ (pymupdf4llm) | ✅ (native) | ✅ (native, best quality) | ✅ (JSON elements) |
| **Install size** | ~25MB | ~3-5GB | ~1-2GB (lighter) | ~3-5GB (detectron2) |
| **Speed** | Instant | ~1-14s/page CPU | Fast (pipeline backend) | Slow |
| **China network** | ✅ (no models) | ❌ (HF models) | ✅ (ModelScope + auto-select) | ❌ (many deps) |
| **中文支持** | ✅ (text PDFs) | ✅ (tesseract-based) | ✅ (native, best in class) | ⚠️ (mediocre) |
| **License** | AGPL / BSD | GPLv3 | Apache 2.0 | Apache 2.0 |

**Decision tree (for China users especially):**

1. **URL available?** → `web_extract` (zero install, fastest)
2. **Text-based PDF?** → `pymupdf` (25MB, instant, no network)
3. **Scanned/Chinese/equations/complex PDF?** → **MinerU** (most China-friendly: ModelScope auto-download, native Chinese OCR, Apache 2.0, outputs structured Markdown)
4. **marker-pdf** — only if MinerU fails (HF models may need manual download in China)
5. **Unstructured** — not recommended for Chinese documents (detectron2 heavy, equation support poor)

If the user needs MinerU but the system lacks ~2GB free disk:
> "MinerU needs ~1-2GB for models. Your system has [X]GB free. Options: free up space, try web_extract for a URL, or use pymupdf for text-based PDFs only."

---

## pymupdf (lightweight)

```bash
pip install pymupdf pymupdf4llm
```

**Via helper script**:
```bash
python scripts/extract_pymupdf.py document.pdf              # Plain text
python scripts/extract_pymupdf.py document.pdf --markdown    # Markdown
python scripts/extract_pymupdf.py document.pdf --tables      # Tables
python scripts/extract_pymupdf.py document.pdf --images out/ # Extract images
python scripts/extract_pymupdf.py document.pdf --metadata    # Title, author, pages
python scripts/extract_pymupdf.py document.pdf --pages 0-4   # Specific pages
```

**Inline**:
```bash
python3 -c "
import pymupdf
doc = pymupdf.open('document.pdf')
for page in doc:
    print(page.get_text())
"
```

---

## MinerU (recommended for Chinese docs, China-network friendly)

**What it is**: PDF/DOCX/PPTX/XLSX/图片 → 结构化 Markdown/JSON. Developed by Shanghai AI Lab (书生·浦语), v3.4 (2026/06), Apache 2.0.

**Best for**: Chinese documents, scanned PDFs, complex layouts, formulas, tables. Automatically selects model source (ModelScope in China, HF elsewhere).

```bash
pip install mineru
```

**CLI usage**:
```bash
# Single file
mineru document.pdf

# Output to specific dir
mineru document.pdf -o ./output

# Batch directory
mineru ./pdfs/

# Use pipeline backend (fast, CPU/GPU, no VLM model needed)
mineru document.pdf --method pipeline
```

**Python API**:
```python
from mineru import MinerU

extractor = MinerU(method="pipeline")  # or "vlm" for higher accuracy
result = extractor("document.pdf")
print(result.markdown)  # Structured markdown
```

**Key advantages over marker-pdf for China users**:
- ModelSource auto-select: ModelScope (accessible in China) instead of HF
- Better Chinese formula/table recognition
- Lighter install (~1-2GB vs ~3-5GB)
- Apache 2.0 license (vs GPLv3)
- Supports DOCX/PPTX/XLSX natively (marker-pdf only does PDF/EPUB/MOBI)

**Pitfalls**:
- First run downloads models automatically (~1GB for pipeline backend, ~2GB for VLM)
- Pipeline backend is CPU-friendly and sufficient for most Chinese docs
- VLM backend needs GPU for reasonable speed but gives highest accuracy
- For very large batches, use `mineru-router` for multi-GPU load balancing

---

## marker-pdf (high-quality OCR)

```bash
# Check disk space first
python scripts/extract_marker.py --check

pip install marker-pdf
```

**Via helper script**:
```bash
python scripts/extract_marker.py document.pdf                # Markdown
python scripts/extract_marker.py document.pdf --json         # JSON with metadata
python scripts/extract_marker.py document.pdf --output_dir out/  # Save images
python scripts/extract_marker.py scanned.pdf                 # Scanned PDF (OCR)
python scripts/extract_marker.py document.pdf --use_llm      # LLM-boosted accuracy
```

**CLI** (installed with marker-pdf):
```bash
marker_single document.pdf --output_dir ./output
marker /path/to/folder --workers 4    # Batch
```

---

## Unstructured (when you need RAG-ready chunking)

**What it is**: Document parsing library from Unstructured.io. Outputs structured JSON with semantic element types (Title, NarrativeText, Table, etc.) — designed for RAG ingestion.

**Best for**: RAG pipelines that need pre-chunked, classified document elements. **Not recommended** for Chinese documents with formulas/tables.

```bash
pip install unstructured[pdf,docx]
```

**Python API**:
```python
from unstructured.partition.pdf import partition_pdf

elements = partition_pdf("document.pdf")
for el in elements:
    print(f"[{el.category}] {el.text[:100]}")
```

**Pitfalls**:
- `pip install` is slow in China (many C++ deps, detectron2)
- Chinese support is mediocre
- Equations → Unicode approximation (not LaTeX), LLM-unfriendly
- For RAG chunking, you can achieve similar results with LangChain's RecursiveCharacterTextSplitter on MinerU/marker-pdf output

---

## Arxiv Papers

```
# Abstract only (fast)
web_extract(urls=["https://arxiv.org/abs/2402.03300"])

# Full paper
web_extract(urls=["https://arxiv.org/pdf/2402.03300"])

# Search
web_search(query="arxiv GRPO reinforcement learning 2026")
```

## Split, Merge & Search

pymupdf handles these natively — use `execute_code` or inline Python:

```python
# Split: extract pages 1-5 to a new PDF
import pymupdf
doc = pymupdf.open("report.pdf")
new = pymupdf.open()
for i in range(5):
    new.insert_pdf(doc, from_page=i, to_page=i)
new.save("pages_1-5.pdf")
```

```python
# Merge multiple PDFs
import pymupdf
result = pymupdf.open()
for path in ["a.pdf", "b.pdf", "c.pdf"]:
    result.insert_pdf(pymupdf.open(path))
result.save("merged.pdf")
```

```python
# Search for text across all pages
import pymupdf
doc = pymupdf.open("report.pdf")
for i, page in enumerate(doc):
    results = page.search_for("revenue")
    if results:
        print(f"Page {i+1}: {len(results)} match(es)")
        print(page.get_text("text"))
```

No extra dependencies needed — pymupdf covers split, merge, search, and text extraction in one package.

---

## DOCX Image Extraction + OCR (PaddleOCR)

Common scenario: a Word report contains terminal-screenshot images with benchmark results. PaddleOCR handles Chinese + English text well and works fully offline (no GFW issues).

### Step 1: Extract Images from DOCX

DOCX is a ZIP archive. Images live in `word/media/`:

```bash
mkdir -p _docx_extract
unzip "report.docx" -d _docx_extract
ls _docx_extract/word/media/
```

Small files (~20-30KB) are often icons/charts; large files (~300-400KB) are typically terminal screenshots worth OCRing.

### Step 2: Install PaddleOCR

```bash
pip install paddleocr paddlepaddle
# Or for GPU (CUDA):
pip install paddlepaddle-gpu
```

First run downloads detection + recognition models (~15MB total) to `~/.paddleocr/`.

### Step 3: OCR Images

```python
from paddleocr import PaddleOCR
from pathlib import Path

ocr = PaddleOCR(use_angle_cls=True, lang='ch')  # 'ch' = Chinese + English

image_dir = Path('_docx_extract/word/media')
for img_path in sorted(image_dir.glob('*.png')):
    result = ocr.ocr(str(img_path))
    print(f"\n## {img_path.name}")
    for line_group in result:
        if line_group is None:
            continue
        for line in line_group:
            text = line[1][0]   # recognized text
            conf = line[1][1]   # confidence
            print(f"  {text}  (conf={conf:.2f})")
```

### Step 4: Output as Markdown

Organize results into per-image `.md` files:

```python
output_dir = Path('实验结果_OCR')
output_dir.mkdir(exist_ok=True)

for i, img_path in enumerate(sorted(image_dir.glob('*.png')), 1):
    result = ocr.ocr(str(img_path))
    lines = []
    for line_group in result:
        if line_group:
            for line in line_group:
                lines.append(line[1][0])
    md_content = f"# {img_path.name}\n\n" + "\n\n".join(lines)
    (output_dir / f"{i:02d}_{img_path.stem}.md").write_text(md_content, encoding='utf-8')
```

### Pitfalls

- **PaddleOCR first run**: downloads models from `paddleocr.bj.bcebos.com` (accessible in China). If blocked, `~/.paddleocr/whl/det/ch/ch_PP-OCRv4_det_infer/` and `~/.paddleocr/whl/rec/ch/ch_PP-OCRv4_rec_infer/` can be manually populated.
- **Confidence filtering**: terminal screenshots often have low contrast — set a low floor (conf >= 0.5) or report all lines with conf scores.
- **Reading order**: PaddleOCR returns lines grouped by detection, not guaranteed top-to-bottom. For terminal outputs this is usually fine; for multi-column layouts, sort results by bounding box y-coordinate.
- **Image format**: PNG preferred. JPEG with compression artifacts reduces accuracy.
- **Memory**: large batches of images keep models loaded — process sequentially to avoid OOM.

---

## Notes

- `web_extract` is always first choice for URLs
- pymupdf is the safe default — instant, no models, works everywhere
- **MinerU** is the best choice for scanned/Chinese/complex docs in China (ModelScope auto-download, Apache 2.0, v3.4)
- marker-pdf is for OCR, scanned docs, equations, complex layouts — install only when needed and HF models are accessible
- Unstructured is for RAG chunking workflows — heavy install, poor Chinese support
- All helper scripts accept `--help` for full usage
- marker-pdf downloads ~2.5GB of models to `~/.cache/huggingface/` on first use (may fail in China)
- MinerU downloads models to `~/.cache/mineru/` from ModelScope (accessible in China)
- For Word docs: `pip install python-docx` (better than OCR — parses actual structure)
- For PowerPoint: see the `powerpoint` skill (uses python-pptx)
