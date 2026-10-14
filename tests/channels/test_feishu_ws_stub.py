"""飞书 **websocket 长连接**适配层（`channels/feishu_ws.py`）的桩验证：**不联网、无凭据、不装 SDK**。

覆盖两批断言：
  A. **链路**：桩 SDK 送来 `im.message.receive_v1` → 白名单拦截 → 跑一轮（替身 runtime/模型）
     → 出站发送器被调用（消息真落库、回复真交给发送器）；
  B. **拦截与健壮性**：未授权者**只收配对提示、不跑会话**；配对码经长连接登记；群聊 @提及 策略；
     非文本事件被忽略；一条坏消息**不打断链路**；SDK 缺装时给人话而不是崩。

为什么用桩 SDK：`import lark_oapi` 本机首次 **116 s**（见 `channels/feishu_ws.py` 顶注），
门禁里不能背这个成本；而长连接的真实边界只有三个名字（`ws.Client` / `EventDispatcherHandler` /
`LogLevel`，见 `feishu_ws.WsSdk`），桩掉它们即可把本模块测到底。
**真 SDK 的形状核对**单独做（`run/feishu_ws_sdk_probe.py`：真对象 → `event_to_payload` →
`parse_message_event`，以及用真 SDK 装配客户端但不 start），结果记在 `docs/17` §3.8。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from scholarscout.channels import feishu, feishu_ws
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state import pairing
from scholarscout.tools import register_tools

from ..conftest import StubLlm, redirect_home, reply

CHAT_ID = "oc_ws_chat_0001"
STUB_REPLY = "这是桩模型对长连接消息的回复"
SENDER = "ou_ws_user"
BOT_OPEN_ID = "ou_ws_bot"
BOT_NAME = "大浩的AI毕设"

_ENV_KEYS = (
    "FEISHU_DM_POLICY",
    "FEISHU_GROUP_POLICY",
    "FEISHU_BOT_OPEN_ID",
    "FEISHU_BOT_NAME",
    "FEISHU_CONNECTION_MODE",
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """`.env` 重定向到 tmp；清掉渠道层环境变量（拦截判定直接读 os.environ）。"""
    redirect_home(monkeypatch, tmp_path)
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    for key in _ENV_KEYS:
        os.environ.pop(key, None)


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_tools()


@pytest.fixture
def recorder() -> feishu.RecordingSender:
    return feishu.RecordingSender()


def _approve(ctx: AppContext, open_id: str = SENDER) -> None:
    pairing.approve_user(ctx.db, open_id, user_name="Stub")


# ------------------------------------------------------------------ 桩 SDK（三个名字）


class _StubHandler:
    """模拟 SDK 的 `EventDispatcherHandler` 实例：`do(event)` = SDK 收到事件后调回调。"""

    def __init__(self, registered: dict[str, Any]) -> None:
        self._registered = registered

    def do(self, event: Any) -> Any:
        return self._registered[feishu.RECEIVE_EVENT](event)


class _StubBuilder:
    """模拟 `EventDispatcherHandler.builder(...)`：只认我们注册的那一个事件。"""

    def __init__(self, owner: _StubEventHandlerCls) -> None:
        self._owner = owner

    def register_p2_im_message_receive_v1(self, fn: Any) -> _StubBuilder:
        self._owner.registered[feishu.RECEIVE_EVENT] = fn
        return self

    def build(self) -> _StubHandler:
        return _StubHandler(self._owner.registered)


class _StubEventHandlerCls:
    """模拟 `lark_oapi.EventDispatcherHandler`（类）：记住 builder 传进来的两个凭据。"""

    def __init__(self) -> None:
        self.builder_calls: list[tuple[str, str]] = []
        self.registered: dict[str, Any] = {}

    def builder(self, encrypt_key: str, verification_token: str) -> _StubBuilder:
        self.builder_calls.append((encrypt_key, verification_token))
        return _StubBuilder(self)


class _StubWsClient:
    """模拟 `lark_oapi.ws.Client`：记住装配参数；`start()` 把排队的事件逐个喂给回调。

    `block_on_start=True` 时 `start()` **不返回**（等 `stop()` 放开）—— 真 SDK 的 `start()` 就是
    一个不返回的收包循环，后台线程用例必须照这个形状才能测出"线程还活着"。
    """

    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self.app_id = kwargs.get("app_id")
        self.app_secret = kwargs.get("app_secret")
        self.domain = kwargs.get("domain")
        self.extra_ua_tags = kwargs.get("extra_ua_tags")
        self.log_level = kwargs.get("log_level")
        self.event_handler = kwargs.get("event_handler")
        self.events: list[Any] = []
        self.started = False
        #: `stop()` 被调过没（`FeishuWsClient.stop()` 会优先调 SDK 的 `stop`/`close`）
        self.stopped = False
        #: `start()` 返回了没 —— 后台线程用例靠它确认收包循环真的收尾了
        self.exited = threading.Event()
        #: 是否模拟"阻塞的收包循环"（默认不阻塞，同步用例才不会被吊住）
        self.block_on_start = False
        self._release = threading.Event()
        #: 测试钩子：`start()` **把事件都喂完之后**调它。后台线程用例靠它等结果，避免与断言竞态。
        self.on_done: Any = None

    def push(self, *events: Any) -> _StubWsClient:
        self.events.extend(events)
        return self

    def start(self) -> None:
        self.started = True
        for event in list(self.events):
            self.event_handler.do(event)
        self.events.clear()
        if self.on_done is not None:
            self.on_done()
        if self.block_on_start:
            self._release.wait(10)  # 真 SDK 在这里一直收包，直到连接断/进程退
        self.exited.set()

    def stop(self) -> None:
        self.stopped = True
        self._release.set()

    def close(self) -> None:
        self.stop()


class StubSdk:
    """桩 SDK：一次给全 `feishu_ws.WsSdk` 需要的三样东西 + 调用记录。

    真 SDK 的 client 是**装配时**才建的（`FeishuWsClient.build_client`），所以事件要先排队
    （`deliver`），装配那一刻再倒进 client 的队列里 —— 这样测试的写法与真链路一致：
    「先有事件，再 start」。
    """

    def __init__(self) -> None:
        self.event_handlers = _StubEventHandlerCls()
        self.clients: list[_StubWsClient] = []
        self.pending: list[Any] = []
        self.on_done: Any = None
        #: 建出来的桩 client 是否模拟"阻塞的收包循环"（后台线程用例设 True）
        self.block_on_start = False
        self.sdk = feishu_ws.WsSdk(
            client_cls=self._make_client, event_handler_cls=self.event_handler_cls, log_level="INFO"
        )

    #: 名字与真 SDK 对齐（`EventDispatcherHandler.builder` 是类方法式的调用）
    @property
    def event_handler_cls(self) -> _StubEventHandlerCls:
        return self.event_handlers

    def _make_client(self, **kwargs: Any) -> _StubWsClient:
        client = _StubWsClient(kwargs)
        client.on_done = self.on_done
        client.block_on_start = self.block_on_start
        if self.pending:
            client.push(*self.pending)
            self.pending.clear()
        self.clients.append(client)
        return client

    @property
    def client(self) -> _StubWsClient:
        assert self.clients, "SDK client 没被装配"
        return self.clients[-1]

    def deliver(self, *events: Any) -> None:
        """把事件排进队列（装配后 `start()` 时会喂给回调）。"""
        self.pending.extend(events)


def _ws_client(
    ctx: AppContext,
    recorder: feishu.RecordingSender,
    stub: StubSdk,
    *,
    dispatcher: Any = dispatch,
) -> feishu_ws.FeishuWsClient:
    return feishu_ws.build_client(ctx, sender=recorder, dispatcher=dispatcher, sdk=stub.sdk)


def _event(
    text: str,
    *,
    chat_id: str = CHAT_ID,
    chat_type: str = "",
    mentions: list[dict[str, Any]] | None = None,
    sender: str = SENDER,
    message_type: str = "text",
) -> dict[str, Any]:
    """一份合造的 `im.message.receive_v1` 事件（形状照真 SDK 反序列化后的报文）。"""
    message: dict[str, Any] = {
        "message_id": "om_ws_msg_0001",
        "chat_id": chat_id,
        "message_type": message_type,
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    if chat_type:
        message["chat_type"] = chat_type
    if mentions is not None:
        message["mentions"] = mentions
    return {
        "schema": "2.0",
        "header": {
            "event_id": "evt_ws_0001",
            "event_type": feishu.RECEIVE_EVENT,
            "create_time": "1700000000000",
        },
        "event": {
            "sender": {"sender_id": {"open_id": sender}, "sender_type": "user"},
            "message": message,
        },
    }


# ================================================================== A. 链路


def test_ws_event_runs_turn_and_sends_reply(ctx: AppContext, recorder, caplog) -> None:
    """收到事件 → 白名单放行 → 真跑一轮（桩模型）→ 出站发送器被调用。"""
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _approve(ctx)
    stub = StubSdk()
    client = _ws_client(ctx, recorder, stub)
    stub.deliver(_event("你好，帮我看下知识追踪入门"))

    lines: list[str] = []
    client.start(say=lines.append)  # 桩 client 的 start 同步喂事件（真 SDK 在这里阻塞）

    assert stub.client.started
    # 订阅的正是那条事件（长连接只订阅它）
    assert feishu.RECEIVE_EVENT in stub.event_handlers.registered
    # 装配参数：**UA 标签不能省**（省了飞书不推群 @ 事件），domain 用平台入口 URL
    assert stub.client.extra_ua_tags == ["channel"]
    assert stub.client.domain == "https://open.feishu.cn"
    assert stub.client.app_id == "" and stub.client.app_secret == ""  # 桩环境没配凭据
    assert any("websocket 长连接启动" in line for line in lines), lines

    # 消息进了会话路径：真落库
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions WHERE session_key = ?", (CHAT_ID,)) == 1
    session_id = ctx.db.query_one("SELECT id FROM sessions WHERE session_key = ?", (CHAT_ID,))["id"]
    users = ctx.db.query(
        "SELECT content FROM messages WHERE session_id = ? AND role = 'user'", (session_id,)
    )
    assert [r["content"] for r in users] == ["你好，帮我看下知识追踪入门"]
    # 回复交给了发送器（真实部署注入飞书 REST 发送；这里记下来）
    assert recorder.sent == [(CHAT_ID, STUB_REPLY)]
    print(f"[ws] 会话={session_id} 发送器收到={recorder.sent}")


def test_ws_same_chat_reuses_session(ctx: AppContext, recorder) -> None:
    """同一个 chat 连收两条 → 复用同一条会话（`session.ensure` 幂等）。"""
    ctx.llm = StubLlm(reply("第一轮"), reply("第二轮"))
    _approve(ctx)
    stub = StubSdk()
    client = _ws_client(ctx, recorder, stub)
    stub.deliver(_event("第一句"), _event("第二句继续"))
    client.start(say=lambda _s: None)

    assert ctx.db.scalar_int("SELECT count(*) FROM sessions WHERE session_key = ?", (CHAT_ID,)) == 1
    assert recorder.sent == [(CHAT_ID, "第一轮"), (CHAT_ID, "第二轮")]


def test_ws_start_in_background_does_not_block(ctx: AppContext, recorder) -> None:
    """`start(block=False)` 立刻返回，收包循环在后台线程里跑（嵌入/桩测用）。

    桩 client 这轮被设成**阻塞的收包循环**（真 SDK 的 `start()` 不返回），所以能测出三件事：
    ① `start()` 没阻塞调用方；② 线程真活着并跑完了那一轮；③ `stop()` 会转发到 SDK 的停止接口，
    收包循环随之退出（`running` 转 False）。
    """
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _approve(ctx)
    stub = StubSdk()
    stub.block_on_start = True
    gate = threading.Event()
    stub.on_done = gate.set
    client = _ws_client(ctx, recorder, stub)
    stub.deliver(_event("后台线程里跑"))
    client.start(block=False, say=lambda _s: None)

    assert gate.wait(5), "后台线程没跑起来"
    assert client.running is True, "收包循环返回得太早，后台线程已经不在了"
    assert recorder.sent == [(CHAT_ID, STUB_REPLY)]

    client.stop()
    assert stub.client.stopped is True, "stop() 应转发给 SDK 的停止接口"
    assert stub.client.exited.wait(5), "收包循环没被放开"
    assert client.running is False


# ================================================================== B. 拦截与健壮性


def test_ws_unapproved_only_gets_pairing_prompt_no_session(ctx: AppContext, recorder) -> None:
    """**反例**：默认 pairing 下未授权私聊 → 只回配对提示，绝不执行会话、绝不叫模型。"""
    ctx.llm = StubLlm(reply(STUB_REPLY))  # 备着；真跑了会消耗它
    stub = StubSdk()
    client = _ws_client(ctx, recorder, stub)
    stub.deliver(_event("喂，在吗"))
    client.start(say=lambda _s: None)

    assert ctx.db.scalar_int("SELECT count(*) FROM sessions") == 0
    assert recorder.sent == [(CHAT_ID, feishu.PAIRING_PROMPT)]
    assert ctx.llm.calls == []


def test_ws_paired_dm_runs_turn(ctx: AppContext, recorder) -> None:
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _approve(ctx)
    stub = StubSdk()
    client = _ws_client(ctx, recorder, stub)
    stub.deliver(_event("我是白名单用户"))
    client.start(say=lambda _s: None)
    assert recorder.sent == [(CHAT_ID, STUB_REPLY)]


def test_ws_pairing_code_registers_openid_without_running_turn(ctx: AppContext, recorder) -> None:
    """未授权者把**向导生成的配对码**私聊发回来 → 登记 openid（只回执、不跑会话）。"""
    code = dispatch(ctx, "feishu.pairing.create", {})["code"]
    stub = StubSdk()
    client = _ws_client(ctx, recorder, stub)
    stub.deliver(_event(code))
    client.start(say=lambda _s: None)

    assert recorder.sent == [(CHAT_ID, feishu.PAIRING_CLAIMED_REPLY)]
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions") == 0
    pending = dispatch(ctx, "feishu.pairing.list", {})["pending"]
    assert [row["user_id"] for row in pending if row["claimed"]] == [SENDER]


def test_ws_allowlist_denies_unlisted_and_logs(ctx: AppContext, recorder, caplog) -> None:
    """**反例**：`allowlist` 策略下名单外 → 直接丢弃并记日志（不回、不跑会话）。"""
    os.environ["FEISHU_DM_POLICY"] = "allowlist"
    ctx.llm = StubLlm(reply(STUB_REPLY))
    with caplog.at_level(logging.WARNING, logger="scholarscout.channels.feishu"):
        stub = StubSdk()
        client = _ws_client(ctx, recorder, stub)
        stub.deliver(_event("我是名单外的"))
        client.start(say=lambda _s: None)

    assert ctx.db.scalar_int("SELECT count(*) FROM sessions") == 0
    assert recorder.sent == []
    assert any("not_allowlisted" in r.message for r in caplog.records)


def test_ws_group_without_mention_is_dropped(ctx: AppContext, recorder, caplog) -> None:
    """**反例**：群聊没 @ 机器人 → 丢弃并记日志。"""
    os.environ["FEISHU_BOT_OPEN_ID"] = BOT_OPEN_ID
    ctx.llm = StubLlm(reply(STUB_REPLY))
    with caplog.at_level(logging.WARNING, logger="scholarscout.channels.feishu"):
        stub = StubSdk()
        client = _ws_client(ctx, recorder, stub)
        stub.deliver(_event("大家好", chat_type="group", chat_id="oc_ws_grp_1"))
        client.start(say=lambda _s: None)

    assert ctx.db.scalar_int("SELECT count(*) FROM sessions") == 0
    assert recorder.sent == []
    assert any("group_no_mention" in r.message for r in caplog.records)


def test_ws_group_with_mention_runs(ctx: AppContext, recorder) -> None:
    """**正例**：群聊 @ 到机器人（按 open_id 认）→ 跑会话。"""
    os.environ["FEISHU_BOT_OPEN_ID"] = BOT_OPEN_ID
    ctx.llm = StubLlm(reply(STUB_REPLY))
    stub = StubSdk()
    client = _ws_client(ctx, recorder, stub)
    stub.deliver(
        _event(
            "@机器人 帮我看下",
            chat_type="group",
            chat_id="oc_ws_grp_2",
            mentions=[{"id": {"open_id": BOT_OPEN_ID}, "name": BOT_NAME}],
        )
    )
    client.start(say=lambda _s: None)
    assert recorder.sent == [("oc_ws_grp_2", STUB_REPLY)]


def test_ws_non_text_event_is_ignored(ctx: AppContext, recorder) -> None:
    """非文本（图片等）事件被忽略：不报错、不跑会话、不回包。"""
    stub = StubSdk()
    client = _ws_client(ctx, recorder, stub)
    assert client.handle_event(_event("x", message_type="image")) is None
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions") == 0
    assert recorder.sent == []


def test_ws_broken_event_does_not_kill_the_link(ctx: AppContext, recorder, caplog) -> None:
    """一条坏消息（那一轮 RPC 炸了）**不能把长连接弄哑**：兜住、记日志，下一条照跑。"""
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _approve(ctx)
    seen = {"n": 0}

    def flaky(ctx_: AppContext, name: str, params: Any = None):
        if name == "chat.send":
            seen["n"] += 1
            if seen["n"] == 1:
                raise RuntimeError("boom")
        return dispatch(ctx_, name, params)

    with caplog.at_level(logging.WARNING, logger="scholarscout.channels.feishu"):
        stub = StubSdk()
        client = _ws_client(ctx, recorder, stub, dispatcher=flaky)
        stub.deliver(_event("第一条会炸"), _event("第二条要能跑通"))
        client.start(say=lambda _s: None)  # 回调抛异常的话这里就会炸

    assert any("处理消息失败" in r.message for r in caplog.records)
    assert recorder.sent == [(CHAT_ID, STUB_REPLY)]  # 第二条照常回


# ================================================================== 纯函数 / 装配契约


def test_event_to_payload_accepts_full_payload_half_payload_and_sdk_object() -> None:
    """三种输入都归一成同一份报文：完整报文 / 只有 event 半截 / 属性式 SDK 对象。"""
    full = _event("归一测试")
    half = {"event": full["event"]}

    sdk_object = SimpleNamespace(
        schema="2.0",
        header=SimpleNamespace(event_type=feishu.RECEIVE_EVENT),
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=SENDER)),
            message=SimpleNamespace(
                message_id="om_ns",
                chat_id=CHAT_ID,
                chat_type="p2p",
                message_type="text",
                content=json.dumps({"text": "归一测试"}, ensure_ascii=False),
            ),
        ),
    )

    for payload in (
        feishu_ws.event_to_payload(full),
        feishu_ws.event_to_payload(half),
        feishu_ws.event_to_payload(sdk_object),
    ):
        message = feishu.parse_message_event(payload)
        assert message is not None
        assert (message.chat_id, message.text, message.sender_open_id) == (
            CHAT_ID,
            "归一测试",
            SENDER,
        )

    # 半截报文也补上事件类型（长连接只订阅了一种）
    assert feishu_ws.event_to_payload(half)["header"]["event_type"] == feishu.RECEIVE_EVENT
    # 非报文/非对象 → 空报文（调用方据此当作"不是消息"）
    assert feishu_ws.event_to_payload(None) == {}
    # 下划线开头的 SDK 内部字段被丢掉
    assert "_types" not in json.dumps(feishu_ws.event_to_payload(sdk_object))


def test_probe_sdk_reports_install_hint_without_importing() -> None:
    """SDK 缺装时给的是**能照着做的一句话**（含安装命令），且**不 import** SDK。"""
    ok, note = feishu_ws.probe_sdk(finder=lambda _name: None)
    assert ok is False
    assert "lark-oapi" in note and "install_deps.py" in note
    ok, note = feishu_ws.probe_sdk(finder=lambda _name: object())
    assert ok is True
    assert "不需要" in note and "隧道" in note


def test_client_is_assembled_once_and_keeps_the_ua_tag(ctx: AppContext, recorder) -> None:
    """重复装配返回同一实例；两条与参照实现对齐的装配细节都被钉住。"""
    stub = StubSdk()
    client = _ws_client(ctx, recorder, stub)
    first = client.build_client()
    assert client.build_client() is first
    assert len(stub.clients) == 1
    # 长连接不校签：builder 的两个凭据传空（报文由已认证的长连接收到）
    assert stub.event_handlers.builder_calls == [("", "")]
    assert tuple(client.extra_ua_tags) == feishu_ws.WS_EXTRA_UA_TAGS


def test_start_line_promises_no_public_address() -> None:
    """起连接那行日志必须**明说什么不需要**（公网地址/回调/隧道）—— 这是本方案的门面承诺。"""
    line = feishu_ws.START_LOG_TEMPLATE.format(app_id="cli_x", domain="https://open.feishu.cn")
    for keyword in ("不需要公网地址", "回调", "隧道", feishu.RECEIVE_EVENT):
        assert keyword in line
