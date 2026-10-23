"""每群绑模块：`channel.set_module` / `channel.get_module` / `channel.module_for_chat`（迁移 0018）。

这三条补的是一个**曾被文档描述过、但代码里根本不存在**的能力（docs/21 §5.3 说"每群绑模块"，
而 `state/migrations/` 止于 0017、`rpc/method_names()` 里没有任何 `channel.*module*`）。
所以这里的断言不追"接口长什么样"，只钉四类"看起来成功了、其实是假的"的失败：

1. **写进去就是真写进库**（不是存在内存/回包好看）：`set` 之后 `get` 能读回，换绑是**覆盖**，
   同一个会话只有一行（主键 `(channel, chat_id)`）—— 追加历史会让"这个群到底用哪个"无解；
2. **没绑不是错误**：`get` 回 `{"module": null}`，不抛 —— 绝大多数群从没绑过，报错会把
   "没配置"渲染成"出故障"；
3. **非法 module 当场拒**（`-32602`）而不是静默落库：库里出现 `kb`（而 `enable_kb` 关着）
   这类"绑了但这一轮用不了"的行，就是又一个假功能；
4. **解析顺序只有一份、且失效不崩**：绑定 → 全局默认 → 兜底；绑定指向的模块已经不在了
   （开关关掉 / 白名单缩小 / 库里是脏值）→ **记 warning、回退全局默认、群消息照常走**。
   一个群因为一条过期绑定就收不到回复，是比"绑错了"更严重的事故。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

import pytest

from scholarscout.rpc import channel as channel_rpc
from scholarscout.rpc.errors import RpcError
from scholarscout.rpc.methods import AppContext, dispatch, method_names
from scholarscout.state import channel_modules as module_store

CHAT = "oc_aaa111"
OTHER = "oc_bbb222"


def _set(ctx: AppContext, module: str, *, chat_id: str = CHAT) -> dict[str, Any]:
    return dispatch(
        ctx, "channel.set_module", {"channel": "feishu", "chat_id": chat_id, "module": module}
    )


def _get(ctx: AppContext, *, chat_id: str = CHAT) -> dict[str, Any]:
    return dispatch(ctx, "channel.get_module", {"channel": "feishu", "chat_id": chat_id})


def _resolve(ctx: AppContext, *, chat_id: str = CHAT) -> dict[str, object]:
    return dispatch(ctx, "channel.module_for_chat", {"channel": "feishu", "chat_id": chat_id})


def _rows(ctx: AppContext) -> int:
    return ctx.db.scalar_int("SELECT count(*) FROM channel_modules")


def _with_kb(ctx: AppContext, *, on: bool) -> AppContext:
    """同一个库、同一个上下文，只翻 `features.enable_kb`（`kb` 模块在不在的唯一开关）。"""
    return replace(
        ctx, config=replace(ctx.config, features=replace(ctx.config.features, enable_kb=on))
    )


# ------------------------------------------------------------------ 设 / 取


def test_set_then_get_roundtrips_through_the_database(ctx: AppContext) -> None:
    """`set` 真写库（关掉再读回同一行），`get` 读到的就是写进去的那个模块。"""
    result = _set(ctx, "research")
    assert result == {
        "ok": True,
        "data": {"channel": "feishu", "chat_id": CHAT, "module": "research"},
    }
    assert _get(ctx) == {"module": "research"}

    # 绕开仓储直接看表：证明落的是 `channel_modules` 这一行，而不是回包自说自话
    row = ctx.db.query_one(
        "SELECT channel, chat_id, module FROM channel_modules WHERE channel = ? AND chat_id = ?",
        ("feishu", CHAT),
    )
    assert row is not None
    assert (row["channel"], row["chat_id"], row["module"]) == ("feishu", CHAT, "research")
    # updated_at 走 `now_iso()`：带时区偏移、带微秒（同秒内两次换绑也能排出次序）
    stamp = ctx.db.scalar(
        "SELECT updated_at FROM channel_modules WHERE channel = ? AND chat_id = ?", ("feishu", CHAT)
    )
    assert str(stamp).endswith("+08:00") or "T" in str(stamp)
    assert "." in str(stamp), "updated_at 缺微秒，同秒换绑的次序会不确定"


def test_unbound_chat_returns_null_instead_of_an_error(ctx: AppContext) -> None:
    """没绑过 = 正常状态：回 `null`，**不抛**（否则界面会把"没配置"显示成"故障"）。"""
    assert _get(ctx) == {"module": None}
    assert _rows(ctx) == 0, "读一个没绑过的群不该顺手写一行"


def test_invalid_module_is_refused_and_nothing_is_written(ctx: AppContext) -> None:
    """`module` 不在白名单 → `-32602`，且**一个字节都不写**（不留"看着绑了、其实用不了"的行）。"""
    for bad in ("ghost", "", "RESEARCH", "research,kb"):
        with pytest.raises(RpcError) as info:
            _set(ctx, bad)
        assert info.value.code == -32602
        assert "module" in str(info.value)
    assert _rows(ctx) == 0, "被拒的写入不许落库"
    assert _get(ctx) == {"module": None}


def test_rebinding_overwrites_the_single_row(ctx: AppContext) -> None:
    """换绑 = **覆盖**（主键 `(channel, chat_id)`）：一个群永远只有一行，不追加历史。"""
    _set(ctx, "research")
    _set(ctx, "kb")
    assert _get(ctx) == {"module": "kb"}
    assert _rows(ctx) == 1, "换绑追加了历史行 —— 之后没人知道这个群到底用哪个"
    assert _resolve(_with_kb(ctx, on=True))["module"] == "kb"


def test_default_is_a_storable_sentinel_not_null(ctx: AppContext) -> None:
    """`module="default"` 与"从没绑过"**语义等价**，但能写进表里（显式表达"跟全局默认"）。"""
    assert _set(ctx, "default")["ok"] is True
    assert _get(ctx) == {"module": "default"}
    resolved = _resolve(ctx)
    assert resolved["source"] == "default"
    assert resolved["toolsets"] == list(ctx.toolsets)


def test_platform_and_channel_arguments_are_checked_not_guessed(ctx: AppContext) -> None:
    """`platform` 是 `channel` 的别名；两个都传且不一致 → 报错（猜错就是绑到别的群上）。"""
    _set(ctx, "research")  # 走 channel
    alias = dispatch(ctx, "channel.get_module", {"platform": "feishu", "chat_id": CHAT})
    assert alias == {"module": "research"}

    with pytest.raises(RpcError) as info:
        dispatch(
            ctx,
            "channel.set_module",
            {"channel": "feishu", "platform": "wechat", "chat_id": CHAT, "module": "kb"},
        )
    assert info.value.code == -32602
    assert "不一致" in str(info.value)


# ------------------------------------------------------------------ 解析：绑定 → 全局默认 → 兜底


def test_binding_wins_over_the_global_default(ctx: AppContext) -> None:
    """绑了就用绑的：`toolsets` 只含该模块，`source="binding"` 说明是哪一级决定的。"""
    _with_kb(ctx, on=True)
    _set(ctx, "kb")
    resolved = _resolve(_with_kb(ctx, on=True))
    assert resolved["module"] == "kb"
    assert resolved["toolsets"] == ["kb"]
    assert resolved["source"] == "binding"
    assert resolved["binding"] == "kb"
    assert resolved["warning"] is None


def test_unbound_chat_uses_the_global_default(ctx: AppContext) -> None:
    """没绑 → 全局默认（`ctx.toolsets`，进程启动时的 `--toolsets`），`source="default"`。"""
    resolved = _resolve(ctx)
    assert resolved["source"] == "default"
    assert resolved["toolsets"] == list(ctx.toolsets) == ["research"]
    assert resolved["module"] == "research"
    assert resolved["binding"] is None
    assert resolved["warning"] is None

    # 绑定是**按群**的：给 A 绑了，B 仍然走全局默认
    _set(ctx, "research", chat_id=OTHER)
    _set(_with_kb(ctx, on=True), "kb")
    assert _resolve(_with_kb(ctx, on=True))["source"] == "binding"
    assert _resolve(_with_kb(ctx, on=True), chat_id="oc_never_bound")["source"] == "default"


def test_binding_to_a_module_that_vanished_falls_back_with_a_warning(
    ctx: AppContext, caplog: pytest.LogCaptureFixture
) -> None:
    """**绑定失效 → 回退默认 + warning，不崩**（本文件最重要的一条）。

    `kb` 在 `features.enable_kb` 关着时**不存在**（`runtime/registry.enabled_toolsets` 会摘掉它）。
    先开着开关把群绑到 `kb`，再把开关关掉 —— 这正是用户手上有过的状态（绑过之后再关开关）。
    期望：照常给出全局默认的工具集、把"为什么没用绑定"写进 `warning` 且落一条日志，
    **绝不抛异常**（抛了这条群消息就丢了）。
    """
    _set(_with_kb(ctx, on=True), "kb")
    assert _resolve(_with_kb(ctx, on=True))["source"] == "binding"

    with caplog.at_level(logging.WARNING, logger="scholarscout.rpc.channel"):
        resolved = _resolve(ctx)  # 开关已关：kb 不在可用模块里
    assert resolved["source"] == "default"
    assert resolved["toolsets"] == ["research"], "回退后的工具集应是全局默认，不是失效的绑定"
    assert resolved["module"] == "research"
    assert resolved["binding"] == "kb", '回包里要如实说"这个群原本绑的是 kb"'
    assert resolved["warning"] is not None and "kb" in str(resolved["warning"])
    assert any(
        record.levelno == logging.WARNING and "绑定失效" in record.getMessage()
        for record in caplog.records
    ), '绑定失效必须留下 warning 日志（否则线上只剩"这个群行为不对"）'
    # 失效的绑定**留在库里**：开关再打开就该立刻恢复
    assert _get(ctx) == {"module": "kb"}
    assert _resolve(_with_kb(ctx, on=True))["source"] == "binding"


def test_dirty_binding_value_does_not_crash_the_turn(
    ctx: AppContext, caplog: pytest.LogCaptureFixture
) -> None:
    """库里是**脏值**（绕过 `set_module` 写进去的）同样只回退、不崩 —— 解析口不信任库里的历史。"""
    ctx.db.execute(
        "INSERT INTO channel_modules (channel, chat_id, module, updated_at)"
        " VALUES ('feishu', ?, 'ghost_module', '2026-10-01T00:00:00.000000+08:00')",
        (CHAT,),
    )
    with caplog.at_level(logging.WARNING, logger="scholarscout.rpc.channel"):
        resolved = _resolve(ctx)
    assert resolved["source"] == "default"
    assert resolved["toolsets"] == ["research"]
    assert "ghost_module" in str(resolved["warning"])
    assert any("绑定失效" in record.getMessage() for record in caplog.records)


def test_empty_toolsets_falls_back_to_research(ctx: AppContext) -> None:
    """第三级兜底：连全局默认都是空的，`enabled_toolsets` 回 `("research",)`，`source="fallback"`。"""
    resolved = _resolve(replace(ctx, toolsets=()))
    assert resolved["source"] == "fallback"
    assert resolved["toolsets"] == ["research"]
    assert resolved["module"] == "research"


def test_module_methods_are_registered() -> None:
    """三条都要在注册表里（漏一条 = 文档写了、界面调不到）。"""
    registered = set(method_names())
    assert {
        "channel.set_module",
        "channel.get_module",
        channel_rpc.MODULE_FOR_CHAT_METHOD,
    } <= registered


def test_module_store_rejects_blank_identifiers(ctx: AppContext) -> None:
    """仓储层同样失败即报：`chat_id` 空白会写出"哪个群都不是"的孤儿行。"""
    with pytest.raises(ValueError):
        module_store.get_module(ctx.db, "feishu", "   ")
    with pytest.raises(ValueError):
        module_store.set_module(ctx.db, "feishu", CHAT, "")
    assert _rows(ctx) == 0
