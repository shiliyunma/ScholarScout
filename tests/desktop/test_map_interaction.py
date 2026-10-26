"""知识地图交互 + "铺满可用区"的源码契约（可入库的那一半）。

为什么这份能入库、`run/ui_map_probe.py` 不能：`run/` 是本地证据目录（含截图、桩桥、
本机 Edge 路径），被 .gitignore 挡着，也不该为它改惯例。所以把**不依赖排版**的契约
提到这里跑门禁；"包围盒占可用区 ≥60%、四边边距 ≥8px" 这类必须靠真排版才能量的，
留在探针里当本地证据。

钉住的几件事（每件都真出过问题）：
1. 重画必须走 `mapTeardown()` → 清账本 → 再建图。漏了清理，上一张图的监听/定时器
   会活到页面关闭（拖拽挂的是 window 上的 mousemove）。
2. **节点热区**：圆点只有 7~9px，标签又是 `pointer-events:none` —— 不做热区就是
   "点文字没反应"。所以每个节点都要有 `.map-hit`（圆点外扩）与 `.map-hit-rect`
   （按标签实测宽度铺的矩形），且两者与标签共用一份贴边平移几何。
3. **拖拽阈值**：按下即可能拖动，位移不足 `MAP_DRAG_THRESHOLD` 就不许写 transform，
   否则"点一下"会被当成"把图拖走了"。
4. 点节点里的来源必须走资料库现成通路 `showSourceBySourceId`，不许在地图里另写一套。
5. 画布必须**按容器实测像素**设 viewBox 并铺满（固定 viewBox + `meet` = 内容缩在中间一条）。
6. 图层切换与"查路径"整条线已删（收敛成概念图单一视图）：删掉的符号不许偷偷回来 ——
   它们会带回两条已经没人维护的取数路径。
"""

from __future__ import annotations

import re

from ._ui import INDEX_HTML, STYLE_CSS, UI_DIR

#: 知识地图的全部前端逻辑都在这一个文件里（app.js 只负责页签接线）
MAP_JS = (UI_DIR / "map.js").read_text(encoding="utf-8")


def _function_body(text: str, name: str) -> str:
    """按大括号配对切出 `function name(...) {...}` 的函数体（含签名）。

    为什么不用正则直接截到下一个 `function`：函数里有嵌套的 callback 与字符串里的
    花括号，必须配对计数才不会切歪。
    """
    start = text.index(f"function {name}(")
    brace = text.index("{", start)
    depth = 0
    for index in range(brace, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"map.js 里 {name} 的花括号没配平（文件被截断了？）")


# --------------------------------------------------------------------- 接线


def test_index_html_loads_map_js_and_its_controls() -> None:
    """页面真的加载了 map.js，且控件 id 齐全（缺一个，那一路交互就静默失效）。"""
    assert "map.js" in INDEX_HTML, "index.html 没有加载 map.js"
    for anchor in (
        'id="map-body"',
        'id="map-legend"',
        'id="map-info"',
        'id="map-search"',
        'id="btn-map-search"',
    ):
        assert anchor in INDEX_HTML, f"index.html 缺少 {anchor}"


def test_layer_switch_and_path_controls_are_gone() -> None:
    """图层切换与查路径已整条删除：控件、符号都不许留（留着就是没人维护的第二条取数路径）。"""
    for gone in (
        "btn-map-layer-source",
        "btn-map-layer-concept",
        "map-path-from",
        "map-path-to",
        "btn-map-path",
        "map-path-note",
    ):
        assert gone not in INDEX_HTML, f"index.html 还留着已删的 {gone}"
    for symbol in ("mapSwitchLayer", "mapRunPath", "MAP_LAYERS", "mapEdgeBand"):
        assert symbol not in MAP_JS, f"map.js 还留着已删的 {symbol}"
    # RPC 契约本身还在（后端有），但界面不许再调这两条已下线的取数路径
    for call in ('scout.call("topic.get_source_graph"', 'scout.call("topic.find_path"'):
        assert call not in MAP_JS, f"map.js 还在调已下线的 {call}"


