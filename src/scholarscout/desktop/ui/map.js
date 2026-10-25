/* 知识地图：侧栏标签页 + 可放大浮层。
 *
 * 纯 SVG + 原生 JS，**不引 d3**（也没有任何构建步骤 —— 与其它 ui/*.js 同一套全局函数风格）。
 * 力导向布局是自己写的 Fruchterman–Reingold 变体（斥力 k²/d、弹簧 d²/k、向心力、降温迭代），
 * 布局是**同步**算完再一次性画：几百个节点也就几十毫秒到一两百毫秒，比逐帧动画省事得多，
 * 也不会让"打开标签页"出现一段空白动画。
 *
 * 三条界面口径（与 docs/01 §5.4 对齐）：
 *   ① 节点颜色按 kind，边线型按 rel；度数（`weight`）由后端算，前端不另算一套；
 *   ② 节点 > 50 时默认只画**度数前 30**（另给"显示全部"）—— 全画出来就是一团毛线；
 *   ③ 一屏只画**当前会话最新的一张**：图属于主题，切到别的会话画出别人的图比空着更糟。
 */

var MAP_NODE_KINDS = [
  ["concept", "概念"],
  ["method", "方法"],
  ["dataset", "数据集"],
  ["tool", "工具"],
  ["venue", "期刊/会议"],
  ["source", "来源"],
];

var MAP_EDGE_KINDS = [
  ["prerequisite", "前置"],
  ["variant", "变体"],
  ["uses", "使用"],
  ["evaluates", "评测"],
  /* 资料层的边是"两篇来源共同支撑了 N 个概念"（`SHARED_CONCEPT_REL = shared_concept`）。
   * 它只会在资料层出现，但不收进这张表的话图例与路径说明会显示成英文原名。 */
  ["shared_concept", "共同支撑"],
];

/* 两层：概念图（落库）+ 资料图（现算）。界面一个开关切换，两层节点/边字段同形状。 */
var MAP_LAYERS = [
  ["concept", "概念图"],
  ["source", "资料图"],
];

/* 资料层线宽三档（下标 = band-1）。**档位由本图最大权重相对分档**（1/3、2/3 切），
 * 不写死绝对阈值：小主题最大权重只有 3，写死"≥5 才算粗"就永远只有一档。
 * 概念层不按权重分档（保持原来的 rel 线型语义），所以这个数组只在资料层用。 */
var MAP_EDGE_BAND_WIDTH = [1.2, 2.4, 3.8];

/* 超过这个数就默认只画前 30 个（用户要求：大图要可读） */
var MAP_TRUNCATE_OVER = 50;
var MAP_PREVIEW_LIMIT = 30;

/* 画布 = 容器的**实际像素**：viewBox 与容器等比 → `meet` 不再留白，坐标即像素
 * （以前固定 800x520，侧栏 364x500 时 `meet` 只画出 364x236 的一条，
 *  标签还被缩到 5px 左右，看着就是"内容缩在右下角一小块"）。
 * 800x520 退化为**量不到尺寸时的兜底**（页签还没显示、host 高度为 0）：真的量到
 * 尺寸后由 `mapRefit` 按真实尺寸重排重缩，容器变尺寸（侧栏拖宽 / 放大浮层）同理。 */
var MAP_VIEW_WIDTH = 800;
var MAP_VIEW_HEIGHT = 520;

/* 铺满可用区时的留白：两侧各占 10%（且有上下限）。固定像素留白在窄栏里会吃掉太多面积，
 * 纯比例留白在浮层里又会贴边，所以两头都夹住。验收线：包围盒占可用区 ≥ 60%、
 * 四边边距 ≥ 8px —— 这里的 padX/padY 就是实测边距的来源（再减去节点半径）。 */
var MAP_PAD_RATIO = 0.1;
var MAP_PAD_MIN_X = 26;
var MAP_PAD_MIN_Y = 24;
var MAP_PAD_MAX_X = 110;
var MAP_PAD_MAX_Y = 90;

/* 状态：两层载荷各存一份（切回来不重取）；`payload` 永远是**当前层**那一份。
 *
 * 还有一本**监听/定时器账本**（`listeners` / `timers`）。为什么要账本而不是"反正
 * innerHTML="" 就没了"：节点/边上的监听确实随 DOM 一起被回收，但
 *   · 拖拽往 `window` 上挂 mousemove/mouseup —— 不删就活到页面关闭；
 *   · 搜索防抖的 setTimeout —— 不清理就会在换了图层之后拿新图去定位旧节点；
 *   · `svg.__mapView` 把整棵旧 SVG 挂在闭包里。
 * 换层/重画一律先 `mapTeardown()`：删监听、清定时器、断引用，再建新的。
 * 账本上每条都记 `epoch`（第几张图），探针靠它断言"没有上一张图的残留"。 */
var mapState = {
  topicId: "",
  layer: "concept",
  layers: { concept: null, source: null },
  payload: null,
  view: null,
  showAll: false,
  epoch: 0,
  listeners: [],
  timers: [],
  observers: [],
  clearedListeners: 0,
  clearedTimers: 0,
  clearedObservers: 0,
  refits: 0,
  path: null,
};

var MAP_NS = "http://www.w3.org/2000/svg";


/* 挂监听并记账（`mapTeardown` 照着账本逐个删）。 */
function mapOn(target, type, handler, options) {
  if (!target || !handler) return null;
  target.addEventListener(type, handler, options);
  mapState.listeners.push({
    target: target,
    type: type,
    handler: handler,
    options: options,
    epoch: mapState.epoch,
  });
  return handler;
}


/* 定时器也记账：防抖搜索用，换图层时必须清掉（否则会拿新图定位旧节点）。 */
function mapLater(fn, ms) {
  var slot = { id: 0, epoch: mapState.epoch };
  slot.id = setTimeout(function () {
    mapState.timers = mapState.timers.filter(function (item) {
      return item !== slot;
    });
    fn();
  }, ms);
  mapState.timers.push(slot);
  return slot;
}


/* 换层/重画前的清理：删干净这一层的监听与定时器，并断开旧 SVG 的引用。 */
function mapTeardown() {
  mapState.listeners.forEach(function (item) {
    item.target.removeEventListener(item.type, item.handler, item.options);
  });
  mapState.clearedListeners += mapState.listeners.length;
  mapState.listeners = [];

  mapState.timers.forEach(function (item) {
    clearTimeout(item.id);
  });
  mapState.clearedTimers += mapState.timers.length;
  mapState.timers = [];

  /* ResizeObserver 也必须断开：不断开的话旧图的 observer 还会在容器变尺寸时
   * 回调到已经拆掉的 SVG 上（形状没变但白跑一次布局），而且闭包会一直挂着旧图。 */
  mapState.observers.forEach(function (item) {
    if (item.observer && typeof item.observer.disconnect === "function") {
      item.observer.disconnect();
    }
  });
  mapState.clearedObservers += mapState.observers.length;
  mapState.observers = [];

  /* 旧 SVG 上的缩放/平移状态与选中态：不删的话 `__mapView` 会让回收晚一步，
   * 而且同一份 DOM 被搬进浮层再搬回来时可能带着上一张图的平移量。 */
  Array.prototype.forEach.call(document.querySelectorAll(".map-svg"), function (svg) {
    try {
      delete svg.__mapView;
    } catch (error) {
      svg.__mapView = null;
    }
    svg.removeAttribute("data-picked");
    svg.removeAttribute("data-centered");
  });
}


