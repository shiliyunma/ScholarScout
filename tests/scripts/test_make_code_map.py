"""`scripts/make_code_map.py` 的回归测试（离线、不依赖网络与真实项目结构）。

为什么值得测：生成器坏了不会让任何业务测试变红，只会让**代码地图悄悄变成假的**
（这正是 Chromium 规范点名的 "stale documentation"）。所以这里钉住四条：
稳定输出（可 diff）、无本机绝对路径、坏文件显式暴露、`--check` 真能抓到过期。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 必须注册进 sys.modules：`from __future__ import annotations` 下 @dataclass 要靠
    # sys.modules[cls.__module__] 解析字符串注解，否则抛
    # AttributeError: 'NoneType' object has no attribute '__dict__'。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


SAMPLE = {
    "pkg/__init__.py": '"""包入口。"""\n',
    "pkg/alpha.py": (
        '"""甲模块：把 A 变成 B。\n\n第二行不参与首句。\n"""\n'
        "from __future__ import annotations\n"
        "import json\n"
        "from scholarscout.state.db import Database\n"
        "\n\n"
        "def sync(db: Database, limit: int = 3) -> None:\n"
        '    """做同步。"""\n'
    ),
    "pkg/nodoc.py": "import os\n\n\nclass Thing:\n    pass\n",
}


def test_lists_files_with_counts_and_symbols(tmp_path: Path) -> None:
    make_code_map = load_script("make_code_map")
    root = _tree(tmp_path, SAMPLE)
    rows = {f.rel: f for f in make_code_map.collect(root, ("pkg",))[0][1]}

    assert set(rows) == {"pkg/__init__.py", "pkg/alpha.py", "pkg/nodoc.py"}
    assert rows["pkg/alpha.py"].doc == "甲模块：把 A 变成 B。"
    assert rows["pkg/alpha.py"].symbols == ("sync(db, limit)",)
    # 依赖里内部名去掉包前缀，外部只留顶层模块名，且不用标准库语法的噪音
    assert "state.db" in rows["pkg/alpha.py"].deps
    assert "json" in rows["pkg/alpha.py"].deps
    assert rows["pkg/nodoc.py"].doc is None


def test_missing_docstring_is_flagged_not_hidden(tmp_path: Path) -> None:
    make_code_map = load_script("make_code_map")
    root = _tree(tmp_path, SAMPLE)
    rendered = make_code_map.render_roots(root, ("pkg",))
    assert "无模块 docstring" in rendered
    # 目录在二级标题里，表格里只写文件名（避免每行重复同一段路径）
    assert "## pkg/" in rendered
    assert "`nodoc.py`" in rendered


def test_output_is_stable_across_runs(tmp_path: Path) -> None:
    """同一份代码跑两次必须逐字相同，否则入库后每次都是脏 diff。"""
    make_code_map = load_script("make_code_map")
    root = _tree(tmp_path, SAMPLE)
    first = make_code_map.render_roots(root, ("pkg",))
    second = make_code_map.render_roots(root, ("pkg",))
    assert first == second


def test_output_has_no_absolute_paths(tmp_path: Path) -> None:
    """清单要能入库：本机绝对路径属于泄漏，也让它无法在别的机器上复现。"""
    make_code_map = load_script("make_code_map")
    root = _tree(tmp_path, SAMPLE)
    rendered = make_code_map.render_roots(root, ("pkg",))
    assert str(root) not in rendered
    assert str(Path.home()) not in rendered


def test_broken_file_is_reported_not_skipped(tmp_path: Path) -> None:
    make_code_map = load_script("make_code_map")
    root = _tree(tmp_path, {**SAMPLE, "pkg/broken.py": "def oops(:\n"})
    rendered = make_code_map.render_roots(root, ("pkg",))
    assert "解析失败" in rendered


def test_check_detects_staleness(tmp_path: Path) -> None:
    """门禁的核心：换一份代码后，旧的清单必须被判过期。"""
    make_code_map = load_script("make_code_map")
    root = _tree(tmp_path, SAMPLE)
    out = root / "map.md"

    assert make_code_map.write_map(root, ("pkg",), out) == 0
    assert make_code_map.check_map(root, ("pkg",), out) == 0

    (root / "pkg" / "new.py").write_text('"""新文件。"""\n', encoding="utf-8")
    assert make_code_map.check_map(root, ("pkg",), out) == 1


def test_missing_output_file_is_staleness(tmp_path: Path) -> None:
    make_code_map = load_script("make_code_map")
    root = _tree(tmp_path, SAMPLE)
    assert make_code_map.check_map(root, ("pkg",), root / "nope.md") == 1


def test_check_ignores_line_count_drift(tmp_path: Path) -> None:
    """只在文件里加了几行（结构没变）不该判过期 —— 否则门禁天天红，红久了就被无视。"""
    make_code_map = load_script("make_code_map")
    root = _tree(tmp_path, SAMPLE)
    out = root / "map.md"
    assert make_code_map.write_map(root, ("pkg",), out) == 0

    target = root / "pkg" / "alpha.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n\n# 只加注释，不改结构\n", "utf-8")
    assert make_code_map.check_map(root, ("pkg",), out) == 0


def test_check_detects_symbol_drift(tmp_path: Path) -> None:
    """同一个文件里新增一个顶层函数 = 结构变了，必须判过期。"""
    make_code_map = load_script("make_code_map")
    root = _tree(tmp_path, SAMPLE)
    out = root / "map.md"
    assert make_code_map.write_map(root, ("pkg",), out) == 0

    target = root / "pkg" / "alpha.py"
    target.write_text(
        target.read_text(encoding="utf-8") + '\n\ndef extra() -> None:\n    """新函数。"""\n',
        encoding="utf-8",
    )
    assert make_code_map.check_map(root, ("pkg",), out) == 1
