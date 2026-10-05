"""给"生成类"工具准备候选来源与模型上下文（``topic.build_map`` / ``route.plan`` 共用）。

**只挑已入库的资料**：模型不许凭空规划，路线与地图上的每条都要能指回库里的 source_id。
"""

from __future__ import annotations

from typing import Any

from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state.ingest import latest_summary_for_source

#: 送进模型的来源条数上限（控制 token）
MAX_CONTEXT_SOURCES = 25
#: 每条来源的摘要截断长度
TEXT_CHARS = 220
#: 判权为"可用于规划"的结论
USABLE_VERDICTS = ("核心", "相关")


def attach_text(ctx: ToolContext, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给来源补上中文标题与摘要（有摘要优先用摘要，主题词多为中文）。"""
    enriched: list[dict[str, Any]] = []
    for row in rows:
        summary = latest_summary_for_source(ctx.db, str(row["id"]))
        title = str((summary or {}).get("title_zh") or row.get("title") or "")
        text = str((summary or {}).get("summary_short") or row.get("abstract") or "")
        enriched.append({**row, "_display_title": title, "_display_text": text[:TEXT_CHARS]})
    return enriched


def candidate_sources(
    ctx: ToolContext,
    *,
    source_ids: list[str] | None = None,
    topic: str | None = None,
    limit: int = MAX_CONTEXT_SOURCES,
) -> list[dict[str, Any]]:
    """按 source_ids → 该主题下判权可用的来源 → 最近入库 三级取候选。"""
    rows: list[dict[str, Any]] = []

    if source_ids:
        for source_id in source_ids:
            row = ctx.db.query_one("SELECT * FROM sources WHERE id = ?", (str(source_id),))
            if row is not None:
                rows.append(dict(row))
        return attach_text(ctx, rows[: max(1, limit)])

    if topic:
        graded = ctx.db.query(
            """
            SELECT s.*, max(g.created_at) AS graded_at
            FROM sources s JOIN source_grades g ON g.source_id = s.id
            WHERE g.topic = ? AND g.verdict IN (?, ?)
            GROUP BY s.id
            ORDER BY graded_at DESC
            LIMIT ?
            """,
            (topic, *USABLE_VERDICTS, max(1, limit)),
        )
        rows = [dict(row) for row in graded]

    if not rows:
        recent = ctx.db.query(
            "SELECT * FROM sources ORDER BY last_seen_at DESC LIMIT ?", (max(1, limit),)
        )
        rows = [dict(row) for row in recent]

    return attach_text(ctx, rows)


def render_sources(rows: list[dict[str, Any]]) -> str:
    """模型上下文里的一行一条来源：``id | 标题 | tier1 | 2025``。"""
    lines: list[str] = []
    for row in rows:
        tier = row.get("tier")
        tier_part = f"tier{tier}" if tier is not None else "未判权"
        year = row.get("year")
        year_part = str(year) if year is not None else "年份不明"
        title = str(row.get("_display_title") or row.get("title") or "")
        text = str(row.get("_display_text") or "")
        suffix = f" ｜ {text}" if text else ""
        lines.append(f"{row['id']} | {title} | {tier_part} | {year_part}{suffix}")
    return "\n".join(lines) if lines else "（库里还没有可用资料）"


def valid_ids(rows: list[dict[str, Any]]) -> set[str]:
    return {str(row["id"]) for row in rows}


def degrade_reason(*, finish_reason: str, content: str, parsed_ok: bool) -> str | None:
    """兜底模式的**原因**（产物里要留痕，否则"降级率 100%"这种数字无法诊断）。

    真机跑 E3 时踩过：推理模型的 reasoning token 吃满 ``max_tokens``，
    ``content`` 空着回来、``finish_reason=length``，产物静默退化成 flat/skeleton，
    报告里只看到一个 100% 的降级率而不知道该改哪里。
    """
    if parsed_ok:
        return None
    if finish_reason == "length":
        return "模型输出被 max_tokens 截断（推理 token 计入 completion），content 不完整"
    if not (content or "").strip():
        return "模型返回空内容"
    return "模型输出无法解析为约定的行格式"
