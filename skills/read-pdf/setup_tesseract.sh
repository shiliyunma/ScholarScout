#!/bin/bash
# === /read-pdf 环境一键配置脚本 ===
# 适用: Windows + Git Bash + MSYS2
# 用途: 新机只需跑一次，即可使用中文 PDF OCR 全流程
#
# 用法: bash setup_tesseract.sh

set -e

echo "=========================================="
echo "  /read-pdf 环境配置 (OCR + 文本提取)"
echo "=========================================="
echo ""

# ============================================================
# Section 1: Python packages (always safe, no admin needed)
# ============================================================
echo "--- [1/6] Python packages ---"
pip install PyPDF2 pdfplumber ocrmypdf
echo ""

# ============================================================
# Section 2: Tesseract OCR + tessdata_best models
# ============================================================
echo "--- [2/6] Tesseract tessdata_best models ---"

TESSDATA="C:/Program Files/Tesseract-OCR/tessdata"

if [ ! -f "$TESSDATA/chi_sim.traineddata" ]; then
    echo "ERROR: Tesseract not found at expected path."
    echo "Please install from: https://github.com/UB-Mannheim/tesseract/wiki"
    echo "Then re-run this script."
    exit 1
fi

# Backup existing models
[ -f "$TESSDATA/chi_sim.traineddata" ] && [ ! -f "$TESSDATA/chi_sim_backup.traineddata" ] && \
    cp "$TESSDATA/chi_sim.traineddata" "$TESSDATA/chi_sim_backup.traineddata" 2>/dev/null || true

# Download best models (float32 LSTM, ~13MB each)
echo "Downloading chi_sim best model..."
curl -L --retry 3 -o "$TESSDATA/chi_sim.traineddata" \
    "https://github.com/tesseract-ocr/tessdata_best/raw/main/chi_sim.traineddata"

echo "Downloading chi_sim_vert best model..."
curl -L --retry 3 -o "$TESSDATA/chi_sim_vert.traineddata" \
    "https://github.com/tesseract-ocr/tessdata_best/raw/main/chi_sim_vert.traineddata"

echo "Downloading eng best model..."
curl -L --retry 3 -o "$TESSDATA/eng.traineddata" \
    "https://github.com/tesseract-ocr/tessdata_best/raw/main/eng.traineddata"

# osd: keep system default (best repo doesn't have osd)
echo ""

# ============================================================
# Section 3: Ghostscript
# ============================================================
echo "--- [3/6] Ghostscript ---"
if ! gswin64c --version &>/dev/null; then
    echo "Ghostscript not found on PATH."
    echo "Please install from: https://ghostscript.com/releases/gsdnld.html"
    echo "Then add its bin/ directory to PATH (see Section 5)."
else
    echo "Ghostscript OK: $(gswin64c --version 2>&1 | head -1)"
fi
echo ""

# ============================================================
# Section 4: MSYS2 tools (unpaper + pngquant)
# ============================================================
echo "--- [4/6] MSYS2 tools (unpaper + pngquant) ---"

if [ -f "/c/msys64/usr/bin/bash.exe" ]; then
    echo "Installing unpaper..."
    /c/msys64/usr/bin/bash.exe -lc "pacman -S --noconfirm --needed mingw-w64-x86_64-unpaper" 2>&1 | tail -1

    echo "Installing pngquant..."
    /c/msys64/usr/bin/bash.exe -lc "pacman -S --noconfirm --needed mingw-w64-ucrt-x86_64-pngquant" 2>&1 | tail -1
else
    echo "MSYS2 not found. Please install from: https://www.msys2.org/"
    echo "Then re-run this script."
fi
echo ""

# ============================================================
# Section 5: .bashrc PATH
# ============================================================
echo "--- [5/6] .bashrc PATH ---"

BASHRC="$HOME/.bashrc"
PATH_BLOCK='
# === /read-pdf toolchain ===
export PATH="$PATH:/c/Program Files/Tesseract-OCR"
export PATH="$PATH:/e/Program Files/gs/gs10.07.1/bin"
export PATH="$PATH:/c/msys64/mingw64/bin"
export PATH="$PATH:/c/msys64/ucrt64/bin"
'

if grep -q "/read-pdf toolchain" "$BASHRC" 2>/dev/null; then
    echo "PATH already configured in .bashrc"
else
    echo "$PATH_BLOCK" >> "$BASHRC"
    echo "PATH block appended to $BASHRC"
fi
echo ""

# ============================================================
# Section 6: Verify
# ============================================================
echo "--- [6/6] Verify ---"
echo ""

check() { printf "  %-20s" "$1"; if $2 &>/dev/null; then echo "✅"; else echo "❌ MISSING"; fi; }

check "Python"             "python --version"
check "PyPDF2"             "python -c 'import PyPDF2'"
check "pdfplumber"         "python -c 'import pdfplumber'"
check "OCRmyPDF"           "python -c 'import ocrmypdf'"
check "Tesseract"          "tesseract --version"
check "Ghostscript"        "gswin64c --version"
check "unpaper"            "unpaper --version"
check "pngquant"           "pngquant --version"

echo ""
echo "Tesseract languages:"
TESSDATA_PREFIX="$TESSDATA" tesseract --list-langs 2>&1
echo ""

echo "=========================================="
echo "  Setup complete. Restart Git Bash."
echo "  Test with:  /read-pdf"
echo "=========================================="
