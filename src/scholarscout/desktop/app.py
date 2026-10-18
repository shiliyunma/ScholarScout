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

from scholarscout.config import UiSettings
from scholarscout.desktop import window_state
from scholarscout.desktop.bridge import Bridge
from scholarscout.desktop.icon import apply_window_icon, set_app_user_model_id
from scholarscout.desktop.rpc_client import RpcClient, default_command

UI_DIR = Path(__file__).resolve().parent / "ui"
#: 窗口/任务栏图标（跟 exe 图标同一份，见 scripts/make_icon.py）；缺了也不影响启动
ICON_PATH = Path(__file__).resolve().parents[3] / "assets" / "icon.ico"
WINDOW_TITLE = "ScholarScout —— 开放世界学术调研与学习辅助"


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


def _screen_rects() -> list[tuple[int, int, int, int]] | None:
    """当前所有屏幕的 (x, y, w, h)。拿不到就返回 None（由系统决定窗口位置）。

    `webview` 在函数里局部导入：本模块刻意不在顶层导入它（没装 pywebview 的机器仍要能跑测试与 CLI）。
    """
    try:
        import webview

        return [(int(s.x), int(s.y), int(s.width), int(s.height)) for s in webview.screens]
    except Exception:
        return None


def _looks_maximized(width: int, height: int) -> bool:  # pragma: no cover - 见调用说明
    """**当前未使用**：单位不可靠（screens 是逻辑像素、window 尺寸是物理像素），
    误判会把"记住大小"整个搞失效，所以 `_remember_geometry` 一律存几何（见其文档串）。
    保留这个函数是为了将来某个后端给出可靠的最大化标志时能接上。"""
    return any(width >= sw - 8 and height >= sh - 8 for _x, _y, sw, sh in _screen_rects() or [])


def _dpi_scale() -> float:
    """系统 DPI 缩放（96 → 1.0，149% → 1.49）。

    **为什么必须换算**：`window.width/height/x/y` 报的是**物理像素**，而
    `create_window(width=…)` 收的是**逻辑像素**（pywebview 内部按缩放再乘一次）。
    不换算的后果实测过：存下 1700x1050，下次打开变成 2539x1547（本机 149% 缩放）。
    """
    try:
        import ctypes

        dpi = int(ctypes.windll.user32.GetDpiForSystem())
        return dpi / 96.0 if dpi > 0 else 1.0
    except Exception:
        return 1.0


def _remember_geometry(window: Any, *, before: dict[str, Any] | None = None) -> None:
    """记住窗口大小/位置（写到 `config.yaml` 的 `ui.window`，下次打开同尺寸）。

    **为什么一律存几何、不判断"是否最大化"**：pywebview 的 `window.width/height` 报的是
    物理像素，而 `screens` / `create_window(width=…)` 用的是逻辑像素 —— 单位对不上时
    "很大的窗口"会被误判成最大化，于是尺寸就不写了，用户要的"记住大小"直接失效（实测踩过：
    拉到 2400x1400 被误判，存出来的只有 `{"maximized": true}`）。
    误判另一头的代价只是"最大化的窗口下次以同尺寸非最大化打开"，比前者小得多，所以不猜。

    **只在几何真的变了才写**：写 `config.yaml` 会重排整份文件（注释丢失，与 `settings_io`
    写回同一条代价），所以"打开又原样关掉"这一最常见路径一个字节都不该动。
    写失败**只记日志**：关窗路径不能因为存不了几何而卡住或报错退出。
    """
    try:
        width = int(getattr(window, "width", 0) or 0)
        height = int(getattr(window, "height", 0) or 0)
        if width <= 0 or height <= 0:
            return
        scale = _dpi_scale()
        # 物理 → 逻辑（存逻辑值，读出来直接交给 create_window 即可）
        logical_w = max(1, round(width / scale))
        logical_h = max(1, round(height / scale))
        x = getattr(window, "x", None)
        y = getattr(window, "y", None)
        payload = {
            "width": logical_w,
            "height": logical_h,
            "x": round(int(x) / scale) if isinstance(x, int | float) else None,
            "y": round(int(y) / scale) if isinstance(y, int | float) else None,
            "maximized": False,
        }
        if before is not None and window_state.same(before, payload):
            return
        window_state.save(
            width=logical_w,
            height=logical_h,
            x=payload["x"] if isinstance(payload["x"], int) else None,
            y=payload["y"] if isinstance(payload["y"], int) else None,
            maximized=False,
        )
    except Exception as exc:  # 纯辅助：失败只记一行，不影响退出
        print(f"[desktop] 窗口几何没记住（不影响使用）：{exc}", file=sys.stderr)
        return


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

    # 上次的窗口几何（大小/位置/是否最大化）。没有记录就用默认值。
    # 外观开关（⑤）：配置坏了不该让窗口打不开 —— 退回普通窗口。
    ui_cfg = UiSettings()
    try:
        from scholarscout.config import load_config as _load_config

        ui_cfg = _load_config().ui
    except Exception:
        pass

    # 屏幕信息用来"按屏收窄"：大屏用 1440x900，小屏不许超出视口（默认与最小尺寸都收）。
    screens = _screen_rects()
    geometry = window_state.normalize(ui_cfg.window, screens=screens)
    window = webview.create_window(
        WINDOW_TITLE,
        str(UI_DIR / "index.html"),
        js_api=bridge,
        width=int(geometry["width"]),
        height=int(geometry["height"]),
        x=geometry.get("x"),
        y=geometry.get("y"),
        # 最小尺寸同样按屏幕收窄（1280x800 在 1366x768 上放不下）
        min_size=window_state.min_size(screens),
        maximized=bool(geometry.get("maximized", False)),
        # ⑤ 无边框：原生标题行整条去掉，改用界面里自绘的窗控条
        frameless=bool(ui_cfg.frameless_window),
    )
    bridge.attach(window, frameless=bool(ui_cfg.frameless_window))

    # 关闭前把几何记下来；`closing` 在销毁之前触发，属性还能读到。
    # 另挂 `closed` 与 start() 之后的兜底：不同后端的事件可用性不完全一致。
    def _save_geometry(_window: Any = None) -> None:
        _remember_geometry(window, before=geometry)

    for event_name in ("closing", "closed"):
        event = getattr(window.events, event_name, None)
        if event is not None:
            with contextlib.suppress(Exception):
                event += _save_geometry
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
            if ui_cfg.frameless_window:
                # 界面靠这个 class 才显示自绘窗控条与右下角把手（有原生标题行时不该出现）
                with contextlib.suppress(Exception):
                    window.evaluate_js("document.documentElement.classList.add('frameless')")
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
        _remember_geometry(window, before=geometry)  # 兜底：万一上面的事件没触发
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
