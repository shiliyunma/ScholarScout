"""单轮对话循环。

流程（docs/01 §3.2 的 ``chat.send`` 语义）：

    落用户消息 → 组上下文 → 调模型 → 执行工具（若有）→ 落助手消息 → 记用量 → 上报事件

约束：
- 工具调用次数受 ``runtime.max_tool_calls_per_turn`` 限制，超限 ``stop_reason="tool_limit"``
- 轮次预算 = 工具预算 + 1：最后一轮**不给工具**，留给模型把结果讲完（用满工具预算≠tool_limit）
- 模型返回了未注册的工具 → 记 trace（``ok=0``）并如实告诉模型，**不伪造结果**
- 助手消息与工具消息都落库，保证会话可回放
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from scholarscout.config import Config
from scholarscout.providers.llm import LlmError
from scholarscout.runtime import (
    budget as daily_budget,
)
from scholarscout.runtime import (
    compaction,
    context_budget,
    disclosure,
    registry,
    research_state,
)
from scholarscout.runtime.prompts import build_system_prompt, system_prompt_hash
from scholarscout.runtime.toolctx import TurnBudget
from scholarscout.runtime.turn_steps import (
    Emitter,
    TurnContext,
    TurnResult,
    account_tool_result,
    append_tool_call_request,
    apply_coverage,
    bind_emitter,
    call_model,
    emit_event,
    execute_tool,
    extract_memory,
    maybe_compact,
    merge_refs,
    prepare_research_state,
    render_context_note,
)
from scholarscout.state import sessions
from scholarscout.state.db import Database

logger = logging.getLogger(__name__)

_HISTORY_LIMIT = 40
#: 历史正文的字符预算（默认 12,000 ≈ 4,000 tokens）。
#: 实测当前会话规模下历史只有 ~1,000 tokens（12 条消息），所以这条**不是**主要省钱手段 ——
#: 它的作用是**防长会话失控**：40 条消息每轮重发，token 会随轮次线性膨胀。
_HISTORY_CHAR_BUDGET = 12000


def _fit_history(history: list[dict[str, Any]], budget_chars: int) -> list[dict[str, Any]]:
    """按字符预算裁剪历史：**保头（最初的需求）保尾（最近的对话）**，中间丢掉并显式说明。

    为什么不直接砍掉最早的几条：用户最初那句话定义了整个任务（"我想入门 XX"），丢了它
    模型就不知道自己在回答什么；而最近几轮决定当前上下文。所以两头都留。
    为什么丢中间要留一句说明：不说明的话模型会以为对话就这么短，可能重复问已经说过的信息。
    """
    if budget_chars <= 0 or not history:
        return history
    if sum(len(str(item.get("content") or "")) for item in history) <= budget_chars:
        return history

    first_user = next(
        (item for item in history if item.get("role") == "user" and item.get("content")), None
    )
    kept: list[dict[str, Any]] = []
    used = 0
    for item in reversed(history):
        size = len(str(item.get("content") or ""))
        if kept and used + size > budget_chars:
            break
        kept.append(item)
        used += size
    kept.reverse()

    dropped = len(history) - len(kept)
    # 前缀稳定性（A6）：**头部不许变**。服务端上下文缓存按"从头开始的最长公共前缀"命中，
    # 而缓存命中的输入单价差一个量级。所以：
    #   ① 最初那条用户消息固定在 system 之后（下轮也一样）→ 前缀 [system, first_user] 恒定；
    #   ② "中间省略"的说明**追加到末尾**，不再插在头部；
    #   ③ 被裁掉的是中间那段，且下一轮裁掉的只会更多 → 后面整体是 append-only 的形态。
    prefix: list[dict[str, Any]] = []
    if first_user is not None and all(item is not first_user for item in kept):
        prefix.append(first_user)
    if dropped <= 0:
        return kept
    note = {
        "role": "system",
        "content": (
            f"（为控制上下文成本，中间 {dropped} 条对话已省略；"
            "上面是用户最初的需求与最近的对话。需要旧信息时再问用户。）"
        ),
    }
    return [*prefix, *kept, note]


def _prompt_toolsets(toolsets: Sequence[str] | None, config: Config) -> list[str]:
    """提示词里那行「当前可用工具集」必须与实际工具一致 —— 所以同样过开关收敛。

    只在 `build_messages` / `mark_system_prompt` 用：`enable_kb` 关着时那行仍是 `research`，
    提示词因此与旧版**逐字节相同**（黄金哈希不变，历史实验数字仍可比）。
    """
    return list(
        registry.enabled_toolsets(
            toolsets,
            enable_kb=config.features.enable_kb,
            enable_files=config.features.enable_files,
        )
    )


def build_messages(
    db: Database,
    session_id: str,
    *,
    config: Config,
    toolsets: Sequence[str] | None,
) -> list[dict[str, Any]]:
    """系统提示 + 会话历史（只取有内容的用户/助手消息）。"""
    prompt = build_system_prompt(
        toolsets=_prompt_toolsets(toolsets, config),
        allow_clarify=config.runtime.allow_clarify,
        auto_artifacts=config.generation.auto_artifacts,
        style=getattr(config.generation, "style", "default"),
        disclosure=bool(config.features.enable_tool_disclosure),
    )
    messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
    # D3：较早对话的提要固定在 system 之后、历史之前 —— 位置固定，服务端缓存才能在
    # 两次压缩之间稳定命中（每次压缩只让缓存冷一次，而不是每轮都冷）。
    state = research_state.load_state(db, session_id) or {}
    summary = str(state.get(compaction.SUMMARY_KEY) or "").strip()
    if summary:
        messages.append({"role": "system", "content": compaction.render_summary_note(summary)})
    history, _ = sessions.list_messages(db, session_id, limit=_HISTORY_LIMIT)
    for row in history:
        if row["role"] in {"user", "assistant"} and row["content"].strip():
            messages.append({"role": row["role"], "content": row["content"]})
    # 保头保尾地裁到字符预算内（长会话防失控；短会话原样返回）
    budget = int(getattr(config.runtime, "max_history_chars", _HISTORY_CHAR_BUDGET) or 0)
    return [messages[0], *_fit_history(messages[1:], budget)]


def run_turn(
    ctx: TurnContext,
    *,
    session_id: str,
    text: str,
    toolsets: Sequence[str] | None = None,
    emit: Emitter | None = None,
    turn_id: str | None = None,
) -> TurnResult:
    """跑一轮：发消息、调模型、执行工具、落库。异常一律收敛成 ``stop_reason``。

    ``turn_id`` 是**预分配**的轮次号（多会话并行要它，docs/20 §2.3）：`SessionRuntime.submit_turn`
    必须在整轮跑完**之前**就把 `turn_id` 交给界面（`mode="async"` 立即返回），所以轮次号不能等到
    这里才生成。不传时自己生成 —— 老的调用点语义一字不变。
    """
    # 事件信封统一带 session_id（docs/20 §3.1）：在这里绑一次，本函数与 _execute_tool 的
    # 全部事件（chat.delta/tool_call/tool_result/error/done）自动带上，调用点不必各自传。
    emit = bind_emitter(emit, session_id)
    turn_id = turn_id or sessions.new_id("trn")
    sessions.append_message(ctx.db, session_id, role="user", content=text)

    specs = ctx.tools(toolsets)
    # ---- 渐进披露（docs/01 §3.17）：只在 `features.enable_tool_disclosure` 开着时生效。
    # 两件事必须同时做，缺一个就成了"半成品"：
    # ① 登记本会话允许的工具全集（目录范围）—— `tool.search` / `tool.describe` 与直调闸
    #    都从这里取，会话的 `toolsets` 可能来自前端，本模块拿不到，只能在取工具这一步顺手记；
    # ② 把**发给模型的声明**裁到「核心 + 元工具 + 本会话已加载」—— 光有闸不裁声明，
    #    模型会看到 11 个工具却只能调 3 个，比不披露更糟。
    disclosure_on = bool(ctx.config.features.enable_tool_disclosure)
    if disclosure_on:
        disclosure.remember_universe(session_id, [spec.name for spec in specs])
    declared = disclosure.declared_specs(specs, session_id=session_id) if disclosure_on else specs
    # ---- D3：历史超过阈值就先压一次（同一段只压一次；失败退回规则式裁剪，本轮照常）。
    maybe_compact(ctx, session_id=session_id, turn_id=turn_id)
    messages = build_messages(ctx.db, session_id, config=ctx.config, toolsets=toolsets)
    tool_decls = registry.openai_tools(declared) or None

    # ---- A1/A2：跨轮研究台账与本轮检索偏好位（详见 _prepare_research_state）。
    state = prepare_research_state(ctx, session_id=session_id, text=text, turn_id=turn_id)

    note = render_context_note(ctx, session_id=session_id, state=state)
    messages.append({"role": "system", "content": note})

    executed: list[str] = []
    usage_total: dict[str, int] = {}
    #: 本轮新发现的来源（A7 的判据：连续几轮没有新增就该停）
    fresh_sources: list[str] = []
    stop_hinted = False
    #: 本轮已发给模型的 source_id —— 同一条来源第二次出现时只回锚点，摘要不再重复计费
    seen_source_ids: set[str] = set()
    stop_reason = "completed"
    final_text = ""
    error_message: str | None = None

    # 轮次预算 = 工具预算 + 1：**最后那一轮不给工具**，专门留给模型把结果讲成人话。
    # 否则模型刚好用满工具预算时会被判 tool_limit，最终答案直接丢掉（踩过）。
    tool_budget = max(1, ctx.config.runtime.max_tool_calls_per_turn)
    # 本轮工具预算账：主循环与子任务（research.delegate）**共用同一本账** ——
    # 子任务内部消耗的工具调用数会 spend 回这个对象，于是主循环的剩余预算跟着变少。
    budget = TurnBudget(tool_budget)
    refs_total: dict[str, list[str]] = {}
    # ---- 流式（总开关 `runtime.stream`）：**每一轮**都开，带工具的轮次也不例外。
    # 为什么不能只流"最后一轮"（原实现，真机复现的 bug）：那个条件要求 `tools_for_round is None`，
    # 而 tools=None 只发生在**工具预算被用满**之后的强制收口轮。模型通常 2–3 次工具调用就自己收口，
    # 于是流式分支**永远进不去** —— 实测：`chat_stream` 被调 0 次、`chat.delta` 只有收尾那 1 条、
    # 一整轮 100s 里正文是一次性蹦出来的。
    # 现在每一轮都走 `chat_stream(messages, tools=...)`：正文增量实时推出；
    # 工具调用参数由 providers/llm.py 按 index 拼装（**不进正文增量**），
    # 所以需要严格 JSON 的结构化调用不会被流式污染。
    # 非流式路径一字不变：没开流式时 deltas_sent 恒为 0，收尾仍照旧推一条最终 chat.delta。
    stream_enabled = bool(getattr(ctx.config.runtime, "stream", False)) and callable(
        getattr(getattr(ctx, "llm", None), "chat_stream", None)
    )
    deltas_sent = 0
    streamed_text = ""
    # ---- 每日用量封顶（docs/20 §5.2 第二处检查点）：**每一次模型调用之前**都要判。
    # 没配上限时 `guard is None` → 整段零成本、行为与引入前逐字一致（可断言的回归点）。
    # 超限**不新造事件名**：收口只体现在 `chat.done` 的 `stop_reason="budget"`。
    guard = daily_budget.from_runtime(ctx.config.runtime)
    budget_stop_used = False
    # ---- 会话级工具调用上限（docs/20 §5.1，评审 R-07）：**每一次工具调用之前**都要判。
    # 与每日封顶同一套"无工具收口"语义，只是理由不同（`session_tool_limit`），且**不回血**：
    # 计数来自 `tool_traces`（跨轮累计、落库），所以重启进程后照样算得准。
    # 没配上限时 `session_guard is None` → 不查库、不判断，行为与引入前逐字一致。
    session_guard = daily_budget.session_guard_from_runtime(ctx.config.runtime)
    session_stop_used = False
    session_summary_sent = False
    try:
        # 先把"这个会话正在跑哪一轮"记下来：界面点「停止」时按会话找到它（整轮结束前拿不到 turn_id）
        ctx.running[session_id] = turn_id
        for _round in range(tool_budget + 1):
            if ctx.is_cancelled(turn_id):
                stop_reason = "cancelled"
                break
            #: 本轮是否已经"卡在等用户选"（见下面工具循环里的缺陷 3 检查点）
            awaiting_choice = False
            tools_for_round = None if (stop_hinted or budget.remaining <= 0) else tool_decls
            # 预算检查点：已超则**当场收口** —— 只再给一次"不给工具"的总结机会，
            # 之后不再调模型（否则"封顶"每轮都还要再花一次钱，等于没封）。
            if guard is not None and guard.exceeded(ctx.db) is not None:
                stop_reason = daily_budget.STOP_REASON
                if budget_stop_used:
                    break
                budget_stop_used = True
                tools_for_round = None
            # ---- 会话级上限用完：同样只再给**一次**无工具收口的机会（与上面那条一样），
            # 之后不再调模型 —— 否则"封顶"每轮都还要再花一次钱。
            if session_stop_used:
                if session_summary_sent:
                    break
                session_summary_sent = True
                tools_for_round = None
                # 只说清"为什么没有工具"，不改系统提示词（黄金哈希不受影响）
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "本会话的工具调用次数已达上限"
                            "（runtime.max_tool_calls_per_session）。本轮**不再给你工具**："
                            "请用已有资料如实总结（现在能确认什么、还缺什么、下一步怎么做），"
                            "并明确告诉用户新开一个会话可以继续。不要假装调用过工具。"
                        ),
                    }
                )
            reply, deltas_sent, streamed_text = call_model(
                ctx,
                messages,
                stream_enabled=stream_enabled,
                tools=tools_for_round,
                emit=emit,
                session_id=session_id,
                turn_id=turn_id,
                deltas_before=deltas_sent,
                usage_total=usage_total,
            )
            final_text = reply.content or final_text

            if not reply.tool_calls:
                break

            if budget_stop_used:
                # 已封顶却仍要工具：**不执行**（不给新工具），就在这里收口。
                # 不在落库前 append 那条 assistant tool_calls，免得留下"有调用没结果"的悬空消息。
                break

            append_tool_call_request(ctx, session_id=session_id, reply=reply, messages=messages)

            if budget.remaining < len(reply.tool_calls):
                stop_reason = "tool_limit"
                emit_event(
                    emit,
                    "chat.error",
                    {"turn_id": turn_id, "code": -32603, "message": "工具调用超限"},
                )
                break

            for index, call in enumerate(reply.tool_calls):
                # ---- 缺陷 3：**遇到选择请求就停手**（第一处检查点在"执行完这次"之后）。
                # 为什么不能先跑完再看：选择卡是**轮次中途**由工具推上来的（``chat.needs_choice``），
                # 那一刻用户还没点；同一 reply 里排在后面的调用若照跑（检索/抓取/子任务），
                # 就成了"卡还没点、搜索已经发生"。已执行完的不回滚（回滚会造成更怪的不一致），
                # 但**不再发起新的**。
                if awaiting_choice:
                    messages.extend(
                        {
                            "role": "tool",
                            "content": (
                                "未执行：本轮在等你选择检索方向（选择卡已发给用户），"
                                "这一轮到此结束。用户点选后会在下一轮继续。"
                            ),
                            "tool_call_id": pending.id,
                            "name": pending.name,
                        }
                        for pending in reply.tool_calls[index:]
                    )
                    break
                # 会话级检查点（docs/20 §5.1）：**真正执行这一次调用之前**按会话累计判一次。
                # 超限就**不执行**（一次都不再调），转入无工具收口 —— 已生成的正文不丢。
                session_spent = (
                    session_guard.exceeded(ctx.db, session_id)
                    if session_guard is not None
                    else None
                )
                if session_spent is not None:
                    stop_reason = daily_budget.SESSION_STOP_REASON
                    session_stop_used = True
                    # 文案必须带上**具体数字**：用户据"上限几次、已用几次"判断要不要新开会话，
                    # 只说一句"超限了"等于让他去翻配置文件。
                    note = (
                        f"未执行：本会话工具调用已达上限 {session_spent['limit']} 次"
                        f"（已用 {session_spent['used']} 次，"
                        "runtime.max_tool_calls_per_session）。"
                    )
                    # 没执行的调用也要有一条**如实**的 tool 回复：assistant 那批 tool_calls
                    # 每个 id 都必须有对应结果，否则回放与下一轮请求的报文是残缺的。
                    messages.extend(
                        {
                            "role": "tool",
                            "content": note,
                            "tool_call_id": pending.id,
                            "name": pending.name,
                        }
                        for pending in reply.tool_calls[index:]
                    )
                    break
                # 每执行一次工具就从本轮预算账里扣 1（子任务工具的额外消耗由 handler 自己回写）。
                budget.spend(1)
                outcome = execute_tool(
                    ctx,
                    session_id,
                    turn_id,
                    len(executed),
                    call,
                    emit,
                    budget=budget,
                    seen_source_ids=seen_source_ids,
                )
                result_text, tool_refs, canonical, tool_ok = (
                    outcome.text,
                    outcome.refs,
                    outcome.canonical,
                    outcome.ok,
                )
                merge_refs(refs_total, tool_refs)
                # ---- A1/A7：把这一批来源并进台账，看本轮到底有没有新东西
                account_tool_result(
                    ctx,
                    session_id=session_id,
                    state=state,
                    call=call,
                    canonical=canonical,
                    tool_refs=tool_refs,
                    tool_ok=tool_ok,
                    fresh_sources=fresh_sources,
                )
                executed.append(canonical)
                messages.append(
                    {
                        "role": "tool",
                        "content": result_text,
                        "tool_call_id": call.id,
                        "name": call.name,
                    }
                )
                # 更早的工具结果折叠成一行：带着 3 万 tokens 的历史一路重发是最贵的写法
                context_budget.collapse_old_tool_results(messages)
                sessions.append_message(
                    ctx.db,
                    session_id,
                    role="tool",
                    content=result_text,
                    tool_call_id=call.id,
                    name=call.name,
                )
                # ---- 缺陷 3 的**终点**：这一次调用让用户先选一次 → 本轮到此为止。
                # 收口理由沿用 ``completed``：选择卡是这一轮**正常**的产出（用户点一下就能继续），
                # 不是预算/上限/错误/取消，也不该让界面多维护一个 ``stop_reason`` 分支。
                if outcome.needs_choice:
                    stop_reason = "completed"
                    awaiting_choice = True

            # ---- 缺陷 3：这一批被选择请求截断了 —— 不再看"没新证据"的提示（那是给继续检索的
            # 轮次用的），也不再调模型。本轮到此为止，等用户点选。
            if awaiting_choice:
                break

            # ---- A7：连续 N 次检索都拿不到新来源 → 明确叫停，让模型如实总结。
            # 为什么不直接断掉：断了就没有总结，用户只看到半句话。多花一次模型调用，
            # 换"如实说明还缺什么"，比让它继续换词重搜（每轮 ~10 次调用）便宜得多。
            if (
                not stop_hinted
                and int(state.get("empty_rounds") or 0) >= research_state.MAX_EMPTY_ROUNDS
            ):
                stop_hinted = True
                stop_reason = "no_new_evidence"
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            f"已经连续 {research_state.MAX_EMPTY_ROUNDS} 次检索没有新增来源。"
                            "请**停止检索**（本轮不再给你工具），如实总结：现在能确认什么、"
                            "还缺什么证据、建议用户下一步怎么做（换来源/换年份/换个说法）。"
                            "不要为了凑内容而重复已有的说法。"
                        ),
                    }
                )
        else:
            # 只有"没被别的理由收口"时才算撞上本轮工具上限（会话级超限/无新证据都已明确收口）
            if stop_reason != daily_budget.SESSION_STOP_REASON:
                stop_reason = "tool_limit"
    except LlmError as exc:
        stop_reason = "error"
        error_message = f"{exc.kind}: {exc}"
        # 流式中途断开：已收到的部分不丢 —— 直接当作本轮答案（界面已经按增量显示过它），
        # 同时如实把真实原因一起报出来（stop_reason=error + 收尾的 chat.done/错误信息）。
        partial = str(getattr(exc, "partial", "") or "")
        if partial:
            final_text = partial
    except Exception as exc:
        stop_reason = "error"
        error_message = f"{type(exc).__name__}: {exc}"
    finally:
        # 无论怎么结束都要摘掉：留着会让"下一次取消"打到一轮已经结束的轮次上
        ctx.running.pop(session_id, None)

    # ---- 封顶后模型一个字都没说出来时，**如实**给一条可读的说明（不假装成功、不留空白）。
    if stop_reason == daily_budget.STOP_REASON and not final_text.strip():
        final_text = (
            "（今日用量已达上限，本轮不再继续调用模型。已产生的记录都在上面的会话里；"
            "可以调高 config.yaml 的 runtime.daily_token_budget，或等滚动 24 小时窗口过去后再继续。）"
        )
    # 会话级上限同理：一个字都没说出来时给一条可读说明，且**指明出路**（新开会话）。
    if stop_reason == daily_budget.SESSION_STOP_REASON and not final_text.strip():
        final_text = (
            "（本会话的工具调用次数已达上限（runtime.max_tool_calls_per_session），"
            "本轮不再继续调用工具。已产生的记录都在上面的会话里；"
            "要接着查请新开一个会话，或调高 config.yaml 的这个键。）"
        )

    # ---- A1/A2：把模型回填的覆盖状态并进台账；记账标记从**可见正文**里剥掉。
    final_text = apply_coverage(
        ctx,
        session_id=session_id,
        turn_id=turn_id,
        state=state,
        final_text=final_text,
        fresh_sources=fresh_sources,
    )

    # ---- D2：跨会话记忆（每个会话最多抽一次；失败不影响本轮结果，开销如实记账）。
    extract_memory(ctx, session_id=session_id, turn_id=turn_id)

    message_id = sessions.append_message(
        ctx.db,
        session_id,
        role="assistant",
        content=final_text,
        token_in=usage_total.get("prompt_tokens"),
        token_out=usage_total.get("completion_tokens"),
    )
    # 非流式路径：仍照旧推一条最终 chat.delta（行为一字不变）。
    # 流式路径：增量已在上面实时推过，这里不再重发（否则前端会收到重复的收尾）；
    # 但**界面最后看到的正文必须与落库的最终正文一致** —— 覆盖标记被剥掉、正文被沿用等
    # 都会让两者不同，不补这一条界面会停在半句话上。
    if deltas_sent == 0 or streamed_text != final_text:
        emit_event(
            emit,
            "chat.delta",
            {"turn_id": turn_id, "seq": deltas_sent, "kind": "text", "text": final_text},
        )
    emit_event(
        emit,
        "chat.done",
        {
            "turn_id": turn_id,
            "stop_reason": stop_reason,
            "usage": usage_total,
            "refs": refs_total,
            # 收尾事件带上**权威正文与落库消息 id**（docs/20 §3.2）：`chat.send` 不再阻塞返回时，
            # 界面必须能从收尾事件一次落定（`chat.delta` 的累计正文是同一个值的流式版本）。
            "text": final_text,
            "message_id": message_id,
        },
    )
    return TurnResult(
        turn_id=turn_id,
        session_id=session_id,
        message_id=message_id,
        text=final_text,
        stop_reason=stop_reason,
        tool_calls=executed,
        usage=usage_total,
        refs=refs_total,
        error=error_message,
    )


def mark_system_prompt(
    db: Database, session_id: str, *, toolsets: Sequence[str] | None, config: Config
) -> str:
    """把当前系统提示词的哈希写进会话（改了提示词，历史就不可比）。"""
    prompt = build_system_prompt(
        toolsets=_prompt_toolsets(toolsets, config),
        allow_clarify=config.runtime.allow_clarify,
        auto_artifacts=config.generation.auto_artifacts,
        style=getattr(config.generation, "style", "default"),
        disclosure=bool(config.features.enable_tool_disclosure),
    )
    value = system_prompt_hash(prompt)
    sessions.set_system_prompt_hash(db, session_id, value)
    return value
