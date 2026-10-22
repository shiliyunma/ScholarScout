"""
ingest.py - 文档导入流水线（AI 嵌入版）

用法:
  .venv/Scripts/python.exe ingest.py                          # 导入 docs/ 下所有
  .venv/Scripts/python.exe ingest.py --reset                  # 清空重新导入
  .venv/Scripts/python.exe ingest.py "docs/xxx.pdf"           # 导入单个文件

支持: .txt .md .pdf .docx
"""

import argparse
import sys
from pathlib import Path

from config import (
    DOCS_DIR,
    INDEX_DIR,
    EMBED_MODEL_PATH,
    DEVICE,
    CHUNK_SIZE,
    CHUNK_OVERLAP,
    SUPPORTED_EXTENSIONS,
)


# ── 1. 文档加载器 ──────────────────────────────────

def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_markdown(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_pdf(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def load_docx(path: Path) -> str:
    from docx import Document
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs)


LOADERS = {
    ".txt":  load_text,
    ".md":   load_markdown,
    ".pdf":  load_pdf,
    ".docx": load_docx,
}


def load_document(path: Path) -> str | None:
    ext = path.suffix.lower()
    loader = LOADERS.get(ext)
    if loader is None:
        return None
    try:
        return loader(path)
    except Exception as e:
        print(f"  ⚠ 读取失败 [{path.name}]: {e}")
        return None


# ── 2. 语义预切分（正文/习题/解析） ────────────────

import re

_EXERCISE_MARKERS = re.compile(r"本节(试题|习题)精选")
_ANSWER_MARKER = re.compile(r"答案与解析")
_SECTION_HEADER = re.compile(r"^\s*\d+\.\d+\s")


def split_content_exercises(text: str) -> list[dict]:
    """拆为三种 type: content / exercise / solution"""
    segments: list[dict] = []
    current_type = "content"
    current_lines: list[str] = []

    def flush():
        text = "".join(current_lines).strip()
        if text:
            segments.append({"text": text, "type": current_type})
        current_lines.clear()

    for line in text.splitlines(keepends=True):
        if _EXERCISE_MARKERS.search(line):
            flush()
            current_type = "exercise"
            continue
        if _ANSWER_MARKER.search(line):
            flush()
            current_type = "solution"
            continue
        if _SECTION_HEADER.match(line):
            if current_type in ("exercise", "solution"):
                flush()
                current_type = "content"
            elif current_type == "content" and current_lines:
                flush()
        current_lines.append(line)
    flush()
    return segments


# ── 3. 分块 ────────────────────────────────────────

from langchain_text_splitters import RecursiveCharacterTextSplitter


def chunk_text(text: str, source: str) -> list[dict]:
    """先语义预切分，再分别处理三种 type。"""
    segments = split_content_exercises(text)

    seg_groups = {
        "content": [s for s in segments if s["type"] == "content"],
        "exercise": [s for s in segments if s["type"] == "exercise"],
        "solution": [s for s in segments if s["type"] == "solution"],
    }

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        length_function=len,
        separators=["\n\n", "\n", "。", ".", " ", ""],
    )

    chunks: list[dict] = []
    idx_counter = {"content": 0, "exercise": 0, "solution": 0}

    for seg_type in ("content", "exercise", "solution"):
        segs = seg_groups[seg_type]
        if not segs:
            continue
        all_text = "\n".join(s["text"] for s in segs)
        split_chunks = splitter.split_text(all_text)
        for chunk in split_chunks:
            i = idx_counter[seg_type]
            chunks.append({
                "text": chunk,
                "metadata": {
                    "source": str(source),
                    "type": seg_type,
                    "chunk_index": i,
                    "total_chunks": len(split_chunks),
                },
            })
            idx_counter[seg_type] += 1

    counts = {k: idx_counter[k] for k in ("content", "exercise", "solution")}
    parts = []
    if counts["content"]:
        parts.append(f"正文 {counts['content']} 片")
    if counts["exercise"]:
        parts.append(f"习题 {counts['exercise']} 片")
    if counts["solution"]:
        parts.append(f"解析 {counts['solution']} 片")
    if parts:
        print(f"    {' + '.join(parts)}")
    return chunks


