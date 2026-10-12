"""应用装配：把配置、数据库、模型客户端组装成一个 :class:`AppContext`。

CLI、stdin/stdout 传输、桌面壳都从这里拿上下文，保证只有一处装配逻辑。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from scholarscout import paths
from scholarscout.config import Config, load_config
from scholarscout.fetch.http import HttpClient
from scholarscout.providers.llm import LlmClient, LlmError, create_client
from scholarscout.rpc.methods import AppContext
from scholarscout.state.db import Database
from scholarscout.state.migrate import apply_migrations


def build_context(
    *,
    config: Config | None = None,
    db_path: Path | None = None,
    llm: LlmClient | None = None,
    http: HttpClient | None = None,
    toolsets: Sequence[str] = ("research",),
    auto_migrate: bool = True,
) -> AppContext:
    """装配上下文：配置、库、模型客户端、出网客户端、已注册工具。

    - **没有密钥也不报错**（客户端置空，system.health 会如实显示未配置）
    - 显式传 ``db_path`` 时会同步写回配置，否则 health/config.get 报的路径与实际库不一致
    - 出网客户端只在这里造一份，供所有工具共用（限速与缓存才能生效）
    """
    from scholarscout.tools import register_tools

    cfg = config or load_config()
    if db_path is not None:
        cfg = replace(cfg, db_path=Path(db_path))

    paths.ensure_dirs(cfg.data_dir.parent)
    db = Database(cfg.db_path)
    if auto_migrate:
        apply_migrations(db)

    client = llm
    if client is None:
        try:
            client = create_client(cfg.chat)
        except LlmError:
            client = None

    http_client = http
    if http_client is None:
        http_client = HttpClient(
            user_agent=cfg.fetch_cfg.user_agent,
            timeout_s=cfg.fetch_cfg.timeout_s,
            # 分阶段超时（全表见 fetch/http.py 顶部）：建连 5s / robots 探针 5s /
            # 整次抓取 deadline（None → timeout_s × 1.5）。不传的话所有阶段共用一个
            # 20s 超时 ×3 次重试，真机 trace 里单次 source.fetch 就是 486s。
            connect_timeout_s=cfg.fetch_cfg.connect_timeout_s,
            deadline_s=cfg.fetch_cfg.deadline_s,
            robots_timeout_s=cfg.fetch_cfg.robots_timeout_s,
            retries=cfg.fetch_cfg.retries,
            max_bytes=cfg.fetch_cfg.max_bytes,
            cache_dir=cfg.fetch_cfg.cache_dir(cfg.data_dir.parent),
            cache_ttl_hours=cfg.fetch_cfg.cache_ttl_hours,
            respect_robots=cfg.fetch_cfg.respect_robots,
            robots_exempt_hosts=cfg.fetch_cfg.robots_exempt_hosts,
        )

    register_tools()
    return AppContext(
        config=cfg,
        db=db,
        llm=client,
        http=http_client,
        toolsets=tuple(toolsets),
    )
