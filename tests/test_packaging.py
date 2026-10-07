"""自包含打包相关：`paths.home()` 在冻结模式下的行为（便携性的根）。

为什么值得单测：打包后如果还按"包目录上三级"找 home，数据会落进 PyInstaller 的临时解包目录
（onefile）或 `_internal/`（onedir）—— 用户重启一次就"资料全没了"，而且拷走也带不走。
所以冻结模式下必须指向 **exe 所在目录**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scholarscout import paths


def test_home_is_project_root_without_freezing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCHOLARSCOUT_HOME", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert (paths.home() / "src" / "scholarscout").is_dir(), "开发态应指向项目根"


def test_home_follows_the_exe_when_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SCHOLARSCOUT_HOME", raising=False)
    fake_exe = tmp_path / "ScholarScout.exe"
    fake_exe.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    assert paths.home() == tmp_path
    # 数据目录跟着 exe 走（便携：拷走整个目录，资料一起走）
    assert paths.data_dir() == tmp_path / "data"
    assert paths.db_path() == tmp_path / "run" / "state.db"


def test_env_var_still_wins_when_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """显式指定 SCHOLARSCOUT_HOME 时以它为准（测试与多实例都靠这个）。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/definitely/not/here.exe")
    monkeypatch.setenv("SCHOLARSCOUT_HOME", str(tmp_path))
    assert paths.home() == tmp_path.resolve()


def test_backend_command_uses_module_in_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    from scholarscout.desktop.rpc_client import default_command

    monkeypatch.delattr(sys, "frozen", raising=False)
    command = default_command(toolsets="research", db_path="run/state.db")
    assert command[1:3] == ["-m", "scholarscout"], "开发态用 `-m scholarscout` 起后端"
    assert "--rpc" in command and command[-2:] == ["--db", "run/state.db"]


def test_backend_command_launches_itself_when_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    """打包后必须 `exe --rpc`：PyInstaller 的引导器不认 `-m 模块`，
    那样起出来的后端会立刻退出 —— 真机表现是"界面能开但页头红字：后端进程已退出"。"""
    from scholarscout.desktop.rpc_client import default_command

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:/bundle/ScholarScout.exe")
    command = default_command(toolsets="research")
    assert command[0] == "C:/bundle/ScholarScout.exe"
    assert command[1:3] == ["--rpc", "--toolsets"], "不能再带 `-m scholarscout`"
    assert "-m" not in command


def test_child_env_never_injects_source_path_when_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    """打包后不许把源码目录挂进 PYTHONPATH。

    真机根因：打包目录里也有一份 `scholarscout/` 源码，挂上 PYTHONPATH 后子进程去 import
    源码而不是包内模块，输出退回 GBK → 壳按 UTF-8 读直接 UnicodeDecodeError → 判定"后端已退出"。
    """
    from scholarscout.desktop.rpc_client import default_env

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    env = default_env()
    assert "src" not in env.get("PYTHONPATH", "")
    assert env["PYTHONIOENCODING"] == "utf-8", "子进程输出必须是 UTF-8"


def test_child_env_injects_source_path_in_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    from scholarscout.desktop.rpc_client import default_env

    monkeypatch.delattr(sys, "frozen", raising=False)
    env = default_env()
    assert env.get("PYTHONPATH", "").endswith("src"), "源码运行要把 src/ 挂进去"