/* 探针/排障用的实时计数（不是界面功能）。stale* 必须恒为 0 —— 不为 0 就是没清干净。 */
function mapDebugCounts() {
  var epoch = mapState.epoch;
  var stale = mapState.listeners.filter(function (item) {
    return item.epoch !== epoch;
  }).length;
  var staleTimers = mapState.timers.filter(function (item) {
    return item.epoch !== epoch;
  }).length;
  var staleObservers = mapState.observers.filter(function (item) {
    return item.epoch !== epoch;
  }).length;
  var bands = {};
  Array.prototype.forEach.call(document.querySelectorAll(".map-svg .map-edge"), function (edge) {
    var band = edge.getAttribute("data-band");
    if (band) bands[band] = (bands[band] || 0) + 1;
  });
  return {
    layer: mapState.layer,
    epoch: epoch,
    listeners: mapState.listeners.length,
    staleListeners: stale,
    clearedListeners: mapState.clearedListeners,
    timers: mapState.timers.length,
    staleTimers: staleTimers,
    clearedTimers: mapState.clearedTimers,
    observers: mapState.observers.length,
    staleObservers: staleObservers,
    clearedObservers: mapState.clearedObservers,
    refits: mapState.refits,
    svgCount: document.querySelectorAll(".map-svg").length,
    nodes: document.querySelectorAll(".map-svg .map-node").length,
    edges: document.querySelectorAll(".map-svg .map-edge").length,
    bands: bands,
    picked: mapState.path ? String(mapState.path.from) + "->" + String(mapState.path.to) : "",
  };
}

if (typeof window !== "undefined") window.mapDebugCounts = mapDebugCounts;


function mapNodeKindLabel(kind) {
  for (var i = 0; i < MAP_NODE_KINDS.length; i += 1) {
    if (MAP_NODE_KINDS[i][0] === kind) return MAP_NODE_KINDS[i][1];
  }
  return kind || "未知";
}


function mapEdgeKindLabel(kind) {
  for (var i = 0; i < MAP_EDGE_KINDS.length; i += 1) {
    if (MAP_EDGE_KINDS[i][0] === kind) return MAP_EDGE_KINDS[i][1];
  }
  return kind || "未知";
}


/* 本次要画的子图：大图默认截断到度数前 30，其余保持原顺序（颜色/标签不因截断而变）。 */
function mapVisibleGraph(payload, showAll) {
  /* 拷贝一份再算坐标：布局会往节点上写 x/y/dx/dy，不能污染 RPC 回来的载荷
   * （「显示全部」要在同一份数据上重算，原位改会把上次的坐标带进去）。 */
  var nodes = ((payload && payload.nodes) || []).map(function (node) {
    return {
      id: String(node.id),
      label: String(node.label || node.id),
      kind: String(node.kind || "concept"),
      weight: Number(node.weight || 0),
      sources: (node.sources || []).slice(),
      /* 资料层节点自带的元数据（概念层没有就是 undefined，不影响画图）：
       * 详情面板靠它说清"这篇来源支撑了几个概念、正文在不在库里"。 */
      url: node.url || "",
      sourceType: node.source_type || "",
      year: node.year === null || node.year === undefined ? null : node.year,
      hasText: node.has_text === undefined ? null : !!node.has_text,
      conceptCount: node.concept_count === undefined ? null : Number(node.concept_count),
      missing: !!node.missing,
    };
  });
  var edges = ((payload && payload.edges) || []).slice();
  var total = nodes.length;
  if (showAll || total <= MAP_TRUNCATE_OVER) {
    return { nodes: nodes, edges: edges, total: total, shown: nodes.length, truncated: false };
  }
  var ranked = nodes.slice().sort(function (a, b) {
    var diff = Number(b.weight || 0) - Number(a.weight || 0);
    if (diff) return diff;
    return String(a.label || a.id).localeCompare(String(b.label || b.id));
  });
  var keep = {};
  ranked.slice(0, MAP_PREVIEW_LIMIT).forEach(function (node) {
    keep[node.id] = true;
  });
  var picked = nodes.filter(function (node) {
    return keep[node.id];
  });
  var pickedEdges = edges.filter(function (edge) {
    return keep[edge.source] && keep[edge.target];
  });
  return {
    nodes: picked,
    edges: pickedEdges,
    total: total,
    shown: picked.length,
    truncated: true,
  };
}


/* 迭代次数按规模降：500 个点跑 180 次是几秒钟的白屏，没人愿意等。
 * 截断后一般只有 30 个点（180 次），"显示全部"才走大数分支 —— 那里形状糙一点也无所谓。 */
function mapIterations(count) {
  if (count <= 80) return 180;
  if (count <= 200) return 120;
  if (count <= 350) return 80;
  return 60;
}


function mapLayout(nodes, edges, width, height) {
  var count = nodes.length;
  var iterations = mapIterations(count);
  if (!count) return iterations;

  var cx = width / 2;
  var cy = height / 2;
  var index = {};
  /* 初始摆位用**确定的圆环**而不是随机撒点：同一张图每次打开形状一样（截图可对比，
   * 也不会出现"刷新一下全变了"），顺便天然没有重叠堆在原点。 */
  nodes.forEach(function (node, i) {
    var angle = (Math.PI * 2 * i) / count - Math.PI / 2;
    var ring = Math.min(width, height) * (0.24 + 0.05 * (i % 3));
    node.x = cx + Math.cos(angle) * ring;
    node.y = cy + Math.sin(angle) * ring;
    node.dx = 0;
    node.dy = 0;
    index[node.id] = node;
  });

  var k = Math.sqrt((width * height) / count) * 0.5;
  var maxStep = Math.min(width, height) / 9;

  for (var iter = 0; iter < iterations; iter += 1) {
    nodes.forEach(function (node) {
      node.dx = 0;
      node.dy = 0;
    });

    /* 斥力：所有点两两推开（O(n²)。500 点 × 60 次 = 1500 万次距离计算，
     * 实测一次性算完在一次点击的预算内；要上万点才需要 Barnes–Hut，这里不做）。 */
    for (var i = 0; i < count; i += 1) {
      var a = nodes[i];
      for (var j = i + 1; j < count; j += 1) {
        var b = nodes[j];
        var rx = a.x - b.x;
        var ry = a.y - b.y;
        var dist = Math.sqrt(rx * rx + ry * ry) || 0.01;
        var push = (k * k) / dist;
        var ux = rx / dist;
        var uy = ry / dist;
        a.dx += ux * push;
        a.dy += uy * push;
        b.dx -= ux * push;
        b.dy -= uy * push;
      }
    }

    /* 弹簧：有边相连的拉到理想边长 k 附近 */
    edges.forEach(function (edge) {
      var src = index[edge.source];
      var dst = index[edge.target];
      if (!src || !dst) return;
      var rx = src.x - dst.x;
      var ry = src.y - dst.y;
      var dist = Math.sqrt(rx * rx + ry * ry) || 0.01;
      var pull = (dist * dist) / k * 0.45;
      var ux = rx / dist;
      var uy = ry / dist;
      src.dx -= ux * pull;
      src.dy -= uy * pull;
      dst.dx += ux * pull;
      dst.dy += uy * pull;
    });

    /* 降温：前几步大步甩开，后面小步微调；向心力把整张图收在画布中间 */
    var temp = maxStep * (1 - iter / iterations) + 0.4;
    nodes.forEach(function (node) {
      node.dx += (cx - node.x) * 0.035;
      node.dy += (cy - node.y) * 0.035;
      var len = Math.sqrt(node.dx * node.dx + node.dy * node.dy) || 0.01;
      var step = Math.min(len, temp);
      node.x += (node.dx / len) * step * 0.85;
      node.y += (node.dy / len) * step * 0.85;
      node.x = Math.max(24, Math.min(width - 24, node.x));
      node.y = Math.max(18, Math.min(height - 18, node.y));
    });
  }

  /* 布局只算相对位置：把原始坐标存到 lx/ly，绝对尺寸交给 `mapFitNodes`。
   * 分开存是为了**容器变尺寸时能重缩**（`mapRefit` 拿 lx/ly 重新铺一次，
   * 而不是在已经缩过的坐标上再缩一次 —— 那会越缩越糊）。 */
  nodes.forEach(function (node) {
    node.lx = node.x;
    node.ly = node.y;
  });
  mapFitNodes(nodes, width, height);
  return iterations;
}


