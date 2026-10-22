# OCR Engine Comparison — Terminal Screenshots

## Test case: Hadoop benchmark terminal screenshots

24 images from a `.docx` file, each a terminal screenshot with:
- Monospace text (Hadoop log output)
- Data values: timestamps, throughput numbers, `real`/`user`/`sys` timing
- Mixed Chinese headers + English log text
- Small images (~280×130, wordcount `real` time results)
- Large images (~1540×570, TestDFSIO results with 70-90 lines)

## Results

| Engine | Granularity | Line integrity | Data extraction | Speed (24 imgs) |
|--------|-------------|----------------|-----------------|-----------------|
| OCRmyPDF + Tesseract 5.4 (chi_sim+eng) | **Line-level** | `real 1m2.637s` ✅ | Throughput, times all matchable | ~2min (CPU) |
| EasyOCR 1.7.2 (ch_sim+en, GPU) | **Word-level** | `real` + `102.6375` split ❌ | Fragments hard to reassemble | ~15s (RTX 4050) |

## Takeaway

For **terminal/log screenshots** with monospace text and structured data:

1. **Tesseract (via OCRmyPDF)** is clearly better — it natively groups text into lines
2. Word-level engines (EasyOCR, PaddleOCR) fragment terminal output so badly that `real 1m2.637s` becomes 5-7 separate tokens with no reliable way to rejoin them
3. The line grouping from Tesseract's PSM 6 (`--tesseract-pagesegmode 6`) is the key advantage

## When word-level engines win

- Dense multi-language documents (mixed CJK + English paragraphs)
- Scanned books with varied fonts
- Low-quality photos of text
- When you need character-level confidence scores

## PaddleOCR 3.7.0 note

PaddleOCR 3.7.0 has a `NotImplementedError: ConvertPirAttribute2RuntimeAttribute` crash on Windows with OneDNN. Setting `DNNL_DEFAULT=0` doesn't help. Use EasyOCR instead for a Paddle-like pipeline, or fall back to Tesseract for terminal screenshots.

## Chinese path workaround (Windows)

OpenCV's `imread` (used by EasyOCR and PaddleOCR internally) cannot open files with Chinese characters in the path. Workaround:

```python
from PIL import Image
import numpy as np

pil_img = Image.open(path_with_chinese).convert('RGB')
img_array = np.array(pil_img)
# pass img_array to OCR engine instead of file path
```
