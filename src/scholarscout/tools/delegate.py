"""``research.delegate`` —— 模型可见的委派入口（把独立子问题交给子任务，子 Agent P1 工具化）。

契约见 ``docs/01 §5.18``；底层的顺序子任务在 ``runtime/subtasks.py``（docs/16 §3.3）。
它只是给模型一个\"主动把几个互相独立的调研子问题分别探下去\"的把手 —— 不做并发、不开进程、不引依赖。

三条铁律（违反任何一条都不算达成）：

1. **绝不把异常抛回主循环**：开关关 / 额度尽 / 参数非法 / 内部异常四种情形一律
   ``ok=false`` + 可读原因（照 ``research.search`` 既有的失败回包约定），主循环照常往下走。
2. **失败隔离**：单个子任务失败只影响它自己（该条 ``stop_reason`` 如实标注），其余照跑；
   仅当**全部**子任务都没产出文本时整体才回 ``ok=false``。
3. **同一本账**：子任务内部消耗的工具调用数从本轮**主循环剩余预算**里扣
   （``ToolContext.budget``，即 ``runtime/toolctx.py:TurnBudget``）—— 不是另起一个池。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.subtasks import (
    MAX_DELEGATE_GOALS,
    charge_budget,
    delegate_budget,
    run_subtasks,
    subtasks_enabled,
)
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.tools.envelope import ErrorEnvelope, ToolResult, fail, ok

DESCRIPTION = (
    "把 2-3 个**互不依赖**的调研子问题分别交给子任务查清楚，返回各自的结论。"
    "每个子问题跑一个隔离的小循环（用它自己的小预算），只把结论正文带回来 —— "
    '适合"有几个独立的调研方向要分别探下去、且本轮还有工具预算"时使用。'
    "子任务的工具消耗**从本轮主循环的剩余预算里扣**；预算不够或功能未开启时返回 "
    "ok=false 并说明原因（请据此改走主流程，不要反复重试）。子问题要写成独立、可单独回答的短句。"
)

#: 注册用的 JSON Schema。``maxItems`` 卡住上限；模型不一定遵守 schema，所以 handler 里还会**硬校验**一次。
PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goals": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": MAX_DELEGATE_GOALS,
            "description": "子问题清单，每条是一句独立、可单独回答的短句；一次最多 3 条",
        }
    },
    "required": ["goals"],
    "additionalProperties": False,
}


def _fail(reason: str, *, requested: int = 0) -> ErrorEnvelope:
    """统一的失败包络（``ok=false`` + 原因；``data``/``refs`` 与成功时同形，方便调用方不分支）。"""
    return fail(
        "delegate_unavailable",
        reason,
        data={
            "items": [],
            "counts": {"requested": requested, "ran": 0, "failed": 0},
            "consumed_tool_calls": 0,
        },
        refs={"source_ids": []},
    )


def _merge_refs(sources: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """把各子任务报出的 refs 归并去重（任何进入回答的 id 都要能被追溯）。

    入参是**各子任务的 refs 映射**（``SubTaskResult.refs``）。**别拿 ``items`` 来归并**：
    ``items`` 的键集是契约固定的 ``{goal,text,stop_reason,tool_calls}``、不含 ``refs``，
    照它归并会**恒得空表** —— 子任务发现的来源就进不了本轮的 refs，界面「这一次生成了什么」会漏。
    """
    merged: dict[str, list[str]] = {}
    for refs in sources:
        if not isinstance(refs, dict):
            continue
        for key, values in refs.items():
            if not isinstance(values, list):
                continue
            bucket = merged.setdefault(str(key), [])
            for value in values:
                text = str(value)
                if text not in bucket:
                    bucket.append(text)
    return merged


def research_delegate(ctx: ToolContext, goals: list[str] | None = None) -> ToolResult:
    """跑一批**互不依赖**的子问题，返回各自的结论。

    四种失败情形都在这里收敛成 ``ok=false``（绝不外抛异常）：
    ① 开关关（``runtime.subtask_enabled=false``）；② 额度不足（主账剩余 ≤ 0）；
    ③ 参数非法（``goals`` 非数组 / 空 / 超过 3 个）；④ 内部异常。
    """
    # ① 开关关（默认关；**复用 P1 的 runtime.subtask_enabled**，不新增开关）
    if not subtasks_enabled(ctx):
        return _fail(
            "子任务总开关未打开：runtime.subtask_enabled=false"
            "（在 config.yaml 的 runtime 段开启后再试，或直接用主流程自己推进）"
        )

    # ② 参数非法
    if not isinstance(goals, list):
        return _fail("参数非法：goals 必须是字符串数组（每条是一句独立的调研子问题）")
    clean = [str(goal).strip() for goal in goals if str(goal).strip()]
    if not clean:
        return _fail("参数非法：goals 不能为空（至少要有一条独立的子问题）")
    if len(clean) > MAX_DELEGATE_GOALS:
        return _fail(
            f"参数非法：一次最多 {MAX_DELEGATE_GOALS} 个子问题（收到 {len(clean)} 条）—— 请拆成多次委派",
            requested=len(clean),
        )

    # ③ 额度不足：池子 = min(P1 上限, 主账剩余)。剩余为 0 就**拒绝**，不绕过去。
    pool = delegate_budget(ctx, ctx.budget)
    if pool <= 0:
        return _fail(
            "额度不足：本轮工具预算已用尽（子任务的消耗要从主循环剩余预算里扣）—— "
            "请用主流程自己推进，或等下一轮再委派",
            requested=len(clean),
        )

    # ④ 内部异常：run_subtasks 自己已做逐条失败隔离，这里兜住它之外的意外（如注册表/记账层出问题）。
    try:
        results = run_subtasks(
            ctx,
            clean,
            total_budget=pool,
            emit=ctx.emit,
        )
    except Exception as exc:
        # 铁律：任何异常都不许抛回主循环 —— 收敛成 ok=false + 原因（照 research.search 的失败回包约定）
        return _fail(f"内部异常：{type(exc).__name__}: {exc}", requested=len(clean))

    # **同一本账**：把子任务实际消耗的工具调用数回写到主循环的预算账上。
    consumed = charge_budget(ctx.budget, results)
    items: list[dict[str, Any]] = [
        {
            "goal": result.goal,
            "text": result.text,
            "stop_reason": result.stop_reason,
            "tool_calls": list(result.tool_calls),
        }
        for result in results
    ]
    ran = sum(1 for item in items if str(item["text"]).strip())

    if ran == 0:
        # 全部子任务都没拿到结论：如实回失败（与 research.search 的"全来源失败"口径一致）。
        return fail(
            "delegate_no_result",
            "全部子任务都没产出结论（预算或检索失败）—— 请用主流程自己推进",
            data={
                "items": items,
                "counts": {"requested": len(clean), "ran": 0, "failed": len(clean)},
                "consumed_tool_calls": consumed,
            },
            refs=_merge_refs(result.refs for result in results),
        )

    return ok(
        {
            "items": items,
            "counts": {"requested": len(clean), "ran": ran, "failed": len(clean) - ran},
            "consumed_tool_calls": consumed,
        },
        refs=_merge_refs(result.refs for result in results),
    )


SPEC = ToolSpec(
    name="research.delegate",
    description=DESCRIPTION,
    parameters=PARAMETERS,
    handler=research_delegate,
    toolset="research",
)


def register() -> None:
    from scholarscout.runtime import registry

    registry.register(SPEC, replace=True)