/* 量可用区：host 的**内容盒**像素（扣掉 padding）。量不到（页签未显示 / 高度 0）
 * 就退回 800x520 —— 由 ResizeObserver 在真显示出来时再重缩一次。 */
function mapMeasure(host) {
  var box = host && host.getBoundingClientRect ? host.getBoundingClientRect() : null;
  var width = box ? Math.round(box.width) : 0;
  var height = box ? Math.round(box.height) : 0;
  if (host && window.getComputedStyle) {
    var style = window.getComputedStyle(host);
    width -= Math.round((parseFloat(style.paddingLeft) || 0) + (parseFloat(style.paddingRight) || 0));
    height -= Math.round((parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0));
  }
  if (width < 120 || height < 120) {
    return { width: MAP_VIEW_WIDTH, height: MAP_VIEW_HEIGHT, measured: false };
  }
  return { width: width, height: height, measured: true };
}


/* 把布局坐标铺满可用区。**两轴各自缩放**而不是等比：侧栏是窄高盒，等比缩的话
 * 横向撑满、纵向只用一半，包围盒占面积不到 40%（就是"缩在角落里"的量化版本）。
 * 只改坐标不改图元：节点圆 / 标签字号都是 px，所以不会被拉扁（只有边长会变）。 */
function mapFitNodes(nodes, width, height) {
  var list = nodes || [];
  if (!list.length) return { padX: 0, padY: 0, sx: 1, sy: 1, spanX: 0, spanY: 0 };
  var padX = Math.max(MAP_PAD_MIN_X, Math.min(MAP_PAD_MAX_X, width * MAP_PAD_RATIO));
  var padY = Math.max(MAP_PAD_MIN_Y, Math.min(MAP_PAD_MAX_Y, height * MAP_PAD_RATIO));
  var minX = Infinity;
  var maxX = -Infinity;
  var minY = Infinity;
  var maxY = -Infinity;
  list.forEach(function (node) {
    var x = node.lx === undefined ? node.x : node.lx;
    var y = node.ly === undefined ? node.y : node.ly;
    minX = Math.min(minX, x);
    maxX = Math.max(maxX, x);
    minY = Math.min(minY, y);
    maxY = Math.max(maxY, y);
  });
  var spanX = Math.max(1, maxX - minX);
  var spanY = Math.max(1, maxY - minY);
  var innerW = Math.max(1, width - padX * 2);
  var innerH = Math.max(1, height - padY * 2);
  var sx = innerW / spanX;
  var sy = innerH / spanY;
  list.forEach(function (node) {
    var x = node.lx === undefined ? node.x : node.lx;
    var y = node.ly === undefined ? node.y : node.ly;
    node.x = padX + (x - minX) * sx;
    node.y = padY + (y - minY) * sy;
  });
  return { padX: padX, padY: padY, sx: sx, sy: sy, spanX: spanX, spanY: spanY };
}


/* 当前画布尺寸（viewBox 为准，不是常量）：缩放/居中都按它算。 */
function mapViewSize(svg) {
  var raw = (svg && svg.getAttribute("viewBox")) || "";
  var parts = raw.split(/[\s,]+/).map(Number);
  if (parts.length === 4 && parts[2] > 0 && parts[3] > 0) {
    return { width: parts[2], height: parts[3] };
  }
  return { width: MAP_VIEW_WIDTH, height: MAP_VIEW_HEIGHT };
}



function mapSvgEl(tag, attrs) {
  var node = document.createElementNS(MAP_NS, tag);
  Object.keys(attrs || {}).forEach(function (key) {
    node.setAttribute(key, String(attrs[key]));
  });
  return node;
}


function mapNodeRadius(weight) {
  return 6 + Math.min(6, Number(weight || 0) * 1.1);
}


/* 标签贴边会有一半跑到画布外（资料层标题很长，"被切掉一半"就是这么来的）：
 * 按字数估半个标签宽，再把标签在画布内平移回来。估算而不是 measureText：
 * 标签还没挂进 DOM 时量不到宽度，而 px 字号下按"中文 11.5 / 西文 6.2"估足够。 */
function mapLabelHalfWidth(text) {
  var width = 0;
  var raw = String(text || "");
  for (var i = 0; i < raw.length; i += 1) {
    width += raw.charCodeAt(i) > 255 ? 11.5 : 6.2;
  }
  return width / 2;
}


function mapLabelShift(x, half, width) {
  var pad = 4;
  if (x - half < pad) return pad - (x - half);
  if (x + half > width - pad) return width - pad - (x + half);
  return 0;
}


/* 悬停：高亮该节点的**所有相邻边与邻居**，其余压暗（靠 svg 上的 is-focusing 统一切）。
 * 点击：右侧信息面板写字号级别的明细（label / kind / 权重 / 来源）。 */
function mapFocusNode(svg, nodeId) {
  var target = null;
  svg.querySelectorAll(".map-node").forEach(function (node) {
    node.classList.toggle("is-hot", node.getAttribute("data-id") === nodeId);
    if (node.classList.contains("is-hot")) target = node;
  });
  var near = {};
  svg.querySelectorAll(".map-edge").forEach(function (edge) {
    var hit = edge.getAttribute("data-source") === nodeId || edge.getAttribute("data-target") === nodeId;
    edge.classList.toggle("is-hot", hit);
    if (hit) {
      near[edge.getAttribute("data-source")] = true;
      near[edge.getAttribute("data-target")] = true;
    }
  });
  svg.querySelectorAll(".map-node").forEach(function (node) {
    var id = node.getAttribute("data-id");
    node.classList.toggle("is-near", id !== nodeId && !!near[id]);
  });
  svg.classList.toggle("is-focusing", !!target);
}


function mapClearFocus(svg) {
  svg.classList.remove("is-focusing");
  svg.querySelectorAll(".map-node").forEach(function (node) {
    node.classList.remove("is-hot");
    node.classList.remove("is-near");
  });
  svg.querySelectorAll(".map-edge").forEach(function (edge) {
    edge.classList.remove("is-hot");
  });
}


function mapWheel(event) {
  var svg = event.currentTarget;
  var stage = svg.querySelector(".map-stage");
  if (!stage) return;
  event.preventDefault();
  var record = svg.__mapView || { k: 1, x: 0, y: 0 };
  var box = svg.getBoundingClientRect();
  if (!box.width || !box.height) return;
  var canvas = mapViewSize(svg);
  /* 光标落在 viewBox 的哪个点，缩放后那个点还要在光标下（否则会"跑掉"） */
  var px = ((event.clientX - box.left) / box.width) * canvas.width;
  var py = ((event.clientY - box.top) / box.height) * canvas.height;
  var next = record.k * (event.deltaY < 0 ? 1.12 : 0.89);
  next = Math.max(0.4, Math.min(4, next));
  record.x = px - ((px - record.x) * next) / record.k;
  record.y = py - ((py - record.y) * next) / record.k;
  record.k = next;
  svg.__mapView = record;
  stage.setAttribute(
    "transform",
    "translate(" + record.x.toFixed(2) + " " + record.y.toFixed(2) + ") scale(" + record.k.toFixed(3) + ")",
  );
  svg.setAttribute("data-scale", record.k.toFixed(3));
}


