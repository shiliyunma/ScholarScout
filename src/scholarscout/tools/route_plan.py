"""``route.plan`` —— 生成带出处的学习路线（docs/01 §5.5）。

守则：
- 每个 item 要么挂**库里真实的 source_id**，要么显式写 `-` 表示"无对应资料"（计入 ``ungrounded``）
- 检查点必须是"能做到什么"；不达标就照实记，不做二次修复调用
- 没有模型时退化成"骨架路线"：按判权层级与年份把已有资料排进各周，``mode="skeleton"``
"""

from __future__ import annotations

import re
from typing import Any

from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state.learning import save_route
from scholarscout.summary.prompts import PromptPack
from scholarscout.tools.source_context import (
    candidate_sources,
    degrade_reason,
    render_sources,
    valid_ids,
)

DESCRIPTION = (
    "为某个方向排一条 N 周学习路线：每周有目标和检查点，每个条目挂真实资料 id 或标明无出处。"
    "顺序按先能跑起来再深入；返回 route_id，条目里带 why（为什么读它）与预计分钟数。"
)

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "description": "方向或主题"},
        "level": {"type": "string", "enum": ["入门", "进阶", "研究"], "description": "读者水平"},
        "weeks": {"type": "integer", "minimum": 1, "maximum": 24, "description": "周数"},
        "weekly_hours": {
            "type": "integer",
            "minimum": 1,
            "maximum": 40,
            "description": "每周可投入小时",
        },
        "source_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "限定用哪些来源",
        },
        "use_model": {"type": "boolean", "description": "是否用模型排路线，默认 true"},
    },
    "required": ["topic"],
    "additionalProperties": False,
}

ALLOWED_ITEM_KINDS = ("paper", "doc", "video", "code", "exercise")
DASH = "-"
MIN_ITEM_MINUTES = 10
MAX_ITEM_MINUTES = 600

_WEEK_LINE = re.compile(r"^\s*week\s*[:：]\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(.+)$")
_ITEM_LINE = re.compile(
    r"^\s*item\s*[:：]\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]+?)\s*$"
)


def parse_plan(text: str) -> dict[str, Any]:
    """解析模型输出（只做语法层）。"""
    weeks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        week_match = _WEEK_LINE.match(line)
        if week_match is not None:
            idx_raw, goal, checkpoint = (part.strip() for part in week_match.groups())
            current = {
                "idx": int(idx_raw) if idx_raw.isdigit() else len(weeks) + 1,
                "goal": goal,
                "checkpoint": checkpoint,
                "items": [],
            }
            weeks.append(current)
            continue

        item_match = _ITEM_LINE.match(line)
        if item_match is not None and current is not None:
            kind, title, source_raw, why, minutes_raw = (
                part.strip() for part in item_match.groups()
            )
            source_id = None if source_raw in {"", DASH, "无", "N/A"} else source_raw
            minutes = int(minutes_raw) if minutes_raw.isdigit() else 0
            current["items"].append(
                {
                    "kind": kind if kind in ALLOWED_ITEM_KINDS else "doc",
                    "title": title,
                    "source_id": source_id,
                    "why": why,
                    "est_minutes": max(MIN_ITEM_MINUTES, min(minutes or 45, MAX_ITEM_MINUTES)),
                }
            )

    return {"weeks": weeks}


def apply_guards(
    plan: dict[str, Any],
    *,
    allowed_ids: set[str],
    weeks: int,
) -> dict[str, Any]:
    """事实校验：来源白名单、周数上限、去重同一来源的重复条目。"""
    kept_weeks: list[dict[str, Any]] = []
    ungrounded: list[str] = []
    seen_sources: set[str] = set()
    dropped = 0

    for week in (plan.get("weeks") or [])[:weeks]:
        items: list[dict[str, Any]] = []
        for item in week.get("items") or []:
            source_id = item.get("source_id")
            if source_id is not None and source_id not in allowed_ids:
                source_id = None
            if source_id is not None:
                if source_id in seen_sources:
                    dropped += 1  # 同一份资料在同一路线里排两次没有意义
                    continue
                seen_sources.add(source_id)
            else:
                ungrounded.append(str(item.get("title") or ""))
            items.append({**item, "source_id": source_id})
        kept_weeks.append(
            {
                "idx": week.get("idx") or len(kept_weeks) + 1,
                "goal": week.get("goal") or "",
                "checkpoint": week.get("checkpoint") or "",
                "items": items,
            }
        )

    return {
        "weeks": kept_weeks,
        "stats": {
            "weeks": len(kept_weeks),
            "items": sum(len(week["items"]) for week in kept_weeks),
            "ungrounded_items": len(ungrounded),
            "ungrounded_titles": ungrounded,
            "dropped_duplicates": dropped,
            "sources_used": len(seen_sources),
        },
    }


