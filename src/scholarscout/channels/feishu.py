"""飞书渠道回调（入口层，docs/17 §3）。

## 它在哪一层

和 `desktop/` 同级：都是"外面来一条消息 → 汇进去跑一轮"的前门。所以它**只准走 `rpc/`**
（进程内 `dispatch`），不读库、不出网、不 import `runtime/tools/state/...`
—— 由 `tests/test_layering.py::RULES["channels"]` 钉死。

## 它做四件事

1. 用标准库 `http.server` 起一个回调监听（**默认关**，见 `config.runtime.feishu_listener_*`）；
2. 解析飞书事件回调 JSON、处理 URL 验证 `challenge`；
3. 两层校验（verification token + 签名），密钥只从 `.env` 读；**没有密钥时不校签、明确告警**；
4. 把消息变成一次会话请求（`session.ensure` + `chat.send`），把回复交给**注入的发送器**
   （真实部署注入走飞书 REST 的发送器；本地桩回调注入记录器）。

## 接口契约（来自 Hermes 适配器，逐条带 `文件:行号`，不凭印象）

`plugins/platforms/feishu/adapter.py`（本机副本，4308 行）：
- 只收 `application/json`（`:2679-2682`）；
- body 上限 1 MB（`:155 / :2685-2687`）；
- 限速：`app_id:path:remote_ip` 滑动窗口 60s/120 次（`:156-157 / :2776-2785`）；
- verification token：`payload["header"]["token"]` 或 `payload["token"]`，`hmac.compare_digest`（`:2708-2717`）；
- URL 验证：`type == "url_verification"` **先过 token** 再回 `{"challenge": ...}`（`:2719-2722`）；
- 签名：`x-lark-signature == sha256(timestamp + nonce + encrypt_key + body)`（`:2759-2771`）；
- 成功回包 `{"code": 0, "msg": "ok"}`（`:2746`）；拒绝回 401（`:2716-2717 / :2725-2726`）。

**回包格式上的唯一差异**（本模块有意为之）：Hermes 的拒绝回**纯文本**，这里统一回 JSON
（`{"code":<status>,"msg":...}`），便于校验与单测；**状态码与判据逐条对齐**。

## 本地自测

不联网、无凭据即可跑通整条链路：见 `tests/channels/test_feishu_stub.py`
（起真 `ThreadingHTTPServer` → POST 一份合造的飞书回调 → 断言进会话路径、回包格式、
非法签名 401、challenge 回显）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import urlparse

from scholarscout.rpc.methods import AppContext, dispatch

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 常量（对齐 Hermes）

#: 回调路径（Hermes 默认 `/feishu/webhook`，adapter.py:151）
DEFAULT_PATH = "/feishu/webhook"
#: body 上限 1 MB（Hermes `_FEISHU_WEBHOOK_MAX_BODY_BYTES`，adapter.py:155）
MAX_BODY_BYTES = 1 * 1024 * 1024
#: 限速滑动窗口：60s / 120 次（Hermes，adapter.py:156-157）
RATE_WINDOW_SECONDS = 60
RATE_LIMIT_MAX = 120
#: 成功回包（Hermes，adapter.py:2746）
SUCCESS_BODY: dict[str, Any] = {"code": 0, "msg": "ok"}
#: 事件类型
RECEIVE_EVENT = "im.message.receive_v1"
CHALLENGE_EVENT = "url_verification"

Sender = Callable[[str, str], dict[str, Any]]
Dispatcher = Callable[[AppContext, str, Any], Any]


# ---------------------------------------------------------------- 接入向导用到的常量与探测（docs/18）

#: 飞书开放平台入口（照 Hermes `_ONBOARD_OPEN_URLS`，adapter.py:202）
OPEN_ENTRY_URLS: dict[str, str] = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}
#: 新建机器人的 launcher 页模板。URL 与三个参数来自用户实测的终端原文
#: （`https://open.feishu.cn/page/launcher?user_code=Z2BC-3GAP&from=hermes&tp=hermes`）；
#: `from` / `tp` 的真实语义**未在源码/文档读到**（docs/18 §② 标 unknown）——只当可配置参数，不当接口契约。
LAUNCHER_URL_TEMPLATE = (
    "https://open.feishu.cn/page/launcher?user_code={user_code}&from={from_}&tp={tp}"
)

#: 设备码注册里 `from`/`tp` 的可配置来源（`docs/18 §②` 标 unknown，故做成参数而非契约）。
#: 键名与 Hermes 里固定的取值同形，进程内向导与 RPC 都读这两个环境变量。
REGISTRATION_FROM_ENV = "FEISHU_REGISTRATION_FROM"
REGISTRATION_TP_ENV = "FEISHU_REGISTRATION_TP"
DEFAULT_REGISTRATION_FROM = "scholarscout"
DEFAULT_REGISTRATION_TP = "scholarscout"

#: 支持的连接模式。本仓库渠道层**只实现 webhook**（见模块顶注）；websocket 仅记录。
SUPPORTED_CONNECTION_MODES = ("webhook", "websocket")
DEFAULT_CONNECTION_MODE = "webhook"
#: 群聊策略：mention=仅被 @ 时响应 / disabled=关闭（对齐参照体验的二选一）
SUPPORTED_GROUP_POLICIES = ("mention", "disabled")
DEFAULT_GROUP_POLICY = "mention"
#: 私聊（DM）授权策略：pairing=配对审批（默认，未授权不跑会话）/ all=放行全部 / allowlist=仅白名单
SUPPORTED_DM_POLICIES = ("pairing", "all", "allowlist")
DEFAULT_DM_POLICY = "pairing"


def open_entry_url(domain: str = "feishu") -> str:
    """飞书 (feishu) / Lark (lark) 的开放平台入口（**真实可打开的官网入口**）。"""
    return OPEN_ENTRY_URLS.get(domain, OPEN_ENTRY_URLS["feishu"])


def launcher_url(
    user_code: str,
    *,
    from_: str = "scholarscout",
    tp: str = "scholarscout",
    domain: str = "feishu",
) -> str:
    """拼"新建机器人"的 launcher 链接。

    `user_code` 由飞书的设备码注册接口签发（Hermes: `_begin_registration` 的
    `verification_uri_complete`，adapter.py:3937）。P0 不联网拿不到真码，调用方传
    占位符即可 —— **绝不伪造设备码**。
    """
    url = LAUNCHER_URL_TEMPLATE.format(user_code=user_code, from_=from_, tp=tp)
    return url.replace("open.feishu.cn", "open.larksuite.com") if domain == "lark" else url


def connection_mode_from_env() -> str:
    """渠道层的连接模式（读 `.env` 的 `FEISHU_CONNECTION_MODE`，键名照 Hermes adapter.py:1311）。"""
    mode = (os.environ.get("FEISHU_CONNECTION_MODE") or DEFAULT_CONNECTION_MODE).strip().lower()
    return mode if mode in SUPPORTED_CONNECTION_MODES else DEFAULT_CONNECTION_MODE


def group_policy_from_env() -> str:
    """渠道层的群聊策略（读 `.env` 的 `FEISHU_GROUP_POLICY`）。"""
    policy = (os.environ.get("FEISHU_GROUP_POLICY") or DEFAULT_GROUP_POLICY).strip().lower()
    return policy if policy in SUPPORTED_GROUP_POLICIES else DEFAULT_GROUP_POLICY


def dm_policy_from_env() -> str:
    """渠道层的私聊策略（读 `.env` 的 `FEISHU_DM_POLICY`；与 `rpc/feishu.py` 同名键）。"""
    policy = (os.environ.get("FEISHU_DM_POLICY") or DEFAULT_DM_POLICY).strip().lower()
    return policy if policy in SUPPORTED_DM_POLICIES else DEFAULT_DM_POLICY


def bot_open_id_from_env() -> str:
    """机器人自身 open_id（键名照 Hermes `FEISHU_BOT_OPEN_ID`，adapter.py:1316）。"""
    return os.environ.get("FEISHU_BOT_OPEN_ID", "").strip()


def bot_name_from_env() -> str:
    """机器人名字（键名照 Hermes `FEISHU_BOT_NAME`，adapter.py:1318）。"""
    return os.environ.get("FEISHU_BOT_NAME", "").strip()


def registration_from_env() -> str:
    """设备码注册 URL 里的 `from`（默认 `scholarscout`，可用 `FEISHU_REGISTRATION_FROM` 覆盖）。"""
    return (
        os.environ.get(REGISTRATION_FROM_ENV) or DEFAULT_REGISTRATION_FROM
    ).strip() or DEFAULT_REGISTRATION_FROM


def registration_tp_from_env() -> str:
    """设备码注册 URL 里的 `tp`（默认 `scholarscout`，可用 `FEISHU_REGISTRATION_TP` 覆盖）。"""
    return (
        os.environ.get(REGISTRATION_TP_ENV) or DEFAULT_REGISTRATION_TP
    ).strip() or DEFAULT_REGISTRATION_TP


def probe_listener(host: str = "127.0.0.1", port: int = 8765) -> tuple[bool, str]:
    """试绑回调端口，成功后立即释放 —— 用来确认"渠道层起得来"。

    只做 `bind` / `close`，**不 serve**（避免真起服务）；端口被占就如实报错。
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, int(port)))
    except OSError as exc:
        return False, f"端口 {host}:{port} 不可用：{exc}"
    finally:
        sock.close()
    return True, f"端口 {host}:{port} 可用"


