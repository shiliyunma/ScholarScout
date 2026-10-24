"""版本号对齐闸：``pyproject.toml`` 与 ``scholarscout.__version__`` 必须是同一个值。

为什么要有这道闸：两处版本各写各的，发版时改了 `__version__` 忘了 `pyproject` （或反过来），
结果是「关于」里显示的版本与装出来的包版本不一致 —— 用户报 bug 时给的版本号是错的，
而这件事在界面上完全看不出来（界面上只读 `__version__`，见 `desktop/bridge.py`）。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import scholarscout

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def test_pyproject_version_matches_package_version() -> None:
    assert _pyproject_version() == scholarscout.__version__


def test_version_is_semver_core() -> None:
    """只钉「三段数字」这个形状，不钉具体值 —— 钉值会让每次发版都得改测试。"""
    parts = scholarscout.__version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)
