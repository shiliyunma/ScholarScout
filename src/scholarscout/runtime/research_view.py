"""研究看板（`research.board`）：把库里已有的东西**换形态**呈现，不做新计算。

对应 `docs/14` 候选清单的 B2 / B4 / B7：
- **B2 证据表**：结论 — 支撑来源 — 证据强度 — 年份，每条可点回原文；
- **B4 时间线**：把本会话的来源按年份排开，顺带指出"哪几年是空的"；
- **B7 看板**：目标 / 子问题（含覆盖状态）/ 缺口 / 来源 / 已搜查询 / 产物，一处看全。

## 三条口径（决定了这张表可不可信）

1. **零模型调用、零新依赖**：全部从 `support_checks`、`research_states`、`sources`、产物表读出来，
   数字必须能一路点回原始来源；
2. **证据强度按确定性规则判**，不让模型打分：
   `summary`（该条目引用的来源里有摘要）+ `supported/partial` → **强**；
   `title`（只有标题）或 `unsupported/unknown` → **弱**。
   （"有摘要"= 强证据这条来自 `docs/05` 的既有口径，与 `eval/verify_support.py` 一致。）
3. **取不到就不编**：没有年份的来源单列"年份未知"、不进"近两年"的分母（与 `eval/metrics.py` 同口径）；
   本会话还没做过引用核查时，证据表**降级为"子问题×来源"**并明确标注 `mode`，不假装有核查结论。
"""

from __future__ import annotations

import json
from typing import Any

from scholarscout.state.db import Database


def _years(rows: list[dict[str, Any]]) -> tuple[int | None, int | None]:
    known = [int(row["year"]) for row in rows if row.get("year")]
    return (min(known), max(known)) if known else (None, None)


def _load_session_sources(db: Database, session_id: str) -> list[dict[str, Any]]:
    """本会话带出来的来源 + 每个来源的"有没有正文/有没有摘要"。"""
    rows = db.query(
        """
        SELECT s.id AS source_id, s.title, s.url, s.domain, s.source_type, s.year, s.tier,
               (SELECT count(*) FROM documents d WHERE d.source_id = s.id) AS docs,
               (SELECT count(*) FROM summaries sm WHERE sm.source_id = s.id) AS sums
        FROM sources s JOIN source_mentions m ON m.source_id = s.id
        WHERE m.session_id = ?
        GROUP BY s.id
        ORDER BY (s.year IS NULL), s.year DESC, s.id
        """,
        (session_id,),
    )
    out = []
    for row in rows:
        item = dict(row)
        item["has_text"] = bool(item.pop("docs"))
        item["has_summary"] = bool(item.pop("sums"))
        out.append(item)
    return out


def _compress_years(years: list[int]) -> list[str]:
    """把年份列表压成"2019–2023"这样的区间串（连着写比罗列好读）。"""
    if not years:
        return []
    ordered = sorted(set(years))
    chunks: list[str] = []
    start = previous = ordered[0]
    for year in ordered[1:]:
        if year == previous + 1:
            previous = year
            continue
        chunks.append(f"{start}" if start == previous else f"{start}–{previous}")
        start = previous = year
    chunks.append(f"{start}" if start == previous else f"{start}–{previous}")
    return chunks


def build_timeline(sources: list[dict[str, Any]], *, now_year: int) -> dict[str, Any]:
    """B4：按年份分组。

    年份未知的单列一桶、不参与任何"缺哪年"的判断（取不到就不猜，与 `eval/metrics.py` 同口径）；
    缺口分两类且都不编：
      · `missing_recent_years`：最新一条之后到"去年"之间的空档（最要紧的那种）；
      · `internal_gaps`：已知年份**区间内部**的空档（例如 2019–2023 一条都没有）——
        只报区间内部，区间之外的空白不叫缺口（可能本来就没资料）。
    """
    buckets: dict[int | None, list[dict[str, Any]]] = {}
    for item in sources:
        year = int(item["year"]) if item.get("year") else None
        buckets.setdefault(year, []).append(item)
    known_years = sorted((year for year in buckets if year is not None), reverse=True)
    groups = [{"year": year, "items": buckets[year]} for year in known_years]
    if None in buckets:
        groups.append({"year": None, "items": buckets[None]})

    missing_recent: list[int] = []
    internal: list[int] = []
    if known_years:
        newest = known_years[0]
        missing_recent = [year for year in range(newest + 1, now_year) if year not in known_years]
        internal = [year for year in range(min(known_years) + 1, newest) if year not in known_years]
    return {
        "groups": groups,
        "known_years": known_years,
        "missing_recent_years": missing_recent,
        "internal_gap_labels": _compress_years(internal),
        "undated": len(buckets.get(None) or []),
    }


