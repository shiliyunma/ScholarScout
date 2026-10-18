"""第 7/8 步：跑完提醒（docs/20 §6.3 第 (2) 条）与每日封顶（docs/20 §5.1/§5.2）。

**全桩**：不真调模型、不真出网（飞书那一层用记录的假 `HttpClient` 顶上）。
这里守的四条，都是"能悄悄坏掉、而门禁看不见"的性质：

1. **默认关**：开关关着时连收件人都不解析 —— "关着时一次外网都不发"是可断言的事实，不是口号；
2. **只私聊**：发送器签名里**没有收件人参数**（目标在构造时闭包成用户 `open_id`），
   实际请求必须带 `receive_id_type=open_id`，且 `receive_id` 就是那个 `ou_...`；
3. **失败不影响轮次**：发送器抛异常时，这一轮的 `stop_reason` / 正文一字不变；
4. **默认无上限**：没配 `daily_*_budget` 时守卫为 `None`（不查库、不判断），轮次行为与引入前一致。

末尾两条是**回归断言**（默认关 / 默认无上限）：引入这两个能力不许改变默认行为。
"""

from __future__ import annotations

import inspect
import json
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from scholarscout.config import RuntimeProfile
from scholarscout.fetch import feishu_send
from scholarscout.fetch.http import FetchResult
from scholarscout.rpc.errors import RpcError
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.rpc.registry import AppContext as AppContextClass
from scholarscout.runtime import budget as daily_budget
from scholarscout.runtime import notify, registry
from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.session_runtime import SessionRuntime
from scholarscout.state import sessions

from ..conftest import reply, tool_call

ECHO_TOOL = "research.echo"
ECHO_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    registry.clear_registry()
    yield
    registry.clear_registry()


# --------------------------------------------------------------------- 桩与工具


def _register_echo() -> None:
    def handler(tool_ctx: Any, query: str) -> dict[str, Any]:
        del tool_ctx
        return {"ok": True, "data": {"query": query}, "refs": {}}

    registry.register(
        ToolSpec(
            name=ECHO_TOOL,
            description="回显查询",
            parameters=ECHO_SCHEMA,
            handler=handler,
            toolset="research",
        )
    )


def _new_session(ctx: AppContext, title: str = "A 会话") -> str:
    session_id = sessions.create_session(ctx.db, model=ctx.config.chat.model)
    sessions.rename_session(ctx.db, session_id, title)
    return session_id


def _with_runtime(ctx: AppContext, **kwargs: Any) -> None:
    """把测试配置的 `runtime` 段换成指定值（`Config` 是 frozen dataclass，只能 replace）。"""
    ctx.config = replace(ctx.config, runtime=replace(ctx.config.runtime, **kwargs))


class _RecordingHttp:
    """记录每一次 POST 的假出网客户端（只认两条：取 token、发消息）。"""

    def __init__(self, message_id: str = "om_test") -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._message_id = message_id

    def post_json(self, url: str, payload: dict[str, Any], **_kwargs: Any) -> FetchResult:
        self.calls.append((url, dict(payload)))
        if "tenant_access_token" in url:
            body: dict[str, Any] = {"code": 0, "tenant_access_token": "t-123", "expire": 7200}
        else:
            body = {"code": 0, "msg": "success", "data": {"message_id": self._message_id}}
        return FetchResult(
            url=url,
            status=200,
            content=json.dumps(body).encode("utf-8"),
            media_type="application/json",
        )


def _seed_usage(db: Any, *, prompt: int, completion: int, est_cost: float | None = None) -> str:
    """往 `usage_records` 插一条**刚发生**的用量（滚动 24h 窗口内）。"""
    usage_id = sessions.new_id("usg")
    db.execute(
        "INSERT INTO usage_records (id, session_id, turn_id, model, purpose, prompt_tokens,"
        " completion_tokens, est_cost, created_at)"
        " VALUES (?, NULL, NULL, 'stub-model', 'chat', ?, ?, ?, ?)",
        (
            usage_id,
            prompt,
            completion,
            est_cost,
            datetime.now(UTC).astimezone().isoformat(timespec="microseconds"),
        ),
    )
    return usage_id


# --------------------------------------------------------------------- 提醒（5 条）


