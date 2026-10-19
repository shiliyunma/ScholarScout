"""飞书入站报文的解析、去重与放行判定（纯函数层，零 IO；docs/17 §3.3 / §3.4）。

## 它在哪一层

与 `channels/feishu.py` 同层，但这一层**不碰任何外部状态**：输入是长连接回调收到的那份报文
（或已解析的 `FeishuMessage`），输出是 `FeishuMessage` / `InterceptDecision` / 布尔值。
所以它不需要 ctx、不需要 RPC，桩事件（甚至一个 dict）就能测到底
（`tests/channels/test_feishu_stub.py`）。

三件事：

1. **解析**：`im.message.receive_v1` 报文 → `FeishuMessage`（`parse_message_event`）；
2. **判定**：群聊 `@提及` 策略 / 私聊 `pairing|all|allowlist` 策略 → `InterceptDecision`
   （`bot_mentioned` / `authorize_message`）；
3. **去重**：按飞书 `message_id` 记「这条见过了」（`RecentMessageIds`，**有界**、线程安全）。

## 边界

本模块**不 import `rpc`**、不读库、不出网：白名单状态由调用方（`feishu.py::decide`）经
`feishu.access` 取到后**传进来** —— 判定是纯函数，取数才是渠道核的事。

从 `channels/feishu.py` 原样搬出（**只搬位置、没改函数体**）；`feishu.py` 仍重导出这里的全部公开
名字（`RECEIVE_EVENT` / `FeishuMessage` / `InterceptDecision` / `parse_message_event` /
`authorize_message` / `bot_mentioned` / `RecentMessageIds` / `PAIRING_PROMPT` 等），
`from scholarscout.channels.feishu import X` 照旧可用。
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: 唯一一条入站事件：收到消息
RECEIVE_EVENT = "im.message.receive_v1"


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


#: 回执去重窗口：只记最近这么多条 `message_id`（飞书重推都紧跟着原事件，窗口够用）。
#: 定成有界是**必须的**：长连接进程跑几天，无界集合就是一条内存泄漏。
ACK_DEDUPE_SIZE = 256


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