def test_wire_map_tab_binds_search_and_zoom() -> None:
    """页签接线：搜索与放大两个入口都接到了对应函数。"""
    body = _function_body(MAP_JS, "wireMapTab")
    assert "mapRunSearch(" in body, "搜索没接到 mapRunSearch"
    assert 'el("btn-map-zoom")' in body, "放大按钮没接线"


# --------------------------------------------------------------------- 节点热区（点文字也能开）


def test_every_node_ships_two_hit_shapes() -> None:
    """每个节点都有两片热区：圆点外扩的圆 + 盖住标签的矩形（标签自己吃不到事件）。

    只留 7~9px 的圆点，"点标签文字没反应"就是必然 —— 用户点的是字。
    """
    body = _function_body(MAP_JS, "buildMapSvg")
    assert '"map-hit"' in body, "buildMapSvg 没给节点挂 .map-hit（圆点热区）"
    assert '"map-hit-rect"' in body, "buildMapSvg 没给节点挂 .map-hit-rect（标签热区）"
    assert body.count('fill: "transparent"') >= 2, "热区不是透明填充（会画成实心块）"
    assert body.count('"pointer-events": "all"') >= 2, (
        "热区没开 pointer-events:all（透明填充默认不接事件）"
    )
    # 热区必须先挂（在圆点/文字之前）= 画在下面，不遮住可见图形
    assert body.index('"map-hit"') < body.index('"map-dot"'), "热区挂在圆点之后（会盖住圆点）"
    assert body.index('"map-hit-rect"') < body.index('"map-label"'), "热区挂在标签之后"


def test_hit_geometry_pads_dot_and_covers_label_width() -> None:
    """热区尺寸由几何函数算：圆点半径 + `MAP_HIT_DOT_PAD`，矩形宽 = 标签宽 + 左右各 4。"""
    assert re.search(r"var MAP_HIT_DOT_PAD = 10;", MAP_JS), "没有 MAP_HIT_DOT_PAD = 10"
    assert re.search(r"var MAP_HIT_PAD_X = \d+;", MAP_JS), "没有 MAP_HIT_PAD_X（标签热区左右留白）"
    assert re.search(r"var MAP_HIT_HEIGHT = \d+;", MAP_JS), "没有 MAP_HIT_HEIGHT（标签热区高度）"

    body = _function_body(MAP_JS, "mapSyncHitShapes")
    assert "mapNodeRadius(" in body, "热区没按节点半径算"
    assert "MAP_HIT_DOT_PAD" in body, "圆点热区没加 MAP_HIT_DOT_PAD（半径不够点）"
    assert "mapLabelHalfWidth(" in body, "标签热区没按标签宽度算"
    assert "MAP_HIT_PAD_X" in body and "MAP_HIT_HEIGHT" in body, "标签热区没用自己的宽高常量"
    for attr in ('"x"', '"y"', '"width"', '"height"'):
        assert f"rect.setAttribute({attr}" in body, f"标签热区没同步 {attr}"


def test_hit_shapes_share_the_label_shift() -> None:
    """贴边节点：标签被平移回画布内，热区矩形必须跟着同一份位移（否则字挪了、热区没挪）。"""
    body = _function_body(MAP_JS, "mapSyncHitShapes")
    assert "mapLabelShift(" in body, "热区没复用 mapLabelShift（贴边标签会与热区错位）"
    assert "shift" in body, "mapSyncHitShapes 没把平移量交出去（标签没法共用）"
    build = _function_body(MAP_JS, "buildMapSvg")
    assert "mapSyncHitShapes(group, node, text, canvas.width)" in build, (
        "buildMapSvg 没用 mapSyncHitShapes 算标签位移与热区"
    )
    refit = _function_body(MAP_JS, "mapRefit")
    assert "mapSyncHitShapes(" in refit, "换尺寸后没重算热区（重铺一次字又点不中了）"

    label = re.search(r"\.map-node \.map-label\s*\{([^}]*)\}", STYLE_CSS)
    assert label, "style.css 里找不到 .map-node .map-label"
    assert "pointer-events: none" in " ".join(label.group(1).split()), (
        ".map-label 又吃事件了（本该让热区接手）"
    )
    hits = re.search(r"\.map-hit,\s*\n?\.map-hit-rect\s*\{([^}]*)\}", STYLE_CSS)
    assert hits, "style.css 里找不到 .map-hit / .map-hit-rect"
    assert "pointer-events: all" in " ".join(hits.group(1).split()), "热区没开 pointer-events:all"


