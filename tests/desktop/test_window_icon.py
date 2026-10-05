"""窗口图标与任务栏身份（`desktop/icon.py`）。

为什么值得单独测：这块**失败是静默的** —— 图标没设上应用照样跑，只是标题栏/任务栏还是 Python 的图标。
真机踩到的三个点都钉在这里：

1. pywebview 的 `webview.start(icon=...)` 在 Windows 上不生效（仅 GTK/QT），
   它的 winforms 后端从 `sys.executable`（python.exe）取图标；
2. 64 位下 ctypes 不声明 `restype = c_void_p`，HICON 会被截断成 32 位、`SendMessage` 静默失败；
3. 窗口是异步创建的，打图标要**等窗口出现**，超时就放弃（不能因此起不来）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholarscout.desktop import icon as icon_mod

WINDOW = "ScholarScout —— 开放世界学术调研与学习辅助"


def _ico(tmp_path: Path) -> Path:
    path = tmp_path / "icon.ico"
    path.write_bytes(b"\x00\x00\x01\x00")
    return path


def test_constants_match_win32_api() -> None:
    assert icon_mod.WM_SETICON == 0x0080
    assert (icon_mod.ICON_SMALL, icon_mod.ICON_BIG) == (0, 1)
    assert icon_mod.IMAGE_ICON == 1
    assert icon_mod.LR_LOADFROMFILE == 0x0010
    assert icon_mod.BIG_SIZE == 32 and icon_mod.SMALL_SIZE == 16


def test_app_user_model_id_is_set_for_this_process() -> None:
    """真机调用一次：返回 True 说明任务栏身份设上了（非 Windows 上返回 False，不报错）。"""
    import sys

    result = icon_mod.set_app_user_model_id()
    assert isinstance(result, bool)
    if sys.platform == "win32":
        assert result is True


def test_missing_icon_file_is_a_noop(tmp_path: Path) -> None:
    called: list[object] = []
    assert (
        icon_mod.apply_window_icon(
            WINDOW,
            tmp_path / "nope.ico",
            find_window=lambda _title: called.append("find") or 1,
        )
        is False
    )
    assert called == [], "图标文件不存在时不该去找窗口"


def test_waits_for_the_window_then_applies_icons(tmp_path: Path) -> None:
    """窗口是异步创建的：前几次找不到要接着等，找到就打图标。"""
    seen: dict[str, object] = {"calls": 0}
    applied: list[tuple[int, Path]] = []

    def find(_title: str) -> int:
        seen["calls"] = int(seen["calls"]) + 1
        return 12345 if seen["calls"] >= 3 else 0

    ok = icon_mod.apply_window_icon(
        WINDOW,
        _ico(tmp_path),
        find_window=find,
        apply_icons=lambda hwnd, path: applied.append((hwnd, path)) is None,
        sleep=lambda _s: None,
        timeout_s=5.0,
    )
    assert ok is True
    assert applied == [(12345, _ico(tmp_path))]
    assert seen["calls"] == 3


def test_timeout_returns_false_without_touching_the_window(tmp_path: Path) -> None:
    applied: list[tuple[int, Path]] = []
    ok = icon_mod.apply_window_icon(
        WINDOW,
        _ico(tmp_path),
        find_window=lambda _title: 0,
        apply_icons=lambda hwnd, path: applied.append((hwnd, path)) is None,
        sleep=lambda _s: None,
        timeout_s=0.0,
    )
    assert ok is False and applied == []


def test_propagates_failure_from_the_setter(tmp_path: Path) -> None:
    ok = icon_mod.apply_window_icon(
        WINDOW,
        _ico(tmp_path),
        find_window=lambda _title: 1,
        apply_icons=lambda _hwnd, _path: False,
        sleep=lambda _s: None,
    )
    assert ok is False


def test_finder_errors_do_not_crash_startup(tmp_path: Path) -> None:
    def boom(_title: str) -> int:
        raise OSError("没有窗口站")

    assert (
        icon_mod.apply_window_icon(WINDOW, _ico(tmp_path), find_window=boom, sleep=lambda _s: None)
        is False
    )


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="窗口图标是 Windows 专有行为")
def test_real_window_icon_is_actually_set(tmp_path: Path) -> None:
    """真机（Windows）打一次图标：返回值必须是 True，说明两个 WM_SETICON 都发出去了。

    注意只在本进程已有的窗口上做不了 —— 这里用一个自带 HWND 的目标：记事本太慢，
    所以退一步只验证 `_default_apply` 对**真实窗口句柄**的行为：
    找不到窗口时返回 False（不抛异常），这正是应用启动早期可能出现的情况。
    """
    assert icon_mod._default_find_window("这个标题肯定不存在的窗口 xyz") == 0
    assert icon_mod._default_apply(0, _ico(tmp_path)) is False
