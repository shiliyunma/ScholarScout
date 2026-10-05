"""应用装配：把配置、数据库、模型客户端组装成一个 :class:`AppContext`。

CLI、stdin/stdout 传输、桌面壳都从这里拿上下文，保证只有一处装配逻辑。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from scholarscout.config import Config, load_config
from scholarscout.providers.llm import LlmClient, LlmError, create_client
from scholarscout.rpc.methods import AppContext
from scholarscout.state.db import Database
from scholarscout.state.migrate import apply_migrations


def build_context(
    *,
    config: Config | None = None,
    db_path: Path | None = None,
    llm: LlmClient | None = None,
    toolsets: Sequence[str] = ("research",),
    auto_migrate: bool = True,
) -> AppContext:
    """装配上下文。**没有密钥也不报错**（客户端置空，system.health 会如实显示未配置）。

    显式传入 ``db_path`` 时会**同步写回配置**，否则 ``system.health`` / ``config.get``
    报告的路径会与实际使用的库不一致（冒烟测试踩到过）。
    """
    cfg = config or load_config()
    if db_path is not None:
        cfg = replace(cfg, db_path=Path(db_path))
    db = Database(cfg.db_path)
    if auto_migrate:
        apply_migrations(db)

    client = llm
    if client is None:
        try:
            client = create_client(cfg.chat)
        except LlmError:
            client = None
    return AppContext(config=cfg, db=db, llm=client, toolsets=tuple(toolsets))