# --------------------------------------------------------------------- 拖拽阈值


def test_drag_writes_transform_only_past_threshold() -> None:
    """按下可能变成拖拽，但位移不足阈值一个像素都不许写 transform（点击不能被当成拖动）。"""
    assert re.search(r"var MAP_DRAG_THRESHOLD = 4;", MAP_JS), "没有 MAP_DRAG_THRESHOLD = 4"
    body = _function_body(MAP_JS, "mapDragStart")
    assert "MAP_DRAG_THRESHOLD" in body, "mapDragStart 没用阈值常量"
    assert re.search(r"Math\.sqrt\(dx \* dx \+ dy \* dy\) < MAP_DRAG_THRESHOLD", body), (
        "阈值不是按位移长度判的（斜着抖 3px+3px 会被当成拖拽）"
    )
    guard = body.index("Math.sqrt(")
    first_write = body.index("stage.setAttribute(")
    assert guard < first_write, "阈值判定排在写 transform 之后（等于没有阈值）"
    assert re.search(r"if \(!dragging\) \{", body), "没有 dragging 标记（越过阈值那次写不进去）"
    # 拖拽挂的是 window 上的监听：必须在 stop 里摘掉并划掉账本
    assert 'mapOff(window, "mousemove", move)' in body, "拖拽结束没摘 mousemove"
    assert 'mapOff(window, "mouseup", stop)' in body, "拖拽结束没摘 mouseup"


# --------------------------------------------------------------------- 重画清理


def test_render_tears_down_before_building_new_figure() -> None:
    """重画顺序：先按账本清理，再建新图（反过来就有两张图的可视状态并存）。"""
    body = _function_body(MAP_JS, "renderMapFigure")
    teardown = body.index("mapTeardown()")
    build = body.index("buildMapSvg(")
    assert teardown < build, "renderMapFigure 里 mapTeardown() 没有排在 buildMapSvg() 之前"
    assert "mapState.epoch += 1" in body, "重建时没换 epoch —— 探针数不出上一张图的残留"


def test_teardown_clears_listeners_timers_and_observers() -> None:
    """账本三样都要清：监听、定时器、ResizeObserver（新增的尺寸监听不能漏）。"""
    body = _function_body(MAP_JS, "mapTeardown")
    for anchor in ("removeEventListener", "clearTimeout", "disconnect()"):
        assert anchor in body, f"mapTeardown 没有 {anchor}"
    assert "mapState.listeners = []" in body
    assert "mapState.timers = []" in body
    assert "mapState.observers = []" in body, "mapTeardown 没把 observers 账本清空"


# --------------------------------------------------------------------- 边


def test_svg_edge_class_uses_rel_prefix() -> None:
    """边上挂的是 `rel-<kind>` class（CSS 规则是 `.map-edge.rel-prerequisite` 等）。"""
    assert '"map-edge rel-"' in MAP_JS, "边的 class 没带 rel- 前缀"
    assert "map-edge kind-" not in MAP_JS, "边被挂成了 kind- 前缀，rel-* 的线型样式不会生效"


def test_search_centers_node_within_current_canvas() -> None:
    """搜索定位按**当前画布**（viewBox）算中心，不许读死常量。"""
    body = _function_body(MAP_JS, "mapCenterNode")
    assert "mapViewSize(" in body, "mapCenterNode 没按 viewBox 算中心（换尺寸后会偏）"
    assert "MAP_VIEW_WIDTH" not in body, "mapCenterNode 又读回了固定常量"


