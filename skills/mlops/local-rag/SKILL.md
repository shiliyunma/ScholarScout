---
name: local-rag
description: "Build, configure, and troubleshoot personal/local RAG (Retrieval-Augmented Generation) systems. Covers the full pipeline: document loading, chunking, embedding, vector storage, retrieval, and LLM-powered answering. Includes China-specific network workarounds (HuggingFace blocked, hf-mirror mirror usage, Ollama DNS issues)."
version: 1.2.0
author: Hermes Agent
tags: [rag, retrieval, embeddings, vector-db, chromadb, sentence-transformers, china-network, hf-mirror]
---

# Local RAG

## Architecture

```
[Documents] → [Loading] → [Chunking] → [Embeddings] → [Vector DB] → [Retrieval] → [Answer with LLM]
             .pdf .txt .md .docx                         ChromaDB
```

## Quick Start (minimal working project)

```
project/
├── config.py         # paths, model, chunk params
├── ingest.py         # load docs → chunk → embed → store
├── query.py          # retrieve + answer
├── docs/             # put your documents here
├── models/           # local embedding model files
└── .venv/
```

### Config pattern (`config.py`)

```python
from pathlib import Path
import torch

PROJECT_ROOT = Path(__file__).parent
DOCS_DIR = PROJECT_ROOT / "docs"
INDEX_DIR = PROJECT_ROOT / "index"         # ChromaDB persistent dir

# Embedding model — local path preferred (no network dependency)
EMBED_MODEL_PATH = r"E:\path\to\models\bge-small-zh-v1.5"
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"   # auto-detect GPU

CHUNK_SIZE = 512
CHUNK_OVERLAP = 128
TOP_K = 5

# LLM — optional, for answer generation
LLM_BASE_URL = None   # e.g. "http://localhost:11434/v1"
LLM_API_KEY = "sk-xxx"
LLM_MODEL = "qwen2.5:7b"
```

### Stack

| Component | Library | Notes |
|-----------|---------|-------|
| Embedding | sentence-transformers | Loads from local path or HuggingFace name |
| Vector DB | ChromaDB | Embedded, no server, PersistentClient |
| Chunking | langchain-text-splitters | RecursiveCharacterTextSplitter |
| Document parsing | pypdf, python-docx, markdown | .txt .md .pdf .docx |
| LLM (optional) | openai | OpenAI-compatible API (Ollama, etc.) |

## Key Design Decisions

### Local model path over HuggingFace name
Always use a local path for the embedding model (e.g., `EMBED_MODEL_PATH = r"E:\RAGLearn\models\bge-small-zh-v1.5"`) rather than a HuggingFace model name (e.g., `"BAAI/bge-small-zh-v1.5"`). This avoids network dependencies at runtime and sidesteps HuggingFace access issues in restricted networks.

### Recommended embedding models for Chinese
- **bge-small-zh-v1.5** (384 dim, ~91MB) — lightweight, fast on CPU, good Chinese
- **bge-base-zh-v1.5** (768 dim, ~400MB) — more accurate

## China-Specific Network Workarounds

HuggingFace (`huggingface.co`) is blocked in mainland China. Ollama's registry (`registry.ollama.ai`) may also suffer DNS resolution failures. Known workarounds:

