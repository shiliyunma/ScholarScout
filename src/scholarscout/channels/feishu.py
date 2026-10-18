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

import json
import logging
import os
import re
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from scholarscout.config import DEFAULT_FEISHU_ACK_TEXT
from scholarscout.rpc.registry import dispatch

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 常量

#: 唯一一条入站事件：收到消息
RECEIVE_EVENT = "im.message.receive_v1"

#: 「发文件」走后端这条 RPC（docs/17 §3.12）：渠道层只解析意图，查库/导出/上传/发送在主进程
FILE_DELIVERY_METHOD = "channel.send_file"

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

#: 支持的连接模式：**只有 websocket**（入站实现在 `feishu_ws.py`）。
#: 历史上两种都"认"，但只有 webhook 能跑；现在反过来 —— `webhook` 不再被接受，
#: 配了会**告警并回落到 websocket**（见 `connection_mode_from_env`）。
SUPPORTED_CONNECTION_MODES = ("websocket",)
DEFAULT_CONNECTION_MODE = "websocket"
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
    """渠道层的连接模式（读 `.env` 的 `FEISHU_CONNECTION_MODE`，键名照 Hermes adapter.py:1311）。

    **只认 websocket**（唯一实现）。配了别的值（含历史遗留的 `webhook`）一律回落到 websocket 并**告警**：
    静默按另一种模式跑起来，比当场说清楚更难查 —— 那正是"以为在收事件、其实一个都收不到"的来源。
    """
    raw = (os.environ.get("FEISHU_CONNECTION_MODE") or DEFAULT_CONNECTION_MODE).strip().lower()
    if raw in SUPPORTED_CONNECTION_MODES:
        return raw
    logger.warning(
        "[Feishu] FEISHU_CONNECTION_MODE=%s 不受支持（本项目只实现 websocket 长连接），按 websocket 处理",
        raw,
    )
    return DEFAULT_CONNECTION_MODE


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


# ---------------------------------------------------------------- 入站即时回执（docs/17 §3.11）

#: 回执文案的**注入通道**（不是用户配置项）：用户在 `config.yaml` 里改 `runtime.feishu_ack_text`，
#: 父进程起渠道子进程时把它写进这个环境变量（`channels/supervisor.py::config_env`）。
#: 渠道层读不到 `config.yaml`（子进程没有配置对象），所以必须有这么一条注入通道 ——
#: 与 `FEISHU_DM_POLICY` / 凭据的走法一致（环境继承）。
ACK_TEXT_ENV = "FEISHU_ACK_TEXT"

#: 回执去重窗口：只记最近这么多条 `message_id`（飞书重推都紧跟着原事件，窗口够用）。
#: 定成有界是**必须的**：长连接进程跑几天，无界集合就是一条内存泄漏。
ACK_DEDUPE_SIZE = 256


def ack_text_from_env() -> str:
    """回执文案：环境变量优先，缺省回 `config.DEFAULT_FEISHU_ACK_TEXT`（"已接受消息，正在处理中"）。

    **空串是合法值**（= 关掉回执），所以不能用 `or` 兜默认 —— 那会把"用户显式关掉"吃掉。
    """
    raw = os.environ.get(ACK_TEXT_ENV)
    if raw is None:
        return DEFAULT_FEISHU_ACK_TEXT
    return raw.strip()


class RecentMessageIds:
    """**有界**的 `message_id` 去重器（回执用）：`mark()` 返回 True 表示"头一回见"。

    为什么必须有：飞书会**重推同一事件**（长连接重连 / 平台重试），一条消息回两次回执
    用户就会看到双份"已接受消息"。去重键用飞书的 `message_id`（同一条消息稳定不变）。

    **拿不到 id 时不判重**（`mark("")` 恒 True）：宁可多回一条，也绝不能把后面所有消息
    都当成"重复"吞掉 —— 那是一条静默失聪的链路。

    线程安全：SDK 的收包回调跑在它自己的线程里，可能并发进来。
    """

    def __init__(self, maxsize: int = ACK_DEDUPE_SIZE) -> None:
        self._maxsize = max(1, int(maxsize))
        self._lock = threading.Lock()
        self._order: deque[str] = deque()
        self._seen: set[str] = set()

    def mark(self, key: str) -> bool:
        """记下这个 key；返回 True 表示首次见到（调用方据此决定"要不要回执"）。"""
        if not key:
            return True
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            self._order.append(key)
            while len(self._order) > self._maxsize:
                self._seen.discard(self._order.popleft())
            return True


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
    """把 `im.message.receive_v1` 事件解成文本消息；非文本/非消息事件回 None。

    `payload` 就是**长连接回调收到的那份报文**（`{"header": {...}, "event": {...}}`，形状与
    webhook body 一致，见 Hermes adapter.py:2749-2757 的事件处理表）。缺 `header.event_type`
    时按本函数唯一支持的事件类型处理（长连接只订阅了它，见 `channels/feishu_ws.py`）。
    """
    header = payload.get("header")
    event_type = ""
    if isinstance(header, Mapping):
        event_type = str(header.get("event_type") or "")
    if event_type and event_type != RECEIVE_EVENT:
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


