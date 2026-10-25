"""单轮对话循环的**步骤实现**（``loop.run_turn`` 的被调用方）。

为什么单独成模块：``loop.py`` 只留编排（顺序 / 条件 / 返回值），每个\"做一件具体事\"的步骤
都在这里 —— 改一处步骤（比如记账口径）不必在近千行的编排里翻找，也避免编排文件再长出第二个职责。

上下文原语（``TurnContext`` / ``TurnResult`` / ``Emitter`` 与事件辅助）在
``runtime/turn_context.py``：本模块**重导出**它们，``from ...turn_steps import TurnContext``
这类旧写法继续可用（``loop`` 与测试都这么用）。

分层铁律：本模块**不导入** ``scholarscout.rpc``。事件只通过 ``emit`` 回调向上冒泡，
依赖（``ctx`` / ``db`` / ``session_id`` / ``turn_id`` / ``emit``）一律**显式传入**，
本模块不持有任何全局可变状态。
"""

from __future__ import annotations

import json
import logging
from typing import Any, NamedTuple

from scholarscout.providers.llm import ChatReply, ToolCall
from scholarscout.runtime import (
    compaction,
    context_budget,
    disclosure,
    memory,
    registry,
    research_state,
    search_intent,
)
from scholarscout.runtime.toolctx import TurnBudget
from scholarscout.runtime.turn_context import (
    Emitter,
    TurnContext,
    TurnResult,
    bind_emitter,
    emit_event,
    merge_refs,
)
from scholarscout.state import sessions, traces

logger = logging.getLogger(__name__)

#: 上下文原语由 ``turn_context`` 定义、此处仅为向后兼容而重导出（见模块 docstring）。
__all__ = [
    "Emitter",
    "ToolOutcome",
    "TurnContext",
    "TurnResult",
    "account_tool_result",
    "accumulate_usage",
    "append_tool_call_request",
    "apply_coverage",
    "awaits_user_choice",
    "bind_emitter",
    "call_model",
    "emit_event",
    "execute_tool",
    "extract_memory",
    "maybe_compact",
    "merge_refs",
    "prepare_research_state",
    "query_from_arguments",
    "render_context_note",
]


def call_model(
    ctx: TurnContext,
    messages: list[dict[str, Any]],
    *,
    stream_enabled: bool,
    tools: list[dict[str, Any]] | None,
    emit: Emitter | None,
    session_id: str,
    turn_id: str,
    deltas_before: int,
    usage_total: dict[str, int],
) -> tuple[ChatReply, int, str]:
    """调一次模型（开流式就边收边推 ``chat.delta``），并把这次调用的钱立即记账。

    返回 ``(reply, 累计已推的增量条数, 本轮收到的流式正文)``。
    """
    streamed_text = ""
    deltas_sent = deltas_before
    if stream_enabled:
        # 这一轮可以是正文（最终答案/前言），也可以是工具调用 —— 两条都走流式：
        # 只流正文增量，工具调用照原样从 `reply.tool_calls` 拿。

        def _on_delta(piece: str) -> None:
            nonlocal streamed_text, deltas_sent
            streamed_text += piece
            deltas_sent += 1
            # 推**累计**正文：前端 chat.delta 的处理是"替换正文"，累计值才能直接接管。
            emit_event(
                emit,
                "chat.delta",
                {
                    "turn_id": turn_id,
                    "seq": deltas_sent,
                    "kind": "text",
                    "text": streamed_text,
                },
            )

        reply = ctx.llm.chat_stream(messages, tools=tools, on_delta=_on_delta)
    else:
        reply = ctx.llm.chat(messages, tools=tools)
    accumulate_usage(usage_total, reply)
    # **每次模型调用立即记账**：原来只在整轮结束记一次，结果是"被停止或中断的轮次
    # 把已经花掉的 token 全丢了"（真机实测：杀掉实例后该会话用量一条不剩）。
    # 代价敏感的用户看到的会话累计必须包含花掉的钱，哪怕这一轮被中途取消。
    traces.record_usage(
        ctx.db,
        model=ctx.config.chat.model,
        purpose="chat",
        session_id=session_id,
        turn_id=turn_id,
        prompt_tokens=reply.usage.get("prompt_tokens"),
        completion_tokens=reply.usage.get("completion_tokens"),
        cached_tokens=reply.usage.get("cached_tokens"),
    )
    return reply, deltas_sent, streamed_text


