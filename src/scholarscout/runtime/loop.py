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

import json
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from scholarscout.config import Config
from scholarscout.fetch.http import HttpClient
from scholarscout.providers.llm import ChatReply, LlmClient, LlmError, ToolCall
from scholarscout.runtime import compaction, context_budget, registry, research_state
from scholarscout.runtime.prompts import build_system_prompt, system_prompt_hash
from scholarscout.runtime.toolctx import ToolContext, TurnBudget
from scholarscout.state import sessions, traces
from scholarscout.state.db import Database

#: 事件回调：``emit(kind, payload)``（RPC 层把 chat.* 事件转成通知）
Emitter = Callable[[str, dict[str, Any]], None]

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


@dataclass
class TurnContext:
    db: Database
    config: Config
    llm: LlmClient
    http: HttpClient | None = None
    cancelled: set[str] = field(default_factory=set)
    #: `session_id -> turn_id`：这个会话此刻正在跑哪一轮。
    #: 为什么需要它：界面在整轮结束前**拿不到 turn_id**（它随 `chat.done` 才回来），
    #: 所以"停止"必须能按会话取消，而不是按 turn_id（否则这个 RPC 从设计上就没法被界面调用）。
    running: dict[str, str] = field(default_factory=dict)
    #: 本轮的取消信号（**每轮一个独立 Event**，docs/20 §6.2）。
    #: 多会话并行时由 `SessionRuntime` 按轮注入；`None`（CLI / 老调用点）= 只看 `cancelled` 集合，
    #: 于是改造前的语义一字不变。
    cancel_event: threading.Event | None = None

    def tools(self, toolsets: Sequence[str] | None) -> list[registry.ToolSpec]:
        return registry.get_tools(toolsets, features=self.config.features)

    def is_cancelled(self, turn_id: str) -> bool:
        """这一轮被取消了吗（步间检查）。

        两条通道都认：**每轮的 Event**（并行下的机制保证，只可能点到这一轮）与
        `cancelled` 集合（按 turn_id 的兼容通道；turn_id 全局唯一，所以不会误伤别的会话）。
        """
        if self.cancel_event is not None and self.cancel_event.is_set():
            return True
        return turn_id in self.cancelled

    def tool_context(
        self,
        *,
        session_id: str,
        turn_id: str,
        budget: TurnBudget | None = None,
        emit: Emitter | None = None,
    ) -> ToolContext:
        """给工具用的上下文（注入库、配置、出网、模型与本次会话/轮次标识）。

        ``budget`` 是本轮的工具预算账（:class:`scholarscout.runtime.toolctx.TurnBudget`）——
        子任务工具（``research.delegate``）据此**从主预算里扣**；``emit`` 让它能发 ``chat.subtask_*``。
        """
        return ToolContext(
            db=self.db,
            config=self.config,
            http=self.http,
            llm=self.llm,
            session_id=session_id,
            turn_id=turn_id,
            budget=budget,
            emit=emit,
        )


@dataclass
class TurnResult:
    turn_id: str
    session_id: str
    message_id: str
    text: str
    stop_reason: str
    tool_calls: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    refs: dict[str, list[str]] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "text": self.text,
            "stop_reason": self.stop_reason,
            "tool_calls": self.tool_calls,
            "usage": self.usage,
            "refs": self.refs,
            "error": self.error,
        }


def _merge_refs(target: dict[str, list[str]], incoming: Any) -> None:
    """把各次工具调用返回的 refs 归并去重（前端靠它显示"这一次生成了什么"，docs/01 §4）。"""
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


def bind_emitter(emit: Emitter | None, session_id: str | None) -> Emitter | None:
    """把 ``session_id`` 绑进事件信封 —— 让"通知必带 session_id"由**机制**保证（docs/20 §3.1）。

    为什么要有：事件不带 ``session_id`` 时，界面无从判断一条 ``chat.delta`` 属于哪个会话
    （真机症状：切到 B 后 A 的回复写进 B 的气泡）。把绑定做在 emitter 上，而不是要求 8 处
    ``_emit`` 调用点各自记得写，才不会有人漏。多绑一次无害（键值相同）。

    ``emit`` 为 ``None``（CLI / 无 GUI 的测试）时**原样返回 None**：包一层空回调只会让
    调用点误以为有事件通道。``session_id`` 为空时同样不绑 —— 绑一个空串只会伪造"合规"。
    """
    if emit is None or not session_id:
        return emit

    def bound(kind: str, payload: dict[str, Any]) -> None:
        # 调用点自己的键优先（如子任务里更精确的 turn_id），但 session_id 以绑定值为准
        emit(kind, {**payload, "session_id": session_id})

    return bound


