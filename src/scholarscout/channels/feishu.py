"""飞书渠道的**入口层核心**（传输无关；docs/17 §3）。

## 它在哪一层

和 `desktop/` 同级：都是"外面来一条消息 → 汇进去跑一轮"的前门。所以它**只准走 `rpc/`**
（进程内 `dispatch`），不读库、不出网、不 import `runtime/tools/state/...`
—— 由 `tests/test_layering.py::RULES["channels"]` 钉死。

## 它做三件事

1. **解析**：把飞书事件报文（`im.message.receive_v1`）解成 `FeishuMessage`（纯函数，可单独断言）；
2. **拦截**：群聊 `@提及` 策略 / 私聊 `pairing|all|allowlist` 策略 —— 纯函数 `authorize_message`，
   白名单与临时放行**经 `feishu.access` RPC 取**（渠道层不读库）；
3. **跑一轮**：一条消息 = 一次会话请求（`session.ensure` + `chat.send`），回复交给**注入的发送器**。

「要文件」（「发我学习路线」）的**规则识别**在 `channels/feishu_files.py`（纯函数、零 IO）；
本模块仍重导出 `FileRequest` / `parse_file_request`，老写法 `feishu.parse_file_request` 照旧可用。

入站的**纯函数层**（报文解析 / `@提及` 判定 / 放行决策 / 消息去重）在 `channels/feishu_inbound.py`，
`.env` 读取与接入向导常量在 `channels/feishu_config.py` —— 两边的名字都由本模块**重导出**，
`from scholarscout.channels.feishu import X` 照旧可用。

## 传输层只有一条：官方 SDK 的长连接

入站在 `channels/feishu_ws.py`（`lark-oapi` 的 websocket 长连接，**不需要公网地址 / 回调 URL / 隧道**）。
本模块**不碰任何 socket**：它只认"已经解好的事件报文"，因此桩事件（甚至一个 dict）就能把整条
链路测到底（`tests/channels/test_feishu_stub.py`、`tests/channels/test_feishu_ws_stub.py`）。

> **历史（2026-10-14 用户拍板）**：此前走的是"标准库 `http.server` webhook 监听 + 内网穿透隧道"，
> 已随本批**整体删除** —— 校验（verification token / 签名 / challenge）、限速限长、`FeishuCallbackServer`、
> `run_listener`、`probe_listener`、`--feishu-listen`、`scripts/feishu_up.py`、`飞书一键起飞.bat`，
> 以及 `config.runtime.feishu_listener_{enabled,port,path}` 三个配置键。
> 取舍（**依赖最小化 vs 接入便捷**）写在 `docs/17` §1.3 与 §3.8 —— 这是本项目真实的架构决策，不是清理癖。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from scholarscout.channels.feishu_config import (
    DEFAULT_CONNECTION_MODE,
    DEFAULT_DM_POLICY,
    DEFAULT_GROUP_POLICY,
    DEFAULT_REGISTRATION_FROM,
    DEFAULT_REGISTRATION_TP,
    LAUNCHER_URL_TEMPLATE,
    OPEN_ENTRY_URLS,
    REGISTRATION_FROM_ENV,
    REGISTRATION_TP_ENV,
    SUPPORTED_CONNECTION_MODES,
    SUPPORTED_DM_POLICIES,
    SUPPORTED_GROUP_POLICIES,
    bot_name_from_env,
    bot_open_id_from_env,
    connection_mode_from_env,
    dm_policy_from_env,
    group_policy_from_env,
    launcher_url,
    open_entry_url,
    registration_from_env,
    registration_tp_from_env,
)
from scholarscout.channels.feishu_files import FileRequest, parse_file_request
from scholarscout.channels.feishu_inbound import (
    ACK_DEDUPE_SIZE,
    PAIRING_CLAIMED_REPLY,
    PAIRING_PROMPT,
    RECEIVE_EVENT,
    FeishuMessage,
    InterceptDecision,
    RecentMessageIds,
    authorize_message,
    bot_mentioned,
    parse_message_event,
)
from scholarscout.channels.feishu_inbound import (
    _parse_mentions as _parse_mentions,
)
from scholarscout.channels.feishu_inbound import (
    _text_from_content as _text_from_content,
)
from scholarscout.config import DEFAULT_FEISHU_ACK_TEXT
from scholarscout.rpc.registry import dispatch

logger = logging.getLogger(__name__)

#: 公开契约（对外可依赖的名字）。入站纯函数层的实现在 `channels/feishu_inbound.py`，
#: 这里的导入是**向后兼容的重导出** —— `from scholarscout.channels.feishu import X` 照旧可用。
#: 列进 `__all__` 还让 lint 明白这些名字是"有意再导出"，不是没用的导入。
__all__ = [
    "ACK_DEDUPE_SIZE",
    "DEFAULT_CONNECTION_MODE",
    "DEFAULT_DM_POLICY",
    "DEFAULT_GROUP_POLICY",
    "DEFAULT_REGISTRATION_FROM",
    "DEFAULT_REGISTRATION_TP",
    "LAUNCHER_URL_TEMPLATE",
    "OPEN_ENTRY_URLS",
    "PAIRING_CLAIMED_REPLY",
    "PAIRING_PROMPT",
    "RECEIVE_EVENT",
    "REGISTRATION_FROM_ENV",
    "REGISTRATION_TP_ENV",
    "SUPPORTED_CONNECTION_MODES",
    "SUPPORTED_DM_POLICIES",
    "SUPPORTED_GROUP_POLICIES",
    "ChannelContext",
    "Dispatcher",
    "FeishuCredentials",
    "FeishuMessage",
    "FeishuMessageHandler",
    "FileRequest",
    "InterceptDecision",
    "MessageHandler",
    "RecentMessageIds",
    "RecordingSender",
    "Sender",
    "ack_text_from_env",
    "authorize_message",
    "bot_mentioned",
    "bot_name_from_env",
    "bot_open_id_from_env",
    "connection_mode_from_env",
    "credentials_from_env",
    "dm_policy_from_env",
    "group_policy_from_env",
    "launcher_url",
    "open_entry_url",
    "parse_file_request",
    "parse_message_event",
    "registration_from_env",
    "registration_tp_from_env",
]

# ------------------------------------------------------------------ 常量


#: 「发文件」走后端这条 RPC（docs/17 §3.12）：渠道层只解析意图，查库/导出/上传/发送在主进程
FILE_DELIVERY_METHOD = "channel.send_file"

#: 入站身份刷新（修 `99992361 open_id cross app`）：放行之后把事件里的**真实 open_id**
#: 落/刷新到 `feishu_paired_users`；**只对白名单内的发送者**，非白名单由主进程再复核一次。
IDENTITY_REMEMBER_METHOD = "feishu.pairing.remember"

Sender = Callable[[str, str], dict[str, Any]]

#: 渠道层认上下文为**不透明对象**：`FeishuMessageHandler` 一个 ctx 字段都不读，只把它原样递给
#: `dispatcher`。主进程给的是 `rpc.registry.AppContext`，渠道子进程给的是
#: `channels.remote.RemoteContext`（没有 db/llm/http，且**刻意**不是 AppContext 的子类）——
#: 标 `Any` 是如实描述这层的鸭子类型，不是放宽检查。
ChannelContext = Any
Dispatcher = Callable[[ChannelContext, str, Any], Any]


class MessageHandler(Protocol):
    """入站处理器的形状：只有 `handle_event`。

    `FeishuMessageHandler` 与 `channels/runner.py::StatefulHandler`（包一层顺手记"最后一条消息"）
    都满足它 —— 两者互不相识，靠结构化兼容；这里若写死成 `FeishuMessageHandler`，
    会把**有意**包的那一层判成类型错误。
    """

    def handle_event(self, payload: Any) -> Any: ...


# ---------------------------------------------------------------- 入站即时回执（docs/17 §3.11）

#: 回执文案的**注入通道**（不是用户配置项）：用户在 `config.yaml` 里改 `runtime.feishu_ack_text`，
#: 父进程起渠道子进程时把它写进这个环境变量（`channels/supervisor.py::config_env`）。
#: 渠道层读不到 `config.yaml`（子进程没有配置对象），所以必须有这么一条注入通道 ——
#: 与 `FEISHU_DM_POLICY` / 凭据的走法一致（环境继承）。
ACK_TEXT_ENV = "FEISHU_ACK_TEXT"


def ack_text_from_env() -> str:
    """回执文案：环境变量优先，缺省回 `config.DEFAULT_FEISHU_ACK_TEXT`（"已接受消息，正在处理中"）。

    **空串是合法值**（= 关掉回执），所以不能用 `or` 兜默认 —— 那会把"用户显式关掉"吃掉。
    """
    raw = os.environ.get(ACK_TEXT_ENV)
    if raw is None:
        return DEFAULT_FEISHU_ACK_TEXT
    return raw.strip()


@dataclass(frozen=True)
class FeishuCredentials:
    """飞书凭据（**只从 .env / 环境变量读**，绝不硬编码、绝不入库）。

    长连接只需要 `app_id` + `app_secret`（用它换 token 才能连上）。历史上 webhook 还要
    verification token / encrypt key —— 那两个键随 webhook 一起删了。

    `app_secret` 显式 `repr=False`：dataclass 的自动 `__repr__` 会把字段原样打出来，
    而 `logger.info("%s", creds)` / 异常里拼 `{creds}` 这类写法到处都是 ——
    密钥不能靠"记得别打"来守，要从 repr 上就看不到。
    """

    app_id: str = ""
    app_secret: str = field(default="", repr=False)

    @property
    def can_send(self) -> bool:
        """能否走飞书 REST 发消息（要有 app_id + app_secret）。"""
        return bool(self.app_id and self.app_secret)


def credentials_from_env() -> FeishuCredentials:
    """从环境变量取凭据（`.env` 由 `config.load_dotenv` 加载进 `os.environ`）。"""
    return FeishuCredentials(
        app_id=os.environ.get("FEISHU_APP_ID", "").strip(),
        app_secret=os.environ.get("FEISHU_APP_SECRET", "").strip(),
    )


class RecordingSender:
    """无凭据/本地桩时的默认发送器：**不假装发成功**，只记录 + 告警。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def __call__(self, chat_id: str, text: str) -> dict[str, Any]:
        self.sent.append((chat_id, text))
        logger.warning("[Feishu] 未注入发送器：回复未真正发往飞书（只记录）chat_id=%s", chat_id)
        return {"code": 0, "recorded": True, "chat_id": chat_id}


