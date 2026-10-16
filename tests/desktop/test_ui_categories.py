"""设置 / 个人中心「可折叠分类」的样式与接线守卫。

为什么要有这个文件：分类是「点了才展开」的交互，出错的形态**全是门禁看不见的** ——

1. 类选择器上的 `display: flex` 会压掉 UA 给 `[hidden]` 的 `display:none`
   → 收起的分组照样显示（"点了收起但没收起来"，本项目在同类问题上踩过）；
2. 新增/改动设置组时忘了写进某个分类的 `sections` → 内容被静默丢进兜底，
   用户根本不知道还有这一项；
3. 分类壳每次打开都重建（老写法 `profile-body.innerHTML = ""`）→ index.html 里**静态**写着的
   画像 / 记忆区块被整块删掉 —— 打开一次个人中心，那两块就永远消失了。

分工（刻意的）：分组头的**行为**（点击翻类、同步 `aria-expanded`、解开 `panel.hidden`、
内容必须装进 panel）由 `categories_probe.js` 在 node 里跑 `profile.js` 的**真源码**验证，
这里只把它的结果断言下来；样式与接线（谁挂在哪一格、默认展开哪一组）由本文件钉住。
真渲染几何（分类点下去相邻栏位有没有动）由 `run/ui_shell_probe.py` 在无头 Edge 里量。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ._ui import APP_JS, INDEX_HTML, STYLE_CSS, UI_DIR

PROBE_JS = Path(__file__).resolve().parent / "categories_probe.js"

PROFILE_JS = (UI_DIR / "profile.js").read_text(encoding="utf-8")

#: 本轮设置浮层的分类数（用户反馈"一个大页面太臃肿"，拆成 5 组）。
SETTINGS_CATEGORY_COUNT = 5

#: 个人中心的分类名（前两组是 index.html 里的**静态**区块，第三组装现算的统计）。
PROFILE_CATEGORY_NAMES = ("portrait", "memory", "stats")

#: 这套设计的令牌是 --ui-* 家族；下面这些名字在 style.css 里**没有任何定义**，
#: 写进声明里等于把那条声明废掉（浏览器不报错，只在计算值阶段静默失效）。
LEGACY_TOKENS = ("--border", "--foreground", "--muted-foreground", "--accent", "--background")


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


def category_rules() -> list[tuple[str, str]]:
    """这块分类相关的规则（只查 `ui-cat` 那几条，别的存量问题不算进来）。"""
    rules = [(sel, body) for sel, body in _rules(STYLE_CSS) if "ui-cat" in sel]
    assert len(rules) >= 7, f"只切到 {len(rules)} 条 ui-cat 规则，切片写歪了（会变成空判）"
    return rules


def body_of(selector: str) -> str:
    return " ".join(body for sel, body in category_rules() if sel == selector)


def _profile_categories_block() -> str:
    return _balanced(PROFILE_JS, "function profileCategories(")


# ------------------------------------------------------------------ ① 行为：跑真源码


@pytest.fixture(scope="module")
def probe_results() -> list[dict[str, object]]:
    """在 node 里用最小假 DOM 跑 `uiCategory` 的**真源码**（没装 node 就跳过）。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("没装 node，跳过分组交互那部分")
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
    assert not failures, "分组交互用例失败：\n" + "\n".join(failures)


def test_probe_covers_the_risky_areas(probe_results: list[dict[str, object]]) -> None:
    """用例集本身别缩水：这几个面必须都有覆盖（少一条就等于少一道防线）。"""
    names = " ".join(str(case["name"]) for case in probe_results)
    for keyword in ("aria-expanded", "is-open", "hidden", "panel", "点"):
        assert keyword in names, f"用例集缺少对「{keyword}」的覆盖"
    assert len(probe_results) >= 10, f"用例太少（{len(probe_results)} 条），是不是被删了"


def test_head_dimensions_are_synced_by_the_real_source() -> None:
    """`uiCategory` 的**真源码**里三处状态必须同源：类 / aria-expanded / panel.hidden。

    只读文本证明不了"点下去真的同步了"（那是 probe 的活儿），但能钉住**同源**：
    三者都从同一个 `next` 值派生，谁单独写死一个值，这里就红。
    """
    block = _balanced(PROFILE_JS, "function uiCategory(")
    assert 'box.setAttribute("data-cat", name);' in block, "分类名没写在 data-cat 上（探针按它读）"
    assert 'var next = !box.classList.contains("is-open");' in block
    assert 'head.setAttribute("aria-expanded", next ? "true" : "false");' in block
    assert "panel.hidden = !next;" in block
    assert "panel.hidden = !open;" in block, "初始展开态没跟着 open 走（默认展开哪一组会失灵）"


