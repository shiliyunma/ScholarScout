"""会话与对话：`session.*` / `chat.*`。

方法实现分散在这里，注册仍然走 `registry.method` 装饰器 —— 注册表只有一份。
"""

from __future__ import annotations

from typing import Any

from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.library_refresh import refresh_exports
from scholarscout.rpc.registry import AppContext, _int_param, _params, _require, method
from scholarscout.runtime import loop, titles
from scholarscout.state import export_html, export_session, sessions, traces, usage


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
        # 建会话也要**推一条事件**（用户报的 bug：飞书中转入站会话在界面上看不见）。
        # 之前只有"标题真被改写"才发 `session.updated`（见 `_title_before_turn`），于是
        # `session.ensure` 这条入口**一个事件都不发** —— 界面的会话列表靠事件驱动刷新，
        # 库里明明有会话与消息（`run/state.db` 的 `ses_*` + 三条消息），界面却要等下一次手动动作。
        # **复用既有事件名**（不新造，否则 `NOTIFICATION_METHODS` 白名单要改、前端要加分支）：
        # `desktop/ui/api.js:365` 处理 `session.updated` 时**不看 `p.title` 也会 `loadSessions()`**
        # （标题那段才判 title），所以只带 `session_id` 就足以把新会话拉进左栏。
        # 载荷里 `created: True` 让前端/桩测能区分"新建"与"标题改写"（旧载荷带 `title`/`source`）。
        if ctx.emit is not None:
            ctx.emit("session.updated", {"session_id": session_id, "created": True})
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
            # 导出件也是产物：写完立刻入库（与 py.exec 同一个函数），否则"导出的看不到"
            refresh_exports(ctx, session_id)
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
    refresh_exports(ctx, session_id)
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


#: `chat.send` 的两种模式（docs/20 §3.2）：`sync`=阻塞返回整轮（**默认，旧语义一字不变**）；
#: `async`=立即返回 `turn_id`，整轮结论走 `chat.done` 事件 / `chat.result` 补取。
_MODE_SYNC = "sync"
_MODE_ASYNC = "async"


def _submit_rejection(rejected: dict[str, Any]) -> RpcError:
    """把工作单元的拒绝翻译成契约错误，**并带上"谁在跑"**（docs/20 §5.3：拒绝要如实告知）。

    为什么不回一个 `{ok:false}` 的 result：`mode="sync"` 的调用方（现有界面/脚本）读的是
    `result["text"]` 这类键，回一个形状不同的对象会让它们以别的方式崩 —— 报错才是诚实的失败。
    """
    reason = str(rejected.get("reason") or "rejected")
    running = list(rejected.get("running") or [])
    names = "、".join(str(item.get("session_id")) for item in running)
    data = {"reason": reason, "running": running}
    if reason == "turn_in_progress":
        return RpcError(
            Code.INVALID_REQUEST,
            f"这个会话已有一轮在跑（turn_in_progress：{rejected.get('turn_id')}）"
            "——同一会话不许两轮并行（消息序号与会话台账都会被踩坏）",
            data,
        )
    if reason == "at_capacity":
        return RpcError(
            Code.SERVICE_UNAVAILABLE,
            f"并发上限已满（at_capacity）：正在跑 {len(running)} 个会话（{names}）。"
            "先停一个或等一个结束（按设计**不排队**）。",
            data,
        )
    if reason == "daily_budget_exceeded":
        budget = dict(rejected.get("budget") or {})
        tokens = dict(budget.get("tokens") or {})
        return RpcError(
            Code.SERVICE_UNAVAILABLE,
            "今日用量已达上限（daily_budget_exceeded）："
            f"已用 {tokens.get('used')} / 上限 {tokens.get('limit')} tokens（滚动 24 小时）。"
            "调高 config.yaml 的 runtime.daily_token_budget 或等窗口过去后再继续。",
            data,
        )
    return RpcError(Code.SERVICE_UNAVAILABLE, f"本轮未被接受（{reason}）", data)


