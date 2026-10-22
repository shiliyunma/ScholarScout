# DOCX Image-Optical Character Recognition (OCR) Workflow

## Trigger
User attaches a `.docx` file containing embedded images (terminal screenshots, benchmark results, charts) and asks to OCR them.

## Prerequisites
- Python 3.8+
- PaddleOCR (`pip install paddleocr paddlepaddle`)
- unzip (available on Linux/macOS/git-bash on Windows)
- Total runtime: ~1s per image on GPU, ~3-5s per image on CPU

## Extraction & OCR Pipeline

### 1. Extract Images from DOCX
```bash
# Create extraction directory
mkdir -p _docx_extract
unzip "report.docx" -d _docx_extract
ls -la _docx_extract/word/media/
```

Identify image types by size:
- **Small (~20-30KB)**: icons, logos, simple charts — often skip
- **Large (~300-400KB)**: terminal screenshots with data — OCR these

### 2. Run PaddleOCR
```python
from paddleocr import PaddleOCR
from pathlib import Path

ocr = PaddleOCR(use_angle_cls=True, lang='ch')

image_dir = Path('_docx_extract/word/media')
for img_path in sorted(image_dir.glob('*.png')):
    result = ocr.ocr(str(img_path))
    print(f"\n## {img_path.name}")
    for line_group in result:
        if not line_group:
            continue
        for line in line_group:
            text = line[1][0]
            conf = line[1][1]
            print(f"  {text}  (conf={conf:.2f})")
```

### 3. Output as Markdown Files
```python
output_dir = Path('实验结果_OCR')
output_dir.mkdir(exist_ok=True)

image_dir = Path('_docx_extract/word/media')

for i, img_path in enumerate(sorted(image_dir.glob('*.png')), 1):
    result = ocr.ocr(str(img_path))
    lines = []
    for line_group in result:
        if line_group:
            for line in line_group:
                lines.append(line[1][0])
    
    md_content = f"# {img_path.name}\n\n"
    md_content += "\n\n".join(lines)
    
    out_path = output_dir / f"{i:02d}_{img_path.stem}.md"
    out_path.write_text(md_content, encoding='utf-8')
    print(f"Written: {out_path} ({len(lines)} lines)")
```

## Output Structure
```
实验结果_OCR/
├── 01_image1.md          ← first image
├── 02_image2.md
├── 03_image3.md          ← terminal screenshot (16+ lines)
└── ...
```

## Pitfalls & Workarounds

### PaddleOCR First-Run Model Download
**Symptom**: First `PaddleOCR()` call hangs or fails with network error.
**Fix**: Models download from `paddleocr.bj.bcebos.com` (CDN accessible in China).
Pre-download manually:
```python
from paddleocr import PaddleOCR
ocr = PaddleOCR(use_angle_cls=True, lang='ch')  # triggers download
```

### Low-Confidence Text
**Symptom**: Terminal screenshots with low contrast produce conf < 0.7.
**Fix**: Accept all lines regardless of conf value; let the user review.

### Reading Order
**Symptom**: Multi-column layouts produce scrambled text.
**Fix**: Sort detection boxes by y-coordinate:
```python
boxes = []
for line_group in result:
    if line_group:
        for line in line_group:
            boxes.append((line[0][0][1], line[1][0]))  # (y, text)
boxes.sort(key=lambda x: x[0])  # sort top-to-bottom
```

### Memory Pressure
**Symptom**: OOM on 20+ large images.
**Fix**: Process one image at a time (PaddleOCR handles this by default).