/* 摘监听并从账本里划掉（拖拽结束用：不划掉的话账本越记越多，探针数出来的"监听数"就失真）。 */
function mapOff(target, type, handler) {
  if (!target || !handler) return;
  target.removeEventListener(type, handler);
  mapState.listeners = mapState.listeners.filter(function (item) {
    return !(item.target === target && item.type === type && item.handler === handler);
  });
}


function mapDragStart(event) {
  if (event.button !== 0) return;
  var svg = event.currentTarget;
  var stage = svg.querySelector(".map-stage");
  if (!stage) return;
  var record = svg.__mapView || { k: 1, x: 0, y: 0 };
  var box = svg.getBoundingClientRect();
  if (!box.width) return;
  var scale = mapViewSize(svg).width / box.width;
  var anchor = { x: event.clientX, y: event.clientY, vx: record.x, vy: record.y };

  function move(moveEvent) {
    record.x = anchor.vx + (moveEvent.clientX - anchor.x) * scale;
    record.y = anchor.vy + (moveEvent.clientY - anchor.y) * scale;
    svg.__mapView = record;
    stage.setAttribute(
      "transform",
      "translate(" + record.x.toFixed(2) + " " + record.y.toFixed(2) + ") scale(" + record.k.toFixed(3) + ")",
    );
  }

  function stop() {
    mapOff(window, "mousemove", move);
    mapOff(window, "mouseup", stop);
  }

  mapOn(window, "mousemove", move);
  mapOn(window, "mouseup", stop);
}


/* 悬停提示：概念层说"几条连接"，资料层说"支撑几个概念 / 正文在不在库里"。 */
function mapNodeTooltipText(node) {
  var parts = [node.label + "（" + mapNodeKindLabel(node.kind) + "）"];
  if (node.kind === "source") {
    if (node.conceptCount !== null) parts.push("支撑 " + node.conceptCount + " 个概念");
    if (node.hasText === false) parts.push("正文未入库");
    if (node.missing) parts.push("库里已查不到这篇");
    if (node.year) parts.push(String(node.year));
  } else {
    parts.push(node.weight + " 条连接");
  }
  return parts.filter(Boolean).join(" · ");
}


/* 资料层：线宽三档（本图最大权重相对切 1/3、2/3）。 */
function mapEdgeBand(weight, maxWeight) {
  var top = Number(maxWeight || 0);
  if (!top) return 1;
  var ratio = Number(weight || 0) / top;
  if (ratio > 0.66) return 3;
  if (ratio > 0.33) return 2;
  return 1;
}


function buildMapSvg(payload, showAll, layer, size) {
  var layerName = layer === "source" ? "source" : "concept";
  var canvas = size || { width: MAP_VIEW_WIDTH, height: MAP_VIEW_HEIGHT, measured: false };
  var view = mapVisibleGraph(payload, showAll);
  var iterations = mapLayout(view.nodes, view.edges, canvas.width, canvas.height);

  var maxWeight = 0;
  if (layerName === "source") {
    view.edges.forEach(function (edge) {
      maxWeight = Math.max(maxWeight, Number(edge.weight || 0));
    });
  }

  /* viewBox = 实测像素 ⇒ 与容器等比，`meet` 的缩放比恒为 1：既没有上下留白，
   * 也不再把 11px 的标签缩成 5px（以前固定坐标系就是这样"缩小小"的）。 */
  var svg = mapSvgEl("svg", {
    class: "map-svg",
    viewBox: "0 0 " + canvas.width + " " + canvas.height,
    preserveAspectRatio: "xMidYMid meet",
    role: "img",
    "aria-label": "知识地图：" + String((payload && payload.topic) || ""),
  });
  svg.setAttribute("data-view-w", String(canvas.width));
  svg.setAttribute("data-view-h", String(canvas.height));
  svg.setAttribute("data-node-count", String(view.nodes.length));
  svg.setAttribute("data-edge-count", String(view.edges.length));
  svg.setAttribute("data-iterations", String(iterations));
  svg.setAttribute("data-truncated", view.truncated ? "1" : "0");
  svg.setAttribute("data-layer", layerName);

  var stage = mapSvgEl("g", { class: "map-stage" });

  /* 先画边（压在点下面），再画点 —— 视觉上点永远盖住线的端点 */
  view.edges.forEach(function (edge) {
    var src = null;
    var dst = null;
    view.nodes.forEach(function (node) {
      if (node.id === edge.source) src = node;
      if (node.id === edge.target) dst = node;
    });
    if (!src || !dst) return;
    var band = layerName === "source" ? mapEdgeBand(edge.weight, maxWeight) : 0;
    var line = mapSvgEl("line", {
      class:
        "map-edge rel-" +
        String(edge.kind || "uses") +
        (band ? " w-band-" + band : ""),
      "data-kind": String(edge.kind || "uses"),
      "data-source": edge.source,
      "data-target": edge.target,
      x1: src.x.toFixed(1),
      y1: src.y.toFixed(1),
      x2: dst.x.toFixed(1),
      y2: dst.y.toFixed(1),
    });
    if (band) {
      /* 档位写在 `data-band` 上（探针读），宽度由 CSS 的 w-band-* 落地 ——
       * 不能只写 stroke-width 属性：CSS 的 `.map-svg .map-edge` 会盖掉表现属性。 */
      line.setAttribute("data-band", String(band));
      line.setAttribute("data-weight", String(Number(edge.weight || 0)));
      line.classList.add("w-band-" + band);
    }
    stage.appendChild(line);
  });

  view.nodes.forEach(function (node) {
    var group = mapSvgEl("g", {
      class: "map-node kind-" + String(node.kind || "concept"),
      "data-id": node.id,
      "data-kind": String(node.kind || "concept"),
      "data-weight": String(node.weight || 0),
      transform: "translate(" + node.x.toFixed(1) + " " + node.y.toFixed(1) + ")",
    });
    group.appendChild(mapSvgEl("circle", { class: "map-dot", r: mapNodeRadius(node.weight).toFixed(1) }));
    var text = short(node.label || node.id, 14);
    var label = mapSvgEl("text", { class: "map-label", x: 0, y: mapNodeRadius(node.weight) + 12 });
    label.textContent = text;
    /* 贴边的节点：把标签在画布内平移回来，别让半串字落到框外 */
    label.setAttribute("x", mapLabelShift(node.x, mapLabelHalfWidth(text), canvas.width).toFixed(1));
    group.appendChild(label);
    var tip = mapSvgEl("title", {});
    tip.textContent = mapNodeTooltipText(node);
    group.appendChild(tip);

    mapOn(group, "mouseenter", function () {
      mapFocusNode(svg, node.id);
    });
    mapOn(group, "mouseleave", function () {
      mapClearFocus(svg);
      var picked = svg.getAttribute("data-picked");
      if (picked) mapFocusNode(svg, picked);
    });
    mapOn(group, "click", function (event) {
      event.stopPropagation();
      svg.setAttribute("data-picked", node.id);
      mapFocusNode(svg, node.id);
      showMapNodeInfo(node);
    });
    stage.appendChild(group);
  });

  svg.appendChild(stage);
  mapOn(svg, "wheel", mapWheel, { passive: false });
  mapOn(svg, "mousedown", mapDragStart);
  svg.__mapView = { k: 1, x: 0, y: 0 };
  return { svg: svg, view: view, iterations: iterations, canvas: canvas };
}


