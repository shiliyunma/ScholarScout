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

/* 单一视图：**概念图**（落库的那一张）。资料不再是另一张图 —— 点节点看它关联的
 * 资料、点资料直接打开（`showMapNodeInfo` → `openMapSource`），一屏里闭环。
 * 后端的 `topic.get_source_graph` / `topic.find_path` 仍在（RPC 契约没动），界面不再调。
 */

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

/* 节点热区：圆点只有 7~9px，而标签是 `pointer-events:none`（不挡住下面的线），
 * 所以"点标签"这件事以前**根本不成立** —— 只有那个小圆点能点中。
 * 每个节点补一块透明热区：圆点外扩 10px 的圆 + 一条盖住标签的矩形（高 34px，
 * 两端各外扩 4px），两者都 `pointer-events: all`，点文字与点圆点走同一个 click。 */
var MAP_HIT_DOT_PAD = 10;
var MAP_HIT_PAD_X = 8;
var MAP_HIT_HEIGHT = 34;

/* 拖拽阈值：按下后位移 **< 4px 一律不算拖拽**（一个像素都不写 transform）。
 * 以前是无条件立即平移：手抖 1px 图就动，click 落在移动后的坐标上 —— 点击被判成拖拽。 */
var MAP_DRAG_THRESHOLD = 4;

/* 状态：`payload` 是当前这张概念图的载荷。
 *
 * 还有一本**监听/定时器账本**（`listeners` / `timers`）。为什么要账本而不是"反正
 * innerHTML="" 就没了"：节点/边上的监听确实随 DOM 一起被回收，但
 *   · 拖拽往 `window` 上挂 mousemove/mouseup —— 不删就活到页面关闭；
 *   · 搜索防抖的 setTimeout —— 不清理就会在重画之后拿新图去定位旧节点；
 *   · `svg.__mapView` 把整棵旧 SVG 挂在闭包里。
 * 重画一律先 `mapTeardown()`：删监听、清定时器、断引用，再建新的。
 * 账本上每条都记 `epoch`（第几张图），探针靠它断言"没有上一张图的残留"。 */
