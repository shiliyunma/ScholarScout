"""输入区（模型 / 思考深度 / 发送同一行靠右）的样式与接线守卫。

为什么要有这个文件：这一轮改的是**输入框内部的定位与联动**，全是「看不见就错了」的东西 ——
`.composer-actions` 一旦不是绝对定位，它就又占回输入框下面一行（相邻栏位几何会动，只是
门禁看不出来）；文本域没留出底部让位带，控件就压在正文上；两个下拉里的档位文案要是前端
另拼一份，必然与 `/models` 的声明漂移（用户选了服务端不认的档，下一轮 400）。

分工（刻意的）：这里钉的是**代码形状**（HTML 嵌套 / 样式声明 / 接线路径）；
真渲染（两个下拉与发送键是否真在同一行、几何是否包含、相邻栏位几何）由
`run/ui_chat_probe.py` 在无头 Edge 里量。

**这一轮把上一版的两条守卫换了方向（不是放宽）**：
· 上一版断言「本项目没有推理强度档位」——那是 2026-10-06 的结论（当时探针发现
  `reasoning_effort` 对 deepseek-flash 无效）。现在服务端 `/models` 自己声明了
  `effort.supported_levels=["low","high","max"]`，所以档位是**真实存在**的；
  守卫改成「档位与文案必须来自后端回包，前端不许自己拼」——比原来更难糊弄。
· 上一版断言「发送那一行只有按钮」——现在的需求就是同一行加两个下拉；
  守卫改成「只许功能控件（select/option/button）」，装饰性 span 仍然禁止。
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

#: 文本域底部让位带（与 run/ui_chat_probe.py 的 TEXTAREA_PADDING_BOTTOM 同一个数）
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
    assert len(rules) >= 9, f"切出来的新样式块只有 {len(rules)} 条规则，切片写歪了（会变成空判）"
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
    """#input / 两个下拉 / #btn-send / #btn-stop 必须都在 `.composer-input` 这个盒子**里面**。

    所以断言按盒子配平取内容，挪出去一格就红。
    """
    box = _element_body(INDEX_HTML, '<div class="composer-input">')
    for marker in (
        'id="composer-model"',
        'id="composer-thinking"',
        'class="composer-actions"',
        'id="btn-send"',
        'id="btn-stop"',
    ):
        assert marker in box, f"输入框内部少了 {marker}（下拉/发送按钮又跑出框外了）"
    assert box.index('id="input"') < box.index('class="composer-actions"'), (
        "文本域要在前：textarea 先定高度，控件才谈得上「落在框底」"
    )


def test_selects_and_send_share_one_actions_row_before_the_buttons() -> None:
    """两个下拉必须**和发送键同处 `.composer-actions`**，且排在发送键之前（同一行靠右）。

    这条是需求本身：分成两行或把下拉放到 `.composer-meta` 那种左侧状态行里，都不算实现。
    """
    box = _element_body(INDEX_HTML, '<div class="composer-input">')
    actions = _element_body(box, '<div class="composer-actions">')
    for marker in ('id="composer-model"', 'id="composer-thinking"', 'id="btn-send"'):
        assert marker in actions, f"{marker} 不在发送那一行里（那就是两行了）"
    assert actions.index('id="composer-model"') < actions.index('id="btn-send"')
    assert actions.index('id="composer-thinking"') < actions.index('id="btn-send"')


def test_composer_actions_holds_functional_controls_only() -> None:
    """发送那一行**只许功能控件**（select / option / button）：装饰性 span/div 仍不许回来。"""
    box = _element_body(INDEX_HTML, '<div class="composer-input">')
    actions = _element_body(box, '<div class="composer-actions">')
    assert "<span" not in actions, f"发送那一行又出现装饰性 span：{actions.strip()[:120]}"
    tags = {name.lower() for name in re.findall(r"<([a-zA-Z]+)", actions)}
    assert tags <= {"select", "option", "button"}, f"发送那一行混进了非控件元素：{sorted(tags)}"
    assert "select" in tags, "下拉被删了？这一行本来就该有模型 / 思考深度两个下拉"
    assert 'id="btn-send"' in actions and 'id="btn-stop"' in actions


def test_usage_and_hint_did_not_come_back() -> None:
    """顺手守住上一轮的清理：用量留在标题右侧，输入区里不再有 #hint / #session-usage。"""
    box = _element_body(INDEX_HTML, '<div class="composer-input">')
    assert 'id="hint"' not in box
    assert 'id="session-usage"' not in box


