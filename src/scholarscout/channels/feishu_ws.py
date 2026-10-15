"""飞书渠道的 **websocket 长连接适配层**（入站传输 = 官方 SDK `lark-oapi`；docs/17 §3.8）。

## 它在哪一层 / 和谁分工

与 `channels/feishu.py` 同层（入口层，只准经 `rpc/` 说话）：

- `channels/feishu.py` 是**传输无关**的入站核心（解析 / 拦截 / `session.ensure` + `chat.send` / 回包）；
- 本模块**只负责传输**：用官方长连接收 `im.message.receive_v1`，把事件归一成报文
  （`{"header": ..., "event": ...}`）交给 `feishu.FeishuMessageHandler.handle_event`。

**不需要公网地址、不需要回调 URL、不需要内网穿透**：本机主动外连飞书服务器。这正是相对
webhook 路线（`http.server` 监听 + 隧道）的真实取舍 —— 见 `docs/17` §1.3 / §3.8。

## 三条实测要点（踩过的，别再踩）

1. **`extra_ua_tags=["channel"]` 不能省**：不加这个 UA 标签，飞书**不会**把群里的 `@` 事件推给长连接
   （Hermes `adapter.py:3702-3712` 的原话）。群聊策略 `mention` 完全依赖 @ 事件，省了就等于群聊静默失效。
   本机无法验证（没有可用凭据），此条是**照参照实现抄的**，不是本项目实测 —— 如实标注。
2. **`import lark_oapi` 很慢**：本机首次 **116 s**（SDK 带上千个子模块，要写 `.pyc`），之后约 14 s。
   所以本模块**绝不在 import 期加载它** —— 只有真要连的那一次才 `importlib.import_module`。
   后果之一：`pytest` 跑本模块的桩测**完全不需要装 SDK**（`tests/channels/test_feishu_ws_stub.py`）。
3. **长连接不校签**：官方 SDK 的 WS 分支走 `EventDispatcherHandler._do_without_validation`
   （`lark_oapi/event/dispatcher_handler.py:167`）—— 报文由 SDK 在已认证的长连接上收到，
   不存在"谁都能往这个端口 POST"的前提，所以 verification token / 签名 / challenge 那一套
   在长连接下**没有意义**（随 webhook 一起删掉了）。

## 用法

    from scholarscout.channels import feishu_ws
    client = feishu_ws.build_client(ctx, sender=sender)   # sender 由入口层用 fetch/feishu_send 造
    client.start()                                        # 阻塞；Ctrl-C 收尾

一键入口：`scripts/feishu_ws_up.py`（根目录双击 `飞书一键开机.bat`）。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from scholarscout.channels import feishu
from scholarscout.rpc.registry import dispatch

logger = logging.getLogger(__name__)

#: SDK 的包名（只用于 `find_spec` 查规格，**不 import**）
SDK_PACKAGE = "lark_oapi"
#: 官方长连接客户端所在模块（`lark_oapi.ws.Client`）
SDK_WS_MODULE = "lark_oapi.ws"
#: 长连接的 UA 标签。**省了它飞书不推群 @ 事件**（见模块顶注第 1 条）。
WS_EXTRA_UA_TAGS: tuple[str, ...] = ("channel",)
#: `.env` 的 `FEISHU_DOMAIN` 取值 → SDK 的 domain 参数（就是平台入口 URL，不另造映射表）
DOMAIN_DEFAULT = "feishu"


# ------------------------------------------------------------------ SDK 在哪（只查规格，不 import）


def sdk_available(*, finder: Callable[[str], Any] = importlib.util.find_spec) -> bool:
    """SDK 装了没 —— **只看规格，不真的 import**（import 一次 100 s+，见模块顶注第 2 条）。"""
    try:
        return finder(SDK_PACKAGE) is not None
    except (ImportError, ValueError):  # pragma: no cover - 罕见环境（父包缺失）
        return False


def probe_sdk(*, finder: Callable[[str], Any] = importlib.util.find_spec) -> tuple[bool, str]:
    """给向导/一键入口的人话自检：`(是否可用, 说明)`。**不联网、不 import**。"""
    if sdk_available(finder=finder):
        return True, f"{SDK_PACKAGE} 已安装（长连接：**不需要**公网地址 / 回调 URL / 隧道）"
    return False, (
        f"未安装 {SDK_PACKAGE}（PyPI 包名 lark-oapi）：先跑 `python scripts/install_deps.py`"
        "（它会照 pyproject 装，并一并带上 websockets / requests 等传递依赖）"
    )


@dataclass(frozen=True)
class WsSdk:
    """从 `lark_oapi` 取到的三样东西。

    抽出来是为了让**桩测能注入替身**（不 import 真 SDK）—— 与 `FeishuWsClient` 的边界就三个名字。
    """

    client_cls: Any  # lark_oapi.ws.Client
    event_handler_cls: Any  # lark_oapi.EventDispatcherHandler
    log_level: Any = None  # lark_oapi.LogLevel.INFO


def load_sdk() -> WsSdk:
    """真正加载 SDK（**只有这里会 import 它**），失败时给人话 + 下一步命令。"""
    try:
        lark = importlib.import_module(SDK_PACKAGE)
        ws_module = importlib.import_module(SDK_WS_MODULE)
    except ImportError as exc:  # pragma: no cover - 桩测注入 sdk，不会走这里
        raise RuntimeError(
            f"未安装 {SDK_PACKAGE}（PyPI 包名 lark-oapi）：先跑 `python scripts/install_deps.py`"
            "（它会照 pyproject 装，并一并带上 websockets / requests 等传递依赖）"
        ) from exc
    return WsSdk(
        client_cls=ws_module.Client,
        event_handler_cls=lark.EventDispatcherHandler,
        log_level=getattr(getattr(lark, "LogLevel", None), "INFO", None),
    )


# ------------------------------------------------------------------ 事件归一（纯函数，可单独测）


def plainify(value: Any) -> Any:
    """把 SDK 的事件对象递归换成**普通 dict/list/标量**（下划线开头的内部字段丢掉）。

    SDK 的事件是 `lark_oapi` 自己的模型对象（`P2ImMessageReceiveV1` 等），而渠道核心只认
    `{"header": ..., "event": ...}` 这种报文。转换写在这里、写成纯函数，是为了**桩测能在
    一份 dict 上把链路测完**，不必为了造一个真 SDK 对象去 import SDK。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): plainify(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, (list, tuple, set)):
        return [plainify(item) for item in value]
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, Mapping):
        return {str(k): plainify(v) for k, v in attrs.items() if not str(k).startswith("_")}
    return value