def _emit(emit: Emitter | None, kind: str, payload: dict[str, Any]) -> None:
    if emit is not None:
        emit(kind, payload)


def build_messages(
    db: Database,
    session_id: str,
    *,
    config: Config,
    toolsets: Sequence[str] | None,
) -> list[dict[str, Any]]:
    """系统提示 + 会话历史（只取有内容的用户/助手消息）。"""
    prompt = build_system_prompt(
        toolsets=list(toolsets or ["research"]),
        allow_clarify=config.runtime.allow_clarify,
        auto_artifacts=config.generation.auto_artifacts,
        style=getattr(config.generation, "style", "default"),
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
    # ---- D3：历史超过阈值就先压一次（**同一段只压一次**，提要与"已覆盖到第几条"存进台账）。
    # 失败了退回规则式裁剪（_fit_history），本轮照常 —— 压缩是省钱手段，不能因为它弄挂一轮。
    if bool(getattr(ctx.config.generation, "compact_by_model", True)) and ctx.llm is not None:
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
        except Exception:
            pass
    messages = build_messages(ctx.db, session_id, config=ctx.config, toolsets=toolsets)
    tool_decls = registry.openai_tools(specs) or None

    # ---- A1/A2：把跨轮研究台账与"缺口清单"注入到**本轮消息的最后**。
    # 为什么放最后而不是最前：服务端缓存按"从头的最长公共前缀"命中（A6），
    # 台账每轮都会变（已搜查询、缺口都会增长），放前面等于每轮都让缓存失效。
    state = research_state.load_state(ctx.db, session_id) or research_state.empty_state(
        goal=text.strip()[:200]
    )
    # 「连续无新增」是**轮内**计数：上一轮留下的计数不能带进这一轮，
    # 否则用户换个话题继续问时，第一轮就会被立刻叫停。
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
    try:
        # 先把"这个会话正在跑哪一轮"记下来：界面点「停止」时按会话找到它（整轮结束前拿不到 turn_id）
        ctx.running[session_id] = turn_id
        for _round in range(tool_budget + 1):
            if ctx.is_cancelled(turn_id):
                stop_reason = "cancelled"
                break
            tools_for_round = None if (stop_hinted or budget.remaining <= 0) else tool_decls
            if stream_enabled:
                # 这一轮可以是正文（最终答案/前言），也可以是工具调用 —— 两条都走流式：
                # 只流正文增量，工具调用照原样从 `reply.tool_calls` 拿。
                streamed_text = ""

                def _on_delta(piece: str) -> None:
                    nonlocal streamed_text, deltas_sent
                    streamed_text += piece
                    deltas_sent += 1
                    # 推**累计**正文：前端 chat.delta 的处理是"替换正文"，累计值才能直接接管。
                    _emit(
                        emit,
                        "chat.delta",
                        {
                            "turn_id": turn_id,
                            "seq": deltas_sent,
                            "kind": "text",
                            "text": streamed_text,
                        },
                    )

                reply = ctx.llm.chat_stream(messages, tools=tools_for_round, on_delta=_on_delta)
            else:
                reply = ctx.llm.chat(messages, tools=tools_for_round)
            _accumulate_usage(usage_total, reply)
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
            final_text = reply.content or final_text

            if not reply.tool_calls:
                break

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

            if budget.remaining < len(reply.tool_calls):
                stop_reason = "tool_limit"
                _emit(
                    emit,
                    "chat.error",
                    {"turn_id": turn_id, "code": -32603, "message": "工具调用超限"},
                )
                break

            for call in reply.tool_calls:
                # 每执行一次工具就从本轮预算账里扣 1（子任务工具的额外消耗由 handler 自己回写）。
                budget.spend(1)
                result_text, tool_refs, canonical, tool_ok = _execute_tool(
                    ctx,
                    session_id,
                    turn_id,
                    len(executed),
                    call,
                    emit,
                    budget=budget,
                    seen_source_ids=seen_source_ids,
                )
                _merge_refs(refs_total, tool_refs)
                # ---- A1/A7：把这一批来源并进台账，看本轮到底有没有新东西
                fresh = research_state.new_sources(state, list(tool_refs.get("source_ids") or []))
                fresh_sources.extend(fresh)
                if canonical == "research.search":
                    if fresh:
                        state["empty_rounds"] = 0
                    else:
                        state["empty_rounds"] = int(state.get("empty_rounds") or 0) + 1
                    # 记账（A3 判重的唯一数据源）：**搜成功才记**，失败不记 ——
                    # 否则一次网络抖动就把这个方向永久锁死。写者只有循环这一个。
                    query = _query_from_arguments(call.arguments)
                    if tool_ok and query:
                        research_state.record_query(state, query)
                    research_state.save_state(ctx.db, session_id, state)
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

    # ---- A1/A2：把模型回填的覆盖状态并进台账；记账标记从**可见正文**里剥掉。
    # 标记解析不出来就什么都不改（保持 open）—— 猜覆盖等于自欺。
    try:
        if research_state.apply_coverage(state, final_text, fresh_sources):
            final_text = research_state.strip_marker(final_text)
        research_state.save_state(ctx.db, session_id, state)
    except Exception:
        pass

    # ---- D2：跨会话记忆。每个会话最多抽一次，且要在"有值得记的迹象"时才抽。
    # 失败不影响本轮结果（回答已经生成好了）；开销按 purpose="memory" 如实记账，
    # 免得它变成看不见的暗开销（用户对成本敏感，这条尤其重要）。
    if bool(getattr(ctx.config.generation, "memory_by_model", True)) and ctx.llm is not None:
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
        except Exception:
            pass

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
        _emit(
            emit,
            "chat.delta",
            {"turn_id": turn_id, "seq": deltas_sent, "kind": "text", "text": final_text},
        )
    _emit(
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


def _accumulate_usage(total: dict[str, int], reply: ChatReply) -> None:
    for key, value in reply.usage.items():
        total[key] = total.get(key, 0) + int(value)


def _query_from_arguments(arguments: str) -> str:
    """从工具调用的实参里取 query（取不到回空串 —— 记账只认确定的东西）。"""
    try:
        payload = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("query") or "").strip()


def _execute_tool(
    ctx: TurnContext,
    session_id: str,
    turn_id: str,
    step: int,
    call: ToolCall,
    emit: Emitter | None,
    *,
    budget: TurnBudget | None = None,
    seen_source_ids: set[str] | None = None,
) -> tuple[str, dict[str, Any], str, bool]:
    """执行一次工具调用，返回（喂回模型的文本, 该工具报出的 refs, 规范工具名, 是否成功）。

    `ok` 单独带出来是给 A3 记账用的：工具**可以不抛异常而回"我失败了"**（例如所有检索来源都挂了），
    那种情况不该把查询记进台账（否则一次网络抖动就把这个方向永久锁死）。

    失败也要如实回话并记 trace；规范名用于事件、轨迹与对外结果（线上名只在 API 报文里用）。
    """
    spec = registry.get_spec(call.name)
    # 模型回的是**线上名**（research__search），落到 trace/事件里一律用规范名（research.search），
    # 这样 docs/01 的方法名、指标的取数口径与论文里的名字始终是一份
    canonical = spec.name if spec is not None else call.name
    _emit(
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
        message = f"工具 {call.name} 未注册（当前阶段可用工具集为空）"
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
        _emit(
            emit,
            "chat.tool_result",
            {"turn_id": turn_id, "seq": step, "call_id": call.id, "ok": False, "refs": {}},
        )
        return message, {}, canonical, False

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
        _emit(
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
        return (
            context_budget.project_tool_result(result, seen_source_ids=seen_source_ids),
            (refs if isinstance(refs, dict) else {}),
            canonical,
            # 工具可以不抛异常而回"我失败了"（例如所有检索来源都挂了）——
            # 判重记账必须按这个 ok 走，否则一次网络抖动就把某个方向永久锁死
            bool(result.get("ok", True)) if isinstance(result, dict) else True,
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
        _emit(
            emit,
            "chat.tool_result",
            {"turn_id": turn_id, "seq": step, "call_id": call.id, "ok": False, "refs": {}},
        )
        return f"工具 {call.name} 执行失败：{exc}", {}, canonical, False


def mark_system_prompt(
    db: Database, session_id: str, *, toolsets: Sequence[str] | None, config: Config
) -> str:
    """把当前系统提示词的哈希写进会话（改了提示词，历史就不可比）。"""
    prompt = build_system_prompt(
        toolsets=list(toolsets or ["research"]),
        allow_clarify=config.runtime.allow_clarify,
        auto_artifacts=config.generation.auto_artifacts,
        style=getattr(config.generation, "style", "default"),
    )
    value = system_prompt_hash(prompt)
    db.execute("UPDATE sessions SET system_prompt_hash = ? WHERE id = ?", (value, session_id))
    return value
