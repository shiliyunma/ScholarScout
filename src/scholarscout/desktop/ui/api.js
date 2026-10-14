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

/* ------------------------------------------ 会话流内的「加载圈」与工具调用信息条

   用户反馈（本轮两条）：
     ① 加载圈原来是 .composer 里的**绝对定位浮层** —— 要求搬进**会话消息流内部**；
     ② 工具调用过程要有反馈：像 Hermes 那样把关键信息（工具名 / 参数摘要 / 结果 / 耗时）
        落成可见条目，并且**每一轮的正文都要看得见**（原来每轮增量都覆盖同一个正文区，
        只有最后一轮留得下）。

   做法 = 纯函数状态机 / 格式化函数（便于 node 直测）
        + 把状态落到「进行中」气泡内部的小函数（见 app.js 的 makePending）。
   **不新增后端事件** —— 复用已有的 chat.tool_call / chat.tool_result / chat.delta /
   chat.done / chat.error；「发起一轮」那一下由前端自己发合成事件 "turn.start"
   （后端没有这个事件，别去事件表里找）。与加载圈无关的事件返回 null（不动状态）。 */

/** 事件 → 加载圈状态。prev 目前不参与判定，但保留形参以免以后加「已经收尾就别再亮」的规则。 */
function turnSpinnerNext(prev, method, params) {
  var p = params || {};
  if (method === "turn.start") return { visible: true, kind: "thinking", tool: "" };
  if (method === "chat.tool_call") {
    return { visible: true, kind: "tool", tool: p.tool ? String(p.tool) : "" };
  }
  if (method === "chat.tool_result") return { visible: true, kind: "waiting", tool: "" };
  if (method === "chat.delta") return { visible: true, kind: "writing", tool: "" };
  if (method === "chat.done" || method === "chat.error") {
    return { visible: false, kind: "off", tool: "" };
  }
  return null;
}

/** 状态 → 文案。文案集中在这一处；工具名取自 chat.tool_call 的 tool。 */
function turnSpinnerText(state) {
  if (!state || state.visible !== true) return "";
  if (state.kind === "tool") return state.tool ? "正在调用 " + state.tool + " 工具" : "正在调用工具…";
  if (state.kind === "thinking") return "正在思考…";
  if (state.kind === "writing") return "正在生成回答…";
  if (state.kind === "waiting") return "等待下一个动作…";
  return "处理中…";
}

/** 工具参数摘要：压掉空白 + 截断（默认 120 字，不刷屏）。空参数回「（无参数）」。 */
function toolArgsSummary(preview, max) {
  var limit = typeof max === "number" && max > 0 ? max : 120;
  var text = String(preview === null || preview === undefined ? "" : preview)
    .replace(/\s+/g, " ")
    .trim();
  if (!text || text === "{}") return "（无参数）";
  return text.length > limit ? text.slice(0, limit) + "…" : text;
}

/** refs 键 → 人话（后端 chat.tool_result 只给"报出了哪些 id"，条数就是信息量）。 */
var TOOL_REF_LABELS = {
  source_ids: "来源",
  route_ids: "路线",
  topic_ids: "主题图",
  note_ids: "笔记",
  card_ids: "抽背卡",
};

/** 返回摘要：ok 明确为 false 才报失败（缺字段按成功处理，与后端 result.ok 默认 True 一致）。 */
function toolResultSummary(params) {
  var p = params || {};
  if (p.ok === false) return "失败";
  var refs = p.refs || {};
  var parts = [];
  Object.keys(refs).forEach(function (key) {
    var list = refs[key];
    var n = list && list.length ? list.length : 0;
    if (n) parts.push((TOOL_REF_LABELS[key] || key) + " " + n + " 条");
  });
  return parts.length ? "完成｜新增 " + parts.join(" ") : "完成";
}

/** 毫秒 → 人话。没有后端真值时**不编**：返回值里会写明是前端计时。 */
function fmtDuration(ms) {
  var n = Math.max(0, Math.round(Number(ms) || 0));
  if (n < 1000) return n + " ms";
  return (n / 1000).toFixed(n < 10000 ? 1 : 0) + " s";
}

/** 耗时文本：后端给的真实 latency_ms 优先（唯一真值）；没有才用前端自计的毫秒。 */
function toolLatencyText(params, startedAt, now) {
  var p = params || {};
  var ms = p.latency_ms;
  if (typeof ms === "number" && isFinite(ms) && ms >= 0) return fmtDuration(ms);
  if (typeof startedAt === "number" && typeof now === "number" && now >= startedAt) {
    return fmtDuration(now - startedAt) + "（前端计时）";
  }
  return "";
}

/** 流式正文分段（纯函数）：chat.delta 的 text 是**本轮累计值**
    （runtime/loop.py 每轮把 streamed_text 清零），所以不能靠「文本变短了」猜分段。
    分段的判据是**回合语义**：一轮的正文流一定在它的 chat.tool_call 之前收尾 ——
    当前段落已被一次工具调用收尾过，下一个增量就属于新一轮。 */
