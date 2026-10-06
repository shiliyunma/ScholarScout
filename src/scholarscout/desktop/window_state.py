"""窗口几何的记住与恢复（下次打开还是上次的大小/位置）。

为什么单独一个模块：读写文件这件事必须能**离线测**——直接写在 `app.py` 里就得起窗口才能验，
而"记住大小"这种需求恰恰错在细节（存了最大化尺寸、存到不存在的显示器坐标、窗口比最小尺寸还小）。

Windows 上还有一条**必须记住的坑**：`window.width/height/x/y` 报的是**物理像素**，
而 `create_window(width=…)` 收的是**逻辑像素**（pywebview 内部按 DPI 缩放再乘一次）。
不换算的后果实测过：存下 1700x1050，下次打开变成 2539x1547（本机 149% 缩放）。
所以 `_remember_geometry()` 存盘前除以 DPI 缩放（`window_state` 里存的一律是逻辑值，
读出来直接交给 `create_window` 即可）。

三条边界（都是真会遇到的）：

1. **单位**：文件里一律存**逻辑像素**（`create_window` 的单位）。`window.width` 报的是物理像素，
   由调用方除以 DPI 缩放后再存 —— 不换算出过事：存 1700x1050，下次开出 2539x1547。
   另注：**不要用"窗口尺寸≈屏幕尺寸"猜最大化**（screens 与窗口尺寸单位不一致会误判，
   误判的代价是尺寸干脆不记了，比"最大化的窗口下次以同尺寸打开"严重得多）。
2. **位置要能在当前显示器上落地**：笔记本外接屏拔掉后，上次的 x/y 可能指向不存在的区域，
   窗口会开在屏幕外（用户以为程序没启动）。落不进去就只恢复尺寸、位置交给系统。
3. **尺寸不得小于最小尺寸**：存的是旧值、最小尺寸后来调大了也要能兜住。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: 记在 run/ 下（gitignore 里已有 run/，与本机环境绑定，不该入库）
STATE_NAME = "window.json"

#: 窗口的最小尺寸（与 desktop/app.py 的 MIN_SIZE 保持一致）
MIN_SIZE = (960, 640)


def state_path(run_dir: Path) -> Path:
    return Path(run_dir) / STATE_NAME


def load(
    run_dir: Path, *, screens: list[tuple[int, int, int, int]] | None = None
) -> dict[str, Any]:
    """读出可用的窗口几何。文件缺失/损坏一律当作"没有记录"，用默认值。

    ``screens`` 是 ``(x, y, width, height)`` 列表（pywebview 的 screens 形状）；
    给了就顺带校验位置能不能落在某块屏幕里。
    """
    path = state_path(run_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}

    result: dict[str, Any] = {}
    if raw.get("maximized"):
        result["maximized"] = True
    width, height = _positive_int(raw.get("width")), _positive_int(raw.get("height"))
    if width and height:
        result["width"] = max(MIN_SIZE[0], width)
        result["height"] = max(MIN_SIZE[1], height)
    x, y = _int_or_none(raw.get("x")), _int_or_none(raw.get("y"))
    if x is not None and y is not None and _on_some_screen(x, y, width or 0, height or 0, screens):
        result["x"], result["y"] = x, y
    return result


def save(
    run_dir: Path,
    *,
    width: int,
    height: int,
    x: int | None = None,
    y: int | None = None,
    maximized: bool = False,
) -> dict[str, Any]:
    """写下窗口几何。最大化时**不写尺寸与位置**（见模块说明第 1 条）。"""
    payload: dict[str, Any] = {"maximized": bool(maximized)}
    if not maximized:
        payload["width"] = max(MIN_SIZE[0], int(width))
        payload["height"] = max(MIN_SIZE[1], int(height))
        if x is not None and y is not None:
            payload["x"], payload["y"] = int(x), int(y)
    path = state_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _on_some_screen(
    x: int, y: int, width: int, height: int, screens: list[tuple[int, int, int, int]] | None
) -> bool:
    """位置是否落在某块屏幕上（没有屏幕信息就放行，由系统决定）。"""
    if not screens:
        return True
    # 只要求"窗口左上角"落在屏幕内，且窗口至少在屏幕上露出一块可点的区域
    margin = 40
    for sx, sy, sw, sh in screens:
        if sx - margin <= x <= sx + sw - margin and sy - margin <= y <= sy + sh - margin:
            return True
    return False
