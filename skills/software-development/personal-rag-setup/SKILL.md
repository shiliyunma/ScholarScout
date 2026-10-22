---
name: personal-rag-setup
description: "搭建个人本地 RAG 系统：文档导入、AI 嵌入、语义检索"
version: 1.3.0
author: Hermes Agent
platforms: [windows]
tags: [rag, chromadb, embedding, local-ai, vector-search]
---

# 个人 RAG 搭建

搭建一套完全本地运行的 RAG（检索增强生成）系统，支持中文文档语义检索。

## 触发条件

当用户说以下内容时加载本技能：
- "搭建 RAG"
- "个人知识库"
- "文档检索"
- "本地 RAG"
- "向量数据库"

## 技术选型

| 组件 | 推荐 | 备选 |
|------|------|------|
| 向量库 | ChromaDB（嵌入式，无需服务） | LanceDB, sqlite-vec |
| 嵌入模型 | BAAI/bge-small-zh-v1.5（512维） | bge-large-zh-v1.5, bce-embedding-base_v1 |
| 分块 | RecursiveCharacterTextSplitter | 段落分割 |
| 检索 | 向量搜索（L2距离） | 加 reranker 精排后取 top-5 |
| LLM（可选） | Ollama + Qwen2.5-3B | OpenAI 兼容接口 |

## 步骤

### 1. 创建项目

```bash
mkdir -p /path/to/project/docs /path/to/project/models
cd /path/to/project
```

创建虚拟环境并安装依赖：

```bash
uv venv .venv
uv pip install chromadb sentence-transformers pypdf python-docx langchain-text-splitters openai
```

### 2. 下载嵌入模型

因 HuggingFace 被墙，使用国内镜像 hf-mirror.com 下载：

**⚠ 先检查模型格式：** 用 curl 确认模型是 safetensors 还是 .bin 格式。.bin 格式需要 torch >= 2.6，safetensors 无此限制。

```bash
# 检查模型文件格式
curl -sL "https://hf-mirror.com/api/models/BAAI/bge-small-zh-v1.5" | python -c "import sys,json; d=json.load(sys.stdin); safetensors=[s['rfilename'] for s in d.get('siblings',[]) if s['rfilename'].endswith('.safetensors')]; print('safetensors数量:', len(safetensors))"
```

**方式一：设置环境变量（推荐先试这个）**
```bash
export HF_ENDPOINT=https://hf-mirror.com
# 然后 Python 代码会自动从镜像下载
```

**方式二：手动下载（环境变量 SSL 失败时使用）**
```bash
MODEL_DIR="models/bge-small-zh-v1.5"
mkdir -p "$MODEL_DIR/1_Pooling"

BASE="https://hf-mirror.com/BAAI/bge-small-zh-v1.5/resolve/main"

# 配置文件
for f in config.json config_sentence_transformers.json modules.json \
         sentence_bert_config.json special_tokens_map.json \
         tokenizer.json tokenizer_config.json vocab.txt; do
  curl -sL "$BASE/$f" -o "$MODEL_DIR/$f"
done
curl -sL "$BASE/1_Pooling/config.json" -o "$MODEL_DIR/1_Pooling/config.json"

# 权重文件（~90MB，可直接下载）
curl -L "$BASE/pytorch_model.bin" -o "$MODEL_DIR/pytorch_model.bin"
```

大模型（1GB+）用后台下载避免超时：
```bash
curl -L "$BASE/pytorch_model.bin" -o "$MODEL_DIR/pytorch_model.bin" &
```

### 3. 编写核心文件

创建以下四个文件：

**config.py** - 统一配置中心

```python
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
DOCS_DIR = PROJECT_ROOT / "docs"
INDEX_DIR = PROJECT_ROOT / "index"

EMBED_MODEL_PATH = r"E:\RAGLearn\models\bge-small-zh-v1.5"
import torch
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
CHUNK_SIZE = 512
CHUNK_OVERLAP = 128
TOP_K = 5

# LLM（可选，Ollama 本地模型）
LLM_BASE_URL = "http://localhost:11434/v1"
LLM_API_KEY = "ollama"
LLM_MODEL = "modelscope.cn/qwen/Qwen2.5-3B-Instruct-GGUF"

SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx"}
```

**ingest.py** - 文档导入（流式分批版）

