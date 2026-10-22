# OCR Cleaning for Chinese Text

Full reference script for rule-based Chinese OCR text cleaning.

## Regex Pattern Reference

| Intent | Pattern | Replacement |
|--------|---------|-------------|
| Strip invisible control chars | `[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]` | `''` |
| Deduplicate Chinese punctuation | `([。，、；：？！．,.;:?!])\1+` | `\1` |
| Deduplicate dash/em-dash runs | `([\-\—]){3,}` | `\1\1` |
| Deduplicate bullet dots | `[·•]{3,}` | `·` |
| Remove isolated garbage chars | `[^\u4e00-\u9fff\uff00-\uffef...]` | `''` |
| Compress space between CJK | `([\u4e00-\u9fff])\s+([\u4e00-\u9fff])` | `\1\2` |
| Normalize blank lines | `\n{3,}` | `\n\n` |

## Safe Character Set for Chinese OCR

The following characters are preserved; everything else is removed:

- `\u4e00-\u9fff` — CJK Unified Ideographs (main Chinese characters)
- `\u3000-\u303f` — CJK Symbols and Punctuation
- `\uff00-\uffef` — Fullwidth Forms
- `a-zA-Z0-9` — Basic Latin alphanumeric
- `.,;:!?\-—()[]《》""''` — Common punctuation
- `+/%=#@_` — Programming-friendly symbols
- `\n\r\t ` — Whitespace and newlines

## Usage

```bash
# Preview changes only (no writes)
python scripts/clean_ocr.py --dry-run

# Overwrite originals in-place
python scripts/clean_ocr.py

# Save cleaned copies to a separate directory
python scripts/clean_ocr.py --output clean_docs/
```

## Full Script

See `E:\RAGLearn\scripts\clean_ocr.py` for the complete implementation.

Key structural features:
- `--dry-run` / `--output` args — safe for first runs
- Per-file stats + summary across all files
- UTF-8 encoding throughout
- Graceful exit if `docs/` dir missing or empty
