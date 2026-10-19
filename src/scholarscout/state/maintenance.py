"""数据库整理（`VACUUM` / `PRAGMA optimize` / 页数统计）—— state 之外的模块不再写 SQL。

为什么单独一层：`settings_io.optimize_database` 只该管"开不开整理、整理完怎么提示"，
它不该知道 SQLite 的整理语句长什么样。搬到这里之后，"哪些 SQL 在跑"只有 `state/` 一个答案，
`grep 'execute(' src/scholarscout | grep -v state/` 保持为空（这是验收口径，不是巧合）。

两条口径：
- **只整理、不删数据**：`VACUUM` 只回收空闲页，不碰任何行（与"资料本体拒绝清理"不冲突）；
- **走 `db.execute`，不新开连接**：它持写锁、串行化，和业务写共用一条写连接；
  自己 `sqlite3.connect` 一条会绕开写锁（VACUUM 期间与写事务撞车）。
"""

from __future__ import annotations

from scholarscout.state.db import Database


def database_pages(db: Database) -> int:
    """逻辑大小（页数 × 页大小）—— VACUUM 真正会收缩的是这个。

    为什么不能只看文件字节数：WAL 模式下写入先进 `-wal`，主库文件可能纹丝不动，
    于是"整理完文件没变小"会被误读成"整理没用"。
    量不到（库已关/PRAGMA 报错）就退化成 0：宁可报告少一项，也不编一个数出来。
    """
    try:
        page_count = db.scalar_int("PRAGMA page_count")
        page_size = db.scalar_int("PRAGMA page_size")
    except Exception:  # pragma: no cover - 拿不到就退化成 0，不编数
        return 0
    return page_count * page_size


def vacuum(db: Database) -> None:
    """重建库文件、回收空闲页（**大库会短暂持写锁**，由 `Database` 的写锁串行化）。"""
    db.execute("VACUUM")


def optimize(db: Database) -> None:
    """让 SQLite 自己挑要更新的统计信息（`PRAGMA optimize`，轻量、可常跑）。"""
    db.execute("PRAGMA optimize")


__all__ = ["database_pages", "optimize", "vacuum"]