def _resolve_sources(db: Database, ids: list[str]) -> dict[str, dict[str, Any]]:
    """按 id 取来源（**跨全会话**，不限本会话）。

    为什么不限本会话：引用核查记的是"这条断言引用了哪些来源"，那些来源可能是在
    生成产物的那一轮带出来的。只在会话内找会让一多半证据行显示"没有可解析的来源"
    （真机渲染核对时抓到：15 条弱证据行全是空的）。
    """
    wanted = [str(item) for item in ids if item]
    if not wanted:
        return {}
    placeholders = ",".join("?" for _ in wanted)
    rows = db.query(
        f"""
        SELECT s.id AS source_id, s.title, s.url, s.domain, s.source_type, s.year, s.tier,
               (SELECT count(*) FROM summaries sm WHERE sm.source_id = s.id) AS sums
        FROM sources s WHERE s.id IN ({placeholders})
        """,
        tuple(wanted),
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        item["has_summary"] = bool(item.pop("sums"))
        out[str(item["source_id"])] = item
    return out


def _evidence_from_support_checks(
    db: Database, session_id: str, sources_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """B2 主口径：用 `support_checks`（引用核查结果）当证据表。"""
    rows = db.query(
        "SELECT artifact_kind, artifact_id, item_id, claim, source_ids_json, evidence_level,"
        " verdict, reason FROM support_checks WHERE session_id = ? ORDER BY artifact_kind, item_id",
        (session_id,),
    )
    parsed: list[tuple[dict[str, Any], list[str]]] = []
    all_ids: set[str] = set()
    for row in rows:
        try:
            ids = [str(item) for item in json.loads(str(row["source_ids_json"] or "[]"))]
        except json.JSONDecodeError:
            ids = []
        parsed.append((dict(row), ids))
        all_ids.update(ids)
    # 会话内已加载的来源优先（含"有没有正文"），缺的按 id 跨会话补查
    missing = [item for item in all_ids if item not in sources_by_id]
    resolved = {**sources_by_id, **_resolve_sources(db, missing)}

    table = []
    for check, ids in parsed:
        cited = [resolved[item] for item in ids if item in resolved]
        strong = str(check["evidence_level"]) == "summary" and str(check["verdict"]) in {
            "supported",
            "partial",
        }
        table.append(
            {
                "artifact_kind": str(check["artifact_kind"]),
                "artifact_id": str(check["artifact_id"]),
                "item_id": str(check["item_id"]),
                "claim": str(check["claim"]),
                "verdict": str(check["verdict"]),
                "evidence_level": str(check["evidence_level"]),
                "strength": "强" if strong else "弱",
                "reason": str(check["reason"] or ""),
                "sources": [
                    {
                        "source_id": item["source_id"],
                        "title": item.get("title") or item["url"],
                        "year": item.get("year"),
                        "tier": item.get("tier"),
                        "has_summary": bool(item.get("has_summary")),
                    }
                    for item in cited
                ],
            }
        )
    return table


def _evidence_from_ledger(
    state: dict[str, Any], sources: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """降级口径：还没做过引用核查时，用"子问题 × 支撑来源"当证据表（并明确标注）。

    强度规则（确定性，不让模型打分）：
      有 ≥1 条"有摘要"的来源 → 中；有 ≥1 条"有正文"的来源 → 中；只有标题 → 弱；没有来源 → 缺。
    """
    by_id = {item["source_id"]: item for item in sources}
    rows = []
    for index, question in enumerate(state.get("questions") or [], start=1):
        if not isinstance(question, dict):
            continue
        cited = [by_id[item] for item in (question.get("evidence") or []) if item in by_id]
        if not cited:
            strength = "缺"
        elif any(item.get("has_summary") for item in cited):
            strength = "中（有摘要）"
        elif any(item.get("has_text") for item in cited):
            strength = "中（仅有正文）"
        else:
            strength = "弱（只有标题）"
        low, high = _years(cited)
        rows.append(
            {
                "artifact_kind": "research",
                "artifact_id": str(state.get("session_id") or ""),
                "item_id": str(index),
                "claim": str(question.get("text") or ""),
                "verdict": "supported" if str(question.get("status")) == "covered" else "unknown",
                "evidence_level": "summary"
                if any(i.get("has_summary") for i in cited)
                else "title",
                "strength": strength,
                "reason": "",
                "year_range": [low, high] if low else None,
                "sources": [
                    {
                        "source_id": item["source_id"],
                        "title": item.get("title") or item["url"],
                        "year": item.get("year"),
                        "tier": item.get("tier"),
                        "has_summary": bool(item.get("has_summary")),
                    }
                    for item in cited
                ],
            }
        )
    return rows


def build_evidence(
    db: Database,
    session_id: str,
    *,
    state: dict[str, Any],
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    """证据表：优先用引用核查结果，没有就降级（并告诉前端是降级）。"""
    by_id = {item["source_id"]: item for item in sources}
    rows = _evidence_from_support_checks(db, session_id, by_id)
    mode = "support_checks"
    if not rows:
        rows = _evidence_from_ledger({**state, "session_id": session_id}, sources)
        mode = "ledger" if rows else "empty"
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["strength"]] = counts.get(row["strength"], 0) + 1
    return {
        "mode": mode,
        "rows": rows,
        "strength_counts": counts,
        # 弱证据条数是这张表存在的理由：它让"哪条结论站不住"变成一个可查的数字
        "weak_count": sum(count for key, count in counts.items() if str(key).startswith("弱")),
        "total": len(rows),
    }


def build_board(
    db: Database,
    session_id: str,
    *,
    gaps: list[dict[str, str]] | None = None,
    now_year: int | None = None,
) -> dict[str, Any]:
    """看板的唯一出口：一次给全，避免前端连打四五个 RPC。"""
    import datetime

    from scholarscout.runtime import research_state

    year_now = now_year or datetime.date.today().year
    state = research_state.load_state(db, session_id) or research_state.empty_state()
    sources = _load_session_sources(db, session_id)
    if gaps is None:
        gaps = research_state.compute_gaps(db, session_id, state, now_year=year_now)

    artifacts = {
        "maps": [
            dict(row)
            for row in db.query(
                "SELECT id, topic, created_at FROM topic_maps WHERE session_id = ?"
                " ORDER BY created_at DESC",
                (session_id,),
            )
        ],
        "routes": [
            dict(row)
            for row in db.query(
                "SELECT id, topic, level, weeks, version, created_at FROM learning_routes"
                " WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            )
        ],
        "notes": [
            dict(row)
            for row in db.query(
                "SELECT id, kind, title, created_at FROM notes WHERE session_id = ?"
                " ORDER BY created_at DESC",
                (session_id,),
            )
        ],
    }

    return {
        "goal": str(state.get("goal") or ""),
        "questions": [
            {
                "index": index,
                "text": str(item.get("text") or ""),
                "status": str(item.get("status") or "open"),
            }
            for index, item in enumerate(state.get("questions") or [], start=1)
            if isinstance(item, dict)
        ],
        "gaps": gaps,
        "queries": [str(item) for item in (state.get("used_queries") or [])],
        "sources": sources,
        "evidence": build_evidence(db, session_id, state=state, sources=sources),
        "timeline": build_timeline(sources, now_year=year_now),
        "artifacts": artifacts,
        "counts": {
            "sources": len(sources),
            "with_summary": sum(1 for item in sources if item.get("has_summary")),
            "without_text": sum(1 for item in sources if not item.get("has_text")),
            "queries": len(state.get("used_queries") or []),
            "open_questions": sum(
                1
                for item in (state.get("questions") or [])
                if isinstance(item, dict) and str(item.get("status")) != "covered"
            ),
        },
    }
