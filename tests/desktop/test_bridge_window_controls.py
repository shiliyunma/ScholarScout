"""⑤ 无边框窗口的窗控：桥只做转发，尺寸在这里夹到最小。

为什么值得钉住：无边框窗口没有系统按钮，这四个方法是**唯一的**最小化/最大化/关闭/缩放手段。
一旦转发写错或尺寸没夹住，用户会得到一个点不动的窗口，或者一条缝。
"""

from __future__ import annotations

import json

from scholarscout.desktop.bridge import MIN_WINDOW_SIZE, Bridge


class FakeWindow:
    """只记下被调用了什么，不真的开窗。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def minimize(self) -> None:
        self.calls.append(("minimize",))

    def maximize(self) -> None:
        self.calls.append(("maximize",))

    def restore(self) -> None:
        self.calls.append(("restore",))

    def resize(self, width: int, height: int) -> None:
        self.calls.append(("resize", width, height))

    def destroy(self) -> None:
        self.calls.append(("destroy",))


def _bridge(window: FakeWindow | None) -> Bridge:
    return Bridge(client=None, window=window)  # type: ignore[arg-type]


def _payload(raw: str) -> dict:
    return json.loads(raw)


def test_minimize_and_close_forward_to_window() -> None:
    window = FakeWindow()
    bridge = _bridge(window)
    assert _payload(bridge.win_minimize())["ok"] is True
    assert _payload(bridge.win_close())["ok"] is True
    assert window.calls == [("minimize",), ("destroy",)]


def test_maximize_toggles_between_maximize_and_restore() -> None:
    """自己记状态：不依赖后端窗口对象是否暴露 maximized 属性。"""
    window = FakeWindow()
    bridge = _bridge(window)
    assert _payload(bridge.win_toggle_maximize())["result"]["maximized"] is True
    assert _payload(bridge.win_toggle_maximize())["result"]["maximized"] is False
    assert window.calls == [("maximize",), ("restore",)]


def test_resize_clamps_to_min_size() -> None:
    """前端传多小都不行 —— 夹的规则只有后端这一处，前端不必重复。"""
    window = FakeWindow()
    bridge = _bridge(window)
    _payload(bridge.win_resize("10", "10"))
    _payload(bridge.win_resize("1400", "900"))
    _payload(bridge.win_resize("", ""))
    assert window.calls == [
        ("resize", MIN_WINDOW_SIZE[0], MIN_WINDOW_SIZE[1]),
        ("resize", 1400, 900),
        ("resize", MIN_WINDOW_SIZE[0], MIN_WINDOW_SIZE[1]),
    ]


def test_without_window_it_reports_instead_of_crashing() -> None:
    """窗口还没 attach 时点按钮 → 报错信息，而不是抛异常。"""
    bridge = _bridge(None)
    for raw in (
        bridge.win_minimize(),
        bridge.win_toggle_maximize(),
        bridge.win_close(),
        bridge.win_resize("1200", "800"),
    ):
        payload = _payload(raw)
        assert payload["ok"] is False
        assert "窗口还没就绪" in payload["error"]["message"]


def test_window_mode_tells_the_ui_whether_to_draw_the_bar() -> None:
    """界面靠这个决定显不显示自绘窗控条；默认必须是 False（有原生标题行时不该出现）。"""
    bridge = _bridge(FakeWindow())
    assert _payload(bridge.window_mode())["result"]["frameless"] is False
    bridge.attach(FakeWindow(), frameless=True)
    payload = _payload(bridge.window_mode())["result"]
    assert payload["frameless"] is True
    assert payload["min_width"] == MIN_WINDOW_SIZE[0]
    assert payload["min_height"] == MIN_WINDOW_SIZE[1]