/* 换尺寸后重铺：改 viewBox + 用 lx/ly 重缩 + 同步 DOM（节点 transform / 边端点）。
 * **不重建 DOM**：选中态、路径高亮、放大浮层里同一份图都还在，形状也不会跳。 */
function mapRefit(svg, view, host) {
  if (!svg || !view) return null;
  var size = mapMeasure(host);
  var old = mapViewSize(svg);
  if (old.width === size.width && old.height === size.height) return null;
  svg.setAttribute("viewBox", "0 0 " + size.width + " " + size.height);
  svg.setAttribute("data-view-w", String(size.width));
  svg.setAttribute("data-view-h", String(size.height));
  var fit = mapFitNodes(view.nodes, size.width, size.height);

  var byId = {};
  view.nodes.forEach(function (node) {
    byId[node.id] = node;
    var group = mapFindNodeEl(svg, node.id);
    if (!group) return;
    group.setAttribute("transform", "translate(" + node.x.toFixed(1) + " " + node.y.toFixed(1) + ")");
    var label = group.querySelector(".map-label");
    if (label) {
      label.setAttribute(
        "x",
        mapLabelShift(node.x, mapLabelHalfWidth(label.textContent || ""), size.width).toFixed(1),
      );
    }
  });
  Array.prototype.forEach.call(svg.querySelectorAll(".map-edge"), function (line) {
    var src = byId[line.getAttribute("data-source")];
    var dst = byId[line.getAttribute("data-target")];
    if (!src || !dst) return;
    line.setAttribute("x1", src.x.toFixed(1));
    line.setAttribute("y1", src.y.toFixed(1));
    line.setAttribute("x2", dst.x.toFixed(1));
    line.setAttribute("y2", dst.y.toFixed(1));
  });

  /* 旧的平移/缩放是按旧画布算的像素量，换了画布留着就会把图推出框：回到 1:1。 */
  svg.__mapView = { k: 1, x: 0, y: 0 };
  mapApplyView(svg, svg.__mapView);
  mapState.refits += 1;
  svg.setAttribute("data-refits", String(mapState.refits));
  return fit;
}


/* 尺寸监听：ResizeObserver 为主（侧栏拖宽 / 页签显示 / 搬进放大浮层都会触发），
 * window.resize 兜底。两者都进账本（observers / listeners），换层时一起清。 */
function mapWatchSize(svg, view, host) {
  if (!svg || !host) return;
  if (typeof ResizeObserver === "function") {
    var observer = new ResizeObserver(function () {
      mapLater(function () {
        mapRefit(svg, view, host);
      }, 0);
    });
    observer.observe(host);
    mapState.observers.push({ observer: observer, epoch: mapState.epoch });
  }
  mapOn(window, "resize", function () {
    mapLater(function () {
      mapRefit(svg, view, host);
    }, 120);
  });
}


/* 节点详情面板：**不是**一行纯文本 —— 每个来源是一个按钮，点了就走资料库那条现成通路
 * （`showSourceBySourceId` → `archive.list(source_id)` → `showSource(doc_id)` → `archive.get`
 *  → 摘要渲染进 `#source-detail`）。这里只负责"把哪个 source_id 交出去"，不另写一套打开逻辑。
 *
 * 概念节点的 `sources` 是 source_id 列表；资料层节点的 `sources` 是它自己（`[nid]`），
 * 所以两种节点共用同一个按钮与同一条通路。 */
function showMapNodeInfo(node) {
  var box = el("map-info");
  if (!box) return;
  box.innerHTML = "";
  box.hidden = false;

  var head = document.createElement("div");
  head.className = "map-info-head";
  head.textContent =
    (node.label || node.id) +
    "｜" +
    mapNodeKindLabel(node.kind) +
    "（" +
    String(node.kind || "") +
    "）｜" +
    String(node.weight || 0) +
    " 条连接";
  box.appendChild(head);

  if (node.kind === "source") {
    var meta = document.createElement("div");
    meta.className = "map-info-meta";
    var bits = [];
    if (node.conceptCount !== null) bits.push("支撑 " + node.conceptCount + " 个概念");
    bits.push(node.hasText === false ? "正文未入库（只能看元数据）" : "正文已入库");
    if (node.year) bits.push(String(node.year));
    if (node.missing) bits.push("库里已查不到这篇");
    meta.textContent = bits.join("｜");
    box.appendChild(meta);
  }

  var refs = [];
  (node.sources || []).forEach(function (ref) {
    var id = String(ref || "").trim();
    if (id && refs.indexOf(id) < 0) refs.push(id);
  });

  var label = document.createElement("div");
  label.className = "map-info-label";
  label.textContent = refs.length ? "关联来源（点一下就去看资料）：" : "关联来源：无出处";
  box.appendChild(label);
  if (!refs.length) return;

  var list = document.createElement("ul");
  list.className = "map-info-sources";
  refs.forEach(function (sourceId, index) {
    var item = document.createElement("li");
    var button = document.createElement("button");
    button.type = "button";
    button.className = "mini map-source-open";
    button.setAttribute("data-source-id", sourceId);
    button.textContent = "打开资料 " + (index + 1) + "（" + short(sourceId, 22) + "）";
    button.title = "在「资料库」里打开这条来源的摘要与原文入口";
    button.addEventListener("click", function () {
      openMapSource(sourceId);
    });
    item.appendChild(button);
    list.appendChild(item);
  });
  box.appendChild(list);

  /* 收起按钮：详情是浮层，会盖住画布右下角那块（那里可能正好有节点/标签）。
   * 让用户能主动收起来，比"只能点别的节点换内容"更省事。 */
  var close = document.createElement("button");
  close.type = "button";
  close.className = "map-info-close";
  close.title = "收起详情";
  close.setAttribute("aria-label", "收起详情");
  close.textContent = "×";
  close.addEventListener("click", function () {
    box.hidden = true;
  });
  box.appendChild(close);
}


/* 打开一条来源：切到「资料库」页签（走 app.js 已绑好的页签点击，不另写切换），
 * 再调 sources.js 现成的 `showSourceBySourceId` —— **这就是"现有通路"**。 */
function openMapSource(sourceId) {
  if (typeof showSourceBySourceId !== "function") return null;
  var tab = document.querySelector('.tab[data-tab="sources"]');
  if (tab) tab.click();
  return showSourceBySourceId(String(sourceId));
}


