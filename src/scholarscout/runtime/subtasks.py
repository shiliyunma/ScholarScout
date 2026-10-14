"""``fetch_many`` —— 并行 provider 扇出（子 Agent P0，见 ``docs/16 §3.3``）。

**这不是 agent，是一个函数。** 它把 ``tools/research_search.py`` 里"逐个来源顺序抓"的循环
换成线程池并发：N 条来源同时出网，攒齐后逐条返回。

四条铁律（违反任何一条都不算达成）：

1. **只并行网络抓取**：线程池里跑的**只有** ``provider.search``（出网）。
   绝不在此并发调模型 —— 摘要、判权、生成类工具仍归主循环，避免费用尖峰。
2. **单条失败不阻塞其它**：每个来源的异常就地兜住，逐条返回 :class:`ProviderFetch`；
   混合成败算成功（与 ``research.search`` 既有语义一致，见 ``docs/05 §3.3``）。
3. **不绕过限速**：每个来源的最小间隔仍按原口径取（``SearchSettings.interval_for``：
   配置表优先、provider 兜底），实际串行化在 ``fetch/http.py`` 的**出网层按 host** 生效 ——
   并行不会让任一 host 被超频（每个 provider 各自一个 host，互不干扰）。
4. **不新增运行期依赖**：只用标准库 ``concurrent.futures``（依赖政策见 ``docs/03 §5``）。

并发上限读 ``runtime.max_parallel_fetches``（默认 3）；``concurrency`` 参数可临时覆盖。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from scholarscout.fetch.http import FetchError
from scholarscout.providers.base import Query, SourceCard
from scholarscout.providers.llm import ChatReply, LlmError, ToolCall
from scholarscout.providers.settings import KNOWN
from scholarscout.runtime import context_budget, registry
from scholarscout.runtime.loop import bind_emitter
from scholarscout.runtime.toolctx import ToolContext, TurnBudget
from scholarscout.state import traces

#: 线程名前缀：真机排查（卡住/超时）时 ``threading.enumerate()`` 一眼能认出是谁
_THREAD_PREFIX = "scout-fetch"


@dataclass(frozen=True)
class ProviderFetch:
    """一条来源的一次抓取结果（成功与失败都在这里，便于逐条观测）。

    ``cards`` 只在成功时非空；``latency_ms`` 供新指标使用，
    **不进** :meth:`as_status`（``provider_status`` 的键集必须与改造前逐字一致）。
    """

    provider: str
    ok: bool
    n: int
    error: str | None = None
    interval_s: float | None = None
    latency_ms: int | None = None
    cards: tuple[SourceCard, ...] = ()

    def as_status(self) -> dict[str, Any]:
        """回给模型的 ``provider_status`` 条目。

        成功：``{provider, ok, n, interval_s}``；失败：``{provider, ok, n, error}``。
        键集与顺序和改造前的顺序循环**完全一致**（有契约测试钉住）。
        """
        if self.ok:
            return {
                "provider": self.provider,
                "ok": True,
                "n": self.n,
                "interval_s": self.interval_s,
            }
        return {"provider": self.provider, "ok": False, "n": self.n, "error": self.error}


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _resolve_concurrency(ctx: ToolContext, override: int | None) -> int:
    """并发上限：显式 ``concurrency`` 优先，否则读 ``runtime.max_parallel_fetches``。

    下限夹到 1（0 会让线程池起不来；负数更没意义）。
    """
    raw = ctx.config.runtime.max_parallel_fetches if override is None else override
    return max(1, int(raw))


def _unavailable(name: str, reasons: Mapping[str, str]) -> ProviderFetch:
    """来源没装配：区分"已知但没配"（带原因）与"名字根本不认识"。"""
    if name in KNOWN:
        return ProviderFetch(name, ok=False, n=0, error=reasons.get(name, "unavailable"))
    return ProviderFetch(name, ok=False, n=0, error="unknown_provider")


def fetch_many(
    ctx: ToolContext,
    providers: Mapping[str, Any],
    *,
    wanted: Sequence[str],
    query: str,
    per_provider: int,
    year_from: int | None = None,
    sort: str = "relevance",
    missing: Mapping[str, str] | None = None,
    concurrency: int | None = None,
) -> list[ProviderFetch]:
    """并行对 ``wanted`` 里的每个来源跑一次检索，按**原顺序**逐条返回结果。

    - ``providers``：``available_providers(ctx)`` 的产物（name → provider 实例）
    - ``wanted``：``resolve_wanted(...)`` 解析后的来源名（保序、去重）
    - ``missing``：``unavailable_providers(ctx)`` 的产物（没装配的来源 → 原因）
    - ``concurrency``：并发上限；``None`` 时读 ``runtime.max_parallel_fetches``

    返回的列表长度恒等于 ``len(wanted)``、顺序一致：调用方据此重建 ``provider_status``
    与汇总卡片。**任何单条异常都被就地兜住**，绝不会让整次扇出挂死或吞掉其它来源。
    """
    if not wanted:
        return []

    reasons = dict(missing or {})
    limit = _resolve_concurrency(ctx, concurrency)
    query_obj = Query(text=query, limit=per_provider, year_from=year_from, sort=sort)

    def run(name: str) -> ProviderFetch:
        provider = providers.get(name)
        if provider is None:
            return _unavailable(name, reasons)
        # 限速口径与顺序版一致：配置表优先、provider fallback（真实串行化在出网层）
        interval = ctx.config.search_cfg.interval_for(name, provider.min_interval_s)
        started = time.monotonic()
        try:
            found = provider.search(query_obj)
        except FetchError as exc:
            return ProviderFetch(
                name,
                ok=False,
                n=0,
                error=f"{exc.kind}: {exc}",
                interval_s=interval,
                latency_ms=_elapsed_ms(started),
            )
        except Exception as exc:  # 单来源异常必须就地兜住，不许外溢到线程池
            return ProviderFetch(
                name,
                ok=False,
                n=0,
                error=f"{type(exc).__name__}: {exc}",
                interval_s=interval,
                latency_ms=_elapsed_ms(started),
            )
        return ProviderFetch(
            name,
            ok=True,
            n=len(found),
            interval_s=interval,
            latency_ms=_elapsed_ms(started),
            cards=tuple(found),
        )

    workers = min(limit, len(wanted))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=_THREAD_PREFIX) as pool:
        futures = [pool.submit(run, name) for name in wanted]
        outcomes: list[ProviderFetch] = []
        for name, future in zip(wanted, futures, strict=True):
            try:
                outcomes.append(future.result())
            except Exception as exc:  # 兜底：worker 已兜住，防线程池层面的意外
                outcomes.append(
                    ProviderFetch(name, ok=False, n=0, error=f"{type(exc).__name__}: {exc}")
                )
    return outcomes


# ================================================================ 子 Agent P1：顺序子任务（ReWOO 式）
#
# 复刻 Hermes「orchestrator → leaf 委派」的**隔离语义**，但拍扁成同进程内的顺序小循环：
# 每个子任务是一个**独立的 mini-loop** —— 自建 messages、自己的工具子集与预算，只把
# **最终文本**带回来（父只看 summary），中间过程不落主会话消息。
#
# 三条铁律：
# 1. **不污染主会话**：不往 ``messages`` 表写任何东西（工具调用只落 ``tool_traces``，
#    ``tool_name`` 前缀 ``subtask:``，便于论文里单独统计）；用的是**独立的系统提示词**
#    ``SUBTASK_SYSTEM_PROMPT``，不碰 ``runtime/prompts.py`` → ``system_prompt_hash`` 不变。
# 2. **成本可预测**：顺序跑（不并发）、每个子任务预算小（默认 2 次工具）、整批受
#    **总额度池** ``total_budget`` 兜底（默认且上限 = ``max_tool_calls_per_turn`` 的 1/2）；
#    每次模型调用都按 ``usage_records.purpose="subtask"`` 如实记账。
# 3. **失败隔离**：任一子任务出错只影响它自己，其余照跑（``stop_reason="error"``）。
#
# **默认关**（``runtime.subtask_enabled=false``）：它一定会多花模型调用，必须由用户显式打开。

#: 事件回调：``emit(kind, payload)``（与 ``runtime/loop.Emitter`` 同形；此处本地定义以免反向依赖）
Emitter = Callable[[str, dict[str, Any]], None]

#: 子任务的系统提示词。**独立于主提示词** —— 不进 ``build_system_prompt``，
#: 所以新增/修改它都不会改变 ``system_prompt_hash``（改哈希＝换实验臂，本项目严禁顺手改）。
SUBTASK_SYSTEM_PROMPT = """\
你是 ScholarScout 的一个**子任务执行器**，只负责把一个具体的小问题查清楚并把结论写回来。

