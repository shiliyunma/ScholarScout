#!/usr/bin/env python3
"""
docx_ocr_pipeline.py — Extract images from a DOCX file and OCR them with PaddleOCR.

Usage:
    python scripts/docx_ocr_pipeline.py report.docx [--output-dir ocr_results]

Requires: paddleocr, paddlepaddle

Output: Markdown files per image, ordered by extraction sequence.
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def extract_images_from_docx(docx_path: Path, extract_dir: Path) -> list[Path]:
    """Unzip the docx and return paths to all images in word/media/."""
    subprocess.run(
        ["unzip", "-o", str(docx_path.resolve()), "-d", str(extract_dir)],
        capture_output=True,
        check=True,
    )
    media_dir = extract_dir / "word" / "media"
    if not media_dir.exists():
        print(f"Error: No word/media/ found in {docx_path}", file=sys.stderr)
        return []
    return sorted(media_dir.glob("*"))


def ocr_images(image_paths: list[Path], output_dir: Path, lang: str = "ch"):
    """Run PaddleOCR on all images and write per-image markdown files."""
    try:
        from paddleocr import PaddleOCR
    except ImportError:
        print("Error: Install paddleocr: pip install paddleocr paddlepaddle", file=sys.stderr)
        sys.exit(1)

    ocr = PaddleOCR(use_angle_cls=True, lang=lang)
    output_dir.mkdir(parents=True, exist_ok=True)

    for i, img_path in enumerate(image_paths, 1):
        print(f"[{i}/{len(image_paths)}] OCR: {img_path.name} ...", end=" ", flush=True)
        result = ocr.ocr(str(img_path))

        lines = []
        for line_group in result:
            if not line_group:
                continue
            for line in line_group:
                lines.append(f"- {line[1][0]}  _(conf={line[1][1]:.2f})_")

        md = f"# {img_path.name}\n\n" + "# {img_path.name}\n\n"
        md = f"# {img_path.name}\n\n"
        md += f"Size: {img_path.stat().st_size // 1024}KB\n\n"
        md += "\n".join(lines) if lines else "_No text recognized._"

        out_file = output_dir / f"{i:02d}_{img_path.stem}.md"
        out_file.write_text(md, encoding="utf-8")
        print(f"→ {out_file.name} ({len(lines)} lines)")

    print(f"\nDone. {len(image_paths)} files → {output_dir}")


def main():
    parser = argparse.ArgumentParser(description="OCR images from a DOCX file")
    parser.add_argument("docx", type=Path, help="Path to the .docx file")
    parser.add_argument(
        "--output-dir", "-o",
        type=Path,
        default=Path("实验结果_OCR"),
        help="Output directory for markdown files (default: 实验结果_OCR)",
    )
    parser.add_argument(
        "--lang", default="ch",
        help="OCR language (default: ch for Chinese+English)",
    )
    parser.add_argument(
        "--keep-extract",
        action="store_true",
        help="Keep the temporary extraction directory",
    )
    args = parser.parse_args()

    if not args.docx.exists():
        print(f"Error: File not found: {args.docx}", file=sys.stderr)
        sys.exit(1)

    with tempfile.TemporaryDirectory(prefix="docx_ocr_") as tmp_dir:
        extract_dir = Path(tmp_dir)
        images = extract_images_from_docx(args.docx, extract_dir)

        if not images:
            sys.exit(1)

        print(f"Extracted {len(images)} image(s) from {args.docx.name}:")
        for img in images:
            print(f"  {img.name} ({img.stat().st_size // 1024}KB)")

        ocr_images(images, args.output_dir, lang=args.lang)

        if args.keep_extract:
            keep_dir = args.output_dir / "_extracted_images"
            if keep_dir.exists():
                shutil.rmtree(keep_dir)
            shutil.copytree(extract_dir / "word" / "media", keep_dir)
            print(f"Raw images kept: {keep_dir}")


if __name__ == "__main__":
    main()
