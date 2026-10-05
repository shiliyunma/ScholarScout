"""校验规则文件正文一致（铁律 #3 的强制手段）。

权威副本：docs/项目规则.md
分发副本：AGENTS.md（各类 AI 读）、CLAUDE.md（Claude Code 读）

用法：
    python scripts/check_rules_sync.py            # 默认三份
    python scripts/check_rules_sync.py A B        # 指定两份（自测用）
退出码：0 = 全部一致；1 = 不一致 / 缺标记 / 缺文件。
"""

from __future__ import annotations

import sys
from pathlib import Path

BEGIN = "<!-- RULES:BEGIN -->"
END = "<!-- RULES:END -->"
ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "docs" / "项目规则.md"
FILES: tuple[Path, ...] = (CANONICAL, ROOT / "AGENTS.md", ROOT / "CLAUDE.md")


def rule_body(path: Path) -> str:
    """取出 RULES 标记之间的正文，两端空白归一化。"""
    text = path.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        raise ValueError(f"{path.name}: 缺少 {BEGIN} / {END} 标记")
    _, _, tail = text.partition(BEGIN)
    body, _, _ = tail.partition(END)
    return body.strip()


def _first_diff(left: str, right: str, left_name: str, right_name: str) -> None:
    a_lines = left.splitlines()
    b_lines = right.splitlines()
    for lineno, (x, y) in enumerate(zip(a_lines, b_lines), start=1):
        if x != y:
            print(f"  首处差异在第 {lineno} 行：")
            print(f"    {left_name}: {x!r}")
            print(f"    {right_name}: {y!r}")
            break
    if len(a_lines) != len(b_lines):
        print(f"  行数不同：{left_name}={len(a_lines)} {right_name}={len(b_lines)}")


def main(paths: tuple[Path, ...] = FILES) -> int:
    bodies: dict[Path, str] = {}
    missing: list[Path] = []
    for path in paths:
        if not path.exists():
            missing.append(path)
            continue
        try:
            bodies[path] = rule_body(path)
        except ValueError as exc:
            print(f"[FAIL] {exc}")
            return 1

    if missing:
        for path in missing:
            print(f"[FAIL] 分发副本不存在：{path.relative_to(ROOT)}")
        print("  说明：写入 agent 指令文件（AGENTS.md / CLAUDE.md）需项目所有者显式授权后创建，")
        print("        内容取自 docs/项目规则.md 的 RULES 段。")
        return 1

    if not bodies:
        print("[FAIL] 没有任何规则文件")
        return 1

    ref_path = paths[0]
    ref_body = bodies[ref_path]
    failed = False
    for path in paths[1:]:
        if bodies[path] != ref_body:
            print(f"[FAIL] {path.name} 与权威副本 {ref_path.name} 不一致")
            _first_diff(ref_body, bodies[path], ref_path.name, path.name)
            failed = True
    if failed:
        return 1

    print(f"[OK] 规则正文一致（{len(paths)} 份，{len(ref_body.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    args = [Path(a).resolve() for a in sys.argv[1:]]
    sys.exit(main(tuple(args) if args else FILES))
