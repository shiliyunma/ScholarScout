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
];

/* 超过这个数就默认只画前 30 个（用户要求：大图要可读） */
var MAP_TRUNCATE_OVER = 50;
var MAP_PREVIEW_LIMIT = 30;

/* 布局坐标系固定 800x520，靠 viewBox 缩放：侧栏里变小、浮层里变大，
 * 是**同一份 DOM**（放大时整块搬进浮层，不重算布局，形状不会跳）。 */
var MAP_VIEW_WIDTH = 800;
var MAP_VIEW_HEIGHT = 520;

var mapState = { topicId: "", payload: null, showAll: false };

var MAP_NS = "http://www.w3.org/2000/svg";


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

  /* 最后统一缩放到画布：布局只看相对位置，绝对尺寸交给这一步（留出标签的边距） */
  var minX = Infinity;
  var maxX = -Infinity;
  var minY = Infinity;
  var maxY = -Infinity;
  nodes.forEach(function (node) {
    minX = Math.min(minX, node.x);
    maxX = Math.max(maxX, node.x);
    minY = Math.min(minY, node.y);
    maxY = Math.max(maxY, node.y);
  });
  var spanX = Math.max(1, maxX - minX);
  var spanY = Math.max(1, maxY - minY);
  var padX = 56;
  var padY = 40;
  var scale = Math.min((width - padX * 2) / spanX, (height - padY * 2) / spanY);
  nodes.forEach(function (node) {
    node.x = padX + (node.x - minX) * scale;
    node.y = padY + (node.y - minY) * scale;
  });
  return iterations;
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
  /* 光标落在 viewBox 的哪个点，缩放后那个点还要在光标下（否则会"跑掉"） */
  var px = ((event.clientX - box.left) / box.width) * MAP_VIEW_WIDTH;
  var py = ((event.clientY - box.top) / box.height) * MAP_VIEW_HEIGHT;
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


function mapDragStart(event) {
  if (event.button !== 0) return;
  var svg = event.currentTarget;
  var stage = svg.querySelector(".map-stage");
  if (!stage) return;
  var record = svg.__mapView || { k: 1, x: 0, y: 0 };
  var box = svg.getBoundingClientRect();
  if (!box.width) return;
  var scale = MAP_VIEW_WIDTH / box.width;
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
    window.removeEventListener("mousemove", move);
    window.removeEventListener("mouseup", stop);
  }

  window.addEventListener("mousemove", move);
  window.addEventListener("mouseup", stop);
}


function buildMapSvg(payload, showAll) {
  var view = mapVisibleGraph(payload, showAll);
  var iterations = mapLayout(view.nodes, view.edges, MAP_VIEW_WIDTH, MAP_VIEW_HEIGHT);

  var svg = mapSvgEl("svg", {
    class: "map-svg",
    viewBox: "0 0 " + MAP_VIEW_WIDTH + " " + MAP_VIEW_HEIGHT,
    preserveAspectRatio: "xMidYMid meet",
    role: "img",
    "aria-label": "知识地图：" + String((payload && payload.topic) || ""),
  });
  svg.setAttribute("data-node-count", String(view.nodes.length));
  svg.setAttribute("data-edge-count", String(view.edges.length));
  svg.setAttribute("data-iterations", String(iterations));
  svg.setAttribute("data-truncated", view.truncated ? "1" : "0");

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
    var line = mapSvgEl("line", {
      class: "map-edge rel-" + String(edge.kind || "uses"),
      "data-kind": String(edge.kind || "uses"),
      "data-source": edge.source,
      "data-target": edge.target,
      x1: src.x.toFixed(1),
      y1: src.y.toFixed(1),
      x2: dst.x.toFixed(1),
      y2: dst.y.toFixed(1),
    });
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
    var label = mapSvgEl("text", { class: "map-label", x: 0, y: mapNodeRadius(node.weight) + 12 });
    label.textContent = short(node.label || node.id, 14);
    group.appendChild(label);
    var tip = mapSvgEl("title", {});
    tip.textContent =
      (node.label || node.id) + "（" + mapNodeKindLabel(node.kind) + "）· " + (node.weight || 0) + " 条连接";
    group.appendChild(tip);

    group.addEventListener("mouseenter", function () {
      mapFocusNode(svg, node.id);
    });
    group.addEventListener("mouseleave", function () {
      mapClearFocus(svg);
      var picked = svg.getAttribute("data-picked");
      if (picked) mapFocusNode(svg, picked);
    });
    group.addEventListener("click", function () {
      svg.setAttribute("data-picked", node.id);
      mapFocusNode(svg, node.id);
      showMapNodeInfo(node);
    });
    stage.appendChild(group);
  });

  svg.appendChild(stage);
  svg.addEventListener("wheel", mapWheel, { passive: false });
  svg.addEventListener("mousedown", mapDragStart);
  svg.__mapView = { k: 1, x: 0, y: 0 };
  return { svg: svg, view: view, iterations: iterations };
}


function showMapNodeInfo(node) {
  var box = el("map-info");
  if (!box) return;
  var sources = (node.sources || []).join("、");
  box.textContent =
    (node.label || node.id) +
    "｜类型：" + mapNodeKindLabel(node.kind) +
    "（" + String(node.kind || "") + "）｜连接数：" + String(node.weight || 0) +
    (sources ? "｜来源：" + sources : "｜来源：无出处");
  box.hidden = false;
}


function renderMapLegend(payload) {
  var box = el("map-legend");
  if (!box) return;
  box.innerHTML = "";
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


function renderMapEmpty(host, note) {
  if (!host) return;
  host.innerHTML = "";
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


function renderMapFigure(host, payload) {
  if (!host) return;
  var built = buildMapSvg(payload, mapState.showAll);
  host.innerHTML = "";
  host.appendChild(built.svg);
  renderMapLegend(payload);

  var info = el("map-info");
  if (info) {
    info.hidden = true;
    info.textContent = "";
  }

  var count = el("map-count");
  if (count) {
    count.textContent = built.view.truncated
      ? "共 " + built.view.total + " 个节点，显示前 " + built.view.shown + "（按连接数排序）"
      : "共 " + built.view.shown + " 个节点、" + built.view.edges.length + " 条边";
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
    mapState.showAll = false;
    var legend = el("map-legend");
    if (legend) legend.innerHTML = "";
    var counter = el("map-count");
    if (counter) counter.textContent = "";
    var all = el("btn-map-all");
    if (all) all.hidden = true;
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
  if (mapState.topicId !== topicId) mapState.showAll = false;
  mapState.topicId = topicId;
  mapState.payload = payload;
  if (!payload || !(payload.nodes || []).length) {
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