# ------------------------------------------------------------------ ② 样式


def test_collapsed_body_is_forced_display_none() -> None:
    """`.ui-cat-body[hidden]` 必须显式 `display:none`（类选择器压得过 UA 的 `[hidden]`）。"""
    hidden = body_of(".ui-cat-body[hidden]")
    assert hidden, "缺 .ui-cat-body[hidden] 规则 —— hidden 加了没用，收起的分组照样显示"
    assert "display: none" in hidden, f"[hidden] 没被强制成 display:none：{hidden!r}"


def test_category_rules_use_only_defined_tokens() -> None:
    """分类样式的每个 `var(--x)` 都必须在 style.css 里真有定义（否则整条声明静默失效）。"""
    offenders: list[str] = []
    used: set[str] = set()
    for sel, body in category_rules():
        for name in re.findall(r"var\((--[a-z0-9-]+)\)", body):
            used.add(name)
            if f"{name}:" not in STYLE_CSS:
                offenders.append(f"{sel} → {name}")
            if name in LEGACY_TOKENS:
                offenders.append(f"{sel} → {name}（旧的、本设计没有的令牌名）")
    assert not offenders, "分类样式用了本设计未定义的令牌（声明会静默失效）：" + "、".join(
        offenders
    )
    assert len(used) >= 4, f"只扫到 {len(used)} 个令牌，切片写歪了（会变成空判）：{sorted(used)}"


def test_category_rules_declare_no_new_tokens() -> None:
    """分类**没有新增任何设计令牌**（0 个）：颜色 / 间距一律复用现有 --ui-* 家族。"""
    declared = [
        f"{sel} → {name}"
        for sel, body in category_rules()
        for name in re.findall(r"(--[a-z0-9-]+)\s*:", body)
    ]
    assert not declared, "分类样式里定义了新令牌（本轮约定是 0 个）：" + "、".join(declared)


def test_caret_flips_between_collapsed_and_open() -> None:
    """箭头是收起 ▸ / 展开 ▾ 两态 —— 只写一个字形的话，两组看起来一模一样。"""
    closed = body_of(".ui-cat-caret::before")
    opened = body_of(".ui-cat.is-open .ui-cat-caret::before")
    assert closed and opened, "缺箭头规则（收起态或展开态）"
    assert 'content: "\u25b8"' in closed, f"收起态箭头不对：{closed!r}"
    assert 'content: "\u25be"' in opened, f"展开态箭头不对：{opened!r}"


def test_first_category_drops_its_top_border() -> None:
    """第一组不加顶部分隔线（浮层顶部已经有边，再画一条会显得"多出一条线"）。"""
    first = body_of(".ui-cat:first-child")
    assert first, "缺 .ui-cat:first-child 规则"
    assert "border-top: none" in first, f"第一组会多出一条顶线：{first!r}"


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


