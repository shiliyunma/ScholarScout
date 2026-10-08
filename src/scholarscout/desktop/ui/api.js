/* 桥、DOM/格式小工具、主题与布局 —— 被其它所有层使用的底座
   从 app.js 拆出来（原来 3246 行一个文件）。同名函数仍是全局声明，
   跨文件调用与拆分前完全一样 —— 不需要打包器，也不需要 import。 */

var THEME_KEY = "scout:theme";


function systemPrefersDark() {
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
}


function resolveTheme(mode) {
  if (mode === "light" || mode === "dark") return mode;
  return systemPrefersDark() ? "dark" : "light";
}


function applyTheme(mode) {
  state.theme = mode;
  document.documentElement.classList.toggle("dark", resolveTheme(mode) === "dark");
  // 主题开关在设置面板里（顶栏那个按钮已按用户要求去掉）
}


async function bridgeCall(name) {
  /* 直接打桥（不走 RPC）：窗控只关乎窗口，后端忙的时候也必须点得动关闭。 */
  const api = (window.pywebview && window.pywebview.api) || {};
  if (typeof api[name] !== "function") throw new Error("桥没有这个方法：" + name);
  const raw = await api[name].apply(api, Array.prototype.slice.call(arguments, 1));
  const payload = JSON.parse(raw);
  if (!payload.ok) throw new Error(payload.error.message);
  return payload.result;
}

/* 无边框窗口的移动与缩放：pywebview 的无边框后端不给这两个能力（实测拖动无效、
   也没有系统边框可拖），所以自己实现。所有尺寸/位置都在后端夹一次，前端不重复规则。 */


function wireFramelessDrag() {
  const throttleMs = 40;

  // 通用拖动：按下 → 取基准几何 → 按屏幕坐标增量算 → 打桥
  function startDrag(event, onMove) {
    if (event.button !== 0) return;
    const target = event.target;
    if (target && target.closest && target.closest("button, a, input, select, textarea")) {
      return; // 按钮/输入框照常点，别被拖动吃掉
    }
    event.preventDefault();
    // 基准直接问浏览器要（screenX/screenY 是窗口在屏幕上的位置，innerWidth/Height 是无边框下的整窗尺寸）。
    // 一开始走桥问 win_geometry，结果拖动毫无反应 —— 桥那侧是好的（用排障通道单独调 win_move 能把窗口
    // 精确移到指定坐标），问题就出在这一步，索性不绕桥。
    const anchor = { x: event.screenX, y: event.screenY };
    const geo = {
      x: window.screenX,
      y: window.screenY,
      width: window.innerWidth,
      height: window.innerHeight,
    };
    let last = 0;
    const move = function (moveEvent) {
      const now = Date.now();
      if (now - last < throttleMs) return;
      last = now;
      onMove(geo, {
        dx: moveEvent.screenX - anchor.x,
        dy: moveEvent.screenY - anchor.y,
      });
    };
    const stop = function () {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", stop);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", stop);
  }

  const bar = document.querySelector(".topbar");
  if (bar) {
    bar.addEventListener("mousedown", function (event) {
      startDrag(event, function (geo, delta) {
        bridgeCall("win_move", String(geo.x + delta.dx), String(geo.y + delta.dy)).catch(
          function () {},
        );
      });
    });
  }

  // 三条隐形抓手：右边缘 / 下边缘 / 右下角
  const edges = [
    { cls: "grip-right", cursor: "ew-resize", mode: "w" },
    { cls: "grip-bottom", cursor: "ns-resize", mode: "h" },
    { cls: "grip-corner", cursor: "nwse-resize", mode: "wh" },
  ];
  edges.forEach(function (spec) {
    const node = document.createElement("div");
    node.className = "frameless-grip " + spec.cls;
    node.style.cursor = spec.cursor;
    node.addEventListener("mousedown", function (event) {
      startDrag(event, function (geo, delta) {
        const width = spec.mode.indexOf("w") >= 0 ? geo.width + delta.dx : geo.width;
        const height = spec.mode.indexOf("h") >= 0 ? geo.height + delta.dy : geo.height;
        bridgeCall("win_resize", String(width), String(height)).catch(function () {});
      });
    });
    // 挂到 documentElement 而不是 body：这段接线可能在 <body> 就绪之前跑
    // （踩过：class 加上了、抓手一个都没建 —— 因为那时 document.body 还是 null）
    document.documentElement.appendChild(node);
  });
}


