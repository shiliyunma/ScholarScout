"""SQLite 门面 —— **唯一写库入口**（关键路径，人工逐行 review）。

约定：
- 连接一律经本类，其他模块不许直接 ``sqlite3.connect``
- 连接参数（WAL / foreign_keys / busy_timeout）只在这里设置
- ``isolation_level=None``：自动提交，需要原子性时用 :meth:`transaction`
- **一写多读**（docs/20 §4.2）：`query` 一族走只读连接，`execute` / `executescript` /
  `transaction` 走专用写连接并由 :attr:`_write_lock`（``RLock``）串行化。
  为什么必须拆：单条共享连接上的显式事务**不可重入** —— 两个会话同时落消息时，第二个
  ``BEGIN`` 直接抛 ``cannot start a transaction within a transaction``；更隐蔽的是交错会
  把别的会话的自动提交写入裹进自己的事务，自己一回滚就把别人的数据一起回滚（不报错、只丢数据）。
  写操作本身极短（单条 INSERT 微秒级），瓶颈在模型与出网，所以"一把写锁串行化"比让 3 个线程
  去抢 SQLite 的 ``SQLITE_BUSY`` 再猜重试更可预测（且零新依赖、不改表结构）。
"""

from __future__ import annotations

import sqlite3
import threading
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
        self._timeout_s = timeout_s
        # check_same_thread=False + WAL：RPC 层是并发的（一轮对话在跑时，界面还要
        # 能点开资料预览 / 点停止），所以两条连接都会被多个工作线程使用。
        # 安全性来自：读连接只读、写连接全部经 `_write_lock` 串行化。
        self._read_conn = self._open_connection()
        self._write_conn = self._open_connection()
        # **可重入**写锁：`append_message` 持事务时还会经 `db.execute` 写会话标题（同一线程），
        # 用普通 Lock 会当场自锁死 —— 那是每个新会话第一条消息都会走的路。
        self._write_lock = threading.RLock()

    # ---------------------------------------------------------------- 连接

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path), timeout=self._timeout_s, isolation_level=None, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        self._apply_connection_pragmas(conn)
        return conn

    def _apply_connection_pragmas(self, conn: sqlite3.Connection) -> None:
        # WAL：读写并发；foreign_keys：外键必须显式打开否则形同注释
        # （**每条连接都要设**：外键级联是按连接生效的，只设一条会让 delete 级联时灵时不灵）
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = NORMAL")

    @property
    def conn(self) -> sqlite3.Connection:
        """逃生舱：只在迁移执行器里用（启动期、单线程）。业务代码不许碰。

        注意它返回**写连接**且**不持写锁** —— 并发路径请走 :meth:`transaction`。
        """
        return self._write_conn

    def close(self) -> None:
        self._read_conn.close()
        self._write_conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- 查询

    def execute(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Cursor:
        """单条语句（写 / DDL 都走**专用写连接**，持写锁）；读请走 :meth:`query` 一族。"""
        with self._write_lock:
            return self._write_conn.execute(sql, params)

    def executescript(self, sql: str) -> None:
        with self._write_lock:
            self._write_conn.executescript(sql)

    def query(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        return list(self._read_conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Row | None:
        return self._read_conn.execute(sql, params).fetchone()

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
        """显式事务（**持写锁** → 同一时刻只有一个写事务在飞）；异常回滚。

        为什么写锁在**事务**这一层而不是每条语句：`append_message` 的"选下一个 idx + INSERT"
        必须在同一临界区里，否则 `UNIQUE(session_id, idx)` 会被并发破掉（docs/20 §4.2）。
        锁是 `RLock`：同一线程在事务里再经 `db.execute` 写（例如写会话标题）不会自锁死。
        """
        with self._write_lock:
            self._write_conn.execute("BEGIN")
            try:
                yield self._write_conn
            except BaseException:
                self._write_conn.execute("ROLLBACK")
                raise
            else:
                self._write_conn.execute("COMMIT")

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
