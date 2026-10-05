"""桌面壳入口：pywebview 窗口 + 一行一 JSON 的后端子进程。

    python scripts/scout.py --desktop

壳里**不含业务逻辑**：起进程 → 建窗口 → 把 JS 的调用转发给后端（docs/01 §2）。
``import webview`` 放在函数里：没装 pywebview 的机器仍能跑测试与 CLI。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scholarscout.desktop.bridge import Bridge
from scholarscout.desktop.rpc_client import RpcClient, default_command

UI_DIR = Path(__file__).resolve().parent / "ui"
WINDOW_TITLE = "ScholarScout —— 开放世界学术调研与学习辅助"
MIN_SIZE = (960, 640)


def launch(
    *,
    db_path: str | Path | None = None,
    toolsets: str = "research",
    debug: bool = False,
) -> int:
    """打开窗口并阻塞到用户关闭。返回进程退出码。"""
    try:
        import webview
    except ImportError:
        print(
            "未安装 pywebview。先跑：python scripts/install_deps.py（依赖已在 deps-allowlist 里）",
            file=sys.stderr,
        )
        return 2

    holder: dict[str, Bridge] = {}
    client = RpcClient(
        default_command(db_path=db_path, toolsets=toolsets),
        on_event=lambda name, params: holder["bridge"].on_event(name, params),
    )
    bridge = Bridge(client)
    holder["bridge"] = bridge
    client.start()

    window = webview.create_window(
        WINDOW_TITLE,
        str(UI_DIR / "index.html"),
        js_api=bridge,
        width=1180,
        height=800,
        min_size=MIN_SIZE,
    )
    bridge.attach(window)
    try:
        webview.start(debug=debug)
    finally:
        client.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scholarscout-desktop", description="ScholarScout 桌面壳")
    parser.add_argument("--db", default=None, help="数据库路径，默认 run/state.db")
    parser.add_argument("--toolsets", default="research", help="逗号分隔的工具集")
    parser.add_argument("--debug", action="store_true", help="开 webview 调试（右键可查元素）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return launch(db_path=args.db, toolsets=str(args.toolsets), debug=bool(args.debug))


if __name__ == "__main__":
    sys.exit(main())