function replyRoundNext(prevClosed, text) {
  return {
    isNew: prevClosed === true,
    text: String(text === null || text === undefined ? "" : text),
  };
}

/** 当前加载圈状态就放在**这个会话自己的槽**上（`slot.spinner`），不再是全局一个。
    事件 → 加载圈。它**不是浮层**：落到「进行中」气泡内部的 .msg-progress 行，
    轮次收尾时会被真正移除。没有进行中气泡（这一轮不是本界面发起的）就什么都不动 ——
    后端补推的迟到事件不该点亮界面。 */
function turnSpinnerUpdate(slot, method, params) {
  if (!slot) return;
  var next = turnSpinnerNext(slot.spinner, method, params);
  slot.spinner = next;
  var trace = slot.trace;
  if (!trace || next === null) return;
  if (next.visible === true) trace.showProgress(turnSpinnerText(next), next.kind, next.tool);
  else trace.hideProgress();
}

/** 摘掉**某个会话的流里**残留的加载圈（幂等）。这是「结束后必消失」的第二道保险：
    正常路径由 trace.hideProgress() 收掉；异常路径（气泡对象丢了、后端没回 chat.done）
    靠这一条兜底，绝不会永久转圈。
    **只扫一条流** —— 扫全局会把别的会话正在转的圈也摘掉（探针抓过这个）。
    找根的顺序：这个会话自己的气泡 → 这个会话自己的流 → 当前显示的流；
    都找不到就什么都不动（宁可留着，也不许动别人）。 */
function turnSpinnerSweep(slot, sessionId) {
  var root = null;
  if (slot && slot.trace && slot.trace.node) {
    root = slot.trace.node.wrap;
  } else {
    var entry = sessionId ? state.streams[String(sessionId)] : null;
    if (entry && entry.node) root = entry.node;
    else {
      var box = el("messages");
      root = (box && box.querySelector(".msg-stream.is-active")) || null;
    }
  }
  if (!root) return;
  root.querySelectorAll(".msg-progress").forEach(function (node) {
    node.remove();
  });
}

/** 强制复位 + 兜底清扫（**按会话**）。轮次收尾（chat.done / 发起就失败）一定调它。 */
function turnSpinnerReset(slot, sessionId) {
  if (slot) slot.spinner = null;
  if (slot && slot.trace) slot.trace.hideProgress();
  turnSpinnerSweep(slot, sessionId);
}

/* ---------------------------------------------- 事件路由（纯函数，node 直测） */

/** 事件 → 它属于哪个会话（纯函数）。返回 session_id 或 null。

    `chat.*` 家族与 `session.updated` 必带 `session_id`（docs/20 §3.1 的**机制保证**：
    `bind_emitter` 在信封上注入），所以"这条事件属于谁"是查字段，不是猜。
    拿不到 session_id 的事件一律返回 null → **不落任何会话的槽**：
    宁可什么都不动，也绝不许落错会话（落错就是用户报的那个 bug）。 */
function eventSessionKey(method, params) {
  var p = params || {};
  var raw = p.session_id;
  if (raw === null || raw === undefined) return null;
  var key = String(raw);
  return key ? key : null;
}

/** 一轮完成 → 这个会话要不要打角标（纯函数）。

    规则：**不是当前显示的那个会话**才打；已经是未读的保持未读
    （不许被后来到达的其它事件顺手清掉 —— 未读只有"切过去/点掉"才清）。 */
function unreadFromDone(doneSessionId, currentSessionId, alreadyUnread) {
  if (!doneSessionId) return false;
  if (alreadyUnread === true) return true;
  return String(doneSessionId) !== String(currentSessionId || "");
}

/* --------------------------------------------------------- 后端推来的事件 */

window.scoutEvent = function (event) {
  if (!event || !event.method) return;
  const p = event.params || {};
  // 标题在轮次开始前就推过来（不等这一轮跑完）——这条要在"按槽路由"之前处理
  if (event.method === "session.updated") {
    if (state.sessionId === p.session_id && p.title) {
      const titleNode = el("chat-title");
      if (titleNode) titleNode.textContent = p.title;
    }
    loadSessions();
    return;
  }
  // 抽背「不会」的流式增量：直接**追加**到当前提示区（收尾时前端会用最终全文校正）。
  // 这条与 chat 轮次无关，所以必须放在"按槽路由"之前 —— 否则抽背永远收不到增量。
  if (event.method === "practice.delta") {
    const out = document.querySelector(".practice-help-out");
    if (out) out.textContent = (out.textContent || "") + (p.delta || "");
    return;
  }
  /* 事件**按 session_id 路由**：只落进它自己会话的槽（docs/20 §3.1/§3.3）。
     不是当前显示的会话也要收下 —— 它的气泡在**它自己那条流**里（切回去能看全）。
     判据是信封上的 session_id（后端 bind_emitter 注入），不是"此刻显示的是谁"。 */
  const key = eventSessionKey(event.method, p);
  const slot = key ? turnSlot(key, false) : null;
  if (!slot || !slot.trace) return; // 这一轮不是本界面发起的（没有进行中气泡）→ 不动界面

  // 收尾（含权威正文 text + message_id）单独走一条：角标/计数也在那里判
  if (event.method === "chat.done") {
    finishTurn(key, p);
    return;
  }

  // 加载圈：只有这个会话真在跑时才动它（只动它自己那条流的进度行）
  turnSpinnerUpdate(slot, event.method, p);

  if (event.method === "chat.tool_call") {
    slot.trace.addToolCall(p);
  } else if (event.method === "chat.tool_result") {
    slot.trace.addToolResult(p);
  } else if (event.method === "chat.delta") {
    // 每轮增量都进会话流：本轮是第一段就新开一段，否则替换当前段的累计值（见 replyRoundNext）
    slot.trace.pushDelta(p.text || "");
  } else if (event.method === "chat.error") {
    slot.trace.add("bad", "后端错误：" + (p.message || ""));
  }
};

