"""学习相关：`route.*`（学习路线）/ `note.*`（笔记与抽背卡）/ `memory.*`（跨会话记忆）。

方法实现分散在这里，注册仍然走 `registry.method` 装饰器 —— 注册表只有一份。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scholarscout import paths
from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.registry import AppContext, _int_param, _params, _require, method
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state import learning, traces


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

    `card_index` 是卡在这条笔记里的位置（`note.get` 返回的 `cards[].index`）——
    一行卡片笔记通常有好几张卡，按行记等于把 10 张压成一个数，练完也说不清哪张没记住。
    """
    params = _params(params)
    note_id = _require(params, "note_id")
    card_index = _int_param(params, "card_index", -1)
    rating = _require(params, "rating")
    try:
        result = learning.review_card(ctx.db, note_id, card_index, str(rating))
    except ValueError as exc:
        raise RpcError(Code.INVALID_PARAMS, str(exc)) from exc
    return {"ok": True, **result, "stats": learning.card_stats(ctx.db)}


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
    from scholarscout.runtime import memory

    try:
        return memory.add_fact(ctx.db, text, kind=str(kind))
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
    return learning.due_cards(ctx.db, limit=limit)


@method("note.grade")
def note_grade(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """本地判分（③ 抽背卡）：选择题按字母、填空题精确比对 —— **零模型调用**。

    判不了就如实回 `graded=false` + 原因（简答 / 答案里认不出字母 / 没作答），
    不猜对错：抽背里"猜错判"比"不判"更伤，用户会照错误反馈去复习。
    """
    params = _params(params)
    note_id = _require(params, "note_id")
    try:
        index = int(params.get("card_index") or 1) - 1
    except (TypeError, ValueError):
        index = -1
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
def route_revise_rpc(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """对已有路线做增量修改（与模型用的 `route.revise` 工具**同一条实现**）。

    为什么 RPC 与工具共用一个实现：这是"必须一致"的路径 —— 界面上点一次和模型调用一次
    得产生完全相同的结果与版本行为，分成两份迟早会漂移。
    """
    params = _params(params)
    from scholarscout.tools.route_revise import route_revise

    route_id = str(params.get("route_id") or "").strip()
    instruction = str(params.get("instruction") or "").strip()
    tool_ctx = ToolContext(db=ctx.db, config=ctx.config, http=ctx.http, llm=ctx.llm)
    try:
        return route_revise(tool_ctx, route_id, instruction)
    except ValueError as exc:
        raise RpcError(Code.INVALID_PARAMS, str(exc)) from exc


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