def test_save_note_is_a_hidden_status_line_inside_the_box() -> None:
    """保存确认要在输入框内部（不占消息流的行高），且默认隐藏、带 role=status。"""
    box = _element_body(INDEX_HTML, '<div class="composer-input">')
    assert 'id="composer-note"' in box, "没有保存确认的位置（选了档位用户看不到有没有存住）"
    note = re.search(r"<p[^>]*id=\"composer-note\"[^>]*>", box)
    assert note, "找不到 #composer-note 的标签"
    assert "hidden" in note.group(0), "保存确认默认必须隐藏，否则空占一行"
    assert 'role="status"' in note.group(0), "没有 aria-live 语义，读屏用户看不到保存结果"


# ------------------------------------------------------------------ CSS：定位与让位


def test_the_input_wrapper_is_the_positioning_context() -> None:
    """定位上下文挂在 `.composer-input` 上（不是 `.composer`：浮层加载圈退休时的约定）。"""
    body = body_of(".composer-input")
    assert "position: relative" in body, f".composer-input 没有成为定位上下文：{body!r}"


def test_actions_row_is_absolute_and_a_single_nowrap_line() -> None:
    """`.composer-actions` 必须绝对定位 + flex + **nowrap** + 靠右：一条行、不换行、不顶移消息流。"""
    body = body_of(".composer-actions")
    assert body, "新样式块里找不到 .composer-actions 规则"
    assert "position: absolute" in body, f".composer-actions 不是绝对定位：{body!r}"
    assert "display: flex" in body, f".composer-actions 不是 flex 行：{body!r}"
    assert "flex-wrap: nowrap" in body, f".composer-actions 允许换行（会吃掉一行高度）：{body!r}"
    assert "justify-content: flex-end" in body, f".composer-actions 没靠右：{body!r}"
    assert "right:" in body and "bottom:" in body, f".composer-actions 没贴右下角：{body!r}"


def test_save_note_is_positioned_and_does_not_swallow_clicks() -> None:
    """保存确认绝对定位在框内左下，且不吃点击（点它应该落到文本域上）。"""
    body = body_of(".composer-note")
    assert body, "新样式块里找不到 .composer-note 规则"
    assert "position: absolute" in body, f".composer-note 不是绝对定位：{body!r}"
    assert "pointer-events: none" in body, f".composer-note 会挡住点击：{body!r}"


def test_selects_shrink_but_never_push_the_send_button_out() -> None:
    """两个下拉可收缩（min-width: 0）且有上限：窄窗口时压的是文案，不是把发送键挤出去。"""
    body = body_of(".composer-model,\n.composer-thinking") or body_of(
        ".composer-model, .composer-thinking"
    )
    assert body, "找不到两个下拉的样式规则"
    assert "min-width: 0" in body, f"下拉不允许收缩（会把发送键挤出去）：{body!r}"
    assert "max-width" in body, f"下拉没有宽度上限（长模型名会撑爆这一行）：{body!r}"


def test_textarea_reserves_room_for_the_overlay_controls() -> None:
    """文本域底部留出让位带，否则最后一行正文会被这一行控件压住。"""
    body = body_of(".composer-input textarea")
    assert body, "找不到 .composer-input textarea 规则（文本域没被框内控件接管）"
    assert f"padding-bottom: {TEXTAREA_PADDING_BOTTOM}" in body, f"没有让位带：{body!r}"


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


def _composer_choices_block() -> str:
    """从下拉装载到保存的整段（`loadModelChoices` … `wireComposerChoices` 结束）。"""
    assert "function loadModelChoices(" in APP_JS, "找不到 loadModelChoices（模型清单装载口）"
    head = APP_JS.index("function loadModelChoices(")
    tail = APP_JS.index("function renderHealth(", head)
    return APP_JS[head:tail]


def test_choices_come_from_models_list_not_a_hardcoded_name() -> None:
    """模型清单与档位都取自 `models.list` 的回包 —— 界面上不许另写一份死清单。"""
    block = _composer_choices_block()
    assert 'scout.call("models.list"' in block, "没读 models.list（显示的就会是另写的一份）"
    assert "available_models" in block, "没取 available_models"
    assert "level_options" in block, "没取 level_options（六档选项）"
    assert "deepseek" not in block.lower(), "把模型名写死了（应该跟着服务端声明走）"


#: 前端**不许自己写**的真实档位字样（写了就说明它在拼一份自己的档位表）。
INVENTED_EFFORT_LABELS = ("（high）", "(high)", "（max）", "(max)")