@method("chat.send")
def chat_send(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """发一条消息并跑一轮。

    `mode="sync"`（**默认**）：行为与改造前**逐字一致** —— 阻塞到整轮结束，返回整轮结论。
    现有界面不改一行也照常工作；`mode="async"`：立即返回 `{turn_id, session_id, accepted}`，
    整轮结论走 `chat.done` 事件（`text` + `message_id` 已带上）或 `chat.result(turn_id)` 补取。
    两种模式都走同一份 `SessionRuntime`（上限 3、同会话不许两轮并行）—— docs/20 §3.2。

    可选参数 `active_session_id`（异步模式由界面传）：**提交这一刻界面上正在显示的会话**。
    跑完提醒据此判「要不要私聊推一条」（非活跃会话才推）；不传 = 一律不推（不猜）。
    """
    params = _params(params)
    session_id = _require(params, "session_id")
    text = _require(params, "text")
    if not text.strip():
        raise RpcError(Code.INVALID_PARAMS, "text 不能为空")
    mode = str(params.get("mode") or _MODE_SYNC).strip().lower()
    if mode not in (_MODE_SYNC, _MODE_ASYNC):
        raise RpcError(Code.INVALID_PARAMS, f"不支持的 mode：{mode}（可用 sync / async）")
    if sessions.get_session(ctx.db, session_id) is None:
        raise RpcError(Code.NOT_FOUND, f"会话不存在：{session_id}")
    toolsets = params.get("toolsets") or list(ctx.toolsets)
    if not isinstance(toolsets, list):
        raise RpcError(Code.INVALID_PARAMS, "toolsets 必须是数组")
    # 模型客户端在**提交之前**校验：异常变成"一轮的错误收尾"会把 NOT_CONFIGURED 这种可诊断的
    # 配置问题藏进事件流里，而改造前它是直接抛给调用方的。
    ctx.require_llm()

    runtime = ctx.session_runtime()
    # `active_session_id`：**提交这一刻界面上正在显示的会话**（docs/20 §6.3 的跑完提醒要用它判
    # "这一轮该不该推私聊"）。运行时**不猜**：前端没传 → 提醒一条都不发（`no_active_session`）。
    accepted = runtime.submit_turn(
        session_id=session_id,
        text=text,
        toolsets=[str(t) for t in toolsets],
        emit=ctx.emit,
        # 标题在**工作线程里、跑轮次之前**定下来（顺序与改造前一致），
        # 但它会调一次模型 —— 放在这里才不拖住 `mode="async"` 的立即返回。
        before_start=lambda: _title_before_turn(ctx, session_id, text),
        active_session_id=str(params.get("active_session_id") or "").strip() or None,
    )
    if not accepted.get("ok"):
        raise _submit_rejection(accepted)
    turn_id = str(accepted["turn_id"])
    if mode == _MODE_ASYNC:
        return {"turn_id": turn_id, "session_id": session_id, "accepted": True}

    result = runtime.wait(turn_id)
    if result is None:  # 只有轮次异常结束（连结果都没留下）才会走到这里，如实报错
        raise RpcError(Code.INTERNAL_ERROR, f"轮次异常结束：{turn_id}（结果信箱里没有它）")
    return result.as_dict()


@method("chat.cancel")
def chat_cancel(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """请求取消一轮对话。**只影响被点到的那一个会话**（docs/20 §6.2）。

    **可以按 `session_id` 取消**（界面用这个）：整轮结束前前端拿不到 `turn_id`，
    只按 turn_id 取消的话，这个 RPC 从设计上就无法被界面调用。
    执行时机是**步间检查**：正在跑的那一步（模型调用或工具调用）结束后才生效 ——
    界面上必须如实这么提示，别让用户以为点了就立刻停。

    实现上按 `session_id` 找到该会话自己的取消 Event（`TurnHandle.cancel_event`）：
    取消信号是**每轮一个对象**，所以"取消 A 误伤 B"在机制上不可能。
    """
    params = _params(params)
    turn_id = str(params.get("turn_id") or "").strip()
    session_id = str(params.get("session_id") or "").strip()
    outcome = ctx.session_runtime().cancel(session_id=session_id or None, turn_id=turn_id or None)
    if outcome.get("ok"):
        return outcome
    # 兼容阶梯（老契约，`tests/rpc/test_cancel_and_session_usage.py` 钉着四条）：
    # ① 只给 session_id 时回退到全局 `ctx.running` 注册表找它（CLI/脚本不走工作单元池的情形）；
    # ② 拿到 turn_id 后记进 `ctx.cancelled` —— 这是**按 turn_id 的兼容通道**（留痕），
    #    真正生效的取消机制是那一轮自己的 Event（`TurnHandle.cancel_event`）。
    if not turn_id and session_id:
        turn_id = ctx.running.get(session_id, "")
    if turn_id:
        ctx.cancelled.add(turn_id)
        return {
            "ok": True,
            "turn_id": turn_id,
            "note": "已请求取消：当前这一步结束后生效（步间检查）",
        }
    return {
        "ok": False,
        "reason": "no_running_turn",
        "note": "这个会话当前没有正在跑的轮次（可能已经结束）",
    }


@method("chat.status")
def chat_status(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """哪些会话在跑 / 跑在什么阶段 / 容量还剩多少（docs/20 §3.2）。

    多会话并行的**观测口**：界面靠它给每个在跑的会话画"进行中 + 停止"，靠 `capacity`
    判断"还能不能再开一个"。不给 `session_id` 时连各会话的用量账一起回。
    """
    params = _params(params)
    session_id = params.get("session_id")
    return ctx.session_runtime().status(str(session_id) if session_id else None)


@method("chat.result")
def chat_result(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """按 `turn_id` 补取一轮的结论（迟到 / 掉线重连场景，docs/20 §3.2）。

    结果信箱只留最近 32 轮；**取不到就如实说取不到**（`unknown_turn`），
    绝不回一个"看着像结果"的空壳。
    """
    params = _params(params)
    turn_id = _require(params, "turn_id")
    result = ctx.session_runtime().result(str(turn_id))
    if result is None:
        return {
            "ok": False,
            "reason": "unknown_turn",
            "turn_id": turn_id,
            "note": "结果信箱里没有这一轮（可能太旧、已被挤出，或轮次号写错了）",
        }
    return result.as_dict()


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
    row = usage.session_usage_totals(ctx.db, session_id)
    by_purpose = usage.session_usage_by_purpose(ctx.db, session_id)
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
        "by_purpose": by_purpose,
    }


# --------------------------------------------------------------------- 设置
