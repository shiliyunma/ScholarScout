"""SQLite 门面 —— **唯一写库入口**（关键路径，人工逐行 review）。

约定：
- 连接一律经本类，其他模块不许直接 ``sqlite3.connect``
- 连接参数（WAL / foreign_keys / busy_timeout）只在这里设置
- ``isolation_level=None``：自动提交，需要原子性时用 :meth:`transaction`
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

#: 项目自己建的表（不含 FTS 影子表与 sqlite_* 内部表）
DOMAIN_TABLES: tuple[str, ...] = (
    "sources",
    "documents",
    "summaries",
    "sessions",
    "messages",
    "tool_traces",
    "source_mentions",
    "topic_maps",
    "learning_routes",
    "notes",
)


class Database:
    """薄门面：连接管理 + 查询包装。仓储方法（写业务数据）后续按需加在子模块里。"""

    def __init__(self, path: str | Path, *, timeout_s: float = 5.0) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=timeout_s, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._apply_connection_pragmas()

    # ---------------------------------------------------------------- 连接

    def _apply_connection_pragmas(self) -> None:
        # WAL：读写并发；foreign_keys：外键必须显式打开否则形同注释
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA synchronous = NORMAL")

    @property
    def conn(self) -> sqlite3.Connection:
        """逃生舱：只在迁移执行器里用。业务代码不许碰。"""
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- 查询

    def execute(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def executescript(self, sql: str) -> None:
        self._conn.executescript(sql)

    def query(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[object] = ()) -> object:
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    def scalar_int(self, sql: str, params: Sequence[object] = ()) -> int:
        """取一个整数标量；行不存在或为 NULL 时返回 0。"""
        row = self.query_one(sql, params)
        if row is None or row[0] is None:
            return 0
        return int(row[0])

    def scalar_str(self, sql: str, params: Sequence[object] = ()) -> str | None:
        """取一个文本标量；行不存在或为 NULL 时返回 None。"""
        row = self.query_one(sql, params)
        if row is None or row[0] is None:
            return None
        return str(row[0])

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务；异常回滚。"""
        self._conn.execute("BEGIN")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ---------------------------------------------------------------- 自检

    def table_names(self) -> set[str]:
        rows = self.query("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {str(r[0]) for r in rows}

    def trigger_names(self) -> set[str]:
        rows = self.query("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        return {str(r[0]) for r in rows}

    def has_table(self, name: str) -> bool:
        return name in self.table_names()

    def counts(self) -> dict[str, int]:
        """各业务表行数，供 system.health 与建库自检用。"""
        out: dict[str, int] = {}
        for table in DOMAIN_TABLES:
            if not self.has_table(table):
                out[table] = 0
                continue
            out[table] = self.scalar_int(f"SELECT count(*) FROM {table}")
        return out