@dataclass(frozen=True)
class FeishuCredentials:
    """飞书凭据（**只从 .env / 环境变量读**，绝不硬编码、绝不入库）。"""

    app_id: str = ""
    app_secret: str = ""
    verification_token: str = ""
    encrypt_key: str = ""

    @property
    def has_webhook_secret(self) -> bool:
        """webhook 模式至少要一个密钥才能校签（照 Hermes adapter.py:1417-1419）。"""
        return bool(self.verification_token or self.encrypt_key)

    @property
    def can_send(self) -> bool:
        """能否走飞书 REST 发消息（要有 app_id + app_secret）。"""
        return bool(self.app_id and self.app_secret)


def credentials_from_env() -> FeishuCredentials:
    """从环境变量取凭据（`.env` 由 `config.load_dotenv` 加载进 `os.environ`）。"""
    return FeishuCredentials(
        app_id=os.environ.get("FEISHU_APP_ID", "").strip(),
        app_secret=os.environ.get("FEISHU_APP_SECRET", "").strip(),
        verification_token=os.environ.get("FEISHU_VERIFICATION_TOKEN", "").strip(),
        encrypt_key=os.environ.get("FEISHU_ENCRYPT_KEY", "").strip(),
    )


@dataclass(frozen=True)
class FeishuMessage:
    """一条飞书文本消息（本模块只认文本，第一版够用）。

    `chat_type`（`p2p` / `group`）与 `mentions`（消息里 @ 到的人）是**运行时拦截**的依据
    （docs/17 §3.4）：群聊按 `@提及` 策略、私聊按 DM 策略。两者缺省即"私聊、没 @ 谁"。
    """

    chat_id: str
    text: str
    message_id: str
    sender_open_id: str
    chat_type: str = ""
    #: 消息 @ 到的人：`(open_id, name)` 列表（顺序照飞书原样），用于"是否 @ 了机器人"的判定
    mentions: tuple[tuple[str, str], ...] = ()