def test_every_settings_group_is_classified() -> None:
    """**每个设置组都必须被某个分类认领** —— 漏一个就被静默塞进兜底，用户找不到。

    这是本轮最容易出的错：加了字段/组，忘了写进 `sections`。所以要求
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


def test_model_and_search_groups_land_in_the_expected_categories() -> None:
    """模型 / 生成策略 → 「模型与生成」；搜索引擎 / 检索与预算 → 「检索与网络」。"""
    by_name = {name: sections for name, _, sections in _categories()}
    assert by_name["model"] == ["模型", "生成策略"]
    assert by_name["search"] == ["搜索引擎", "检索与预算"]


def test_only_the_first_category_starts_open() -> None:
    """默认只展开第一组（打开浮层先看到一组，其余点标题展开）。"""
    assert "uiCategory(spec.name, spec.title, spec.hint, index === 0)" in PROFILE_JS, (
        "分类没按 index === 0 决定默认展开组（会全展开或全收起）"
    )
    block = _balanced(PROFILE_JS, "async function openSettings(")
    host = "hostOf[group.title] || panels.appearance"
    assert host in block, "组没有走「按标题归位 + 兜底外观与关于」的路径"
    assert block.index(host) < block.index(".appendChild(box);", block.index(host)), (
        "内容没被 append 进分类面板（挂到别处就不受分类开关管了）"
    )


def test_settings_body_is_wiped_but_categories_are_rebuilt_together() -> None:
    """设置是每次打开重画：先清空 `settings-body`，再一次性搭出全部分类壳。

    顺序反了（先搭壳后清空）就会出现"打开设置什么也没有"。
    """
    block = _balanced(PROFILE_JS, "async function openSettings(")
    assert 'var body = el("settings-body");' in block
    assert 'body.textContent = "";' in block
    assert block.index('body.textContent = "";') < block.index("uiCategory("), (
        "清空 settings-body 跑在搭壳之后了 —— 会把刚搭好的分类一起清掉"
    )
    assert "SETTINGS_CATEGORIES.forEach" in block


# ------------------------------------------------------------------ ④ 个人中心的接线


def test_index_html_still_holds_the_static_profile_blocks() -> None:
    """静态区块必须还在 html 里（分类壳由 JS 建，别写进 html，否则与"搬节点"打架）。"""
    for marker in ('id="profile-body"', 'id="profile-overview"', 'id="memory-input"'):
        assert marker in INDEX_HTML, f"index.html 里少了 {marker}"
    assert 'class="ui-cat' not in INDEX_HTML, "分类壳被写进 html 了（会与 appendChild 搬节点冲突）"


def test_profile_categories_wrap_the_static_blocks_instead_of_deleting_them() -> None:
    """三组分类：画像 / 记忆把 index.html 里的**静态**区块搬进自己的 panel，统计现算。"""
    block = _profile_categories_block()
    calls = [
        (m.group("name"), m.group("open") == "true")
        for m in re.finditer(
            r'uiCategory\(\s*"(?P<name>[a-z_]+)"\s*,\s*"[^"]*"\s*,\s*"[^"]*"\s*,\s*'
            r"(?P<open>true|false)\s*\)",
            block,
        )
    ]
    assert [name for name, _ in calls] == list(PROFILE_CATEGORY_NAMES), (
        f"个人中心的分类名变了：{calls}"
    )
    assert [open_ for _, open_ in calls] == [True, False, False], (
        f"默认展开的组不是「只第一组」：{calls}"
    )
    assert 'el("profile-overview")' in block and 'el("memory-input")' in block, (
        "没有去取那两个静态区块（那它们就还在浮层根部，等于没分类）"
    )
    assert '.closest(".board-block")' in block, (
        "没按外层区块整块搬（只搬内层会把标题/说明留在原地）"
    )
    for move in (
        "portrait.panel.appendChild(portraitBlock);",
        "memory.panel.appendChild(memoryBlock);",
        "body.appendChild(portrait.box);",
        "body.appendChild(memory.box);",
        "body.appendChild(statCat.box);",
    ):
        assert move in block, f"缺搬迁语句：{move}"


def test_profile_shell_is_built_once_and_never_wipes_the_body() -> None:
    """**只搭一次壳**：已存在就直接复用；且这里不许出现 `innerHTML`/清空整块。

    老写法 `profile-body.innerHTML = ""` 会把静态的画像 / 记忆区块删掉 ——
    打开一次个人中心，那两块就永远消失了（真 bug，不是理论风险）。
    """
    block = _profile_categories_block()
    assert "var stats = body.querySelector(\".ui-cat[data-cat='stats'] .ui-cat-body\");" in block
    early = "if (stats) return { stats: stats };"
    assert early in block, "没有「已搭过就复用」的早退 —— 每次刷新都会重搭一遍壳"
    assert block.index(early) < block.index("uiCategory("), "早退排在搭壳之后，等于没早退"
    assert "innerHTML" not in block, "又在清空整块（静态的画像 / 记忆区块会被删掉）"


def test_render_profile_only_repaints_the_stats_cell() -> None:
    """刷新时只重画「学习统计」那一格，不碰另外两组（它们的内容是静态节点）。"""
    block = _balanced(PROFILE_JS, "async function renderProfile(")
    assert "var cats = profileCategories();" in block
    assert 'cats.stats.textContent = "";' in block, "统计那格没清（会重复累加）"
    assert "profile-body" not in block, "renderProfile 又直接动了整块 profile-body"
    for part in ("profileRoutes", "profileNotes", "profileCards", "profileBoard"):
        assert part in block, f"统计那格少了 {part}"
        assert f"cats.stats.appendChild(await {part}());" in block, f"{part} 没装进统计那格"