def _strip_js_comments(src: str) -> str:
    """剥掉 JS 的 `//` 与 `/* */` 注释（**字符串里的同形字符不动**）。

    为什么要这个：守卫原先拿裸文本扫，于是
    `// label 由后端给（"中（high）"）—— 前端不自己拼档位名` 这句**解释约定的注释**
    把它判成了"前端在拼档位"，逼人去删好注释（上一轮就这么红的）。
    所以判红前先剥注释 —— 但剥离器本身要能区分"注释"与"字符串里的 //"，
    否则一句 `"https://…"` 就会把后面的代码全吞掉，守卫反而变成空判。
    """
    out: list[str] = []
    i = 0
    total = len(src)
    quote = ""
    while i < total:
        ch = src[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < total:
                out.append(src[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < total and src[i + 1] == "/":
            while i < total and src[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < total and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = total if end < 0 else end + 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _invented_effort_labels(code: str) -> list[str]:
    """代码（**非注释**）里自己写出来的真实档位字样；空列表 = 干净。"""
    stripped = _strip_js_comments(code)
    return [label for label in INVENTED_EFFORT_LABELS if label in stripped]


def test_level_labels_are_backend_supplied_with_real_effort() -> None:
    """档位文案（"中（high）"）必须由后端拼好，前端只落地 —— 不许自己写档位名或真实档位。

    这条防的是"界面看着有五档、服务端只有三档"的幻觉：真实档位是 `/models` 声明的，
    前端再拼一份（哪怕只是拼括号里的 high）就会与声明漂移。

    **判据只扫代码，不扫注释**：注释里写「label 由后端给（"中（high）"）」是在解释约定，
    不是前端在拼档位；拿裸文本判会逼人删掉好注释（并掩盖真正的硬编码）。
    """
    block = _composer_choices_block()
    code = _strip_js_comments(block)
    # 剥注释不能把整段吃光：空块会让下面所有断言变成空判（假绿比红危险）
    assert len(code) > 400, f"剥注释后只剩 {len(code)} 字符，剥离器写歪了（会变成空判）"
    assert "function fillLevelOptions(" in code, "剥注释把函数头一起吃掉了"
    assert "item.label" in code, "没有直接用后端给的 label（那就是前端自己拼档位名）"
    offenders = _invented_effort_labels(block)
    assert not offenders, f"前端自己拼了真实档位（代码里，不是注释）：{offenders}"
    assert "item.supported === false" in code, "不支持的档位没有被置灰（会让人选到发不出去的档）"


def test_hardcoded_level_guard_still_catches_real_code() -> None:
    """**反例**：代码里真写了档位文字就必须红；写注释里则不许红。

    没有这条，上面那条的"剥注释"就是给自己放水 —— 剥多了（连代码一起吃掉）它会静默通过。
    三个样本分别钉住：真硬编码要被抓、注释里说不算、字符串里的 `//` 不吃掉后续代码。
    """
    hardcoded = (
        "function fillLevelOptions(node, choices) {\n"
        '  const option = document.createElement("option");\n'
        '  option.textContent = "中（high）";\n'
        "  node.appendChild(option);\n"
        "}\n"
    )
    assert _invented_effort_labels(hardcoded) == ["（high）"], "反例没被抓住 → 守卫已经是空判"

    comment_only = (
        "function fillLevelOptions(node) {\n"
        '  // label 由后端给（"中（high）"），前端不拼\n'
        "  /* 另一个写法：(max) 也不许自己拼 */\n"
        "  option.textContent = item.label;\n"
        "}\n"
    )
    assert _invented_effort_labels(comment_only) == [], "把注释里的说明当成代码了（会逼人删注释）"

    url_inside_string = (
        'const endpoint = "https://example.test/v1";\noption.textContent = "（max）";\n'
    )
    assert _invented_effort_labels(url_inside_string) == ["（max）"], (
        "字符串里的 // 被当成注释，后面的代码逃过了检查"
    )


def test_saving_a_choice_writes_config_keys_and_says_when_it_applies() -> None:
    """选完要真的写 config.yaml 的两个键，并明说"下一轮生效"（不是"立即生效"）。"""
    block = _composer_choices_block()
    assert 'scout.call("settings.update"' in block, "选择没有写回配置（那就是个假下拉）"
    assert '"model"' in block and '"reasoning_effort"' in block, (
        "没写 llm.chat.model / reasoning_effort"
    )
    assert "下一轮生效" in block, "没说明什么时候生效（中途换会让正在跑的这一轮结果对不上）"
    assert "保存失败" in block, "保存失败没有反馈（用户会以为改好了）"


def test_meta_is_refreshed_after_saving_settings() -> None:
    """改完模型保存后要立刻刷新下拉里的显示（否则显示的是旧值）。"""
    calls = APP_JS.count("await refreshComposerMeta();")
    assert calls >= 2, f"refreshComposerMeta 只在 {calls} 处被调用（保存后没刷新）"


def test_choices_are_wired_at_boot() -> None:
    """下拉的 change 事件必须在启动时接上 —— 否则点了没反应。"""
    block = APP_JS[APP_JS.index("async function boot(") :]
    assert "wireComposerChoices();" in block, "启动时没接下拉的事件"
    assert 'addEventListener("change"' in APP_JS, "两个下拉没有 change 监听"


def test_meta_elements_exist_in_html() -> None:
    for marker in ('id="composer-model"', 'id="composer-thinking"'):
        assert marker in INDEX_HTML, f"界面里少了 {marker}"