# ------------------------------------------------------------------ 纯函数（可单独测）


def compute_signature(timestamp: str, nonce: str, encrypt_key: str, body_bytes: bytes) -> str:
    """`sha256(timestamp + nonce + encrypt_key + body)`（照 Hermes adapter.py:2759-2771）。"""
    body_str = body_bytes.decode("utf-8", errors="replace")
    return hashlib.sha256(f"{timestamp}{nonce}{encrypt_key}{body_str}".encode()).hexdigest()


def signature_valid(headers: Mapping[str, str], body_bytes: bytes, encrypt_key: str) -> bool:
    """定时安全地比对 `x-lark-signature`（缺任一要素即 False，不抛异常；取头大小写无关）。"""
    lowered = {str(k).lower(): v for k, v in headers.items()}
    timestamp = str(lowered.get("x-lark-request-timestamp", "") or "")
    nonce = str(lowered.get("x-lark-request-nonce", "") or "")
    signature = str(lowered.get("x-lark-signature", "") or "")
    if not timestamp or not nonce or not signature:
        return False
    try:
        computed = compute_signature(timestamp, nonce, encrypt_key, body_bytes)
        return hmac.compare_digest(computed.encode(), signature.encode())
    except Exception:  # pragma: no cover - 防御性兜底
        return False


def token_valid(payload: Mapping[str, Any], verification_token: str) -> bool:
    """比对 body/header 里的 `token`（照 Hermes adapter.py:2708-2717）。"""
    header = payload.get("header")
    token = ""
    if isinstance(header, Mapping):
        token = str(header.get("token") or "")
    if not token:
        token = str(payload.get("token") or "")
    if not token:
        return False
    return hmac.compare_digest(token.encode(), verification_token.encode())


