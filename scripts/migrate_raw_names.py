"""一次性迁移：旧命名 → 「类型/年-月/人类可读文件名」布局（docs/19 §3）。

干跑与真跑走**同一条计划**（``scholarscout.library.migrate.plan_moves``），
所以 ``--dry-run`` 看到的就是真跑会发生的事，不含任何\"跑起来才发现\"的意外。

用法（在项目根目录）：
    python scripts/migrate_raw_names.py --dry-run     # 只打印「旧 → 新」清单与冲突数，不动盘
    python scripts/migrate_raw_names.py               # 真迁移：先改名 → 再在事务里更新库 → 最后重建索引

退出码：失败数为 0 才返回 0（有失败逐条打印原因，不静默）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scholarscout import paths  # noqa: E402
from scholarscout.library import index as library_index  # noqa: E402
from scholarscout.library import migrate  # noqa: E402
from scholarscout.state.db import Database  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把抓取资料迁到人类可读的命名与放置布局")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不动任何文件")
    parser.add_argument("--db", help="数据库路径（默认 run/state.db）")
    parser.add_argument("--root", help="项目根（默认按 SCHOLARSCOUT_HOME / 包位置探测）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(args.root).resolve() if args.root else None
    db_file = Path(args.db) if args.db else paths.db_path(root)
    db = Database(db_file)
    try:
        report = migrate.run_migration(db, root=root, dry_run=args.dry_run)
        if args.dry_run:
            for move in report.moves:
                if move.kind == "skip":
                    continue
                print(f"[{move.kind}] {move.old_raw}  →  {move.new_raw}")
                if move.old_text != move.new_text:
                    print(f"          {move.old_text}  →  {move.new_text}")
            print("（dry-run：以上均未落盘、未写库）")
        else:
            payload = library_index.write_indexes(db, root=root)
            print(
                f"索引已重建：{payload['index_path']}"
                f"（{payload['total']} 条 / {payload['buckets']} 个桶）"
            )
            for item in report.conflict_items:
                print(f"冲突：{item['doc_id']} 目标已占用 → {item['new_raw']}")
            for failure in report.failures:
                print(f"失败：{failure}")
            if report.orphans:
                print(f"未引用历史文件已隔离 {report.orphans} 个 → <data>/**/{migrate.ORPHAN_DIR}/")
            for removed in report.removed_dirs:
                print(f"已删除空目录：{removed}")
        print("== 汇总：", report.summary_line())
        return 0 if report.failed == 0 else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