function renderMapLegend(payload) {
  var box = el("map-legend");
  if (!box) return;
  box.innerHTML = "";

  /* 图例是**固定一行**（高 20px，横向可滑）：两行会把画布挤掉二十几像素，
   * 挤出来的那点高度让整张图显得"缩在角落里"。标题带出来，才知道这行是图例。 */
  var title = document.createElement("span");
  title.className = "map-legend-title";
  title.textContent = "图例";
  box.appendChild(title);

  /* 资料层：节点只有"来源"一种，边只有"共同支撑"一种 —— 图例要说清**线宽三档**的含义，
   * 否则用户只能看出线粗线细却不知道粗细代表什么。 */
  if (mapState.layer === "source") {
    var note = document.createElement("span");
    note.className = "map-legend-note";
    note.textContent = "资料层：点=来源，线=共同支撑概念数（线宽三档）";
    box.appendChild(note);
    [1, 2, 3].forEach(function (band) {
      var item = document.createElement("span");
      item.className = "map-legend-edge";
      item.setAttribute("data-band", String(band));
      var swatch = document.createElement("i");
      swatch.className = "map-line band-" + band;
      item.appendChild(swatch);
      /* 档名只写一个字：一行 20px 的图例放不下"少（第 1 档）"这种三倍长度的写法 */
      item.appendChild(document.createTextNode(["少", "中", "多"][band - 1]));
      box.appendChild(item);
    });
    return;
  }

  var counts = (payload && payload.counts && payload.counts.kinds) || {};
  MAP_NODE_KINDS.forEach(function (pair) {
    var kind = pair[0];
    if (!counts[kind]) return;
    var item = document.createElement("span");
    item.className = "map-legend-item";
    item.setAttribute("data-kind", kind);
    var swatch = document.createElement("i");
    swatch.className = "map-swatch kind-" + kind;
    item.appendChild(swatch);
    item.appendChild(document.createTextNode(pair[1] + " " + counts[kind]));
    box.appendChild(item);
  });

  /* 边线型也上一条图例（只列这张图真出现过的 rel） */
  var seen = {};
  ((payload && payload.edges) || []).forEach(function (edge) {
    seen[String(edge.kind || "uses")] = true;
  });
  MAP_EDGE_KINDS.forEach(function (pair) {
    if (!seen[pair[0]]) return;
    var item = document.createElement("span");
    item.className = "map-legend-edge";
    item.setAttribute("data-kind", pair[0]);
    var swatch = document.createElement("i");
    swatch.className = "map-line rel-" + pair[0];
    item.appendChild(swatch);
    item.appendChild(document.createTextNode(pair[1]));
    box.appendChild(item);
  });
}


/* 画布里的节点详情是**浮层**（CSS 里 absolute 贴在画布右下角，不在文档流里）：
 * 换图时不能连它一起清掉 —— 清了等于「刚点开详情就被抹掉」，而且之后 `el("map-info")`
 * 永远拿不到它（详情再也弹不出来）。所以清场统一走这里：只留 `#map-info`，
 * 万一它已经不在了就补一个回来（弹不出来的静默失败最难查）。 */
function mapKeepInfo(host) {
  if (!host) return null;
  var info = el("map-info");
  Array.prototype.slice.call(host.childNodes).forEach(function (child) {
    if (info && child === info) return;
    host.removeChild(child);
  });
  if (info) {
    if (host.firstChild !== info) host.appendChild(info);
    return info;
  }
  var box = document.createElement("div");
  box.className = "hint map-info";
  box.id = "map-info";
  box.hidden = true;
  host.appendChild(box);
  return box;
}


function renderMapEmpty(host, note) {
  if (!host) return;
  mapKeepInfo(host);
  var box = document.createElement("div");
  box.className = "map-empty";
  var text = document.createElement("p");
  text.textContent = note || "这个主题还没有知识地图，先在会话里让它建一张";
  box.appendChild(text);
  var button = document.createElement("button");
  button.type = "button";
  button.className = "ghost";
  button.id = "btn-map-build";
  button.textContent = "让它建一张";
  button.addEventListener("click", function () {
    /* 与快捷区「生成知识地图」同一条路：直接把话填进输入框（发不发由用户决定） */
    if (typeof askArtifact === "function") askArtifact("map");
  });
  box.appendChild(button);
  host.appendChild(box);
}


/* --------------------------------------------------------------- 图内搜索定位 */

function mapActiveSvg() {
  return document.querySelector("#map-body svg.map-svg");
}


function mapFindNodeEl(svg, nodeId) {
  if (!svg) return null;
  var found = null;
  Array.prototype.forEach.call(svg.querySelectorAll(".map-node"), function (node) {
    if (found) return;
    if (node.getAttribute("data-id") === String(nodeId)) found = node;
  });
  return found;
}


function mapNodeTranslate(node) {
  var match = /translate\(\s*(-?[\d.]+)[ ,]+(-?[\d.]+)\s*\)/.exec(node.getAttribute("transform") || "");
  if (!match) return { x: 0, y: 0 };
  return { x: parseFloat(match[1]), y: parseFloat(match[2]) };
}


function mapApplyView(svg, record) {
  var stage = svg.querySelector(".map-stage");
  if (!stage) return;
  stage.setAttribute(
    "transform",
    "translate(" + record.x.toFixed(2) + " " + record.y.toFixed(2) + ") scale(" + record.k.toFixed(3) + ")",
  );
  svg.setAttribute("data-scale", record.k.toFixed(3));
}


/* 把节点移到 viewBox 正中。节点在 `.map-stage` 里是 translate(nx ny)，
 * 屏幕坐标 = 局部坐标 × k + 平移 ⇒ 解 平移 = 半宽 − 局部 × k 就正好居中。
 * 一个像素都不靠"滚到可视区"这种碰运气的做法（侧栏与浮层是同一个 viewBox）。 */
function mapCenterNode(svg, nodeId) {
  var group = mapFindNodeEl(svg, nodeId);
  var stage = svg && svg.querySelector(".map-stage");
  if (!group || !stage) return false;
  var record = svg.__mapView || { k: 1, x: 0, y: 0 };
  var local = mapNodeTranslate(group);
  var canvas = mapViewSize(svg);
  record.x = canvas.width / 2 - local.x * record.k;
  record.y = canvas.height / 2 - local.y * record.k;
  svg.__mapView = record;
  mapApplyView(svg, record);
  svg.setAttribute("data-centered", String(nodeId));
  return true;
}


function mapMarkFoundNode(svg, group) {
  Array.prototype.forEach.call(svg.querySelectorAll(".map-node.is-found"), function (node) {
    node.classList.remove("is-found");
  });
  if (group) group.classList.add("is-found");
}


function mapHideSearchResults() {
  var box = el("map-search-results");
  if (!box) return;
  box.hidden = true;
  box.innerHTML = "";
}


function mapRenderSearchResults(items, note) {
  var box = el("map-search-results");
  if (!box) return;
  box.innerHTML = "";
  if (!items.length) {
    var empty = document.createElement("li");
    empty.className = "map-search-empty";
    empty.textContent = note || "没有匹配的节点";
    box.appendChild(empty);
    box.hidden = false;
    return;
  }
  items.forEach(function (item) {
    var li = document.createElement("li");
    var button = document.createElement("button");
    button.type = "button";
    button.className = "map-search-hit";
    button.setAttribute("data-id", String(item.id));
    button.setAttribute("data-kind", String(item.kind || ""));
    button.setAttribute("data-weight", String(item.weight || 0));
    button.textContent = String(item.label || item.id);
    var meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = "　" + mapNodeKindLabel(item.kind) + " · " + String(item.weight || 0) + " 连接";
    button.appendChild(meta);
    button.addEventListener("click", function () {
      mapLocateNode(String(item.id));
    });
    li.appendChild(button);
    box.appendChild(li);
  });
  box.hidden = false;
}