def is_challenge(payload: Mapping[str, Any]) -> bool:
    return str(payload.get("type") or "") == CHALLENGE_EVENT


def challenge_body(payload: Mapping[str, Any]) -> dict[str, Any]:
    """URL 验证的回包（照 Hermes adapter.py:2719-2722）。"""
    return {"challenge": payload.get("challenge", "")}


def _text_from_content(raw: Any) -> str:
    """飞书 `message.content` 是**一个 JSON 字符串**（`{"text": "..."}`）。照 Hermes 解包。"""
    if isinstance(raw, Mapping):
        parsed: Any = dict(raw)
    else:
        try:
            parsed = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            return str(raw or "")
    if isinstance(parsed, Mapping):
        return str(parsed.get("text", "") or "")
    return str(parsed or "")


def parse_message_event(payload: Mapping[str, Any]) -> FeishuMessage | None:
    """把 `im.message.receive_v1` 事件解成文本消息；非文本/非消息事件回 None。"""
    header = payload.get("header")
    event_type = ""
    if isinstance(header, Mapping):
        event_type = str(header.get("event_type") or "")
    if event_type != RECEIVE_EVENT:
        return None
    event = payload.get("event")
    message = event.get("message") if isinstance(event, Mapping) else None
    if not isinstance(message, Mapping):
        return None
    if str(message.get("message_type") or "").strip().lower() != "text":
        return None
    text = _text_from_content(message.get("content"))
    if not text.strip():
        return None
    sender = event.get("sender") if isinstance(event, Mapping) else None
    open_id = ""
    if isinstance(sender, Mapping):
        sender_id = sender.get("sender_id")
        if isinstance(sender_id, Mapping):
            open_id = str(sender_id.get("open_id") or "")
    return FeishuMessage(
        chat_id=str(message.get("chat_id") or ""),
        text=text,
        message_id=str(message.get("message_id") or ""),
        sender_open_id=open_id,
        chat_type=str(message.get("chat_type") or ""),
        mentions=_parse_mentions(message.get("mentions")),
    )


def _parse_mentions(raw: Any) -> tuple[tuple[str, str], ...]:
    """解析 `message.mentions`（`[{"key":..., "id":{"open_id":...}, "name":...}]`）。

    Hermes 的 `_message_mentions_bot` 按 `open_id > user_id > name` 判定（`adapter.py:3318-3322`）；
    本模块只存 `(open_id, name)` 两种，判定在 :func:`bot_mentioned` 里做。
    """
    if not isinstance(raw, list):
        return ()
    mentions: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        mention_id = item.get("id")
        open_id = ""
        if isinstance(mention_id, Mapping):
            open_id = str(mention_id.get("open_id") or "")
        mentions.append((open_id, str(item.get("name") or "")))
    return tuple(mentions)


#: 未授权私聊时回的配对提示（docs/17 §3.4 / docs/18 §③：用户把向导生成的码发回来）
PAIRING_PROMPT = (
    "你好～ 我还没被授权为任何人服务。\n"
    "请在机器人主人那侧的 ScholarScout 里运行「飞书接入向导 / 设置页」生成一枚配对码，"
    "然后把那 8 位码**直接私聊发给我**，主人确认后即可使用。"
)
#: 收到配对码并成功登记后的回执（不跑会话）
PAIRING_CLAIMED_REPLY = "收到配对码，已登记。请在机器人主人那侧确认配对（只需点一下批准）。"


@dataclass(frozen=True)
class InterceptDecision:
    """一条消息的处置决定（运行时拦截的出口，可单独断言）。

    `action`：`run`（跑会话）/ `pair`（只回配对提示，不跑会话）/ `drop`（丢弃并记日志）。
    `reason` 用于日志与测试；`reply` 是 `pair` 时回给用户的文本。
    """

    action: str  # "run" | "pair" | "drop"
    reason: str = ""
    reply: str = ""


def bot_mentioned(message: FeishuMessage, *, bot_open_id: str = "", bot_name: str = "") -> bool:
    """消息是否 @ 了机器人。

    判定优先级照 Hermes `_FeishuBotIdentity.matches`（`adapter.py:3318-3322`）：
    `open_id` 优先；`name` 作兜底（仅在配置了 bot 名时精确比对）。两者都没配 → 一律 False
    （宁可漏过、也不把"没 @ 谁"当成"@ 了机器人"）。
    """
    for open_id, name in message.mentions:
        if bot_open_id and open_id and open_id == bot_open_id:
            return True
        if not bot_open_id and bot_name and name and name == bot_name:
            return True
    return False


