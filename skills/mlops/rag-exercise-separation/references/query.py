"""
query.py - 检索 + 重排序 + 回答（AI 嵌入版）

两种模式:
  .venv/Scripts/python.exe query.py                    # 交互式，搜知识库
  .venv/Scripts/python.exe query.py "你的问题"          # 单次，搜知识库
  .venv/Scripts/python.exe query.py --practice "问题"   # 搜习题库
  .venv/Scripts/python.exe query.py --raw "问题"        # 只看原文
  .venv/Scripts/python.exe query.py --rerank-only "..." # 只看排序
"""

import argparse
import sys

from config import (
    INDEX_DIR, EMBED_MODEL_PATH, DEVICE, TOP_K, RETRIEVAL_TOP_K,
    RERANKER_MODEL_PATH,
    LLM_BASE_URL, LLM_API_KEY, LLM_MODEL,
)


# ── 1. 检索 ────────────────────────────────────────

def retrieve(query: str, embed_model, collection, where: dict | None = None) -> list[dict]:
    """向量检索，返回 RETRIEVAL_TOP_K 条。"""
    is_knowledge = where and where.get("type", {}).get("$in") == ["content", "solution"]
    is_practice = where and where.get("type", {}).get("$in") == ["exercise", "solution"]
    mode_label = "📝 知识库" if is_knowledge else "✏️  习题库" if is_practice else "🔍"
    print(f"{mode_label} 检索: {query}")

    q_embedding = embed_model.encode([query]).tolist()[0]
    results = collection.query(
        query_embeddings=[q_embedding],
        n_results=RETRIEVAL_TOP_K,
        where=where,
    )

    docs = []
    for i in range(len(results["ids"][0])):
        meta = results["metadatas"][0][i] if results.get("metadatas") else {}
        docs.append({
            "id": results["ids"][0][i],
            "text": results["documents"][0][i],
            "score": results["distances"][0][i] if results.get("distances") else 0,
            "source": meta.get("source", "?"),
            "doc_type": meta.get("type", "?"),
        })

    return docs


# ── 2. 重排序（Reranker） ──────────────────────────

def rerank(query: str, docs: list[dict], reranker) -> list[dict]:
    """用 Cross-Encoder 对检索结果精排，取 TOP_K 条。"""
    if len(docs) <= TOP_K:
        return docs

    print(f"🔁 重排序 ({len(docs)} 条 → {TOP_K} 条)...")

    pairs = [(query, d["text"]) for d in docs]
    scores = reranker.predict(pairs)

    scored = list(zip(docs, scores))
    scored.sort(key=lambda x: x[1], reverse=True)

    top_docs = []
    for d, s in scored[:TOP_K]:
        d["rerank_score"] = float(s)
        top_docs.append(d)

    return top_docs


# ── 3. 生成回答 ────────────────────────────────────

SYSTEM_PROMPT = """你是一个基于本地知识库的问答助手。
请综合下面所有的参考文档片段，给出一个完整、详细的回答。
要求：
1. 涵盖各个片段提到的不同方面，不要只取其中一段
2. 如果片段之间有互补信息，合并成连贯的叙述
3. 用清晰的段落组织答案，适当分点说明
4. 引用来源时标注文件名
5. 如果文档中没有相关信息，直接说"文档中没有相关信息"，不要编造"""


def build_context(docs: list[dict]) -> str:
    lines = []
    for i, d in enumerate(docs, 1):
        source_info = f"[来源 {i}] {d['source']}"
        if "rerank_score" in d:
            source_info += f" (相关度: {d['rerank_score']:.4f})"
        lines.append(source_info)
        lines.append(d["text"])
        lines.append("---")
    return "\n".join(lines)


def answer_with_llm(query: str, context: str) -> str | None:
    if not LLM_BASE_URL:
        return None

    from openai import OpenAI
    client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"## 参考文档\n\n{context}\n\n## 问题\n\n{query}"},
            ],
            temperature=0.3,
            max_tokens=1024,
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"[LLM 调用失败: {e}]"


# ── 4. 显示 ────────────────────────────────────────

def print_results(query: str, docs: list[dict], raw: bool = False):
    print(f"\n{'='*60}")
    print(f"问题: {query}")
    print(f"{'='*60}\n")

    if not raw:
        context = build_context(docs)
        answer = answer_with_llm(query, context)
        if answer:
            print(f"📝 回答:\n{answer}\n")

    print(f"\n📄 参考来源 (共 {len(docs)} 条, 按 reranker 相关度排序):")
    for d in docs:
        score_str = f" (rerank: {d['rerank_score']:.4f})" if "rerank_score" in d else ""
        type_tag = " [习题]" if d.get("doc_type") == "exercise" else " [解析]" if d.get("doc_type") == "solution" else ""
        print(f"\n  [{d['source']}]{type_tag}{score_str}")
        print(f"  {d['text'][:300]}{'...' if len(d['text'])>300 else ''}")


# ── 5. 交互循环 ────────────────────────────────────

def interactive(embed_model, collection, reranker, where: dict | None = None):
    mode = "习题" if where == {"type": {"$in": ["exercise", "solution"]}} else "知识"
    print(f"💬 RAG 问答模式 — 检索{mode}库 (输入 q 退出)")
    while True:
        try:
            query = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not query:
            continue
        if query.lower() in ("q", "quit", "exit"):
            break

        docs = retrieve(query, embed_model, collection, where=where)
        if not docs:
            print("  无相关结果")
            continue
        docs = rerank(query, docs, reranker)
        print_results(query, docs)


# ── 入口 ───────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="RAG 检索问答")
    parser.add_argument("query", nargs="*", help="问题（留空进入交互模式）")
    parser.add_argument("--raw", action="store_true", help="只检索原文，不调用 LLM")
    parser.add_argument("--rerank-only", action="store_true",
                        help="只显示 reranker 排序结果，不调 LLM")
    parser.add_argument("--practice", action="store_true",
                        help="习题模式：在习题库（exercise+solution）中检索（默认：知识库 content+solution）")
    args = parser.parse_args()

    # ── 启动时一次性加载 ──
    print("⏳ 加载嵌入模型...", end=" ", flush=True)
    from sentence_transformers import SentenceTransformer
    embed_model = SentenceTransformer(EMBED_MODEL_PATH, device=DEVICE)
    print(f"✅ (device={embed_model.device}, dim={embed_model.get_embedding_dimension()})")

    print("⏳ 加载 reranker (CPU)...", end=" ", flush=True)
    from sentence_transformers import CrossEncoder
    reranker = CrossEncoder(RERANKER_MODEL_PATH, device="cpu")
    print("✅")

    import chromadb
    client = chromadb.PersistentClient(path=str(INDEX_DIR))
    collection = client.get_collection("docs")
    print(f"📦 向量库已连接 ({collection.count()} 条片段)")

    where = {"type": {"$in": ["exercise", "solution"]}} if args.practice else {"type": {"$in": ["content", "solution"]}}
    mode_name = "习题库" if args.practice else "知识库"
    print(f"📖 模式: {mode_name}")

    if not args.query:
        interactive(embed_model, collection, reranker, where=where)
        return

    query = " ".join(args.query)
    docs = retrieve(query, embed_model, collection, where=where)
    if not docs:
        print("  无相关结果")
        sys.exit(1)
    docs = rerank(query, docs, reranker)
    raw = args.raw or args.rerank_only
    print_results(query, docs, raw=raw)


if __name__ == "__main__":
    main()
