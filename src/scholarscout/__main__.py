"""``python -m scholarscout`` 入口。

M1 阶段只有存储层可用；对话与桌面壳在 M2 接入（见 docs/00-总索引.md §5）。
"""

from __future__ import annotations

import argparse
import sys

from scholarscout import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scholarscout", description="开放世界学术调研与学习辅助智能体"
    )
    parser.add_argument("--version", action="store_true", help="打印版本并退出")
    parser.add_argument("--initdb", action="store_true", help="建库/迁移后退出")
    parser.add_argument("--db", default=None, help="数据库路径，默认 run/state.db")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.version:
        print(f"ScholarScout {__version__}")
        return 0

    if args.initdb:
        from scholarscout.state import initdb

        forwarded = ["--db", args.db] if args.db else []
        return initdb.main(forwarded)

    print("M1 阶段尚无对话功能。可用：--version / --initdb（见 docs/00-总索引.md §5）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
