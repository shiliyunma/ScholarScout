#!/bin/bash
# =========================================================================
# ocr_pdf_slides.sh — OCR 中文 PDF 幻灯片 → 清洗后 txt
#
# 用途：PowerPoint 导出 PDF 经 OCRmyPDF + Tesseract 提取中文文本
#       解决 pdftotext/PyPDF2 只能提英文、中文字符全丢失的问题
#
# 依赖：ocrmypdf, tesseract(chi_sim+eng), gswin64c, python(PyPDF2)
#
# 用法：
#   ./ocr_pdf_slides.sh  input.pdf                  → 输出 input.txt
#   ./ocr_pdf_slides.sh  input.pdf  output_dir/     → 输出 output_dir/input.txt
#   ./ocr_pdf_slides.sh  src_dir/   output_dir/     → 批量处理 src_dir/ 下所有 .pdf
#
# 之后自动跑 clean_ocr.py（如存在）
# =========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TMP_DIR="${PROJECT_DIR}/tmp"
CLEAN_SCRIPT="${SCRIPT_DIR}/clean_ocr.py"

# ── 参数 ──────────────────────────────────────────────────────────────
INPUT="${1:?用法: $0 <input.pdf|input_dir> [output_dir]}"
OUTPUT_DIR="${2:-$(dirname "$INPUT")}"

# 如果 OUTPUT_DIR 是相对路径，转绝对
OUTPUT_DIR="$(cd "$(dirname "$OUTPUT_DIR/.")" && pwd)/$(basename "$OUTPUT_DIR")"
mkdir -p "$OUTPUT_DIR" "$TMP_DIR"

# ── 工具检查 ──────────────────────────────────────────────────────────
command -v ocrmypdf >/dev/null 2>&1 || { echo "[ERROR] ocrmypdf not found"; exit 1; }
command -v tesseract >/dev/null 2>&1 || { echo "[ERROR] tesseract not found"; exit 1; }

# 检查 Tesseract 是否有 chi_sim
if ! tesseract --list-langs 2>&1 | grep -q chi_sim; then
    echo "[ERROR] Tesseract missing chi_sim language pack"
    echo "  Download from: https://github.com/tesseract-ocr/tessdata_best/raw/main/chi_sim.traineddata"
    echo "  Place in: C:\\Program Files\\Tesseract-OCR\\tessdata\\"
    exit 1
fi

# ── 处理单个 PDF ──────────────────────────────────────────────────────
ocr_one() {
    local pdf="$1"
    local outdir="$2"
    local base
    base="$(basename "$pdf" .pdf)"
    local txt_out="${outdir}/${base}.txt"
    local ocr_pdf="${TMP_DIR}/${base}_ocr.pdf"

    echo "──────────────────────────────────────────────"
    echo "[OCR] $base"

    # Step 1: OCR
    # 关键参数说明：
    #   --force-ocr           : 覆盖已有但缺中文的文本层
    #   --language chi_sim+eng: 中英双语
    #   --tesseract-oem 1     : LSTM 引擎，CJK 效果好
    #   --oversample 300      : 300 DPI 足够
    #   --tesseract-pagesegmode 6: 单文本块（幻灯片最佳）
    #   不使用 --clean        : unpaper 会损坏幻灯片文字
    if ! ocrmypdf \
        --language chi_sim+eng \
        --force-ocr \
        --tesseract-oem 1 \
        --oversample 300 \
        --tesseract-pagesegmode 6 \
        "$pdf" "$ocr_pdf" 2>&1; then
        echo "[FAIL] OCR failed for $base"
        return 1
    fi

    # Step 2: 提取文本
    python -c "
import sys
from PyPDF2 import PdfReader
try:
    reader = PdfReader(r'$ocr_pdf')
    texts = [(p.extract_text() or '') for p in reader.pages]
    with open(r'$txt_out', 'w', encoding='utf-8') as f:
        f.write('\n\n'.join(texts))
    print(f'  [输出] {len(texts)} pages, {sum(len(t) for t in texts)} chars')
except Exception as e:
    print(f'  [ERROR] {e}', file=sys.stderr)
    sys.exit(1)
"

    # Step 3: 清理临时文件
    rm -f "$ocr_pdf"
    echo "[DONE] $base"
}

# ── 批量/单文件 ───────────────────────────────────────────────────────
if [ -d "$INPUT" ]; then
    echo "📂 Batch mode: processing all PDFs in $INPUT"
    count=0
    for f in "$INPUT"/*.pdf; do
        [ -f "$f" ] || continue
        ocr_one "$f" "$OUTPUT_DIR"
        count=$((count + 1))
    done
    echo ""
    echo "✅ 批量完成！处理 $count 个文件"
else
    ocr_one "$INPUT" "$OUTPUT_DIR"
    echo ""
    echo "✅ 输出: ${OUTPUT_DIR}/$(basename "$INPUT" .pdf).txt"
fi

# Step 4: 可选 — 运行 clean_ocr.py 清洗
if [ -f "$CLEAN_SCRIPT" ]; then
    echo ""
    echo "🧹 运行 clean_ocr.py 清洗..."
    python "$CLEAN_SCRIPT" --output "$OUTPUT_DIR"
fi
