"""``kb.search`` —— 在**本地已收录资料**里检索（docs/01 §5.8，受 ``features.enable_kb`` 控制）。

**默认关**：`features.enable_kb=false` 时这个工具**不会注册**（`runtime/registry.py::enabled_toolsets`
把 `kb` 工具集摘掉），所以模型清单里根本没有它 —— 开关不是「开了什么都不发生」的假开关，
而是真的决定「有没有这个工具」（`tests/runtime/test_registry.py::test_enable_kb_switch_controls_kb_search`）。

它查的是**本项目自己的库**：`documents`/`sources`/`summaries` 里的标题、摘要、标签，加上
`data/text/` 下的抽取全文（索引在 `state/kb_index.py`，正文不进资料库表）。要新资料仍然用
`research.search` 联网检索 —— 两个工具不是一回事，提示词里也这么分。

返回只给**定位信息 + 一小段带高亮的片段**（`«»`，≤300 字），不给整篇正文：整篇会吃掉本轮
上下文预算，而模型真要读全文有 `source.fetch` 那条路。
"""

from __future__ import annotations

from typing import Any

from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state.kb_index import KbIndex
from scholarscout.tools.envelope import ToolResult, fail, ok

DESCRIPTION = (
    "在本地已收录的资料里检索（库内标题/摘要/标签 + data/text 抽取全文），"
    "回答「我以前收过什么/哪篇讲过 X」。这不是联网检索（要新资料用 research.search）。"
    "**默认关**：只有 features.enable_kb=true 时本工具才会注册。"
)

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "检索词。中文按逐字命中（「深度」能命中「深度学习」），英文不分大小写、支持前缀",
        },
        "limit": {
            "type": "integer",
            "description": "最多回几条（默认 10，夹在 1–50）",
            "minimum": 1,
            "maximum": 50,
        },
        "source_types": {
            "type": "array",
            "items": {"type": "string"},
            "description": "只在这几类来源里搜（库里 source_type 的取值，如 arxiv / github / web）",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


def kb_search(
    ctx: ToolContext,
    query: str,
    limit: int = 10,
    source_types: list[str] | None = None,
) -> ToolResult:
    """检索本地知识库；空 query 是**参数错**（绝不退化成「列出全部资料」）。"""
    if not isinstance(query, str) or not query.strip():
        return fail(
            "invalid_query",
            "query 不能为空：kb.search 不给「列出全部资料」这种降级行为，请给一个检索词",
            data={"error_kind": "invalid_query"},
        )
    try:
        limit_value = int(limit)
    except (TypeError, ValueError):
        return fail(
            "invalid_limit", f"limit 必须是整数：{limit!r}", data={"error_kind": "invalid_limit"}
        )

    index = KbIndex.for_config(ctx.db, ctx.config)
    try:
        outcome = index.search(query, limit=limit_value, source_types=source_types)
    finally:
        index.close()

    data: dict[str, Any] = {
        "query": outcome.query,
        "total": outcome.total,
        "items": [item.as_dict() for item in outcome.items],
        # 降级时不静默：模型能看到这次不是 FTS5/bm25 排的（理由见 state/kb_index.py 顶注）
        "index_mode": outcome.mode,
        "indexed_docs": outcome.docs,
    }
    if outcome.total == 0:
        data["hint"] = "本地库里没有含这些词的资料；要新资料请用 research.search 联网检索"
    return ok(
        data,
        refs={
            "source_ids": _unique(item.source_id for item in outcome.items),
            "doc_ids": _unique(item.doc_id for item in outcome.items),
        },
    )


def _unique(values: Any) -> list[str]:
    """去重保序（`refs` 里同一个 id 出现两次等于噪声）。"""
    seen: list[str] = []
    for value in values:
        text = str(value)
        if text and text not in seen:
            seen.append(text)
    return seen


SPEC = ToolSpec(
    name="kb.search",
    description=DESCRIPTION,
    parameters=PARAMETERS,
    handler=kb_search,
    toolset="kb",
)


def register() -> None:
    from scholarscout.runtime import registry

    registry.register(SPEC, replace=True)