# --------------------------------------------------------------------- 来源通路


def test_source_click_goes_through_show_source_by_source_id() -> None:
    """点节点里的来源 → 切到资料库页签 → `showSourceBySourceId`（不另写一套）。"""
    body = _function_body(MAP_JS, "openMapSource")
    assert "showSourceBySourceId(" in body, "没有走资料库现成通路 showSourceBySourceId"
    assert '.tab[data-tab="sources"]' in body, "打开来源前没切到「资料库」页签"
    assert "archive.get" not in body, "openMapSource 自己调了 archive.get（重复实现打开逻辑）"


def test_node_info_renders_source_buttons_with_ids() -> None:
    """详情面板的每条来源都是按钮（带 data-source-id / data-action），点了才走通路。

    按钮的构造搬进了 `mapInfoSourceRow`（一行 = 标题 + 状态 + 按钮）：这里跟着搬，
    断言的还是同一件事 —— "每个来源一个按钮，按钮上带 source_id，点了走 openMapSource"。
    """
    body = _function_body(MAP_JS, "mapInfoSourceRow")
    assert "map-source-open" in body, "详情里没有 .map-source-open 按钮"
    assert "data-source-id" in body, "来源按钮没带 data-source-id"
    assert "data-action" in body, "来源按钮没带 data-action（探针要按动作挑按钮）"
    assert "openMapSource(" in body, "来源按钮没接到 openMapSource"
    panel = _function_body(MAP_JS, "showMapNodeInfo")
    assert "mapInfoSourceRow(ref)" in panel, "详情面板没按行渲染来源"


# --------------------------------------------------------------------- 详情面板：真标题 / 缺失计数 / 获取正文


def test_node_info_renders_real_titles_from_source_refs() -> None:
    """详情面板读 `source_refs` 的**真标题**（后端已做三级回退，不会为空）。

    改之前用的是 `node.sources` 的 id，渲染成「打开资料 N（src-xxx）」—— 用户看到的就是
    一串「资料1 资料2」（第 6 条反馈）。
    """
    refs = _function_body(MAP_JS, "mapInfoSourceRefs")
    assert "node.source_refs" in refs, "详情面板没读 source_refs（还在用纯 id，所以只有「资料N」）"
    assert "ref.title" in refs, "source_refs 的 title 没被取用"
    assert "node.sources" in refs and '"来源 " + short(id, 12)' in refs, (
        "老数据（没有 source_refs）没有回退成「来源 <id 前 12 位>」"
    )
    row = _function_body(MAP_JS, "mapInfoSourceRow")
    assert "ref.title" in row, "行里没显示标题"
    assert '"（" + ref.year + "）"' in row, "标题没带年份（有年份时应显示「标题（2024）」）"
    # 编号式文案一个字符都不许留
    assert "index + 1" not in MAP_JS, "map.js 里还有编号来源的残留（「打开资料 N」）"
    assert '"打开资料 "' not in MAP_JS, "map.js 里还有「打开资料 N」的拼法"


def test_source_rows_sort_by_title_with_chinese_collation() -> None:
    """按标题排序（中文走拼音），同标题再按 id —— 每次打开顺序一致，按钮不会乱跳。"""
    refs = _function_body(MAP_JS, "mapInfoSourceRefs")
    assert "out.sort(" in refs, "来源没有排序（后端返回顺序一变，按钮就换位置）"
    assert 'localeCompare(String(b.title), "zh")' in refs, "没按标题做中文排序"
    assert "localeCompare(String(b.id))" in refs, "同标题没有按 id 稳定兜底"


