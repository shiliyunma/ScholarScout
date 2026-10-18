"""轮次跑完提醒（docs/20 §6.3 第 (2) 条）：**默认关、只私聊、失败不影响轮次**。

为什么单独一个模块：这是**唯一会主动出网**的运行时行为（用户没在等这一轮，也不在界面前）。
所以四条硬约束全部做在机制上，而不是靠调用点"记得"：

1. **默认关**：``enabled=False`` 时 ``notify()`` 第一步就回 ``disabled`` —— 不查库、不出网、
   连收件人都不解析（"关着时一次外网都不发"因此是可断言的事实）；
2. **只私聊、不发群**：发送器的签名是 ``sender(text) -> dict`` —— **没有收件人参数**。
   目标是构造发送器时钉死的**用户 open_id**（``receive_id_type=open_id``），
   群 chat_id 在类型上就没有位置可传。群里发主动消息本来也需要群主授权，项目口径就是不发；
3. **失败不影响轮次**：``notify()`` 自己吞掉一切异常并只记日志；调用点 ``SessionRuntime._finish``
   另外再包一层（发送发生在轮次收尾，一次发送失败绝不该让一轮对话的结论变形）；
4. **同一 ``turn_id`` 只提醒一次 + 频率限制**：按 ``turn_id`` 记已提醒（有界记忆），
   ``min_interval_s``（默认 60s）内最多一条 —— 三个会话同时收尾也只推一条，不刷屏。

**当前活跃会话由调用方传进来**（``active_session_id``），本模块**绝不猜**：缺这个参数就
不发（``no_active_session``）。猜一个"大概不是活跃的"只会把提醒变成噪声。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: 目前只有这一个私聊通道（QQ 通道本项目没有，不假装支持；docs/20 §6.3）
DEFAULT_PLATFORM = "feishu"
#: 同一进程内两条提醒的最小间隔（防刷屏）
DEFAULT_MIN_INTERVAL_S = 60.0
#: 首行摘要长度
PREVIEW_CHARS = 200
#: 已提醒过的 turn_id 记忆条数（有界，长期运行不涨内存）
MAX_SEEN_TURNS = 256

#: **只发私聊**的发送器：参数只有正文。收件人在构造时钉死，调用方无法改目标。
Sender = Callable[[str], dict[str, Any]]
#: 发送器解析器：真正要发时才解析（没配好凭据/没有唯一配对用户 → None，那就一条都不发）
SenderResolver = Callable[[], Sender | None]


def preview(text: str, *, limit: int = PREVIEW_CHARS) -> str:
    """取正文**第一个非空行**当摘要（首行摘要，不是整段截断：首行通常就是结论）。"""
    for line in str(text or "").splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…"
    return "（本轮没有正文）"


def format_duration(seconds: float | None) -> str:
    """时长写法：``12s`` / ``2m13s`` / ``1h05m``（给人看，不给程序解析）。"""
    if seconds is None or seconds < 0:
        return ""
    # ``round()`` 不带 ndigits 时**本来就回 int**（没有多余的 ``int()`` 可加）
    total = round(seconds)
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def build_text(
    *,
    title: str,
    text: str,
    stop_reason: str,
    duration_s: float | None = None,
    limit: int = PREVIEW_CHARS,
) -> str:
    """提醒正文：会话标题 + 收口原因/耗时 + 首行摘要（都不含任何本机路径）。"""
    name = str(title or "").strip() or "（未命名会话）"
    duration = format_duration(duration_s)
    tail = f"，{duration}" if duration else ""
    return f"【ScholarScout】「{name}」这一轮已结束（{stop_reason}{tail}）\n{preview(text, limit=limit)}"


@dataclass(frozen=True)
class NotifyOutcome:
    """一次提醒尝试的结论（**如实**：没发就说为什么没发）。"""

    fired: bool
    reason: str
    text: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "fired": self.fired,
            "reason": self.reason,
            "text": self.text,
            "error": self.error,
        }


class TurnDoneNotifier:
    """一轮跑完 → 非活跃会话 → 私聊一条摘要（开关、去重、频率、失败隔离都在这里）。"""

    def __init__(
        self,
        *,
        enabled: bool = False,
        platform: str = DEFAULT_PLATFORM,
        resolve_sender: SenderResolver | None = None,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        max_seen: int = MAX_SEEN_TURNS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._enabled = bool(enabled)
        self._platform = str(platform or DEFAULT_PLATFORM).strip().lower()
        self._resolve_sender = resolve_sender
        self._min_interval_s = max(0.0, float(min_interval_s))
        self._max_seen = max(1, int(max_seen))
        self._clock = clock
        self._lock = threading.Lock()
        #: 已提醒过的 ``turn_id``（有界 LRU）。**先登记再发送** —— 并发收尾的两轮不会双发。
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._last_sent_at: float | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def platform(self) -> str:
        return self._platform

    def notify(
        self,
        *,
        turn_id: str,
        session_id: str,
        title: str = "",
        text: str = "",
        stop_reason: str = "completed",
        duration_s: float | None = None,
        active_session_id: str | None = None,
    ) -> NotifyOutcome:
        """满足全部条件才发一条；**任何情况都不抛异常**。

        ``reason`` 取值：``sent`` / ``disabled`` / ``no_active_session`` / ``is_active_session`` /
        ``unsupported_platform`` / ``duplicate`` / ``rate_limited`` / ``no_private_target`` /
        ``send_failed``。
        """
        if not self._enabled:
            return NotifyOutcome(False, "disabled")
        # 活跃会话**必须由调用方给**：运行时猜出来的"当前会话"只会在并发下猜错
        active = str(active_session_id or "").strip()
        if not active:
            return NotifyOutcome(False, "no_active_session")
        if active == session_id:
            return NotifyOutcome(False, "is_active_session")
        if self._platform != DEFAULT_PLATFORM:
            # 只实现了飞书这一条私聊通道；别的平台名一律不发（不假装支持）
            logger.warning(
                "[Notify] 不支持的平台 %r（目前只有 %s）", self._platform, DEFAULT_PLATFORM
            )
            return NotifyOutcome(False, "unsupported_platform")

        body = build_text(title=title, text=text, stop_reason=stop_reason, duration_s=duration_s)
        # 解析发送器放在去重之前：没配好收件人时"一条都没发"，不该消耗这次的去重/限频名额
        sender = self._resolve()
        if sender is None:
            logger.info("[Notify] 没有可用的私聊收件人：这一条不发（%s）", session_id)
            return NotifyOutcome(False, "no_private_target", body)

        with self._lock:
            if turn_id in self._seen:
                return NotifyOutcome(False, "duplicate", body)
            now = self._clock()
            if self._last_sent_at is not None and now - self._last_sent_at < self._min_interval_s:
                return NotifyOutcome(False, "rate_limited", body)
            # 先登记再发送：一次真实投递只可能发生一次（去重与限频都按"尝试"计，
            # 失败也不重试 —— 提醒不值得为它再出一次网）
            self._remember(turn_id, now)
            self._last_sent_at = now

        try:
            outcome = sender(body)
        except Exception as exc:  # 发送失败只记日志：绝不影响轮次收尾
            logger.warning("[Notify] 提醒发送异常（已吞）：turn=%s err=%s", turn_id, exc)
            return NotifyOutcome(False, "send_failed", body, f"{type(exc).__name__}: {exc}")

        if isinstance(outcome, Mapping) and outcome.get("ok") is False:
            error = str(outcome.get("error") or "发送失败")
            logger.warning("[Notify] 提醒发送失败：turn=%s %s", turn_id, error)
            return NotifyOutcome(False, "send_failed", body, error)
        logger.info("[Notify] 已推送跑完提醒：session=%s turn=%s", session_id, turn_id)
        return NotifyOutcome(True, "sent", body)

    # --------------------------------------------------------------- 内部

    def _resolve(self) -> Sender | None:
        try:
            return self._resolve_sender() if self._resolve_sender is not None else None
        except Exception as exc:  # 解析失败等同"没有收件人"，同样不该影响轮次
            logger.warning("[Notify] 解析私聊收件人失败（已吞）：%s", exc)
            return None

    def _remember(self, turn_id: str, now: float) -> None:
        """登记已提醒的轮次，超出上限就丢最旧的（调用方已持锁）。"""
        self._seen[turn_id] = now
        self._seen.move_to_end(turn_id)
        while len(self._seen) > self._max_seen:
            self._seen.popitem(last=False)


# --------------------------------------------------------------------- 装配（产品默认路径）


def build_private_resolver(db: Any, http: Any) -> SenderResolver:
    """造一个"只发给**唯一**已配对用户"的解析器。

    - 已配对用户 **0 个或多个** → 回 ``None``（一条都不发）：多个用户时"该推给谁"没有答案，
      猜一个就是把别人的对话摘要发给另一个人；
    - 有且只有一个 → 用 ``fetch/feishu_send.py::build_private_sender`` 钉死他的 ``open_id``，
      签名里没有收件人参数，所以"发群"在类型上不可能；
    - 凭据/出网客户端缺失 → ``build_private_sender`` 自己回 ``None``（同样是"不发"，不是"发了才失败"）。
    """

    def resolve() -> Sender | None:
        from scholarscout.fetch import feishu_send
        from scholarscout.state import pairing

        users = pairing.approved_users(db)
        if len(users) != 1:
            return None
        open_id = str(users[0].get("user_id") or "").strip()
        if not open_id:
            return None
        return feishu_send.build_private_sender(http, open_id=open_id)

    return resolve


def from_config(runtime_cfg: object, *, db: Any, http: Any) -> TurnDoneNotifier | None:
    """按 `runtime.notify_turn_done` 造提醒器；**开关关着回 ``None``**（调用点因此零分支成本）。

    只有飞书一条实现：`platform` 不是 `feishu` 时照样造（保持"配置写错要能被看见"），
    但 `notify()` 会回 `unsupported_platform` 并记一条 warning —— 不假装支持别的平台。
    """
    if not bool(getattr(runtime_cfg, "notify_turn_done", False)):
        return None
    platform = str(
        getattr(runtime_cfg, "notify_turn_done_platform", DEFAULT_PLATFORM) or ""
    ).strip()
    return TurnDoneNotifier(
        enabled=True,
        platform=platform or DEFAULT_PLATFORM,
        resolve_sender=build_private_resolver(db, http),
    )
