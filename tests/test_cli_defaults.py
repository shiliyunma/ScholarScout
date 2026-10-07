"""打包后可执行文件的"点开即用"行为（`__main__.main` 的无参数默认）。

为什么要有这一条：打包成 exe 后，用户双击就是**不带任何参数**地运行。
如果这时还按开发习惯打印一行用法，双击的结果就是"什么都没发生"——
真机第一次验证就是这样：双击后无窗口、进程立刻退出，只有手动 `--desktop` 才出界面。
"""

from __future__ import annotations

import sys

import pytest

from scholarscout import __main__ as cli


def test_no_args_opens_desktop_when_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_launch(**kwargs: object) -> int:
        calls.update(kwargs)
        return 0

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr("scholarscout.desktop.app.launch", fake_launch)

    assert cli.main([]) == 0
    assert calls, "双击（无参数）时必须打开桌面窗口"
    assert calls.get("open_settings") is False


def test_no_args_prints_usage_in_dev(monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """源码运行时不改行为：还能打印用法（脚本与排障都靠它）。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert cli.main([]) == 0
    assert "--desktop" in capsys.readouterr().out


def test_explicit_flags_win_over_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """带参数时默认值不许抢戏（例如 `--version` 仍只打印版本）。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        "scholarscout.desktop.app.launch",
        lambda **kwargs: pytest.fail("带 --version 时不该开窗口"),
    )
    assert cli.main(["--version"]) == 0
