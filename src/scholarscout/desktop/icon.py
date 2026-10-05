"""Windows 窗口图标与任务栏身份（纯 stdlib ctypes，不新增依赖）。

## 为什么需要这个文件

`webview.start(icon=...)` 在 Windows 上**是被忽略的**（pywebview 自己的文档写明
"Supported only on GTK/QT"）。而它的 Windows 后端（`platforms/winforms.py`）会这么做：

    handle = kernel32.GetModuleHandleW(None)
    icon_handle = windll.shell32.ExtractIconW(handle, sys.executable, 0)
    self.Icon = Icon.FromHandle(...).Clone()

即**从 `sys.executable`（也就是 python.exe）取图标** —— 所以我们双击启动器打开窗口时，
标题栏和任务栏显示的是 **Python 的图标**（真机确认过）。

## 两件事一起做才够

1. `WM_SETICON` 直接打在窗口句柄上 → 标题栏左上角（ICON_SMALL）与任务栏（ICON_BIG）；
2. `SetCurrentProcessExplicitAppUserModelID` → 给进程一个自己的身份，
   否则任务栏按钮会按 python.exe 归组（图标也跟着变 Python）。

## 注意（踩过会很难查）

64 位下必须显式声明 `restype = c_void_p`：ctypes 默认把返回值当 `c_int`，
HICON 句柄会被截断成 32 位，`SendMessage` 静默失败 —— 看起来"代码跑了但图标没变"。
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path

#: 任务栏身份（装成快捷方式后，钉在任务栏上的图标也认它）
APP_USER_MODEL_ID = "shiliyunma.ScholarScout"

WM_SETICON = 0x0080
ICON_SMALL = 0
ICON_BIG = 1
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010

#: 标题栏/任务栏分别用多大尺寸的图标（从多尺寸 .ico 里挑）
BIG_SIZE = 32
SMALL_SIZE = 16


def set_app_user_model_id(app_id: str = APP_USER_MODEL_ID) -> bool:
    """给进程一个显式的任务栏身份。非 Windows 或调用失败都只是返回 False，不影响启动。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        shell32 = ctypes.windll.shell32
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        return shell32.SetCurrentProcessExplicitAppUserModelID(app_id) == 0
    except (AttributeError, OSError):
        return False


def _default_find_window(title: str) -> int:
    import ctypes

    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.FindWindowW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    return int(user32.FindWindowW(None, title) or 0)


def _default_apply(hwnd: int, ico_path: Path) -> bool:
    """把 .ico 里的 32/16 两档分别设成窗口的大/小图标。"""
    import ctypes

    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = ctypes.c_void_p  # ← 不写这句句柄会被截断
    user32.LoadImageW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    user32.SendMessageW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]

    applied = False
    for icon_kind, size in ((ICON_BIG, BIG_SIZE), (ICON_SMALL, SMALL_SIZE)):
        handle = user32.LoadImageW(None, str(ico_path), IMAGE_ICON, size, size, LR_LOADFROMFILE)
        if handle:
            # 句柄要一直留着：SendMessage 之后由窗口持有，销毁它会变回默认图标
            user32.SendMessageW(hwnd, WM_SETICON, icon_kind, handle)
            applied = True
    return applied


def apply_window_icon(
    title: str,
    ico_path: Path,
    *,
    timeout_s: float = 20.0,
    find_window: Callable[[str], int] | None = None,
    apply_icons: Callable[[int, Path], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """等窗口出现后把图标打上去。窗口还没建好就轮询等（pywebview 的 start() 是异步的）。

    找不到窗口（超时）返回 False —— 图标没设上不该让应用起不来。
    """
    if sys.platform != "win32":
        return False
    if not ico_path.is_file():
        return False

    find = find_window or _default_find_window
    apply = apply_icons or _default_apply
    deadline = timeout_s
    waited = 0.0
    while True:
        try:
            hwnd = find(title)
        except (OSError, AttributeError):
            return False
        if hwnd:
            return bool(apply(hwnd, ico_path))
        if waited >= deadline:
            return False
        sleep(0.25)
        waited += 0.25