/* ------------------------------------------------------------------ 骨架 */


function bubble(kind, who, sessionId) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + kind;
  const who_el = document.createElement("div");
  who_el.className = "who";
  who_el.textContent = who;
  const body = document.createElement("div");
  body.className = "body";
  wrap.appendChild(who_el);
  wrap.appendChild(body);
  // 挂在**这个会话自己的**消息流里（不是"此刻正在显示的那条"）
  streamBox(sessionId === undefined ? state.sessionId : sessionId).appendChild(wrap);
  el("messages").scrollTop = el("messages").scrollHeight;
  return { wrap: wrap, body: body, who: who_el };
}


/** 会话流里的一条系统提示（没有「进行中」气泡可挂时用它，别把反馈静默丢掉）。 */
function notice(text, sessionId) {
  const wrap = document.createElement("div");
  wrap.className = "msg notice";
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = text;
  wrap.appendChild(body);
  streamBox(sessionId === undefined ? state.sessionId : sessionId).appendChild(wrap);
  el("messages").scrollTop = el("messages").scrollHeight;
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


/* ------------------------------------------------ 左栏（会话）收起 / 展开 */

/*: localStorage 键（与 scout:theme / scout:layout / scout:uiScale 同一个前缀与风格）。 */
var SIDEBAR_COLLAPSE_KEY = "scout:sidebarCollapsed";


/** 纯函数：存档值 → 是否收起。**只有 "1" 算收起**（没存过、被写坏了都当展开）。 */
function sidebarCollapsedFromStored(raw) {
  return raw === "1";
}


/** 纯函数：是否收起 → 存档值。 */
function sidebarStoredValue(collapsed) {
  return collapsed ? "1" : "0";
}


/** 读存档；localStorage 不可用（隐私模式）时当展开，不因此报错。 */
function readSidebarCollapsed() {
  try {
    return sidebarCollapsedFromStored(localStorage.getItem(SIDEBAR_COLLAPSE_KEY));
  } catch (error) {
    return false;
  }
}


/** 应用收起态：类挂在 .layout 上（CSS 靠它改 grid 轨道），按钮的文案/箭头随状态变。
    persist === false 时不回写 localStorage（启动时读存档用）。 */
function applySidebarCollapsed(collapsed, persist) {
  state.sidebarCollapsed = collapsed;
  const layout = layoutEl();
  if (layout) layout.classList.toggle("is-side-collapsed", collapsed);
  const button = el("btn-side-toggle");
  if (button) {
    const label = collapsed ? "展开侧栏" : "收起侧栏";
    button.title = label;
    button.setAttribute("aria-label", label);
    button.setAttribute("aria-expanded", collapsed ? "false" : "true");
  }
  if (persist !== false) {
    try {
      localStorage.setItem(SIDEBAR_COLLAPSE_KEY, sidebarStoredValue(collapsed));
    } catch (error) {
      /* 存不了就算了：只是不持久化，不影响本次收起 */
    }
  }
}


function toggleSidebarCollapsed() {
  applySidebarCollapsed(!state.sidebarCollapsed);
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

/* 所有「摘要 / 详情」区的统一关闭按钮（用户要求：每一块都能关掉）。
   放在 api.js 是因为它属于共享 DOM 工具，不属于某一个业务面板。 */
function attachDetailClose(box, hint) {
  const head = document.createElement("div");
  head.className = "detail-head";
  const closer = document.createElement("button");
  closer.type = "button";
  closer.className = "ghost detail-close";
  closer.title = "关闭";
  closer.textContent = "✕";
  closer.addEventListener("click", function () {
    // 大浩选定：关闭 = **整块收起**（详情区隐藏、列表占满高度），点列表条目时自动再展开
    box.innerHTML = "";
    box.classList.add("is-collapsed");
  });
  box.classList.remove("is-collapsed");   // 每次渲染（点条目）都先展开
  head.appendChild(closer);
  box.appendChild(head);
  return head;
}
