"""一次性迁移：任意已知旧布局 → **当前布局**（默认 ``<会话>/<格式>``，docs/19 §3）。

干跑与真跑走**同一条计划**（``scholarscout.library.migrate.plan_moves``），
所以 ``--dry-run`` 看到的就是真跑会发生的事，不含任何"跑起来才发现"的意外。

布局是参数：不给 ``--layout`` 就读 ``config.yaml`` 的 ``library.layout``（默认 ``<会话>/<格式>``）。
可用段：``collected``（收录年月）/ ``published``（发表年月）/ ``session``（会话）/ ``format``（格式）。
**未知段直接报错**，不会静默把文件写到别处。

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
from scholarscout.config import load_config  # noqa: E402
from scholarscout.library import index as library_index  # noqa: E402
from scholarscout.library import migrate  # noqa: E402
from scholarscout.state.db import Database  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把抓取资料迁到当前的命名与放置布局")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不动任何文件")
    parser.add_argument("--db", help="数据库路径（默认 run/state.db）")
    parser.add_argument("--root", help="项目根（默认按 SCHOLARSCOUT_HOME / 包位置探测）")
    parser.add_argument(
        "--layout",
        help="目录如何切分，斜杠分隔（默认读 config.yaml 的 library.layout；如 session/format）",
    )
    return parser


def _resolve_layout(args: argparse.Namespace, root: Path | None) -> tuple[str, ...]:
    """布局：命令行 > config.yaml；两者都没有就用内置默认。**未知段当场报错**。"""
    if args.layout:
        return paths.normalize_layout(args.layout)
    config_file = (root or paths.home()) / "config.yaml"
    return load_config(config_file, load_env=False).library.layout


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(args.root).resolve() if args.root else None
    db_file = Path(args.db) if args.db else paths.db_path(root)
    layout = _resolve_layout(args, root)
    db = Database(db_file)
    try:
        report = migrate.run_migration(db, root=root, dry_run=args.dry_run, layout=layout)
        print(f"布局：<{'/'.join(layout)}>")
        if args.dry_run:
            for move in report.moves:
                if move.kind == "skip":
                    continue
                print(f"[{move.kind}] {move.old_raw}  →  {move.new_raw}")
                if move.old_text != move.new_text:
                    print(f"          {move.old_text}  →  {move.new_text}")
            print("（dry-run：以上均未落盘、未写库）")
        else:
            payload = library_index.write_indexes(db, root=root, layout=layout)
            print(
                f"索引已重建：{payload['index_path']}"
                f"（{payload['total']} 条 / {payload['buckets']} 个桶）"
            )
            for stale in payload["removed"]:
                print(f"已删除过期索引目录：{stale}")
            for item in report.conflict_items:
                print(f"冲突：{item['doc_id']} 目标已占用 → {item['new_raw']}")
            for failure in report.failures:
                print(f"失败：{failure}")
            if report.orphans:
                print(f"未引用历史文件已隔离 {report.orphans} 个 → <data>/**/{migrate.ORPHAN_DIR}/")
            for removed in report.removed_dirs:
                print(f"已删除旧目录：{removed}")
        print("== 汇总：", report.summary_line())
        return 0 if report.failed == 0 else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
