"""调研相关：`topic.*`（知识地图）/ `research.*`（看板）/ `search.*` / `trace.*` / `system.*`。

方法实现分散在这里，注册仍然走 `registry.method` 装饰器 —— 注册表只有一份。
"""

from __future__ import annotations

from typing import Any

from scholarscout import __version__
from scholarscout.providers.settings import provider_statuses
from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.registry import AppContext, _int_param, _params, _require, method
from scholarscout.runtime import registry
from scholarscout.state import learning, traces


@method("system.health")
def system_health(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "version": __version__,
        "db_path": str(ctx.config.db_path),
        "counts": ctx.db.counts(),
        "tools": registry.registered_names(),
        "llm_configured": bool(ctx.config.api_key()),
    }


@method("trace.list")
def trace_list(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    limit = _int_param(params, "limit", 100)
    items = traces.list_traces(
        ctx.db,
        session_id=params.get("session_id") if isinstance(params.get("session_id"), str) else None,
        turn_id=params.get("turn_id") if isinstance(params.get("turn_id"), str) else None,
        tool_name=params.get("tool_name") if isinstance(params.get("tool_name"), str) else None,
        limit=limit,
    )
    return {"items": items}


# --------------------------------------------------------------------- 资料库


@method("search.providers")
def search_providers(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """哪些搜索引擎/来源现在能用、不能用的原因、以及怎么才能用。

    界面把这张表和"填 key 的输入框"放在一起，用户填完 key 立刻能看到它变成可用
    （`settings.update` 写 `.env` 的同时会更新进程环境，不必重启）。
    """
    del params
    settings = ctx.config.search_cfg
    items = [status.as_dict() for status in provider_statuses(settings)]
    return {
        "items": items,
        "auto": list(settings.auto_names()),
        "usable_count": sum(1 for item in items if item["usable"]),
        "note": "「auto」只带此刻真能用的来源；填了 key 立即生效（不必重启）。",
    }


@method("topic.list")
def topic_list(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """列出历史主题图（轻量条目，正文走 `topic.get`）。

    前端原先只能看本会话刚生成的产物，进程一退就再也点不开 —— 这个方法让
    库里攒下的产物都能翻出来看。
    """
    params = _params(params)
    limit = int(params.get("limit") or 50)
    return {"items": learning.list_topic_maps(ctx.db, limit=limit)}


@method("topic.get")
def topic_get(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    topic_id = _require(params, "topic_id")
    payload = learning.get_topic_map(ctx.db, topic_id)
    if payload is None:
        raise RpcError(Code.NOT_FOUND, f"主题地图不存在：{topic_id}")
    return payload


@method("research.board")
def research_board(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """研究看板（B2 证据表 + B4 时间线 + B7 看板）：**只读**，零模型调用。

    为什么合成一个 RPC：这三节是同一次"用户打开右栏"的动作，拆成三个方法会让前端连打三枪、
    还可能出现"证据表是新的、时间线是旧的"这种不一致。
    """
    params = _params(params)
    session_id = _require(params, "session_id")
    from scholarscout.runtime import research_view

    return research_view.build_board(ctx.db, session_id)


@method("research.set_question")
def research_set_question(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """勾选子问题是否已覆盖（看板可编辑）。**只写状态，不调模型**。

    编号从 1 开始，与界面上的序号一致；越界或非法状态直接报错，不静默改。
    """
    params = _params(params)
    session_id = _require(params, "session_id")
    try:
        index = int(params.get("index") or 0)
    except (TypeError, ValueError):
        index = 0
    status = str(params.get("status") or "")
    from scholarscout.runtime import research_state

    try:
        state = research_state.set_question_status(ctx.db, session_id, index, status)
    except ValueError as exc:
        raise RpcError(Code.INVALID_PARAMS, str(exc)) from exc
    return {"ok": True, "questions": state.get("questions") or []}


@method("research.overview")
def research_overview(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """跨会话的研究看板汇总（⑦）：**只读**，零模型调用。

    为什么单独开一个方法而不是让前端循环调 research.board：前端循环 N 次就是把
    "N 个会话各打一枪"搬到界面层，还可能出现半新半旧的混合结果。
    """
    params = _params(params)
    limit = int(params.get("limit") or 30)
    from scholarscout.runtime import research_view

    return research_view.build_overview(ctx.db, limit=limit)