规矩：
1. 你的预算是**很小**的（默认最多 2 次工具调用）—— 别铺开，直奔这个问题，够用就收。
2. 检索关键词用英文（arXiv 只索引英文）；中文只用于你的结论。
3. 结论里给出可追溯的标识（source_id 或来源标题）；没有出处的内容要显式说明是通用知识。
4. 只输出这个子问题的**结论正文**（几段话），不要写"我完成了子任务"之类的过程叙述。
"""


@dataclass
class SubTaskResult:
    """一个子任务的产出：只有最终文本与它报出的出处/用量，**没有中间过程**（父只见 summary）。"""

    goal: str
    text: str
    refs: dict[str, list[str]]
    tool_calls: list[str]
    usage: dict[str, int]
    stop_reason: str


def open_goals(state: Mapping[str, Any] | None) -> list[str]:
    """从研究台账里取**还没有证据**的子问题 —— 这就是子任务的来源（接现有「缺口驱动续搜」）。

    只认 ``status != "covered"`` 的：模型没回填状态就保持 open（**不猜已覆盖**），
    与 ``research_state.compute_gaps`` 的缺口口径一致。
    """
    questions = (state or {}).get("questions") or []
    goals: list[str] = []
    for item in questions:
        if not isinstance(item, dict) or str(item.get("status")) == "covered":
            continue
        text = str(item.get("text") or "").strip()
        if text:
            goals.append(text)
    return goals


def subtasks_enabled(ctx: ToolContext) -> bool:
    """P1 总开关：``runtime.subtask_enabled``（**默认关**）。关着时 :func:`run_subtasks` 直接回空表。"""
    return bool(getattr(ctx.config.runtime, "subtask_enabled", False))


def _emit(emit: Emitter | None, kind: str, payload: dict[str, Any]) -> None:
    if emit is not None:
        emit(kind, payload)


def _accumulate_usage(total: dict[str, int], reply: ChatReply) -> None:
    for key, value in reply.usage.items():
        total[key] = total.get(key, 0) + int(value)


def _merge_refs(target: dict[str, list[str]], incoming: Any) -> None:
    if not isinstance(incoming, dict):
        return
    for key, values in incoming.items():
        if not isinstance(values, list):
            continue
        bucket = target.setdefault(str(key), [])
        for value in values:
            text = str(value)
            if text not in bucket:
                bucket.append(text)


def _run_subtask_tool(
    ctx: ToolContext,
    turn_id: str,
    step: int,
    call: ToolCall,
    seen_source_ids: set[str],
) -> tuple[str, dict[str, list[str]], str]:
    """执行一次子任务内的工具调用，返回（喂回模型的文本, refs, 规范工具名）。

    轨迹落 ``tool_traces``，``tool_name`` 前缀 ``subtask:`` —— 与主循环的调用分得开，
    论文里能单独统计"子任务花了多少次工具、成功率多少"。
    """
    spec = registry.get_spec(call.name)
    canonical = spec.name if spec is not None else call.name
    trace_name = f"subtask:{canonical}"
    session_id = ctx.session_id or ""
    if spec is None:
        traces.record_trace(
            ctx.db,
            session_id=session_id,
            turn_id=turn_id,
            step=step,
            tool_name=trace_name,
            ok=False,
            args=call.arguments,
            error="tool_not_registered",
        )
        return f"工具 {call.name} 未注册", {}, canonical
    try:
        arguments = call.parsed_arguments()
        result = registry.invoke(spec, ctx, arguments)
        traces.record_trace(
            ctx.db,
            session_id=session_id,
            turn_id=turn_id,
            step=step,
            tool_name=trace_name,
            ok=True,
            args=arguments,
            result=result,
        )
        refs = result.get("refs", {}) if isinstance(result, dict) else {}
        return (
            context_budget.project_tool_result(result, seen_source_ids=seen_source_ids),
            refs if isinstance(refs, dict) else {},
            canonical,
        )
    except Exception as exc:
        traces.record_trace(
            ctx.db,
            session_id=session_id,
            turn_id=turn_id,
            step=step,
            tool_name=trace_name,
            ok=False,
            args=call.arguments,
            error=f"{type(exc).__name__}: {exc}",
        )
        return f"工具 {call.name} 执行失败：{exc}", {}, canonical


def run_subtask(
    ctx: ToolContext,
    goal: str,
    *,
    toolset: str = "research",
    max_tool_calls: int = 2,
    emit: Emitter | None = None,
    seq: int = 0,
) -> SubTaskResult:
    """跑**一个**隔离的 mini-loop：自建 messages、自己的工具子集与预算，只回最终文本。

    - **不读也不写主会话的 messages**（``ctx.session_id`` 只用于给轨迹/用量挂账）；
    - 轮次预算 = 工具预算 + 1（与主 loop 同款语义）：最后一轮**不给工具**，逼它收口，
      免得"刚好用满预算"被误判 ``tool_limit`` 而丢掉最终答案；
    - 任一异常都收敛成 ``stop_reason``（``completed`` / ``tool_limit`` / ``error`` / ``empty``），绝不外抛。
    """
    turn_id = str(ctx.turn_id or "")
    goal_text = str(goal or "").strip()
    # 子任务事件（chat.subtask_start/done）也必须带 session_id（docs/20 §3.1）：
    # 主 loop 传进来的 emitter 已经绑过，这里再绑一次是为了**直接拿 ToolContext 起子任务**
    # 的路径（工具/测试）也不漏 —— 绑两次键值相同，无副作用。
    emit = bind_emitter(emit, ctx.session_id)
    if not goal_text:
        return SubTaskResult(
            goal="", text="", refs={}, tool_calls=[], usage={}, stop_reason="empty"
        )
    _emit(
        emit,
        "chat.subtask_start",
        {"turn_id": turn_id, "seq": seq, "index": seq, "goal": goal_text},
    )

    specs = registry.get_tools([toolset], features=ctx.config.features)
    tool_decls = registry.openai_tools(specs) or None
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SUBTASK_SYSTEM_PROMPT},
        {"role": "user", "content": goal_text},
    ]

    budget = max(1, int(max_tool_calls))
    executed: list[str] = []
    usage_total: dict[str, int] = {}
    refs_total: dict[str, list[str]] = {}
    seen_source_ids: set[str] = set()
    final_text = ""
    stop_reason = "completed"

    try:
        llm = ctx.require_llm()
        for _round in range(budget + 1):
            tools_for_round = None if len(executed) >= budget else tool_decls
            reply = llm.chat(messages, tools=tools_for_round)
            _accumulate_usage(usage_total, reply)
            # 每次模型调用立即记账（purpose="subtask"）——不会被中断吞掉，也不许变成暗开销
            traces.record_usage(
                ctx.db,
                model=ctx.config.chat.model,
                purpose="subtask",
                session_id=ctx.session_id,
                turn_id=turn_id or None,
                prompt_tokens=reply.usage.get("prompt_tokens"),
                completion_tokens=reply.usage.get("completion_tokens"),
                cached_tokens=reply.usage.get("cached_tokens"),
            )
            final_text = reply.content or final_text
            if not reply.tool_calls:
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.arguments},
                        }
                        for call in reply.tool_calls
                    ],
                }
            )
            if len(executed) + len(reply.tool_calls) > budget:
                stop_reason = "tool_limit"
                break
            for call in reply.tool_calls:
                text, refs, canonical = _run_subtask_tool(
                    ctx, turn_id, len(executed), call, seen_source_ids
                )
                _merge_refs(refs_total, refs)
                executed.append(canonical)
                messages.append(
                    {"role": "tool", "content": text, "tool_call_id": call.id, "name": call.name}
                )
                # 更早的工具结果折叠成一行：子任务预算小，但一样别把历史一路重发
                context_budget.collapse_old_tool_results(messages)
        else:
            stop_reason = "tool_limit"
    except LlmError as exc:
        stop_reason = "error"
        final_text = final_text or f"子任务模型调用失败：{exc.kind}: {exc}"
    except Exception as exc:
        stop_reason = "error"
        final_text = final_text or f"子任务执行失败：{type(exc).__name__}: {exc}"

    result = SubTaskResult(
        goal=goal_text,
        text=final_text,
        refs=refs_total,
        tool_calls=executed,
        usage=usage_total,
        stop_reason=stop_reason,
    )
    _emit(
        emit,
        "chat.subtask_done",
        {
            "turn_id": turn_id,
            "seq": seq,
            "index": seq,
            "stop_reason": stop_reason,
            "refs": refs_total,
        },
    )
    return result


def _total_budget(ctx: ToolContext, explicit: int | None) -> int:
    """总额度池：默认**且上限**都是主轮预算的一半 —— 子任务不该吃掉主 loop 的一半以上。"""
    ceiling = max(1, int(ctx.config.runtime.max_tool_calls_per_turn) // 2)
    if explicit is None:
        return ceiling
    return max(1, min(int(explicit), ceiling))


def run_subtasks(
    ctx: ToolContext,
    goals: Sequence[str],
    *,
    per_task_budget: int = 2,
    total_budget: int | None = None,
    emit: Emitter | None = None,
) -> list[SubTaskResult]:
    """**顺序**跑 K 个子任务（不做并发 → 峰值成本可预测）。

    - ``goals``：子任务清单（来源见 :func:`open_goals`）；
    - ``per_task_budget``：每个子任务的工具调用上限（默认 2）；
    - ``total_budget``：整批的**总额度池**（工具调用数）。不传＝主轮预算的 1/2，
      传入值也会被夹到该上限。池子耗尽后，剩余子任务直接标 ``stop_reason="budget"``；
    - **默认关**：``runtime.subtask_enabled=false`` 时直接回空表（不触发、不记账、不发事件）。

    失败隔离：任一子任务异常只影响它自己，其余照跑。
    """
    clean = [str(goal).strip() for goal in (goals or []) if str(goal).strip()]
    if not clean or not subtasks_enabled(ctx):
        return []

    pool = _total_budget(ctx, total_budget)
    per = max(1, int(per_task_budget))
    remaining = pool
    results: list[SubTaskResult] = []
    for index, goal in enumerate(clean):
        if remaining <= 0:
            results.append(
                SubTaskResult(
                    goal=goal, text="", refs={}, tool_calls=[], usage={}, stop_reason="budget"
                )
            )
            continue
        try:
            result = run_subtask(
                ctx, goal, max_tool_calls=min(per, remaining), emit=emit, seq=index
            )
        except Exception as exc:  # 兜底：run_subtask 已自行收敛，这里防意外
            result = SubTaskResult(
                goal=goal,
                text=f"子任务失败：{type(exc).__name__}: {exc}",
                refs={},
                tool_calls=[],
                usage={},
                stop_reason="error",
            )
        results.append(result)
        remaining -= len(result.tool_calls)
    return results


# ==================================================== 子 Agent P1 工具化：模型可见的委派入口
#
# `tools/delegate.py`（规范名 `research.delegate`，docs/01 §5.18）在这里取两样东西：
# ① 委派可用的**额度池**（= min(P1 上限, 主账剩余)）；② 跑完把消耗**回写**到主账。

#: 一次委派最多接受几个子问题（schema 与 handler 双重卡；>3 直接 ok=false）
MAX_DELEGATE_GOALS = 3


def delegate_budget(ctx: ToolContext, ledger: TurnBudget | None) -> int:
    """委派可用的工具预算 = ``min(P1 上限, 主账剩余)``。

    - ``P1 上限`` = :func:`_total_budget`（主轮预算的 1/2，子任务不该吃掉一半以上）；
    - ``ledger`` 是本轮主循环的 :class:`TurnBudget`（**同一本账**）；为 ``None`` 时（脚本直调、
      无主循环）退化为 P1 上限，行为与引入委托工具前一致；
    - 结果可能为 0 —— 此时委派应**拒绝**（``ok=false`` + "额度不足"），绝不绕过去。
    """
    cap = _total_budget(ctx, None)
    if ledger is None:
        return cap
    return max(0, min(cap, ledger.remaining))


def charge_budget(ledger: TurnBudget | None, results: Sequence[SubTaskResult]) -> int:
    """把子任务**实际消耗**的工具调用数回写到主账，返回扣掉的数（可测、可复现）。"""
    consumed = sum(len(result.tool_calls) for result in results)
    if ledger is not None and consumed:
        ledger.spend(consumed)
    return consumed