async function wireWindowControls() {
  // 有原生标题行时不该出现自绘窗控：问桥要窗口模式，别猜、也别依赖启动时机。
  let mode = { frameless: false };
  try {
    mode = await bridgeCall("window_mode");
  } catch (error) {
    return; // 普通窗口：什么都不接管
  }
  if (!mode.frameless) return;
  document.documentElement.classList.add("frameless");
  wireFramelessDrag();
  const report = function (label) {
    return function (error) {
      el("status").textContent = label + "失败：" + error.message;
    };
  };
  el("win-min").addEventListener("click", function () {
    bridgeCall("win_minimize").catch(report("最小化"));
  });
  el("win-max").addEventListener("click", function () {
    bridgeCall("win_toggle_maximize").catch(report("最大化"));
  });
  el("win-close").addEventListener("click", function () {
    bridgeCall("win_close").catch(report("关闭"));
  });

  // 右下角把手：无边框窗口没有系统边框，缩放得自己来。
  // 按屏幕绝对坐标算新尺寸，再交给后端夹到最小尺寸（夹的规则只有后端一处）。
  const grip = el("resize-grip");
  let start = null;
  let lastSent = 0;
  grip.addEventListener("mousedown", function (event) {
    start = {
      x: event.screenX,
      y: event.screenY,
      w: window.innerWidth,
      h: window.innerHeight,
    };
    event.preventDefault();
  });
  window.addEventListener("mousemove", function (event) {
    if (!start) return;
    const now = Date.now();
    if (now - lastSent < 60) return; // 节流：别每移动一像素就打一次桥
    lastSent = now;
    const width = Math.round(start.w + (event.screenX - start.x));
    const height = Math.round(start.h + (event.screenY - start.y));
    bridgeCall("win_resize", String(width), String(height)).catch(function () {});
  });
  window.addEventListener("mouseup", function () {
    start = null;
  });
}


function el(id) {
  return document.getElementById(id);
}


function esc(text) {
  return String(text === null || text === undefined ? "" : text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}


function short(text, n) {
  const value = String(text === null || text === undefined ? "" : text);
  return value.length > n ? value.slice(0, n) + "…" : value;
}

/* --------------------------------------------------------- 后端推来的事件 */

window.scoutEvent = function (event) {
  if (!event || !event.method) return;
  const p = event.params || {};
  // 标题在轮次开始前就推过来（不等这一轮跑完）——这条要在"没有 pending 就返回"之前处理
  if (event.method === "session.updated") {
    if (state.sessionId === p.session_id && p.title) {
      const titleNode = el("chat-title");
      if (titleNode) titleNode.textContent = p.title;
    }
    loadSessions();
    return;
  }
  const trace = state.pending;
  if (!trace) return;

  if (event.method === "chat.tool_call") {
    trace.add("call", "▶ " + p.tool + "（" + short(p.args_preview, 90) + "）");
  } else if (event.method === "chat.tool_result") {
    const refs = p.refs || {};
    const keys = Object.keys(refs);
    const detail = keys.length ? "｜" + keys.join(", ") : "";
    if (p.ok) {
      trace.add("ok", "✓ " + (p.latency_ms || 0) + " ms" + detail);
    } else {
      trace.add("bad", "✗ 工具失败" + detail);
    }
  } else if (event.method === "chat.delta") {
    trace.setText(p.text || "");
  } else if (event.method === "chat.error") {
    trace.add("bad", "后端错误：" + (p.message || ""));
  }
};

/* ------------------------------------------------------------------ 骨架 */


function bubble(kind, who) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + kind;
  const who_el = document.createElement("div");
  who_el.className = "who";
  who_el.textContent = who;
  const body = document.createElement("div");
  body.className = "body";
  wrap.appendChild(who_el);
  wrap.appendChild(body);
  el("messages").appendChild(wrap);
  el("messages").scrollTop = el("messages").scrollHeight;
  return { wrap: wrap, body: body };
}


function addChip(parent, label, handler) {
  const chip = document.createElement("span");
  chip.className = "chip";
  chip.textContent = label;
  chip.addEventListener("click", function () {
    Promise.resolve(handler()).catch(function (error) {
      alert("打开失败：" + error.message);
    });
  });
  parent.appendChild(chip);
}

/* ------------------------------------------------------------------ 布局 */


var LAYOUT_KEY = "scout:layout";
/*: 三栏最小宽度（与 style.css 的默认值一起改；拖到再小也停在这里）。
    中间栏别小于 360：聊天气泡与工具卡片在更窄的宽度下会挤成一条线。 */


var LAYOUT_MIN = { left: 180, center: 360, right: 280 };


var LAYOUT_DEFAULT = { left: 216, right: 380 };


var SPLITTER_WIDTH = 6;


function layoutEl() {
  return document.querySelector(".layout");
}


function clampLayout(left, right) {
  const total = layoutEl().clientWidth || window.innerWidth;
  const usable = total - 2 * SPLITTER_WIDTH;
  left = Math.max(LAYOUT_MIN.left, Math.round(left));
  right = Math.max(LAYOUT_MIN.right, Math.round(right));
  // 先保证右边，再让左边吃剩下的空间；两头都不够时中间栏优先保底
  if (left + right > usable - LAYOUT_MIN.center) {
    right = Math.max(LAYOUT_MIN.right, usable - LAYOUT_MIN.center - left);
    if (left + right > usable - LAYOUT_MIN.center) {
      left = Math.max(LAYOUT_MIN.left, usable - LAYOUT_MIN.center - right);
    }
  }
  return { left: left, right: right };
}