关键点：
- 递归扫描 docs/ 下所有子文件夹
- 读取 .txt/.md/.pdf/.docx 文件
- 切块（RecursiveCharacterTextSplitter, chunk_size=512, overlap=128）
- 流式分批编码：每 64 块一批，编码后立即写入

核心代码：
```python
BATCH_SIZE = 64
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
    
    print(f"  批次 {batch_idx+1}/{batch_count} 完成 ({end}/{total})")
```

**query.py** - 检索 + 问答（模型预加载版）

关键点：嵌入模型只在 main() 启动时加载一次，所有查询复用。

```python
def main():
    print("加载嵌入模型...")
    model = SentenceTransformer(EMBED_MODEL_PATH, device=DEVICE)
    client = chromadb.PersistentClient(path=str(INDEX_DIR))
    collection = client.get_collection("docs")
    
    if not args.query:
        interactive(model, collection)  # 交互模式，模型保持加载
        return
    
    query = " ".join(args.query)
    docs = retrieve(query, model, collection)
    print_results(query, docs)

def retrieve(query, model, collection):
    q_embedding = model.encode([query]).tolist()[0]
    results = collection.query(query_embeddings=[q_embedding], n_results=TOP_K)
    ...
```

### 4. 可选：加 Reranker（提升检索精度）

检索时先取 20 条，用交叉编码器精排后取前 5：

```python
from sentence_transformers import CrossEncoder

RERANKER_MODEL_PATH = r"E:\RAGLearn\models\bge-reranker-base"
RETRIEVAL_TOP_K = 20  # 初始检索更多

reranker = CrossEncoder(RERANKER_MODEL_PATH, device=DEVICE)

# 先用嵌入模型检索 20 条
initial_results = collection.query(query_embeddings=[q_embedding], n_results=RETRIEVAL_TOP_K)

# reranker 精排
pairs = [[query, doc["text"]] for doc in initial_docs]
scores = reranker.predict(pairs)
ranked = sorted(zip(initial_docs, scores), key=lambda x: x[1], reverse=True)
final_docs = [doc for doc, _ in ranked[:TOP_K]]
```

推荐 reranker：
- bge-reranker-base（1.1GB，safetensors，无需 torch>=2.6）
- bge-reranker-v2-m3（2.1GB，多语言，质量更好）

### 5. 使用

```bash
# 建索引
.venv\Scripts\python.exe ingest.py

# 清空重建
.venv\Scripts\python.exe ingest.py --reset

# 提问
.venv\Scripts\python.exe query.py "你的问题"

# 交互模式
.venv\Scripts\python.exe query.py
```

## LLM 提示词优化（小模型专用）

3B 类模型容易偷懒只复述第一条片段。用好提示词：

```python
SYSTEM_PROMPT = """你是一个基于本地知识库的问答助手。
请综合下面所有的参考文档片段，给出一个完整、详细的回答。
要求：
1. 涵盖各个片段提到的不同方面，不要只取其中一段
2. 如果片段之间有互补信息，合并成连贯的叙述
3. 用清晰的段落组织答案，适当分点说明
4. 引用来源时标注文件名
5. 如果文档中没有相关信息，直接说"文档中没有相关信息"，不要编造"""
```

关键：明确说"综合所有片段"、"不要只取一段"、"分点说明"。

## 常见问题

### 第一次查询很慢（10-15秒），之后秒回

**原因**：retrieve() 函数每次调用都重新加载嵌入模型。修复：在 main() 中一次性加载，传给 retrieve() 复用。

### CUDA 装了但跑出来还是 CPU

虚拟环境里的 torch 可能是 CPU 版。修复：

```bash
# 删掉 CPU 版
rm -rf .venv/Lib/site-packages/torch*
# 用 CUDA 索引重装
uv pip install torch --index-url https://download.pytorch.org/whl/cu121 --python .venv/Scripts/python.exe
```

或用系统已装好的 CUDA torch 直接复制（更快，需同 Python 版本）。

### 虚拟环境没有 pip

uv 创建的 venv 默认不带 pip。用 uv 代替：

```bash
uv pip install <package> --python .venv/Scripts/python.exe
```

### 不要误把 `openai` 包说成依赖 OpenAI 服务

项目里用了 `from openai import OpenAI`，但 `base_url` 指向 `http://localhost:11434/v1`（Ollama）。`openai` Python 包只是**通用 OpenAI 兼容 API 客户端**，不代表依赖 OpenAI 云服务。

