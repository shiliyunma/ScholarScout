"""窗口几何的记住/恢复（`desktop/window_state.py`）。

这类功能的错法都很安静：错误地记住最大化尺寸、把窗口开到已拔掉的显示器上、存出比最小尺寸
还小的窗口 —— 用户看到的是"程序好像没启动"，日志里什么都没有。所以这里逐条钉住。
"""

from __future__ import annotations

import json
from pathlib import Path

from scholarscout.desktop import window_state as ws


def test_missing_file_means_no_state(tmp_path: Path) -> None:
    assert ws.load(tmp_path) == {}


def test_corrupt_file_is_ignored(tmp_path: Path) -> None:
    ws.state_path(tmp_path).write_text("{不是 JSON", encoding="utf-8")
    assert ws.load(tmp_path) == {}


def test_round_trip_keeps_size_and_position(tmp_path: Path) -> None:
    ws.save(tmp_path, width=1400, height=900, x=100, y=80)
    # 加载结果只带"有意义的键"：maximized=False 是默认态，不必回传（调用方 `bool(get(...,False))` 一样成立）
    assert ws.load(tmp_path, screens=[(0, 0, 1920, 1080)]) == {
        "width": 1400,
        "height": 900,
        "x": 100,
        "y": 80,
    }


def test_maximized_does_not_store_the_screen_sized_geometry(tmp_path: Path) -> None:
    """模块**支持**只存最大化标志（将来某后端能给出可靠标志时用得上）。

    注意：**调用方目前不猜最大化** —— pywebview 的 screens 与窗口尺寸单位不一致，
    猜错会让"记住大小"整个失效（实测踩过）。这里测的是模块自身契约，不是调用方策略。
    """
    payload = ws.save(tmp_path, width=2560, height=1600, x=0, y=0, maximized=True)
    assert payload == {"maximized": True}
    assert ws.load(tmp_path) == {"maximized": True}


def test_size_smaller_than_minimum_is_raised(tmp_path: Path) -> None:
    ws.save(tmp_path, width=200, height=100)
    state = ws.load(tmp_path)
    assert (state["width"], state["height"]) == ws.MIN_SIZE


def test_saved_yaml_with_bogus_values_falls_back(tmp_path: Path) -> None:
    ws.state_path(tmp_path).write_text(
        json.dumps({"width": "abc", "height": None, "x": "1"}, ensure_ascii=False),
        encoding="utf-8",
    )
    state = ws.load(tmp_path)
    assert "width" not in state and "height" not in state
    assert "x" not in state and "y" not in state, "x/y 必须成对才用"


def test_position_off_all_screens_is_dropped(tmp_path: Path) -> None:
    """拔掉外接屏之后，上次的坐标可能落在不存在的区域 —— 窗口会开到看不见的地方。"""
    ws.save(tmp_path, width=1200, height=800, x=3000, y=2000)
    assert "x" not in ws.load(tmp_path, screens=[(0, 0, 1920, 1080)])
    assert ws.load(tmp_path, screens=[(0, 0, 3840, 2160)])["x"] == 3000


def test_position_without_screen_info_is_kept(tmp_path: Path) -> None:
    """拿不到屏幕信息时不该自作主张丢掉位置（多屏环境下 pywebview 可能报不出来）。"""
    ws.save(tmp_path, width=1200, height=800, x=3000, y=2000)
    assert ws.load(tmp_path)["x"] == 3000


def test_state_lives_under_run(tmp_path: Path) -> None:
    """与本机环境绑定的小状态放 run/（已 gitignore），不要写进仓库。"""
    assert ws.state_path(tmp_path).parent == tmp_path
    assert ws.state_path(tmp_path).name == "window.json"
