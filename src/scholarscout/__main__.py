"""``python -m scholarscout`` 入口（CLI 请用 ``scripts/scout.py``，原因见 docs/03 §9）。

子命令：
    --version          版本
    --initdb           建库/迁移
    --rpc              stdin/stdout 上的 JSON-RPC 服务（桌面壳与测试用）
    --chat             终端里的极简对话（M2 的自验手段）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scholarscout import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scholarscout", description="开放世界学术调研与学习辅助智能体"
    )
    parser.add_argument("--version", action="store_true", help="打印版本并退出")
    parser.add_argument("--initdb", action="store_true", help="建库/迁移后退出")
    parser.add_argument("--rpc", action="store_true", help="在 stdin/stdout 上跑 JSON-RPC 服务")
    parser.add_argument(
        "--chat", action="store_true", help="终端极简对话（需要 .env 里的模型密钥）"
    )
    parser.add_argument("--db", default=None, help="数据库路径，默认 run/state.db")
    parser.add_argument("--toolsets", default="research", help="逗号分隔的工具集，默认 research")
    return parser


def _chat_repl(db_path: Path | None, toolsets: tuple[str, ...]) -> int:
    from scholarscout.app import build_context
    from scholarscout.rpc.methods import dispatch

    ctx = build_context(db_path=db_path, toolsets=toolsets)
    if ctx.llm is None:
        print(
            "未配置模型密钥：把 LLM_API_KEY 写进 .env（本地 Ollama 可填 ollama）", file=sys.stderr
        )
        return 2
    created = dispatch(ctx, "session.create", {})
    session_id = created["session_id"]
    print(f"新会话 {session_id}｜模型 {ctx.config.chat.model}｜工具集 {','.join(toolsets)}")
    print("输入内容对话；空行或 /exit 退出。\n")
    try:
        while True:
            try:
                line = input("你> ").strip()
            except EOFError:
                break
            if not line or line in {"/exit", "/quit"}:
                break
            result = dispatch(ctx, "chat.send", {"session_id": session_id, "text": line})
            print(f"\n助手> {result['text']}\n")
            print(f"      [stop_reason={result['stop_reason']} usage={result['usage']}]\n")
    finally:
        ctx.db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    toolsets = tuple(t.strip() for t in str(args.toolsets).split(",") if t.strip()) or ("research",)
    db_target = Path(args.db) if args.db else None

    if args.version:
        print(f"ScholarScout {__version__}")
        return 0

    if args.initdb:
        from scholarscout.state import initdb

        forwarded = ["--db", args.db] if args.db else []
        return initdb.main(forwarded)

    if args.rpc:
        from scholarscout.app import build_context
        from scholarscout.rpc import server

        ctx = build_context(db_path=db_target, toolsets=toolsets)
        try:
            return server.serve(ctx)
        finally:
            ctx.db.close()

    if args.chat:
        return _chat_repl(db_target, toolsets)

    print(
        "可用：--version / --initdb / --rpc / --chat\n"
        "M2 阶段尚无桌面壳；工具尚未注册（M3 起接入）。见 docs/00-总索引.md §5"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
