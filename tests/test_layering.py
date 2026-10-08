"""分层门禁：用 import 方向把架构钉住。

为什么要有：分层的价值全在「上层可以依赖下层，反过来不行」。这条约束靠自觉守不住 ——
一次"图省事直接 import"就把两层缠死，之后谁也拆不开。所以写成测试（借鉴 DeepTutor 用
import-linter 进 CI 的思路，但**不引新依赖**：标准库 ast 就够，也免得过依赖白名单）。

规则表在下面。如果某条规则和真实需要冲突，**改这张表并把理由写清楚**，不要删规则。
另有一条元规则：`scholarscout` 下的每个顶层包都必须出现在规则表里 ——
新增模块时忘了想"它在哪一层"，这条会当场拦下。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "scholarscout"

#: 顶层包 → 它**不允许**直接 import 的上层包。
#: 方向：state / learning / grading / providers / fetch（下） ← runtime ← tools ← rpc ← desktop（上）
RULES: dict[str, tuple[str, ...]] = {
    # 数据与状态层：谁都不该依赖它上面的东西
    "state": ("runtime", "rpc", "desktop", "tools"),
    # 外部能力：模型 / 抓取，只依赖自己的基础设施
    "providers": ("runtime", "rpc", "desktop", "state", "tools"),
    "fetch": ("runtime", "rpc", "desktop", "state", "tools"),
    # 领域逻辑：可以用 state，不该碰运行时、协议、界面
    "learning": ("runtime", "rpc", "desktop", "tools"),
    "grading": ("runtime", "rpc", "desktop", "tools"),
    # 运行时（回合循环 / 工具注册）：可以用下面的全部，不该碰协议与界面
    "runtime": ("rpc", "desktop"),
    # 工具实现：同上
    "tools": ("rpc", "desktop"),
    # 协议层：当然不能反过来依赖界面
    "rpc": ("desktop",),
    # 桌面外壳：只通过 RPC 跟后端说话，不该伸手进运行时
    "desktop": ("runtime",),
    # 上面这些的"顶层"：入口与叶子工具，可以依赖下层一切
    "app": (),
    "__main__": (),
    "paths": (),
    "summary": (),
}

#: 这几个顶层"包"其实是文件，也一并纳入（包名 → 规则名）
FILE_AS_PACKAGE = {"config": "config", "settings_io": "settings_io"}


def _module_name(path: Path) -> str:
    """src/scholarscout/runtime/loop.py → runtime；src/scholarscout/config.py → config。"""
    rel = path.relative_to(SRC)
    return rel.parts[0].removesuffix(".py") if len(rel.parts) == 1 else rel.parts[0]


def _imports(path: Path) -> set[str]:
    """这个文件里直接 import 的 scholarscout 子包名（不看绝对/相对，都能认）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("scholarscout."):
                found.add(module.split(".", 2)[1])
            # 相对导入：from ..runtime import x → level=2
            if node.level and module:
                found.add(module.split(".", 1)[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("scholarscout."):
                    found.add(alias.name.split(".", 2)[1])
    return found


def _violations() -> list[str]:
    problems: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        owner = _module_name(path)
        forbidden = RULES.get(owner)
        if not forbidden:
            continue
        for imported in _imports(path):
            if imported in forbidden:
                rel = path.relative_to(SRC)
                problems.append(f"{rel} 依赖了上层 {imported}（{owner} 不该 import {imported}）")
    return problems


def test_no_layer_imports_a_higher_layer() -> None:
    problems = _violations()
    assert not problems, "分层被打穿了：\n" + "\n".join(problems)


def test_every_top_level_package_has_a_rule() -> None:
    """元规则：新增顶层包必须想清楚它在哪一层，否则这里报错。"""
    present = {p.name for p in SRC.iterdir() if p.is_dir() and not p.name.startswith("__")}
    present |= {p.stem for p in SRC.glob("*.py") if p.stem != "__init__"}
    uncovered = sorted(present - set(RULES) - set(FILE_AS_PACKAGE))
    assert not uncovered, f"这些顶层包还没有分层规则：{uncovered}"


@pytest.mark.parametrize("rule_name", sorted(RULES))
def test_rule_name_is_a_real_package(rule_name: str) -> None:
    """规则表里的名字必须真实存在（改名之后规则不能变成"永远通过"）。"""
    assert (SRC / rule_name).exists() or (SRC / f"{rule_name}.py").is_file()
