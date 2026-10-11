"""生成代码地图的符号清单（**唯一允许写入符号清单的方式**，禁止手改）。

依据（业界一手出处）：
- C4 模型官网（Simon Brown）Code diagram 页：代码级的图/清单 "Ideally ... automatically
  generated using tooling" —— 能生成的绝不手写。
- Chromium《Documentation Best Practices》："Minimum viable documentation"（少而准）、
  "Duplication is evil"（不重复）、"Update docs with code"（文档与代码同一提交）。

因此**文件清单 / 行数 / 导出符号 / 依赖方向**这类会随代码腐烂的内容一律由本脚本产出；
人只写各目录 `README.md` 里的"这一层是干什么的"（不随代码变的那部分）。

用法（在项目根目录）：
    python scripts/make_code_map.py                  # 重新生成 docs/代码地图/00-符号清单.md
    python scripts/make_code_map.py --check          # 只校验是否过期（门禁用；过期退出码 1）
    python scripts/make_code_map.py --roots src      # 只扫指定根目录

退出码：0 = 成功（`--check` 时为"与磁盘一致"）；1 = `--check` 发现过期。

`--check` 只比**结构**（文件清单、导出符号、依赖、docstring 首句），**行数不参与判定** ——
否则任何一次编辑都会改行数、门禁天天红，红久了就会被人无视（"狼来了"）。
行数仍写在文件里给人看，允许滞后；要刷新数字随时重跑生成器。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOTS: tuple[str, ...] = ("src", "scripts", "eval")
DEFAULT_OUT = "docs/代码地图/00-符号清单.md"

HEADER = """<!-- 由 scripts/make_code_map.py 生成，禁止手改；要改内容请改代码本体的 docstring 或改生成器。 -->
# 符号清单（自动生成）

> 生成器：`scripts/make_code_map.py` ｜ 覆盖根目录：{roots}
> **本文件不含时间戳，也没有本机绝对路径** —— 重复生成内容不变，可安全入库与 diff。
> 行数 = 总行数（含空行与注释，与 `wc -l` 一致）；门禁 `--check` 只比**结构**，**行数允许滞后**
> （要刷新数字就重跑 `python scripts/make_code_map.py`）。
> 各目录"是干什么的、为什么这么切"不在这里，看该目录的 `README.md`。

