"""``tool.search`` / ``tool.describe`` —— 工具目录的两个元工具（渐进披露的入口，docs/01 §3.17）。

为什么工具要分两层（E4 实测，docs/26）：11 个工具的 schema 全常驻时，逐轮工具名准确率只有
**0.067**，实际调用 64 次对 gold 15 次 —— 声明越多，模型越选不准，而且每一轮都为它们付 token。
所以提示词/工具声明里只留 :data:`~scholarscout.runtime.disclosure.CORE_TOOLS` 与这两个元工具，
其余能力由模型自己"查目录 → 取 schema → 调用"。

分工（为什么必须是**两个**工具，而不是一个"给名字就回 schema、不给就回目录"的）：

1. ``tool.search`` 只回**名字 + 一句话用途**（:func:`~...disclosure.one_line`），不回 ``parameters`` ——
   目录是"有什么可用"，代价必须小到可以随便翻；
2. ``tool.describe`` 回**完整 schema**，并把这些工具加进**本会话**的可用集（之后可直接调用）。
   一次最多 :data:`~...disclosure.MAX_DESCRIBE` 个：不设上限的话模型会一口气把目录全拉回来，
   渐进披露就白做了（与 ``skill.list`` / ``skill.load`` 两段式同一个理由）。

没 describe 就直调 = ``tool_not_loaded``（闸在 ``runtime/turn_steps.py::execute_tool``），
错误文案由 :func:`~...disclosure.not_loaded_message` 统一给，教它"先 describe 再调"。

回包走 :mod:`scholarscout.tools.envelope`：成功 ``{ok, data, refs}``，失败 ``{ok:false, error, ...}``。
"""

from __future__ import annotations

from typing import Any

from scholarscout.runtime import disclosure, registry
from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.tools.envelope import ToolResult, fail, ok

#: ``tool.search`` 默认回多少条（一页 10 条：够看清目录，也不至于把一屏塞满）
DEFAULT_LIMIT = 10

#: 上限：再多的正确做法是用 ``query`` 收窄，而不是一次读几十条
MAX_LIMIT = 50

SEARCH_DESCRIPTION = (
    "翻工具目录：只回**名字 + 一句话用途**（不返参数定义），支持字符匹配；"
    "不给 query 就按名字列当前会话允许的全部工具。"
    "要用某个工具之前先在这里找到名字，再 tool.describe 取它的完整参数定义。"
)

DESCRIBE_DESCRIPTION = (
    "取回工具的**完整 schema**（参数定义），并把它们加入**本会话**的可用集 —— 之后就能直接调用。"
    "一次最多 3 个；名字要从 tool.search 里拿，不要凭印象编。"
    "没 describe 过的工具直接调用会回 tool_not_loaded。"
)

SEARCH_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "能力关键词（如 map / route / grade / 代码 / 技能 / 委派 / 文件）；留空列全部",
        },
        "limit": {
            "type": "integer",
            "description": f"最多回多少条（默认 {DEFAULT_LIMIT}，上限 {MAX_LIMIT}）",
            "minimum": 1,
            "maximum": MAX_LIMIT,
            "default": DEFAULT_LIMIT,
        },
    },
    "required": [],
    "additionalProperties": False,
}

DESCRIBE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "names": {
            "type": "array",
            "items": {"type": "string"},
            "description": f"要加载的工具名（从 tool.search 拿到），一次最多 {disclosure.MAX_DESCRIBE} 个",
            "minItems": 1,
            "maxItems": disclosure.MAX_DESCRIBE,
        },
    },
    "required": ["names"],
    "additionalProperties": False,
}


def _clamp_limit(limit: int | None) -> int:
    """把 limit 收进 ``[1, MAX_LIMIT]``；回包带**生效值**，所以不是静默改参数。"""
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


def tool_search(ctx: ToolContext, query: str | None = None, limit: int | None = None) -> ToolResult:
    """列目录（**只有名字与一句话**）。范围 = 本会话允许的工具集，元工具自己不在里面。"""
    effective = _clamp_limit(limit)
    text = (query or "").strip()
    page, matched = disclosure.search_entries(
        ctx.config, query=text, limit=effective, session_id=ctx.session_id
    )
    return ok(
        {
            "query": text,
            "matched": matched,
            "count": len(page),
            "limit": effective,
            "truncated": matched > len(page),
            "loaded": sorted(disclosure.loaded_names(ctx.session_id)),
            "tools": page,
            "note": (
                "目录只有名字与一句话；要参数定义就 tool.describe（一次最多 "
                f"{disclosure.MAX_DESCRIBE} 个），加载后本会话内可直接调用。"
            ),
        },
        refs={"tool_names": [item["name"] for item in page]},
    )


