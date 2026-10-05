"""校验规则文件正文一致（铁律 #3 的强制手段）。

权威副本：`docs/项目规则.md`（必须存在）
分发副本：`AGENTS.md`（必须存在，各类 AI 读）、`CLAUDE.md`（**可选**）

关于 CLAUDE.md：2026-10-05 项目所有者决定**不创建**（本机不用 Claude Code）。
因此它是可选副本 —— 存在才校验，不存在只提示、不算失败。
要启用时直接 `cp AGENTS.md CLAUDE.md` 并把开头两行注释改对即可，正文必须逐字一致。

用法：
    python scripts/check_rules_sync.py            # 默认集合
    python scripts/check_rules_sync.py A B        # 指定两份（自测用）
退出码：0 = 一致；1 = 不一致 / 缺标记 / 缺必需文件。
"""

from __future__ import annotations

import sys
from pathlib import Path

BEGIN = "<!-- RULES:BEGIN -->"
END = "<!-- RULES:END -->"

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "docs" / "项目规则.md"
REQUIRED: tuple[Path, ...] = (CANONICAL, ROOT / "AGENTS.md")
OPTIONAL: tuple[Path, ...] = (ROOT / "CLAUDE.md",)


def display(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


def rule_body(path: Path) -> str:
    """取出 RULES 标记之间的正文，两端空白归一化。"""
    text = path.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        raise ValueError(f"{display(path)}: 缺少 {BEGIN} / {END} 标记")
    _, _, tail = text.partition(BEGIN)
    body, _, _ = tail.partition(END)
    return body.strip()


def _first_diff(left: str, right: str, left_name: str, right_name: str) -> None:
    a_lines = left.splitlines()
    b_lines = right.splitlines()
    for lineno, (x, y) in enumerate(zip(a_lines, b_lines, strict=False), start=1):
        if x != y:
            print(f"  首处差异在第 {lineno} 行：")
            print(f"    {left_name}: {x!r}")
            print(f"    {right_name}: {y!r}")
            break
    if len(a_lines) != len(b_lines):
        print(f"  行数不同：{left_name}={len(a_lines)} {right_name}={len(b_lines)}")


def main(paths: tuple[Path, ...] | None = None) -> int:
    if paths is not None:
        targets = paths
        optional: tuple[Path, ...] = ()
    else:
        targets = REQUIRED
        optional = OPTIONAL

    bodies: dict[Path, str] = {}
    missing_required: list[Path] = []
    for path in targets:
        if not path.exists():
            missing_required.append(path)
            continue
        try:
            bodies[path] = rule_body(path)
        except ValueError as exc:
            print(f"[FAIL] {exc}")
            return 1

    if missing_required:
        for path in missing_required:
            print(f"[FAIL] 规则文件不存在：{display(path)}")
        return 1

    skipped: list[Path] = []
    for path in optional:
        if not path.exists():
            skipped.append(path)
            continue
        try:
            bodies[path] = rule_body(path)
        except ValueError as exc:
            print(f"[FAIL] {exc}")
            return 1

    reference = targets[0]
    reference_body = bodies[reference]
    failed = False
    for path in list(targets[1:]) + list(optional):
        if path not in bodies:
            continue
        if bodies[path] != reference_body:
            print(f"[FAIL] {display(path)} 与权威副本 {display(reference)} 不一致")
            _first_diff(reference_body, bodies[path], display(reference), display(path))
            failed = True
    if failed:
        return 1

    checked = len(bodies)
    print(f"[OK] 规则正文一致（{checked} 份，{len(reference_body.splitlines())} 行）")
    if skipped:
        names = "、".join(display(p) for p in skipped)
        print(f"     （可选副本未创建，跳过：{names}）")
    return 0


if __name__ == "__main__":
    args = [Path(a).resolve() for a in sys.argv[1:]]
    sys.exit(main(tuple(args) if args else None))
