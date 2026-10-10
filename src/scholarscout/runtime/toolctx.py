"""工具执行期上下文。

**约定**：工具 handler 的签名是 ``handler(ctx: ToolContext, **arguments)``。
模型只填 ``arguments``（由 JSON Schema 约束），依赖（库、配置、出网、模型）由 loop 注入。
这样工具函数不需要 import 全局单例，测试里塞桩也方便。
如果注册的工具没有 ctx 参数，:func:`scholarscout.runtime.registry.invoke` 会直接报错。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from scholarscout.config import Config
from scholarscout.fetch.http import HttpClient
from scholarscout.providers.llm import LlmClient, LlmError
from scholarscout.state.db import Database

#: 事件回调：``emit(kind, payload)``（与 ``runtime/loop.Emitter`` 同形；此处本地定义以免反向依赖）
Emitter = Callable[[str, dict[str, Any]], None]


@dataclass
class TurnBudget:
    """**本轮工具预算账** —— 主循环与子任务（``research.delegate``）共用**同一本账**。

    为什么要有它（而不是像原来那样只用 ``loop`` 里的 ``len(executed)`` 计数）：把顺序子任务做成
    模型可见的工具后，子任务内部的工具调用**必须从主循环的剩余预算里扣** —— 一次委派花掉的额度，
    要占用主循环后续的工具调用次数。用 `total/used` 一本账，主循环与委派都只看 ``remaining``。

    - 主循环每执行一次工具：``spend(1)``；
    - 委派跑完：``spend(sum(len(result.tool_calls)))`` —— **回写**到同一对象；
    - ``spend`` 会夹住（不会扣成负数），所以账目恒满足 ``0 <= used <= total``。

    纯数据 + 纯方法，可在测试里直接断言"扣了多少、还剩多少"。
    """

    total: int
    used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.used)

    def can_spend(self, n: int) -> bool:
        return 0 <= n <= self.remaining

    def spend(self, n: int) -> int:
        """扣 ``n``，返回**实际扣掉**的数量（不足则扣到账底为止）。"""
        granted = min(max(0, int(n)), self.remaining)
        self.used += granted
        return granted


@dataclass
class ToolContext:
    db: Database
    config: Config
    http: HttpClient | None = None
    llm: LlmClient | None = None
    session_id: str | None = None
    turn_id: str | None = None
    #: 本轮工具预算账（见 :class:`TurnBudget`）；主循环会注入，子任务工具据此扣主预算。
    budget: TurnBudget | None = None
    #: 本轮事件回调（子任务工具用它发 ``chat.subtask_*``）；无 GUI 时为空。
    emit: Emitter | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def require_http(self) -> HttpClient:
        if self.http is None:
            raise LlmError("network", "出网客户端未装配（app.build_context 应注入 HttpClient）")
        return self.http

    def require_llm(self) -> LlmClient:
        if self.llm is None:
            raise LlmError("auth", "未配置模型（把 LLM_API_KEY 写进 .env）")
        return self.llm
