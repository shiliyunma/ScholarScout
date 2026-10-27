"""资料库搜索结果可见性的守卫（缺陷 D4：搜完像没搜）。

证据（`run/ui_eval/final/defects.md`、`shots/s5-library.png`）：搜「指针」命中 50 条，
**50 条全在 13 个默认折叠的分组里**（`display:none`），搜索框下面看着像空的。

这一组测试钉三件事：
  ① 有搜索词时命中分组默认展开、组头只报本组命中数、列表顶部给「共 N 条，分布在 M 组」；
  ② 0 命中必须给「没找到包含「词」的资料」+ 换词建议 + 清空搜索，绝不留空白列表；
  ③ 搜索态点组头仍能折回去，且重画之后保持（状态不写死成"永远展开"）；
     无搜索词时仍是原来的默认折叠。

分工：行为由 `sources_search_probe.js` 在 node 里跑**真源码**（真 `loadSources` + 假 DOM +
桩 `archive.list`）；浏览器里的 `display:none` 由 `run/library_search_probe.py` 在无头 Edge
里量（`python run/library_search_probe.py --shot`）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ._ui import STYLE_CSS, UI_DIR

SOURCES_JS = (UI_DIR / "sources.js").read_text(encoding="utf-8")
PROBE_JS = Path(__file__).resolve().parent / "sources_search_probe.js"

#: 这套设计令牌体系里**真的定义了**的那些（--ui-* / --radius-*）；其余名字写进声明等于把
#: 那条声明废掉（项目里踩过：`var(--border)` 让整条 border 失效）。
LEGACY_TOKENS = ("--border", "--foreground", "--muted-foreground", "--accent", "--background")

#: 本缺陷新增/改动的样式选择器
NEW_SELECTORS = ("source-summary", "source-empty")


@pytest.fixture(scope="module")
def probe_results() -> list[dict[str, object]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("没装 node，跳过搜索结果行为那部分")
    assert PROBE_JS.is_file(), f"缺少用例集：{PROBE_JS}"
    completed = subprocess.run(
        [node, str(PROBE_JS), str(UI_DIR / "sources.js")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert completed.returncode == 0, f"node 跑挂了：{completed.stderr}"
    payload = (completed.stdout or "").strip()
    assert payload, "node 没有任何输出"
    return json.loads(payload)


def _code_only(text: str) -> str:
    """剥掉注释再判"写了什么" —— 注释里提到某段文案不算实现了它。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _load_sources_block() -> str:
    code = _code_only(SOURCES_JS)
    start = code.index("async function loadSources(")
    return code[start : code.index("\nasync function", start + 10)]


def _function_block(name: str) -> str:
    code = _code_only(SOURCES_JS)
    start = code.index(f"function {name}(")
    return code[start : code.index("\n}", start) + 2]


def _rules() -> list[tuple[str, str]]:
    stripped = re.sub(r"/\*.*?\*/", "", STYLE_CSS, flags=re.S)
    return [
        (" ".join(sel.split()), " ".join(body.split()))
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", stripped)
        if sel.strip()
    ]


# ------------------------------------------------------------------ ① 行为（真源码）


def test_search_probe_cases_all_pass(probe_results: list[dict[str, object]]) -> None:
    failures = [f"{case['name']} —— {case['got']}" for case in probe_results if not case.get("ok")]
    assert not failures, "搜索结果用例失败：\n" + "\n".join(failures)


def test_probe_covers_the_risky_areas(probe_results: list[dict[str, object]]) -> None:
    """用例集别缩水：展开 / 汇总 / 空结果 / 手动折叠 / 无搜索词 必须都有覆盖。"""
    names = " ".join(str(case["name"]) for case in probe_results)
    for keyword in ("默认展开", "汇总行", "0 命中", "点组头", "无搜索词", "清空搜索"):
        assert keyword in names, f"用例集缺少对「{keyword}」的覆盖"
    assert len(probe_results) >= 8, f"用例太少（{len(probe_results)} 条），是不是被删了"


# ------------------------------------------------------------------ ② 接线 / 文案


def test_fold_default_depends_on_whether_we_are_searching() -> None:
    """折叠默认值必须由"有没有搜索词"决定，且搜索态仍以手动状态表为准。"""
    block = _function_block("sourcesIsFolded")
    assert "expandedByDefault === true" in block, (
        "sourcesIsFolded 没有按搜索态分档 —— 搜索时命中分组会继续默认折叠（就是 D4）"
    )
    assert "sourcesFoldState[key] === true" in block, (
        "搜索态没看手动折叠表（点组头会被写死成永远展开）"
    )
    assert "sourcesFoldState[key] !== false" in block, "无搜索词时的「默认折叠」判据被改掉了"

    load = _load_sources_block()
    assert "const searching = !!searchText;" in load, "画列表时没算出「这次是不是在搜」"
    assert "const folded = sourcesIsFolded(group.key, searching);" in load, (
        "画列表时没把搜索态交给折叠判定（命中分组还是会折着）"
    )
    assert "list.appendChild(sourceGroupHeader(group, folded, searching));" in load, (
        "组头没拿到搜索态（括号里报不出命中数）"
    )


