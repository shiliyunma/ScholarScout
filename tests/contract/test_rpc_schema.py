"""契约闸 1/4：`docs/01 §3` 方法表与 `rpc.method_names()` **双向一致**（docs/01 §7）。

为什么这道闸必须有：方法表是实现之外唯一一份"有哪些接口"的清单。

- 只查 `代码 ⊆ 文档`：后端能加方法而文档不动 —— 论文/答辩里说不清系统到底有什么能力；
- 只查 `文档 ⊆ 代码`：文档画出"契约"而代码没有 —— 客户端照着写就拿到 `-32601`。

所以两个方向都断言，且未实现的 3 条**只能**走显式白名单（`scripts/check_rpc_docs.UNIMPLEMENTED`，
理由写在 `docs/01 §3.11`）。白名单与 §3.11 那张表必须逐条一致：两边各写一份就迟早不同步。

解析与差集**不在这里另写一份** —— 直接复用 `scripts/check_rpc_docs.py`（同一个正则、同一份白名单），
命令行跑它和跑 pytest 得到的是同一结论。这与 `research_state.similarity` 的经验一致：
两处各写一份判定，迟早给出两套数字。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = ROOT / "docs" / "01-接口契约.md"


def _load_checker() -> Any:
    """按路径加载 `scripts/check_rpc_docs.py`（scripts/ 不是包，不搞 sys.path 污染）。"""
    path = ROOT / "scripts" / "check_rpc_docs.py"
    spec = importlib.util.spec_from_file_location("check_rpc_docs", path)
    assert spec is not None and spec.loader is not None, f"加载失败：{path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def test_code_methods_are_all_documented() -> None:
    """`method_names()` 里的每个方法都必须在 §3 方法表里有行。"""
    result = checker.diff()
    assert result["code_only"] == [], (
        f"这些方法已注册但 docs/01 §3 没有登记（可用能力在文档里看不见）：{result['code_only']}"
    )


def test_documented_methods_exist_or_are_whitelisted() -> None:
    """文档里写的方法**必须真的能调**；唯一的例外是 `UNIMPLEMENTED` 白名单。"""
    result = checker.diff()
    unknown = [name for name in result["doc_only"] if name not in checker.UNIMPLEMENTED]
    assert unknown == [], f"这些方法文档里有、代码里没有，且不在白名单：{unknown}"


def test_whitelist_matches_known_unimplemented_section() -> None:
    """白名单键集必须与 `docs/01 §3.11` 那张表逐条一致（少一条/多一条都红）。"""
    result = checker.diff()
    assert result["doc_unimplemented"] == set(checker.UNIMPLEMENTED), (
        "白名单与 §3.11 表不一致：文档表里是 "
        f"{sorted(result['doc_unimplemented'])}，白名单是 {sorted(checker.UNIMPLEMENTED)}"
    )


def test_whitelisted_methods_are_really_unimplemented() -> None:
    """白名单里的方法如果真被实现了，就必须从白名单删掉（否则会掩盖漏登记）。"""
    stale = checker.diff()["stale_whitelist"]
    assert stale == [], f"这些方法已实现，请从 UNIMPLEMENTED 删除并补文档行：{stale}"


def test_event_table_lists_chat_needs_choice_with_session_id() -> None:
    """`chat.needs_choice` 必须在 §4 事件表里，且载荷带 `session_id`（否则多会话会串）。"""
    text = DOC_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("## 4."))
    end = next(i for i, line in enumerate(lines[start + 1 :], start + 1) if line.startswith("## "))
    rows = [
        line
        for line in lines[start:end]
        if line.startswith("|") and "`chat.needs_choice`" in line.split("|")[1]
    ]
    assert len(rows) == 1, f"§4 事件表里 chat.needs_choice 应恰好 1 行，实际 {len(rows)} 行"
    assert "session_id" in rows[0], "chat.needs_choice 的载荷必须带 session_id"
    for field in ("turn_id", "query", "options", "intent"):
        assert field in rows[0], f"chat.needs_choice 载荷缺字段：{field}"
