"""分层门禁（第二条）：**非 `state/` 不许碰 SQL**。

`docs/03` §2 写着「`state/*` 是唯一碰 SQL 的地方」，但 `tests/test_layering.py` 只钉 import
方向 —— 它看不见「代码里直接写 SQL」。这条规则因此长期没有闸：评审发现时已经长到 69 处
（见 `docs/23` 的缺陷行）。没有闸的规则等于没有规则，所以这里补一道**递减白名单**：

- 现状**逐文件计次**冻结在下面的 `ALLOWED` 里：同一个文件里**再添一处**也会红，不只是新文件；
- 只许减不许增：修掉一处就把对应数字减一，数字归零就删掉整个条目；
- 修完之后白名单条目还在 = 僵尸条目 → `test_whitelist_entry_is_alive` 当场拦下。

**扫描口径**（改判据前先想清楚，它决定这道闸能拦什么）：

- 工具是 `ast`，不是正则：注释里的 `execute(`、字符串里的 `"SELECT …"` 都不会被误计，
  改写法（换引号、拆行、隐式拼接）也躲不过；
- 计的是**调用点**：`Call` 节点，方法名 ∈ `SQL_METHODS`，且**接收者是库句柄**
  （`db` / `ctx.db` / `conn` / `cursor` —— 取表达式最后一段名字判断）。
  为什么把 `query` / `query_one` 也算进来：本项目的 SQL 全部经 `state/db.py::Database` 门面，
  `db.query("SELECT …")` 与 `conn.execute(…)` 一样是「在 `state/` 之外写 SQL」；
- 范围：`src/scholarscout/**/*.py`，跳过 `state/` 与 `__pycache__`。

修复方向只有一个：**把这次查询搬进 `state/` 的仓储函数**，让调用方调函数而不是写 SQL。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "scholarscout"

#: 库门面上会跑 SQL 的方法名。
SQL_METHODS = frozenset({"execute", "executemany", "executescript", "query", "query_one"})

#: 库句柄的变量名（取表达式最后一段：`ctx.db` → `db`）。
DB_RECEIVERS = frozenset({"db", "conn", "cursor"})

#: **递减白名单：只许减不许增。**
#:
#: 键 = 相对 `src/scholarscout/` 的路径，值 = 该文件里 SQL 调用点的处数。
#: 修掉一处 SQL，就把那个数字减一；减到 0 删掉整条（自检会拦僵尸条目）。
#: **新增 SQL 不许往这里加数字**：先把查询搬进 `state/`，再来把数字改小。
#: 现值：2026-10-23 实测 **18 处 / 11 个模块**（`tools/source_grade.py` 的 3 处已下沉到
#: `state/source_context.py` 的 `ungated_sources` / `source_by_url_canonical`，按 id 那一条与
#: `tools/source_context.py` 共用 `source_by_id`，该条整条删除）。
#: 上一轮快照：2026-10-23 为 21 处 / 12 个模块（`tools/source_context.py` 的 3 处下沉到
#: `state/source_context.py` 的 `source_by_id` / `graded_sources_for_topic` / `recent_sources`，
#: 该条整条删除）。
#: 更早快照：2026-10-23 为 28 处 / 14 个模块（`grading/support.py` 的 5 处下沉到
#: `state/support.py`）；2026-10-22 为 33 处 / 15 个模块（`runtime/research_view.py` 的 7 处下沉到
#: `state/research_board.py` / `state/support.py`；`runtime/learner_profile.py` 的 7 处更早已下沉到
#: `state/learner_profile.py`；`runtime/memory.py` 的 9 处下沉到 `state/facts.py` /
#: `state/sessions.py` / `state/memory.py`）；再往前为 40 处 / 16 个模块；47 处 / 17 个模块；
#: 2026-10-21 为 56 处 / 18 个模块；评审清单记的 69 处是更早的粗计数，差别见 `docs/23` 那一行。
ALLOWED: dict[str, int] = {
    "grading/tiers.py": 1,
    "library/index.py": 2,
    "library/migrate.py": 2,
    "library/naming.py": 1,
    "rpc/channel_file.py": 2,
    "rpc/session.py": 2,
    "runtime/budget.py": 1,
    "runtime/compaction.py": 1,
    "runtime/practice.py": 2,
    "runtime/research_state.py": 1,
    "tools/source_fetch.py": 3,
}


def _receiver_name(func: ast.expr) -> str:
    """`db.query` → `db`；`ctx.db.query` → `db`；`self.conn.execute` → `conn`。"""
    target = func.value if isinstance(func, ast.Attribute) else None
    if target is None:
        return ""
    return ast.unparse(target).rsplit(".", 1)[-1]


def _sql_sites(path: Path) -> int:
    """这个文件里 SQL 调用点的处数（口径见模块 docstring）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in SQL_METHODS and _receiver_name(node.func) in DB_RECEIVERS:
            count += 1
    return count


def _actual() -> dict[str, int]:
    """`state/` 之外每个文件的 SQL 调用点数（只收 > 0 的文件）。"""
    found: dict[str, int] = {}
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(SRC)
        if rel.parts[0] == "state":
            continue
        count = _sql_sites(path)
        if count:
            found[rel.as_posix()] = count
    return found


def test_no_sql_outside_state() -> None:
    """实际命中必须**逐文件等于**白名单：多一处红，少一处也红（忘了改数字）。"""
    actual = _actual()
    problems: list[str] = []

    for rel in sorted(set(actual) | set(ALLOWED)):
        now = actual.get(rel, 0)
        frozen = ALLOWED.get(rel, 0)
        if now > frozen:
            problems.append(f"{rel}：白名单 {frozen} 处 → 实际 {now} 处（多了 {now - frozen} 处）")
        elif now < frozen:
            problems.append(
                f"{rel}：白名单 {frozen} 处 → 实际 {now} 处（少了 {frozen - now} 处，修好了就把数字改小）"
            )

    assert not problems, (
        "非 state/ 的 SQL 和白名单对不上：\n  "
        + "\n  ".join(problems)
        + "\n\n怎么办：把这次查询搬进 state/ 的仓储函数（调用方调函数，不写 SQL），"
        "然后把白名单里对应数字改小 —— 白名单**只许减不许增**。"
        "若确实要在 state/ 之外写 SQL，先想清楚为什么 state/ 放不下，再改数字并在 docs/23 记一条。"
    )


@pytest.mark.parametrize("rel", sorted(ALLOWED))
def test_whitelist_entry_is_alive(rel: str) -> None:
    """白名单条目必须是活的：文件真存在、且确实还有 SQL —— 防止重构后留下永远通过的死条目。"""
    path = SRC / rel
    assert path.is_file(), (
        f"白名单里的 {rel} 不存在了（改名或删除？）：把这条从 ALLOWED 里删掉，"
        "否则它会变成永远通过的死条目，掩盖真正的新增 SQL"
    )
    count = _sql_sites(path)
    assert count > 0, (
        f"白名单里的 {rel} 已经一处 SQL 都没有了：把这条从 ALLOWED 里删掉 —— "
        "白名单只该记「还没修掉的」，不该记「已经干净的」"
    )
    assert count <= ALLOWED[rel], (
        f"{rel} 实际 {count} 处，白名单记的 {ALLOWED[rel]} 处偏大：把数字改小到实际值"
    )
