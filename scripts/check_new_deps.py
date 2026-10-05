"""依赖审批校验（铁律 #6：新增依赖要审批）。

比对 pyproject.toml 的运行期依赖与 deps-allowlist.txt，出现未审批依赖即退出码 1。

用法：python scripts/check_new_deps.py
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
ALLOWLIST = ROOT / "deps-allowlist.txt"


# 依赖名 → 规范包名：取 PEP 508 描述里的名字部分（去掉版本区间、extras、环境标记）
def normalize(spec: str) -> str:
    name = spec.split(";", 1)[0].strip()
    for sep in ("[", ">", "<", "=", "!", "~", " "):
        name = name.split(sep, 1)[0]
    return name.strip().lower().replace("_", "-")


def load_allowlist(path: Path) -> set[str]:
    if not path.exists():
        print(f"[FAIL] 找不到白名单文件：{path.name}")
        sys.exit(1)
    names: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.add(normalize(line))
    return names


def main() -> int:
    if not PYPROJECT.exists():
        print(f"[FAIL] 找不到 {PYPROJECT.name}")
        return 1

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    deps = data.get("project", {}).get("dependencies", [])
    allowed = load_allowlist(ALLOWLIST)

    unapproved = sorted({normalize(d) for d in deps} - allowed)
    if unapproved:
        print("[FAIL] 发现未审批的运行期依赖：")
        for name in unapproved:
            print(f"    - {name}")
        print(
            "  处理：按 docs/项目规则.md §4 提交审批，登记进 docs/03 §6，再加入 deps-allowlist.txt"
        )
        return 1

    print(f"[OK] 运行期依赖 {len(deps)} 项，全部在白名单内")
    return 0


if __name__ == "__main__":
    sys.exit(main())
