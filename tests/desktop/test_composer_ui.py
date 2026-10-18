"""输入区（发送按钮收进输入框 + 当前模型 / 深度思考）的样式与接线守卫。

为什么要有这个文件：这一轮改的是**输入框内部的定位**，全是「看不见就错了」的东西 ——
`.composer-actions` 一旦不是绝对定位，它就又占回输入框下面一行（相邻栏位几何会动，只是
门禁看不出来）；文本域没留出底部让位带，发送按钮就压在正文上；样式里写了一个这套设计
从未定义的令牌，整条声明在**计算值阶段静默失效**（本项目在 `var(--border)` 上踩过）。

分工（刻意的）：这里钉的是**代码形状**（HTML 嵌套 / 样式声明 / 接线路径）；
真渲染（按钮盒子是否真在输入框盒子里、分组点下去是否真展开、相邻栏位几何）由
`run/ui_shell_probe.py` 在无头 Edge 里量。
"""

from __future__ import annotations

import re

from ._ui import APP_JS, INDEX_HTML, STYLE_CSS

#: 本轮新增那一块样式的 banner。切片起点要退到 banner 注释的 `/*` **开头**
#: （从注释文字里切的话注释剥不干净，整段 banner 会被当成一个选择器 —— 本项目踩过）。
NEW_CSS_BANNER = "④ 输入区内的发送按钮"

#: 这套设计的令牌是 --ui-* 家族（--ui-stroke-* / --ui-text-* / --font-* …）。
#: 下面这些名字在 style.css 里**没有任何定义**，写进声明里等于把该声明废掉。
LEGACY_TOKENS = ("--border", "--foreground", "--muted-foreground", "--accent", "--background")

#: 文本域底部让位带（与 run/ui_shell_probe.py 的 TEXTAREA_PADDING_BOTTOM 同一个数）
TEXTAREA_PADDING_BOTTOM = "34px"


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


def new_rules() -> list[tuple[str, str]]:
    """本轮新增那块样式的规则（按 banner 切分，切不到就红，绝不静默测空）。"""
    assert NEW_CSS_BANNER in STYLE_CSS, "style.css 里找不到本轮新增的样式 banner"
    marker = STYLE_CSS.index(NEW_CSS_BANNER)
    rules = _rules(STYLE_CSS[STYLE_CSS.rindex("/*", 0, marker) :])
    assert len(rules) >= 12, f"切出来的新样式块只有 {len(rules)} 条规则，切片写歪了（会变成空判）"
    return rules


def body_of(selector: str) -> str:
    """某个选择器的全部声明块拼起来（同一选择器可能出现在常规 + 媒体查询里）。"""
    return " ".join(body for sel, body in new_rules() if sel == selector)


def _element_body(html: str, marker: str) -> str:
    """取 marker 那个 `<div …>` 的**自身内容**（按 div 配平，不靠找 `</form>` 之类的外部锚）。

    用外部锚会让"把节点挪到盒子外面"这种改动**照样通过** —— 假绿比红危险。
    """
    start = html.index(marker)
    i = html.index(">", start) + 1
    depth = 1
    while depth:
        nxt_open = html.find("<div", i)
        nxt_close = html.find("</div>", i)
        assert nxt_close >= 0, f"{marker} 的 div 没闭合"
        if 0 <= nxt_open < nxt_close:
            depth += 1
            i = nxt_open + 4
        else:
            depth -= 1
            i = nxt_close + 6
    return html[html.index(">", start) + 1 : nxt_close]


# ------------------------------------------------------------------ HTML：结构性包含


def test_send_button_is_inside_the_input_box() -> None:
    """#input / 模型与思考 / #btn-send / #btn-stop 必须都在 `.composer-input` 这个盒子**里面**。

    这是用户这一轮点名要的效果（Hermes 风：按钮在输入框内部，不是下面单独一行）。
    所以断言按盒子配平取内容，挪出去一格就红。
    """
    box = _element_body(INDEX_HTML, '<div class="composer-input">')
    for marker in (
        'class="composer-meta"',
        'id="composer-model"',
        'id="composer-thinking"',
        'class="composer-actions"',
        'id="btn-send"',
        'id="btn-stop"',
    ):
        assert marker in box, f"输入框内部少了 {marker}（发送按钮/状态行又跑出框外了）"
    assert box.index('id="input"') < box.index('class="composer-actions"'), (
        "文本域要在前：textarea 先定高度，按钮才谈得上「落在框底」"
    )


def test_composer_actions_still_only_holds_buttons() -> None:
    """发送那一行**只有按钮**：装饰性 span 不许回到 .composer-actions 里。"""
    box = _element_body(INDEX_HTML, '<div class="composer-input">')
    actions = _element_body(box, '<div class="composer-actions">')
    assert "<span" not in actions, f"发送那一行又出现装饰性 span：{actions.strip()[:120]}"
    assert 'id="btn-send"' in actions and 'id="btn-stop"' in actions


def test_usage_and_hint_did_not_come_back() -> None:
    """顺手守住上一轮的清理：用量留在标题右侧，输入区里不再有 #hint / #session-usage。"""
    box = _element_body(INDEX_HTML, '<div class="composer-input">')
    assert 'id="hint"' not in box
    assert 'id="session-usage"' not in box


# ------------------------------------------------------------------ CSS：定位与让位


