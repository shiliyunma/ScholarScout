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
    """详情面板的每个来源都是按钮（带 data-source-id），点了才走通路。"""
    body = _function_body(MAP_JS, "showMapNodeInfo")
    assert "map-source-open" in body, "详情里没有 .map-source-open 按钮"
    assert "data-source-id" in body, "来源按钮没带 data-source-id"
    assert "openMapSource(" in body, "来源按钮没接到 openMapSource"


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
