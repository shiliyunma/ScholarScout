---
name: read-pdf
description: Extract text from .pdf files via PyPDF2/pdfplumber (text PDFs) or OCRmyPDF + Tesseract + Ghostscript (scanned PDFs). Also handles PowerPoint-exported PDFs where Chinese chars are missing from text layer.
---

## Decision tree

1. **Not a PDF?** DOCX/PPTX/etc. with embedded screenshots → see Step 0.5.
2. **PyPDF2** — first try, works for text PDFs
3. **pdfplumber** — fallback for complex layouts / tables
4. **PowerPoint-exported PDF** — has text layer but Chinese chars missing (PyPDF2 returns >200 chars but no Chinese). Skip to **Step 3.5**.
5. **Scanned PDF** — PyPDF2 returns <200 chars total. Skip to **Step 3**.

## Prerequisites

```bash
# Python
pip install PyPDF2 pdfplumber ocrmypdf img2pdf

# System tools (must be on PATH — see .bashrc)
tesseract --version      # v5.4+, with chi_sim and eng
gswin64c --version       # Ghostscript
pngquant --version       # via MSYS2: pacman -S mingw-w64-ucrt-x64_64-pngquant
```

## Step 0: Check PDF type

```bash
python -c "
from PyPDF2 import PdfReader
reader = PdfReader('<FILE>')
text = ''.join((p.extract_text() or '') for p in reader.pages)
total = len(text.strip())
has_chinese = bool(__import__('re').search(r'[\u4e00-\u9fff]', text))
print(f'{total} chars / {len(reader.pages)} pages')
print(f'Has Chinese: {has_chinese}')
"
```

| Result | Type | Action |
|--------|------|--------|
| >200 chars + has Chinese | Text PDF | Steps 1-2 |
| >200 chars + **no Chinese** | **PPT-exported PDF** | **Step 3.5** (special OCR) |
| <200 chars | Scanned PDF | Step 3 |

## Step 0.5: DOCX/PPTX with embedded images (not a PDF)

If the source is a DOCX/PPTX (not a PDF) and you need to OCR screenshots inside it:

### 0.5a. Extract images

```bash
# DOCX/PPTX is a ZIP — unzip and extract images
mkdir -p _docx_extract
unzip -o "<FILE.docx>" -d _docx_extract
ls _docx_extract/word/media/
```

Images land in `word/media/` as PNG files. Small files (~23KB, ~280×130) are usually icons/headers; large files (~350KB, ~1540×570) are screenshots with data.

### 0.5b. Convert images to PDF

```bash
python -c "
import img2pdf, os, re

media_dir = '_docx_extract/word/media'
png_files = sorted(
    [f for f in os.listdir(media_dir) if f.endswith('.png')],
    key=lambda x: int(re.search(r'image(\d+)', x).group(1))
)
img_paths = [os.path.join(media_dir, f) for f in png_files]
with open('_combined.pdf', 'wb') as f:
    f.write(img2pdf.convert(img_paths))
print(f'PDF: {len(img_paths)} pages')
"
```

### 0.5c. Resume at Step 3 (scanned PDF OCR) or Step 3.5 (slides) with `_combined.pdf`

**Windows path note**: Python on Windows does NOT understand MSYS paths like `/d/path`. Use `D:/path` or `D:\\path` format in Python scripts.

**Pillow JPEG pitfall**: `Image.save(save_all=True)` can crash with `KeyError: 'JPEG'` on some Pillow builds. Use `img2pdf` instead — it's more reliable for converting images to PDF.

**Chinese path pitfall**: OpenCV's `imread()` (used by EasyOCR/PaddleOCR internally) cannot open files with CJK characters in the path on Windows. Workaround — read with Pillow and pass the numpy array instead of the file path:
```python
from PIL import Image
import numpy as np
img_array = np.array(Image.open(path).convert('RGB'))
# Now pass img_array to OCR engine
```

### Script for the full pipeline

A re-runnable script lives at `scripts/docx_ocr_to_md.py` — run it directly:
```bash
python <skill_dir>/scripts/docx_ocr_to_md.py "path/to/file.docx" [output_dir]
```

### Reference: OCR engine comparison

See `references/ocr-engine-comparison.md` for detailed comparison of Tesseract vs EasyOCR vs PaddleOCR on terminal screenshots. Key insight: **Tesseract (line-level) beats word-level engines** for monospace log output.

---

## Steps 1-2: Text PDF (PyPDF2 → pdfplumber)

```bash
# PyPDF2
python -c "
from PyPDF2 import PdfReader
text = ''.join((p.extract_text() or '') + '\n' for p in PdfReader('<FILE>').pages)
with open('<OUT>.txt', 'w', encoding='utf-8') as f: f.write(text)
print(f'{len(text)} chars')
"

# pdfplumber (slower, better layout)
python -c "
import pdfplumber
with pdfplumber.open('<FILE>') as pdf:
    text = ''.join((p.extract_text() or '') + '\n' for p in pdf.pages)
with open('<OUT>.txt', 'w', encoding='utf-8') as f: f.write(text)
"
```

## Step 3: Scanned/original paper PDF — OCR

### 3a. Verify Tesseract models

`tesseract --list-langs` must show `chi_sim` and `eng`.

**Required**: Use `tessdata_best` models (float32 LSTM, ~13MB each). The default 43MB `chi_sim` is legacy+LSTM int8 — worse at Chinese.