# ── 4. 嵌入 + 写入 ChromaDB ──────────────────────

def build_index(file_paths: list[Path], reset: bool = False):
    print(f"[1/4] 加载嵌入模型: {EMBED_MODEL_PATH}  (device={DEVICE})")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(EMBED_MODEL_PATH, device=DEVICE)

    print(f"[2/4] 连接 ChromaDB → {INDEX_DIR}")
    import chromadb
    client = chromadb.PersistentClient(path=str(INDEX_DIR))

    if reset:
        try:
            client.delete_collection("docs")
            print("     已清空旧集合")
        except Exception:
            pass

    collection = client.get_or_create_collection(name="docs")

    # ── 遍历文件 ──
    all_chunks: list[dict] = []
    for fp in file_paths:
        print(f"  读取: {fp.name}")
        text = load_document(fp)
        if not text:
            continue
        rel_path = fp.relative_to(DOCS_DIR)
        chunks = chunk_text(text, source=str(rel_path))
        all_chunks.extend(chunks)
        print(f"    → {len(chunks)} 个片段")

    if not all_chunks:
        print("没有找到可处理的文档。")
        return

    # ── 流式分批嵌入 + 写入 ──
    BATCH_SIZE = 64
    total = len(all_chunks)
    batch_count = (total + BATCH_SIZE - 1) // BATCH_SIZE

    print(f"[3/4] 流式分批编码并写入 ({total} 条, {batch_count} 批)...")
    for batch_idx in range(batch_count):
        start = batch_idx * BATCH_SIZE
        end = min(start + BATCH_SIZE, total)
        batch = all_chunks[start:end]

        texts = [c["text"] for c in batch]
        metadatas = [c["metadata"] for c in batch]
        # ⚠️ ID 必须含 type，避免 content/exercise/solution 的 chunk_index 冲突
        ids = [f"{m['source']}#{m['type']}#{m['chunk_index']}" for m in metadatas]

        embeddings = model.encode(texts).tolist()

        collection.add(
            ids=ids,
            documents=texts,
            metadatas=metadatas,
            embeddings=embeddings,
        )

        print(f"  ✓ 批次 {batch_idx+1}/{batch_count} 写入完成 ({end}/{total})")

    print(f"\n✅ 完成！共导入 {total} 个片段到 {INDEX_DIR}")
    print(f"   使用设备: {model.device}")


# ── 入口 ───────────────────────────────────────────

def discover_files(paths: list[str]) -> list[Path]:
    if paths:
        return [Path(p) for p in paths if Path(p).is_file()]
    files = []
    for ext in SUPPORTED_EXTENSIONS:
        files.extend(DOCS_DIR.rglob(f"*{ext}"))
    return sorted(files)


def main():
    parser = argparse.ArgumentParser(description="文档导入 RAG 向量库")
    parser.add_argument("files", nargs="*", help="指定文件（留空则导入 docs/ 下所有）")
    parser.add_argument("--reset", action="store_true", help="清空向量库重新导入")
    args = parser.parse_args()

    files = discover_files(args.files)
    if not files:
        print(f"❌ 没找到任何文档（支持: {', '.join(SUPPORTED_EXTENSIONS)}）")
        print(f"   请把文件放到: {DOCS_DIR}")
        print(f"   或者: .venv/Scripts/python.exe ingest.py path/to/file.pdf")
        sys.exit(1)

    print(f"📂 发现 {len(files)} 个文件:")
    for f in files:
        print(f"   - {f}")
    print()

    build_index(files, reset=args.reset)


if __name__ == "__main__":
    main()