# ---------------------------------------------------------------- 「发文件」意图（规则识别，不新增工具）
#
# 用户拍板：「如果可以发送文件，请开发发送文件的渠道」+「生成物和已入库的资料都应该可以发」。
# 做法是**在入站路径里用规则解析**，不新增模型可见的工具、不改提示词：
# 模型那一侧的结构（工具表 / 系统提示）一个字都不动，`chat.send` 也不会被调用 ——
# 这是个确定性动作（查库 → 上传 → 发送），不需要模型参与，反而更省 token、更不会瞎发。
#
# 三条原则：
# 1. **保守识别**：必须同时有"发送动词"和"文件类目标"，且整句很短（≤40 字）；
#    「给我讲讲学习路线」「为什么要做知识地图」这类会被明确排除（不能让一句话术把问答劫走）。
# 2. **解析不到就不发**：回可读提示 + 候选清单，绝不猜一个文件发出去。
# 3. **权限沿用入站拦截**：文件请求排在 `authorize_message` 之后 —— 未授权的人发不出东西来
#    （否则"发我笔记"就是一条越权读取通道）。

#: 发送类动词（用户原话：「发我 / 给我 / 传我 …文件/文档/PDF/路线/笔记/地图」）
FILE_VERB_RE = re.compile(
    r"(发我|发给我|给我发|发一下|发个|发一份|发份|传我|传给我|发送|导出|来一份|来份|给我)"
)
#: 动词后面紧跟这些词的，是**提问**不是"要文件"（「给我讲讲」「发我解释一下」）
FILE_ASK_RE = re.compile(
    r"^(讲|说|解释|介绍|分析|看看|看一下|读|对比|总结|列|写|做|画|查|搜|算|评|推荐)"
)
#: 目标里必须出现的"文件类"线索（按类别判归属）
FILE_ARTIFACT_HINTS: tuple[tuple[str, str], ...] = (
    (r"地图|主题图|思维导图", "topic"),
    (r"路线|计划|学习安排", "route"),
    (r"笔记|卡片|抽背", "note"),
)
FILE_LIBRARY_RE = re.compile(r"文件|文档|资料|论文|文献|文章|pdf|附件", re.IGNORECASE)
#: 本地路径线索（`data/…`、`D:\…`、`xxx.md`）—— 只认这两种形态，不做"猜路径"
FILE_PATH_RE = re.compile(
    r"(^[A-Za-z]:[\\/]|^data[\\/]|^run[\\/]|[\w\-.]+\.(md|pdf|txt|csv|json|docx?|pptx?|xlsx?)$)",
    re.IGNORECASE,
)
#: 目标里的填充词（"发我**那份**论文" → "论文"）：清掉后剩下的才是搜索词
FILE_FILLER_RE = re.compile(
    r"(一份|那份|这份|这个|那个|那篇|这篇|上次的|刚才的|我的|的|文件|文档|资料|论文|文献|文章|pdf|md|markdown|附件)",
    re.IGNORECASE,
)
#: 句首填充词（"请/帮我/麻烦/把/能不能…"）—— 动词前后的修饰语
FILE_LEAD_RE = re.compile(r"^(请|帮我|帮忙|麻烦|能不能|能|可以|你|再|把|将|我想要|我要|\s)+")
#: 尾部的客套话（"发我路线，谢谢"）：先剥掉，再判"这是不是一句话"
FILE_POLITE_RE = re.compile(r"[，,]\s*(谢谢|多谢|麻烦了|拜托|好吗|可以吗|行吗)[！!。.～~]*$")
#: 目标里出现这些标点 = 它是一句话/一段话，**不是一个可以发的目标**
#: （实例：「发我一下这篇文章的主要贡献，顺便说说它的局限…」——38 字，长度护栏拦不住）
FILE_SENTENCE_RE = re.compile(r"[，,；;！!？?]")


