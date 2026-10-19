"""校验 `docs/01 §3` 方法表与 `rpc.method_names()` **双向一致**（缺登记、多登记都算失败）。

为什么要这个脚本：方法表是实现之外唯一一份"有哪些接口"的清单。只靠人来同步，
迟早在某一侧多出方法而门禁全绿 —— 前端调一个不存在的名字拿到 `-32601`，或者
后端加了个方法论文里一个字都没有。所以两个方向都对比：

1. `method_names() - 文档` ≠ ∅ → **有方法没登记**（可用能力在文档里看不见）；
2. `文档 - method_names()` ≠ ∅ → **文档有、代码没有**（契约画了张空头支票）。

第 2 类只允许 `UNIMPLEMENTED` 里显式列出的条目，且这些条目**必须**与 `docs/01 §3.11`
那张表逐字一致（表里写的方法名 = 白名单键）。三个方向互相钉住：

- 文档多写一条 → (2) 里有它但不在白名单 → 红；
- 白名单多一条 → 与 §3.11 表不一致 → 红；
- 白名单里的方法真被实现了 → 它同时出现在 `method_names()` 里 → "白名单已实现" → 红（提醒删白名单）。

用法（项目根目录）：
    python scripts/check_rpc_docs.py
退出码：0 = 双向差集为空（未实现的 3 条走白名单）；1 = 不满足。

`tests/contract/test_rpc_schema.py` 直接 import 本模块的 `parse_doc_methods()` /
`parse_known_unimplemented()` / `diff()`，不另写一份解析 —— 两处各写一份正则，
迟早给出两套结论（与 `research_state.similarity` 同一条教训）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

DOC_PATH = ROOT / "docs" / "01-接口契约.md"

#: **已知未实现**：契约写了、代码里没有（`docs/01 §3.11` 逐条写明原因与绕法）。
#: 只允许这 3 条；要往里加东西必须先改 `docs/01 §3.11`（两处对不上就红）。
UNIMPLEMENTED: dict[str, str] = {
    "system.shutdown": "关窗走桌面壳 bridge.shutdown()，RPC 自自杀未落地",
    "source.get": "按 source 反查 documents/summaries/mentions 的聚合口未实现",
    "eval.export": "离线评测直接读库（eval/），不经 RPC",
}

#: 方法名：点号分段的小写标识符（`feishu.pairing.create` 这种多段也算）
_NAME = re.compile(r"`([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)`")

#: 表格首列写着它的才是方法表（§3.7 那张表首列是"内容"，里面是 `runtime.plan_by_model` 这类配置键）
_METHOD_HEADER = "方法"


def _table_names(text: str, *, only_subsection: str | None) -> set[str]:
    """取出 §3 里首列为「方法」的表格中的方法名；给了 `only_subsection` 就只取那一节。"""
    names: set[str] = set()
    in_section = False
    in_target = only_subsection is None
    in_table = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = line.startswith("## 3.")
            in_target = only_subsection is None
            in_table = False
            continue
        if line.startswith("### "):
            in_target = only_subsection is None or line.startswith(only_subsection)
            in_table = False
            continue
        if not line.startswith("|"):
            in_table = False
            continue
        cells = line.split("|")
        if len(cells) < 3:
            continue
        first = cells[1].strip()
        if first == _METHOD_HEADER:
            in_table = True
            continue
        if first and set(first) <= {"-", ":", " "}:
            continue  # 表头分隔行（| --- | --- |），表格还在继续
        if not (in_section and in_target and in_table):
            continue
        names.update(_NAME.findall(first))
    return names


def parse_doc_methods(text: str) -> set[str]:
    """文档里出现过的全部方法名（含 §3.11 那 3 条未实现的 —— 它们靠白名单兜住）。"""
    return _table_names(text, only_subsection=None)


def parse_known_unimplemented(text: str) -> set[str]:
    """`docs/01 §3.11`「已知未实现」表里列的方法名。"""
    return _table_names(text, only_subsection="### 3.11")


def diff() -> dict[str, Any]:
    """两个方向的差集 + 白名单与 §3.11 的一致性。"""
    from scholarscout.rpc.methods import method_names  # 注册入口（import 即注册）

    text = DOC_PATH.read_text(encoding="utf-8")
    code = set(method_names())
    doc = parse_doc_methods(text)
    return {
        "code": code,
        "doc": doc,
        "doc_unimplemented": parse_known_unimplemented(text),
        "code_only": sorted(code - doc),
        "doc_only": sorted(doc - code),
        "stale_whitelist": sorted(set(UNIMPLEMENTED) & code),
    }


def main() -> int:
    result = diff()
    code_only = result["code_only"]
    doc_only = result["doc_only"]
    stale = result["stale_whitelist"]
    doc_unimpl = result["doc_unimplemented"]
    whitelist_mismatch = sorted(doc_unimpl ^ set(UNIMPLEMENTED))
    unknown = [name for name in doc_only if name not in UNIMPLEMENTED]

    print(f"method_names(): {len(result['code'])} 条；docs/01 §3 方法表: {len(result['doc'])} 条")
    print(f"A. 代码有、文档没登记（应为空）: {code_only if code_only else '[]'}")
    print(
        f"B. 文档有、代码没有（应只剩白名单 {len(UNIMPLEMENTED)} 条）: {doc_only if doc_only else '[]'}"
    )
    for name in doc_only:
        print(f"   - {name}: {UNIMPLEMENTED.get(name, '!! 不在白名单，必须补实现或从文档移除')}")
    print(f"C. 白名单已实现（应从 UNIMPLEMENTED 删除）: {stale if stale else '[]'}")
    print(
        f"D. 白名单与 §3.11 表不一致（应为空）: {whitelist_mismatch if whitelist_mismatch else '[]'}"
    )

    if code_only or stale or unknown or whitelist_mismatch:
        print("结果：不合（双向差集 / 白名单与文档不一致）")
        return 1
    print("结果：通过（双向差集为空；文档白名单仅剩未实现的 3 条，与 §3.11 逐条一致）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