def append_tool_call_request(
    ctx: TurnContext, *, session_id: str, reply: ChatReply, messages: list[dict[str, Any]]
) -> None:
    """把模型这一轮的 tool_calls 落库，并写进本轮消息（下一轮模型与回放都要看到它）。"""
    assistant_row = sessions.append_message(
        ctx.db,
        session_id,
        role="assistant",
        content=reply.content or "",
        tool_calls_json=json.dumps(
            [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in reply.tool_calls
            ],
            ensure_ascii=False,
        ),
    )
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
    del assistant_row  # 已落库，无需再用


def accumulate_usage(total: dict[str, int], reply: ChatReply) -> None:
    for key, value in reply.usage.items():
        total[key] = total.get(key, 0) + int(value)


def query_from_arguments(arguments: str) -> str:
    """从工具调用的实参里取 query（取不到回空串 —— 记账只认确定的东西）。"""
    try:
        payload = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("query") or "").strip()


class ToolOutcome(NamedTuple):
    """一次工具执行的结果（喂回模型的文本 / refs / 规范名 / 是否成功 / 是否在等用户选）。

    ``needs_choice`` 单独带出来是给主循环的**停手机制**用的：工具可以让用户先选一次
    （``research.search`` 在"没说清要哪类资料"时就是这样），那种结果是**这一轮的终点**，
    而不是"失败的调用"—— 后面排着的调用一次都不许再跑。
    """

    text: str
    refs: dict[str, Any]
    canonical: str
    ok: bool
    needs_choice: bool


#: 为什么按**信封顶层键**判、不按工具名特判：``needs_choice`` / ``options`` 是 ``tools/envelope.py``
#: 里的既有契约（前端 ``app.js::renderChoiceCard`` 也直接读它们）。按工具名写 ``if name ==
#: "research.search"`` 会让"将来另一个工具也想问用户"变成又要改一次循环。
def awaits_user_choice(result: Any) -> bool:
    """这次工具结果是不是「让用户先选一下」（**有选项才算**）。

    为什么要求 ``options`` 非空：界面渲染选择卡的唯一来源就是这份选项（无选项 → 不出卡）。
    选项为空时停手，用户既没东西可点、这一轮也不再干活 —— 比继续跑更糟。所以只有
    "卡片真能渲染出来"的选择请求才配叫停这一轮。
    """
    if not isinstance(result, dict) or not result.get("needs_choice"):
        return False
    options = result.get("options")
    return isinstance(options, list) and bool(options)


