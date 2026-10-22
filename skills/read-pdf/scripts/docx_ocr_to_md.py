#!/usr/bin/env python3
"""
DOCX → OCR → Markdown pipeline.
Extracts images from a .docx file, OCRs them with OCRmyPDF+Tesseract,
and outputs per-page .md files + a summary README.

Usage:
    python docx_ocr_to_md.py <input.docx> [output_dir]

Requirements:
    PyPDF2, img2pdf, Pillow
    OCRmyPDF + Tesseract (chi_sim, eng)
    Ghostscript, pngquant (on PATH)
"""
import os, sys, re, shutil, subprocess, tempfile

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    docx_path = os.path.abspath(sys.argv[1])
    out_dir = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else \
        os.path.join(os.path.dirname(docx_path), 'OCR_output')

    os.makedirs(out_dir, exist_ok=True)

    # --- Step 1: Extract images from docx ---
    extract_dir = tempfile.mkdtemp(prefix='docx_extract_')
    print(f'[1/4] Extracting images from {docx_path}...')
    subprocess.run(['unzip', '-o', docx_path, '-d', extract_dir],
                   capture_output=True, check=True)

    media_dir = os.path.join(extract_dir, 'word', 'media')
    png_files = sorted(
        [f for f in os.listdir(media_dir) if f.endswith('.png')],
        key=lambda x: int(re.search(r'image(\d+)', x).group(1))
    )
    print(f'       Found {len(png_files)} images')
    if not png_files:
        print('ERROR: No images found in docx.')
        sys.exit(1)

    # --- Step 2: Convert to PDF ---
    import img2pdf
    pdf_path = os.path.join(out_dir, '_combined.pdf')
    print(f'[2/4] Converting {len(png_files)} images to PDF...')
    img_paths = [os.path.join(media_dir, f) for f in png_files]
    with open(pdf_path, 'wb') as f:
        f.write(img2pdf.convert(img_paths))
    print(f'       PDF: {pdf_path}')

    # --- Step 3: OCR with OCRmyPDF ---
    ocr_pdf_path = os.path.join(out_dir, '_combined_ocr.pdf')
    print(f'[3/4] Running OCRmyPDF (chi_sim+eng)...')
    subprocess.run([
        'ocrmypdf',
        '--language', 'chi_sim+eng',
        '--clean',
        '--tesseract-oem', '1',
        '--oversample', '450',
        '--tesseract-pagesegmode', '6',
        pdf_path, ocr_pdf_path
    ], check=True)
    print(f'       OCR done: {ocr_pdf_path}')

    # --- Step 4: Extract text and write markdown ---
    from PyPDF2 import PdfReader
    print(f'[4/4] Writing markdown files...')
    reader = PdfReader(ocr_pdf_path)

    for i, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or '').strip()
        md = f'# Image {i}\n\n```\n{text}\n```\n'
        fname = f'page_{i:02d}.md'
        with open(os.path.join(out_dir, fname), 'w', encoding='utf-8') as f:
            f.write(md)

    # Summary README
    lines = [
        '# OCR Results',
        '',
        f'Source: `{os.path.basename(docx_path)}` → {len(png_files)} images → OCRmyPDF (chi_sim+eng)',
        '',
        f'OCR\'d pages: {len(reader.pages)}',
        '',
        '| Page | File |',
        '|------|------|',
    ]
    for i in range(1, len(reader.pages) + 1):
        lines.append(f'| {i} | `page_{i:02d}.md` |')

    with open(os.path.join(out_dir, 'README.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    # Cleanup temp
    shutil.rmtree(extract_dir, ignore_errors=True)
    print(f'\nDone! {len(reader.pages)} pages -> {out_dir}')

if __name__ == '__main__':
    main()
