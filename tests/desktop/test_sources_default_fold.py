"""资料库列表「每次启动默认折叠」的守卫（行为 + 接线 + 复用既有折叠机制）。

背景（用户要求）：资料库那一栏的列表**每次启动都默认折叠**；同一会话内展开/收起的状态可以记住。

为什么不能只读代码文本：
  · 默认值写成"展开"（老行为）时，文本上照样有 `is-folded-row` 字样 —— 只有真跑代码
    （node 里求值两次 = 冷启动 / 重启）才分得清"默认折叠"和"默认展开"；
  · 折叠态若落进 localStorage，"记住"会变成"永远不折叠（重启也记着上次展开）" ——
    这一条也只有跑两遍才能发现。

分工：折叠**行为**（冷启动折叠、会话内记住、重启回到折叠、两个组互不干扰、行不遗漏）
由 `sources_fold_probe.js` 跑 `sources.js` 的真源码验证；这里断言结果 + 接线 + 样式复用。
真渲染（行到底有没有消失、组头 ▸ 有没有出现）由 `run/ui_shell_probe.py --scenes sources-fold`
在无头 Edge 里量。
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
PROBE_JS = Path(__file__).resolve().parent / "sources_fold_probe.js"


@pytest.fixture(scope="module")
def probe_results() -> list[dict[str, object]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("没装 node，跳过折叠行为那部分")
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


# ------------------------------------------------------------------ ① 行为


def test_every_probe_case_passes(probe_results: list[dict[str, object]]) -> None:
    failures = [f"{case['name']} —— {case['got']}" for case in probe_results if not case.get("ok")]
    assert not failures, "折叠用例失败：\n" + "\n".join(failures)


def test_probe_covers_the_risky_areas(probe_results: list[dict[str, object]]) -> None:
    """用例集别缩水：冷启动 / 重启 / 重画 / 分组独立 必须都有覆盖。"""
    names = " ".join(str(case["name"]) for case in probe_results)
    for keyword in ("冷启动", "重启", "重画", "互不干扰", "所有", "localStorage"):
        assert keyword in names, f"用例集缺少对「{keyword}」的覆盖"
    assert len(probe_results) >= 8, f"用例太少（{len(probe_results)} 条），是不是被删了"


# ------------------------------------------------------------------ ② 接线


def _code_only(text: str) -> str:
    """剥掉注释再判"用没用某个 API" —— 注释里提到 localStorage 不算用了它。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def test_unrecorded_group_counts_as_folded() -> None:
    """「默认折叠」的唯一判据必须是 `!== false`（没记录过 = 折叠）。

    写成 `=== true` 也"碰巧"对，但再包一层取反或改默认值就全反了 —— 这条钉住语义。
    """
    code = _code_only(SOURCES_JS)
    start = code.index("function sourcesIsFolded(")
    block = code[start : code.index("}", start) + 1]
    assert "!== false" in block, f"默认值不是折叠：{block!r}"
    # 只看这一块的实现（文件别处还有 splitter 高度之类的 localStorage，与本条无关）
    region = code[code.index("var sourcesFoldState") : code.index("function sourceGroupHeader(")]
    assert "localStorage" not in region, "折叠态进了 localStorage —— 重启就不是默认折叠了"
    assert "sessionStorage" not in region, "折叠态进了 sessionStorage（同上）"


def test_rows_are_born_in_the_same_state_as_their_header() -> None:
    """新画出来的行必须与组头**同态**（否则组头 ▸ 而条目全显示）。"""
    block = SOURCES_JS[SOURCES_JS.index("async function loadSources(") :]
    assert "const folded = sourcesIsFolded(group.key);" in block, (
        "画列表时没问折叠态（每个组都按默认画）"
    )
    assert "list.appendChild(sourceGroupHeader(group, folded));" in block, (
        "组头没拿到折叠态（会永远画成展开）"
    )
    assert re.search(
        r'li\.className = "source-row" \+ \(folded \? " is-folded-row" : ""\);', block
    ), "行没有按折叠态出生（组头折叠、行却全显示）"


def test_group_header_remembers_the_toggle_into_the_state_table() -> None:
    """点组头必须把选择记进状态表，并同时翻组头/行 —— 三处同源。"""
    start = SOURCES_JS.index("function sourceGroupHeader(")
    block = SOURCES_JS[start : SOURCES_JS.index("\n}", start) + 2]
    assert 'const next = !li.classList.contains("is-folded");' in block, "切换没从当前态推导"
    assert "sourcesSetFolded(group.key, next);" in block, "切换没记进状态表（重画列表就丢）"
    assert 'li.classList.toggle("is-folded", next);' in block, "组头态没翻"
    assert 'node.classList.toggle("is-folded-row", next);' in block, "行态没跟着翻"
    assert 'button.setAttribute("aria-expanded", next ? "false" : "true");' in block, (
        "aria-expanded 没跟着翻（读屏软件读不到折叠态）"
    )


def test_reuses_the_existing_fold_mechanism() -> None:
    """沿用仓库已有的折叠机制：`is-folded-row` + `#source-list` 带 id 的 display:none。

    不另造一套（比如 hidden 属性）—— `.source-row` 自带 display，`[hidden]` 会被压掉，
    那正是这条 CSS 规则存在的原因（见 test_sources_fold_style.py）。
    """
    assert "is-folded-row" in SOURCES_JS
    code = _code_only(SOURCES_JS).split("function sourceGroupHeader(")[1].split("\n}")[0]
    assert "hidden" not in code, (
        "折叠又改用 hidden 属性了（.source-row 的 display 会压掉它，点了没反应）"
    )
    rule = [
        body
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", STYLE_CSS)
        if "#source-list" in sel and "is-folded-row" in sel
    ]
    assert rule, "样式里没有 #source-list li.source-row.is-folded-row 规则"
    assert "display: none" in rule[0], f"折叠规则没有 display:none：{rule[0]!r}"