def test_dropped_sources_are_counted_and_never_buttons() -> None:
    """库里查不到的来源（`dropped_sources`）只留一行灰字计数，**绝不**渲染成按钮。

    用户第 5 条："有很多没有来源，要么不出现，要么点了立即查" —— 点了必然是空的那一类不出现。
    """
    body = _function_body(MAP_JS, "showMapNodeInfo")
    assert "dropped_sources" in body, "详情面板没读 dropped_sources"
    assert "另有 " in body and "条来源已不在库中（可能被删除或未入库）" in body, "缺了计数灰字"
    assert "map-info-dropped" in body, "计数行没有自己的 class（灰字样式落不到）"
    # 按钮只从 refs 生成；行构造里不许出现 dropped
    assert "mapInfoSourceRow(ref) }" in body or "mapInfoSourceRow(ref)" in body
    assert "dropped" not in _function_body(MAP_JS, "mapInfoSourceRow"), (
        "被丢弃的来源仍会渲染成一行/一个按钮（点了必然空）"
    )


def test_node_without_any_source_shows_placeholder_and_no_empty_header() -> None:
    """一条都没有（且没有丢的）→ 一句「这个节点没有关联资料」，**不出**空列表头。"""
    body = _function_body(MAP_JS, "showMapNodeInfo")
    assert "这个节点没有关联资料" in body, "没有说明文字"
    guard = body.index("!refs.length && !dropped")
    label = body.index('"map-info-label"')
    assert guard < label, "「没有关联资料」排在列表头之后 —— 会先画一个空列表头"


def test_missing_text_source_offers_fetch_button() -> None:
    """`has_text:false` → 按钮是「获取正文」，点了立刻 `archive.download({source_id})`。"""
    row = _function_body(MAP_JS, "mapInfoSourceRow")
    assert "获取正文" in row and "打开资料" in row, "没有区分「获取正文」与「打开资料」"
    assert "ref.has_text === false" in row, "没按 has_text 分流"
    fetch = _function_body(MAP_JS, "fetchMapSourceText")
    assert 'scout.call("archive.download"' in fetch, "「获取正文」没调 archive.download"
    assert "{ source_id: ref.id }" in fetch, "没按 source_id 传给 archive.download"
    assert "openMapSource(ref.id)" in fetch, "抓到正文后没继续打开"
    assert "alert(" not in fetch, "「获取正文」里用了 alert"


def test_download_failure_splits_disabled_from_large() -> None:
    """失败分三类说：功能未开启（+「去设置」）/ 大文件 / 其它原文 —— 全部写行内，不弹窗。

    默认配置 `features.enable_download=false`，后端**一定**回 ok:false + reason
    "按需下载已关闭…"，所以「获取正文」默认就是"点了告诉你去哪儿开"，不能做成永远没反馈。
    """
    err = _function_body(MAP_JS, "mapInfoDownloadError")
    assert "res.disabled === true" in err, "没认回包的 disabled:true"
    assert "/未开启|已关闭/" in err, "没按 reason 认「功能未开启」这一类"
    assert "设置 → 功能 → 允许下载" in err, "没告诉用户去哪儿开下载"
    assert "去设置" in err and "openSettings" in err, "没给「去设置」按钮（复用现有设置通路）"
    assert "文件较大，可在资料库里确认后重试" in err, "大文件没给二次确认的提示"
    assert "alert(" not in err, "失败提示用了 alert"
    assert "map-info-error" in _function_body(MAP_JS, "mapInfoSourceRow"), "行内错误没有落点"


def test_open_source_scrolls_detail_into_view() -> None:
    """打开资料后把 `#source-detail` 滚进视口（它在列表下面，不滚就是"被压在下面"）。"""
    body = _function_body(MAP_JS, "openMapSource")
    assert "mapRevealSourceDetail(" in body, "打开后没滚详情区"
    assert "showSourceBySourceId(" in body, "没走资料库现成通路"
    reveal = _function_body(MAP_JS, "mapRevealSourceDetail")
    assert 'el("source-detail")' in reveal, "滚的不是 #source-detail"
    assert 'scrollIntoView({ block: "nearest"' in reveal, "没用 scrollIntoView(block:nearest)"
    assert "result === false" in reveal, "没正文（false）时也会滚过去 —— 滚到的是一片空白"


