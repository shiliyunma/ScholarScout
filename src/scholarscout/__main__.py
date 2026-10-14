"""``python -m scholarscout`` 入口（CLI 请用 ``scripts/scout.py``，原因见 docs/03 §9）。

子命令：
    --version          版本
    --initdb           建库/迁移
    --rpc              stdin/stdout 上的 JSON-RPC 服务（桌面壳与测试用）
    --chat             终端里的极简对话（M2 的自验手段）
    --desktop          pywebview 桌面窗口（M7：壳只转发，逻辑在后端）
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
    parser.add_argument("--desktop", action="store_true", help="打开桌面窗口（需要 pywebview）")
    parser.add_argument(
        "--open-settings",
        action="store_true",
        help="配合 --desktop：启动后直接打开设置面板（排障/截图用）",
    )
    parser.add_argument(
        "--check-ui",
        action="store_true",
        help="校验桌面 UI 资源（文件/方法名/无外链），门槛脚本用",
    )
    parser.add_argument(
        "--setup-feishu",
        action="store_true",
        help="飞书接入向导（逐步问答；密钥只写 .env 不回显；见 docs/18）",
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
    # 打包后**双击 exe 就是不带参数**：那就直接开界面（"点开即用"）。
    # 源码运行时保持打印用法 —— 免得误伤脚本调用与排障习惯。
    if getattr(sys, "frozen", False) and not any(
        (
            args.version,
            args.initdb,
            args.rpc,
            args.chat,
            args.desktop,
            args.check_ui,
            args.setup_feishu,
        )
    ):
        args.desktop = True
    toolsets = tuple(t.strip() for t in str(args.toolsets).split(",") if t.strip()) or ("research",)
    db_target = Path(args.db) if args.db else None

    if args.version:
        print(f"ScholarScout {__version__}")
        return 0

    if args.initdb:
        from scholarscout.state import initdb

        forwarded = ["--db", args.db] if args.db else []
        return initdb.main(forwarded)

    if args.check_ui:
        from scholarscout.desktop import assets

        return assets.main()

    if args.rpc:
        from scholarscout.app import build_context
        from scholarscout.rpc import server

        ctx = build_context(db_path=db_target, toolsets=toolsets)
        try:
            return server.serve(ctx)
        finally:
            # 关的**顺序**由 `AppContext.close()` 定：先 shutdown 会话池，再 HTTP，最后库。
            # 只关库的话，并行工作线程仍挂在解释器退出的 join 名单上 → 关窗会卡到那一轮跑完。
            ctx.close()

    if args.chat:
        return _chat_repl(db_target, toolsets)

    if args.setup_feishu:
        from scholarscout.app import build_context
        from scholarscout.channels import feishu_setup

        ctx = build_context(db_path=db_target, toolsets=toolsets)
        try:
            return feishu_setup.run_setup(ctx)
        finally:
            ctx.close()

    if args.desktop:
        from scholarscout.desktop.app import launch

        return launch(
            db_path=db_target,
            toolsets=",".join(toolsets),
            open_settings=bool(getattr(args, "open_settings", False)),
        )

    print(
        "可用：--version / --initdb / --rpc / --chat / --desktop / --setup-feishu\n"
        "飞书入站（websocket 长连接，不需要隧道）：python scripts/feishu_ws_up.py\n"
        "桌面壳：python scripts/scout.py --desktop（需先装 pywebview）；见 docs/00-总索引.md"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
