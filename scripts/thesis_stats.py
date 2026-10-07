"""打印论文"系统规模"表的**真数字**（从代码里数，不靠手写）。

为什么要有它：论文第 1/4/6 章都有一张"规模"表（源码行数、测试数、迁移数、工具数、提交数），
手写的数字**每改一次代码就过期一次**，而答辩时被问"你这个数据怎么来的"就没法答。
现在一句话就能重新生成，并且和论文里的表逐项对照。

用法：``.venv/Scripts/python.exe scripts/thesis_stats.py``
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "scholarscout"
UI = SRC / "desktop" / "ui"
TESTS = ROOT / "tests"
MIGRATIONS = SRC / "state" / "migrations"


def _lines(paths: list[Path]) -> int:
    return sum(len(path.read_text(encoding="utf-8").splitlines()) for path in paths)


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return result.stdout.strip()


def collect() -> dict[str, object]:
    sys.path.insert(0, str(SRC.parent))  # 让 `import scholarscout` 成立（SRC 是包目录本身）
    from scholarscout.tools import register_tools

    backend = sorted(SRC.rglob("*.py"))
    tests = sorted(TESTS.rglob("*.py"))
    docs = sorted(ROOT.glob("docs/*.md")) + sorted(ROOT.glob("prompts/*.md"))
    return {
        "后端源码": f"{len(backend)} 个 .py，{_lines(backend):,} 行",
        "前端": f"{len(list(UI.glob('*')))} 个文件，{_lines(sorted(UI.glob('*'))):,} 行",
        "测试": f"{len(tests)} 个文件，{_lines(tests):,} 行（pytest 用例 {_collect_tests()} 项）",
        "数据库迁移": f"{len(list(MIGRATIONS.glob('*.sql')))} 个 .sql",
        "模型可见工具": f"{len(register_tools())} 个",
        "设计文档": f"{len(docs)} 个文件，{_lines(docs):,} 行",
        "提交数": _git("rev-list", "--count", "HEAD"),
    }


def _collect_tests() -> int:
    """用 pytest 真收集一次（参数化展开后的用例数），比数 `def test_` 准确。"""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"(\d+)\s+tests?\s+collected", result.stdout)
    if match:
        return int(match.group(1))
    # pytest 8 的 `-q --collect-only` 逐文件输出 `path: N`，把它们加起来就是总数
    per_file = [
        int(found.group(1))
        for line in result.stdout.splitlines()
        if (found := re.search(r":\s*(\d+)\s*$", line))
    ]
    if per_file:
        return sum(per_file)
    return len([line for line in result.stdout.splitlines() if "::" in line])


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, indent=2))
