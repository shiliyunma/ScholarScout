"""工具执行期上下文。

**约定**：工具 handler 的签名是 ``handler(ctx: ToolContext, **arguments)``。
模型只填 ``arguments``（由 JSON Schema 约束），依赖（库、配置、出网、模型）由 loop 注入。
这样工具函数不需要 import 全局单例，测试里塞桩也方便。
如果注册的工具没有 ctx 参数，:func:`scholarscout.runtime.registry.invoke` 会直接报错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scholarscout.config import Config
from scholarscout.fetch.http import HttpClient
from scholarscout.providers.llm import LlmClient, LlmError
from scholarscout.state.db import Database


@dataclass
class ToolContext:
    db: Database
    config: Config
    http: HttpClient | None = None
    llm: LlmClient | None = None
    session_id: str | None = None
    turn_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def require_http(self) -> HttpClient:
        if self.http is None:
            raise LlmError("network", "出网客户端未装配（app.build_context 应注入 HttpClient）")
        return self.http

    def require_llm(self) -> LlmClient:
        if self.llm is None:
            raise LlmError("auth", "未配置模型（把 LLM_API_KEY 写进 .env）")
        return self.llm