def test_no_alert_in_map_or_source_open_path() -> None:
    """打开/下载这条链路上一个 alert 都不许有（它把页签切换和详情一起挡住 = "点了没反应"）。"""
    assert "alert(" not in MAP_JS, "map.js 还有 alert"
    body = _function_body(
        (UI_DIR / "sources.js").read_text(encoding="utf-8"), "showSourceBySourceId"
    )
    assert "alert(" not in body, "showSourceBySourceId 还有 alert"
    assert 'el("source-download-status")' in body, "没写进详情区下面那行提示"
    assert "return false;" in body, "没回 false（调用方据此决定要不要滚详情）"
    assert "return true;" in body, "打开成功没回 true"


def test_detail_has_a_floor_height_when_not_empty() -> None:
    """详情区不许是 0 高：滚进视口也要真的有内容可看（CSS 层给下限，不用内联 hack）。"""
    rule = re.search(r"#pane-sources \.detail:not\(:empty\)\s*\{([^}]*)\}", STYLE_CSS)
    assert rule, "style.css 没有给资料库详情区设下限高度"
    assert re.search(r"min-height:\s*\d+px", rule.group(1)), "下限高度不是像素值"
    assert ".detail" in STYLE_CSS and ":empty" in STYLE_CSS, "空详情区仍然要能收起"


# --------------------------------------------------------------------- 铺满可用区


def test_view_box_follows_measured_container_size() -> None:
    """viewBox 按**实测像素**建，不再固定 800x520（固定 = meet 留白 = 内容缩在中间）。"""
    body = _function_body(MAP_JS, "buildMapSvg")
    assert 'viewBox: "0 0 " + canvas.width + " " + canvas.height' in body, (
        "buildMapSvg 的 viewBox 不是按 canvas（实测尺寸）算的"
    )
    assert 'preserveAspectRatio: "xMidYMid meet"' in body, (
        "preserveAspectRatio 被改成别的了（会拉伸图形）"
    )
    measure = _function_body(MAP_JS, "mapMeasure")
    assert "getBoundingClientRect()" in measure, "mapMeasure 没量容器尺寸"
    assert "paddingLeft" in measure, "mapMeasure 没扣掉 padding（可用区会算大）"


def test_layout_is_fitted_to_the_available_box() -> None:
    """布局坐标按容器铺满：两轴各自缩到 (宽-2pad)×(高-2pad)，留白有下限也有上限。"""
    body = _function_body(MAP_JS, "mapFitNodes")
    assert "MAP_PAD_RATIO" in body and "MAP_PAD_MIN_X" in body, "没按比例/下限算留白"
    assert "innerW = Math.max(1, width - padX * 2)" in body, "横向没有铺到可用宽 - 2*pad"
    assert "innerH = Math.max(1, height - padY * 2)" in body, "纵向没有铺到可用高 - 2*pad"
    # 两轴独立缩放（窄高侧栏才不浪费高度）：sx / sy 分开算
    assert "sx = innerW / spanX" in body and "sy = innerH / spanY" in body, (
        "fit 用了单一 scale（等比缩）—— 窄栏里横向撑满、纵向只占一半，包围盒占比会掉到 40% 左右"
    )
    layout = _function_body(MAP_JS, "mapLayout")
    assert "node.lx = node.x" in layout, "mapLayout 没留原始坐标（换尺寸就无法重铺）"
    assert "mapFitNodes(nodes, width, height)" in layout, "mapLayout 末尾没有调用 mapFitNodes"


def test_resize_is_watched_and_ledgered() -> None:
    """容器变尺寸（侧栏拖宽 / 搬进放大浮层）要重铺，且监听进账本。"""
    body = _function_body(MAP_JS, "mapWatchSize")
    assert "new ResizeObserver(" in body, "没有 ResizeObserver（浮层/拖宽后画布不会重铺）"
    assert "observer.observe(host)" in body, "ResizeObserver 没观察 map-body"
    assert "mapState.observers.push(" in body, "ResizeObserver 没记账（换层时不会被断开）"
    assert 'mapOn(window, "resize"' in body, "没有 window.resize 兜底"

    refit = _function_body(MAP_JS, "mapRefit")
    assert 'setAttribute("viewBox"' in refit, "mapRefit 没更新 viewBox"
    assert "mapFitNodes(view.nodes" in refit, "mapRefit 没重算坐标"
    assert 'line.setAttribute("x1"' in refit, "mapRefit 没同步边端点（线会留在旧位置）"

    figure = _function_body(MAP_JS, "renderMapFigure")
    assert "mapWatchSize(built.svg, built.view, host)" in figure, "renderMapFigure 没挂尺寸监听"


