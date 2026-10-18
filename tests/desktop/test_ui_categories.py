"""设置 / 个人中心「左侧表项 + 右侧内容」的样式与接线守卫。

为什么要有这个文件：这个交互出错的形态**全是门禁看不见的** ——

1. 类选择器上的 `display: flex` 会压掉 UA 给 `[hidden]` 的 `display:none`
   → 切走的那一项内容照样占着位置（本项目在 `.ui-cat-body` 上踩过同一条）；
2. 切表项时**重建**面板 → 用户正在填的输入框、展开的日历全被清空（看起来像"点一下就丢了"）；
3. 新增/改动设置组时忘了写进某个分类的 `sections` → 内容被静默丢进兜底，
   用户根本不知道还有这一项；
4. 分类壳每次打开都重建（老写法 `profile-body.innerHTML = ""`）→ index.html 里**静态**写着的
   画像 / 记忆区块被整块删掉 —— 打开一次个人中心，那两块就永远消失了；
5. `.ui-pane` 用 `display:flex` 而 `.ui-pane[hidden]` 忘了写 `display:none`。

分工（刻意的）：切换的**行为**（点表项换屏、默认第一项、键盘 ↑/↓/Home/End、切换不重建节点）
由 `rail_probe.js` 在 node 里跑 `profile.js` 的**真源码**验证，这里只把它的结果断言下来；
样式与接线（谁挂在哪一格、浮层加宽是否只作用于这两个浮层）由本文件钉住。
真渲染几何（点表项/开浮层时相邻栏位有没有动）由 `run/ui_shell_probe.py` 在无头 Edge 里量。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ._ui import APP_JS, INDEX_HTML, STYLE_CSS, UI_DIR

PROBE_JS = Path(__file__).resolve().parent / "rail_probe.js"

PROFILE_JS = (UI_DIR / "profile.js").read_text(encoding="utf-8")

#: 设置浮层的表项数（用户拍板沿用现有分类：5 组）。
SETTINGS_CATEGORY_COUNT = 5

#: 个人中心的表项（顺序即左侧顺序，第一项默认选中）。
PROFILE_CATEGORY_NAMES = ("portrait", "memory", "stats")

#: 这套设计的令牌是 --ui-* 家族；下面这些名字在 style.css 里**没有任何定义**，
#: 写进声明里等于把那条声明废掉（浏览器不报错，只在计算值阶段静默失效）。
LEGACY_TOKENS = ("--border", "--foreground", "--muted-foreground", "--accent", "--background")

#: 浮层加宽后只作用于这两个浮层；其它浮层仍是 .sheet 的 760px。
TWO_COLUMN_SHEETS = ("settings", "profile")


# ------------------------------------------------------------------ 通用取段


def _balanced(text: str, opener: str, open_ch: str = "{", close_ch: str = "}") -> str:
    """取 `opener` 起、括号配平的那一段（不靠找 `\\n];` 之类的外部锚）。

    用外部锚会让"把节点挪到别处"照样通过 —— 假绿比红危险。切不到就抛，绝不静默返回空。
    """
    assert opener in text, f"找不到锚点：{opener}"
    start = text.index(opener)
    i = text.index(open_ch, start)
    depth = 0
    while i < len(text):
        ch = text[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    raise AssertionError(f"{opener} 的括号没配平")


def _rules(css: str) -> list[tuple[str, str]]:
    """粗切「选择器 → 声明块」；先剥注释，否则注释里的花括号会把规则切歪。"""
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", stripped):
        sel = " ".join(selector.split())
        if not sel:
            continue
        out.append((sel, " ".join(body.split())))
    return out


def rail_rules() -> list[tuple[str, str]]:
    """这块两栏相关的规则（只查 ui-rail / ui-pane / ui-split，别的存量问题不算进来）。"""
    rules = [
        (sel, body)
        for sel, body in _rules(STYLE_CSS)
        if "ui-rail" in sel or "ui-pane" in sel or "ui-split" in sel
    ]
    assert len(rules) >= 8, f"只切到 {len(rules)} 条两栏规则，切片写歪了（会变成空判）"
    return rules


def body_of(selector: str) -> str:
    return " ".join(body for sel, body in rail_rules() if sel == selector)


def body_of_sheet() -> str:
    """`.sheet` 的基宽度（只取最后一条定义，层叠以最后出现的为准）。

    放在这里而不是贴着用它的用例：原来定义写在使用之后（Python 能跑，但读起来像是
    先用了一个未定义的名字，也挡不住"把定义搬走"这类改动被误当成没事）。
    """
    bodies = [body for sel, body in _rules(STYLE_CSS) if sel == ".sheet"]
    assert bodies, "样式里找不到 .sheet 的基规则"
    return bodies[-1]


def _profile_categories_block() -> str:
    return _balanced(PROFILE_JS, "function profileCategories(")


# ------------------------------------------------------------------ ① 行为：跑真源码


@pytest.fixture(scope="module")
def probe_results() -> list[dict[str, object]]:
    """在 node 里用最小假 DOM 跑 `uiRail` 的**真源码**（没装 node 就跳过）。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("没装 node，跳过表项交互那部分")
    assert PROBE_JS.is_file(), f"缺少用例集：{PROBE_JS}"
    completed = subprocess.run(
        [node, str(PROBE_JS), str(UI_DIR / "profile.js")],
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


def test_every_probe_case_passes(probe_results: list[dict[str, object]]) -> None:
    failures = [f"{case['name']} —— {case['got']}" for case in probe_results if not case.get("ok")]
    assert not failures, "表项交互用例失败：\n" + "\n".join(failures)


def test_probe_covers_the_risky_areas(probe_results: list[dict[str, object]]) -> None:
    """用例集本身别缩水：这几个面必须都有覆盖（少一条就等于少一道防线）。"""
    names = " ".join(str(case["name"]) for case in probe_results)
    for keyword in ("默认选中第一项", "点第 2 项", "不重建", "↓", "Home / End", "aria-selected"):
        assert keyword in names, f"用例集缺少对「{keyword}」的覆盖"
    assert len(probe_results) >= 12, f"用例太少（{len(probe_results)} 条），是不是被删了"


def test_selection_is_derived_from_one_source() -> None:
    """`uiRail` 的**真源码**里选中态三处必须同源：类 / aria-selected / 面板 hidden。

    只读文本证明不了"点下去真的换屏了"（那是 probe 的活儿），但能钉住**同源**：
    三者都从同一个 `on` 值派生，谁单独写死一个值，这里就红。
    """
    block = _balanced(PROFILE_JS, "function uiRail(")
    assert 'var on = button.getAttribute("data-cat") === name;' in block, (
        "表项选中态不是按 data-cat 与要选中的名字比对出来的（会出现两项同时高亮）"
    )
    assert 'button.classList.toggle("is-active", on);' in block
    assert 'button.setAttribute("aria-selected", on ? "true" : "false");' in block
    assert "panels[key].hidden = !on;" in block, "面板可见性没跟着同一个 on 走"
    assert "var on = key === name;" in block, "面板的 on 与表项的 on 不是同一判据"
    assert "select(specs[0].name)" in block, "没默认选中第一项（打开要先点一下才看到内容）"


def test_switching_only_toggles_visibility_never_rebuilds() -> None:
    """切换路径里不许出现重建：没有 innerHTML / textContent 清空 / createElement。"""
    block = _balanced(PROFILE_JS, "function select(name)")
    assert "panels[key].hidden" in block, "select 里没在切面板可见性（那切换就什么都不发生）"
    for banned in ("innerHTML", 'textContent = ""', "createElement"):
        assert banned not in block, f"切换表项时出现了 `{banned}` —— 会重建面板，丢掉用户填的内容"


# ------------------------------------------------------------------ ② 样式（权重 / 令牌 / 加宽）


def test_hidden_pane_is_forced_display_none() -> None:
    """`.ui-pane[hidden]` 必须显式 `display:none`（类选择器压得过 UA 的 `[hidden]`）。"""
    hidden = body_of(".ui-pane[hidden]")
    assert hidden, "缺 .ui-pane[hidden] 规则 —— hidden 加了没用，切走的那一屏照样占位置"
    assert "display: none" in hidden, f"[hidden] 没被强制成 display:none：{hidden!r}"
    pane = body_of(".ui-pane")
    assert "display: flex" in pane, f".ui-pane 的基样式变了（折叠规则的前提要重估）：{pane!r}"


def test_two_column_body_overrides_the_stacked_sheet_body() -> None:
    """两栏容器必须把 `.sheet-body` 的纵向 flex 改成横向（类 + 类的权重才压得过它）。"""
    split = body_of(".sheet-body.ui-split")
    assert split, "缺 .sheet-body.ui-split 规则 —— 两栏会继续纵向堆叠（表项在上、内容在下）"
    assert "flex-direction: row" in split, f"没改成横向：{split!r}"
    assert "overflow: hidden" in split, f"容器还在自己滚（表项会跟着内容滚走）：{split!r}"


def test_only_the_two_sheets_are_widened() -> None:
    """加宽只作用于设置 / 个人中心；其它浮层仍是 .sheet 的 760px（用户只允许这两个变宽）。

    宽度现在走令牌（`var(--ui-settings-width)`），不再写字面 920px；两个面板**合并成了一条
    选择器组**，所以逐条拆分检查 —— 仍然只许这两个面板变宽。
    """
    widened = [sel for sel, body in _rules(STYLE_CSS) if "var(--ui-settings-width)" in body]
    assert widened, "没找到任何用 --ui-settings-width 的加宽规则（用户要求的加宽没落地）"
    for sel in widened:
        for part in sel.split(","):
            assert any(f"#{name} .sheet" in part for name in TWO_COLUMN_SHEETS), (
                f"加宽规则 {sel!r} 的 `{part.strip()}` 不在 #settings / #profile 之下"
                " —— 会把练习、导出等浮层一起改宽"
            )
    base = body_of_sheet()
    assert "width: min(760px, 92vw)" in base, f"`.sheet` 的基础宽度被改动了：{base!r}"


def test_rail_item_active_state_reuses_existing_row_tokens() -> None:
    """选中态复用行态令牌（与 .tab.is-active 同一套），不新造令牌、不写死颜色。"""
    active = body_of(".ui-rail-item.is-active")
    assert active, "缺 .ui-rail-item.is-active 规则 —— 选中态看不见"
    assert "var(--ui-row-active-background)" in active, f"选中态没用既有行态令牌：{active!r}"
    assert "var(--ui-stroke-primary)" in active, f"选中态没有描边（只靠底色看不出选中）：{active!r}"


def test_rail_item_is_focusable_and_focused_state_is_visible() -> None:
    """键盘可达要有可见反馈：`:focus-visible` 必须给轮廓，否则 Tab 到了也看不出来。"""
    focus = body_of(".ui-rail-item:focus-visible")
    assert focus, "缺 .ui-rail-item:focus-visible 规则（Tab 到表项看不出焦点在哪）"
    assert "outline:" in focus, f"焦点态没有 outline：{focus!r}"


def test_rail_rules_use_only_defined_tokens() -> None:
    """两栏样式的每个 `var(--x)` 都必须在 style.css 里真有定义（否则整条声明静默失效）。"""
    offenders: list[str] = []
    used: set[str] = set()
    for sel, body in rail_rules():
        for name in re.findall(r"var\((--[a-z0-9-]+)\)", body):
            used.add(name)
            if f"{name}:" not in STYLE_CSS:
                offenders.append(f"{sel} → {name}")
            if name in LEGACY_TOKENS:
                offenders.append(f"{sel} → {name}（旧的、本设计没有的令牌名）")
    assert not offenders, "两栏样式用了本设计未定义的令牌（声明会静默失效）：" + "、".join(
        offenders
    )
    assert len(used) >= 5, f"只扫到 {len(used)} 个令牌，切片写歪了（会变成空判）：{sorted(used)}"


def test_rail_rules_declare_no_new_tokens() -> None:
    """两栏**没有新增任何设计令牌**（0 个）：颜色 / 间距一律复用现有 --ui-* 家族。"""
    declared = [
        f"{sel} → {name}"
        for sel, body in rail_rules()
        for name in re.findall(r"(--[a-z0-9-]+)\s*:", body)
    ]
    assert not declared, "两栏样式里定义了新令牌（本轮约定是 0 个）：" + "、".join(declared)


# ------------------------------------------------------------------ ③ 设置浮层的接线


def _spec_titles() -> list[str]:
    """`SETTINGS_SPEC` 里每个**组**的标题（字段是 `label`，不会混进来）。"""
    block = _balanced(APP_JS, "var SETTINGS_SPEC = [", "[", "]")
    return re.findall(r'title:\s*"([^"]+)"', block)


CATEGORY_RE = re.compile(
    r'name:\s*"(?P<name>[a-z_]+)".*?title:\s*"(?P<title>[^"]+)".*?'
    r"sections:\s*\[(?P<sections>[^\]]*)\]",
    re.S,
)

NAME_TITLE_RE = re.compile(r'name:\s*"(?P<name>[a-z_]+)".*?title:\s*"(?P<title>[^"]+)"', re.S)


def _categories() -> list[tuple[str, str, list[str]]]:
    block = _balanced(APP_JS, "var SETTINGS_CATEGORIES = [", "[", "]")
    out = [
        (m.group("name"), m.group("title"), re.findall(r'"([^"]+)"', m.group("sections")))
        for m in CATEGORY_RE.finditer(block)
    ]
    assert len(out) == SETTINGS_CATEGORY_COUNT, (
        f"切到 {len(out)} 个分类（期望 {SETTINGS_CATEGORY_COUNT}），解析写歪了或分类被改过：{out}"
    )
    return out


def test_settings_rail_keeps_the_five_agreed_items() -> None:
    """左侧表项沿用用户拍板的 5 项（名字与顺序都不许漂）。"""
    assert [(name, title) for name, title, _ in _categories()] == [
        ("model", "模型与生成"),
        ("channel", "渠道"),
        ("search", "检索与网络"),
        ("data", "数据与存储"),
        ("appearance", "外观与关于"),
    ]


def test_every_settings_group_is_classified() -> None:
    """**每个设置组都必须被某个分类认领** —— 漏一个就被静默塞进兜底，用户找不到。

    这是最容易出的错：加了字段/组，忘了写进 `sections`。所以要求
    `sections` 的并集**恰好等于** SETTINGS_SPEC 的组标题集合（多写一个也要红）。
    """
    titles = _spec_titles()
    claimed = [t for _, _, sections in _categories() for t in sections]
    assert len(titles) >= 4, f"只切到 {len(titles)} 个设置组，解析写歪了"
    assert sorted(claimed) == sorted(titles), (
        f"分类认领的组 {sorted(claimed)} 与 SETTINGS_SPEC 的组 {sorted(titles)} 不一致 —— "
        "多写的会变成空面板，漏写的会被静默丢进兜底"
    )
    assert len(set(claimed)) == len(claimed), f"同一个组被两个分类认领了：{claimed}"


def test_settings_uses_the_rail_and_wipes_before_building() -> None:
    """设置每次打开重画：先清空 `settings-body`，再一次性搭出左表项 + 右面板。

    顺序反了（先搭壳后清空）就会出现"打开设置什么也没有"。
    """
    block = _balanced(PROFILE_JS, "async function openSettings(")
    assert 'var body = el("settings-body");' in block
    assert 'body.textContent = "";' in block
    assert 'body.classList.add("ui-split");' in block, "两栏容器类没加上（会继续纵向堆叠）"
    assert "uiRail(SETTINGS_CATEGORIES)" in block, "没走 uiRail（左侧表项不存在）"
    assert "body.appendChild(shell.nav);" in block and "body.appendChild(shell.panes);" in block
    assert block.index('body.textContent = "";') < block.index("uiRail("), (
        "清空 settings-body 跑在搭壳之后了 —— 会把刚搭好的表项一起清掉"
    )
    host = "hostOf[group.title] || panels.appearance"
    assert host in block, "组没有走「按标题归位 + 兜底外观与关于」的路径"
    assert block.index(host) < block.index(".appendChild(box);", block.index(host)), (
        "内容没被 append 进右侧面板（挂到别处就不受表项切换管了）"
    )
    assert "shell.select(SETTINGS_CATEGORIES[0].name);" in block, "没把表项复位到第一项"


# ------------------------------------------------------------------ ④ 个人中心的接线


def _profile_categories() -> list[tuple[str, str]]:
    block = _balanced(APP_JS, "var PROFILE_CATEGORIES = [", "[", "]")
    out = [(m.group("name"), m.group("title")) for m in NAME_TITLE_RE.finditer(block)]
    assert out, "解析不到个人中心的分类（切片写歪了会变成空判）"
    return out


def test_index_html_still_holds_the_static_profile_blocks() -> None:
    """静态区块必须还在 html 里（两栏壳由 JS 建，别写进 html，否则与"搬节点"打架）。"""
    for marker in ('id="profile-body"', 'id="profile-overview"', 'id="memory-input"'):
        assert marker in INDEX_HTML, f"index.html 里少了 {marker}"
    assert 'class="ui-rail' not in INDEX_HTML, "左表项被写进 html 了（会与 appendChild 搬节点冲突）"
    assert 'class="ui-pane' not in INDEX_HTML, "右面板被写进 html 了（切表项只切 JS 建的那批）"


def test_profile_rail_keeps_the_three_agreed_items() -> None:
    assert _profile_categories() == [
        ("portrait", "学习者画像"),
        ("memory", "跨会话记忆"),
        ("stats", "学习统计"),
    ]


def test_profile_categories_wrap_the_static_blocks_instead_of_deleting_them() -> None:
    """三项：画像 / 记忆把 index.html 里的**静态**区块搬进自己的面板，统计现算。"""
    block = _profile_categories_block()
    assert "uiRail(PROFILE_CATEGORIES)" in block, "个人中心没走 uiRail"
    assert 'body.classList.add("ui-split");' in block, "两栏容器类没加上"
    assert 'el("profile-overview")' in block and 'el("memory-input")' in block, (
        "没有去取那两个静态区块（那它们就还在浮层根部，等于没分类）"
    )
    assert '.closest(".board-block")' in block, (
        "没按外层区块整块搬（只搬内层会把标题/说明留在原地）"
    )
    for move in (
        "shell.panels.portrait.appendChild(portraitBlock);",
        "shell.panels.memory.appendChild(memoryBlock);",
        "body.insertBefore(shell.panes, body.firstChild);",
        "body.insertBefore(shell.nav, shell.panes);",
    ):
        assert move in block, f"缺搬迁/挂载语句：{move}"


def test_profile_shell_is_built_once_and_never_wipes_the_body() -> None:
    """**只搭一次壳**：已存在就直接复用；且这里不许出现 `innerHTML`/清空整块。

    老写法 `profile-body.innerHTML = ""` 会把静态的画像 / 记忆区块删掉 ——
    打开一次个人中心，那两块就永远消失了（真 bug，不是理论风险）。
    """
    block = _profile_categories_block()
    assert "var stats = body.querySelector(\".ui-pane[data-cat='stats']\");" in block, (
        "找复用面板的选择器改了 —— 找不到就会每次重搭壳"
    )
    early = "if (stats) return { stats: stats };"
    assert early in block, "没有「已搭过就复用」的早退 —— 每次刷新都会重搭一遍壳"
    assert block.index(early) < block.index("uiRail("), "早退排在搭壳之后，等于没早退"
    assert "innerHTML" not in block, "又在清空整块（静态的画像 / 记忆区块会被删掉）"


def test_render_profile_only_repaints_the_stats_cell() -> None:
    """刷新时只重画「学习统计」那一格，不碰另外两项（它们的内容是静态节点）。"""
    block = _balanced(PROFILE_JS, "async function renderProfile(")
    assert "var cats = profileCategories();" in block
    assert 'cats.stats.textContent = "";' in block, "统计那格没清（会重复累加）"
    assert "profile-body" not in block, "renderProfile 又直接动了整块 profile-body"
    for part in ("profileRoutes", "profileNotes", "profileCards", "profileBoard"):
        assert part in block, f"统计那格少了 {part}"
        assert f"cats.stats.appendChild(await {part}());" in block, f"{part} 没装进统计那格"
