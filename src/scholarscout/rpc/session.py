"""会话与对话：`session.*` / `chat.*`。

方法实现分散在这里，注册仍然走 `registry.method` 装饰器 —— 注册表只有一份。
"""

from __future__ import annotations

from typing import Any

from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.registry import AppContext, _int_param, _params, _require, method
from scholarscout.runtime import loop, titles
from scholarscout.state import export_html, export_session, sessions, traces


@method("session.create")
def session_create(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    title = params.get("title")
    session_key = params.get("session_key")
    session_id = sessions.create_session(
        ctx.db,
        title=title if isinstance(title, str) else None,
        session_key=session_key if isinstance(session_key, str) else None,
        source="desktop" if ctx.emit else "cli",
        model=ctx.config.chat.model,
    )
    loop.mark_system_prompt(ctx.db, session_id, toolsets=ctx.toolsets, config=ctx.config)
    row = sessions.get_session(ctx.db, session_id) or {}
    return {"session_id": session_id, "created_at": row.get("created_at")}


@method("session.ensure")
def session_ensure(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """按 `session_key` **幂等**拿会话（存在就返回，不存在才建）—— 渠道层入口用（docs/17 §3.4）。

    和 `session.create` 的区别：`create` 是"新开一个"（session_key 撞唯一约束就报错）；
    这里把外部 `chat_id` 当 key，**同一个群/人复用同一条会话**，所以必须幂等。
    """
    params = _params(params)
    session_key = _require(params, "session_key")
    title = params.get("title")
    source = params.get("source")
    session_id, created = sessions.ensure_session(
        ctx.db,
        session_key=str(session_key),
        title=title if isinstance(title, str) else None,
        source=str(source)
        if isinstance(source, str) and source
        else ("desktop" if ctx.emit else "cli"),
        model=ctx.config.chat.model,
    )
    if created:
        loop.mark_system_prompt(ctx.db, session_id, toolsets=ctx.toolsets, config=ctx.config)
    row = sessions.get_session(ctx.db, session_id) or {}
    return {
        "session_id": session_id,
        "created": created,
        "created_at": row.get("created_at"),
    }


@method("session.rename")
def session_rename(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """改会话标题（改过之后就不再被自动命名覆盖）。"""
    params = _params(params)
    session_id = _require(params, "session_id")
    title = _require(params, "title")
    stored = sessions.rename_session(ctx.db, session_id, str(title))
    if stored is None:
        raise RpcError(Code.NOT_FOUND, f"会话不存在：{session_id}")
    return {"ok": True, "session_id": session_id, "title": stored}


@method("session.list")
def session_list(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    limit = _int_param(params, "limit", 50)
    offset = _int_param(params, "offset", 0)
    return {"items": sessions.list_sessions(ctx.db, limit=limit, offset=offset)}


@method("session.messages")
def session_messages(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    session_id = _require(params, "session_id")
    limit = _int_param(params, "limit", 50)
    before = params.get("before_idx")
    rows, has_more = sessions.list_messages(
        ctx.db,
        session_id,
        limit=limit,
        before_idx=int(before) if isinstance(before, int | float) else None,
    )
    return {"items": rows, "has_more": has_more}


@method("session.pin")
def session_pin(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """置顶 / 取消置顶。不碰 `updated_at`（置顶只是换位置，不该假装"最近更新过"）。"""
    params = _params(params)
    session_id = _require(params, "session_id")
    pinned = bool(params.get("pinned", True))
    if not sessions.set_pinned(ctx.db, session_id, pinned):
        raise RpcError(Code.NOT_FOUND, f"会话不存在：{session_id}")
    return {"ok": True, "session_id": session_id, "pinned": pinned}


@method("session.delete")
def session_delete(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """删会话。返回各表删掉的行数 —— 界面要如实告诉用户删掉了什么。

    `keep_artifacts=true` 只删对话，留下这次会话产出的路线/地图/笔记。
    """
    params = _params(params)
    session_id = _require(params, "session_id")
    keep = bool(params.get("keep_artifacts", False))
    removed = sessions.delete_session(ctx.db, session_id, keep_artifacts=keep)
    if removed is None:
        raise RpcError(Code.NOT_FOUND, f"会话不存在：{session_id}")
    return {"ok": True, "session_id": session_id, "removed": removed, "kept_artifacts": keep}


@method("session.export")
def session_export(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """导出会话。`fmt="md"`（默认）给可复制的 Markdown；`fmt="html"` 给**自包含单页 HTML**。

    为什么两种：Markdown 适合再编辑，但发给别人常被聊天软件吃掉格式；单页 HTML 内联渲染器、
    不引外部资源，双击就能看（答辩演示/发人都用得上）。**两者正文同源**（同一套路径打码规则）。
    """
    params = _params(params)
    session_id = _require(params, "session_id")
    fmt = str(params.get("fmt") or "md").strip().lower()
    if fmt not in ("md", "html"):
        raise RpcError(Code.INVALID_PARAMS, f"不支持的导出格式：{fmt}（可用 md / html）")
    try:
        if fmt == "html":
            saved = export_html.export_html_to_file(ctx.db, session_id)
            return {
                "ok": True,
                "session_id": session_id,
                "fmt": "html",
                "path": saved["path"],
                "chars": saved["chars"],
                "bytes": saved["bytes"],
                # HTML 整份回给界面没有用处（界面只显示路径），所以不回 text
                "text": "",
            }
        result = export_session.export_to_file(ctx.db, session_id)
    except KeyError:
        raise RpcError(Code.NOT_FOUND, f"会话不存在：{session_id}") from None
    return {
        "ok": True,
        "session_id": session_id,
        "fmt": "md",
        "path": result["path"],
        "text": result["text"],
        "chars": len(result["text"]),
    }


# --------------------------------------------------------------------- 对话


def _title_before_turn(ctx: AppContext, session_id: str, text: str) -> None:
    """轮次开始前先把标题定下来 —— 用户一发送就能在左侧栏看到名字。

    顺序很重要（踩过）：用户消息一落库，`sessions.retitle_if_unnamed` 就会用**本地截断**
    起名；那个名字看起来像真标题，之后模型总结的标题**再也写不进去**。
    所以必须"先总结标题 → 再跑轮次"。

    任何失败都静默放过：标题不值得让一轮对话失败，本地兜底会补上。
    """
    if not ctx.config.generation.title_by_model or ctx.llm is None:
        return
    row = sessions.get_session(ctx.db, session_id)
    if row is not None and not sessions.is_unnamed(row["title"]):
        return  # 已经有真标题（含用户手动改名）→ 一次调用都不花
    suggestion = titles.suggest_title(ctx.llm, text, prompts_dir=ctx.config.summary_cfg.prompts_dir)
    if suggestion is None:
        return
    # 这一次调用的钱也要记（purpose="title"）—— 否则"关掉标题总结能省多少"没人答得出来
    traces.record_usage(
        ctx.db,
        model=ctx.config.chat.model,
        purpose="title",
        session_id=session_id,
        prompt_tokens=suggestion.prompt_tokens,
        completion_tokens=suggestion.completion_tokens,
    )
    applied = sessions.set_title_if_unnamed(ctx.db, session_id, suggestion.title)
    if applied and ctx.emit is not None:
        ctx.emit("session.updated", {"session_id": session_id, "title": applied, "source": "model"})


@method("chat.send")
def chat_send(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    session_id = _require(params, "session_id")
    text = _require(params, "text")
    if not text.strip():
        raise RpcError(Code.INVALID_PARAMS, "text 不能为空")
    if sessions.get_session(ctx.db, session_id) is None:
        raise RpcError(Code.NOT_FOUND, f"会话不存在：{session_id}")
    toolsets = params.get("toolsets") or list(ctx.toolsets)
    if not isinstance(toolsets, list):
        raise RpcError(Code.INVALID_PARAMS, "toolsets 必须是数组")

    _title_before_turn(ctx, session_id, text)  # 标题在轮次前定下来（用户一发送就能看到）
    result = loop.run_turn(
        ctx.turn_context(),
        session_id=session_id,
        text=text,
        toolsets=[str(t) for t in toolsets],
        emit=ctx.emit,
    )
    return result.as_dict()


@method("chat.cancel")
def chat_cancel(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """请求取消一轮对话。

    **可以按 `session_id` 取消**（界面用这个）：整轮结束前前端拿不到 `turn_id`，
    只按 turn_id 取消的话，这个 RPC 从设计上就无法被界面调用。
    `turn_id` 仍然接受（脚本/测试用）。

    执行时机是**步间检查**：正在跑的那一步（模型调用或工具调用）结束后才生效 ——
    界面上必须如实这么提示，别让用户以为点了就立刻停。
    """
    params = _params(params)
    turn_id = str(params.get("turn_id") or "").strip()
    session_id = str(params.get("session_id") or "").strip()
    if not turn_id and session_id:
        turn_id = ctx.running.get(session_id, "")
    if not turn_id:
        return {
            "ok": False,
            "reason": "no_running_turn",
            "note": "这个会话当前没有正在跑的轮次（可能已经结束）",
        }
    ctx.cancelled.add(turn_id)
    return {
        "ok": True,
        "turn_id": turn_id,
        "note": "已请求取消：当前这一步结束后生效（步间检查）",
    }


# --------------------------------------------------------------------- 轨迹


@method("session.usage")
def session_usage(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """这个会话一共花了多少（token 与调用次数）。

    为什么值得单列：一轮 3–12 分钟、多次模型调用，用户只看得到"这一轮的 token"，
    看不到"这次会话累计"。口径：直接来自 `usage_records`（唯一取数入口），
    单价未配置时 `est_cost` 回 None —— **不猜价格**，只报可核对的 token。
    """
    params = _params(params)
    session_id = _require(params, "session_id")
    total = ctx.db.query_one(
        "SELECT count(*) AS calls, coalesce(sum(prompt_tokens),0) AS prompt_tokens,"
        " coalesce(sum(completion_tokens),0) AS completion_tokens,"
        " coalesce(sum(cached_tokens),0) AS cached_tokens,"
        " count(cached_tokens) AS calls_with_cache_field,"
        " coalesce(sum(est_cost),0.0) AS est_cost FROM usage_records WHERE session_id = ?",
        (session_id,),
    )
    by_purpose = ctx.db.query(
        "SELECT coalesce(purpose,'?') AS purpose, count(*) AS calls,"
        " coalesce(sum(prompt_tokens),0) AS prompt_tokens,"
        " coalesce(sum(completion_tokens),0) AS completion_tokens"
        " FROM usage_records WHERE session_id = ? GROUP BY 1 ORDER BY calls DESC",
        (session_id,),
    )
    row = dict(total) if total is not None else {}
    cost = float(row.get("est_cost") or 0.0)
    prompt_tokens = int(row.get("prompt_tokens") or 0)
    cached_tokens = int(row.get("cached_tokens") or 0)
    return {
        "session_id": session_id,
        "calls": int(row.get("calls") or 0),
        "tokens": {
            "prompt": prompt_tokens,
            "completion": int(row.get("completion_tokens") or 0),
            # 缓存命中的输入（服务端真返回了才非零；一口都没命中时这里是 0，与"没这个字段"不同）
            "cached": cached_tokens,
        },
        # 缓存命中率：**只在服务端确实返回过缓存字段时**才算，否则回 None
        # （把"服务端不报"算成 0% 会让人误以为前缀策略完全失效）
        "cache_hit_rate": (
            cached_tokens / prompt_tokens
            if int(row.get("calls_with_cache_field") or 0) > 0 and prompt_tokens > 0
            else None
        ),
        # 单价没配就不报金额（报 0 会让人以为"免费"）
        "est_cost": cost if cost > 0 else None,
        "by_purpose": [dict(item) for item in by_purpose],
    }


# --------------------------------------------------------------------- 设置