```bash
# Download best models (one-time)
curl -L -o "C:\Program Files\Tesseract-OCR\tessdata\chi_sim.traineddata" \
  "https://github.com/tesseract-ocr/tessdata_best/raw/main/chi_sim.traineddata"
curl -L -o "C:\Program Files\Tesseract-OCR\tessdata\chi_sim_vert.traineddata" \
  "https://github.com/tesseract-ocr/tessdata_best/raw/main/chi_sim_vert.traineddata"
```

### 3b. Run OCR (scanned paper docs)

```bash
ocrmypdf \
  --language chi_sim+eng \
  --clean \
  --tesseract-oem 1 \
  --oversample 300 \
  --tesseract-pagesegmode 3 \
  "<INPUT>" "<OUTPUT>"
```

### 3c. Extract text from OCR'd PDF

```bash
python -c "
from PyPDF2 import PdfReader
text = ''.join((p.extract_text() or '') + '\n' for p in PdfReader('<OUTPUT>').pages)
with open('<OUT>.txt', 'w', encoding='utf-8') as f: f.write(text)
"
```

---

## Step 3.5: PowerPoint-exported PDF — SPECIAL OCR

⚠️ **Critical difference from scanned docs**: These PDFs have a partial text layer (English/numbers OK, Chinese missing). Using `--clean` (unpaper) or wrong pagesegmode produces **heavy garbling** — >50% Chinese chars become noise.

**Key rules:**
- **NO `--clean`** — unpaper destroys slide backgrounds/decorations, producing garbage text
- **`--tesseract-pagesegmode 6`** — single uniform block (slides are simpler layouts than paper)
- **`--oversample 300`** — 300 DPI sufficient; 450+ adds no benefit
- **`--force-ocr`** — required to overwrite the existing (partial) text layer

### 3.5a. Run OCR

```bash
ocrmypdf \
  --language chi_sim+eng \
  --force-ocr \
  --tesseract-oem 1 \
  --oversample 300 \
  --tesseract-pagesegmode 6 \
  "<INPUT>" "<OUTPUT>"
```

### 3.5b. Extract text

```bash
python -c "
from PyPDF2 import PdfReader
text = ''.join((p.extract_text() or '') + '\n' for p in PdfReader('<OUTPUT>').pages)
with open('<OUT>.txt', 'w', encoding='utf-8') as f: f.write(text)
"
```

### 3.5c. Quick script (bundled in this skill)

A re-runnable script lives at `scripts/ocr_pdf_slides.sh` — supports single file or batch directory mode:

```bash
# Single file
bash <skill_dir>/scripts/ocr_pdf_slides.sh "D:/slides.pdf" output_dir/

# Batch directory
bash <skill_dir>/scripts/ocr_pdf_slides.sh "D:/slides_dir/" output_dir/
```

The script also runs clean_ocr.py automatically after extraction.

---

## Parameter reference

| Flag | Scanned paper | PPT-exported slides |
|------|:-------------:|:-------------------:|
| `--language chi_sim+eng` | ✓ | ✓ |
| `--force-ocr` | optional | **required** |
| `--clean` (unpaper) | **✓ beneficial** | ❌ **harmful** |
| `--tesseract-oem 1` | ✓ | ✓ |
| `--oversample` | 300 | 300 |
| `--tesseract-pagesegmode` | `3` (auto) | **`6`** (single block) |

## Flags to AVOID (harmful or useless)

| Flag | Why |
|------|-----|
| `--clean` on digital slides | unpaper destroys slide backgrounds/decorations, producing >50% garbled Chinese |
| `--deskew` | Only needed for skewed scans. On straight pages it inflates output 5× and creates edge artifacts. |
| `--tesseract-thresholding sauvola` | Extremely slow, minimal gain over `--clean` |
| `--remove-background` | Broken in OCRmyPDF v17 on Windows |
| `--tesseract-config <inline params>` | Use `--tesseract-oem N` instead |

## Diagnostics

| Warning | Meaning | Action |
|---------|---------|--------|
| `lots of diacritics` | CJK false positive — Tesseract's Latin diacritic heuristic misreads Chinese glyphs | Harmless, ignore |
| `no verapdf available` | PDF/A validator not installed | Harmless unless you need PDF/A |
| `Image too small to scale` | Page decorations/dots too tiny | Harmless if occasional |
| `No font found with glyphs for 'chi_sim'` | pikepdf can't find a CJK font for the text layer | Install Noto Sans/Serif CJK fonts; add filename pattern to `system_font_provider.py` |

## Path requirements (`.bashrc`)

```bash
export PATH="$PATH:/c/Program Files/Tesseract-OCR"
export PATH="$PATH:/e/Program Files/gs/gs10.07.1/bin"
export PATH="$PATH:/c/msys64/mingw64/bin"      # unpaper
export PATH="$PATH:/c/msys64/ucrt64/bin"       # pngquant
```

## Post-process: fix CJK spacing

```bash
python -c "
import re
with open('<OUT>.txt', 'r', encoding='utf-8') as f: text = f.read()
text = re.sub(r'(?<=[一-鿿])\s+(?=[一-鿿])', '', text)
text = re.sub(r'(?<=[一-鿿])\s+(?=[，。；：、！？）\)」】》])', '', text)
text = re.sub(r'(\d+)\.\s+(\d+%)', r'\1.\2', text)
with open('<OUT>.txt', 'w', encoding='utf-8') as f: f.write(text)
"
```
