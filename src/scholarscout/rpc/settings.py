"""配置与设置：`config.*` / `settings.*`（含「密钥只写不回读」的口径）。

方法实现分散在这里，注册仍然走 `registry.method` 装饰器 —— 注册表只有一份。
"""

from __future__ import annotations

from typing import Any

from scholarscout import settings_io
from scholarscout.providers import models
from scholarscout.providers.llm import LlmError, create_client
from scholarscout.providers.thinking import LEVEL_TO_EFFORT, options, plan_for
from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.registry import AppContext, _params, method


@method("config.get")
def config_get(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """**绝不含密钥**（docs/01 §3.1）。"""
    return {
        "llm": {
            "model": ctx.config.chat.model,
            "base_url": ctx.config.chat.base_url,
            "api_key_set": bool(ctx.config.api_key()),
        },
        "runtime": {
            "max_tool_calls_per_turn": ctx.config.runtime.max_tool_calls_per_turn,
            "allow_clarify": ctx.config.runtime.allow_clarify,
        },
        "features": {
            "enable_mineru": ctx.config.features.enable_mineru,
            "enable_topic_map": ctx.config.features.enable_topic_map,
            "enable_route_plan": ctx.config.features.enable_route_plan,
            # 本地知识库检索（`kb.search`）：默认关；关着时这个工具**不在**工具清单里
            # （docs/01 §5.8）。回给界面是为了能看出"为什么没有 kb.search"。
            "enable_kb": ctx.config.features.enable_kb,
        },
        "paths": {
            "data_dir": str(ctx.config.data_dir),
            "db_path": str(ctx.config.db_path),
        },
    }


# --------------------------------------------------------------------- 会话


@method("settings.get")
def settings_get(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """当前设置（**密钥只报"是否已配置"，值永不回传**）。"""
    return settings_io.read_settings(ctx.config)


@method("settings.update")
def settings_update(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """改设置：校验 → 写回 `config.yaml`（密钥写 `.env`）→ **立即生效**（重建模型客户端）。"""
    params = _params(params)
    updates = params.get("updates")
    if not isinstance(updates, dict):
        raise RpcError(Code.INVALID_PARAMS, "updates 必须是一个对象")
    try:
        notes: list[str] = []
        new_config = settings_io.apply_updates(ctx.config, updates, notes=notes)
    except settings_io.SettingsError as exc:
        raise RpcError(Code.INVALID_PARAMS, str(exc)) from exc
    ctx.config = new_config
    try:
        ctx.llm = create_client(new_config.chat)
    except LlmError:
        # 换了地址/模型但密钥没配好 → 客户端置空，health 会如实显示"未配置模型"
        ctx.llm = None
    return {"ok": True, "settings": settings_io.read_settings(new_config), "notes": notes}


@method("models.list")
def models_list(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """可选模型 + 每个模型的**真实思考档位**（界面下拉置灰/标注的唯一来源）。

    为什么要有这个 RPC：界面上的两个下拉必须显示「服务端真支持什么」，而不是前端另写
    一份清单 —— 写了就必然和 `/models` 漂移。清单来自 ``GET /models``；探测失败**不报错**，
    而是回落到 ``config.yaml`` 里的候选 + 当前模型，并在 ``note`` 里如实说明没探到。

    ``params.remember=true`` 时把探测到的候选写进 ``llm.chat.available_models``
    （只在探测成功且与已记录的不同才写：读接口不该每次都动用户的配置文件）。
    """
    params = _params(params)
    chat = ctx.config.chat
    key = ctx.config.api_key()
    # 没 key 就不探测：必然 401，白等一次网络；直接回落配置并如实说明（也让 api_key 收窄成 str）
    if key:
        found = models.discover(
            base_url=chat.base_url,
            api_key=key,
            current=chat.model,
            configured=chat.available_models,
        )
    else:
        found = models.fallback(
            current=chat.model,
            configured=chat.available_models,
            note="未配置 LLM_API_KEY，跳过 /models 探测（候选来自 config.yaml + 当前模型）",
        )
    if params.get("remember") is True and found["source"] == "probe":
        names = tuple(found["available"])
        if names != chat.available_models:
            try:
                ctx.config = settings_io.remember_models(ctx.config, names)
            except (settings_io.SettingsError, OSError) as exc:
                # 写不进去只是"下次启动不知道有哪些模型"，**不能**把清单本身弄丢
                found["note"] = (found["note"] + "；" if found["note"] else "") + (
                    f"候选清单未能写进 config.yaml（{exc}），本次仍可使用"
                )
    supported = list(found["efforts"])
    plan = plan_for(chat.reasoning_effort, supported=supported, thinking=chat.thinking)
    levels = options(supported=supported, thinking=chat.thinking)
    return {
        "available_models": found["available"],
        "models": [
            {
                "id": item["id"],
                "label": item["label"],
                "supported_levels": item["efforts"],
                "default_level": item["default_effort"],
            }
            for item in found["models"]
        ],
        "current": chat.model,
        "current_level": chat.reasoning_effort,
        "current_plan": {
            "level": plan.level,
            "effort": plan.effort,
            "exact": plan.exact,
            "label": plan.label(),
        },
        # 六个选项（五档 + 关）：label 里带真实档位（"中（high）"），supported 供界面置灰。
        # supported 判的是**这一档原本要发的 effort** 在不在声明里 —— 不能用 plan 里降级后的
        # effort 判：降级总会落回支持集内，那样每一档都会被判"支持"，置灰功能等于没有（真踩过）。
        # 「关」不发 effort，永远可选。
        "level_options": [
            {
                **item,
                "supported": str(item["value"]) == "none"
                or LEVEL_TO_EFFORT.get(str(item["value"])) in supported,
            }
            for item in levels
        ],
        "supported_levels": supported,
        "default_level": found["default_effort"],
        "source": found["source"],
        "note": found["note"],
    }


@method("settings.usage")
def settings_usage(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """磁盘占用（数据库、抓取缓存、原件、全文、笔记、run/）。"""
    return {"usage": settings_io.data_usage(), "cleanable": settings_io.CLEANABLE}


@method("settings.cleanup")
def settings_cleanup(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """清理**缓存类**目录。**资料本体（data/raw、text、notes）与数据库不在可清理范围内。**

    `older_than_days` 给定时只删够旧的文件 —— 全清会让下次检索重新下载一遍。
    """
    params = _params(params)
    target = str(params.get("target") or "fetch_cache")
    raw_days = params.get("older_than_days")
    days = (
        float(raw_days)
        if isinstance(raw_days, int | float) and not isinstance(raw_days, bool)
        else None
    )
    try:
        removed = settings_io.cleanup(target=target, older_than_days=days)
    except settings_io.SettingsError as exc:
        raise RpcError(Code.INVALID_PARAMS, str(exc)) from exc
    return {"ok": True, "target": target, "older_than_days": days, "removed": removed}


@method("settings.optimize")
def settings_optimize(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """整理数据库（`VACUUM` + `PRAGMA optimize`）：**只整理、不删数据**。

    为什么值得单独给个入口：删会话/删资料不会让库文件变小，用户会以为"删了没用"。
    """
    del params
    result = settings_io.optimize_database(ctx.db)
    return {"ok": True, **result, "usage": settings_io.data_usage().get("database", {})}


# --------------------------------------------------------------------- 学习产物
