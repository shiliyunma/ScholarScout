"""``source.grade`` —— 来源判权与相关性（docs/01 §5.3）。

- **权威性**来自规则表（确定性、可复现），模型不参与
- **相关性**默认本地确定性打分；给了 ``topic`` 且 ``use_model`` 为真时交给模型，
  并在结果里标明 ``relevance_source``（local / model）
- 判权结果**只追加不覆盖**，同时把 tier/authority 反范式化回 ``sources``
"""

from __future__ import annotations

from typing import Any

from scholarscout.grading import relevance as rel
from scholarscout.grading import tiers
from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state.ingest import (
    apply_tier_to_source,
    document_for_source,
    latest_summary_for_source,
    save_grade,
)
from scholarscout.summary.prompts import PromptPack
from scholarscout.tools.envelope import ToolResult, ok

DESCRIPTION = (
    "判断资料的权威性与相关性：权威性由来源分层规则表判定（可解释、可复现），"
    "相关性在给出 topic 时由模型打分、否则本地按主题词覆盖与时效计算。"
    "返回 tier、authority、relevance、verdict（核心/相关/外围/排除）与理由。"
)

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "要判权的 source_id 列表",
        },
        "urls": {"type": "array", "items": {"type": "string"}, "description": "或直接给 URL 列表"},
        "topic": {"type": "string", "description": "相关性判断所用的主题；不给则只判权威性"},
        "use_model": {
            "type": "boolean",
            "description": "给了 topic 时是否用模型判相关性，默认 true",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "未指定来源时批量判权的条数",
        },
    },
    "additionalProperties": False,
}

#: 未指定来源时，最多顺手判这么多条（通常是刚检索入库、tier 还空着的）
UNGATED_BATCH = 20


def _load_sources(
    ctx: ToolContext, params: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按 source_ids / urls / 未判权的最新来源 取来源；返回 (找到的, 跳过的)。"""
    found: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    ids = [str(value) for value in (params.get("source_ids") or [])]
    for source_id in ids:
        row = ctx.db.query_one("SELECT * FROM sources WHERE id = ?", (source_id,))
        if row is None:
            skipped.append({"source_id": source_id, "reason": "not_found"})
        else:
            found.append(dict(row))

    urls = [str(value) for value in (params.get("urls") or [])]
    if urls:
        from scholarscout.providers.base import canonical_url

        for url in urls:
            row = ctx.db.query_one(
                "SELECT * FROM sources WHERE url_canonical = ?", (canonical_url(url),)
            )
            if row is None:
                skipped.append({"url": url, "reason": "not_found"})
            else:
                found.append(dict(row))

    if not found and not skipped:
        limit = int(params.get("limit") or UNGATED_BATCH)
        rows = ctx.db.query(
            """
            SELECT * FROM sources WHERE tier IS NULL
            ORDER BY last_seen_at DESC LIMIT ?
            """,
            (max(1, min(limit, 100)),),
        )
        found.extend(dict(row) for row in rows)

    return found, skipped


def _text_for_relevance(ctx: ToolContext, source: dict[str, Any]) -> tuple[str, str | None]:
    """相关性判断用的文本：优先中文摘要（主题词多为中文），退回标题与原文摘要。"""
    summary = latest_summary_for_source(ctx.db, str(source["id"]))
    document = document_for_source(ctx.db, str(source["id"]))
    title = str((summary or {}).get("title_zh") or source.get("title") or "")
    abstract = None
    if summary is not None and summary.get("summary_short"):
        abstract = str(summary["summary_short"])
    elif document is not None and document.get("abstract"):
        abstract = str(document["abstract"])
    return title, abstract


def source_grade(
    ctx: ToolContext,
    source_ids: list[str] | None = None,
    urls: list[str] | None = None,
    topic: str | None = None,
    use_model: bool = True,
    limit: int | None = None,
) -> ToolResult:
    params: dict[str, Any] = {
        "source_ids": source_ids,
        "urls": urls,
        "topic": topic,
        "use_model": use_model,
        "limit": limit,
    }
    sources, skipped = _load_sources(ctx, params)
    rules = tiers.load_rules(ctx.db)
    pack = PromptPack(ctx.config.summary_cfg.prompts_dir)
    use_llm = bool(topic) and use_model and ctx.llm is not None

    items: list[dict[str, Any]] = []
    for source in sources:
        tier_verdict = tiers.classify_source(ctx.db, source, rules)
        title, abstract = _text_for_relevance(ctx, source)
        year = source.get("year")

        if topic and use_llm:
            relevance_result = rel.model_relevance(
                ctx.llm,
                title=title,
                abstract=abstract,
                topic=topic,
                year=int(year) if isinstance(year, int) else None,
                venue=str(source.get("venue") or "") or None,
                pack=pack,
            )
        elif topic:
            relevance_result = rel.local_relevance(
                title=title,
                abstract=abstract,
                topic=topic,
                year=int(year) if isinstance(year, int) else None,
                current_year=rel._current_year(),
            )
        else:
            relevance_result = rel.RelevanceResult(score=None, source="none", signals=[])

        verdict = rel.verdict_for(tier_verdict.authority, relevance_result.score)
        signals = [*tier_verdict.signals, *relevance_result.signals]
        grade_id = save_grade(
            ctx.db,
            source_id=str(source["id"]),
            tier=tier_verdict.tier,
            tier_label=tier_verdict.tier_label,
            authority=tier_verdict.authority,
            relevance=relevance_result.score,
            verdict=verdict,
            topic=topic,
            matched_rule_id=tier_verdict.matched_rule_id,
            signals=signals,
            reason=relevance_result.reason,
            relevance_source=relevance_result.source,
            guard_failed=relevance_result.guard_failed,
            model=relevance_result.model,
            prompt_version=relevance_result.prompt_version,
        )
        apply_tier_to_source(
            ctx.db, str(source["id"]), tier=tier_verdict.tier, authority=tier_verdict.authority
        )
        items.append(
            {
                "grade_id": grade_id,
                "source_id": source["id"],
                "title": source.get("title"),
                "url": source.get("url"),
                "domain": source.get("domain"),
                "tier": tier_verdict.tier,
                "tier_label": tier_verdict.tier_label,
                "authority": tier_verdict.authority,
                "relevance": relevance_result.score,
                "relevance_source": relevance_result.source,
                "verdict": verdict,
                "matched_rule": tier_verdict.matched_pattern,
                "reason": relevance_result.reason,
                "signals": signals,
                "guard_failed": relevance_result.guard_failed,
            }
        )

    summary_counts: dict[str, int] = {}
    for item in items:
        summary_counts[item["verdict"]] = summary_counts.get(item["verdict"], 0) + 1

    return ok(
        {
            "items": items,
            "skipped": skipped,
            "counts": summary_counts,
            "topic": topic,
            "relevance_engine": "model" if use_llm else ("local" if topic else "none"),
        },
        refs={"source_ids": [str(item["source_id"]) for item in items]},
    )


SPEC = ToolSpec(
    name="source.grade",
    description=DESCRIPTION,
    parameters=PARAMETERS,
    handler=source_grade,
    toolset="research",
)


def register() -> None:
    from scholarscout.runtime import registry

    registry.register(SPEC, replace=True)