def authorize_message(
    message: FeishuMessage,
    *,
    dm_policy: str,
    group_policy: str,
    approved: bool,
    temp_open: bool = False,
    bot_open_id: str = "",
    bot_name: str = "",
) -> InterceptDecision:
    """运行时拦截的核心（纯函数，便于正反例桩测；docs/17 §3.4）。

    规则（对齐参照体验）：
    - **群聊**：`disabled` 一律丢弃；`mention` 仅在被 @ 时跑，否则丢弃；
    - **私聊**：`all` 放行；`allowlist` 只放行白名单，名单外丢弃；`pairing`（默认）**未授权不跑会话**，
      只回配对提示（`approved` 或「临时放行」时放行）。
    """
    if message.chat_type == "group":
        if group_policy == "disabled":
            return InterceptDecision("drop", "group_disabled")
        if not bot_mentioned(message, bot_open_id=bot_open_id, bot_name=bot_name):
            return InterceptDecision("drop", "group_no_mention")
        return InterceptDecision("run", "group_mentioned")

    if dm_policy == "all":
        return InterceptDecision("run", "dm_all")
    if dm_policy == "allowlist":
        if approved:
            return InterceptDecision("run", "dm_allowlisted")
        return InterceptDecision("drop", "not_allowlisted")
    # 默认：pairing —— 未授权只回配对提示，绝不执行会话
    if approved:
        return InterceptDecision("run", "dm_paired")
    if temp_open:
        return InterceptDecision("run", "dm_temp_open")
    return InterceptDecision("pair", "dm_unpaired", reply=PAIRING_PROMPT)


