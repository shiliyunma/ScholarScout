"""分层门禁（第二条）：**全仓 `state/` 之外零 SQL**。

`docs/03` §2 写着「`state/*` 是唯一碰 SQL 的地方」，但 `tests/test_layering.py` 只钉 import
方向 —— 它看不见「代码里直接写 SQL」。这条规则因此长期没有闸：评审发现时已经长到 69 处
（见 `docs/23` 的缺陷行）。没有闸的规则等于没有规则，所以这里补一道闸，口径是**现状即断言**：

- `state/` 之外**一处 SQL 都不许有**：起点 2026-10-21 是 56 处 / 18 个模块，2026-10-23 清零到
  **0 处 / 0 个模块**（逐轮「谁、几处、去向」全记在 `docs/23`）；
- 下面的 `ALLOWED` **必须是空的** —— 它不是「还能放几处」的配额，而是历史遗留的递减白名单：
  数字归零就删条目，条目全删就是这个空字典。断言是「实际 == 白名单」，白名单为空即「必须 0 处」，
  所以**再添一处当场红**；想加数字之前先问「为什么这条查询搬不进 `state/`」；
- `test_new_cross_layer_sql_is_caught_immediately` 是**证伪用例**：它真的造一个越层 SQL 文件
  跑同一套判据，确认这道闸会当场报出它 —— 防的是「判据悄悄坏掉（`SQL_METHODS` 被改窄、
  `state/` 跳过条件写宽、AST 遍历改成只看模块顶层）而闸还一片绿灯」这种假绿。

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
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "scholarscout"

#: 库门面上会跑 SQL 的方法名。
SQL_METHODS = frozenset({"execute", "executemany", "executescript", "query", "query_one"})

#: 库句柄的变量名（取表达式最后一段：`ctx.db` → `db`）。
DB_RECEIVERS = frozenset({"db", "conn", "cursor"})

#: **空字典 = 现状即断言：全仓 `state/` 之外零 SQL。**
#:
#: 它曾经是「递减白名单」：键 = 相对 `src/scholarscout/` 的路径，值 = 该文件还没搬走的 SQL 处数，
#: 断言是「实际 == 白名单」（多了红、少了也红 —— 少了说明修好了却没改数字）。2026-10-21 起点是
#: **56 处 / 18 个模块**，此后逐轮一处一处搬进 `state/`，**2026-10-23 清零到 0 处 / 0 个模块**，
#: 于是最后一条条目也删掉、留下这个空字典（谁、几处、搬到哪个仓储函数，逐轮记在 `docs/23`）。
#:
#: 空字典不是「暂时没东西可填」，它就是这条规则本身：`_actual()` 回来的每一处都会被判红。
#: 所以**不要再往里加数字** —— 遇到「这条查询能不能留在 `state/` 之外」时，先答「为什么搬不进
#: `state/` 的仓储函数」，答不出来就搬。
ALLOWED: dict[str, int] = {}


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


def _mismatches(actual: dict[str, int], allowed: dict[str, int]) -> list[str]:
    """判据本体：实际 vs 白名单，逐文件比。返回空列表 = 过关。

    单独抽出来是为了让**证伪用例**能拿同一份判据去判一个故意违规的目录 —— 判据只有一份，
    证伪用例证明的就是这一份没坏。
    """
    problems: list[str] = []

    for rel in sorted(set(actual) | set(allowed)):
        now = actual.get(rel, 0)
        frozen = allowed.get(rel, 0)
        if now > frozen:
            problems.append(f"{rel}：白名单 {frozen} 处 → 实际 {now} 处（多了 {now - frozen} 处）")
        elif now < frozen:
            problems.append(
                f"{rel}：白名单 {frozen} 处 → 实际 {now} 处（少了 {frozen - now} 处，修好了就把数字改小）"
            )
    return problems


def test_no_sql_outside_state() -> None:
    """实际命中必须**逐文件等于**白名单：多一处红，少一处也红（忘了改数字）。"""
    problems = _mismatches(_actual(), ALLOWED)

    assert not problems, (
        "非 state/ 的 SQL 和白名单对不上：\n  "
        + "\n  ".join(problems)
        + "\n\n怎么办：把这次查询搬进 state/ 的仓储函数（调用方调函数，不写 SQL），"
        "然后把白名单里对应数字改小 —— 白名单**只许减不许增**。"
        "若确实要在 state/ 之外写 SQL，先想清楚为什么 state/ 放不下，再改数字并在 docs/23 记一条。"
    )


def test_new_cross_layer_sql_is_caught_immediately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**证伪用例**：这道闸对越层 SQL 真的会红，而不是「碰巧绿」。

    闸的判据全是结构性条件（`SQL_METHODS` 的集合、`state/` 跳过条件、AST 遍历范围），把它们改窄一点
    —— 比如从 `SQL_METHODS` 里拿掉 `query`，或者跳过多一层目录 —— 真实仓库在清零之后依然一片绿，
    而这道闸已经看不懂新写的 SQL 了。这种**假绿**只能靠「造一个必然违规的输入、看判据是否判红」来防。

    做法：`monkeypatch` 把扫描根 `SRC` 指向 `tmp_path`（同一套 `_actual()` 与 `_mismatches()`，
    不复制判据），在里头造 `runtime/bad.py`，正文是 `ctx.db.query("SELECT 1")` —— 与
    `runtime/research_state.py` 搬走之前**一模一样**的越层写法；再放一个 `state/good.py` 写同样的
    调用，它必须被跳过（否则「`state/` 是唯一允许碰 SQL 的地方」这半条规则也一起坏了）。
    """
    (tmp_path / "runtime").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "runtime" / "bad.py").write_text(
        'def gaps(ctx):\n    return ctx.db.query("SELECT 1")\n',
        encoding="utf-8",
    )
    (tmp_path / "state" / "good.py").write_text(
        'def rows(db):\n    return db.query("SELECT 1")\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "SRC", tmp_path)

    actual = _actual()
    assert actual == {"runtime/bad.py": 1}, f"闸没认出越层 SQL（只能报 {actual!r}）"

    # 判据必须对它判红；空 `ALLOWED` 下「多了一处」就是红 —— 这正是真实新增一处越层 SQL 时的输出。
    problems = _mismatches(actual, ALLOWED)
    assert problems, "越层 SQL 摆在眼前，判据却判绿 —— 这道闸已经失去判别能力"
    assert "runtime/bad.py" in problems[0]
    assert "多了 1 处" in problems[0]


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
