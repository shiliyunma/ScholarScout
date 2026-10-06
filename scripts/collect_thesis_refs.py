"""为论文第 2 章收集候选文献（真检索，落盘备查）。

用项目自己的 provider 层（arXiv + OpenAlex），不是另起一套脚本 —— 这样"第 2 章的文献"
和系统实际能检索到的来源是同一批，论文里可以这么说。
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
from scholarscout.providers.base import Query, SourceCard  # noqa: E402
from scholarscout.providers.openalex import OpenAlexProvider  # noqa: E402

QUERIES: list[tuple[str, str]] = [
    # (类别, 查询词)
    ("A 学术检索/文献发现", "scholarly literature search engine API open access"),
    ("B 本地知识库与 RAG", "retrieval augmented generation local knowledge base"),
    ("B 本地知识库与 RAG", "RAG chunking embedding retrieval evaluation"),
    ("C 自主调研 Agent", "deep research agent autonomous literature survey LLM"),
    ("C 自主调研 Agent", "LLM agent tool use multi step search planning"),
    ("D 教育智能体与学习路径", "LLM educational agent personalized learning path"),
    ("D 教育智能体与学习路径", "knowledge map concept graph learning recommendation"),
    ("E 反幻觉与引用可核查", "citation verification attribution hallucination LLM"),
    ("E 反幻觉与引用可核查", "faithfulness attribution evaluation grounded generation"),
    ("F 上下文与成本控制", "context compression prompt caching LLM cost efficiency agent"),
]


def _dedupe_key(card: SourceCard) -> str:
    if card.doi:
        return "doi:" + card.doi.lower()
    if card.external_id:
        return "ext:" + card.external_id.lower()
    return "title:" + "".join(card.title.lower().split())[:80]


def main() -> int:
    http = HttpClient(cache_dir=ROOT / "run" / "refs_cache", timeout_s=25.0)
    arxiv = ArxivProvider(http)
    # OpenAlex 连发会 429（本机实测 10/10 失败）→ 显式放慢 + 失败后冷却重试一次
    openalex = OpenAlexProvider(http, mailto="", min_interval_s=2.0)
    seen: dict[str, dict[str, object]] = {}
    failures: list[str] = []

    for category, text in QUERIES:
        for provider in (arxiv, openalex):
            query = Query(text=text, limit=6, year_from=2023)
            cards: list[SourceCard] = []
            for attempt in (1, 2):
                try:
                    cards = provider.search(query)
                    break
                except Exception as exc:  # 检索失败要记下来，不能装作没发生
                    if attempt == 2:
                        failures.append(f"{provider.name} × {text!r}: {type(exc).__name__}: {exc}")
                    else:
                        time.sleep(8.0)  # 冷却后重试：429 是瞬时的
            for card in cards:
                key = _dedupe_key(card)
                record = seen.get(key)
                if record is None:
                    seen[key] = {
                        "title": card.title,
                        "year": card.year,
                        "authors": list(card.authors),
                        "venue": card.venue,
                        "url": card.url,
                        "doi": card.doi,
                        "external_id": card.external_id,
                        "source_type": card.source_type,
                        "abstract": (card.abstract or "")[:400],
                        "categories": [category],
                        "queries": [text],
                    }
                else:
                    cats = record["categories"]
                    if isinstance(cats, list) and category not in cats:
                        cats.append(category)

    out = ROOT / "run" / "refs_candidates.json"
    out.write_text(
        json.dumps(
            {"items": list(seen.values()), "failures": failures}, ensure_ascii=False, indent=1
        ),
        encoding="utf-8",
    )
    print(f"去重后候选：{len(seen)} 条｜失败：{len(failures)} 次 → {out}")
    for failure in failures:
        print("  ! " + failure)
    print("\n--- 按年份倒序 ---")
    items = sorted(
        seen.values(), key=lambda r: (int(r["year"] or 0), str(r["title"])), reverse=True
    )
    for item in items:
        authors = item["authors"] if isinstance(item["authors"], list) else []
        first = authors[0].split()[-1] if authors else "?"
        print(
            f"{item['year']} | {first} et al. | {str(item['title'])[:88]}"
            f" | {item['venue'] or item['source_type']} | {(item['categories'] or ['?'])[0]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
