# Resume variant recipe (Windows, style-preserving docx surgery)

Goal: produce `张思浩-简历-v10-B01-AI应用开发.docx` (+PDF) from `...-v10-A01 .docx`
without touching layout. Worked example: `D:/找工作找工作/简历优化/_tools/build_b01.py`.

## 1. Recon: dump paragraph + run structure

```python
import re, zipfile
xml = zipfile.ZipFile(SRC).read('word/document.xml').decode('utf-8')
paras = re.findall(r'<w:p [^>]*?>.*?</w:p>', xml, re.S)   # numbered dump -> which index holds what
for m in re.finditer(r'<w:r(?:\s[^>]*)?>(.*?)</w:r>', para, re.S):  # per run: bold flag + w:t text
    ...
```

Facts from this template family:
- Every visible line appears **twice** (mc:Choice + mc:Fallback textboxes) => assert `count == 2`.
- Styles live at run level: `w:rPr` with 微软雅黑 / `w:sz w:val="18"` (9pt) / color 414141;
  bold runs differ only by `<w:b/>` and `w:cs="Times New Roman"`.
- Body text sits in fixed-geometry textboxes; long edits can overflow silently, so verify by
  rendering rather than by character count.

## 2. Edit

```python
REPL = [(old1, new1), ...]
for old, new in REPL:
    assert xml.count(old) == 2, old
    xml = xml.replace(old, new)
assert all(old not in xml for old, _ in REPL)
```

Write the zip back with the original entry order (`namelist()`), preserving every other part.

## 3. Render + verify

```bash
SOF="/c/Program Files/LibreOffice/program/soffice.exe"
"$SOF" --headless -env:UserInstallation=file:///D:/.../_render/loprofile \
  --convert-to pdf --outdir <render_dir> <docx>
```

- Page count without extra deps: `grep -a -o "/Type */Page[^s]" out.pdf | wc -l` and `grep -a -o "/Count [0-9]*" out.pdf`.
- Page-1 PNG for visual check: `--convert-to png:"writer_png_Export"`, then inspect with vision.
- Always look at the PNG: it is the only reliable way to catch textbox overflow / wrong-line placement.