# ------------------------------------------------------------------ 入站核心（与传输解耦）


class FeishuMessageHandler:
    """入站核心：**传输无关**（不碰 socket、不知道事件从哪来），所以能直接单测。

    传输层（`channels/feishu_ws.py` 的长连接）只做两件事：收事件 → 调 :meth:`handle_event`。
    这里没有任何限速/限长/校签 —— 那些是公网回调才需要的东西，长连接由官方 SDK 维护、
    本机主动外连，不存在"谁都能往这个端口 POST"的前提（详见 docs/17 §3.3 的取舍说明）。
    """

    def __init__(
        self,
        ctx: ChannelContext,
        *,
        credentials: FeishuCredentials | None = None,
        sender: Sender | None = None,
        dispatcher: Dispatcher = dispatch,
        source: str = "feishu",
        bot_open_id: str | None = None,
        bot_name: str | None = None,
        ack_text: str | None = None,
        ack_dedupe_size: int = ACK_DEDUPE_SIZE,
    ) -> None:
        self._ctx = ctx
        self._credentials = credentials or credentials_from_env()
        self._dispatcher = dispatcher
        self._source = source
        #: 机器人自身身份（群聊 @提及 判定用）。默认读 `.env`，可注入替换（桩测）。
        self._bot_open_id = bot_open_id if bot_open_id is not None else bot_open_id_from_env()
        self._bot_name = bot_name if bot_name is not None else bot_name_from_env()
        #: 入站即时回执文案（docs/17 §3.11）。默认读 `.env` 的 `FEISHU_ACK_TEXT`
        #: （由入口层从 `runtime.feishu_ack_text` 注入）；**空串 = 关掉回执**。
        self._ack_text = ack_text.strip() if ack_text is not None else ack_text_from_env()
        #: 回执去重（按飞书 message_id）：同一条消息重推只回执一次。
        self._seen_messages = RecentMessageIds(ack_dedupe_size)

        if sender is not None:
            self._sender: Sender = sender
        else:
            self._recorder = RecordingSender()
            self._sender = self._recorder

    @property
    def credentials(self) -> FeishuCredentials:
        return self._credentials

    @property
    def sender(self) -> Sender:
        """当前发送器（入口层可据此报告"出站关着只记录"）。"""
        return self._sender

    @property
    def ack_text(self) -> str:
        """当前回执文案（空串 = 关掉回执）。"""
        return self._ack_text

    def decide(self, message: FeishuMessage) -> tuple[InterceptDecision, dict[str, Any]]:
        """拿白名单状态（经 RPC）后做运行时拦截判定，返回（决定, 原始访问状态）。

        **渠道层不读库**：`approved` / `temp_open` / `dm_policy` 都由 `feishu.access` RPC 给出
        （`docs/17 §3.4`、`docs/01 §3.8`）；群聊策略读 `.env`（与本模块同名键）。
        取不到访问状态时**保守处理**（不误放行）：私聊按未授权回配对提示、群聊丢弃。
        """
        group_policy = group_policy_from_env()
        try:
            access = self._dispatcher(
                self._ctx, "feishu.access", {"open_id": message.sender_open_id}
            )
        except Exception as exc:  # RPC 失败不能变成"放行"（那就等于没拦）
            logger.warning("[Feishu] 取访问状态失败，按未授权处理：%s", exc)
            access = {"approved": False, "temp_open": False, "dm_policy": dm_policy_from_env()}
        decision = authorize_message(
            message,
            dm_policy=str(access.get("dm_policy") or dm_policy_from_env()),
            group_policy=group_policy,
            approved=bool(access.get("approved")),
            temp_open=bool(access.get("temp_open")),
            bot_open_id=self._bot_open_id,
            bot_name=self._bot_name,
        )
        return decision, dict(access)

    def remember_sender(self, message: FeishuMessage) -> bool:
        """白名单放行之后：把这条**真实 open_id** 落/刷新到 `feishu_paired_users`。

        为什么需要（这是真打接口查出来的 bug）：飞书 **open_id 按应用隔离** —— 白名单里
        那条手工抄来的 `ou_...` 属于别的应用时，私聊发送永远回
        `{"code":99992361,"msg":"open_id cross app"}`，配置全对也发不出去。
        入站事件里的 `sender.sender_id.open_id` **一定属于本应用**，所以每收到一条已放行的
        消息就顺手对齐一次：表里的 id 从此只可能来自入站事件。

        三条边界（都有桩测钉住）：

        1. **只对白名单内的发送者**：调用点先看 `feishu.access` 的 `approved`；
           主进程 / `state` 层再查一次 `is_approved`，非白名单**一行都不写**；
        2. **失败不冒泡**：RPC 异常只记日志，**不影响本轮回复**（对齐是锦上添花）；
        3. **渠道层仍然不读库、不出网**：这件事只经 RPC（`channels/` 不许 import `state`）。
        """
        if not message.sender_open_id:
            return False  # 拿不到 id（非用户消息等）：没有可对齐的东西，直接跳过
        try:
            result = self._dispatcher(
                self._ctx, IDENTITY_REMEMBER_METHOD, {"open_id": message.sender_open_id}
            )
        except Exception as exc:  # 写库失败/主进程不在：只记日志，绝不拖垮这一轮
            logger.warning(
                "[Feishu] 入站身份刷新失败（不影响本轮）：open_id=%s err=%s",
                message.sender_open_id,
                exc,
            )
            return False
        updated = bool(result.get("updated")) if isinstance(result, Mapping) else False
        if updated:
            logger.debug("[Feishu] 入站身份已对齐：open_id=%s", message.sender_open_id)
        else:
            logger.debug(
                "[Feishu] 入站身份未刷新（不在白名单）：open_id=%s", message.sender_open_id
            )
        return updated

    def try_claim(self, message: FeishuMessage) -> bool:
        """未授权私聊：**先把整条消息当配对码登记**。

        登记成功（码对得上）就回一句回执并返回 True —— **不跑任何会话**，也不再用配对提示
        覆盖它。这是"用户把向导生成的码私聊发回来"的唯一入口（`docs/18 §③`）。
        码不对/过期/登记失败都返回 False（正常路径，不是错误）。
        """
        code = message.text.strip()
        if not (code and message.sender_open_id):
            return False
        try:
            claimed = self._dispatcher(
                self._ctx,
                "feishu.pairing.claim",
                {"code": code, "open_id": message.sender_open_id},
            )
        except Exception as exc:  # 登记失败不是错误路径，按普通未授权处理
            logger.warning("[Feishu] 配对码登记失败：%s", exc)
            return False
        if claimed and claimed.get("claimed"):
            logger.info("[Feishu] 配对码已登记：sender=%s", message.sender_open_id)
            self._sender(message.chat_id, PAIRING_CLAIMED_REPLY)
            return True
        return False

    def send_ack(self, message: FeishuMessage) -> bool:
        """收到消息后**先回一条固定文案**（白名单放行之后、跑轮次之前）；返回是否真发了。

        三条硬约定（docs/17 §3.11，都有用例钉住）：

        1. **失败不拖垮轮次**：发送器抛异常、或回 `ok=false`（网络 / 限流 / 平台报错）一律
           只记日志，**照常往下跑轮次** —— 回执是"锦上添花"，绝不能变成链路的单点；
        2. **受同一个出站开关管**：回执走的就是注入的那个发送器，所以
           `runtime.feishu_reply_enabled=false` 时它**一次外网都不发**（只告警、回 `ok=false`）
           —— 渠道层不需要、也不该另立一个开关；
        3. **按 `message_id` 去重**：飞书重推同一事件时只回执一次，用户不会看到双份。

        文案为空（`runtime.feishu_ack_text: ""`）时**直接不发**，连告警都不记（是用户的选择）。
        **去重是"记了就发一次"**：发送失败也占掉这个 id —— 宁可漏一条回执，也不给用户双份。
        """
        if not self._ack_text:
            return False
        if not self._seen_messages.mark(message.message_id):
            logger.info(
                "[Feishu] 同一条消息重推（message_id=%s）：回执不重复发", message.message_id
            )
            return False
        try:
            outcome = self._sender(message.chat_id, self._ack_text)
        except Exception as exc:  # 发送器自己炸了也不能把这一轮拖垮
            logger.warning(
                "[Feishu] 回执发送异常（已吞，照常跑轮次）：chat_id=%s err=%s",
                message.chat_id,
                exc,
            )
            return False
        if isinstance(outcome, Mapping) and outcome.get("ok") is False:
            logger.warning(
                "[Feishu] 回执未发出（不影响本轮，照常跑）：chat_id=%s err=%s",
                message.chat_id,
                outcome.get("error"),
            )
            return False
        logger.info("[Feishu] 已回执「%s」chat_id=%s", self._ack_text, message.chat_id)
        return True

    def deliver_file(self, message: FeishuMessage, request: FileRequest) -> InterceptDecision:
        """把一次"要文件"交给后端（`channel.send_file`），再把结果用一句话回给用户。

        **渠道层仍然不读库、不出网**：它只把「谁要 / 要哪一类 / 关键词」递上去，
        查库、生成导出文件、上传、发送都在主进程那一侧（`rpc/channel_file.py`，
        那里才允许 import `state` 与 `fetch`）。任何失败都只回一句可读的话，**不外抛**
        —— 长连接回调里抛异常等于整条链路静默失聪。
        """
        try:
            result = self._dispatcher(
                self._ctx,
                FILE_DELIVERY_METHOD,
                {
                    "platform": self._source,
                    "open_id": message.sender_open_id,
                    "kind": request.kind,
                    "query": request.query,
                    "session_key": message.chat_id,
                },
            )
        except Exception as exc:  # RPC/后端炸了也不外抛
            logger.warning("[Feishu] 发文件失败（已吞）：kind=%s err=%s", request.kind, exc)
            result = None
        reply = str(result.get("reply") or "") if isinstance(result, Mapping) else ""
        if not reply:
            reply = "暂时发不了文件：后端没有给出结果（详情见运行日志）。"
        self._sender(message.chat_id, reply)
        logger.info(
            "[Feishu] 发文件：chat_id=%s kind=%s query=%r → %s",
            message.chat_id,
            request.kind,
            request.query,
            "ok" if isinstance(result, Mapping) and result.get("ok") else "failed",
        )
        return InterceptDecision("file", "file_request")

    def run_turn(self, message: FeishuMessage) -> InterceptDecision:
        """一条消息 = 一次会话请求：**先过运行时拦截**，再幂等拿会话 → 跑一轮 → 回包。

        拦截顺序（对齐 `docs/17 §3.4`）：
        `drop` → 记日志丢弃；未授权私聊 → **先给一次配对码登记机会**（登记成功只回执、不跑会话），
        否则 `pair` 回配对提示、`run`（含"临时放行"）才真跑。返回本次的处置决定。

        放行之后、跑轮次之前**先发一条即时回执**（`docs/17 §3.11`）：一轮要跑 3–12 分钟，
        用户不该等这么久才知道"收到了"。回执失败**不影响**这一轮（成功/失败都在
        :meth:`send_ack` 里处理，异常一律不外抛）。
        """
        decision, access = self.decide(message)
        if decision.action == "drop":
            logger.warning(
                "[Feishu] 消息被丢弃（%s）chat_id=%s sender=%s",
                decision.reason,
                message.chat_id,
                message.sender_open_id,
            )
            return decision
        # 白名单放行之后**顺手对齐真实 open_id**（飞书 open_id 按应用隔离，只有入站事件里的
        # id 一定属于本应用）。放在拦截之后：被丢弃的消息不该有任何写动作。
        if bool(access.get("approved")):
            self.remember_sender(message)
        # 未授权私聊：先尝试把它当配对码登记（临时放行期间同样先试一次，否则码会被当普通消息跑掉）
        if (
            message.chat_type != "group"
            and not bool(access.get("approved"))
            and self.try_claim(message)
        ):
            return InterceptDecision("pair", "pairing_claimed")

        if decision.action == "pair":
            self._sender(message.chat_id, decision.reply or PAIRING_PROMPT)
            return decision

        # 「发我学习路线 / 传我那份 PDF」——**规则识别，不经过模型**（docs/17 §3.12）：
        # 放在放行之后（未授权的人要不到文件），放在跑轮次之前（能确定的事不必花一轮 token）。
        request = parse_file_request(message.text)
        if request is not None:
            return self.deliver_file(message, request)

        # 白名单放行之后、跑轮次之前：**即时回执**（顺序不能反 —— 先让用户安心，再落库跑轮次）。
        # 未授权者走不到这里（他们在上面就 `pair` / `drop` 返回了），所以不会收到回执。
        self.send_ack(message)

        ensured = self._dispatcher(
            self._ctx,
            "session.ensure",
            {"session_key": message.chat_id, "source": self._source},
        )
        session_id = str(ensured["session_id"])
        result = self._dispatcher(
            self._ctx, "chat.send", {"session_id": session_id, "text": message.text}
        )
        reply_text = str((result or {}).get("text") or "")
        self._sender(message.chat_id, reply_text)
        logger.info(
            "[Feishu] chat_id=%s → session=%s（stop_reason=%s，回复 %d 字）",
            message.chat_id,
            session_id,
            (result or {}).get("stop_reason"),
            len(reply_text),
        )
        return decision

    def handle_event(self, payload: Mapping[str, Any]) -> InterceptDecision | None:
        """一条事件报文 → 解析 → 跑（或拦截）。非文本/非消息事件回 None（不当成错误）。"""
        message = parse_message_event(payload)
        if message is None:
            logger.debug("[Feishu] 事件不是文本消息，忽略")
            return None
        return self.handle_message(message)

    def handle_message(self, message: FeishuMessage) -> InterceptDecision:
        """一条已解析的消息：拦截 → 跑会话 → 回包。

        **异常一律不外抛**：长连接的事件回调是由 SDK 的线程调的，一条坏消息把回调抛死
        就等于整条链路静默失聪 —— 所以这里兜住、记日志、如实回一个 `drop`。
        """
        try:
            return self.run_turn(message)
        except Exception as exc:
            logger.warning("[Feishu] 处理消息失败（本条丢弃，链路继续）：%s", exc)
            return InterceptDecision("drop", "handler_error")