描述依赖时要说准：
- ✅ "`openai` — 调本地 Ollama（Qwen2.5-3B）的 API 客户端"
- ❌ "`openai` — 调 OpenAI 的 API"

用户会纠正你。

### 不要乱杀进程切环境

服务（Streamlit 等）已经在系统 Python 上跑着，报 `ModuleNotFoundError` 时，正确做法：

1. **确认服务用的是什么 Python**（看启动命令用的是 `python` 还是 `.venv/Scripts/python.exe`）
2. **在同一个环境里装缺少的包** — 先别杀进程
3. **装好了再杀 & 重启**

不要杀掉运行中的服务，然后从另一个环境（比如从系统 Python 切到 venv）重启。新环境往往缺更多包，导致连锁失败。用户会问"为什么需要安装，之前的接口不可以用吗"——他们有道理。

**当用户明确要求切到 venv**（"你把需要的库直接从系统库移植到这里"），用复制代替重装：

```bash
# 找系统 site-packages
python -c "import site; print([p for p in site.getsitepackages() if 'site-packages' in p][0])"
# → C:\Users\<user>\AppData\Local\Programs\Python\Python312\Lib\site-packages

# 找 venv site-packages
.venv/Scripts/python -c "import site; print([p for p in site.getsitepackages() if 'site-packages' in p][0])"
# → <project>/.venv/Lib/site-packages

# 先查 venv 缺什么
for pkg in pkg1 pkg2 ...; do
  .venv/Scripts/python -c "import $pkg" 2>&1 || echo "MISSING: $pkg"
done

# 只复制缺的（纯 Python 包直接 cp 就行）
cp -r /c/Users/.../Python312/Lib/site-packages/pymysql .venv/Lib/site-packages/pymysql
```

### torch 版本限制导致无法加载 .bin 模型

transformers 新版本要求 torch >= 2.6 才能加载 .bin（pickle）格式权重（CVE-2025-32434）。

**最佳方案**：选 safetensors 格式的模型（如 bce-embedding-base_v1、bge-reranker-base）。

### Ollama 下载模型

registry.ollama.ai 被 DNS 污染，可用以下方案。

**方案一（不推荐）：ModelScope 网关**

```bash
ollama pull modelscope.cn/qwen/Qwen2.5-3B-Instruct-GGUF
```

⚠ **ModelScope 上的 GGUF 文件有中文编码 bug** — 用中文问问题会输出英文/乱码。所有 Qwen 版本（0.5B、3B）都有此问题。仅在验证 LLM 通路是否连通时临时使用。

**方案二（推荐）：从 hf-mirror 下载官方 GGUF 并本地导入**

```bash
# 1. 下载官方 GGUF（~1.8GB，后台下载避免超时）
cd models/
curl -L "https://hf-mirror.com/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf" \
  -o qwen2.5-3b-q4_k_m.gguf &

# 2. 创建 Modelfile 并导入 Ollama
cat > Modelfile << 'MODEL_EOF'
FROM ./qwen2.5-3b-q4_k_m.gguf
TEMPLATE """{{- if .System }}{{ .System }}{{ end }}
{{- range .Messages }}
{{- if eq .Role "user" }}user: {{ .Content }}
{{- else if eq .Role "assistant" }}assistant: {{ .Content }}
{{- end }}
{{- end }}"""
PARAMETER temperature 0.3
PARAMETER num_ctx 4096
MODEL_EOF

ollama create qwen2.5-3b-local -f Modelfile

# 3. 更新 config.py
# LLM_MODEL = "qwen2.5-3b-local"
```

**报错 "digest mismatch"** → 文件损坏（后台下载被中断），重新下载即可：
```bash
rm -f qwen2.5-3b-q4_k_m.gguf
curl -L "https://hf-mirror.com/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf" \
  -o qwen2.5-3b-q4_k_m.gguf
ollama create qwen2.5-3b-local -f Modelfile
```

**方案三：系统代理**（Ollama 会遵从系统 HTTP_PROXY）

### ChromaDB 报 DuplicateIDError

ID 用相对路径而非纯文件名。修复：

```python
rel_path = fp.relative_to(DOCS_DIR)
ids = [f"{rel_path}#{chunk_index}" ...]
```