def test_group_header_reports_hits_while_searching() -> None:
    """组头文案：搜索时 `RAG 综述（3）`；不搜时维持 `（N 条）`；两种态都要能点着翻。"""
    label = _function_block("sourceGroupLabel")
    assert 'searching ? "（" + group.items.length + "）"' in label, "搜索时组头没报本组命中数"
    assert 'group.items.length + " 条）"' in label, "无搜索词时的「（N 条）」被改掉了"
    assert 'const mark = folded ? "▸ " : "▾ ";' in label, "组头三角没跟着折叠态走"

    header = _function_block("sourceGroupHeader")
    assert "button.textContent = sourceGroupLabel(group, folded, searching);" in header
    assert "button.textContent = sourceGroupLabel(group, next, searching);" in header, (
        "点开/折起之后组头文案没重算（命中数会丢）"
    )
    assert "sourcesSetFolded(group.key, next);" in header, "点了组头没记状态（重画就丢）"


def test_summary_row_counts_hits_and_groups() -> None:
    """顶部汇总：`共 50 条，分布在 13 组`，且只在搜索时出现、画在分组之前。"""
    summary = _function_block("sourceSummaryRow")
    assert '"共 " + hits + " 条，分布在 " + groupCount + " 组"' in summary, "汇总行文案不对"

    load = _load_sources_block()
    assert (
        "if (searching) {\n    list.appendChild(sourceSummaryRow(items.length, groups.length));"
        in load
    ), "汇总行没有「只在搜索时」画，或没报命中总数"
    assert load.index("sourceSummaryRow(items.length, groups.length)") < load.index(
        "groups.forEach(function (group) {"
    ), "汇总行画在了分组之后（用户第一眼看到的还是折叠三角）"


def test_empty_result_says_what_happened_and_offers_a_way_out() -> None:
    """0 命中：`没找到包含「词」的资料` + 换词建议 + 可点的「清空搜索」；绝不返回空白列表。"""
    load = _load_sources_block()
    assert "list.appendChild(sourceEmptyRow(searchText));" in load, (
        "0 命中时没往列表里放任何东西 —— 这就是「搜完像没结果」的另一半"
    )
    empty = _function_block("sourceEmptyRow")
    assert '"没找到包含「" + searchText + "」的资料"' in empty, "缺「没找到包含「词」的资料」这句"
    assert "换个更短的词" in empty, "缺一句可操作建议"
    assert '"清空搜索"' in empty, "缺「清空搜索」按钮"
    assert 'const input = el("search");' in empty and 'loadSources("")' in empty, (
        "「清空搜索」没真清输入框 / 没重拉全部资料"
    )
    # 无搜索词 + 空库：保留原来那句，不冒充"没找到包含…"
    assert "库里还没有资料" in empty, "空库（没搜过）的提示被顺手改掉了"


# ------------------------------------------------------------------ ③ 样式


def test_new_rows_are_id_scoped_and_use_defined_tokens() -> None:
    """新样式必须带 `#source-list` 前缀（.list li 的基础规则是 id 选择器），令牌只许用已定义的。"""
    rules = [(sel, body) for sel, body in _rules() if any(name in sel for name in NEW_SELECTORS)]
    assert rules, "样式里找不到 .source-summary / .source-empty 的规则"

    offenders = [sel for sel, _ in rules if not sel.startswith("#source-list")]
    assert not offenders, f"这些新规则没带 #source-list 前缀，权重压不过基础规则：{offenders}"

    legacy = [
        (sel, token) for sel, body in rules for token in LEGACY_TOKENS if f"var({token})" in body
    ]
    assert not legacy, f"新样式用了未定义的旧令牌 {legacy} —— 改用 --ui-* 家族"

    undefined = set()
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", STYLE_CSS))
    for _, body in rules:
        for token in re.findall(r"var\((--[a-z0-9-]+)\)", body):
            if token not in defined:
                undefined.add(token)
    assert not undefined, f"新样式引用了 style.css 里根本没定义的令牌：{sorted(undefined)}"

    bodies = " ".join(body for _, body in rules)
    assert "display: block" in bodies, (
        "汇总行/空结果是 <li>，不给 display:block 会被 .list 的排版吃掉"
    )
