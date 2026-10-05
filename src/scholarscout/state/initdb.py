"""建库入口（幂等）：``python -m scholarscout.state.initdb --db run/state.db``。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scholarscout import paths
from scholarscout.state.db import Database
from scholarscout.state.migrate import apply_migrations, current_version


def init_db(db_path: Path, *, target: int | None = None, make_dirs: bool = True) -> Database:
    """建/迁移数据库并返回连接。重复调用是安全的。"""
    if make_dirs:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # 默认位置的库（run/state.db）顺便把 data/ 子目录也建好
        if db_path.parent.name == "run":
            paths.ensure_dirs(db_path.parent.parent)
    db = Database(db_path)
    applied = apply_migrations(db, target=target)
    if applied:
        print(f"已应用迁移：{applied}")
    else:
        print("没有需要应用的迁移")
    print(f"当前 schema 版本：{current_version(db)}")
    return db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="建立/迁移 ScholarScout 数据库（幂等）")
    parser.add_argument("--db", default=None, help="数据库路径，默认 run/state.db")
    parser.add_argument("--target", type=int, default=None, help="只升到指定版本")
    parser.add_argument("--no-dirs", action="store_true", help="不创建 data/ 子目录")
    args = parser.parse_args(argv)

    db_file = Path(args.db) if args.db else paths.db_path()
    db = init_db(db_file, target=args.target, make_dirs=not args.no_dirs)
    try:
        counts = db.counts()
        print(f"数据库：{db_file}")
        print("表行数：" + "，".join(f"{k}={v}" for k, v in counts.items()))
        print(
            f"FTS 索引：summaries_fts={'summaries_fts' in db.table_names()}，"
            f"messages_fts={'messages_fts' in db.table_names()}"
        )
        print(f"触发器：{len(db.trigger_names())} 个")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
