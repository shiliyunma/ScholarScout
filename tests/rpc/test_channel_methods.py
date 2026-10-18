"""`channel.*` RPC 契约（docs/21 §5.1 的签名 + §6 的密钥保管口径）。

为什么单独一套：`channel.*` 是把 platform 提成参数后的**唯一门面**，界面（渠道卡片）只经它说话。
下面七条每条都对应一种"界面上看起来成功了、其实是假的"的失败：

1. `set_credentials` 的回包里**没有任何密钥值或片段**（只有键名 / 布尔，连尾号都不给）—— 回读了
   就等于把密钥交给了 pywebview 的 JS 层，密钥会进截图、进日志、进崩溃转储；
2. 白名单之外的键**报错**而不是静默忽略（静默忽略 = 用户以为改了、其实没改）；
3. 传空串 = **清除**该键（`.env` 里连键名一起删掉，留一个 `KEY=` 会让人以为还配着什么）；
4. `stop` 没在跑 → `{ok: false, reason: "not_running"}`（照 `chat.cancel` 的既有铁律，不假装成功）；
5. `start` **幂等**：已在跑就回 `already_running`，绝不起第二个实例（两个子进程 = 两个长连接）；
6. `feishu.*` 别名与 `channel.*` **返回一致**（别名不是抄一份，抄一份就是两套行为）；
7. `channel.state` 已登记进 `NOTIFICATION_METHODS`（漏登记会被加上 `chat.` 前缀，界面永远收不到）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from scholarscout import paths
from scholarscout.channels import supervisor
from scholarscout.rpc.errors import RpcError
from scholarscout.rpc.methods import AppContext, dispatch, method_names
from scholarscout.rpc.server import NOTIFICATION_METHODS

from ..conftest import redirect_home

_ENV_KEYS = ("FEISHU_APP_ID", "FEISHU_APP_SECRET")
#: 故意做成"一眼能在回包里搜到"的密钥：尾 4 位是 `3f9c`（回包**不该**出现它，含尾号）
APP_ID = "cli_a1b2c3d4e5f6"
SECRET = "sec_SUPER_SECRET_VALUE_3f9c"

#: docs/21 §5.1 里本轮落地的十条 `channel.*`（含 docs/17 §3.12 的发文件）
CHANNEL_METHODS = frozenset(
    {
        "channel.list",
        "channel.status",
        "channel.guide",
        "channel.set_credentials",
        "channel.test",
        "channel.send",
        # 发文件（生成物 / 已入库资料 / 项目内路径）：渠道层规则识别后交给它
        "channel.send_file",
        "channel.start",
        "channel.stop",
        # 子进程专用（父进程是状态快照的唯一写者）：子进程只上报，落盘在父进程
        "channel.report_state",
    }
)


def _dotenv() -> Path:
    return paths.home() / ".env"


def _render(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """`.env` 重定向到 tmp；进出都清干净渠道层环境变量（RPC 会直接写 `os.environ`）。"""
    redirect_home(monkeypatch, tmp_path)
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    for key in _ENV_KEYS:
        os.environ.pop(key, None)


def _configure(ctx: AppContext, *, app_id: str = APP_ID, secret: str = SECRET) -> dict[str, object]:
    """把凭据写进 `.env`（走产品自己的 RPC，不直接碰文件）。"""
    fields: dict[str, str] = {}
    if app_id:
        fields["app_id"] = app_id
    if secret:
        fields["app_secret"] = secret
    return dispatch(ctx, "channel.set_credentials", {"platform": "feishu", "fields": fields})


# ------------------------------------------------------------------ 1. 只写不回读


def test_list_and_status_never_carry_a_secret_value(ctx: AppContext) -> None:
    """两张卡片的取数口都搜不到密钥（**连尾号也不给**）；`list`/`status` 都只给布尔（docs/21 §5.1）。"""
    _configure(ctx)
    listing = dispatch(ctx, "channel.list", {})
    assert SECRET not in _render(listing)
    rows = listing["channels"]
    assert [row["platform"] for row in rows] == ["feishu"]
    assert rows[0]["configured"] is True and rows[0]["enabled"] is False
    assert rows[0]["state"] == "stopped"
    assert "credentials" not in rows[0], "列表口**不该**带凭据视图（只要布尔）"

    status = dispatch(ctx, "channel.status", {"platform": "feishu"})
    dumped = _render(status)
    assert SECRET not in dumped, "channel.status 的回包里带上了密钥值"
    assert "3f9c" not in dumped, "channel.status 回了密钥尾号 —— 尾号也是值的一部分（docs/01 §3.7）"
    assert status["credentials"] == {"app_id_set": True, "secret_set": True}


def test_credentials_view_is_exactly_two_booleans(ctx: AppContext) -> None:
    _configure(ctx)
    status = dispatch(ctx, "channel.status", {"platform": "feishu"})
    assert status["configured"] is True
    assert status["credentials"] == {"app_id_set": True, "secret_set": True}
    assert "seq" not in _render(status["credentials"])  # 只有两个布尔，没有别的字段


def test_set_credentials_reply_has_no_secret_value(ctx: AppContext) -> None:
    """**回包无密钥**：`written` 是键名、`configured` 只有布尔，输入值（含尾号）一个都不回。"""
    result = dispatch(
        ctx,
        "channel.set_credentials",
        {"platform": "feishu", "fields": {"app_id": APP_ID, "app_secret": SECRET}},
    )
    dumped = _render(result)
    assert SECRET not in dumped, "回包里出现了 App Secret 的值"
    assert APP_ID not in dumped, "回包里出现了 App ID 的值（`status` 里也只有布尔，输入值不必回读）"
    assert result["written"] == ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]
    assert result["cleared"] == []
    assert result["configured"] == {"app_id_set": True, "secret_set": True}
    # 但**真的写进 `.env` 了**（不能为了"不泄漏"干脆不写）
    text = _dotenv().read_text(encoding="utf-8")
    assert f"FEISHU_APP_SECRET={SECRET}" in text
    assert "3f9c" in text


def test_short_secret_is_not_returned_at_all(ctx: AppContext) -> None:
    """短密钥同样一个字符都不回（历史上给过 `****` 占位，现在连占位都不要）。"""
    result = _configure(ctx, secret="abc")
    assert result["configured"] == {"app_id_set": True, "secret_set": True}
    assert "abc" not in _render(result)


def test_no_channel_reply_contains_any_secret_fragment(ctx: AppContext) -> None:
    """**回包不含密钥任何片段**：把整个长密钥按 4 字符滑窗切开，逐个窗口都不许出现在回包里。

    （尾号只是其中一个窗口；这条把"半个密钥"也算进去 —— 泄漏不需要是全文。）
    """
    _configure(ctx)
    payloads = {
        "list": dispatch(ctx, "channel.list", {}),
        "status": dispatch(ctx, "channel.status", {"platform": "feishu"}),
        "guide": dispatch(ctx, "channel.guide", {"platform": "feishu"}),
        "set": _configure(ctx),
    }
    fragments = {SECRET[i : i + 4] for i in range(0, len(SECRET) - 3)}
    for name, payload in payloads.items():
        dumped = _render(payload)
        # 键名（`FEISHU_APP_SECRET`）里本身含 `SECR` 这类窗口，先摘掉键名再查值
        for key in _ENV_KEYS:
            dumped = dumped.replace(key, "")
        for fragment in fragments:
            assert fragment not in dumped, f"{name} 的回包里出现了密钥片段 {fragment!r}"


# ------------------------------------------------------------------ 2. 白名单之外报错


def test_unknown_credential_key_is_refused_not_ignored(ctx: AppContext) -> None:
    with pytest.raises(RpcError) as info:
        dispatch(
            ctx,
            "channel.set_credentials",
            {"platform": "feishu", "fields": {"app_id": APP_ID, "bot_token": "t-x"}},
        )
    assert info.value.code == -32602
    assert "bot_token" in str(info.value), "报错要说清**哪个键**不认识"
    assert not _dotenv().is_file(), "整批被拒时一个键都不许写（哪怕里面有合法的 app_id）"


def test_empty_fields_is_refused(ctx: AppContext) -> None:
    with pytest.raises(RpcError) as info:
        dispatch(ctx, "channel.set_credentials", {"platform": "feishu", "fields": {}})
    assert info.value.code == -32602


def test_unknown_platform_is_refused(ctx: AppContext) -> None:
    """本轮只做飞书：传别的平台**明确报错**，不假装支持（微信/QQ 见 docs/21 §8）。"""
    for method, params in (
        ("channel.status", {"platform": "wechat"}),
        ("channel.start", {"platform": "qq"}),
        ("channel.set_credentials", {"platform": "wechat", "fields": {"app_id": "x"}}),
    ):
        with pytest.raises(RpcError) as info:
            dispatch(ctx, method, params)
        assert info.value.code == -32602
        assert "feishu" in str(info.value)


# ------------------------------------------------------------------ 3. 空串 = 清除


def test_empty_string_clears_the_key(ctx: AppContext) -> None:
    _configure(ctx)
    assert "FEISHU_APP_SECRET" in _dotenv().read_text(encoding="utf-8")
    result = dispatch(
        ctx, "channel.set_credentials", {"platform": "feishu", "fields": {"app_secret": ""}}
    )
    assert result["cleared"] == ["FEISHU_APP_SECRET"]
    assert result["written"] == []
    assert result["configured"] == {"app_id_set": True, "secret_set": False}
    text = _dotenv().read_text(encoding="utf-8")
    assert "FEISHU_APP_SECRET" not in text, "清除要**删行**，留一个 KEY= 会让人以为还配着"
    assert "FEISHU_APP_ID" in text, "只清一个键时不许顺手清掉别的"
    assert os.environ.get("FEISHU_APP_SECRET") is None, "进程环境也要一起摘掉"


def test_clearing_both_leaves_configured_false(ctx: AppContext) -> None:
    _configure(ctx)
    result = dispatch(
        ctx,
        "channel.set_credentials",
        {"platform": "feishu", "fields": {"app_id": "", "app_secret": ""}},
    )
    assert sorted(result["cleared"]) == ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]
    assert dispatch(ctx, "channel.status", {"platform": "feishu"})["configured"] is False


# ------------------------------------------------------------------ 4/5. 起停语义


def test_stop_when_not_running_does_not_pretend_success(ctx: AppContext) -> None:
    result = dispatch(ctx, "channel.stop", {"platform": "feishu"})
    assert result == {
        "ok": False,
        "state": "stopped",
        "platform": "feishu",
        "reason": "not_running",
    }, "没在跑时 stop 必须如实回 not_running（照 chat.cancel 的铁律）"


def test_start_is_idempotent_and_never_spawns_twice(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已在跑 → `already_running`。**连 subprocess 都不许碰**（碰了就有可能起第二个实例）。"""
    monkeypatch.setattr(supervisor, "live_pid", lambda platform, root=None: 4242)

    class _NoSpawn:
        def __getattr__(self, name: str) -> object:  # pragma: no cover - 触发即失败
            raise AssertionError(f"已经在跑，不该碰 subprocess.{name}")

    monkeypatch.setattr(supervisor, "subprocess", _NoSpawn())
    first = dispatch(ctx, "channel.start", {"platform": "feishu"})
    second = dispatch(ctx, "channel.start", {"platform": "feishu"})
    assert first == second
    assert first["ok"] is True
    assert first["state"] == "running" and first["note"] == "already_running"
    assert first["pid"] == 4242