var mapState = {
  topicId: "",
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


/* 定时器也记账：防抖搜索用，重画时必须清掉（否则会拿新图定位旧节点）。 */
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


/* 重画前的清理：删干净上一张图的监听与定时器，并断开旧 SVG 的引用。 */
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
  return {
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
    hits: document.querySelectorAll(".map-svg .map-hit").length,
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


/* 弱边（待验证）：后端给 `confidence:"weak"`，**老数据没有这个字段 → 按 strong 画实线**。
 * 只认明确写了 weak 的：缺省、null、"strong"、乱七八糟的值一律实线 ——
 * 宁可漏标一条待验证，也不把有证据的边画成虚线（那等于告诉用户"这条是猜的"）。 */
function mapEdgeIsWeak(edge) {
  return String((edge && edge.confidence) || "strong") === "weak";
}


/* 待验证条数：优先信后端 `stats.weak_edges` / `counts.weak_edges`（读不到也不算错），
 * 读不到就数**这张图真画出来的**边。截断显示时不采信 stats —— 它数的是全图，
 * 会和屏幕上的边数对不上（"8 条边其中 5 条待验证"却只看得到 3 条）。 */
function mapWeakEdgeCount(payload, edges, truncated) {
  if (!truncated) {
    var holders = [(payload && payload.stats) || {}, (payload && payload.counts) || {}];
    for (var i = 0; i < holders.length; i += 1) {
      var value = holders[i].weak_edges;
      if (typeof value === "number" && isFinite(value) && value >= 0) return value;
    }
  }
  var counted = 0;
  (edges || []).forEach(function (edge) {
    if (mapEdgeIsWeak(edge)) counted += 1;
  });
  return counted;
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
      /* 详情面板要的**可读来源**（后端 `topic.get_map` 的 `source_refs`）必须一起拷过来：
       * 这里是白名单拷贝，漏一个字段就是"面板退化成裸 id"——而且不报错、只是显示成
       * 「来源 src-xxx」，看起来像后端没给。 */
      source_refs: Array.isArray(node.source_refs) ? node.source_refs.slice() : [],
      dropped_sources: Number(node.dropped_sources || 0),
      /* 节点可能带的元数据（没有就是 undefined，不影响画图）：详情面板靠它
       * 说清"这个点是什么、正文在不在库里"。 */
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


/* 标签贴边会有一半跑到画布外（标题很长时"被切掉一半"就是这么来的）：
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


/* 热区几何：圆点外扩一圈 + 一条按标签实际宽度铺的矩形（**跟着标签的平移走**）。
 * 为什么矩形也要算 `mapLabelShift`：贴边节点的标签被挪回画布内，热区不跟着挪就
 * 又变成"点文字没反应"（挪动量与标签一致才盖得住）。
 * 返回的 shift 给标签的 `x` 用，两边共用同一份几何 → 永远不会错位。 */
function mapSyncHitShapes(group, node, text, canvasWidth) {
  var radius = mapNodeRadius(node.weight);
  var half = mapLabelHalfWidth(text);
  var shift = mapLabelShift(node.x, half, canvasWidth);
  var labelY = radius + 12;
  var hit = group.querySelector(".map-hit");
  if (hit) hit.setAttribute("r", (radius + MAP_HIT_DOT_PAD).toFixed(1));
  var rect = group.querySelector(".map-hit-rect");
  if (rect) {
    var width = half * 2 + MAP_HIT_PAD_X;
    rect.setAttribute("x", (shift - width / 2).toFixed(1));
    rect.setAttribute("y", (labelY - MAP_HIT_HEIGHT / 2).toFixed(1));
    rect.setAttribute("width", width.toFixed(1));
    rect.setAttribute("height", String(MAP_HIT_HEIGHT));
  }
  return { radius: radius, shift: shift, labelY: labelY };
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


/* 按下即记账，但**位移不足 `MAP_DRAG_THRESHOLD`（4px）就不算拖拽**：
 * 阈值以内一个像素都不写 transform，「点击」不会被当成「拖着图走了一下」。
 * 阈值由长度（不是单轴）判定 —— 斜着抖 3px+3px 也算点击。 */
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
  var dragging = false;

  function move(moveEvent) {
    var dx = moveEvent.clientX - anchor.x;
    var dy = moveEvent.clientY - anchor.y;
    if (!dragging) {
      if (Math.sqrt(dx * dx + dy * dy) < MAP_DRAG_THRESHOLD) return;
      dragging = true;
    }
    record.x = anchor.vx + dx * scale;
    record.y = anchor.vy + dy * scale;
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


/* 悬停提示：一句话说清这个点是什么、几条连接（原生 <title>，不额外挂 tooltip）。 */
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


function buildMapSvg(payload, showAll, size) {
  var canvas = size || { width: MAP_VIEW_WIDTH, height: MAP_VIEW_HEIGHT, measured: false };
  var view = mapVisibleGraph(payload, showAll);
  var iterations = mapLayout(view.nodes, view.edges, canvas.width, canvas.height);

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

  var stage = mapSvgEl("g", { class: "map-stage" });

  /* 先画边（压在点下面），再画点 —— 视觉上点永远盖住线的端点 */
  var weakEdges = 0;
  view.edges.forEach(function (edge) {
    var src = null;
    var dst = null;
    view.nodes.forEach(function (node) {
      if (node.id === edge.source) src = node;
      if (node.id === edge.target) dst = node;
    });
    if (!src || !dst) return;
    /* 弱边 = 待验证：加 `is-weak`（CSS 出虚线），线型 rel-* 照旧挂上 ——
     * `is-hot` 只管颜色/粗细，悬停高亮不因虚线而失效。 */
    var weak = mapEdgeIsWeak(edge);
    if (weak) weakEdges += 1;
    var line = mapSvgEl("line", {
      class: "map-edge rel-" + String(edge.kind || "uses") + (weak ? " is-weak" : ""),
      "data-kind": String(edge.kind || "uses"),
      "data-confidence": weak ? "weak" : "strong",
      "data-source": edge.source,
      "data-target": edge.target,
      x1: src.x.toFixed(1),
      y1: src.y.toFixed(1),
      x2: dst.x.toFixed(1),
      y2: dst.y.toFixed(1),
    });
    stage.appendChild(line);
  });
  svg.setAttribute("data-weak-edges", String(weakEdges));

  view.nodes.forEach(function (node) {
    var group = mapSvgEl("g", {
      class: "map-node kind-" + String(node.kind || "concept"),
      "data-id": node.id,
      "data-kind": String(node.kind || "concept"),
      "data-weight": String(node.weight || 0),
      transform: "translate(" + node.x.toFixed(1) + " " + node.y.toFixed(1) + ")",
    });
    /* 热区先挂（在圆点/文字下面）：圆点外扩 10px 的圆 + 盖住标签的矩形。
       两者都是 `fill="transparent"` + `pointer-events="all"`，点圆点与点文字
       落到同一个 group 的 click 上 —— 这是"点某些点不响应"的正面修法。 */
    group.appendChild(
      mapSvgEl("circle", { class: "map-hit", r: 1, fill: "transparent", "pointer-events": "all" }),
    );
    group.appendChild(
      mapSvgEl("rect", {
        class: "map-hit-rect",
        x: 0,
        y: 0,
        width: 1,
        height: MAP_HIT_HEIGHT,
        fill: "transparent",
        "pointer-events": "all",
      }),
    );
    group.appendChild(mapSvgEl("circle", { class: "map-dot", r: mapNodeRadius(node.weight).toFixed(1) }));
    var text = short(node.label || node.id, 14);
    var label = mapSvgEl("text", { class: "map-label", x: 0, y: mapNodeRadius(node.weight) + 12 });
    label.textContent = text;
    /* 贴边的节点：把标签在画布内平移回来，别让半串字落到框外。
     * 平移量与热区矩形共用一份几何（`mapSyncHitShapes`），所以文字永远在热区里。 */
    label.setAttribute("x", mapSyncHitShapes(group, node, text, canvas.width).shift.toFixed(1));
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
      /* 点击只做两件事：切 class（高亮邻居）+ 填**浮层**详情（absolute，不占文档流）。
       * **不许**在这里重排/重画 —— 一次点击重建一遍全图就是"点了很久才响应"的来源。 */
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
      /* 标签与热区矩形一起挪（同一个几何函数）：只挪标签不挪热区，字又点不中了。 */
      var geom = mapSyncHitShapes(group, node, label.textContent || "", size.width);
      label.setAttribute("x", geom.shift.toFixed(1));
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


/* 节点详情面板：**不是**一行纯文本 —— 每条关联资料一行，右边一个按钮，点了就走资料库那条
 * 现成通路（`showSourceBySourceId` → `archive.list(source_id)` → `showSource(doc_id)` →
 * `archive.get` → 摘要渲染进 `#source-detail`）。这里只负责"把哪个 source_id 交出去"。
 *
 * 三条口径（对应用户的三条反馈，改之前分别踩过）：
 *   ① 显示后端给的**资料真标题**（`source_refs[].title`，后端已做三级回退、不会为空）——
 *      以前拿 `node.sources` 的 id 编号成「打开资料 1/2/3」，用户看到的是一串「资料1 资料2」；
 *   ② 库里查不到的来源（`dropped_sources`）**不出按钮**：点了必然是空的，
 *      只在底部留一行灰字计数；
 *   ③ `has_text:false`（只有元数据）→ 按钮改成「获取正文」，点了立刻 `archive.download`，
 *      成了再打开；失败把原因写在**那一行下面**，不弹 alert。
 */
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

  var refs = mapInfoSourceRefs(node);
  var dropped = Math.max(0, parseInt(node.dropped_sources, 10) || 0);
  /* 一条都没有 → 一句话说清楚。**不出**空列表头：那看着像"还在加载"。 */
  if (!refs.length && !dropped) {
    var empty = document.createElement("p");
    empty.className = "map-info-empty";
    empty.textContent = "这个节点没有关联资料";
    box.appendChild(empty);
    box.appendChild(mapInfoCloseButton());
    return;
  }

  if (refs.length) {
    var label = document.createElement("div");
    label.className = "map-info-label";
    label.textContent = "关联资料 " + refs.length + " 条：";
    box.appendChild(label);
    var list = document.createElement("ul");
    list.className = "map-info-sources";
    refs.forEach(function (ref) {
      list.appendChild(mapInfoSourceRow(ref));
    });
    box.appendChild(list);
  }

  /* 查不到的那些**只计数、不出按钮**（用户第 5 条：要么不出现，要么点了立即查） */
  if (dropped > 0) {
    var miss = document.createElement("p");
    miss.className = "map-info-dropped";
    miss.textContent = "另有 " + dropped + " 条来源已不在库中（可能被删除或未入库）";
    box.appendChild(miss);
  }
  box.appendChild(mapInfoCloseButton());
}


/* 详情面板要显示的来源：优先 `source_refs`（真标题），老数据（没有 source_refs）退回
 * `sources` 的 id，显示成「来源 <id 前 12 位>」—— **同样不编号**。
 * 排序按标题（中文走拼音 `localeCompare("zh")`），同标题再按 id：每次打开顺序都一样，
 * 不会因为后端返回顺序变了而"按钮换了位置"。 */
function mapInfoSourceRefs(node) {
  var out = [];
  var seen = {};
  var raw = Array.isArray(node.source_refs) ? node.source_refs : [];
  raw.forEach(function (ref) {
    if (!ref) return;
    var id = String(ref.id || "").trim();
    if (!id || seen[id]) return;
    seen[id] = true;
    out.push({
      id: id,
      title: String(ref.title || "").trim() || "来源 " + short(id, 12),
      year: ref.year || null,
      /* 三种值都要保住：true（打开）/ false（获取正文）/ null（老数据不知道 → 按打开走） */
      has_text: ref.has_text === false ? false : ref.has_text === true ? true : null,
      domain: String(ref.domain || "").trim(),
    });
  });
  if (!raw.length) {
    (node.sources || []).forEach(function (sourceId) {
      var id = String(sourceId || "").trim();
      if (!id || seen[id]) return;
      seen[id] = true;
      out.push({
        id: id,
        title: "来源 " + short(id, 12),
        year: null,
        has_text: null,
        domain: "",
      });
    });
  }
  out.sort(function (a, b) {
    var byTitle = String(a.title).localeCompare(String(b.title), "zh");
    return byTitle !== 0 ? byTitle : String(a.id).localeCompare(String(b.id));
  });
  return out;
}


/* 一行资料：标题（有年份就带「（2024）」）+ 已入库/未入库标签 + 域名（小字），右边一个动作按钮。
 * 按钮的 `data-action` 是 open / fetch —— 探针按它挑按钮，不用猜第几个。 */
function mapInfoSourceRow(ref) {
  var item = document.createElement("li");
  item.className = "map-info-source";
  item.setAttribute("data-source-id", ref.id);

  var main = document.createElement("div");
  main.className = "map-info-source-main";

  var title = document.createElement("span");
  title.className = "map-info-source-title";
  title.textContent = ref.title + (ref.year ? "（" + ref.year + "）" : "");
  title.title = ref.domain ? ref.title + "｜" + ref.domain : ref.title;
  main.appendChild(title);

  var tag = document.createElement("span");
  var pending = ref.has_text === false;
  tag.className = "map-info-tag " + (pending ? "is-pending" : "is-ready");
  tag.textContent = pending ? "未入库" : "已入库";
  main.appendChild(tag);

  if (ref.domain) {
    var domain = document.createElement("span");
    domain.className = "map-info-domain";
    domain.textContent = ref.domain;
    main.appendChild(domain);
  }
  item.appendChild(main);

  var button = document.createElement("button");
  button.type = "button";
  button.className = "mini map-source-open";
  button.setAttribute("data-source-id", ref.id);
  button.setAttribute("data-action", pending ? "fetch" : "open");
  button.textContent = pending ? "获取正文" : "打开资料";
  button.title = pending
    ? "库里只有元数据：先用 archive.download 抓正文，成功后再打开"
    : "在「资料库」里打开这条来源的摘要与原文入口";
  button.addEventListener("click", function () {
    if (ref.has_text === false) {
      fetchMapSourceText(ref, item, button);
      return;
    }
    openMapSource(ref.id, ref);
  });
  item.appendChild(button);

  var note = document.createElement("p");
  note.className = "map-info-error";
  note.hidden = true;
  item.appendChild(note);
  return item;
}


/* 「获取正文」：库里只有元数据 → 直接 `archive.download`（后端默认**关**，回 ok:false + reason）。
 * 成功就顺着同一条通路打开；失败把结论写在这一行下面 —— **不弹 alert**（打断式弹窗把切页签、
 * 地图详情一起挡住，那正是"点了没反应"的观感来源之一）。 */
async function fetchMapSourceText(ref, row, button) {
  var note = row.querySelector(".map-info-error");
  if (!note) {
    note = document.createElement("p");
    note.className = "map-info-error";
    row.appendChild(note);
  }
  note.textContent = "";
  note.title = "";
  note.hidden = true;
  var original = button.textContent;
  button.disabled = true;
  button.textContent = "正在获取…";
  var res = null;
  try {
    res = await scout.call("archive.download", { source_id: ref.id });
  } catch (error) {
    res = { ok: false, reason: (error && error.message) || String(error) };
  }
  button.disabled = false;
  button.textContent = original;
  if (res && res.ok) {
    /* 抓到了：正文已落库，接着走打开通路（列表那边这时就能查到 doc_id） */
    mapSourceStatus("已获取正文：" + ref.title, false);
    return openMapSource(ref.id, ref);
  }
  mapInfoDownloadError(note, res || {});
  return false;
}


/* 下载失败的三类说法（判据来自 `rpc/archive.py::archive_download`，前端只分流、不改判据）：
 *   ① 功能未开启（默认配置 features.enable_download=false 就是这条）→ 说清去哪儿开 + 「去设置」；
 *   ② 大文件 → reason 原文 + 可在资料库里确认后重试；
 *   ③ 其它（网络 / 落盘 / 没记录源地址）→ reason 原文。 */
function mapInfoDownloadError(note, res) {
  var reason = String(res.reason || "下载失败（没有给出原因）");
  note.hidden = false;
  note.textContent = "";
  note.title = reason;
  if (res.disabled === true || /未开启|已关闭/.test(reason)) {
    note.appendChild(
      document.createTextNode("下载未开启：设置 → 功能 → 允许下载，打开后再点一次"),
    );
    var go = document.createElement("button");
    go.type = "button";
    go.className = "mini map-info-goto-settings";
    go.textContent = "去设置";
    go.title = "打开设置（按需下载由 features.enable_download 控制）";
    go.addEventListener("click", function () {
      if (typeof openSettings !== "function") {
        note.textContent = "这个版本没有设置面板，请直接改 config.yaml 的 features.enable_download";
        return;
      }
      Promise.resolve(openSettings()).catch(function (error) {
        note.textContent = "打不开设置：" + ((error && error.message) || error);
      });
    });
    note.appendChild(go);
    return;
  }
  note.textContent = res.large ? reason + "；文件较大，可在资料库里确认后重试" : reason;
}


/* 收起按钮：详情是浮层，会盖住画布右下角那块（那里可能正好有节点/标签）。
 * 让用户能主动收起来，比"只能点别的节点换内容"更省事。 */
function mapInfoCloseButton() {
  var close = document.createElement("button");
  close.type = "button";
  close.className = "map-info-close";
  close.title = "收起详情";
  close.setAttribute("aria-label", "收起详情");
  close.textContent = "×";
  close.addEventListener("click", function () {
    var box = el("map-info");
    if (box) box.hidden = true;
  });
  return close;
}


/* 写资料库那行下载/打开提示（`#source-download-status` 在详情区之外，重画详情不会被清掉）。 */
function mapSourceStatus(text, bad) {
  var status = el("source-download-status");
  if (!status) return null;
  status.textContent = text || "";
  status.hidden = !text;
  status.className = bad === true ? "hint set-bad" : "hint";
  return status;
}


/* ───────────────────────────────────────────── 资料浮层（#source-zoom）
 * 点「打开资料」**不切侧栏页签**（用户第 1 条：切页签等于把当前视图强行换掉），
 * 改成在应用内开一层只读详情。交互照地图「放大」那套（#map-zoom），但：
 *   · 内容层独立：DOM 是 index.html 里静态声明的 `#source-zoom`，不搬地图那张图；
 *   · **单例**：DOM 只有一份，再点别的资料只换内容（不叠第二个窗、也不另开一层）；
 *   · 可拖动（标题栏）+ 可缩放（右下角把手），**最小 420×320**（再小摘要就挤成一行）；
 *     尺寸记 localStorage，下次打开沿用；
 *   · 关闭三条路：Esc / 关闭按钮 / 点遮罩外 —— 关掉**不动地图的选中态**（一个字段都不碰）。
 */
var SOURCE_ZOOM_MIN_W = 420;
var SOURCE_ZOOM_MIN_H = 320;
var SOURCE_ZOOM_SIZE_KEY = "scholarscout.sourceZoom.size";

/* 浮层里现在是哪一条（换内容只改这里，**不重建 DOM**）。`seq` 用来丢掉过期的异步结果：
 * 连着点两条时，先发的那次 archive.get 晚回来，绝不能把 A 的正文画到 B 的标题下。 */
var sourceZoomState = {
  sourceId: "",
  ref: null,
  docId: "",
  doc: null,
  summary: null,
  paths: null,
  seq: 0,
  drag: null,
  resize: null,
};


function sourceZoomOverlay() {
  return el("source-zoom");
}


function sourceZoomSheet() {
  var overlay = sourceZoomOverlay();
  return overlay ? overlay.querySelector(".source-sheet") : null;
}


/* 上次拖出来的尺寸（localStorage）。读不到 / 坏了就回 null —— 那就用 CSS 的默认尺寸。 */
function sourceZoomStoredSize() {
  try {
    var raw = window.localStorage.getItem(SOURCE_ZOOM_SIZE_KEY);
    if (!raw) return null;
    var parsed = JSON.parse(raw);
    var w = Number(parsed && parsed.w);
    var h = Number(parsed && parsed.h);
    if (!isFinite(w) || !isFinite(h) || w <= 0 || h <= 0) return null;
    return { w: w, h: h };
  } catch (error) {
    return null;
  }
}


/* 夹到 [最小尺寸, 视口内]：420×320 是硬下限（拖多小都不许低于它，否则摘要看不清）。 */
function sourceZoomClamp(w, h) {
  var maxW = Math.max(SOURCE_ZOOM_MIN_W, window.innerWidth - 24);
  var maxH = Math.max(SOURCE_ZOOM_MIN_H, window.innerHeight - 24);
  return {
    w: Math.min(Math.max(Math.round(w || 0), SOURCE_ZOOM_MIN_W), maxW),
    h: Math.min(Math.max(Math.round(h || 0), SOURCE_ZOOM_MIN_H), maxH),
  };
}


function sourceZoomApplySize(w, h) {
  var sheet = sourceZoomSheet();
  if (!sheet) return null;
  var size = sourceZoomClamp(w, h);
  sheet.style.width = size.w + "px";
  sheet.style.height = size.h + "px";
  return size;
}


/* 存尺寸：隐私模式 / 配额满时 setItem 会抛 —— 记不住尺寸不算事，浮层照用。 */
function sourceZoomPersistSize() {
  var sheet = sourceZoomSheet();
  if (!sheet) return;
  var box = sheet.getBoundingClientRect();
  if (!box.width || !box.height) return;
  try {
    window.localStorage.setItem(
      SOURCE_ZOOM_SIZE_KEY,
      JSON.stringify({ w: Math.round(box.width), h: Math.round(box.height) }),
    );
  } catch (error) {
    /* 记不住就不记 —— 不许因为 localStorage 把「打开资料」弄失败 */
  }
}


/* 打开一条来源：**不切页签**（用户第 1 条：切侧栏会把当前视图强行换掉），
 * 在应用内开一层只读详情浮层。`ref` 是详情面板那一行（带真标题/年份/类型），
 * 只有 id 的调用方也能用 —— 标题回落到 `archive.get` 回来的真标题。 */
function openMapSource(sourceId, ref) {
  return openSourceZoom(String(sourceId), ref || null);
}


/* 打开（或复用）资料浮层：**无论点多少条，都是往同一个盒子里换内容**（单例）。 */
function openSourceZoom(sourceId, ref) {
  var overlay = sourceZoomOverlay();
  var body = el("source-zoom-body");
  var title = el("source-zoom-title");
  var meta = el("source-zoom-meta");
  if (!overlay || !body || !title || !meta) return null;
  var id = String(sourceId || "").trim();
  sourceZoomState.sourceId = id;
  sourceZoomState.ref = ref || null;
  sourceZoomState.docId = "";
  sourceZoomState.doc = null;
  sourceZoomState.summary = null;
  sourceZoomState.paths = null;
  sourceZoomState.seq += 1;

  /* 标题栏先给个能看的：ref 有真标题就用；只有 id 时先写「资料 <id 前 12 位>」，
     等 archive.get 回来换成真标题。主标题永远是标题，来源 id 只作后面的小字。 */
  title.textContent = (ref && ref.title) || "资料 " + short(id, 12);
  title.title = title.textContent;
  meta.textContent = [
    ref && ref.year ? String(ref.year) + " 年" : "",
    ref && ref.source_type ? sourceZoomKindLabel(ref.source_type) : "",
    id ? "id " + short(id, 18) : "",
  ]
    .filter(function (part) {
      return !!part;
    })
    .join("｜");

  body.innerHTML = "";
  sourceZoomStatus("正在读取这条资料的详情…", false);
  var stored = sourceZoomStoredSize();
  if (stored) sourceZoomApplySize(stored.w, stored.h);
  overlay.hidden = false;
  return loadSourceZoom(id, sourceZoomState.seq);
}


/* 来源类型 → 中文标签（后端只给 arxiv/github/official/web，旧数据还有 paper/blog 之类）。 */
function sourceZoomKindLabel(kind) {
  var key = String(kind || "")
    .trim()
    .toLowerCase();
  var labels = {
    arxiv: "论文（arXiv）",
    paper: "论文",
    github: "代码库",
    official: "官方文档",
    web: "网页",
    blog: "博客",
    docs: "文档",
    video: "视频",
  };
  return labels[key] || key;
}


async function loadSourceZoom(sourceId, seq) {
  var payload = null;
  try {
    var listed = await scout.call("archive.list", { source_id: sourceId, limit: 1 });
    var hit = ((listed && listed.items) || [])[0];
    if (hit && hit.doc_id) {
      payload = await scout.call("archive.get", { doc_id: hit.doc_id });
      sourceZoomState.docId = String(hit.doc_id);
    }
  } catch (error) {
    if (seq !== sourceZoomState.seq) return false;
    sourceZoomState.docId = "";
    sourceZoomRender();
    sourceZoomStatus("打开资料失败：" + ((error && error.message) || error), true);
    return false;
  }
  /* 这期间用户点了别的资料：这次结果已过期，直接丢掉（不覆盖新的那一条） */
  if (seq !== sourceZoomState.seq) return false;
  if (!payload) {
    /* 库里没有这条（拿不到 doc_id）：只显示元数据 + 一行说明，并留「去原站」 */
    sourceZoomState.doc = null;
    sourceZoomState.summary = null;
    sourceZoomRender();
    sourceZoomStatus("这条还没有正文，只有元数据", true);
    return false;
  }
  sourceZoomState.doc = payload.document || null;
  sourceZoomState.summary = (payload.summaries || [])[0] || null;
  sourceZoomState.paths = payload.paths || null;
  sourceZoomRender();
  return true;
}


/* 重画浮层内容：真标题 / 元数据（年份·类型·域名·来源 id）/ 摘要 / 「原文」「去原站」。
 * 只读 —— 不写库、不动地图状态；也**不新建第二个浮层**（正文就渲染在本层的 `.source-zoom-text`）。 */
function sourceZoomRender() {
  var body = el("source-zoom-body");
  var titleBox = el("source-zoom-title");
  var metaBox = el("source-zoom-meta");
  if (!body) return null;
  var ref = sourceZoomState.ref || {};
  var doc = sourceZoomState.doc || {};
  var first = sourceZoomState.summary || {};
  var id = sourceZoomState.sourceId;

  /* 真标题：摘要里的中译标题 → 库里的来源标题/文档标题 → ref（地图带上来的）→ 兜底 */
  var realTitle =
    String(first.title_zh || doc.source_title || doc.title || ref.title || "").trim() || "（无标题）";
  var year = doc.year || ref.year || "";
  var kind = sourceZoomKindLabel(doc.source_type || ref.source_type || "");
  var domain = String(doc.domain || ref.domain || "").trim();
  var url = String(doc.url || ref.url || "").trim();

  if (titleBox) {
    titleBox.textContent = realTitle;
    titleBox.title = realTitle;
  }
  if (metaBox) {
    metaBox.textContent = [
      year ? String(year) + " 年" : "",
      kind || "",
      domain,
      id ? "id " + short(id, 18) : "",
    ]
      .filter(function (part) {
        return !!part;
      })
      .join("｜");
  }

  body.innerHTML = "";
  var facts = document.createElement("div");
  facts.className = "source-zoom-facts";
  [
    ["年份", year ? String(year) + " 年" : ""],
    ["类型", kind ? String(kind) : ""],
    ["来源", domain ? String(domain) : ""],
    ["来源 id", id || ""],
  ].forEach(function (pair) {
    /* 不知道就**不写**：三个“未标注”并排，是占位符噪音，不是信息。来源 id 总会渲染。 */
    if (!pair[1]) return;
    var item = document.createElement("span");
    item.setAttribute("data-fact", pair[0]);
    var label = document.createElement("b");
    label.textContent = pair[0] + "：";
    item.appendChild(label);
    item.appendChild(document.createTextNode(pair[1]));
    facts.appendChild(item);
  });
  body.appendChild(facts);

  /* 摘要（只读，选不中文字也能滚——真正可滚的是外层 .source-zoom-body） */
  var summary = document.createElement("p");
  summary.className = "source-zoom-summary";
  var summaryText = String(first.summary_short || "").trim();
  if (summaryText) {
    summary.textContent = summaryText;
  } else {
    summary.textContent = "（这条资料还没出摘要）";
    summary.classList.add("is-empty");
  }
  body.appendChild(summary);

  /* 两个出口：「原文」走 archive.open_raw（无原件则把 archive.text 渲染在下面那块），
     「去原站」走系统浏览器（没有源地址时按钮禁用并在浮层里说明，绝不 alert） */
  var actions = document.createElement("div");
  actions.className = "actions source-zoom-actions";
  var rawBtn = document.createElement("button");
  rawBtn.type = "button";
  rawBtn.className = "ghost";
  rawBtn.id = "btn-source-zoom-raw";
  rawBtn.textContent = "原文";
  rawBtn.title = "有原件就用系统默认程序打开；没有原件则把全文显示在这里";
  if (!sourceZoomState.docId) {
    rawBtn.disabled = true;
    rawBtn.title = "这条还没有正文（只有元数据），没有可打开的原件";
  }
  rawBtn.addEventListener("click", function () {
    sourceZoomOpenRaw(rawBtn);
  });
  var siteBtn = document.createElement("button");
  siteBtn.type = "button";
  siteBtn.className = "ghost";
  siteBtn.id = "btn-source-zoom-origin";
  siteBtn.textContent = "去原站";
  siteBtn.title = "用系统浏览器打开这条资料的源网址";
  if (!url) {
    siteBtn.disabled = true;
    siteBtn.title = "这条资料没有记录源地址";
  }
  siteBtn.addEventListener("click", function () {
    sourceZoomOpenOrigin();
  });
  actions.appendChild(rawBtn);
  actions.appendChild(siteBtn);
  body.appendChild(actions);

  if (!sourceZoomState.docId) {
    var onlyMeta = document.createElement("p");
    onlyMeta.className = "source-zoom-status is-bad";
    onlyMeta.textContent = "这条还没有正文，只有元数据";
    body.appendChild(onlyMeta);
  }

  /* 状态行（打开原件/读全文/失败都写这一行，不弹窗） */
  var status = document.createElement("p");
  status.className = "source-zoom-status source-zoom-status-main";
  status.hidden = true;
  body.appendChild(status);

  /* 全文落点：没有原件时 `archive.text` 的结果渲染在这里（本层可滚，不开第二个窗） */
  var holder = document.createElement("pre");
  holder.id = "source-zoom-text";
  holder.className = "source-zoom-text";
  holder.hidden = true;
  body.appendChild(holder);
  return body;
}


/* 浮层里那一行状态字（找不到就现造一行追加在最下面）。 */
function sourceZoomStatus(text, bad) {
  var body = el("source-zoom-body");
  if (!body) return null;
  var line = body.querySelector(".source-zoom-status-main");
  if (!line) {
    line = document.createElement("p");
    line.className = "source-zoom-status source-zoom-status-main";
    line.hidden = true;
    body.appendChild(line);
  }
  line.textContent = text || "";
  line.hidden = !text;
  line.classList.toggle("is-bad", bad === true);
  return line;
}


/* 「原文」：有原件 → `archive.open_raw`（与资料库「⋯ → 打开文件」同一条通路，系统默认程序打开）；
 * 没有原件但有全文 → `archive.text` 的结果渲染在本浮层的可滚区域（**不再开第二个窗**）；
 * 两样都没有 → 在浮层里说清楚（如实说，不弹 alert）。 */
async function sourceZoomOpenRaw(button) {
  var docId = sourceZoomState.docId;
  if (!docId) {
    sourceZoomStatus("这条还没有正文，只有元数据", true);
    return false;
  }
  var exists = (sourceZoomState.paths && sourceZoomState.paths.exists) || {};
  if (button) button.disabled = true;
  try {
    if (exists.raw_path) {
      try {
        await scout.call("archive.open_raw", { doc_id: docId });
        sourceZoomStatus("已用系统默认程序打开原件", false);
        return true;
      } catch (error) {
        /* 原件被移走/删掉：先说明，再退到全文（不弹窗） */
        sourceZoomStatus("原件打不开（" + ((error && error.message) || error) + "），改用全文", true);
      }
    } else {
      sourceZoomStatus("原件不在磁盘上，改用全文", false);
    }
    return await sourceZoomShowText();
  } finally {
    if (button) button.disabled = false;
  }
}


/* 把全文读进浮层里那块可滚区域（`archive.text` → `.source-zoom-text`）。 */
async function sourceZoomShowText() {
  var docId = sourceZoomState.docId;
  var holder = el("source-zoom-text");
  if (!docId || !holder) return false;
  holder.hidden = false;
  holder.textContent = "正在读取全文…";
  try {
    var payload = await scout.call("archive.text", { doc_id: docId });
    if (payload && payload.available === false) {
      holder.hidden = true;
      holder.textContent = "";
      sourceZoomStatus("读不到全文：" + (payload.reason || "没有落盘的正文"), true);
      return false;
    }
    var text = String((payload && payload.text) || "").trim();
    holder.textContent = text || "（全文是空的）";
    if (payload && payload.truncated) {
      sourceZoomStatus("全文较长，这里只显示前 " + (payload.chars || text.length) + " 字", false);
    } else {
      sourceZoomStatus("", false);
    }
    return true;
  } catch (error) {
    holder.hidden = true;
    holder.textContent = "";
    sourceZoomStatus("读全文失败：" + ((error && error.message) || error), true);
    return false;
  }
}


/* 「去原站」：系统浏览器打开源网址。空地址**不调** openExternalUrl（它内部会 alert），
 * 改成在浮层里写一行说明 —— 这条链路上一个 alert 都不许有。 */
function sourceZoomOpenOrigin() {
  var doc = sourceZoomState.doc || {};
  var ref = sourceZoomState.ref || {};
  var url = String(doc.url || ref.url || "").trim();
  if (!url) {
    sourceZoomStatus("这条资料没有记录源地址，去不了原站", true);
    return false;
  }
  openExternalUrl(url);
  return true;
}


/* 关浮层：只收起，不碰地图的选中态 / 详情面板 / 侧栏页签（用户第 3 条）。 */
function closeSourceZoom() {
  var overlay = sourceZoomOverlay();
  if (!overlay || overlay.hidden) return false;
  overlay.hidden = true;
  return true;
}


/* 拖标题栏移动浮层：从 flex 居中改成 left/top 绝对定位，改的只是位置，尺寸不动。 */
function sourceZoomDragStart(event) {
  if (event.button !== 0) return;
  var target = event.target;
  /* 标题栏里的按钮（关闭）不当拖拽把手 */
  if (target && target.closest && target.closest("button")) return;
  var sheet = sourceZoomSheet();
  if (!sheet) return;
  var box = sheet.getBoundingClientRect();
  sheet.style.position = "absolute";
  sheet.style.margin = "0";
  sheet.style.left = Math.round(box.left) + "px";
  sheet.style.top = Math.round(box.top) + "px";
  sourceZoomState.drag = { dx: event.clientX - box.left, dy: event.clientY - box.top };
  if (event.preventDefault) event.preventDefault();
}


function sourceZoomDragMove(event) {
  var drag = sourceZoomState.drag;
  if (!drag) return;
  var sheet = sourceZoomSheet();
  if (!sheet) return;
  var box = sheet.getBoundingClientRect();
  /* 不许拖到完全出界：横竖都留 120px/40px 在身上，才抓得回来 */
  var left = Math.min(
    Math.max(event.clientX - drag.dx, -box.width + 120),
    window.innerWidth - 120,
  );
  var top = Math.min(Math.max(event.clientY - drag.dy, 0), window.innerHeight - 40);
  sheet.style.left = Math.round(left) + "px";
  sheet.style.top = Math.round(top) + "px";
}


function sourceZoomDragEnd() {
  sourceZoomState.drag = null;
}


/* 右下角把手缩放：最小 420×320 由 sourceZoomClamp 兜住，松手时把尺寸记下来。 */
function sourceZoomResizeStart(event) {
  if (event.button !== 0) return;
  var sheet = sourceZoomSheet();
  if (!sheet) return;
  var box = sheet.getBoundingClientRect();
  sourceZoomState.resize = { x: event.clientX, y: event.clientY, w: box.width, h: box.height };
  if (event.preventDefault) event.preventDefault();
  if (event.stopPropagation) event.stopPropagation();
}


function sourceZoomResizeMove(event) {
  var state = sourceZoomState.resize;
  if (!state) return;
  sourceZoomApplySize(state.w + (event.clientX - state.x), state.h + (event.clientY - state.y));
}


function sourceZoomResizeEnd() {
  if (!sourceZoomState.resize) return;
  sourceZoomState.resize = null;
  sourceZoomPersistSize();
}


/* 接线只做一次（浮层不随地图重画，所以不进地图的监听账本 —— mapTeardown 会清账本）。
 * 关闭三条路：Esc / 关闭按钮 / 点遮罩外（点在 .source-sheet 里不算）。 */
function wireSourceZoom() {
  var overlay = sourceZoomOverlay();
  if (!overlay) return false;
  var closer = el("btn-source-zoom-close");
  if (closer) {
    closer.addEventListener("click", function () {
      closeSourceZoom();
    });
  }
  overlay.addEventListener("click", function (event) {
    if (event.target === overlay) closeSourceZoom();
  });
  var head = overlay.querySelector(".source-sheet-head");
  if (head) head.addEventListener("pointerdown", sourceZoomDragStart);
  var handle = el("source-zoom-resize");
  if (handle) handle.addEventListener("pointerdown", sourceZoomResizeStart);
  document.addEventListener("pointermove", function (event) {
    sourceZoomDragMove(event);
    sourceZoomResizeMove(event);
  });
  document.addEventListener("pointerup", function () {
    sourceZoomDragEnd();
    sourceZoomResizeEnd();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    /* 只有真关掉了才拦这一下：浮层没开时 Esc 归地图的搜索框 */
    if (closeSourceZoom() && event.stopPropagation) event.stopPropagation();
  });
  return true;
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

  /* 弱边（待验证）：数的是**画出来这子图**里的弱边（`mapState.view` 由 renderMapFigure
   * 先置好）。一条都没有时不挂这项 —— 挂个"虚线 = 待验证"只会让人满图找虚线。 */
  var shown = (mapState.view && mapState.view.edges) || (payload && payload.edges) || [];
  var weak = 0;
  shown.forEach(function (edge) {
    if (mapEdgeIsWeak(edge)) weak += 1;
  });

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

  /* 弱边（待验证）单独一项，排在**图例标题后面**：364px 的右栏一行放不下所有项，
   * 「横向可滑」是隐藏能力 —— 放在末尾等于看不见，而这一项正是解释图上虚线的唯一说明。
   * 定高 20px + nowrap，所以它不会把图例顶成两行。 */
  if (weak > 0) {
    var weakItem = document.createElement("span");
    weakItem.className = "map-legend-edge map-legend-weak";
    weakItem.setAttribute("data-confidence", "weak");
    var weakSwatch = document.createElement("i");
    weakSwatch.className = "map-line is-weak";
    weakItem.appendChild(weakSwatch);
    weakItem.appendChild(document.createTextNode("虚线 = 待验证"));
    box.insertBefore(weakItem, title.nextSibling);
  }
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


/* 搜索：走后端 `topic.search_nodes`（只管概念图这一层，不再带 layer）。 */
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
      limit: 8,
    });
  } catch (error) {
    mapRenderSearchResults([], "搜索失败：" + ((error && error.message) || String(error)));
    return;
  }
  /* 搜索期间图被重画：这批结果属于上一张图，丢掉（否则定位到不存在的节点）。 */
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


/* --------------------------------------------------------------- 提示行 */

function mapLabelOf(nodeId) {
  var nodes = (mapState.view && mapState.view.nodes) || [];
  for (var i = 0; i < nodes.length; i += 1) {
    if (nodes[i].id === String(nodeId)) return nodes[i].label;
  }
  return String(nodeId);
}


/* 一行提示（搜不到 / 节点被截断时说话）。查路径那一行已经删掉，这里只剩搜索用。 */
function mapShowMapNote(text, kind) {
  var note = el("map-note");
  if (!note) return;
  note.textContent = text;
  note.hidden = !text;
  note.setAttribute("data-kind", String(kind || "info"));
}


function renderMapFigure(host, payload) {
  if (!host) return;
  /* 顺序要紧：先按账本把上一张图的监听/定时器删干净，再换代，最后才建新图。 */
  mapTeardown();
  mapState.epoch += 1;

  /* 画布按**容器实测像素**建（量不到就 800x520 兜底，随后 mapWatchSize 会纠正）。 */
  var canvas = mapMeasure(host);
  var built = buildMapSvg(payload, mapState.showAll, canvas);
  mapState.view = built.view;
  mapKeepInfo(host);
  host.appendChild(built.svg);
  /* 挂尺寸监听：侧栏拖宽 / 页签重新显示 / 搬进放大浮层都按新尺寸重铺 */
  mapWatchSize(built.svg, built.view, host);
  renderMapLegend(payload);
  mapShowMapNote("", "info");

  var info = el("map-info");
  if (info) {
    info.hidden = true;
    info.textContent = "";
  }

  var count = el("map-count");
  if (count) {
    /* "（其中 K 条待验证）"：K=0 不显示括号 —— 干净图不该被 0 提醒。
     * K 优先取后端 `stats.weak_edges`（读不到就数画出来的边，老数据自然得 0）。 */
    var weak = mapWeakEdgeCount(payload, built.view.edges, built.view.truncated);
    var weakNote = weak > 0 ? "（其中 " + weak + " 条待验证）" : "";
    count.textContent = built.view.truncated
      ? "共 " + built.view.total + " 个节点，显示前 " + built.view.shown + "（按连接数排序）" + weakNote
      : "共 " + built.view.shown + " 个节点、" + built.view.edges.length + " 条边" + weakNote;
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
    mapState.showAll = false;
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
  }
  mapState.topicId = topicId;
  mapState.payload = payload;
  if (!payload || !(payload.nodes || []).length) {
    mapTeardown();
    renderMapEmpty(host, "这张知识地图还是空的（建图时没取到任何节点），先在会话里重新建一张");
    return;
  }
  renderMapFigure(host, payload);
}


function wireMapTab() {
  /* 资料浮层（#source-zoom）只接一次线：它不随地图重画，开着的时候也不该被地图的清理带走 */
  wireSourceZoom();
  var all = el("btn-map-all");
  if (all) {
    all.addEventListener("click", function () {
      if (!mapState.payload) return;
      mapState.showAll = true;
      renderMapFigure(el("map-body"), mapState.payload);
    });
  }

  /* 图内搜索：输入框防抖 220ms（定时器记账，重画会清掉），回车/按钮立即搜 */
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