class RecordingSender:
    """无凭据/本地桩时的默认发送器：**不假装发成功**，只记录 + 告警。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def __call__(self, chat_id: str, text: str) -> dict[str, Any]:
        self.sent.append((chat_id, text))
        logger.warning("[Feishu] 未注入发送器：回复未真正发往飞书（只记录）chat_id=%s", chat_id)
        return {"code": 0, "recorded": True, "chat_id": chat_id}


# ------------------------------------------------------------------ 回调核心（与传输解耦）


class FeishuWebhook:
    """回调处理核心：**传输无关**（不碰 socket），所以能直接单测。"""

    def __init__(
        self,
        ctx: AppContext,
        *,
        credentials: FeishuCredentials | None = None,
        sender: Sender | None = None,
        dispatcher: Dispatcher = dispatch,
        source: str = "feishu",
        rate_limit_max: int = RATE_LIMIT_MAX,
        rate_window_s: int = RATE_WINDOW_SECONDS,
        bot_open_id: str | None = None,
        bot_name: str | None = None,
    ) -> None:
        self._ctx = ctx
        self._credentials = credentials or credentials_from_env()
        self._dispatcher = dispatcher
        self._source = source
        self._rate_limit_max = rate_limit_max
        self._rate_window_s = rate_window_s
        self._rate_counts: dict[str, list[float]] = {}
        self._rate_lock = threading.Lock()
        #: 机器人自身身份（群聊 @提及 判定用）。默认读 `.env`，可注入替换（桩测）。
        self._bot_open_id = bot_open_id if bot_open_id is not None else bot_open_id_from_env()
        self._bot_name = bot_name if bot_name is not None else bot_name_from_env()

        if sender is not None:
            self._sender: Sender = sender
        else:
            self._recorder = RecordingSender()
            self._sender = self._recorder

        if not self._credentials.has_webhook_secret:
            logger.warning(
                "[Feishu] 未配置 FEISHU_VERIFICATION_TOKEN / FEISHU_ENCRYPT_KEY："
                "回调**不校签**（仅适用于本地桩自测，勿用于公网）"
            )

    @property
    def credentials(self) -> FeishuCredentials:
        return self._credentials

    def _decide(self, message: FeishuMessage) -> tuple[InterceptDecision, dict[str, Any]]:
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

    def _try_claim(self, message: FeishuMessage) -> bool:
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

    def _run_turn(self, message: FeishuMessage) -> None:
        """一条消息 = 一次会话请求：**先过运行时拦截**，再幂等拿会话 → 跑一轮 → 回包。

        拦截顺序（对齐 `docs/17 §3.4`）：
        `drop` → 记日志丢弃；未授权私聊 → **先给一次配对码登记机会**（登记成功只回执、不跑会话），
        否则 `pair` 回配对提示、`run`（含"临时放行"）才真跑。
        """
        decision, access = self._decide(message)
        if decision.action == "drop":
            logger.warning(
                "[Feishu] 消息被丢弃（%s）chat_id=%s sender=%s",
                decision.reason,
                message.chat_id,
                message.sender_open_id,
            )
            return
        # 未授权私聊：先尝试把它当配对码登记（临时放行期间同样先试一次，否则码会被当普通消息跑掉）
        if (
            message.chat_type != "group"
            and not bool(access.get("approved"))
            and self._try_claim(message)
        ):
            return
        if decision.action == "pair":
            self._sender(message.chat_id, decision.reply or PAIRING_PROMPT)
            return

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

    def _check_rate(self, rate_key: str) -> bool:
        """滑动窗口限速（键 `app_id:path:remote_ip`，照 Hermes adapter.py:2776-2785）。"""
        import time

        now = time.time()
        with self._rate_lock:
            hits = [t for t in self._rate_counts.get(rate_key, []) if now - t < self._rate_window_s]
            if len(hits) >= self._rate_limit_max:
                self._rate_counts[rate_key] = hits
                return False
            hits.append(now)
            self._rate_counts[rate_key] = hits
            return True

    def handle(
        self, body_bytes: bytes, headers: Mapping[str, str], *, remote_ip: str = "unknown"
    ) -> tuple[int, dict[str, Any]]:
        """处理一条回调请求，返回 `(http_status, body_dict)`。"""
        rate_key = f"{self._credentials.app_id}:{DEFAULT_PATH}:{remote_ip}"
        if not self._check_rate(rate_key):
            logger.warning("[Feishu] 回调被限速：%s", remote_ip)
            return 429, {"code": 429, "msg": "Too Many Requests"}

        if len(body_bytes) > MAX_BODY_BYTES:
            return 413, {"code": 413, "msg": "Request body too large"}

        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return 400, {"code": 400, "msg": "invalid json"}
        if not isinstance(payload, dict):
            return 400, {"code": 400, "msg": "payload must be a JSON object"}

        # 第 1 层：verification token（有配才校）
        if self._credentials.verification_token and not token_valid(
            payload, self._credentials.verification_token
        ):
            logger.warning("[Feishu] 回调被拒：verification token 不对（%s）", remote_ip)
            return 401, {"code": 401, "msg": "Invalid verification token"}

        # URL 验证：**先过 token 再回 challenge**（防未授权者证明自己控制端点）
        if is_challenge(payload):
            return 200, challenge_body(payload)

        # 第 2 层：签名（有配才校）
        if self._credentials.encrypt_key and not signature_valid(
            headers, body_bytes, self._credentials.encrypt_key
        ):
            logger.warning("[Feishu] 回调被拒：签名不对（%s）", remote_ip)
            return 401, {"code": 401, "msg": "Invalid signature"}

        if payload.get("encrypt"):
            return 400, {"code": 400, "msg": "encrypted payloads not supported"}

        message = parse_message_event(payload)
        if message is not None:
            try:
                self._run_turn(message)
            except Exception as exc:  # 事件处理失败也要回 200（否则飞书会重试风暴）
                logger.warning("[Feishu] 处理消息事件失败（已回 200 结束重试）：%s", exc)
        return 200, dict(SUCCESS_BODY)


# ------------------------------------------------------------------ stdlib HTTP 监听


class _FeishuHTTPServer(ThreadingHTTPServer):
    """把 webhook 与路径挂到 server 上，handler 用 `cast` 取（不用动态属性 + type: ignore）。"""

    daemon_threads = True
    webhook: FeishuWebhook
    webhook_path: str


class _FeishuHandler(BaseHTTPRequestHandler):
    server_version = "ScholarScoutFeishu/0.1"
    protocol_version = "HTTP/1.1"

    def _write(self, status: int, payload: Mapping[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # http.server 规定的名字
        server = cast(_FeishuHTTPServer, self.server)
        if urlparse(self.path).path != server.webhook_path:
            self._write(404, {"code": 404, "msg": "not found"})
            return
        content_type = str(self.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
        if content_type and content_type != "application/json":
            self._write(415, {"code": 415, "msg": "Unsupported Media Type"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._write(413, {"code": 413, "msg": "Request body too large"})
            return
        body = self.rfile.read(length) if length else b""
        remote_ip = str(self.client_address[0]) if self.client_address else "unknown"
        status, payload = server.webhook.handle(
            body, {k.lower(): v for k, v in self.headers.items()}, remote_ip=remote_ip
        )
        self._write(status, payload)

    def log_message(self, *args: Any) -> None:  # 静音默认 stderr 请求日志
        return


class FeishuCallbackServer:
    """`ThreadingHTTPServer` 包装：只绑给定 host（默认 127.0.0.1），起后台线程。"""

    def __init__(
        self,
        webhook: FeishuWebhook,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        path: str = DEFAULT_PATH,
    ) -> None:
        self._httpd = _FeishuHTTPServer((host, port), _FeishuHandler)
        self._httpd.webhook = webhook
        self._httpd.webhook_path = path
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    @property
    def path(self) -> str:
        return self._httpd.webhook_path

    def start(self) -> None:
        """起后台线程 `serve_forever`（daemon，不挡进程退出）。"""
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="feishu-callback", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def build_server(
    ctx: AppContext,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    path: str = DEFAULT_PATH,
    credentials: FeishuCredentials | None = None,
    sender: Sender | None = None,
    dispatcher: Dispatcher = dispatch,
) -> FeishuCallbackServer:
    """装配一个回调服务（**不启动**）：`FeishuWebhook` + `FeishuCallbackServer`。"""
    webhook = FeishuWebhook(ctx, credentials=credentials, sender=sender, dispatcher=dispatcher)
    return FeishuCallbackServer(webhook, host=host, port=port, path=path)


# ------------------------------------------------------------------ 监听入口（docs/17 §3.8）

#: 启动日志的模板：**明确打印监听地址与路径**，方便与内网穿透工具对接。
#: 例：`[Feishu] listening on 127.0.0.1:8765/feishu/webhook`
LISTEN_LOG_TEMPLATE = "[Feishu] listening on {host}:{port}{path}"
#: 第二行提示（穿透工具要映射的本地地址）。
LISTEN_HINT_TEMPLATE = (
    "[Feishu] 内网穿透请把公网地址映射到 http://{host}:{port}{path}，"
    "再把该公网 URL 填进飞书后台事件订阅（详见 docs/17 §3.8）"
)


def run_listener(
    ctx: AppContext,
    *,
    sender: Sender | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    path: str = DEFAULT_PATH,
    credentials: FeishuCredentials | None = None,
    dispatcher: Dispatcher = dispatch,
    say: Callable[[str], None] = print,
    block: bool = True,
) -> FeishuCallbackServer:
    """起回调监听并**打印监听地址与路径**（`docs/17 §3.8`），返回已启动的 server。

    - 只绑 `host`（默认 `127.0.0.1`）：公网交给内网穿透/反代，进程本身不直接暴露；
    - `sender` 由调用方注入（真实部署注入 `fetch/feishu_send.build_sender`；缺省即 `RecordingSender`，
      **不假装发成功**）；
    - 打印 `LISTEN_LOG_TEMPLATE`（如 `[Feishu] listening on 127.0.0.1:8765/feishu/webhook`）
      + `LISTEN_HINT_TEMPLATE`；日志同时经 `logger.info` 落盘；
    - `block=True`（CLI 入口）阻塞到 Ctrl-C；`block=False` 立即返回（桩测用）。
    """
    import time as _time

    server = build_server(
        ctx,
        host=host,
        port=port,
        path=path,
        credentials=credentials,
        sender=sender,
        dispatcher=dispatcher,
    )
    server.start()
    bound_host, bound_port = server.address
    line = LISTEN_LOG_TEMPLATE.format(host=bound_host, port=bound_port, path=server.path)
    hint = LISTEN_HINT_TEMPLATE.format(host=bound_host, port=bound_port, path=server.path)
    logger.info(line)
    logger.info(hint)
    say(line)
    say(hint)
    if block:
        try:
            while True:
                _time.sleep(3600)
        except KeyboardInterrupt:  # Ctrl-C：优雅收尾
            say("[Feishu] 收到中断，正在停止监听…")
            server.stop()
    return server
