---
title: RAG 知识/习题双库分离方案
name: rag-exercise-separation
description: 使用语义预切分将教材正文和习题段分离，在同一 ChromaDB collection 中通过 metadata type 字段实现双模式检索。
tags: [rag, chromadb, semantic-chunking, exercise-separation, bge-embedding, textbook]
category: mlops
---

# RAG 知识/习题双库分离方案

## 适用场景

王道考研教材（或其他结构化教材）的 RAG 系统，希望将正文知识点和章末练习题分开检索互不干扰。

## 教材结构识别

王道教材的通用结构：
- `X.X 本节(试题|习题)精选` — 练习题区域起点（标记行之后跟大量选择题/综合题）
- `X.X 答案与解析` — 答案解析区域起点（含详细解题步骤、总结，有价值的知识）
- `X.X 新章节标题` — 新一节内容起点，关闭上一个段

## 核心逻辑：三类型语义预切分

在 `RecursiveCharacterTextSplitter` 之前加一道预处理，将文档拆为三种 type：

```
文本流:
  [正文...] → [本节习题精选] → [题目...] → [答案与解析] → [解答...] → [1.2 下一节标题] → [正文...]
     ↑ content      ↑ exercise ────────┘↑ solution ───────┘   ↑ 切回 content
```

关键规则：
1. `本节(试题|习题)精选` → 切到 exercise
2. `答案与解析` → 切到 solution（与 exercise 独立，两模式通搜）
3. 下一个章节标题（如 `1.2 标题`）→ 切回 content

### 为何 solution 独立为第三种 type

答案解析包含大量解题思路、总结、技巧，既有知识价值又和题目紧密相关。让 `solution` 独立出来，用 ChromaDB 的 `$in` 过滤器实现双模式通搜：
- **知识模式**：搜 `content` + `solution`（学知识点时能看到配套解析）
- **习题模式**：搜 `exercise` + `solution`（刷题时自带答案详解）

## 完整代码（ingest.py）

### 语义预切分函数

```python
import re

_EXERCISE_MARKERS = re.compile(r"本节(试题|习题)精选")
_ANSWER_MARKER = re.compile(r"答案与解析")
_SECTION_HEADER = re.compile(r"^\s*\d+\.\d+\s")

def split_content_exercises(text: str) -> list[dict]:
    """拆为三种 type: content / exercise / solution"""
    segments = []
    current_type = "content"
    current_lines = []

    def flush():
        t = "".join(current_lines).strip()
        if t:
            segments.append({"text": t, "type": current_type})
        current_lines.clear()

    for line in text.splitlines(keepends=True):
        if _EXERCISE_MARKERS.search(line):
            flush(); current_type = "exercise"; continue
        if _ANSWER_MARKER.search(line):
            flush(); current_type = "solution"; continue
        if _SECTION_HEADER.match(line):
            if current_type in ("exercise", "solution"):
                flush(); current_type = "content"
            elif current_type == "content" and current_lines:
                flush()
        current_lines.append(line)
    flush()
    return segments
```

### 分块阶段（三种类型分别处理）

```python
def chunk_text(text: str, source: str) -> list[dict]:
    segments = split_content_exercises(text)
    seg_groups = {t: [s for s in segments if s["type"] == t]
                  for t in ("content", "exercise", "solution")}

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
        length_function=len,
        separators=["\n\n", "\n", "。", ".", " ", ""],
    )

    chunks = []
    idx_counter = {"content": 0, "exercise": 0, "solution": 0}

    for seg_type in ("content", "exercise", "solution"):
        segs = seg_groups[seg_type]
        if not segs:
            continue
        all_text = "\n".join(s["text"] for s in segs)
        for chunk_text in splitter.split_text(all_text):
            i = idx_counter[seg_type]
            chunks.append({
                "text": chunk_text,
                "metadata": {
                    "source": str(source),
                    "type": seg_type,
                    "chunk_index": i,
                },
            })
            idx_counter[seg_type] += 1
    return chunks
```

### 写入 ChromaDB（ID 必须含 type 前缀）

```python
# ⚠️ ID 必须含 type！否则 content/exercise/solution 的
# chunk_index 都从 0 开始，后写入的覆盖先写入的！
ids = [f"{m['source']}#{m['type']}#{m['chunk_index']}" for m in metadatas]
```

## 检索（query.py）

### CLI 参数 + ChromaDB where 过滤

```python
parser.add_argument("--practice", action="store_true",
    help="习题模式：在习题库（exercise+solution）中检索（默认：知识库 content+solution）")

# 知识模式 → content 和 solution 都搜
where = {"type": {"$in": ["exercise", "solution"]}} if args.practice \
   else {"type": {"$in": ["content", "solution"]}}
```

### 结果展示区分

```python
type_tag = " [习题]" if d.get("doc_type") == "exercise" \
      else " [解析]" if d.get("doc_type") == "solution" else ""
```

## 注意事项（踩坑记录）

### 1. ⚠️ ID 冲突（必读）
Content/exercise/solution 各有独立的 chunk_index 计数器，都从 0 开始。如果 IDs 只用 `{source}#{chunk_index}`，后写入的类型会覆盖先写入的类型。**必须包含 type**：`{source}#{type}#{chunk_index}`。

**症状**：`collection.count()` 远小于导入总数，部分类型 chunks 凭空消失。

### 2. ⚠️ "答案与解析" 必须独立为 solution 类型
不要把它合并到 exercise 段里。原因：答案解析中蕴含大量知识价值，需要让知识模式也能搜到。作为独立 type，用 `$in` 过滤器双模式通搜是最干净的方案。

### 3. ⚠️ 章节标题正则误匹配答案行（检测顺序重要）
`答案与解析` 行（如 `12.8 答案与解析`）匹配章节标题正则 `^\s*\d+\.\d+\s`。在切分逻辑中，`_ANSWER_MARKER` 必须在 `_SECTION_HEADER` **之前**检查，否则 solution 段会被错误地当做新章节标题而关闭。**顺序敏感**：先查 _ANSWER_MARKER，再查 _SECTION_HEADER。

### 4. ⚠️ 内联练习题不在过滤范围内
教材正文中穿插的即时练习题属于 content 段，不会被过滤。这是预期的——它们是教学流程的一部分。

### 5. ⚠️ 跨教材噪音
习题模式仅在 exercise+solution 中搜索（约占总量的 60%），不同教材的习题段语义区分度不足，可能跨教材混淆。Reranker 能降权但无法完全消除。

### 6. ⚠️ OCR 乱码
教材 OCR 转写后的标记行可能有乱码字符（如 `] 之 7 本节习题精选`），正则 `search()` 能匹配，不影响。

### 7. 工作流偏好
当用户说"运行我的 RAG 看看有什么问题"时，优先检查 embedding/reranker/retrieval 质量，不要先动 LLM 配置。LLM（生成回答）和 RAG（检索质量）是独立的两个环节——先确保检索准了，再考虑 LLM。

## 参考文件

- `references/ingest.py` — 完整的 ingest.py 实现
- `references/query.py` — 完整的 query.py 实现