def _normalize_names(names: Any) -> list[str]:
    """去空白、去重、保序 —— 重复名字不该占掉 3 个额度里的两个。"""
    if not isinstance(names, list):
        return []
    ordered: list[str] = []
    for item in names:
        text = str(item or "").strip()
        if text and text not in ordered:
            ordered.append(text)
    return ordered


def _reject_reason(name: str, allowed: dict[str, registry.ToolSpec]) -> str | None:
    """为什么这个不能加载；能加载时回 ``None``。

    三种拒绝各有不同出路，所以文案分开写：**不存在**（换词去 search）/ **工具集关着**
    （开关没开，模型做什么都没用，如实说）/ **不在本会话的工具集里**（这个会话只要了别的工具集）。
    """
    if name in allowed:
        return None
    spec = registry.get_spec(name)
    if spec is None:
        return f"{name} 不存在（先用 tool.search 用能力词找准确名字，不要凭印象编）"
    if spec.toolset in registry.GATED_TOOLSETS:
        return (
            f"{spec.name} 属于 {spec.toolset} 工具集，本机没开对应的特性开关"
            "（config.yaml 的 features），所以拿不到它 —— 如实告诉用户这个能力当前没启用"
        )
    return (
        f"{spec.name} 属于 {spec.toolset} 工具集，但本会话没有启用它"
        "（会话的 toolsets 里没有这个分组）—— 换成本会话有的工具，或让用户新开一个要它的会话"
    )


def tool_describe(ctx: ToolContext, names: Any = None) -> ToolResult:
    """取完整 schema 并加载进本会话（≤3 个，原子：有一个不行就一个都不加载）。"""
    wanted = _normalize_names(names)
    if not wanted:
        return fail(
            "names_required",
            'tool.describe 需要 names：例如 tool.describe({"names": ["route.plan"]})。'
            "名字先用 tool.search 找到。",
            data={"error_kind": "invalid_argument"},
        )
    if len(wanted) > disclosure.MAX_DESCRIBE:
        return fail(
            "too_many_names",
            f"tool.describe 一次最多 {disclosure.MAX_DESCRIBE} 个（这次给了 {len(wanted)} 个）。"
            "先想清楚这一轮真正缺哪个能力，只加载那几个；剩下的下一轮再说。",
            data={
                "error_kind": "too_many_names",
                "limit": disclosure.MAX_DESCRIBE,
                "requested": wanted,
            },
        )

    allowed = {
        spec.name: spec for spec in disclosure.allowed_specs(ctx.config, session_id=ctx.session_id)
    }
    for meta in disclosure.META_TOOLS:
        spec = registry.get_spec(meta)
        if spec is not None:
            allowed.setdefault(spec.name, spec)

    rejected: dict[str, str] = {}
    for name in wanted:
        reason = _reject_reason(name, allowed)
        if reason is not None:
            rejected[name] = reason
    if rejected:
        return fail(
            "tool_not_loadable",
            "；".join(rejected.values()) + "。本次一个都没有加载（要么全成、要么全不动）。",
            data={"error_kind": "not_loadable", "rejected": sorted(rejected)},
        )

    loaded = disclosure.load(ctx.session_id, wanted)
    return ok(
        {
            "count": len(wanted),
            "limit": disclosure.MAX_DESCRIBE,
            "loaded": list(loaded),
            "tools": [disclosure.describe_payload(allowed[name]) for name in wanted],
            "note": (
                "这些工具已加入本会话的可用集，现在可以直接调用（参数按上面的 parameters）。"
                "新会话不会继承，需要时再 describe。"
            ),
        },
        refs={"tool_names": wanted},
    )


SPEC_SEARCH = ToolSpec(
    name="tool.search",
    description=SEARCH_DESCRIPTION,
    parameters=SEARCH_PARAMETERS,
    handler=tool_search,
    toolset="research",
)

SPEC_DESCRIBE = ToolSpec(
    name="tool.describe",
    description=DESCRIBE_DESCRIPTION,
    parameters=DESCRIBE_PARAMETERS,
    handler=tool_describe,
    toolset="research",
)


def register() -> None:
    from scholarscout.runtime import registry as _registry

    _registry.register(SPEC_SEARCH, replace=True)
    _registry.register(SPEC_DESCRIBE, replace=True)