def event_to_payload(event: Any) -> dict[str, Any]:
    """把长连接回调收到的东西归一成 `feishu.parse_message_event` 认的报文。

    三种输入都吃：① SDK 事件对象（`P2ImMessageReceiveV1`）；② 已经是完整报文的 dict
    （`{"header": ..., "event": ...}`）；③ 只有 `event` 的那半截 dict。
    缺 `header.event_type` 时补成本模块唯一订阅的事件类型（长连接只注册了它）。
    """
    plain = plainify(event)
    if not isinstance(plain, Mapping):
        return {}
    body = dict(plain)
    if "event" not in body:
        body = {"event": body}
    header = body.get("header")
    if not isinstance(header, Mapping) or not str(header.get("event_type") or ""):
        merged = dict(header) if isinstance(header, Mapping) else {}
        merged["event_type"] = feishu.RECEIVE_EVENT
        body["header"] = merged
    return body


# ------------------------------------------------------------------ 长连接客户端


@dataclass
class FeishuWsClient:
    """长连接客户端：建 SDK client → 注册 `im.message.receive_v1` → 起连接。

    `sdk` 可注入（桩测给替身，不 import 真 SDK）；`handler` 可注入（默认新建
    `feishu.FeishuMessageHandler`，发送器由入口层注入 —— 渠道层不出网）。
    """

    ctx: feishu.ChannelContext
    sender: feishu.Sender | None = None
    dispatcher: feishu.Dispatcher = dispatch
    credentials: feishu.FeishuCredentials | None = None
    handler: feishu.MessageHandler | None = None
    sdk: WsSdk | None = None
    app_id: str = ""
    app_secret: str = ""
    domain: str = DOMAIN_DEFAULT
    #: 入站即时回执文案（docs/17 §3.11）：转发给它自己建的那个 `FeishuMessageHandler`。
    #: `None` = 让渠道核心按 `.env` 的 `FEISHU_ACK_TEXT` 决定（入口层注入的那份），空串 = 关掉回执。
    ack_text: str | None = None
    extra_ua_tags: Sequence[str] = field(default_factory=lambda: WS_EXTRA_UA_TAGS)
    _client: Any = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _started: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.credentials is None:
            self.credentials = feishu.credentials_from_env()
        if not self.app_id:
            self.app_id = self.credentials.app_id
        if not self.app_secret:
            self.app_secret = self.credentials.app_secret
        if self.handler is None:
            self.handler = feishu.FeishuMessageHandler(
                self.ctx,
                credentials=self.credentials,
                sender=self.sender,
                dispatcher=self.dispatcher,
                ack_text=self.ack_text,
            )

    # ---------------------------------------------------------------- 只读视图

    @property
    def client(self) -> Any:
        """SDK 的 client 实例（**未建时是 None**；桩测用它断言注册与参数）。"""
        return self._client

    @property
    def running(self) -> bool:
        """已 start 且（若是后台线程）线程还活着。"""
        if not self._started:
            return False
        return self._thread is None or self._thread.is_alive()

    @property
    def domain_url(self) -> str:
        """SDK 的 `domain` 参数（飞书 / Lark 的开放平台入口 URL）。"""
        return feishu.open_entry_url(self.domain)

    # ---------------------------------------------------------------- 装配

    def build_client(self) -> Any:
        """装配 SDK 长连接客户端（**不启动**；重复调用返回同一个实例）。

        两处细节都有出处：
        - `EventDispatcherHandler.builder("", "")` 传空凭据 —— 长连接不校签（模块顶注第 3 条）；
        - `extra_ua_tags=["channel"]` —— 省了飞书不推群 @ 事件（模块顶注第 1 条）。
        """
        if self._client is not None:
            return self._client
        sdk = self.sdk or load_sdk()
        event_handler = (
            sdk.event_handler_cls.builder("", "")
            .register_p2_im_message_receive_v1(self.on_message_event)
            .build()
        )
        self._client = sdk.client_cls(
            app_id=self.app_id,
            app_secret=self.app_secret,
            log_level=sdk.log_level,
            event_handler=event_handler,
            domain=self.domain_url,
            extra_ua_tags=list(self.extra_ua_tags),
        )
        return self._client

    # ---------------------------------------------------------------- 收事件

    def handle_event(self, event: Any) -> feishu.InterceptDecision | None:
        """一条 SDK 事件 → 归一 → 交给入站核心。**只管转发，不解释**。"""
        assert self.handler is not None  # __post_init__ 必填
        return self.handler.handle_event(event_to_payload(event))

    def on_message_event(self, data: Any) -> None:
        """注册给 SDK 的回调（跑在 SDK 自己的线程里）。

        **绝不外抛**：回调抛异常会顺着 SDK 的收包线程上去，一条坏消息就够让整条链路静默失聪。
        """
        try:
            self.handle_event(data)
        except Exception as exc:
            logger.warning("[Feishu] 长连接事件处理异常（本条丢弃，连接继续）：%s", exc)

    # ---------------------------------------------------------------- 生命周期

    def start(self, *, block: bool = True, say: Callable[[str], None] = print) -> FeishuWsClient:
        """起长连接。

        - `block=True`（默认，CLI/一键入口）：在当前线程跑 SDK 的收包循环，Ctrl-C 收尾；
        - `block=False`：在 daemon 线程里跑（嵌入与桩测用）。
        """
        client = self.build_client()
        line = START_LOG_TEMPLATE.format(app_id=self.app_id, domain=self.domain_url)
        logger.info(line)
        say(line)
        self._started = True
        if block:
            try:
                client.start()
            except KeyboardInterrupt:
                say("[Feishu] 收到 Ctrl-C，长连接收尾…")
        else:

            def _run() -> None:
                try:
                    client.start()
                except Exception as exc:  # pragma: no cover - 桩测不会走
                    logger.warning("[Feishu] 长连接退出：%s", exc)

            self._thread = threading.Thread(target=_run, name="feishu-ws", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        """尽力收尾。

        `lark-oapi` 的长连接**没有公开的关闭接口**（`lark_oapi.ws.Client` 只有 `start()`；
        它的重连循环活着就是为了断线自愈）。所以这里只能：标记停止 + 若 SDK 将来提供了
        `close()/stop()` 就顺手调一下。**进程退出即断开**，这就是当前的真实语义。
        """
        self._started = False
        client = self._client
        for name in ("close", "stop"):
            closer = getattr(client, name, None)
            if callable(closer):
                try:
                    closer()
                except Exception as exc:  # pragma: no cover - 兜底
                    logger.warning("[Feishu] 关闭长连接失败（忽略）：%s", exc)
                return
        logger.info("[Feishu] 长连接无公开关闭接口：随进程退出断开")


#: 起连接时打印的那一行（**明确说什么不需要**：公网地址 / 回调 / 隧道）
START_LOG_TEMPLATE = (
    "[Feishu] websocket 长连接启动：app_id={app_id} domain={domain}"
    "（本机主动外连，不需要公网地址 / 回调 URL / 隧道；事件订阅勾选 im.message.receive_v1）"
)


def build_client(
    ctx: feishu.ChannelContext,
    *,
    sender: feishu.Sender | None = None,
    dispatcher: feishu.Dispatcher = dispatch,
    sdk: WsSdk | None = None,
    ack_text: str | None = None,
) -> FeishuWsClient:
    """给入口层用的装配口：`FeishuWsClient`（sender / dispatcher / sdk / ack_text 可注入）。"""
    return FeishuWsClient(ctx, sender=sender, dispatcher=dispatcher, sdk=sdk, ack_text=ack_text)