def test_disabled_never_resolves_a_recipient_and_never_sends() -> None:
    """开关关着 → `disabled`，**连收件人都不解析**（"零外网"因此可断言）。"""
    resolved: list[str] = []
    sent: list[str] = []

    def resolver() -> notify.Sender:
        resolved.append("resolved")
        return lambda text: sent.append(text) or {"ok": True}

    notifier = notify.TurnDoneNotifier(enabled=False, resolve_sender=resolver)
    outcome = notifier.notify(
        turn_id="trn_off", session_id="ses_a", active_session_id="ses_b", text="A 的正文"
    )

    assert notifier.enabled is False
    assert outcome.fired is False
    assert outcome.reason == "disabled"
    assert resolved == [], "关着时不该解析收件人（解析要查配对表）"
    assert sent == [], "关着时一条都不该发"


def test_enabled_private_chat_is_the_only_channel() -> None:
    """开着 + 非当前会话 → 发**一条**，且走的是 `receive_id_type=open_id` 的私聊通道。

    这里故意把整条链路都跑真（`build_private_sender` → `send_text` → 假 HttpClient），
    因为"只私聊"是**机制**保证（签名里没有收件人 + 目标闭包成 open_id），不是调用点自律。
    """
    http = _RecordingHttp(message_id="om_private")
    sender = feishu_send.build_private_sender(
        http, open_id="ou_owner", app_id="cli_test", app_secret="secret_test"
    )
    assert sender is not None
    # 签名里**没有收件人参数** → 调用方在类型上就没法把它发到别处（更发不到群）
    assert list(inspect.signature(sender).parameters) == ["text"]

    notifier = notify.TurnDoneNotifier(
        enabled=True, resolve_sender=lambda: sender, min_interval_s=0.0
    )
    outcome = notifier.notify(
        turn_id="trn_p1",
        session_id="ses_a",
        title="A 会话",
        text="A 的结论在第一行\n第二行不该进摘要",
        stop_reason="completed",
        duration_s=12,
        active_session_id="ses_b",
    )

    assert outcome.fired is True, outcome.reason
    assert outcome.reason == "sent"
    send_url, send_body = http.calls[-1]
    assert "receive_id_type=open_id" in send_url, "私聊必须用 open_id 级别的收件人类型"
    assert send_body["receive_id"] == "ou_owner", "收件人只能是构造时钉死的那个 open_id"
    text = json.loads(send_body["content"])["text"]
    assert "A 会话" in text and "A 的结论在第一行" in text
    assert "第二行不该进摘要" not in text, "摘要只取首行（不是整段截断）"
    assert "completed" in text


def test_notify_is_skipped_for_the_session_the_user_is_looking_at() -> None:
    """当前会话**不提醒**（提醒是给"你没在看的那个会话"用的）。

    同时钉住"运行时不许猜"：没上报活跃会话 → 一条都不发。
    """
    sent: list[str] = []
    notifier = notify.TurnDoneNotifier(
        enabled=True, resolve_sender=lambda: lambda text: sent.append(text) or {"ok": True}
    )

    active = notifier.notify(
        turn_id="trn_act", session_id="ses_a", text="x", active_session_id="ses_a"
    )
    unknown = notifier.notify(turn_id="trn_unknown", session_id="ses_a", text="x")

    assert (active.fired, active.reason) == (False, "is_active_session")
    assert (unknown.fired, unknown.reason) == (False, "no_active_session")
    assert sent == []


def test_a_failing_sender_does_not_change_the_turn(ctx: AppContext) -> None:
    """发送失败只记日志：**这一轮照样 `completed`、正文一字不变**，而且提醒真的被调到了。

    这条同时是"`_finish` 里真调了 notifier"的证据 —— 不调的话 `recorded` 会是空的。
    """
    recorded: list[str] = []

    def exploding_sender(text: str) -> dict[str, Any]:
        recorded.append(text)
        raise RuntimeError("模拟：取 token 时网络断了")

    notifier = notify.TurnDoneNotifier(
        enabled=True, resolve_sender=lambda: exploding_sender, min_interval_s=0.0
    )
    ctx.llm = type("_One", (), {"chat": lambda self, messages, tools=None: reply("A 的正文-1")})()
    session_id = _new_session(ctx)
    runtime = SessionRuntime(
        ctx, max_concurrent=2, notifier=notifier, thread_prefix="probe-notify-fail"
    )
    try:
        accepted = runtime.submit_turn(
            session_id=session_id,
            text="A 的问题",
            toolsets=["research"],
            emit=None,
            active_session_id="ses_other",
        )
        result = runtime.wait(str(accepted["turn_id"]), timeout=10)

        assert result is not None
        assert result.stop_reason == "completed"
        assert result.text == "A 的正文-1"
        # `_finish` 是**先** `done_event.set()`、**后**发提醒（别让一次私聊推送拖慢"这一轮已结束"），
        # 所以 `wait()` 返回时提醒可能还没跑到 —— 轮询等它落下，不用 sleep 赌时序（断言意图不变：恰好 1 次）。
        deadline = time.monotonic() + 2.0
        while not recorded and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(recorded) == 1, "提醒必须被真的调到（否则这条用例测不到失败隔离）"
        assert "A 的正文-1" in recorded[0]
        rows = ctx.db.query("SELECT content FROM messages WHERE session_id = ?", (session_id,))
        assert any("A 的正文-1" in str(row["content"]) for row in rows), "正文照常落库"
    finally:
        runtime.shutdown()