def test_toolbar_and_legend_have_a_fixed_height_budget() -> None:
    """控件行与图例都是**定高单行**：它们多一行，画布就少一行高度。"""
    tools = re.search(r"\.row\.map-tools\s*\{([^}]*)\}", STYLE_CSS)
    assert tools, "style.css 里找不到 .row.map-tools"
    body = " ".join(tools.group(1).split())
    assert "flex-wrap: nowrap" in body, ".row.map-tools 允许换行（换行就吃掉画布高度）"
    assert re.search(r"height:\s*30px", body), ".row.map-tools 没有固定高度"

    legend = re.search(r"\.map-legend\s*\{([^}]*)\}", STYLE_CSS)
    assert legend, "style.css 里找不到 .map-legend"
    body = " ".join(legend.group(1).split())
    assert "flex-wrap: nowrap" in body, "图例还是 wrap —— 概念图 6 类 + 4 种边必然两行"
    assert re.search(r"height:\s*20px", body), "图例没有固定一行的高度"
    assert "overflow-x: auto" in body, "图例不让横向滑，一行放不下就看不到后面的项"

    assert "map-legend-title" in MAP_JS, "图例前面没有标题（单行图例看不出是什么）"


def test_visible_graph_copies_source_refs_for_the_info_panel() -> None:
    """`mapVisibleGraph` 是**白名单拷贝**：漏掉 `source_refs` / `dropped_sources`，
    详情面板会静默退化成裸 id（显示成「来源 src-xxx」），看起来像后端没给数据 ——
    这正是「资料显示成资料1」那一类 bug 的入口，必须钉住。"""
    body = _function_body(MAP_JS, "mapVisibleGraph")
    assert "source_refs" in body, "mapVisibleGraph 没拷 source_refs：详情面板拿不到真标题"
    assert "dropped_sources" in body, "mapVisibleGraph 没拷 dropped_sources：缺失计数永远 0"


# --------------------------------------------------------------------- 弱边（待验证）


def test_weak_edge_gets_is_weak_but_keeps_rel_class() -> None:
    """`confidence:"weak"` 的边既要有 `is-weak`（出虚线），也**不许丢掉** `rel-*`。

    为什么两条都要：`rel-*` 是边的语义线型（前置/变体/评测），`is-weak` 是
    "这条还没证据"的标记。只挂一条，另一层信息就没了。
    """
    body = _function_body(MAP_JS, "buildMapSvg")
    assert "mapEdgeIsWeak(edge)" in body, "buildMapSvg 没按 confidence 判弱边"
    assert '"map-edge rel-" + String(edge.kind || "uses")' in body, "边的 rel-* 前半段被改了"
    assert '" is-weak"' in body, "弱边没加 is-weak（CSS 的虚线就落不到它身上）"
    assert '"data-confidence"' in body, "弱边没写 data-confidence（探针/DOM 都认不出来）"
    assert '"data-weak-edges"' in body, "SVG 上没有弱边计数（探针没法一步断言）"