@dataclass(frozen=True)
class FileRequest:
    """一次"要文件"的解析结果（纯数据，可单独断言）。

    `kind`：`artifact`（本会话/库里最近的生成物）/ `library`（已入库资料）/ `path`（本地路径）。
    `query`：`artifact` 时是 `topic|route|note` 或空串（=最近的那件）；`library` 时是标题模糊词；
    `path` 时是用户给的相对/绝对路径。
    """

    kind: str
    query: str = ""
    raw: str = ""


def _strip_lead(text: str) -> str:
    """反复剥掉句首填充词（一次替换可能只剥掉"请帮我"里的"请"）。"""
    result = text.strip()
    for _ in range(3):
        trimmed = FILE_LEAD_RE.sub("", result).strip()
        if trimmed == result:
            break
        result = trimmed
    return result


def _looks_like_target(text: str) -> bool:
    """像不像"一个可以发的目标"：路径 / 生成物 / 入库资料，三类线索之一。"""
    if not text:
        return False
    if FILE_PATH_RE.search(text):
        return True
    if any(re.search(pattern, text) for pattern, _ in FILE_ARTIFACT_HINTS):
        return True
    return bool(FILE_LIBRARY_RE.search(text))


def _classify(target: str) -> FileRequest:
    """把目标短语归到三类之一（顺序即优先级：路径 > 生成物 > 入库资料 > 最近生成物）。"""
    clean = target.strip().strip("。！？!?，,、~～ ")
    if FILE_PATH_RE.search(clean):
        return FileRequest("path", clean.strip("\"'“”"), target)
    for pattern, name in FILE_ARTIFACT_HINTS:
        if re.search(pattern, clean):
            return FileRequest("artifact", name, target)
    if FILE_LIBRARY_RE.search(clean):
        query = re.sub(r"\s+", " ", FILE_FILLER_RE.sub(" ", clean)).strip()
        return FileRequest("library", query, target)
    # 只有动词没有名词（"发我文件"/"发我一份"）：给**最近的那件生成物**
    query = re.sub(r"\s+", " ", FILE_FILLER_RE.sub(" ", clean)).strip()
    return FileRequest("artifact", "", query)


def parse_file_request(text: str) -> FileRequest | None:
    """判断一条消息是不是"给我发某个文件"，是就解析出目标（纯函数、零 IO）。

    能识别的两种语序：
    - **动词在前**：`发我学习路线` / `传我那份 PDF` / `给我 注意力机制 那篇论文`；
    - **目标在前**：`把学习路线发我` / `学习路线发我一下`。

    识别失败（提问、闲聊、超长）一律回 `None` —— 调用方据此走正常问答，**绝不瞎发**。
    """
    raw = (text or "").strip()
    if not raw or len(raw) > 40:
        return None  # 一整段话里夹一句"发我"不算指令（长度是最便宜的护栏）
    body = raw.rstrip("。.!！~～ ")
    match = FILE_VERB_RE.search(body)
    if match is None:
        return None
    head = _strip_lead(body[: match.start()].strip())
    tail = body[match.end() :].strip()
    tail_core = _strip_lead(tail)
    if tail_core:
        if FILE_ASK_RE.match(tail_core):
            return None  # 「给我讲讲学习路线」——这是提问，不是要文件
        if FILE_SENTENCE_RE.search(FILE_POLITE_RE.sub("", tail_core)):
            return None  # 一整句话（有逗号/问号）= 在提问/闲聊，不是在点名一个文件
        if _looks_like_target(tail_core):
            return _classify(tail_core)
        return None
    # 动词在句末：「把学习路线发我」——目标在动词之前
    if head and _looks_like_target(head):
        if FILE_SENTENCE_RE.search(head):
            return None
        return _classify(head)
    return None


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

    `action`：`run`（跑会话）/ `pair`（只回配对提示，不跑会话）/ `drop`（丢弃并记日志）/
    `file`（要文件的确定动作：已交给 `channel.send_file`，不跑模型，见 `docs/17 §3.12`）。
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