def test_the_same_turn_is_never_notified_twice_and_is_rate_limited() -> None:
    """同一 `turn_id` 只发一次；不同 turn 之间受 60s 限频（并发收尾不刷屏）。"""
    sent: list[str] = []
    clock = {"now": 1000.0}
    notifier = notify.TurnDoneNotifier(
        enabled=True,
        resolve_sender=lambda: lambda text: sent.append(text) or {"ok": True},
        min_interval_s=60.0,
        clock=lambda: clock["now"],
    )

    first = notifier.notify(
        turn_id="trn_1", session_id="ses_a", text="一", active_session_id="ses_b"
    )
    duplicate = notifier.notify(
        turn_id="trn_1", session_id="ses_a", text="一", active_session_id="ses_b"
    )
    clock["now"] = 1030.0
    limited = notifier.notify(
        turn_id="trn_2", session_id="ses_a", text="二", active_session_id="ses_b"
    )
    clock["now"] = 1061.0
    later = notifier.notify(
        turn_id="trn_3", session_id="ses_a", text="三", active_session_id="ses_b"
    )

    assert (first.fired, first.reason) == (True, "sent")
    assert (duplicate.fired, duplicate.reason) == (False, "duplicate")
    assert (limited.fired, limited.reason) == (False, "rate_limited")
    assert (later.fired, later.reason) == (True, "sent")
    assert len(sent) == 2, "同 turn 一次 + 限频后的一条 = 两次投递"


# --------------------------------------------------------------------- 预算（3 条）


def test_a_tiny_daily_budget_stops_the_turn_with_budget_stop_reason(ctx: AppContext) -> None:
    """极小上限 → 本轮收口 `stop_reason="budget"`，而且**收口那一次仍给了正文**（不丢结论）。"""
    _with_runtime(ctx, daily_token_budget=1)
    _register_echo()
    ctx.llm = type(
        "_Scripted",
        (),
        {
            "chat": lambda self, messages, tools=None: (
                reply("", tool_calls=[tool_call(ECHO_TOOL, '{"query":"x"}')])
                if tools
                else reply("已到上限的收口正文")
            )
        },
    )()
    session_id = _new_session(ctx)
    runtime = SessionRuntime(ctx, max_concurrent=2, thread_prefix="probe-budget-stop")
    try:
        accepted = runtime.submit_turn(
            session_id=session_id, text="A 的问题", toolsets=["research"], emit=None
        )
        assert accepted["ok"] is True, accepted
        result = runtime.wait(str(accepted["turn_id"]), timeout=10)

        assert result is not None
        assert result.stop_reason == daily_budget.STOP_REASON == "budget"
        assert result.text == "已到上限的收口正文"
        # 记账照常（封顶不改账本口径）
        assert daily_budget.usage_since(ctx.db, daily_budget.window_start())["calls"] >= 1
    finally:
        runtime.shutdown()


