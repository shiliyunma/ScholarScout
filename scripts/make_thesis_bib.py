"""生成 `docs/13-论文参考文献.md`（编号顺序 = 第 2 章的引用顺序）。

元数据全部来自真检索结果（run/refs_candidates.json + run/refs_canonical.json），
**不手工编写条目** —— 免得把作者名或编号写错。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 引用顺序（分组与第 2 章一致）：用 arXiv 编号或标题特征词定位
ORDER: list[str] = [
    # A 学术检索与文献发现
    "1903.10676",  # SciBERT
    "Testing Our Foundations",  # 引用错误与幻觉趋势
    # B 检索增强生成与本地知识库
    "2005.11401",  # RAG 原始论文
    "2310.11511",  # Self-RAG
    "Chunking, Retrieval, and Re-ranking",
    "Rethinking Chunk Size For Long-Document Retrieval",
    "Chunk Twice, Embed Once",
    "SmartChunk Retrieval",
    "TagRAG",
    "TOBUGraph",
    "W-RAG",
    "Parser, Chunking, and Embedding Interactions",
    # C 自主调研与工具使用智能体
    "2201.11903",  # CoT
    "2210.03629",  # ReAct
    "2302.04761",  # Toolformer
    "2308.11432",  # 自主智能体综述
    "2306.05685",  # LLM-as-a-Judge
    "Laser: Governing Long-Horizon",
    "MCP-Bench",
    "ToolTree",
    "AnovaX",
    # D 教育智能体与学习路径生成
    "Multi-Agent Learning Path Planning",
    "IB-GRPO",
    "A Theory-Guided LLM Pedagogical Agent",
    "LLM-powered Multi-agent Framework for Goal-oriented Learning",
    "GraphMASAL",
    "Agentic Workflow for Education",
    # E 引用可核查与反幻觉
    "2112.12870",  # Measuring Attribution
    "VeriCite",
    "CiteGuard",
    "Attribute First, then Generate",
    "Learning Fine-Grained Grounded Citations",
    "Deceptive Grounding",
    "Compound Deception in Elite Peer Review",
    "AI Hallucination from Students' Perspective",
    # F 上下文与成本
    "2307.03172",  # Lost in the Middle
    "2310.08560",  # MemGPT
    "TokenDance",
]


def load_pool() -> list[dict[str, object]]:
    pool: list[dict[str, object]] = []
    for name in ("refs_candidates.json", "refs_canonical.json"):
        data = json.loads((ROOT / "run" / name).read_text(encoding="utf-8"))
        pool.extend(data["items"])
    return pool


def arxiv_id(item: dict[str, object]) -> str:
    raw = str(item.get("external_id") or "")
    if raw:
        return raw.split("v")[0]
    match = re.search(r"abs/([0-9.]+)", str(item.get("url") or ""))
    return match.group(1) if match else ""


def authors_text(names: list[str], *, limit: int = 3) -> str:
    def one(full: str) -> str:
        parts = [p for p in full.replace(".", " ").split() if p]
        if not parts:
            return full
        surname = parts[-1]
        initials = "".join(p[0].upper() for p in parts[:-1])
        return f"{surname} {initials}" if initials else surname

    shown = [one(n) for n in names[:limit]]
    text = ", ".join(shown)
    return text + (", et al" if len(names) > limit else "")


def main() -> int:
    pool = load_pool()
    used: set[str] = set()
    lines: list[str] = []
    missing: list[str] = []
    for index, key in enumerate(ORDER, start=1):
        match = None
        for item in pool:
            if (arxiv_id(item) == key or key.lower() in str(item["title"]).lower()) and id(
                item
            ) not in used:
                match = item
                break
        if match is None:
            missing.append(key)
            continue
        used.add(id(match))
        aid = arxiv_id(match)
        authors = match.get("authors") or []
        authors = authors if isinstance(authors, list) else []
        venue = match.get("venue") or "arXiv 预印本"
        entry = (
            f"[{index}] {authors_text(authors)}. {str(match['title']).rstrip('.')}. "
            f"{venue}, {match['year']}. arXiv:{aid}."
        )
        lines.append(entry)
        print(entry)
    if missing:
        print("\n!! 没找到（检查标题写错还是检索没命中）：")
        for key in missing:
            print("   - " + key)
    out = ROOT / "docs" / "13-论文参考文献.md"
    out.write_text(
        "# 参考文献\n\n"
        "> 本表由 `run/make_bib.py` 从真检索结果生成（元数据来自 arXiv API 返回，非手工编写）。\n"
        "> 序号与第 2 章的引用顺序一致。OpenAlex 在本机当期返回 429，故全部条目来自 arXiv，"
        "每条均带编号可核。\n\n" + "\n\n".join(lines) + "\n",
        encoding="utf-8",
    )
    print(f"\n共 {len(lines)} 条 → {out}")
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