def test_the_input_wrapper_is_the_positioning_context() -> None:
    """定位上下文挂在 `.composer-input` 上（不是 `.composer`：浮层加载圈退休时的约定）。"""
    body = body_of(".composer-input")
    assert "position: relative" in body, f".composer-input 没有成为定位上下文：{body!r}"


def test_buttons_and_status_line_are_positioned_inside_the_box() -> None:
    """按钮与状态行都得是绝对定位 —— 只要一个回到文档流，输入区就又长出一行。"""
    for selector in (".composer-actions", ".composer-meta"):
        body = body_of(selector)
        assert body, f"新样式块里找不到 {selector} 规则"
        assert "position: absolute" in body, f"{selector} 不是绝对定位：{body!r}"


def test_textarea_reserves_room_for_the_overlay_controls() -> None:
    """文本域底部留出让位带，否则最后一行正文会被按钮压住。"""
    body = body_of(".composer-input textarea")
    assert body, "找不到 .composer-input textarea 规则（文本域没被框内控件接管）"
    assert f"padding-bottom: {TEXTAREA_PADDING_BOTTOM}" in body, f"没有让位带：{body!r}"


def test_status_line_does_not_swallow_clicks() -> None:
    """状态行不吃点击：点它也应该落到文本域上（否则"点空白处光标没反应"）。"""
    body = body_of(".composer-meta")
    assert "pointer-events: none" in body, f"状态行会挡住点击：{body!r}"


def test_new_style_block_uses_only_defined_tokens() -> None:
    """**本轮新增的每一条声明**只许用这套设计真实存在的令牌。

    比"黑名单"更强：把所有 `var(--x)` 抽出来，逐个要求在 style.css 里有 `--x:` 的定义。
    写了不存在的令牌名时，浏览器**不报错**，整条声明在计算值阶段静默失效 —— 门禁与
    单测都看不见，只有真机看图才发现（本项目在 `var(--border)` 上踩过）。
    """
    offenders: list[str] = []
    used: set[str] = set()
    for sel, body in new_rules():
        for name in re.findall(r"var\((--[a-z0-9-]+)\)", body):
            used.add(name)
            if f"{name}:" not in STYLE_CSS:
                offenders.append(f"{sel} → {name}")
            for legacy in LEGACY_TOKENS:
                if name == legacy:
                    offenders.append(f"{sel} → {name}（旧的、本设计没有的令牌名）")
    assert not offenders, "新增样式用了本设计未定义的令牌（声明会静默失效）：" + "、".join(
        offenders
    )
    assert len(used) >= 6, f"只扫到 {len(used)} 个令牌，切片写歪了（会变成空判）：{sorted(used)}"


def test_new_style_block_declares_no_new_tokens() -> None:
    """本轮**没有新增任何设计令牌**（0 个）：颜色/间距一律复用现有 --ui-* 家族。"""
    declared = []
    for sel, body in new_rules():
        for name in re.findall(r"(--[a-z0-9-]+)\s*:", body):
            declared.append(f"{sel} → {name}")
    assert not declared, "新增样式块里定义了新令牌（本轮约定是 0 个）：" + "、".join(declared)


# ------------------------------------------------------------------ JS：接线


def _composer_meta_block() -> str:
    """`refreshComposerMeta()` 那一段（从函数头到紧接着的 renderHealth）。"""
    assert "async function refreshComposerMeta(" in APP_JS, (
        "找不到 refreshComposerMeta（模型/思考的读取口）"
    )
    head = APP_JS.index("async function refreshComposerMeta(")
    tail = APP_JS.index("function renderHealth(", head)
    return APP_JS[head:tail]


def test_meta_shows_the_settings_values_not_a_hardcoded_name() -> None:
    """模型名与思考开关都取自 `settings.get` 的 values —— 界面上不许另写一份死值。"""
    block = _composer_meta_block()
    assert 'scout.call("settings.get"' in block, "没读 settings.get（显示的就会是另写的一份）"
    assert 'values["llm.chat.model"]' in block, "没取 llm.chat.model"
    assert 'values["llm.chat.thinking"]' in block, "没取 llm.chat.thinking"
    assert "deepseek" not in block.lower(), "把模型名写死了（应该跟着设置走）"


def test_meta_does_not_invent_a_reasoning_gear() -> None:
    """**本项目没有推理强度档位** —— 不许为了好看造一个。

    真实的那个开关是 `llm.chat.thinking`（布尔）；`reasoning_effort` 实测这个模型不认。
    """
    block = _composer_meta_block()
    assert "reasoning_effort" not in block, "又引用了这个模型不认的推理强度参数"
    assert "推理强度：" not in block, "界面上写了一个不存在的档位读数"
    assert "深度思考：" in block, "思考开关的文案没了"


def test_meta_is_refreshed_after_saving_settings() -> None:
    """改完模型保存后要立刻刷新输入框里的显示（否则显示的是旧值）。"""
    calls = APP_JS.count("await refreshComposerMeta();")
    assert calls >= 2, f"refreshComposerMeta 只在 {calls} 处被调用（保存后没刷新）"


def test_meta_elements_exist_in_html() -> None:
    for marker in ('id="composer-model"', 'id="composer-thinking"'):
        assert marker in INDEX_HTML, f"界面里少了 {marker}"