/* 搜索：走后端 `topic.search_nodes`（与图同一层，`layer` 一起带过去）。 */
async function mapRunSearch() {
  var input = el("map-search");
  if (!input) return;
  var query = String(input.value || "").trim();
  if (!query) {
    mapHideSearchResults();
    return;
  }
  if (!mapState.topicId || !mapState.payload) return;
  var epoch = mapState.epoch;
  var payload = null;
  try {
    payload = await scout.call("topic.search_nodes", {
      topic_id: mapState.topicId,
      q: query,
      layer: mapState.layer,
      limit: 8,
    });
  } catch (error) {
    mapRenderSearchResults([], "搜索失败：" + ((error && error.message) || String(error)));
    return;
  }
  /* 搜索期间换了图层：这批结果属于上一张图，丢掉（否则定位到不存在的节点）。 */
  if (epoch !== mapState.epoch) return;
  mapRenderSearchResults((payload && payload.nodes) || []);
}


/* 选中一条搜索结果：高亮 + 居中。节点被截断（不在前 30 个）时说明白，不假装定位成功。 */
function mapLocateNode(nodeId) {
  var svg = mapActiveSvg();
  mapHideSearchResults();
  if (!svg) return false;
  var group = mapFindNodeEl(svg, nodeId);
  if (!group) {
    var shown = mapState.view ? mapState.view.shown : 0;
    mapShowMapNote("「" + mapLabelOf(nodeId) + "」不在当前显示的 " + shown + " 个节点里：先点「显示全部」，再定位。", "warn");
    return false;
  }
  svg.setAttribute("data-picked", nodeId);
  mapFocusNode(svg, nodeId);
  mapMarkFoundNode(svg, group);
  mapCenterNode(svg, nodeId);
  return true;
}


/* --------------------------------------------------------------- 路径查询 */

function mapLabelOf(nodeId) {
  var nodes = (mapState.view && mapState.view.nodes) || [];
  for (var i = 0; i < nodes.length; i += 1) {
    if (nodes[i].id === String(nodeId)) return nodes[i].label;
  }
  return String(nodeId);
}


function mapShowMapNote(text, kind) {
  var note = el("map-path-note");
  if (!note) return;
  note.textContent = text;
  note.hidden = !text;
  note.setAttribute("data-kind", String(kind || "info"));
}


function mapClearPathMarks(svg) {
  if (!svg) return;
  svg.classList.remove("is-pathing");
  Array.prototype.forEach.call(svg.querySelectorAll(".map-node.is-path"), function (node) {
    node.classList.remove("is-path");
  });
  Array.prototype.forEach.call(svg.querySelectorAll(".map-edge.is-path"), function (edge) {
    edge.classList.remove("is-path");
  });
}


function mapHighlightPath(svg, path) {
  mapClearPathMarks(svg);
  if (!svg || !path) return 0;
  var wanted = {};
  (path.nodes || []).forEach(function (id) {
    wanted[String(id)] = true;
  });
  var marked = 0;
  Array.prototype.forEach.call(svg.querySelectorAll(".map-node"), function (node) {
    if (wanted[node.getAttribute("data-id")]) {
      node.classList.add("is-path");
      marked += 1;
    }
  });
  (path.edges || []).forEach(function (edge) {
    Array.prototype.forEach.call(svg.querySelectorAll(".map-edge"), function (line) {
      var a = line.getAttribute("data-source");
      var b = line.getAttribute("data-target");
      /* 后端保留存储方向，前端两个方向都认（无向连通是 `find_path` 的口径） */
      if ((a === edge.src && b === edge.dst) || (a === edge.dst && b === edge.src)) {
        line.classList.add("is-path");
      }
    });
  });
  svg.classList.add("is-pathing");
  return marked;
}


/* 两个下拉框：填当前**显示中**的节点（截断到 30 个时，选不到的节点也查不出路径来）。 */
function mapRefreshPathSelects(view) {
  var from = el("map-path-from");
  var to = el("map-path-to");
  if (!from || !to) return;
  var keepFrom = from.value;
  var keepTo = to.value;
  from.innerHTML = "";
  to.innerHTML = "";
  var nodes = ((view && view.nodes) || []).slice().sort(function (a, b) {
    return String(a.label || a.id).localeCompare(String(b.label || b.id));
  });
  nodes.forEach(function (node) {
    [from, to].forEach(function (select) {
      var option = document.createElement("option");
      option.value = node.id;
      option.textContent = short(node.label || node.id, 16);
      select.appendChild(option);
    });
  });
  var ids = nodes.map(function (node) {
    return node.id;
  });
  if (keepFrom && ids.indexOf(keepFrom) >= 0) from.value = keepFrom;
  if (keepTo && ids.indexOf(keepTo) >= 0) to.value = keepTo;
  else if (nodes.length > 1) to.value = nodes[1].id;
}


/* 路径查询：`topic.find_path` → 高亮路径 + 列出经过的边类型；查不到给人话。 */
async function mapRunPath() {
  var from = el("map-path-from");
  var to = el("map-path-to");
  if (!from || !to) return;
  if (!mapState.topicId) return;
  if (!from.value || !to.value) {
    mapShowMapNote("先选起点和终点两个节点。", "warn");
    return;
  }
  if (from.value === to.value) {
    mapShowMapNote("起点和终点是同一个节点：换个终点。", "warn");
    return;
  }
  var epoch = mapState.epoch;
  var payload = null;
  try {
    payload = await scout.call("topic.find_path", {
      topic_id: mapState.topicId,
      from: from.value,
      to: to.value,
      layer: mapState.layer,
      max_depth: 4,
    });
  } catch (error) {
    mapShowMapNote("查路径失败：" + ((error && error.message) || String(error)), "warn");
    return;
  }
  if (epoch !== mapState.epoch) return;
  var svg = mapActiveSvg();
  var paths = (payload && payload.paths) || [];
  var head = "「" + mapLabelOf(from.value) + "」→「" + mapLabelOf(to.value) + "」";

  if (!paths.length) {
    mapClearPathMarks(svg);
    mapState.path = null;
    mapShowMapNote(
      head + "：在 " + String((payload && payload.max_depth) || 4) + " 跳内没有连起来（换一对节点，或切到资料图看看）。",
      "warn",
    );
    return;
  }

  var path = paths[0];
  var marked = mapHighlightPath(svg, path);
  mapState.path = { from: from.value, to: to.value, edges: path.edges || [] };

  var kinds = [];
  (path.edges || []).forEach(function (edge) {
    /* 路径里的边用 `rel` 键（`knowledge_graph._path_edge`），图上画出来的边用 `kind` ——
     * 两个都认，缺一个就会把关系名显示成"未知"。 */
    var kind = String(edge.rel || edge.kind || "");
    var text =
      mapEdgeKindLabel(kind) +
      "（" + kind + (edge.weight === undefined ? "" : "·" + String(edge.weight)) + "）";
    if (kinds.indexOf(text) < 0) kinds.push(text);
  });
  var chain = (path.nodes || []).map(mapLabelOf).join(" → ");
  var note =
    head + "：最短 " + String(Math.max(0, (path.nodes || []).length - 1)) + " 跳｜" + chain +
    "｜边类型：" + (kinds.join("、") || "无");
  if (!marked) {
    note += "｜路径上的节点不在当前显示的前 30 个里，点「显示全部」再查一次";
  }
  mapShowMapNote(note, marked ? "ok" : "warn");
}


/* --------------------------------------------------------------- 图层切换 */

function mapUpdateLayerButtons() {
  Array.prototype.forEach.call(document.querySelectorAll(".map-layer"), function (button) {
    var on = button.getAttribute("data-layer") === mapState.layer;
    button.classList.toggle("is-active", on);
    button.setAttribute("aria-pressed", on ? "true" : "false");
  });
}


