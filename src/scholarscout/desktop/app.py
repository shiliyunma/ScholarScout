"""桌面壳入口：pywebview 窗口 + 一行一 JSON 的后端子进程。

    python scripts/scout.py --desktop

壳里**不含业务逻辑**：起进程 → 建窗口 → 把 JS 的调用转发给后端（docs/01 §2）。
``import webview`` 放在函数里：没装 pywebview 的机器仍能跑测试与 CLI。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from scholarscout.desktop.bridge import Bridge
from scholarscout.desktop.icon import apply_window_icon, set_app_user_model_id
from scholarscout.desktop.rpc_client import RpcClient, default_command

UI_DIR = Path(__file__).resolve().parent / "ui"
#: 窗口/任务栏图标（跟 exe 图标同一份，见 scripts/make_icon.py）；缺了也不影响启动
ICON_PATH = Path(__file__).resolve().parents[3] / "assets" / "icon.ico"
WINDOW_TITLE = "ScholarScout —— 开放世界学术调研与学习辅助"
MIN_SIZE = (960, 640)


def window_icon() -> str | None:
    """有就用，没有返回 None（别因为少个图标起不来）。"""
    return str(ICON_PATH) if ICON_PATH.is_file() else None


def _open_settings_later(window: Any, delay_s: float = 2.0) -> None:
    """页面渲染完再执行一段 JS（设置面板 / `SCHOLARSCOUT_DEBUG_JS` 指定的排障片段）。

    为什么不在 URL 上加 `#settings`：pywebview 解析 `index.html#settings` 会加载失败，
    窗口全白（实测）。所以改成页面起来之后再调 JS —— 界面里的函数都是全局的。
    """
    time.sleep(delay_s)
    snippet = "if (typeof openSettings === 'function') { openSettings(); }"
    override = os.environ.get("SCHOLARSCOUT_DEBUG_JS", "").strip()
    if override:
        snippet = override
    # 纯排障用途：窗口已经关了 / JS 报错都不该影响应用
    with contextlib.suppress(Exception):
        window.evaluate_js(snippet)


def launch(
    *,
    db_path: str | Path | None = None,
    toolsets: str = "research",
    debug: bool = False,
    open_settings: bool = False,
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
        # 任务栏身份：不设的话任务栏按钮会按 python.exe 归组，图标跟着变 Python
        set_app_user_model_id()

        def _on_started() -> None:
            """窗口起来之后再把图标打上去。

            为什么不用 `webview.start(icon=...)`：那个参数**在 Windows 上被忽略**
            （pywebview 文档："Supported only on GTK/QT"），而它的 winforms 后端会
            从 `sys.executable`（python.exe）取图标 —— 所以默认是 Python 的图标。
            细节见 `desktop/icon.py` 的模块说明。
            """
            apply_window_icon(WINDOW_TITLE, ICON_PATH)
            if open_settings or os.environ.get("SCHOLARSCOUT_DEBUG_JS", "").strip():
                # 排障入口：--open-settings 开设置面板；SCHOLARSCOUT_DEBUG_JS 可直接跑一段 JS
                thread = threading.Thread(target=_open_settings_later, args=(window,), daemon=True)
                thread.start()

        start_kwargs: dict[str, Any] = {"debug": debug, "func": _on_started}
        if sys.platform != "win32":
            icon = window_icon()
            if icon:
                start_kwargs["icon"] = icon  # GTK/QT 才认这个参数
        webview.start(**start_kwargs)
    finally:
        client.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scholarscout-desktop", description="ScholarScout 桌面壳")
    parser.add_argument("--db", default=None, help="数据库路径，默认 run/state.db")
    parser.add_argument("--toolsets", default="research", help="逗号分隔的工具集")
    parser.add_argument("--debug", action="store_true", help="开 webview 调试（右键可查元素）")
    parser.add_argument(
        "--open-settings",
        action="store_true",
        help="启动后直接打开设置面板（排障/截图用）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return launch(
        db_path=args.db,
        toolsets=str(args.toolsets),
        debug=bool(args.debug),
        open_settings=bool(args.open_settings),
    )


if __name__ == "__main__":
    sys.exit(main())