def test_status_reflects_the_child_state_snapshot(ctx: AppContext) -> None:
    """子进程报的错误要能一路读到界面（否则界面永远是"一切正常"）。"""
    supervisor.write_state(
        "feishu",
        {
            "state": "error",
            "connected": False,
            "reconnects": 2,
            "last_error": "40034105 主动消息无权限",
        },
    )
    status = dispatch(ctx, "channel.status", {"platform": "feishu"})
    assert status["state"] == "error"
    assert "40034105" in str(status["last_error"])
    assert status["reconnects"] == 2
    assert status["connected"] is False


# ------------------------------------------------------------------ 6. 别名一致


def test_feishu_aliases_return_exactly_what_channel_does(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`feishu.*` 是别名，不是第二套实现：同一份入参必须回同一份结果。

    `start`/`stop` 一律**打桩成"已经在跑的假进程"**：真跑一条就会 spawn 真子进程、
    或对不存在的 pid 打真的 `taskkill` —— 契约测试不该有这种副作用。
    """
    monkeypatch.setattr(supervisor, "live_pid", lambda platform, root=None: 4242)
    monkeypatch.setattr(supervisor, "pid_alive", lambda pid: False)
    monkeypatch.setattr(supervisor, "kill_pid", lambda pid: True)
    expected_note = "already_running"

    for alias, canonical, params in (
        ("feishu.guide", "channel.guide", {}),
        ("feishu.set_credentials", "channel.set_credentials", {"fields": {"app_id": APP_ID}}),
        ("feishu.start", "channel.start", {}),
        ("feishu.stop", "channel.stop", {}),
        ("feishu.test", "channel.test", {"mode": "probe"}),
    ):
        merged = {**params, "platform": "feishu"}
        via_alias = dispatch(ctx, alias, params)
        via_canonical = dispatch(ctx, canonical, merged)
        # 自检里的耗时是**真量出来的**，逐次会差几毫秒 —— 只对账这条之外的全部字段
        for payload in (via_alias, via_canonical):
            if isinstance(payload, dict):
                payload.pop("latency_ms", None)
        assert via_alias == via_canonical, f"{alias} 与 {canonical} 的行为不一致（别名抄了一份？）"
        if canonical == "channel.start":
            assert via_alias.get("note") == expected_note, "别名不该在「已在跑」时另起一个实例"


# ------------------------------------------------------------------ 7. 通知与登记


def test_channel_namespace_is_registered_and_state_is_a_notification() -> None:
    registered = set(method_names())
    assert registered >= CHANNEL_METHODS, f"少了这些方法：{sorted(CHANNEL_METHODS - registered)}"
    assert "channel.state" in NOTIFICATION_METHODS, (
        "channel.state 没登记进 NOTIFICATION_METHODS —— 它会被加上 chat. 前缀，界面永远收不到状态变化"
    )


def test_platform_limits_report_the_platform_hard_limits(ctx: AppContext) -> None:
    """平台硬限要如实报成机器可读的摘要（界面直接显示"哪条做不了"）。"""
    limits = dispatch(ctx, "channel.status", {"platform": "feishu"})["platform_limits"]
    assert limits["secret_reset_api"] is False
    assert limits["connection_start_api"] is True
    assert limits["connection_stop_api"] is False
    assert limits["callback_url_required"] is False
    assert limits["auto_create_app"] == "semi_automatic"