def test_weak_edge_is_dashed_and_wins_over_the_rel_line_styles() -> None:
    """弱边的虚线写在 CSS 里，且必须排在 `rel-*` 之后 —— 同选择器权重，靠后才生效。

    顺序反了会怎样：一条 `rel-prerequisite` 的弱边显示成 6 4 的前置线型，
    "待验证"在图上完全看不出来，而这正是这次改动要解决的事。
    """
    rule = re.search(r"\.map-edge\.is-weak\s*\{([^}]*)\}", STYLE_CSS)
    assert rule, "style.css 里没有 .map-edge.is-weak 规则"
    assert re.search(r"stroke-dasharray:\s*4\s*3", rule.group(1)), (
        "弱边没有 stroke-dasharray（画不成虚线）"
    )
    assert STYLE_CSS.index(".map-edge.is-weak {") > STYLE_CSS.index(".map-edge.rel-evaluates {"), (
        ".map-edge.is-weak 排在 rel-* 前面：前置/变体/评测的线型会盖掉弱边虚线"
    )
    # 悬停高亮照旧：is-hot 只改颜色/粗细，绝不许复位 dasharray（否则一悬停变实线）
    hot = re.search(r"\.map-edge\.is-hot\s*\{([^}]*)\}", STYLE_CSS)
    assert hot, "style.css 里没有 .map-edge.is-hot 规则"
    assert "dasharray" not in hot.group(1), (
        ".map-edge.is-hot 里动了 dasharray —— 悬停/选中时弱边会变实线"
    )


def test_legend_marks_dashes_as_pending_verification() -> None:
    """图例补一项「虚线 = 待验证」，且只在这张图真有弱边时出现（不硬塞一条找不到对应物的说明）。"""
    body = _function_body(MAP_JS, "renderMapLegend")
    assert "虚线 = 待验证" in body, "图例没有「虚线 = 待验证」"
    assert "map-legend-weak" in body, "图例里没有可定位的弱边项（class 缺失）"
    assert "map-line is-weak" in body, "图例样线不是 is-weak（看起来还是实线）"
    assert "mapEdgeIsWeak(" in body, "图例没按边的 confidence 数弱边"
    assert "if (weak > 0)" in body, "图例无条件挂「待验证」：没有弱边的图上会让人满图找虚线"
    assert "insertBefore(weakItem, title.nextSibling)" in body, (
        "「虚线 = 待验证」挂在了图例末尾：右栏 364px 一行放不下，默认就是看不见（要横滑才有）"
    )


def test_old_payload_without_confidence_stays_solid() -> None:
    """老数据（边没有 `confidence`）按 strong 画实线，且不报错。"""
    body = _function_body(MAP_JS, "mapEdgeIsWeak")
    assert '"strong"' in body and '=== "weak"' in body, "判弱边不是「明确等于 weak 才算」"
    assert '|| "strong"' in body, "缺省没落到 strong（老数据会被当成弱边）"
    assert "!!edge.confidence" not in body, '用了 truthy 判断：confidence:"high" 之类也会被画成虚线'
    # 边是整条 slice() 透传（不做白名单拷贝），所以 confidence 不会在取子图时被丢
    assert "((payload && payload.edges) || []).slice()" in _function_body(
        MAP_JS, "mapVisibleGraph"
    ), "mapVisibleGraph 改了边的拷贝方式：confidence 可能在半路被丢"


def test_count_line_reports_pending_edges_only_when_there_are_some() -> None:
    """计数行：`共 N 个节点、M 条边（其中 K 条待验证）`；K=0 不显示括号。

    K 优先取后端 `stats.weak_edges` / `counts.weak_edges`，读不到就数画出来的边
    （老后端没有这个字段也不许报错/显示 undefined）。
    """
    figure = _function_body(MAP_JS, "renderMapFigure")
    assert "条待验证" in figure, "计数行没有「其中 K 条待验证」"
    assert "weak > 0" in figure, "K=0 也打了括号（干净图上多一句 0 条待验证）"
    assert '" 个节点、"' in figure and '" 条边"' in figure, "计数行的节点/边文案被改了"

    helper = _function_body(MAP_JS, "mapWeakEdgeCount")
    assert "weak_edges" in helper, "没读后端的 stats.weak_edges / counts.weak_edges"
    assert 'typeof value === "number"' in helper, "没做类型容错（undefined 会算出 NaN）"
    assert "mapEdgeIsWeak(" in helper, "后端没给数时没有回落到数画出来的边"
