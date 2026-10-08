"""配置与设置：`config.*` / `settings.*`（含「密钥只写不回读」的口径）。

方法实现分散在这里，注册仍然走 `registry.method` 装饰器 —— 注册表只有一份。
"""

from __future__ import annotations

from typing import Any

from scholarscout import settings_io
from scholarscout.providers.llm import LlmError, create_client
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
            "enable_kb": ctx.config.features.enable_kb,
            "enable_mineru": ctx.config.features.enable_mineru,
            "enable_topic_map": ctx.config.features.enable_topic_map,
            "enable_route_plan": ctx.config.features.enable_route_plan,
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
