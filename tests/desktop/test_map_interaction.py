"""知识地图四个交互 + "铺满可用区"的源码契约（可入库的那一半）。

为什么这份能入库、`run/ui_map_probe.py` 不能：`run/` 是本地证据目录（含截图、桩桥、
本机 Edge 路径），被 .gitignore 挡着，也不该为它改惯例。所以把**不依赖排版**的契约
提到这里跑门禁；"包围盒占可用区 ≥60%、四边边距 ≥8px" 这类必须靠真排版才能量的，
留在探针里当本地证据。

钉住的四件事（每件都真出过问题）：
1. 图层切换必须走 `mapTeardown()` → 清账本 → 再建图。漏了清理，上一层的监听/定时器
   会活到页面关闭（拖拽挂的是 window 上的 mousemove）。
2. 路径高亮读边时 `rel` 与 `kind` 都得认（后端 `topic.find_path` 回的是 `rel`），
   图上画出来的边 class 用 `rel-` 前缀（CSS 的 `.map-edge.rel-*` 才生效）。
3. 点节点里的来源必须走资料库现成通路 `showSourceBySourceId`，不许在地图里另写一套打开逻辑。
4. 画布必须**按容器实测像素**设 viewBox 并铺满（固定 viewBox + `meet` = 内容缩在中间一条，
   侧栏里看着就是缩在右下角一小块），且尺寸监听要进账本、换层时一起断掉。
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
        'id="btn-map-layer-concept"',
        'id="btn-map-layer-source"',
        'id="map-path-from"',
        'id="map-path-to"',
        'id="btn-map-path"',
    ):
        assert anchor in INDEX_HTML, f"index.html 缺少 {anchor}"


def test_wire_map_tab_binds_layer_switch_and_search() -> None:
    """页签接线：图层按钮、搜索、路径三个入口都接到了对应函数。"""
    body = _function_body(MAP_JS, "wireMapTab")
    assert "mapSwitchLayer(" in body, "图层按钮没接到 mapSwitchLayer"
    assert "mapRunSearch(" in body, "搜索没接到 mapRunSearch"
    assert "mapRunPath(" in body, "路径没接到 mapRunPath"


# --------------------------------------------------------------------- 图层切换与清理


def test_render_tears_down_before_building_new_figure() -> None:
    """重画顺序：先按账本清理，再建新图（反过来就有两张图的可视状态并存）。"""
    body = _function_body(MAP_JS, "renderMapFigure")
    teardown = body.index("mapTeardown()")
    build = body.index("buildMapSvg(")
    assert teardown < build, "renderMapFigure 里 mapTeardown() 没有排在 buildMapSvg() 之前"
    assert "mapState.epoch += 1" in body, "重建时没换 epoch —— 探针数不出上一张图的残留"


def test_switch_layer_uses_source_graph_rpc_and_keeps_cache() -> None:
    """资料层走 `topic.get_source_graph`；两层各缓存一份（切回来不重取）。"""
    body = _function_body(MAP_JS, "mapSwitchLayer")
    assert "topic.get_source_graph" in body, "资料层没有调 topic.get_source_graph"
    assert "mapState.layers" in body, "图层载荷没有分层缓存（切回来会重取）"
    assert "mapState.layer" in body, "切换后没记当前层"


def test_teardown_clears_listeners_timers_and_observers() -> None:
    """账本三样都要清：监听、定时器、ResizeObserver（新增的尺寸监听不能漏）。"""
    body = _function_body(MAP_JS, "mapTeardown")
    for anchor in ("removeEventListener", "clearTimeout", "disconnect()"):
        assert anchor in body, f"mapTeardown 没有 {anchor}"
    assert "mapState.listeners = []" in body
    assert "mapState.timers = []" in body
    assert "mapState.observers = []" in body, "mapTeardown 没把 observers 账本清空"


# --------------------------------------------------------------------- 路径 / 搜索


def test_path_edges_read_rel_key_not_only_kind() -> None:
    """`topic.find_path` 的边用 `rel`：只认 `kind` 会把关系名显示成"未知"。"""
    body = _function_body(MAP_JS, "mapRunPath")
    assert re.search(r"edge\.rel\s*\|\|\s*edge\.kind", body), (
        "mapRunPath 里没有 `edge.rel || edge.kind` —— 路径的边类型会退化成未知"
    )


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