def build_skeleton(rows: list[dict[str, Any]], *, weeks: int) -> dict[str, Any]:
    """无模型兜底：按层级（先一手权威）与年份把已有资料轮流排进各周，不做推断。"""
    ordered = sorted(
        rows,
        key=lambda row: (row.get("tier") or 9, -(row.get("year") or 0)),
    )
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(weeks)]
    for index, row in enumerate(ordered):
        buckets[index % weeks].append(row)

    result_weeks: list[dict[str, Any]] = []
    for index, bucket in enumerate(buckets):
        items = [
            {
                "kind": "paper"
                if str(row.get("source_type")) in {"arxiv", "openalex", "semantic_scholar"}
                else "doc",
                "title": str(row.get("_display_title") or row.get("title") or row["id"]),
                "source_id": str(row["id"]),
                "why": "骨架路线：按来源层级与年份排序后均匀分配，未做内容推断。",
                "est_minutes": 90,
            }
            for row in bucket
        ]
        result_weeks.append(
            {
                "idx": index + 1,
                "goal": f"读完第 {index + 1} 组资料并写下要点",
                "checkpoint": "能用自己的话说明这组资料在解决什么问题",
                "items": items,
            }
        )

    return {
        "weeks": result_weeks,
        "stats": {
            "weeks": len(result_weeks),
            "items": sum(len(week["items"]) for week in result_weeks),
            "ungrounded_items": 0,
            "ungrounded_titles": [],
            "dropped_duplicates": 0,
            "sources_used": len(ordered),
        },
    }


def route_plan(
    ctx: ToolContext,
    topic: str,
    level: str = "入门",
    weeks: int = 4,
    weekly_hours: int = 6,
    source_ids: list[str] | None = None,
    use_model: bool = True,
) -> dict[str, Any]:
    topic = topic.strip()
    if not topic:
        raise ValueError("topic 不能为空")
    week_count = max(1, min(int(weeks), 24))
    hours = max(1, min(int(weekly_hours), 40))

    rows = candidate_sources(ctx, source_ids=source_ids, topic=topic)
    allowed = valid_ids(rows)
    pack = PromptPack(ctx.config.summary_cfg.prompts_dir)

    mode = "skeleton"
    reason: str | None = None
    plan: dict[str, Any]
    if use_model and ctx.llm is not None and rows:
        prompt = pack.compose(
            "plan-route",
            topic=topic,
            level=level,
            weeks=str(week_count),
            weeklyHours=str(hours),
            sources=render_sources(rows),
        )
        content = ""
        finish_reason = ""
        try:
            reply = ctx.llm.chat([{"role": "user", "content": prompt}])
            content = str(getattr(reply, "content", ""))
            finish_reason = str(getattr(reply, "finish_reason", "") or "")
            parsed = parse_plan(content)
        except Exception:
            parsed = {"weeks": []}
        if parsed["weeks"]:
            mode = "model"
            plan = apply_guards(parsed, allowed_ids=allowed, weeks=week_count)
        else:
            plan = build_skeleton(rows, weeks=week_count)
            reason = degrade_reason(finish_reason=finish_reason, content=content, parsed_ok=False)
    else:
        plan = build_skeleton(rows, weeks=week_count)
        reason = "未使用模型（use_model=false 或没有可用模型）"

    route_id = save_route(
        ctx.db,
        topic=topic,
        level=level,
        weeks=week_count,
        plan={
            **plan,
            "topic": topic,
            "level": level,
            "mode": mode,
            "mode_reason": reason,
            "weekly_hours": hours,
        },
        session_id=ctx.session_id,
    )

    return {
        "ok": True,
        "data": {
            "route_id": route_id,
            "topic": topic,
            "level": level,
            "mode": mode,
            "mode_reason": reason,
            "weeks": plan["weeks"],
            "stats": plan["stats"],
        },
        "refs": {"route_ids": [route_id], "source_ids": sorted(allowed)},
    }


SPEC = ToolSpec(
    name="route.plan",
    description=DESCRIPTION,
    parameters=PARAMETERS,
    handler=route_plan,
    toolset="research",
)


def register() -> None:
    from scholarscout.runtime import registry

    registry.register(SPEC, replace=True)
