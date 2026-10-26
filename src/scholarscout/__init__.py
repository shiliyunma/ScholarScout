"""ScholarScout —— 开放世界学术调研与学习辅助智能体。

M1（骨架与存储）阶段只有存储层可用，见 docs/00-总索引.md §5。

## 版本号只有一个事实源：`pyproject.toml`

这个模块**不再手写** `__version__`（第一版写成了 `__version__ = "0.1.0"`，与 `pyproject`
各写各的：发版时改一处忘另一处，界面「关于」显示的版本与装出来的包版本就不是一回事，
而用户报 bug 时给的版本号是错的）。现在按下面顺序**读**出来：

1. `importlib.metadata.version("scholarscout")` —— 装出来的包（wheel / `uv pip install`），
   版本由包元数据给出，不新增任何依赖；
2. 源码树：从本文件向上找 `pyproject.toml` 的 `[project].version`；
3. 冻结包（PyInstaller）：向上找 `_VERSION` —— 那个文件由 `scripts/build_bundle.py`
   从 `pyproject.toml` 写出来，所以打包版报的也是同一个值。

三处都读不到才退到 `0.0.0+unknown`：宁可让 `--version` 明显不对，也不许在这里
再抄一遍真版本号（抄了就又是"两处各写各的"）。`tests/test_version_alignment.py` 钉住
"pyproject 与 `__version__` 必须相等"这道闸。
"""

from __future__ import annotations

import sys
from pathlib import Path

#: 任何来源都读不到时的兜底值。**故意带 `unknown`**：显眼，不会被当成真版本。
_UNKNOWN = "0.0.0+unknown"


def _from_distribution() -> str | None:
    """装出来的包：版本交给包元数据（`pyproject` 是它的上游事实源）。"""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - Python 3.8+ 都有，留作防御
        return None
    try:
        return version("scholarscout")
    except PackageNotFoundError:
        return None


def _read_pyproject_version(path: Path) -> str | None:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - 本项目要求 3.11+，留作防御
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    project = data.get("project")
    value = project.get("version") if isinstance(project, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def _from_bundle_or_source_tree() -> str | None:
    """源码树读 `pyproject.toml`，冻结包读 `_VERSION`（两者都从包的父目录逐级向上找）。"""
    starts: list[Path] = [Path(__file__).resolve().parent]
    meipass = getattr(sys, "_MEIPASS", None)  # PyInstaller onedir：包内根目录
    if meipass:
        starts.append(Path(meipass))
    for start in starts:
        for parent in (start, *start.parents):
            marker = parent / "_VERSION"
            if marker.is_file():
                text = marker.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    return text
            project = parent / "pyproject.toml"
            if project.is_file():
                found = _read_pyproject_version(project)
                if found:
                    return found
    return None


def _resolve_version() -> str:
    for candidate in (_from_distribution(), _from_bundle_or_source_tree()):
        if candidate:
            return candidate
    return _UNKNOWN


__version__ = _resolve_version()
__all__ = ["__version__"]