"""


@dataclass(frozen=True)
class FileFacts:
    """一个 Python 文件的静态事实（全部来自源码本身，无人工输入）。"""

    rel: str
    lines: int
    doc: str | None
    symbols: tuple[str, ...]
    deps: tuple[str, ...]
    broken: str | None = None


def _format_symbol(node: ast.stmt) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        names = [arg.arg for arg in node.args.args]
        if node.args.vararg is not None:
            names.append("*" + node.args.vararg.arg)
        if node.args.kwarg is not None:
            names.append("**" + node.args.kwarg.arg)
        return f"{node.name}({', '.join(names)})"
    if isinstance(node, ast.ClassDef):
        return f"class {node.name}"
    return None


def _dependencies(tree: ast.Module) -> tuple[str, ...]:
    """内部依赖（去掉 scholarscout. 前缀）与外部顶层模块名，去重后排序。"""
    internal: set[str] = set()
    external: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module is None:  # 相对导入
                continue
            if node.module == "scholarscout" or node.module.startswith("scholarscout."):
                internal.add(node.module.split("scholarscout.", 1)[-1])
            else:
                external.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name.split(".", 1)[0]
                if name == "scholarscout":
                    internal.add("<包根>")
                else:
                    external.add(name)
    return tuple(sorted(internal | external))


def describe(path: Path, root: Path) -> FileFacts:
    text = path.read_text(encoding="utf-8")
    rel = path.relative_to(root).as_posix()
    lines = len(text.splitlines())
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:  # 坏文件要显式暴露，不许静默跳过
        return FileFacts(rel, lines, None, (), (), f"解析失败：{exc.msg}（第 {exc.lineno} 行）")
    raw_doc = (ast.get_docstring(tree) or "").strip()
    doc = raw_doc.splitlines()[0].strip() if raw_doc else None
    symbols = [s for s in (_format_symbol(n) for n in tree.body) if s is not None]
    return FileFacts(rel, lines, doc, tuple(symbols), _dependencies(tree))


def _cell(text: str) -> str:
    """Markdown 表格单元格：竖线要转义，换行压平。"""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def collect(root: Path, roots: tuple[str, ...]) -> list[tuple[str, list[FileFacts]]]:
    """按目录分组收集；目录与文件都排序，保证输出稳定。"""
    groups: list[tuple[str, list[FileFacts]]] = []
    for name in roots:
        base = root / name
        if not base.is_dir():
            groups.append((f"{name}/", []))
            continue
        by_dir: dict[str, list[FileFacts]] = {}
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            parent = path.parent.relative_to(root).as_posix() + "/"
            by_dir.setdefault(parent, []).append(describe(path, root))
        for parent in sorted(by_dir):
            groups.append((parent, sorted(by_dir[parent], key=lambda f: f.rel)))
    return groups


def render_roots(root: Path, roots: tuple[str, ...]) -> str:
    groups = collect(root, roots)
    out = [HEADER.format(roots="、".join(f"`{r}`" for r in roots))]
    total_files = sum(len(files) for _, files in groups)
    total_lines = sum(f.lines for _, files in groups for f in files)
    out.append(f"合计：**{total_files}** 个 Python 文件、**{total_lines}** 行。\n")

    for parent, files in groups:
        if not files:
            out.append(f"## {parent}\n\n（目录不存在或没有 Python 文件）\n")
            continue
        out.append(f"## {parent}\n")
        out.append(f"共 {len(files)} 个文件、{sum(f.lines for f in files)} 行。\n")
        out.append("| 文件 | 行数 | 一句话（模块 docstring 首句） | 顶层符号 | 依赖 |")
        out.append("|---|---|---|---|---|")
        for f in files:
            if f.broken:
                doc = f"**⚠️ {_cell(f.broken)}**"
            elif f.doc:
                doc = _cell(f.doc)
            else:
                doc = "**⚠️ 无模块 docstring**"
            syms = "、".join(f"`{s}`" for s in f.symbols[:6])
            if len(f.symbols) > 6:
                syms += f" 等 {len(f.symbols)} 个"
            deps = "、".join(f"`{d}`" for d in f.deps[:6]) or "—"
            out.append(f"| `{Path(f.rel).name}` | {f.lines} | {doc} | {syms or '—'} | {deps} |")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


_LINES_JOIN = re.compile(r"、\**\d+\** 行")
_ROW_COUNT = re.compile(r"^(\| `[^`]+` \| )\d+( \|)", re.MULTILINE)


def normalize_structure(text: str) -> str:
    """抹掉"行数"字段，只留结构，供 `--check` 比对。

    两边都过同一个函数，所以只要结构（文件/符号/依赖/docstring 首句）一致就算通过；
    行数变化不判过期 —— 见模块 docstring 里的理由。
    """
    return _ROW_COUNT.sub(r"\1#\2", _LINES_JOIN.sub("", text))


def write_map(root: Path, roots: tuple[str, ...], target: Path, *, quiet: bool = False) -> int:
    text = render_roots(root, roots)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    if not quiet:
        rows = text.count("\n| `")
        print(f"[OK] 已写入 {target.name}（{rows} 个文件条目）")
    return 0


def check_map(root: Path, roots: tuple[str, ...], target: Path, *, quiet: bool = False) -> int:
    """过期检测：文件不存在，或**结构**与当前代码不一致 → 1（门禁用）。

    只比结构，不比行数：日常编辑会改行数，若把行数纳入判定，门禁会几乎每次都红。
    """
    if not target.exists():
        print(f"[FAIL] {target.name} 不存在；先跑 python scripts/make_code_map.py")
        return 1
    if normalize_structure(target.read_text(encoding="utf-8")) != normalize_structure(
        render_roots(root, roots)
    ):
        print(f"[FAIL] {target.name} 已过期（结构变了）；重跑 python scripts/make_code_map.py")
        return 1
    if not quiet:
        print(f"[OK] {target.name} 与代码一致（结构）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="make_code_map", description="生成/校验代码地图符号清单")
    parser.add_argument("--roots", nargs="+", default=list(DEFAULT_ROOTS), help="要扫描的根目录")
    parser.add_argument("--out", default=DEFAULT_OUT, help="输出文件（相对项目根）")
    parser.add_argument("--check", action="store_true", help="只校验是否过期，不写盘")
    parser.add_argument("--quiet", action="store_true", help="少打印")
    args = parser.parse_args(argv)

    roots = tuple(args.roots)
    target = ROOT / args.out
    if args.check:
        return check_map(ROOT, roots, target, quiet=args.quiet)
    return write_map(ROOT, roots, target, quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