def execute_tool(
    ctx: TurnContext,
    session_id: str,
    turn_id: str,
    step: int,
    call: ToolCall,
    emit: Emitter | None,
    *,
    budget: TurnBudget | None = None,
    seen_source_ids: set[str] | None = None,
) -> ToolOutcome:
    """执行一次工具调用，返回 ``ToolOutcome``（喂回模型的文本 / refs / 规范名 / 成功 / 等用户选）。

    `ok` 单独带出来是给 A3 记账用的：工具**可以不抛异常而回"我失败了"**（例如所有检索来源都挂了），
    那种情况不该把查询记进台账（否则一次网络抖动就把这个方向永久锁死）。

    `needs_choice` 是**停手信号**：工具让用户先选一次（``research.search`` 没说清要哪类资料时），
    主循环读到它就不再执行同一 reply 里剩下的调用。

    失败也要如实回话并记 trace；规范名用于事件、轨迹与对外结果（线上名只在 API 报文里用）。
    """
    spec = registry.get_spec(call.name)
    # 模型回的是**线上名**（research__search），落到 trace/事件里一律用规范名（research.search），
    # 这样 docs/01 的方法名、指标的取数口径与论文里的名字始终是一份
    canonical = spec.name if spec is not None else call.name
    emit_event(
        emit,
        "chat.tool_call",
        {
            "turn_id": turn_id,
            "seq": step,
            "call_id": call.id,
            "tool": canonical,
            "args_digest": traces.digest(call.arguments),
            "args_preview": call.arguments[:200],
        },
    )
    if spec is None:
        # 回给模型的文案只说人话：工具名与 `tool_not_registered` 只进 trace。
        # 模型会**照抄**它读到的标识给用户（缺陷 4），所以文案侧不能带内部名。
        message = (
            "这个工具当前未注册（当前阶段可用工具集为空）：不要重试，也不要假装调用成功；"
            "换用已提供的工具，或如实告诉用户这个能力现在不可用。"
        )
        traces.record_trace(
            ctx.db,
            session_id=session_id,
            turn_id=turn_id,
            step=step,
            tool_name=canonical,
            ok=False,
            args=call.arguments,
            error="tool_not_registered",
        )
        emit_event(
            emit,
            "chat.tool_result",
            {"turn_id": turn_id, "seq": step, "call_id": call.id, "ok": False, "refs": {}},
        )
        return ToolOutcome(message, {}, canonical, False, False)

    # ---- 渐进披露的闸（docs/01 §3.17）：**没 describe 过的工具不许直接调**。
    # 为什么做在这一层而不是"只在 payload 里不给它"：模型完全可以凭旧记忆/提示词里的名字
    # 硬调一个没声明的工具（E4 的错选就是这么来的）。只在 payload 里省掉，等于假设模型不会绕；
    # 真绕了就会**静默执行**，比报错更糟 —— 它会以为自己"确认过"这个工具的参数。
    # **只在 `features.enable_tool_disclosure` 开着时生效**：关着时（产品默认）11 个工具
    # 全部常驻、全部可直接调，与引入本功能前逐字一致 —— 否则默认臂会被这条闸静默弄坏。
    if ctx.config.features.enable_tool_disclosure and not disclosure.is_available(
        canonical, session_id=session_id
    ):
        message = disclosure.not_loaded_message(canonical)
        traces.record_trace(
            ctx.db,
            session_id=session_id,
            turn_id=turn_id,
            step=step,
            tool_name=canonical,
            ok=False,
            args=call.arguments,
            error="tool_not_loaded",
        )
        emit_event(
            emit,
            "chat.tool_result",
            {"turn_id": turn_id, "seq": step, "call_id": call.id, "ok": False, "refs": {}},
        )
        return ToolOutcome(message, {}, canonical, False, False)

    import time

    tool_ctx = ctx.tool_context(session_id=session_id, turn_id=turn_id, budget=budget, emit=emit)
    started = time.perf_counter()
    try:
        arguments = call.parsed_arguments()
        result = registry.invoke(spec, tool_ctx, arguments)
        latency_ms = int((time.perf_counter() - started) * 1000)
        traces.record_trace(
            ctx.db,
            session_id=session_id,
            turn_id=turn_id,
            step=step,
            tool_name=canonical,
            ok=True,
            args=arguments,
            result=result,
            latency_ms=latency_ms,
        )
        refs = result.get("refs", {}) if isinstance(result, dict) else {}
        emit_event(
            emit,
            "chat.tool_result",
            {
                "turn_id": turn_id,
                "seq": step,
                "call_id": call.id,
                "ok": True,
                "digest": traces.digest(result),
                "latency_ms": latency_ms,
                "refs": refs,
            },
        )
        return ToolOutcome(
            context_budget.project_tool_result(result, seen_source_ids=seen_source_ids),
            (refs if isinstance(refs, dict) else {}),
            canonical,
            # 工具可以不抛异常而回"我失败了"（例如所有检索来源都挂了）——
            # 判重记账必须按这个 ok 走，否则一次网络抖动就把某个方向永久锁死
            bool(result.get("ok", True)) if isinstance(result, dict) else True,
            awaits_user_choice(result),
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        traces.record_trace(
            ctx.db,
            session_id=session_id,
            turn_id=turn_id,
            step=step,
            tool_name=canonical,
            ok=False,
            args=call.arguments,
            error=f"{type(exc).__name__}: {exc}",
            latency_ms=latency_ms,
        )
        emit_event(
            emit,
            "chat.tool_result",
            {"turn_id": turn_id, "seq": step, "call_id": call.id, "ok": False, "refs": {}},
        )
        # 同上：工具名与异常类名只进 trace（上面那条），喂回模型的是人话 + 原始原因。
        return ToolOutcome(
            f"这一步没有成功：{exc}（如实说明哪一步没做成、大概为什么；"
            "不要把工具名、错误码或异常类名写进回答）",
            {},
            canonical,
            False,
            False,
        )


def maybe_compact(ctx: TurnContext, *, session_id: str, turn_id: str) -> None:
    """D3：历史超过阈值就先压一次（**同一段只压一次**，提要与"已覆盖到第几条"存进台账）。

    失败了退回规则式裁剪（_fit_history），本轮照常 —— 压缩是省钱手段，不能因为它弄挂一轮。
    """
    if not (bool(getattr(ctx.config.generation, "compact_by_model", True)) and ctx.llm is not None):
        return
    try:
        threshold = int(
            getattr(ctx.config.runtime, "compact_after_chars", compaction.DEFAULT_AFTER_CHARS)
        )
        if ctx.config.summary_cfg is not None and compaction.should_compact(
            ctx.db, session_id, after_chars=threshold
        ):
            outcome = compaction.compact(
                ctx.llm,
                ctx.db,
                session_id,
                prompts_dir=ctx.config.summary_cfg.prompts_dir,
            )
            if outcome.get("summary"):
                compaction.save_summary(
                    ctx.db,
                    session_id,
                    summary=str(outcome["summary"]),
                    covered=int(outcome["covered"]),
                )
            usage = outcome.get("usage") or {}
            if usage:
                traces.record_usage(
                    ctx.db,
                    model=ctx.config.summary.model,
                    purpose="compact",
                    session_id=session_id,
                    turn_id=turn_id,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    cached_tokens=usage.get("cached_tokens"),
                )
    except Exception as exc:
        # 仍然是"只吞不抛"（压缩失败不该让这一轮开不起来），但必须留痕：
        # 否则压缩/记账一直失败，用户只会看到上下文越来越长、账单对不上，
        # 却没有任何一处日志说明为什么。控制流与原来完全一致。
        logger.warning(
            "压缩与压缩记账失败（本轮照常继续，历史不压缩）：session=%s turn=%s err=%s",
            session_id,
            turn_id,
            exc,
        )


def prepare_research_state(
    ctx: TurnContext, *, session_id: str, text: str, turn_id: str
) -> dict[str, Any]:
    """读跨轮研究台账 → 开局拆子问题（每会话一次）→ 解析本轮检索偏好位（方案 3）。

    「连续无新增」是**轮内**计数：上一轮留下的计数不能带进这一轮，
    否则用户换个话题继续问时，第一轮就会被立刻叫停。
    """
    # ---- A1/A2：把跨轮研究台账与"缺口清单"注入到**本轮消息的最后**。
    # 为什么放最后而不是最前：服务端缓存按"从头的最长公共前缀"命中（A6），
    # 台账每轮都会变（已搜查询、缺口都会增长），放前面等于每轮都让缓存失效。
    state = research_state.load_state(ctx.db, session_id) or research_state.empty_state(
        goal=text.strip()[:200]
    )
    state["empty_rounds"] = 0
    if not state.get("questions") and not state.get("used_queries"):
        # 开局拆一次子问题（每会话一次的小调用）——关掉开关就退化成"只有目标 + 只有缺口"
        if (
            bool(getattr(ctx.config.runtime, "plan_by_model", False))
            and ctx.llm is not None
            and ctx.config.summary_cfg is not None
        ):
            questions, reason = research_state.decompose_with_reason(
                ctx.llm, text, prompts_dir=ctx.config.summary_cfg.prompts_dir
            )
            state["questions"] = questions
            if reason:
                # 留痕：拆不出来时缺口粒度会退化，台账里要看得见（真机栽过一次）
                state["plan_error"] = reason
        research_state.save_state(ctx.db, session_id, state)
    # ---- 方案 3：本轮检索偏好位（`runtime/search_intent.py`）。**不进系统提示** —— 它写进
    # 研究台账的 payload_json，系统提示仍是静态文本，`system_prompt_hash` 与用户点选无关。
    # 规则优先（"只看论文/别搜了"不调模型），模糊查询才花一次极小调用；失败开放，绝不阻断本轮。
    try:
        intent = search_intent.resolve_turn(
            ctx.llm,
            text,
            state=state,
            turn_id=turn_id,
            prompts_dir=ctx.config.summary_cfg.prompts_dir if ctx.config.summary_cfg else None,
            enabled=bool(getattr(ctx.config.runtime, "classify_search_intent", True)),
        )
        if intent is not None and intent.usage:
            # 这次小调用的钱也要记账（purpose="intent"）—— 否则"关掉它能省多少"没人答得出来
            traces.record_usage(
                ctx.db,
                model=ctx.config.chat.model,
                purpose="intent",
                session_id=session_id,
                turn_id=turn_id,
                prompt_tokens=intent.usage.get("prompt_tokens"),
                completion_tokens=intent.usage.get("completion_tokens"),
            )
        research_state.save_state(ctx.db, session_id, state)
    except Exception:
        # 分类是省钱的辅助，不是主流程的前置条件：它挂了，本轮照常检索与回答
        pass
    return state


def render_context_note(ctx: TurnContext, *, session_id: str, state: dict[str, Any]) -> str:
    """本轮末尾那条 system 的内容：台账（目标/缺口/已搜查询）+ 学习者画像 + 跨会话记忆。"""
    gaps = research_state.compute_gaps(ctx.db, session_id, state)
    # 学习者画像（跨会话）：只放"能改变行为"的信息（水平、薄弱块、已做过的主题）。
    # 纯本地汇总、零模型调用；没有卡片也没有产物时这一段是空串，不会白占 token。
    from scholarscout.runtime import learner_profile

    profile_note = learner_profile.render_profile_note(learner_profile.build_profile(ctx.db))
    note = research_state.render_note(state, gaps)
    if profile_note:
        note = note + "\n\n" + profile_note
    # 跨会话记忆（D2）：稳定事实 + "冲突以用户当前说法为准"的规矩
    from scholarscout.runtime import memory

    facts_note = memory.render_facts_note(ctx.db)
    if facts_note:
        note = note + "\n\n" + facts_note
    return note


def account_tool_result(
    ctx: TurnContext,
    *,
    session_id: str,
    state: dict[str, Any],
    call: ToolCall,
    canonical: str,
    tool_refs: Any,
    tool_ok: bool,
    fresh_sources: list[str],
) -> None:
    """A1/A7：把一次工具结果并进跨轮台账 —— 新来源、空轮计数、已搜查询（A3 判重数据源）。"""
    fresh = research_state.new_sources(state, list(tool_refs.get("source_ids") or []))
    fresh_sources.extend(fresh)
    if canonical == "research.search":
        if fresh:
            state["empty_rounds"] = 0
        elif tool_ok:
            # 只有**真的搜过且没新来源**才算"空轮"。没搜（用户选择不检索 / 等用户选类型 /
            # 来源报错）不算 —— 否则会把"没搜"误判成"搜不到"，提前触发停搜提示。
            state["empty_rounds"] = int(state.get("empty_rounds") or 0) + 1
        # 记账（A3 判重的唯一数据源）：**搜成功才记**，失败不记 ——
        # 否则一次网络抖动就把这个方向永久锁死。写者只有循环这一个。
        query = query_from_arguments(call.arguments)
        if tool_ok and query:
            research_state.record_query(state, query)
        research_state.save_state(ctx.db, session_id, state)


def apply_coverage(
    ctx: TurnContext,
    *,
    session_id: str,
    turn_id: str,
    state: dict[str, Any],
    final_text: str,
    fresh_sources: list[str],
) -> str:
    """A1/A2：把模型回填的覆盖状态并进台账，并从可见正文里剥掉记账标记。

    标记解析不出来就什么都不改（保持 open）—— 猜覆盖等于自欺。台账写回失败不拖垮这一轮，
    但必须留痕：静默失败 = 下一轮带着同一份旧台账跑，用户只会觉得"它怎么又忘了"。
    """
    try:
        if research_state.apply_coverage(state, final_text, fresh_sources):
            final_text = research_state.strip_marker(final_text)
        research_state.save_state(ctx.db, session_id, state)
    except Exception as exc:
        logger.warning(
            "跨轮台账写回失败（保持 open，本轮照常收尾）：session=%s turn=%s err=%s",
            session_id,
            turn_id,
            exc,
        )
    return final_text


def extract_memory(ctx: TurnContext, *, session_id: str, turn_id: str) -> None:
    """D2：跨会话记忆。每个会话最多抽一次，且要在"有值得记的迹象"时才抽。

    失败不影响本轮结果（回答已经生成好了）；开销按 purpose="memory" 如实记账，
    免得它变成看不见的暗开销（用户对成本敏感，这条尤其重要）。
    """
    if not (bool(getattr(ctx.config.generation, "memory_by_model", True)) and ctx.llm is not None):
        return
    try:
        # P1-1：nudge_turns=0（默认）时行为与引入前逐字一致（每会话最多一次）
        nudge_turns = int(getattr(ctx.config.generation, "memory_nudge_turns", 0) or 0)
        if ctx.config.summary_cfg is not None and memory.should_extract(
            ctx.db, session_id, nudge_turns=nudge_turns
        ):
            outcome = memory.extract_facts(
                ctx.llm,
                ctx.db,
                session_id,
                prompts_dir=ctx.config.summary_cfg.prompts_dir,
            )
            # 记基准轮（抽到空表也记）—— 否则下一轮会再次触发，白花钱
            memory.mark_extracted(ctx.db, session_id)
            usage = outcome.get("usage") or {}
            if usage:
                traces.record_usage(
                    ctx.db,
                    model=ctx.config.summary.model,
                    purpose="memory",
                    session_id=session_id,
                    turn_id=turn_id,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    cached_tokens=usage.get("cached_tokens"),
                )
    except Exception as exc:
        # 仍然是"只吞不抛"（回答已经生成好了），但记忆抽取失败必须留痕：
        # 它同时会吃掉 purpose="memory" 的那条用量记账，静默就等于暗开销。
        logger.warning(
            "跨会话记忆抽取失败（不影响本轮结果）：session=%s turn=%s err=%s",
            session_id,
            turn_id,
            exc,
        )