/* 概念图 ⇄ 资料图。两层载荷各留一份（切回来不重取、不重算），每次重画都先清理旧图。 */
async function mapSwitchLayer(layer) {
  var next = layer === "source" ? "source" : "concept";
  if (!mapState.topicId) return;
  if (next === mapState.layer && mapState.payload) return;
  var host = el("map-body");
  var payload = mapState.layers[next];
  if (!payload) {
    try {
      payload =
        next === "source"
          ? await scout.call("topic.get_source_graph", { topic_id: mapState.topicId, min_shared: 2 })
          : await scout.call("topic.get_map", { topic_id: mapState.topicId });
    } catch (error) {
      renderMapEmpty(host, "切不过去（" + next + "）：" + ((error && error.message) || String(error)));
      return;
    }
    mapState.layers[next] = payload;
  }
  mapState.layer = next;
  mapState.payload = payload;
  mapState.showAll = false;
  mapState.path = null;
  if (!payload || !(payload.nodes || []).length) {
    mapState.view = null;
    mapTeardown();
    mapUpdateLayerButtons();
    renderMapEmpty(
      host,
      next === "source"
        ? "资料层是空的：这张图里的来源还没有正文/摘要，导不出来源之间的共同支撑"
        : "这张知识地图还是空的，先在会话里重新建一张",
    );
    return;
  }
  renderMapFigure(host, payload);
}


function renderMapFigure(host, payload) {
  if (!host) return;
  /* 顺序要紧：先按账本把上一张图的监听/定时器删干净，再换代，最后才建新图。 */
  mapTeardown();
  mapState.epoch += 1;

  /* 画布按**容器实测像素**建（量不到就 800x520 兜底，随后 mapWatchSize 会纠正）。 */
  var canvas = mapMeasure(host);
  var built = buildMapSvg(payload, mapState.showAll, mapState.layer, canvas);
  mapState.view = built.view;
  mapKeepInfo(host);
  host.appendChild(built.svg);
  /* 挂尺寸监听：侧栏拖宽 / 页签重新显示 / 搬进放大浮层都按新尺寸重铺 */
  mapWatchSize(built.svg, built.view, host);
  renderMapLegend(payload);
  mapUpdateLayerButtons();
  mapRefreshPathSelects(built.view);
  mapShowMapNote("", "info");

  var info = el("map-info");
  if (info) {
    info.hidden = true;
    info.textContent = "";
  }

  var count = el("map-count");
  if (count) {
    var prefix = mapState.layer === "source" ? "资料层 · " : "";
    count.textContent = built.view.truncated
      ? prefix + "共 " + built.view.total + " 个节点，显示前 " + built.view.shown + "（按连接数排序）"
      : prefix + "共 " + built.view.shown + " 个节点、" + built.view.edges.length + " 条边";
  }

  var all = el("btn-map-all");
  if (all) {
    all.hidden = !built.view.truncated;
    all.textContent = "显示全部（" + built.view.total + "）";
  }
}


async function loadMapTab() {
  var host = el("map-body");
  if (!host) return;
  var items = [];
  try {
    var listed = await scout.call("topic.list", { limit: 50 });
    items = (listed && listed.items) || [];
  } catch (error) {
    renderMapEmpty(host, "读不到知识地图：" + error.message);
    return;
  }
  var mine = items.filter(function (item) {
    return String(item.session_id || "") === String(state.sessionId || "");
  });
  if (!mine.length) {
    mapState.topicId = "";
    mapState.payload = null;
    mapState.view = null;
    mapState.layers = { concept: null, source: null };
    mapState.showAll = false;
    mapState.path = null;
    mapTeardown();
    var legend = el("map-legend");
    if (legend) legend.innerHTML = "";
    var counter = el("map-count");
    if (counter) counter.textContent = "";
    var all = el("btn-map-all");
    if (all) all.hidden = true;
    mapHideSearchResults();
    mapShowMapNote("", "info");
    renderMapEmpty(host);
    return;
  }

  var topicId = String(mine[0].topic_id || "");
  var payload = null;
  try {
    payload = await scout.call("topic.get_map", { topic_id: topicId });
  } catch (error) {
    renderMapEmpty(host, "这张知识地图打不开：" + error.message);
    return;
  }
  if (mapState.topicId !== topicId) {
    mapState.showAll = false;
    /* 换了主题：两层的缓存都属于上一张图，必须一起丢掉（否则资料层还是旧图）。 */
    mapState.layers = { concept: null, source: null };
    mapState.layer = "concept";
    mapState.path = null;
  }
  mapState.topicId = topicId;
  mapState.payload = payload;
  mapState.layers.concept = payload;
  if (!payload || !(payload.nodes || []).length) {
    mapTeardown();
    renderMapEmpty(host, "这张知识地图还是空的（建图时没取到任何节点），先在会话里重新建一张");
    return;
  }
  renderMapFigure(host, payload);
}


function wireMapTab() {
  var all = el("btn-map-all");
  if (all) {
    all.addEventListener("click", function () {
      if (!mapState.payload) return;
      mapState.showAll = true;
      renderMapFigure(el("map-body"), mapState.payload);
    });
  }

  /* 图层开关：概念图 ⇄ 资料图（资料图走 `topic.get_source_graph`，线宽按权重分三档） */
  Array.prototype.forEach.call(document.querySelectorAll(".map-layer"), function (button) {
    button.addEventListener("click", function () {
      mapSwitchLayer(button.getAttribute("data-layer"));
    });
  });
  mapUpdateLayerButtons();

  /* 图内搜索：输入框防抖 220ms（定时器记账，换层会清掉），回车/按钮立即搜 */
  var searchInput = el("map-search");
  if (searchInput) {
    searchInput.addEventListener("input", function () {
      mapLater(mapRunSearch, 220);
    });
    searchInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        mapRunSearch();
      } else if (event.key === "Escape") {
        mapHideSearchResults();
      }
    });
  }
  var searchBtn = el("btn-map-search");
  if (searchBtn) {
    searchBtn.addEventListener("click", function () {
      mapRunSearch();
    });
  }

  var pathBtn = el("btn-map-path");
  if (pathBtn) {
    pathBtn.addEventListener("click", function () {
      mapRunPath();
    });
  }

  /* 放大：把**已经画好的那一块**整个搬进浮层，关闭时搬回原位（照学习路线那套）。
   * 不重取数、不重算布局 —— 浮层里和侧栏里是同一张图，只是坐标盒子变大了。 */
  var zoom = el("btn-map-zoom");
  if (zoom) {
    zoom.addEventListener("click", function () {
      var overlay = el("map-zoom");
      var body = el("map-zoom-body");
      var figure = el("map-body");
      if (!overlay || !body || !figure) return;
      figure.dataset.homeIndex = String(
        Array.prototype.indexOf.call(figure.parentNode.children, figure),
      );
      body.appendChild(figure);
      overlay.hidden = false;
    });
  }

  var closer = el("btn-map-zoom-close");
  if (closer) {
    closer.addEventListener("click", function () {
      var overlay = el("map-zoom");
      var pane = el("pane-map");
      var figure = el("map-body");
      if (overlay) overlay.hidden = true;
      if (!pane || !figure) return;
      var index = Number(figure.dataset.homeIndex || 0);
      var siblings = Array.prototype.slice.call(pane.children).filter(function (node) {
        return node !== figure;
      });
      var before = siblings[index] || null;
      if (before && before.nextSibling) pane.insertBefore(figure, before.nextSibling);
      else pane.appendChild(figure);
    });
  }
}


if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wireMapTab);
else wireMapTab();
