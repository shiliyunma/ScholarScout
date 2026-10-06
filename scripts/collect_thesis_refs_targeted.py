"""定向补检索：第 2 章需要引用的"必引经典"（按标题检索，拿真实 arXiv 编号）。

为什么单独跑：OpenAlex 在本机当期硬 429（单发 + 冷却 65s 仍 429），
所以文献只能全部来自 arXiv —— 好处是每条都带 arXiv 编号，可逐条核查。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scholarscout.fetch.http import HttpClient  # noqa: E402
from scholarscout.providers.arxiv import ArxivProvider  # noqa: E402
from scholarscout.providers.base import Query  # noqa: E402

#: 类别 → 要钉住的工作（用标题里的特征词，让 arXiv 命中那篇）
TARGETS: list[tuple[str, str]] = [
    ("B 本地知识库与 RAG", "Retrieval-Augmented Generation Knowledge-Intensive NLP Tasks"),
    ("C 自主调研 Agent", "ReAct Synergizing Reasoning Acting Language Models"),
    ("B 本地知识库与 RAG", "Self-RAG Learning Retrieve Generate Critique Self-Reflection"),
    ("C 自主调研 Agent", "Toolformer Language Models Teach Themselves Use Tools"),
    ("C 自主调研 Agent", "Chain-of-Thought Prompting Elicits Reasoning Large Language Models"),
    ("E 反幻觉与引用可核查", "Judging LLM-as-a-Judge MT-Bench Chatbot Arena"),
    ("F 上下文与成本控制", "Lost in the Middle Language Models Use Long Contexts"),
    ("F 上下文与成本控制", "MemGPT Towards LLMs as Operating Systems"),
    ("E 反幻觉与引用可核查", "Measuring Attribution Natural Language Generation Models"),
    ("D 教育智能体与学习路径", "Generative Agents Interactive Human Behavior Simulation"),
    ("A 学术检索/文献发现", "SciBERT Pretrained Language Model Scientific Text"),
    ("C 自主调研 Agent", "Survey on Large Language Model based Autonomous Agents"),
]


def main() -> int:
    http = HttpClient(cache_dir=ROOT / "run" / "refs_cache", timeout_s=25.0)
    arxiv = ArxivProvider(http)
    items: list[dict[str, object]] = []
    failures: list[str] = []
    for category, text in TARGETS:
        try:
            cards = arxiv.search(Query(text=text, limit=1, sort="relevance"))
        except Exception as exc:
            failures.append(f"{text!r}: {type(exc).__name__}: {exc}")
            continue
        for card in cards:
            items.append(
                {
                    "category": category,
                    "query": text,
                    "title": card.title,
                    "year": card.year,
                    "authors": list(card.authors),
                    "venue": card.venue,
                    "url": card.url,
                    "external_id": card.external_id,
                    "doi": card.doi,
                    "abstract": (card.abstract or "")[:300],
                }
            )
            print(f"{card.year} | {card.external_id} | {card.title[:78]}")
        time.sleep(3.2)  # arXiv 限速
    out = ROOT / "run" / "refs_canonical.json"
    out.write_text(
        json.dumps({"items": items, "failures": failures}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"\n命中 {len(items)} 条｜失败 {len(failures)} → {out}")
    for failure in failures:
        print("  ! " + failure)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