function applyLayout(left, right, persist) {
  const next = clampLayout(left, right);
  const style = layoutEl().style;
  style.setProperty("--col-left", next.left + "px");
  style.setProperty("--col-right", next.right + "px");
  state.layout = next;
  if (persist !== false) {
    try {
      localStorage.setItem(LAYOUT_KEY, JSON.stringify(next));
    } catch (error) {
      /* 存储被禁用就算了，不影响本次会话内拖动 */
    }
  }
}


function initLayout() {
  /* 窗口尺寸变化时重新夹一次：左右栏是用户定的固定宽度，窗口缩小后必须让出空间，
     否则中间栏会被挤到最小宽度以下（表现就是"字挤成一列"）。 */
  window.addEventListener("resize", function () {
    if (!layoutEl()) return;
    applyLayout(state.layout.left, state.layout.right, false);
  });
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "null");
  } catch (error) {
    saved = null;
  }
  const left = saved && saved.left ? saved.left : LAYOUT_DEFAULT.left;
  const right = saved && saved.right ? saved.right : LAYOUT_DEFAULT.right;
  applyLayout(left, right, false);

  wireSplitter("split-left", "left");
  wireHeightSplitter("split-sources", "source-detail", SOURCE_HEIGHT_KEY);
  wireSplitter("split-right", "right");
  window.addEventListener("resize", function () {
    applyLayout(state.layout.left, state.layout.right, false);
  });
}


function wireSplitter(id, side) {
  const handle = el(id);
  if (!handle) return;

  handle.addEventListener("dblclick", function () {
    applyLayout(LAYOUT_DEFAULT.left, LAYOUT_DEFAULT.right);
  });

  handle.addEventListener("pointerdown", function (event) {
    event.preventDefault();
    const startX = event.clientX;
    const start = { left: state.layout.left, right: state.layout.right };
    handle.classList.add("is-dragging");
    handle.setPointerCapture(event.pointerId);

    function move(moveEvent) {
      const delta = moveEvent.clientX - startX;
      if (side === "left") {
        applyLayout(start.left + delta, start.right);
      } else {
        applyLayout(start.left, start.right - delta);
      }
    }
    function up() {
      handle.classList.remove("is-dragging");
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
    }
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  });
}

/* ------------------------------------------------------------ 会话与轨迹 */


function addButton(parent, label, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", function () {
    Promise.resolve(handler()).catch(function (error) {
      alert("操作失败：" + error.message);
    });
  });
  parent.appendChild(button);
}

/* ------------------------------------------------------------ 学习产物 */


function plainHint(text) {
  var p = document.createElement("p");
  p.className = "hint";
  p.textContent = text;
  return p;
}

/* -------------------------------------------- 浮动菜单 / 打开链接 / 高度分隔条 */


function openFloatingMenu(anchorEl, rows) {
  closeRowMenus();
  var rect = anchorEl.getBoundingClientRect();
  var menu = document.createElement("div");
  menu.className = "row-menu is-floating";
  rows.forEach(function (row) {
    if (!row) return; // 拿不到 URL 之类的项会传 null，跳过
    var button = document.createElement("button");
    button.type = "button";
    button.textContent = row[0];
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      closeRowMenus();
      row[1]();
    });
    menu.appendChild(button);
  });
  menu.style.top = Math.round(rect.bottom + 4) + "px";
  menu.style.left = Math.max(8, Math.round(rect.left - 150)) + "px";
  document.body.appendChild(menu);
  setTimeout(function () {
    document.addEventListener("click", closeRowMenus, { once: true });
  }, 0);
}


function fmtBytes(bytes) {
  var n = Number(bytes) || 0;
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
  return (n / 1024 / 1024 / 1024).toFixed(2) + " GB";
}


function closeRowMenus() {
  document.querySelectorAll(".row-menu").forEach(function (node) {
    node.remove();
  });
}


function closeTextModal() {
  el("textmodal").hidden = true;
}


async function copyTextModal() {
  var area = el("textmodal-body");
  var note = el("textmodal-note");
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(area.value);
    } else {
      area.select();
      document.execCommand("copy");
    }
    note.textContent = "已复制到剪贴板";
    note.className = "hint set-ok";
  } catch (error) {
    // 复制失败就把文本选中，让用户自己 Ctrl+C —— 总比什么都没发生好
    area.select();
    note.textContent = "自动复制不可用，已帮你选中，Ctrl+C 即可";
    note.className = "hint set-bad";
  }
}