def test_a_new_turn_is_refused_once_the_daily_budget_is_spent(ctx: AppContext) -> None:
    """先把窗口内的用量花超 → 新轮次**直接拒绝**，且**一个字都不落库**。

    拒绝载荷要如实带上 `budget` 快照（`chat.status.global_budget` 是同一个形状），
    界面才有东西可显示 —— 不能只回一句"不行"。
    """
    _with_runtime(ctx, daily_token_budget=100)
    _seed_usage(ctx.db, prompt=400, completion=100)
    session_id = _new_session(ctx)
    runtime = SessionRuntime(ctx, max_concurrent=2, thread_prefix="probe-budget-refuse")
    try:
        rejected = runtime.submit_turn(
            session_id=session_id, text="A 的问题", toolsets=["research"], emit=None
        )

        assert rejected["ok"] is False
        assert rejected["reason"] == daily_budget.REJECT_REASON == "daily_budget_exceeded"
        assert rejected["budget"]["exceeded"] is True
        assert rejected["budget"]["reason"] == "daily_token_budget"
        assert rejected["budget"]["tokens"] == {
            "used": 500,
            "limit": 100,
            "remaining": 0,
            "prompt": 400,
            "completion": 100,
        }
        assert rejected["running"] == []
        rows = ctx.db.query("SELECT id FROM messages WHERE session_id = ?", (session_id,))
        assert rows == [], "被拒绝的轮次不该留下任何消息"

        # 同一个快照也并进了 `chat.status`（`global_budget`）—— 不另算一套口径
        global_budget = runtime.status()["global_budget"]
        assert global_budget["enabled"] is True
        assert global_budget["tokens"]["used"] == 500
        assert global_budget["tokens"]["limit"] == 100
        assert global_budget["reason"] == "daily_token_budget"
    finally:
        runtime.shutdown()


def test_without_a_budget_configured_nothing_changes(ctx: AppContext) -> None:
    """**不配上限**（null）→ 守卫为 `None`，轮次与观测口径都和引入前一致，且能连开两轮。"""
    assert daily_budget.from_runtime(ctx.config.runtime) is None
    ctx.llm = type("_Two", (), {"chat": lambda self, messages, tools=None: reply("普通正文")})()
    session_id = _new_session(ctx)
    runtime = SessionRuntime(ctx, max_concurrent=2, thread_prefix="probe-budget-off")
    try:
        for text in ("第一问", "第二问"):
            accepted = runtime.submit_turn(
                session_id=session_id, text=text, toolsets=["research"], emit=None
            )
            assert accepted["ok"] is True, "没配上限时不该有任何拒绝"
            result = runtime.wait(str(accepted["turn_id"]), timeout=10)
            assert result is not None
            assert result.stop_reason == "completed"

        global_budget = runtime.status()["global_budget"]
        assert global_budget["enabled"] is False
        # 未启用时报 None 而不是 0 —— 0 会被读成"没花钱"
        assert global_budget["tokens"] == {"used": None, "limit": None, "remaining": None}
        assert global_budget["cost"]["used"] is None
        assert global_budget["exceeded"] is False
    finally:
        runtime.shutdown()


# --------------------------------------------------------------------- 接线（1 条）


def test_chat_send_forwards_active_session_id_to_the_runtime(
    ctx: AppContext, monkeypatch: Any
) -> None:
    """`chat.send` 必须把界面报上来的 `active_session_id` **透传**下去（运行时不许猜）。"""
    session_id = _new_session(ctx, "A")
    active = _new_session(ctx, "B")
    seen: dict[str, Any] = {}

    class _FakeRuntime:
        def submit_turn(self, **kwargs: Any) -> dict[str, Any]:
            seen.update(kwargs)
            return {"ok": False, "reason": "turn_in_progress", "turn_id": "trn_x"}

    monkeypatch.setattr(AppContextClass, "session_runtime", lambda self: _FakeRuntime())
    with pytest.raises(RpcError):
        dispatch(
            ctx,
            "chat.send",
            {
                "session_id": session_id,
                "text": "A 的问题",
                "mode": "async",
                "active_session_id": active,
            },
        )
    assert seen["active_session_id"] == active


# --------------------------------------------------------------------- 两条回归断言


def test_regression_turn_done_notify_is_off_by_default(ctx: AppContext) -> None:
    """回归断言 1：**默认关** —— 默认配置值 + `from_config` 都不造提醒器（零成本、零外网）。"""
    assert RuntimeProfile().notify_turn_done is False
    assert RuntimeProfile().notify_turn_done_platform == notify.DEFAULT_PLATFORM == "feishu"
    assert notify.from_config(RuntimeProfile(), db=ctx.db, http=None) is None


def test_regression_daily_budget_is_unlimited_by_default(ctx: AppContext) -> None:
    """回归断言 2：**默认无上限** —— 两个键都是 None、守卫不介入、快照 enabled=False。"""
    assert RuntimeProfile().daily_token_budget is None
    assert RuntimeProfile().daily_cost_budget is None
    assert daily_budget.from_runtime(RuntimeProfile()) is None
    assert daily_budget.snapshot(RuntimeProfile(), ctx.db)["enabled"] is False
