"""学习相关：`route.*`（学习路线）/ `note.*`（笔记与抽背卡）/ `memory.*`（跨会话记忆）。

方法实现分散在这里，注册仍然走 `registry.method` 装饰器 —— 注册表只有一份。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from scholarscout import paths
from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.registry import AppContext, _int_param, _params, _require, method
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state import learning, traces
from scholarscout.tools.envelope import ToolResult


def _card_index(params: dict[str, Any]) -> int:
    """取 `card_index`（**0 起** = 卡的 `cards[]` 下标）：`note.review` / `note.reveal` / `note.history`。

    缺参数**不默认成第 0 张**：默认会把记录写到别的卡上，比报错更难查。
    """
    return _int_param(params, "card_index", -1)


def _card_number(params: dict[str, Any]) -> int:
    """取 `card_index`（**1 起**，与界面上的「第几张」一致）并换成 0 起下标。

    `note.grade` / `note.extend` / `note.help` 这一族收 1 起 —— 这是既有的流式契约
    （`tests/rpc/test_practice_stream.py` 与 `test_events_carry_session_id.py` 都按「单卡笔记 + 传 1 +
    事件里 `card_index: 1`」钉着）。所以这里**不接受 0**：0 起与 1 起差一张，静默按某一种解读
    就是把讲解/判分/延伸题算到别的卡上，宁可 0 直接 -32602 让调用方立刻暴露。
    """
    value = _int_param(params, "card_index", 1)
    if value < 1:
        raise RpcError(Code.INVALID_PARAMS, "card_index 1 起（第一张卡传 1）")
    return value - 1


@method("route.list")
def route_list(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    limit = int(params.get("limit") or 50)
    return {"items": learning.list_routes(ctx.db, limit=limit)}


@method("route.get")
def route_get(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    route_id = _require(params, "route_id")
    payload = learning.get_route(ctx.db, route_id)
    if payload is None:
        raise RpcError(Code.NOT_FOUND, f"学习路线不存在：{route_id}")
    return payload


@method("route.export")
def route_export(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """把路线导出成可直接看的 Markdown（条目带 why 与来源链接）。"""
    params = _params(params)
    route_id = _require(params, "route_id")
    payload = learning.get_route(ctx.db, route_id)
    if payload is None:
        raise RpcError(Code.NOT_FOUND, f"学习路线不存在：{route_id}")

    plan = payload["plan"]
    lines = [f"# 学习路线：{payload['topic']}", ""]
    lines.append(
        f"- 水平：{payload.get('level') or '未指定'}｜周数：{payload.get('weeks') or len(plan.get('weeks', []))}"
    )
    lines.append(f"- 生成方式：{plan.get('mode', 'unknown')}")
    lines.append("")
    for week in plan.get("weeks", []):
        lines.append(f"## 第 {week['idx']} 周：{week.get('goal', '')}")
        lines.append("")
        for item in week.get("items", []):
            source = item.get("source_id")
            ref = f"（`{source}`）" if source else "（无出处）"
            lines.append(
                f"- **{item.get('title', '')}**｜{item.get('kind', '')}｜约 {item.get('est_minutes', 0)} 分钟{ref}"
            )
            if item.get("why"):
                lines.append(f"  - 为什么读它：{item['why']}")
        if week.get("checkpoint"):
            lines.append("")
            lines.append(f"**检查点**：{week['checkpoint']}")
        lines.append("")

    out = params.get("out")
    if isinstance(out, str) and out.strip():
        target = paths.under_data(out.strip())
    else:
        target = paths.under_data(Path("notes") / f"route_{route_id}.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    return {"ok": True, "path": paths.rel_to_home(target), "route_id": route_id}


@method("note.list")
def note_list(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    kind = params.get("kind") if isinstance(params.get("kind"), str) else None
    limit = _int_param(params, "limit", 50)
    return {"items": learning.list_notes(ctx.db, kind=kind, limit=limit)}


@method("note.get")
def note_get(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    note_id = _require(params, "note_id")
    payload = learning.get_note(ctx.db, note_id)
    if payload is None:
        raise RpcError(Code.NOT_FOUND, f"笔记不存在：{note_id}")
    return payload


@method("note.review")
def note_review(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """记一次抽背评价（"练"模式的记账）。

    `card_index` 是卡在这条笔记里的位置 —— **0 起**，就是 `note.get` / `note.due` 回的
    `cards[]` 下标（`card.card_index`）。一行卡片笔记通常有好几张卡，按行记等于把 10 张压成一个数，
    练完也说不清哪张没记住。

    缺参数**不默认成第 0 张**：默认会把评价记到别的卡上，比报错更难查。
    """
    params = _params(params)
    note_id = _require(params, "note_id")
    card_index = _card_index(params)
    rating = _require(params, "rating")
    response = params.get("response")
    correct = params.get("correct")
    try:
        result = learning.review_card(
            ctx.db,
            note_id,
            card_index,
            str(rating),
            response=str(response) if isinstance(response, str) and response else None,
            correct=bool(correct) if isinstance(correct, bool) else None,
        )
    except ValueError as exc:
        raise RpcError(Code.INVALID_PARAMS, str(exc)) from exc
    return {"ok": True, **result, "stats": learning.card_stats(ctx.db)}


@method("note.history")
def note_history(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """这张卡的历史作答（大浩选定：每题每次作答都留痕、可回看）。只读、零模型调用。

    老数据（迁移 0015 之前）只有档位，response / correct 是 null —— 如实标出，不假装那时也记了。
    """
    params = _params(params)
    note_id = _require(params, "note_id")
    index = _int_param(params, "card_index", 0)
    return {"items": learning.card_attempts(ctx.db, note_id, index)}


@method("memory.list")
def memory_list(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """跨会话记忆列表（D2）。**能看才能改** —— 自动记忆没有出口就是污染。"""
    del params
    from scholarscout.runtime import memory

    return {"items": memory.list_facts(ctx.db)}


@method("memory.add")
def memory_add(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    text = _require(params, "text")
    kind = params.get("kind") if isinstance(params.get("kind"), str) else "fact"
    target = params.get("target") if isinstance(params.get("target"), str) else "user"
    from scholarscout.runtime import memory

    try:
        return memory.add_fact(ctx.db, text, kind=str(kind), target=str(target))
    except ValueError as exc:
        raise RpcError(Code.INVALID_PARAMS, str(exc)) from exc


@method("memory.update")
def memory_update(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    fact_id = _require(params, "fact_id")
    text = _require(params, "text")
    kind = params.get("kind") if isinstance(params.get("kind"), str) else None
    from scholarscout.runtime import memory

    try:
        return memory.update_fact(ctx.db, fact_id, text, kind=kind)
    except ValueError as exc:
        raise RpcError(Code.INVALID_PARAMS, str(exc)) from exc


@method("memory.remove")
def memory_remove(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    fact_id = _require(params, "fact_id")
    from scholarscout.runtime import memory

    try:
        return memory.remove_fact(ctx.db, fact_id)
    except ValueError as exc:
        raise RpcError(Code.NOT_FOUND, str(exc)) from exc


@method("memory.extract")
def memory_extract(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """手动整理一次记忆（每会话最多一次；空表也是正确结果）。"""
    params = _params(params)
    session_id = _require(params, "session_id")
    from scholarscout.runtime import memory

    if ctx.llm is None:
        raise RpcError(Code.NOT_CONFIGURED, "未配置模型（把 LLM_API_KEY 写进 .env）")
    if ctx.config.summary_cfg is None:
        raise RpcError(Code.NOT_CONFIGURED, "摘要配置缺失，无法整理记忆")
    outcome = memory.extract_facts(
        ctx.llm, ctx.db, session_id, prompts_dir=ctx.config.summary_cfg.prompts_dir
    )
    usage = outcome.get("usage") or {}
    if usage:
        traces.record_usage(
            ctx.db,
            model=ctx.config.summary.model,
            purpose="memory",
            session_id=session_id,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            cached_tokens=usage.get("cached_tokens"),
        )
    return outcome


@method("learner.profile")
def learner_profile_rpc(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """学习者画像（跨会话）：水平、主题、卡片组掌握度、来源偏好。

    **只读、零模型调用** —— 掌握度是按确定性规则从 `card_reviews` 的排期算出来的，
    让模型"感觉"用户水平既不可复算，也会随措辞漂移。
    """
    del params
    from scholarscout.runtime import learner_profile

    return learner_profile.build_profile(ctx.db)


@method("note.due")
def note_due(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """今天该练哪些卡（B6 间隔重复）：新卡 + 到期卡。

    只读、纯本地算、**不花 token** —— 排期是 SM-2 的确定性结果，问模型等于把可复算的东西
    换成不可复算的东西。
    """
    params = _params(params)
    limit = _int_param(params, "limit", 30)
    note_id = params.get("note_id")
    note_id = str(note_id) if isinstance(note_id, str) and note_id else None
    return learning.due_cards(ctx.db, limit=limit, note_id=note_id)


@method("note.repair_options")
def note_repair_options(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """老卡补选项（大浩选定）：选择题没写选项时，用模型补上 A/B/C/D 并写回这张笔记。

    这是**要花钱**的一步（一次模型调用），但只补选项、不改题意与答案；模型没给出带选项的卡片时
    **原卡不动**（宁可没补成，也不能把用户的卡改坏）。
    """
    params = _params(params)
    note_id = _require(params, "note_id")
    if ctx.llm is None:
        raise RpcError(Code.NOT_CONFIGURED, "未配置模型（把 LLM_API_KEY 写进 .env）")
    from scholarscout.runtime import practice

    outcome = practice.build_option_repair(ctx.llm, ctx.db, note_id=note_id)
    usage = outcome.get("usage") or {}
    if usage:
        traces.record_usage(
            ctx.db,
            model=ctx.config.summary.model,
            purpose="practice_repair_options",
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            cached_tokens=usage.get("cached_tokens"),
        )
    return outcome


@method("note.help")
def note_help(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """抽背卡「不会」时问模型：结合复习记录 + 该会话已入库资料，给答案、讲解与该补哪条资料。

    **这一步要花钱**（一次模型调用），所以只有用户主动点才会走到这里 ——
    选项渲染、选择/填空判分全在本地，不产生费用。
    """
    params = _params(params)
    note_id = _require(params, "note_id")
    index = _card_number(params)
    row = learning.get_note(ctx.db, note_id) or {}
    cards = learning.parse_cards(str(row.get("content") or ""))
    if index < 0 or index >= len(cards):
        return {"text": "", "reason": "这张卡不在了", "sources": []}
    if ctx.llm is None:
        raise RpcError(Code.NOT_CONFIGURED, "未配置模型（把 LLM_API_KEY 写进 .env）")
    from scholarscout.runtime import practice

    card = {**cards[index], "note_id": note_id, "card_index": index + 1}
    session_id = row.get("session_id")
    # 流式：只在桌面（有 emit）且总开关打开时开；增量为**片段**，事件名是 practice.delta
    # （已在 rpc/server.py 的 NOTIFICATION_METHODS 登记，否则会被加上 chat. 前缀发错名）。
    raw_emit = ctx.emit
    on_delta: Callable[[str], None] | None = None
    if raw_emit is not None and bool(getattr(ctx.config.runtime, "stream", False)):

        def emit_practice_delta(piece: str) -> None:
            raw_emit(
                "practice.delta",
                {
                    "note_id": note_id,
                    "card_index": index + 1,
                    "delta": piece,
                    # 事件必须能分派回会话（docs/20 §3.1）：抽背是某个会话里的动作，
                    # 前端靠它把增量归位；note 没挂会话时**不伪造**一个空串。
                    **({"session_id": session_id} if session_id else {}),
                },
            )

        on_delta = emit_practice_delta

    outcome = practice.build_help(
        ctx.llm,
        ctx.db,
        session_id=str(session_id) if session_id else None,
        card=card,
        response=str(params.get("response") or ""),
        stage="hint" if params.get("stage") == "hint" else "full",
        on_delta=on_delta,
    )
    usage = outcome.get("usage") or {}
    if usage:
        traces.record_usage(
            ctx.db,
            model=ctx.config.summary.model,
            purpose="practice_help",
            session_id=str(session_id) if session_id else None,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            cached_tokens=usage.get("cached_tokens"),
        )
    return {
        "text": outcome["text"],
        "sources": outcome["sources"],
        "truncated": outcome["truncated"],
        # 流式中途断开时的真实原因（正常返回时是 None）；界面据此在保留的半句后附一行说明
        "error": outcome.get("error"),
    }


@method("note.extend")
def note_extend(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """主动延伸：按原卡出 2 道更深的题，并**存成一张新卡笔记**（这样它会进复习计划）。

    模型没按格式输出时如实返回空题、**不建空笔记**（库里不留废卡）。
    """
    params = _params(params)
    note_id = _require(params, "note_id")
    index = _card_number(params)
    row = learning.get_note(ctx.db, note_id) or {}
    cards = learning.parse_cards(str(row.get("content") or ""))
    if index < 0 or index >= len(cards):
        return {"questions": [], "note_id": None, "reason": "这张卡不在了"}
    if ctx.llm is None:
        raise RpcError(Code.NOT_CONFIGURED, "未配置模型（把 LLM_API_KEY 写进 .env）")
    from scholarscout.runtime import practice

    card = {**cards[index], "note_id": note_id, "card_index": index + 1}
    session_id = row.get("session_id")
    outcome = practice.build_extension(
        ctx.llm,
        ctx.db,
        session_id=str(session_id) if session_id else None,
        card=card,
        response=str(params.get("response") or ""),
        count=2,
    )
    usage = outcome.get("usage") or {}
    if usage:
        traces.record_usage(
            ctx.db,
            model=ctx.config.summary.model,
            purpose="practice_extend",
            session_id=str(session_id) if session_id else None,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            cached_tokens=usage.get("cached_tokens"),
        )
    return {
        "questions": outcome["questions"],
        "note_id": outcome["note_id"],
        "stored": outcome["stored"],
    }


@method("note.grade")
def note_grade(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """本地判分（③ 抽背卡）：选择题按字母、填空题精确比对 —— **零模型调用**。

    判不了就如实回 `graded=false` + 原因（简答 / 答案里认不出字母 / 没作答），
    不猜对错：抽背里"猜错判"比"不判"更伤，用户会照错误反馈去复习。
    """
    params = _params(params)
    note_id = _require(params, "note_id")
    index = _card_number(params)
    response = str(params.get("response") or "")
    row = learning.get_note(ctx.db, note_id) or {}
    cards = learning.parse_cards(str(row.get("content") or ""))
    if index < 0 or index >= len(cards):
        return {"graded": False, "reason": "这张卡不在了"}
    return learning.grade_card(cards[index], response)


@method("note.reveal")
def note_reveal(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """取某张抽背卡的答案（② 答案谜底分离）。

    为什么单独一个方法：`note.due` 只发题目。如果把答案一起发下去，"看答案"就只是前端的一个显示开关，考生（或任何打开开发者工具的人）随时能看到 —— 那就不叫抽背了。
    """
    params = _params(params)
    note_id = _require(params, "note_id")
    card_index = _int_param(params, "card_index", 0)
    return learning.reveal_card(ctx.db, note_id, card_index)


@method("note.stats")
def note_stats(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """抽背总体情况：练过几张、各档多少、记住率（分母只算评过的卡）。"""
    del params
    return learning.card_stats(ctx.db)


@method("route.revise")
def route_revise_rpc(ctx: AppContext, params: dict[str, Any]) -> ToolResult:
    """对已有路线做增量修改（与模型用的 `route.revise` 工具**同一条实现**）。

    为什么 RPC 与工具共用一个实现：这是"必须一致"的路径 —— 界面上点一次和模型调用一次
    得产生完全相同的结果与版本行为，分成两份迟早会漂移。

    返回类型就是工具的信封（`ToolResult` = `OkEnvelope | ErrorEnvelope`）：成功 `ok:true`，
    失败由这一层翻成 `-32602`（与 `route.versions` 的参数错误同一口径）—— 工具自己不抛，
    所以这里用 `.get` 读分支（不是下标：成功信封本来就没有 `error` 键）。
    """
    params = _params(params)
    from scholarscout.tools.route_revise import route_revise

    route_id = str(params.get("route_id") or "").strip()
    instruction = str(params.get("instruction") or "").strip()
    tool_ctx = ToolContext(db=ctx.db, config=ctx.config, http=ctx.http, llm=ctx.llm)
    result = route_revise(tool_ctx, route_id, instruction)
    if result.get("ok") is False:
        raise RpcError(Code.INVALID_PARAMS, str(result.get("error") or "增量修改失败"))
    return result


@method("route.versions")
def route_versions(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """列出这条路线**已归档的旧版本**（改错了要能退回去，所以得看得见）。

    `with_payload=true` 时连正文一起给（用于对照查看或回退），默认只给元信息。
    """
    params = _params(params)
    route_id = str(params.get("route_id") or "").strip()
    if not route_id:
        raise RpcError(Code.INVALID_PARAMS, "route_id 不能为空")
    if not bool(params.get("with_payload")):
        return {"items": learning.list_versions(ctx.db, kind="route", artifact_id=route_id)}
    items = []
    for meta in learning.list_versions(ctx.db, kind="route", artifact_id=route_id):
        full = learning.get_version(
            ctx.db, kind="route", artifact_id=route_id, version=int(meta["version"])
        )
        items.append(full or meta)
    return {"items": items}


# --------------------------------------------------------------------- 分发