### Workaround 1: Download model files via curl from hf-mirror
`hf-mirror.com` is accessible (curl works, even when Python's `transformers`/`huggingface_hub` SSL connections fail).

```bash
BASE="https://hf-mirror.com/BAAI/bge-small-zh-v1.5/resolve/main"
mkdir -p models/bge-small-zh-v1.5/1_Pooling

# Config files (small)
for f in config.json config_sentence_transformers.json modules.json \
         sentence_bert_config.json special_tokens_map.json tokenizer.json \
         tokenizer_config.json vocab.txt; do
  curl -sOL "$BASE/$f"
done
curl -sL "$BASE/1_Pooling/config.json" -o 1_Pooling/config.json

# Model weights (large ~91MB for small, ~400MB for base)
curl -L -o model.safetensors "$BASE/model.safetensors"
```

Then point `EMBED_MODEL_PATH` to the local directory.

### Workaround 2: HuggingFace mirror env var
```bash
export HF_ENDPOINT=https://hf-mirror.com
```
Note: `huggingface_hub` library may still fail with SSL errors even when curl works. Prefer Workaround 1 for reliability.

### Workaround 3: Manual browser download
Download from https://hf-mirror.com/BAAI/bge-small-zh-v1.5 in a browser, save files locally.

### Ollama in China (registry.ollama.ai DNS-blocked)

Three approaches to get Ollama running with Qwen models when the default registry is unreachable.

#### Approach A: Use ModelScope2Registry proxy (easiest, no manual files)

A community-maintained Azure proxy service wraps ModelScope models into the Ollama registry format. Set the env var so all `ollama pull`/`ollama run` commands route through it:

```bash
# Windows: set system env var OLLAMA_HOST=https://modelscope2ollama-registry.azurewebsites.net
# Then run like normal:
ollama run qwen2.5:7b
```

Or use the full URL directly in a single command:

```bash
ollama run modelscope2ollama-registry.azurewebsites.net/qwen/Qwen2.5-7B-Instruct-gguf:Q8_0--qwen2
```

Supported models include Qwen2.5, DeepSeek, and other popular GGUF models mirrored on ModelScope. The proxy is open-source: https://github.com/onllama/Onllama.ModelScope2Registry

Caveat: this is a community Azure App Service — availability not guaranteed.

#### Approach B: Manual GGUF download + Modelfile (most reliable, no proxy dependency)

Download the GGUF file from hf-mirror.com or ModelScope, create a Modelfile, and import into Ollama.

```bash
# 1. Download Qwen2.5 7B GGUF (Q4_K_M ~4.5GB)
curl -L -o qwen2.5-7b-instruct-q4_k_m.gguf \
  "https://hf-mirror.com/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q4_k_m.gguf?download=true"

# 2. Create Modelfile
cat > Modelfile << 'EOF'
FROM ./qwen2.5-7b-instruct-q4_k_m.gguf
TEMPLATE """{{ if .System }}<|im_start|>system
{{ .System }}<|im_end|>
{{ end }}<|im_start|>user
{{ .Prompt }}<|im_end|>
<|im_start|>assistant
"""
PARAMETER temperature 0.7
PARAMETER top_p 0.8
PARAMETER stop "<|im_start|>"
PARAMETER stop "<|im_end|>"
EOF

# 3. Import and run
ollama create qwen2.5-7b -f Modelfile
ollama run qwen2.5-7b

# Or download from ModelScope (may be faster in China)
curl -L -o qwen2.5-7b-instruct-q4_k_m.gguf \
  "https://modelscope.cn/api/v1/models/qwen/Qwen2.5-7B-Instruct-GGUF/repo?Revision=master&FilePath=qwen2.5-7b-instruct-q4_k_m.gguf"
```

#### Approach C: Use ModelScope official Ollama integration

ModelScope now officially supports Ollama integration. See:
https://modelscope.cn/docs/Ollama-集成
This is the most future-proof approach — check the docs for the latest instructions.

#### Recommended Qwen2.5 GGUF sizes

| Model | Size (Q4_K_M) | Min RAM |
|-------|---------------|---------|
| qwen2.5:0.5b | ~350MB | Any |
| qwen2.5:1.5b | ~1GB | Any |
| qwen2.5:3b | ~2GB | 4GB |
| qwen2.5:7b | ~4.5GB | 8GB |
| qwen2.5:14b | ~9GB | 16GB |
| qwen2.5:32b | ~20GB | 32GB |
| qwen2.5:72b | ~46GB | 64GB |

On a modern laptop CPU (e.g., i9-13900HX), Q4_K_M of 7B or 14B provides the best speed/quality tradeoff.

## Performance Optimization

### CUDA Auto-Detection

Instead of hardcoding `DEVICE = "cpu"`, use auto-detection so the RAG system transparently uses GPU when available:

```python
import torch
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
```

**Expected speedup** (bge-small-zh, 384d):
- CPU (modern laptop): ~50-100 chunks/sec
- GPU (RTX 4050 laptop): ~500-800 chunks/sec
- For 4,600 chunks (4 x 44万字 documents): CPU ~5-10min -> GPU ~30-60sec

### Batch Streaming for Large Ingestion

When ingesting many documents, the naive approach (encode all chunks at once then write) can OOM. Batch streaming processes chunks in small groups and writes incrementally:

```python
BATCH_SIZE = 64                    # tune based on GPU memory
total = len(all_chunks)
batch_count = (total + BATCH_SIZE - 1) // BATCH_SIZE

for batch_idx in range(batch_count):
    start = batch_idx * BATCH_SIZE
    end = min(start + BATCH_SIZE, total)
    batch = all_chunks[start:end]

    texts = [c["text"] for c in batch]
    metadatas = [c["metadata"] for c in batch]
    ids = [f"{m['source']}#{m['chunk_index']}" for m in metadatas]

    embeddings = model.encode(texts).tolist()
    collection.add(ids=ids, documents=texts, metadatas=metadatas, embeddings=embeddings)

    print(f"  Batch {batch_idx+1}/{batch_count} ({end}/{total})")
```

**Benefits:** low memory, crash-safe (data already persisted in written batches), usable progress feedback per batch.

### Capacity Estimation

| Parameter | Typical Value | Limit Source |
|-----------|---------------|-------------|
| Chars per chunk | 512 (CHUNK_SIZE) | Configurable; model max ~700 chars (512 tokens) |
| Overlap | 128 (overlap) | Configurable; prevents mid-sentence cut |
| Vector dims | 384 (bge-small) | Model architecture |
| Total entries | Up to ~1M chunks | ChromaDB/SQLite practical limit |
| Embedding memory | ~2KB/chunk (vector+text) | 50MB for 25K chunks |
| Disk | ~2KB/chunk persisted | 250K chunks ~500MB-1GB |

**Rule of thumb:** bge-small-zh (384d) handles ~1M chunks before precision degrades noticeably. Upgrade to bge-large-zh (1024d) or add a cross-encoder reranker beyond that.

## Dependencies (`requirements.txt`)

```
chromadb>=0.6.0
sentence-transformers>=3.0.0
pypdf>=5.0.0
python-docx>=1.1.0
markdown>=3.7
langchain-text-splitters>=0.3.0
openai>=1.0.0
```

## Usage

```bash
# Activate venv
.venv\Scripts\python.exe ingest.py              # scan and index docs/
.venv\Scripts\python.exe ingest.py --reset       # clear and rebuild

# Query
.venv\Scripts\python.exe query.py "your question"
.venv\Scripts\python.exe query.py               # interactive mode
.venv\Scripts\python.exe query.py --raw "..."   # skip LLM, show source only
```

## Pitfalls

- **HuggingFace blocked in China**: do NOT use model names like `"BAAI/bge-small-zh-v1.5"` directly — use a local path after downloading via curl from hf-mirror
- **hf-mirror SSL in Python**: `transformers`/`huggingface_hub` may fail with SSL connection errors even when `curl` works. Don't waste time debugging — use Workaround 1 (curl download -> local path)
- **Ollama DNS failure in China**: `registry.ollama.ai` may not resolve. DO NOT skip Ollama — use the workarounds in the "Ollama in China" section above (ModelScope2Registry proxy, manual GGUF+Modelfile, or ModelScope official integration)
- **Windows paths in docstrings**: paths like `E:\RAGLearn\...` in Python docstrings trigger `SyntaxWarning: invalid escape sequence`. Use raw strings (`r"..."`) or forward slashes
- **System Python vs venv Python**: always use `.venv\Scripts\python.exe` explicitly. Running `python ingest.py` without the venv prefix may pick up system Python which lacks dependencies
- **ChromaDB PersistentClient**: use `chromadb.PersistentClient(path=str(INDEX_DIR))` — don't use the old `Client()` without a path, or data won't persist
- **All-at-once encoding OOM on large docs**: avoid `model.encode(all_texts)` for 10K+ chunks. Use batch streaming (see Performance Optimization) — encode 64 chunks at a time and write to ChromaDB incrementally
- **Venv torch CPU-only despite system torch having CUDA**: After setting `DEVICE = "cuda" if torch.cuda.is_available() else "cpu"`, ingestion may still show "cpu" because the *venv*'s torch is `torch 2.x.x+cpu` (CPU-only). The system Python's torch may have CUDA, but the venv's torch is a separate installation. Fix: delete the venv's CPU torch and reinstall from the CUDA index:
  ```bash
  rm -rf .venv/Lib/site-packages/torch* .venv/Lib/site-packages/torchgen*
  uv pip install torch --index-url https://download.pytorch.org/whl/cu121 --python .venv/Scripts/python.exe
  ```
  Note: torch CUDA is 2.3GB — use a background process with a high timeout.
- **Venv missing pip**: Some venvs lack pip.exe. `python -m pip` gives `No module named pip`. Fix: use `uv pip install` with `--python .venv/Scripts/python.exe` to target the venv directly.
- **Hardcoded DEVICE="cpu"**: if user has NVIDIA GPU but DEVICE is hardcoded to "cpu", ingestion is 10-50x slower than necessary. Use auto-detection
- **ChromaDB ID collision when using multiple chunk types**: If your `ingest.py` assigns chunks different `type` metadata (e.g. `content` vs `exercise` vs `solution`), each type has its own `chunk_index` counter starting from 0. Using `f"{source}#{chunk_index}"` as the ChromaDB ID causes **silent overwrite** of earlier chunks by later ones when IDs collide — `collection.count()` will be less than the total imported. **Fix**: include type in the ID: `f"{source}#{type}#{chunk_index}"`. Only necessary when chunks of different types share the same source prefix.

## Related sub-skills

- **`rag-exercise-separation`** — 三类型语义预切分（content/exercise/solution），将王道考研教材的正文知识点、练习题题干、答案解析分开。同一 ChromaDB collection 内通过 metadata type 字段 + `$in` 过滤器实现双模式检索：知识模式搜 content+solution，习题模式搜 exercise+solution。`ingest.py` 中通过正则识别 `本节(试题|习题)精选`、`答案与解析` 和章节标题边界进行切分。`query.py` 支持 `--practice` 参数切换到习题检索模式。

## RAG Accuracy Optimization (beyond baseline)

Once you have a working RAG pipeline (Chunk → Embed → Retrieve → Rerank → LLM), further accuracy gains require new mechanisms. Listed by impact-to-effort ratio.

### Level 1: Small changes, noticeable improvement

#### 🎯 Hybrid Search (BM25 + Embedding)

Dense retrieval (embedding) excels at semantic similarity but misses exact keyword matches crucial for technical terms. Sparse retrieval (BM25) is the opposite. Combine them:

```
final_score = α × embedding_cosine + (1-α) × bm25_score
```

**When it helps**: terminology-heavy queries ("反向传播公式", "卷积层参数计算"), proper nouns, code snippets. BM25 pins the exact paragraph while embedding provides semantically similar alternatives.

**Effort**: ~half day. Add `rank_bm25` package. Build BM25 index during ingest (persist to pickle). Weighted merge in query, then pass to Reranker as usual.

**Recommended α**: 0.5–0.7 (leaning toward embedding, but letting BM25 correct misses).

#### 🎯 Query Rewrite

Users often ask ambiguous or verbose questions. Before retrieval, ask the LLM to reformulate the user query into 2–3 independent search queries targeting different angles:

```
用户: "神经网络怎么调学习率"
→ LLM rewrites to:
  1. "学习率调整策略 神经网络"
  2. "learning rate scheduling 效果对比"
  3. "Adam SGD 学习率设置建议"
```

Each query retrieves independently → merge, deduplicate, Rerank.

**When it helps**: vague questions, cross-topic queries, or when the user's natural language doesn't match the document's technical vocabulary.

**Effort**: ~dozens of lines in query.py. One extra LLM call per query (fast, small output). Risk: token cost doubles.

### Level 2: Medium effort, richer context

#### 🎯 Parent Document Retriever

Current problem: a 512-char chunk may lack surrounding context ("如前所述..."). Solution: two-tier chunking.

```
Original paragraph (~2000 chars) → Parent chunk (stored as metadata)
  ↓
Child chunks (~200 chars) → vectorized for retrieval
```

Retrieval: find matching child → group by parent → feed the full parent text to the LLM.

**When it helps**: textbooks with strong cross-reference dependencies ("如上一节所述"), or when individual chunks are too terse to answer standalone.

**Effort**: ~half day. Modify ingest to store parent text in chunk metadata. Modify query to deduplicate by parent after Reranker.

**Cost**: LLM token usage increases since full parent text is sent.

#### 🎯 Metadata Filtering

Attach chapter/section tags to each chunk during ingest:

```python
metadata = {
    "type": "content|exercise|solution",
    "chapter": "第三章 卷积神经网络",
    "section": "3.2 卷积层",
    "has_formula": True,
}
```

During retrieval, use ChromaDB's `where` clause to restrict search to relevant sections:

```python
collection.query(where={"chapter": "第三章 卷积神经网络"})
```

**When it helps**: user specifies a chapter, or the system auto-detects which section the question belongs to. Smaller candidate set → higher precision.

**Effort**: depends on document structure. If your docs have clear chapter headers, this is straightforward regex parsing.

### Level 3: Biggest gains, highest complexity

#### 🎯 Multi-round Retrieval (Self-RAG)

Don't settle for a single Top-5. Let the LLM examine retrieved chunks and decide: is this enough? If not, generate a follow-up query and retrieve again.

```
Round 1: retrieve Top-5 → LLM examines → "missing convolution formula"
  ↓ LLM generates
"卷积层前向传播公式" → Round 2: retrieve → merge with Round 1 → final answer
```

**When it helps**: complex questions spanning multiple knowledge points that no single query can cover.

**Effort**: significant. Risk: latency doubles or triples. Add a timeout fail-safe.

### Decision guide

| If you need… | Start with |
|--------------|------------|
| Better exact-match recall | Hybrid Search |
| Better precision on vague queries | Query Rewrite |
| Richer context for LLM | Parent Document Retriever |
| Fewer irrelevant candidates | Metadata Filtering |
| Multi-hop reasoning | Multi-round Retrieval |

## Document Preprocessing — OCR Cleaning

When source documents are OCR outputs from scanned textbooks (common in Chinese RAG), they contain noise that degrades retrieval quality. A two-phase approach is recommended.

### Phase 1: Rule-based cleaning (recommended, 0 cost)

Cleans ~80% of garbage: invisible control chars, duplicate punctuation, isolated symbol junk, intra-Chinese extra whitespace.

**Common Chinese OCR noise types**:

| Noise type | Example | Fix |
|-----------|---------|-----|
| Invisible chars | `\x00\x01` | Strip regex |
| Duplicate punctuation | `。。。。。` | Deduplicate to one |
| Random symbols | `@#$%^&*` | Remove outside charset |
| Extra spaces inside Chinese | `人 工智 能` | Remove spaces between CJK |
| Wrong whitespace around English | `Le α rning` | Preserve English word spacing |

**Script structure** (`scripts/clean_ocr.py`):

```python
def clean_ocr(text: str) -> str:
    # 1. Remove control chars (keep \t \n \r)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    # 2. Deduplicate punctuation
    text = re.sub(r'([。，、；：？！．,.;:?!])\1+', r'\1', text)
    # 3. Remove isolated junk outside safe charset
    KEEP = r'\u4e00-\u9fff\uff00-\uffef...'
    text = re.sub(f'[^{KEEP}]', '', text)
    # 4. Compress spaces between CJK chars
    text = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', text)
    # 5. Collapse excess blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()
```

**Supporting modes**: `--dry-run` (preview without writing), `--output DIR` (preserve originals).

**Typical effect**: ~1–2% of total chars removed for textbook OCR. Higher for low-quality scans.

### Phase 2: LLM-based polishing (optional, costs tokens)

After rules, send each chunk to a local LLM for semantic-level correction: fix similar-looking characters (伸→伸, α→a), repair broken words, remove contextual garbage that rules can't catch.

**Prompt**:
```
system: You are an OCR post-processor. Fix OCR errors: correct similar-looking
        characters, repair broken words, remove garbled text. Preserve the
        original paragraph structure. Do NOT add or rewrite content.
```

**Risk**: LLM may "helpfully" rephrase content (making it more fluent but changing meaning). Strict prompt constraints are essential.

### Recommendation

Start with Phase 1 only — it's zero-cost, fast, and handles most noise. Only add Phase 2 if answers remain clearly garbled.

## References

- `references/download-gguf-for-ollama.md` — detailed GGUF download steps
- `references/download-models-from-hf-mirror.md` — model weight download scripts
- `references/ocr-cleaning-for-chinese-text.md` — reference: full clean_ocr.py script and regex pattern table
- `references/rag-accuracy-checklist.md` — reference: decision flowcharts and tuning guide for each accuracy strategy
