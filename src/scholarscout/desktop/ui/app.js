/* ScholarScout 桌面壳前端（无构建、离线）。
 *
 * 原则：界面**不做业务判断** —— 一切都经 scout.call 打到后端，后端是唯一真相。
 * 所以这里只出现在 docs/01 §3 方法表里真实存在的方法名。
 */
"use strict";

var state = {
  sessionId: null,
  sending: false,
  routes: [],
  cards: [],
  pending: null,
  theme: "auto",
  history: false,
  noteKind: "note",
  layout: { left: 216, right: 380 },
};

/* ------------------------------------------------------------------ 主题 */

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
  var button = el("btn-theme");
  if (button) {
    button.textContent = mode === "auto" ? "跟随系统" : mode === "dark" ? "深色" : "浅色";
    button.title = "主题：" + button.textContent + "（点一下切换浅/深，再次回到跟随系统）";
  }
}

function cycleTheme() {
  var next = state.theme === "auto" ? (systemPrefersDark() ? "light" : "dark") : state.theme === "dark" ? "light" : "dark";
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch (error) {
    /* 隐私模式下 localStorage 可能不可用，只是不持久化而已 */
  }
  applyTheme(next);
}

/* ------------------------------------------------------------------ 通道 */

var scout = {
  async call(method, params) {
    const raw = await window.pywebview.api.rpc(method, params ? JSON.stringify(params) : "");
    const payload = JSON.parse(raw);
    if (!payload.ok) {
      const error = new Error(payload.error.message);
      error.code = payload.error.code;
      throw error;
    }
    return payload.result;
  },
};

function el(id) {
  return document.getElementById(id);
}

function esc(text) {
  return String(text === null || text === undefined ? "" : text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/** token 数按人看的写法：12345 → "12.3k"。 */
function fmtTokens(value) {
  const n = Number(value || 0);
  if (!n) return "0";
  if (n < 1000) return String(n);
  return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
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

function renderHealth(health) {
  const llm = health.llm_configured ? "已配置模型" : "未配置模型（只能搜，不能生成）";
  const tools = (health.tools || []).length;
  el("status").textContent =
    "v" + health.version + "｜工具 " + tools + " 个｜" + llm + "｜库 " + health.db_path;
}

function renderSessions(items) {
  const list = el("session-list");
  list.innerHTML = "";
  items.forEach(function (item) {
    const li = document.createElement("li");
    if (item.session_id === state.sessionId) li.className = "is-active";
    if (item.pinned) li.classList.add("is-pinned");
    // 置顶要有可见标记：不然用户点了置顶只看到顺序变了，不知道是不是生效了
    const badge = item.pinned ? '<span class="pin-flag" title="已置顶">顶</span>' : "";
    li.innerHTML =
      badge +
      esc(short(item.title || "未命名会话", 16)) +
      '<span class="meta">' +
      esc(short(item.updated_at || "", 16)) +
      "｜" +
      item.message_count +
      " 条</span>";
    li.addEventListener("click", function () {
      openSession(item.session_id);
    });
    li.title = (item.title || "未命名会话") + "（双击可改名）";
    li.addEventListener("dblclick", function (event) {
      event.stopPropagation();
      startRename(item, li);
    });
    li.appendChild(rowMenuButton(item, li));
    list.appendChild(li);
  });
}

function startRename(item, li) {
  /* 双击改名：标题是自动生成的，用户总有想改的时候。
     用内联输入而不是 window.prompt —— pywebview 里 prompt 不保证可用。 */
  const input = document.createElement("input");
  input.className = "rename-input";
  input.value = item.title || "";
  li.innerHTML = "";
  li.appendChild(input);
  input.focus();
  input.select();

  let done = false;
  async function commit(save) {
    if (done) return;
    done = true;
    if (save) {
      const name = input.value.trim();
      if (name && name !== item.title) {
        try {
          await scout.call("session.rename", { session_id: item.session_id, title: name });
        } catch (error) {
          /* 改名失败就按原样刷新，别把界面留在半截状态 */
        }
      }
    }
    await loadSessions();
  }

  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      commit(true);
    } else if (event.key === "Escape") {
      event.preventDefault();
      commit(false);
    }
  });
  input.addEventListener("blur", function () {
    commit(true);
  });
  input.addEventListener("click", function (event) {
    event.stopPropagation();
  });
}

/* ------------------------------------------------------------------ 聊天 */

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

function renderMessage(message) {
  if (message.role === "user") {
    bubble("user", "你").body.textContent = message.content || "";
    return;
  }
  if (message.role === "tool") return; // 工具结果在轨迹里看，不铺在正文
  if (message.role === "assistant" && (message.content || "").trim()) {
    ScoutMarkdown.renderInto(bubble("assistant", "ScholarScout").body, message.content);
  }
}

function makePending() {
  const node = bubble("assistant", "ScholarScout · 进行中");
  const traceBox = document.createElement("div");
  traceBox.className = "trace";
  node.body.appendChild(traceBox);
  const textBox = document.createElement("div");
  textBox.className = "reply";
  node.body.appendChild(textBox);

  /* 流式渲染的节流：模型每秒能吐几十个增量，每个都重排一遍 DOM 会卡。
     120ms 一次肉眼已经"实时"了，末次结果直接落（见 flush）。 */
  const THROTTLE_MS = 120;
  let lastPaint = 0;
  let timer = null;
  let buffered = "";
  let finished = false;

  function paint() {
    lastPaint = Date.now();
    ScoutMarkdown.renderInto(textBox, buffered);
    el("messages").scrollTop = el("messages").scrollHeight;
  }

  function flush() {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    paint();
  }

  return {
    add: function (cls, text) {
      const line = document.createElement("div");
      line.className = cls;
      line.textContent = text;
      traceBox.appendChild(line);
      el("messages").scrollTop = el("messages").scrollHeight;
    },
    setText: function (text) {
      buffered = text || "";
      if (finished) {
        flush();
        return;
      }
      const now = Date.now();
      if (now - lastPaint >= THROTTLE_MS) {
        flush();
        return;
      }
      if (!timer) timer = setTimeout(flush, THROTTLE_MS - (now - lastPaint));
    },
    finish: function () {
      finished = true;
      flush();
    },
    node: node,
  };
}

async function send(text) {
  if (state.sending || !state.sessionId) return;
  state.sending = true;
  el("btn-send").disabled = true;
  // 一轮 3–12 分钟，必须能停：后端按下一次「步间检查」生效
  el("btn-stop").hidden = false;
  bubble("user", "你").body.textContent = text;
  state.pending = makePending();

  try {
    const result = await scout.call("chat.send", { session_id: state.sessionId, text: text });
    state.pending.node.wrap.classList.remove("assistant");
    if (result.text) state.pending.setText(result.text);
    state.pending.finish();
    state.pending.add(
      result.stop_reason === "completed" ? "ok" : "warn",
      "stop_reason=" + result.stop_reason + "｜工具 " + (result.tool_calls || []).length + " 次"
    );
    const usage = result.usage || {};
    if (usage.prompt_tokens || usage.completion_tokens) {
      // 把每一轮的真实 token 摆出来 —— 用户抱怨"缓存没效果、账单很高"，界面就该给得出数字
      state.pending.add(
        "ok",
        "token 输入 " + fmtTokens(usage.prompt_tokens) + " ／ 输出 " + fmtTokens(usage.completion_tokens) +
          (usage.cached_tokens ? "（命中缓存 " + fmtTokens(usage.cached_tokens) + "）" : "")
      );
    }
    await refreshSide();
    // 首条消息发出后标题才有值 → 刷一次会话列表，让新标题立刻出现
    await loadSessions();
    await refreshSessionUsage();
  } catch (error) {
    state.pending.add("bad", "调用失败：" + error.message);
    state.pending.node.wrap.classList.add("error");
    await refreshSessionUsage();
  } finally {
    state.pending = null;
    state.sending = false;
    el("btn-send").disabled = false;
    el("btn-stop").hidden = true;
  }
}

// 「这次会话一共花了多少」—— 用户在意账单，就把可核对的数字摆在手边。
// 只报 token 与调用次数；单价未配置时**不折算金额**（报 0 会让人以为免费）。
async function refreshSessionUsage() {
  const node = el("session-usage");
  if (!node || !state.sessionId) return;
  try {
    const usage = await scout.call("session.usage", { session_id: state.sessionId });
    const tokens = usage.tokens || {};
    if (!usage.calls) {
      node.textContent = "";
      return;
    }
    node.textContent =
      "本会话累计 输入 " + fmtTokens(tokens.prompt) + " ／ 输出 " + fmtTokens(tokens.completion) +
      " ／ 模型调用 " + usage.calls + " 次" + (usage.est_cost ? " ／ 约 " + usage.est_cost : "");
    node.title = "数据来自 usage_records；未配置单价故不折算金额";
  } catch (error) {
    node.textContent = "";
  }
}

// 「停止」：按会话取消（整轮结束前拿不到 turn_id），并如实说明生效时机
async function stopTurn() {
  if (!state.sessionId) return;
  const button = el("btn-stop");
  button.disabled = true;
  try {
    const result = await scout.call("chat.cancel", { session_id: state.sessionId });
    const note = result && result.note ? result.note : "已请求取消";
    if (state.pending) state.pending.add(result && result.ok ? "warn" : "bad", note);
    else el("hint").textContent = note;
  } catch (error) {
    if (state.pending) state.pending.add("bad", "取消失败：" + error.message);
  } finally {
    button.disabled = false;
  }
}

/* 说明：原先进回答末尾追加的"路线/卡片/主题图/资料"chip 卡片已按用户反馈去掉 ——
   这些入口在右侧面板（资料库/学习路线/笔记）与「显示全部历史产物」里都能找到，
   正文末尾那排 chip 只是重复噪音。`addChip` 仍被笔记页的引用跳转使用，保留。 */

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

async function loadSessions() {
  const payload = await scout.call("session.list", { limit: 50 });
  const items = payload.items || [];
  renderSessions(items);
  // 顶部标题跟着当前会话走：标题在轮次开始前就生成，用户一发送就能看到
  const current = items.filter(function (item) { return item.session_id === state.sessionId; })[0];
  const titleNode = el("chat-title");
  if (titleNode) titleNode.textContent = (current && current.title) || "新会话";
  return items;
}

async function newSession() {
  // 不要传 title：让后端在首条用户消息到来时自动命名（传了占位标题就永远轮不到自动命名）
  const created = await scout.call("session.create", {});
  state.sessionId = created.session_id;
  el("messages").innerHTML =
    '<p class="empty">新会话已建好（标题会在你发出第一句后自动生成）。试试：「帮我找教育智能体方向的入门资料」</p>';
  await loadSessions();
}

function collectRefs(messages) {
  const refs = { route_ids: [], topic_ids: [], note_ids: [], source_ids: [] };
  const seen = {};
  messages.forEach(function (message) {
    if (message.role !== "tool" || !message.content) return;
    let payload = null;
    try {
      payload = JSON.parse(message.content);
    } catch (error) {
      return;
    }
    const item = payload && payload.refs ? payload.refs : {};
    Object.keys(item).forEach(function (key) {
      if (!refs[key]) refs[key] = [];
      (item[key] || []).forEach(function (value) {
        if (!seen[key + value]) {
          seen[key + value] = true;
          refs[key].push(value);
        }
      });
    });
  });
  return refs;
}

async function openSession(sessionId) {
  state.sessionId = sessionId;
  const payload = await scout.call("session.messages", { session_id: sessionId, limit: 200 });
  const items = payload.items || [];
  el("messages").innerHTML = "";
  items.forEach(renderMessage);
  if (!items.length) {
    el("messages").innerHTML = '<p class="empty">这个会话还没有消息。</p>';
  }
  const refs = collectRefs(items);
  await renderRoutes(refs.route_ids || []);
  await loadSessions();
  await refreshSessionUsage();  // 累计用量跟着会话走，不是全局
}

/* -------------------------------------------------------------- 资料库 */

function normalize(item) {
  return {
    docId: item.doc_id,
    sourceId: item.source_id,
    title: item.title_zh || item.title || item.source_title || "(无标题)",
    text: item.summary_short || item.snippet || "",
    tier: item.tier,
    authority: item.authority,
    year: item.year,
    domain: item.domain,
    url: item.url,
  };
}

async function loadSources(query) {
  const params = { limit: 50 };
  if (query) params.query = query;
  const payload = await scout.call("archive.list", params);
  const items = (payload.items || []).map(normalize);
  const list = el("source-list");
  list.innerHTML = "";
  if (!items.length) {
    list.innerHTML = '<li class="hint">库里还没有资料。先在左边让它去搜一轮。</li>';
    return;
  }
  items.forEach(function (item) {
    const li = document.createElement("li");
    li.innerHTML =
      esc(short(item.title, 22)) +
      '<span class="meta">' +
      esc(item.domain || "") +
      "｜tier " +
      esc(item.tier === null || item.tier === undefined ? "?" : item.tier) +
      (payload.search_path ? "｜" + payload.search_path : "") +
      "</span>";
    li.addEventListener("click", function () {
      showSource(item.docId);
    });
    list.appendChild(li);
  });
}

async function showSource(docId) {
  const payload = await scout.call("archive.get", { doc_id: docId });
  const doc = payload.document || {};
  const summaries = payload.summaries || [];
  const first = summaries[0] || {};
  const points = safePoints(first.points_json);

  const box = el("source-detail");
  box.innerHTML = "";
  const title = document.createElement("h3");
  title.textContent = first.title_zh || doc.title || "(无标题)";
  box.appendChild(title);

  const meta = document.createElement("div");
  meta.className = "kv";
  meta.textContent =
    (doc.domain || "") +
    "｜tier " +
    (doc.tier === null || doc.tier === undefined ? "未判权" : doc.tier) +
    "｜authority " +
    (doc.authority === null || doc.authority === undefined ? "-" : doc.authority) +
    "｜" +
    (doc.extract_status || "") +
    (doc.truncated ? "｜超长已截断" : "");
  box.appendChild(meta);

  const body = document.createElement("div");
  body.textContent = first.summary_short || "（这条资料还没出摘要）";
  box.appendChild(body);

  if (points.length) {
    const ul = document.createElement("ul");
    points.forEach(function (point) {
      const li = document.createElement("li");
      li.textContent = point;
      ul.appendChild(li);
    });
    box.appendChild(ul);
  }

  const paths = await scout.call("archive.paths", { doc_id: docId });
  const pathInfo = document.createElement("div");
  pathInfo.className = "kv";
  pathInfo.textContent =
    "原件 " + (paths.exists && paths.exists.raw_path ? "√ " : "× ") + paths.raw_path +
    "\n全文 " + (paths.exists && paths.exists.text_path ? "√ " : "× ") + paths.text_path;
  box.appendChild(pathInfo);

  const actions = document.createElement("div");
  actions.className = "actions";
  addButton(actions, "用系统程序打开原件", async function () {
    await scout.call("archive.open_raw", { doc_id: docId });
  });
  addButton(actions, "在文件夹中定位", async function () {
    await scout.call("archive.reveal", { doc_id: docId, which: "raw" });
  });
  addButton(actions, "定位全文文本", async function () {
    await scout.call("archive.reveal", { doc_id: docId, which: "text" });
  });
  box.appendChild(actions);
}

async function showSourceBySourceId(sourceId) {
  const payload = await scout.call("archive.list", { limit: 200 });
  const hit = (payload.items || []).find(function (item) {
    return item.source_id === sourceId;
  });
  if (hit && hit.doc_id) await showSource(hit.doc_id);
  else alert("这条资料还没有抓取正文（只有 source_id：" + sourceId + "）");
}

function safePoints(raw) {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch (error) {
    return [];
  }
}

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

async function renderRoutes(routeIds) {
  state.routes = routeIds;
  const list = el("route-list");
  list.innerHTML = "";
  if (!routeIds.length) {
    list.innerHTML = '<li class="hint">这个会话还没排过路线。让它「排一条 4 周路线」。</li>';
    return;
  }
  for (const routeId of routeIds) {
    let payload = null;
    try {
      payload = await scout.call("route.get", { route_id: routeId });
    } catch (error) {
      continue;
    }
    const li = document.createElement("li");
    const weeks = (payload.plan && payload.plan.weeks ? payload.plan.weeks.length : 0);
    li.innerHTML =
      esc(short(payload.topic, 18)) +
      '<span class="meta">' +
      esc(payload.level || "") +
      "｜" +
      weeks +
      " 周｜" +
      esc((payload.plan && payload.plan.mode) || "") +
      "</span>";
    li.addEventListener("click", function () {
      showRoute(routeId);
    });
    list.appendChild(li);
  }
}

async function showRoute(routeId) {
  const payload = await scout.call("route.get", { route_id: routeId });
  const plan = payload.plan || {};
  const box = el("route-detail");
  box.innerHTML = "";

  const title = document.createElement("h3");
  title.textContent = "学习路线：" + (payload.topic || "");
  box.appendChild(title);

  const meta = document.createElement("div");
  meta.className = "kv";
  const stats = plan.stats || {};
  meta.textContent =
    "水平 " + (payload.level || "-") +
    "｜周数 " + (payload.weeks || (plan.weeks || []).length) +
    "｜生成方式 " + (plan.mode || "-") +
    "｜条目 " + (stats.items === undefined ? "-" : stats.items) +
    "｜无出处 " + (stats.ungrounded_items === undefined ? "-" : stats.ungrounded_items);
  box.appendChild(meta);

  (plan.weeks || []).forEach(function (week) {
    const wrap = document.createElement("div");
    wrap.className = "week";
    const head = document.createElement("div");
    head.textContent = "第 " + week.idx + " 周：" + (week.goal || "");
    wrap.appendChild(head);

    (week.items || []).forEach(function (item) {
      const line = document.createElement("div");
      line.className = "kv";
      line.textContent =
        "· [" + (item.kind || "") + "] " + (item.title || "") +
        "（约 " + (item.est_minutes || 0) + " 分钟，来源 " + (item.source_id || "无出处") + "）";
      wrap.appendChild(line);
      if (item.why) {
        const why = document.createElement("div");
        why.className = "kv";
        why.style.paddingLeft = "12px";
        why.textContent = "↳ " + item.why;
        wrap.appendChild(why);
      }
    });

    if (week.checkpoint) {
      const checkpoint = document.createElement("div");
      checkpoint.className = "checkpoint";
      checkpoint.textContent = "检查点：" + week.checkpoint;
      wrap.appendChild(checkpoint);
    }
    box.appendChild(wrap);
  });

  const actions = document.createElement("div");
  actions.className = "actions";
  addButton(actions, "导出 Markdown", async function () {
    const exported = await scout.call("route.export", { route_id: routeId });
    const info = document.createElement("div");
    info.className = "kv";
    info.textContent = "已导出：" + exported.path;
    actions.appendChild(info);
    await scout.call("archive.reveal", { path: exported.path });
  });
  box.appendChild(actions);
}

function showTopic(payload) {
  const graph = payload.graph || {};
  const nodes = graph.nodes || [];
  const edges = graph.edges || [];
  const stats = graph.stats || {};
  const box = el("source-detail");
  box.innerHTML = "";
  const title = document.createElement("h3");
  title.textContent = "主题地图：" + (payload.topic || "");
  box.appendChild(title);
  const meta = document.createElement("div");
  meta.className = "kv";
  meta.textContent =
    "节点 " + nodes.length + "｜边 " + edges.length +
    "｜无出处节点 " + (stats.ungrounded_nodes === undefined ? "-" : stats.ungrounded_nodes) +
    "｜生成方式 " + (graph.mode || "-");
  box.appendChild(meta);
  nodes.forEach(function (node) {
    const line = document.createElement("div");
    line.className = "kv";
    line.textContent =
      "· " + node.label + " [" + node.kind + "] " +
      ((node.source_ids || []).join(", ") || "无出处");
    box.appendChild(line);
  });
  edges.forEach(function (edge) {
    const line = document.createElement("div");
    line.className = "kv";
    line.textContent = "→ " + edge.src + " " + edge.rel + " " + edge.dst;
    box.appendChild(line);
  });
}

async function loadCards() {
  const payload = await scout.call("note.list", { kind: "card", limit: 50 });
  const items = payload.items || [];
  state.cards = items;
  const list = el("card-list");
  list.innerHTML = "";
  if (!items.length) {
    list.innerHTML = '<li class="hint">还没有抽背卡。让它「把要点做成抽背卡」。</li>';
    return;
  }
  items.forEach(function (item) {
    const li = document.createElement("li");
    li.innerHTML =
      esc(short(item.title || "抽背卡", 22)) +
      '<span class="meta">' + esc(short(item.created_at || "", 16)) + "</span>";
    li.addEventListener("click", function () {
      showCard(item.id);
    });
    list.appendChild(li);
  });
}

async function showCard(noteId) {
  const note = await scout.call("note.get", { note_id: noteId });
  const box = el("card-detail");
  box.innerHTML = "";
  const title = document.createElement("h3");
  title.textContent = note.title || "抽背卡";
  box.appendChild(title);
  (String(note.content || "").split("\n") || []).forEach(function (line) {
    const div = document.createElement("div");
    if (line.indexOf("## Q") === 0) div.className = "q";
    div.textContent = line;
    box.appendChild(div);
  });
  const actions = document.createElement("div");
  actions.className = "actions";
  addButton(actions, "定位 Markdown", async function () {
    await scout.call("archive.reveal", { path: note.path });
  });
  box.appendChild(actions);
}

async function loadNotes(kind) {
  /* 笔记页：kind=note 的学习笔记 / kind=summary 的资料摘要。
     之前 `note.list` 只在卡片那栏传了 kind=card，其余两类写进库就再也读不到。 */
  state.noteKind = kind || state.noteKind;
  const payload = await scout.call("note.list", { kind: state.noteKind, limit: 50 });
  const items = payload.items || [];
  const list = el("note-list");
  list.innerHTML = "";
  if (!items.length) {
    list.innerHTML =
      '<li class="hint">还没有内容。让它「把这个方向写成学习笔记」或「给这篇资料做摘要」。</li>';
    return;
  }
  items.forEach(function (item) {
    const li = document.createElement("li");
    li.innerHTML =
      esc(short(item.title || "（无标题）", 22)) +
      '<span class="meta">' + esc(short(item.created_at || "", 16)) + "</span>";
    li.addEventListener("click", function () {
      showNote(item.id);
    });
    list.appendChild(li);
  });
}

async function showNote(noteId) {
  const note = await scout.call("note.get", { note_id: noteId });
  const box = el("note-detail");
  box.innerHTML = "";

  const title = document.createElement("h3");
  title.textContent = note.title || "（无标题）";
  box.appendChild(title);

  const meta = document.createElement("div");
  meta.className = "kv";
  meta.textContent =
    "类型 " + (note.kind || "-") + "｜创建 " + (note.created_at || "-");
  box.appendChild(meta);

  const body = document.createElement("div");
  body.className = "note-body";
  String(note.content || "").split("\n").forEach(function (line) {
    const div = document.createElement("div");
    if (line.indexOf("#") === 0) div.className = "q";
    div.textContent = line;
    body.appendChild(div);
  });
  box.appendChild(body);

  const refs = note.refs && note.refs.source_ids ? note.refs.source_ids : [];
  if (refs.length) {
    const chips = document.createElement("div");
    chips.className = "actions";
    refs.forEach(function (sourceId) {
      addChip(chips, short(sourceId, 12), function () {
        showSourceBySourceId(sourceId);
      });
    });
    box.appendChild(chips);
  }

  const actions = document.createElement("div");
  actions.className = "actions";
  if (note.path) {
    addButton(actions, "定位 Markdown", async function () {
      await scout.call("archive.reveal", { path: note.path });
    });
  }
  box.appendChild(actions);
}

async function renderHistory() {
  /* 全部会话的历史产物（后端 topic.list / route.list）。
     之前只能看本会话 refs 里带的产物，进程一退就再也点不开。 */
  const list = el("route-list");
  list.innerHTML = '<li class="hint">加载历史产物…</li>';
  let topics = [];
  let routes = [];
  try {
    const [topicPayload, routePayload] = await Promise.all([
      scout.call("topic.list", { limit: 30 }),
      scout.call("route.list", { limit: 30 }),
    ]);
    topics = topicPayload.items || [];
    routes = routePayload.items || [];
  } catch (error) {
    list.innerHTML = '<li class="hint is-bad">读历史产物失败：' + esc(error.message) + "</li>";
    return;
  }
  list.innerHTML = "";
  if (!topics.length && !routes.length) {
    list.innerHTML = '<li class="hint">库里还没有产物。让它「给我画一张主题地图」。</li>';
    return;
  }
  if (topics.length) {
    const head = document.createElement("li");
    head.className = "hint";
    head.textContent = "主题地图（" + topics.length + "）";
    list.appendChild(head);
  }
  topics.forEach(function (item) {
    const li = document.createElement("li");
    li.innerHTML =
      esc(short(item.topic, 18)) +
      '<span class="meta">' +
      item.nodes +
      " 节点｜" +
      esc(item.mode || "") +
      "｜" +
      esc(short(item.created_at, 16)) +
      "</span>";
    li.addEventListener("click", async function () {
      try {
        showTopic(await scout.call("topic.get", { topic_id: item.topic_id }));
      } catch (error) {
        /* 失败就保持原样，不静默换成别的产物 */
      }
    });
    list.appendChild(li);
  });
  if (routes.length) {
    const head = document.createElement("li");
    head.className = "hint";
    head.textContent = "学习路线（" + routes.length + "）";
    list.appendChild(head);
  }
  routes.forEach(function (item) {
    const li = document.createElement("li");
    li.innerHTML =
      esc(short(item.topic, 18)) +
      '<span class="meta">' +
      esc(item.level || "") +
      "｜" +
      (item.weeks || 0) +
      " 周｜" +
      item.entries +
      " 条｜" +
      esc(short(item.created_at, 16)) +
      "</span>";
    li.addEventListener("click", function () {
      showRoute(item.route_id);
    });
    list.appendChild(li);
  });
}

async function toggleHistory() {
  state.history = !state.history;
  const button = el("btn-history");
  button.textContent = state.history ? "只看本会话" : "显示全部历史产物";
  if (state.history) {
    await renderHistory();
  } else {
    await refreshSide();
  }
}

async function refreshSide() {
  await loadSources(el("search").value.trim());
  await loadCards();
  await loadNotes();
  if (state.history) {
    await renderHistory();
    return;
  }
  const payload = await scout.call("session.messages", {
    session_id: state.sessionId,
    limit: 200,
  });
  const refs = collectRefs(payload.items || []);
  await renderRoutes(refs.route_ids || []);
}

/* ------------------------------------------------------------------ 启动 */

async function boot() {
  try {
    initLayout();
    renderHealth(await scout.call("system.health", {}));
    const items = await loadSessions();
    if (items.length) await openSession(items[0].session_id);
    else await newSession();
    await loadCards();
    await loadSources("");
    await loadNotes();
    // 深链接 #settings：给排障/截图用，正常使用不受影响
    if (location.hash === "#settings") await openSettings();
  } catch (error) {
    el("status").textContent = "连接后端失败：" + error.message;
    el("status").classList.add("is-bad");
  }
}

/* ------------------------------------------------------------------ 设置 */

var UI_SCALE_KEY = "scout:uiScale";

/* 面板是**由这份声明画出来的**，字段名必须与 settings_io.FIELDS 一一对应（后端会拒未知键）。 */
var SETTINGS_SPEC = [
  {
    title: "模型",
    hint: "写回 config.yaml 并立即生效；API Key 只写进 .env，永不回显",
    fields: [
      { key: "llm.chat.model", label: "对话模型", kind: "text" },
      { key: "llm.chat.base_url", label: "接口地址", kind: "text" },
      { key: "llm.chat.temperature", label: "temperature", kind: "number", step: "0.1" },
      { key: "llm.chat.max_tokens", label: "max_tokens", kind: "number" },
      { key: "llm.chat.timeout_s", label: "超时（秒）", kind: "number" },
      { key: "llm.chat.thinking", label: "深度思考（关掉省 ~26% token，但质量会掉）", kind: "bool" },
      { key: "llm.chat.llm_cache", label: "回复缓存（评测复跑用）", kind: "bool" },
      { key: "llm.summary.model", label: "摘要模型", kind: "text" },
      { key: "llm.summary.base_url", label: "摘要接口地址", kind: "text" },
      { key: "llm.summary.max_tokens", label: "摘要 max_tokens", kind: "number" },
      { key: "llm.summary.thinking", label: "摘要档深度思考", kind: "bool" },
      { key: "llm.api_key", label: "API Key", kind: "secret" },
    ],
  },
  {
    title: "生成策略",
    hint: "关掉「自动」之后，知识地图与学习路线只在你点了按钮或明确要求时才生成",
    fields: [
      { key: "generation.auto_artifacts", label: "每轮自动生成产物", kind: "bool" },
      { key: "generation.title_by_model", label: "标题用模型总结（关＝用首句截断）", kind: "bool" },
      { key: "features.enable_topic_map", label: "允许生成知识地图", kind: "bool" },
      { key: "features.enable_route_plan", label: "允许生成学习路线", kind: "bool" },
      { key: "features.enable_mineru", label: "扫描版 PDF 走 MinerU 兜底", kind: "bool" },
      { key: "runtime.allow_clarify", label: "允许 AI 反问澄清", kind: "bool" },
    ],
  },
  {
    title: "搜索引擎",
    hint: "「可用」＝现在就会被检索用到；填了 key 保存即生效，不必重启",
    providers: true,
    fields: [
      { key: "search.searxng_base_url", label: "自建 SearxNG 地址", kind: "text" },
      { key: "search.google_cse_cx", label: "Google CSE 搜索引擎 ID（CX）", kind: "text" },
      {
        key: "search.allow_searchpage_scraping",
        label: "允许抓搜索结果页（Bing / DuckDuckGo，默认关）",
        kind: "bool",
      },
      { key: "search.tavily_api_key", label: "Tavily Key", kind: "secret" },
      { key: "search.serper_api_key", label: "Serper Key", kind: "secret" },
      { key: "search.brave_api_key", label: "Brave Key", kind: "secret" },
      { key: "search.google_cse_key", label: "Google CSE Key", kind: "secret" },
      { key: "search.s2_api_key", label: "Semantic Scholar Key", kind: "secret" },
      { key: "search.github_token", label: "GitHub Token（可选，提高额度）", kind: "secret" },
    ],
  },
  {
    title: "检索与预算",
    hint: "对应 config.yaml 里的 search.* 与 runtime.*",
    fields: [
      { key: "search.openalex_mailto", label: "OpenAlex 联系邮箱", kind: "text" },
      { key: "runtime.max_tool_calls_per_turn", label: "单轮工具预算", kind: "number" },
      { key: "runtime.max_context_chars", label: "单文档正文上限", kind: "number" },
      { key: "runtime.max_history_chars", label: "历史正文预算（字符）", kind: "number" },
      { key: "mineru.api_token", label: "MinerU Token（扫描版精准解析；空＝走免费通道）", kind: "secret" },
    ],
  },
];

var THEME_CHOICES = [
  ["auto", "跟随系统"],
  ["light", "浅色"],
  ["dark", "深色"],
];

/*: 「数据」栏的中文名 —— 后端给的是目录键名，界面别直接甩英文。 */
var USAGE_LABELS = {
  database: "数据库",
  fetch_cache: "抓取缓存",
  raw: "原件",
  text: "全文",
  notes: "笔记",
  run: "临时文件（run/）",
};

function fmtBytes(bytes) {
  var n = Number(bytes) || 0;
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
  return (n / 1024 / 1024 / 1024).toFixed(2) + " GB";
}

function fieldId(key) {
  return "set-" + key.replace(/\./g, "-");
}

/** 界面缩放：用 zoom 让正文与内边距一起缩放；同时把高度换算回来，免得底部输入框被裁掉。 */
function applyUiScale(scale) {
  var value = Math.min(1.25, Math.max(0.9, Number(scale) || 1));
  document.body.style.zoom = String(value);
  document.body.style.height = (100 / value).toFixed(2) + "vh";
  try {
    localStorage.setItem(UI_SCALE_KEY, String(value));
  } catch (error) {
    /* 只是不持久化 */
  }
  return value;
}

function loadUiScale() {
  var saved = null;
  try {
    saved = localStorage.getItem(UI_SCALE_KEY);
  } catch (error) {
    saved = null;
  }
  return applyUiScale(saved === null ? 1 : Number(saved));
}

function setDeep(target, path, value) {
  var node = target;
  for (var index = 0; index < path.length - 1; index += 1) {
    if (!node[path[index]] || typeof node[path[index]] !== "object") node[path[index]] = {};
    node = node[path[index]];
  }
  node[path[path.length - 1]] = value;
}

function renderSettingsField(field, values, secrets) {
  var row = document.createElement("label");
  row.className = "set-row";
  var name = document.createElement("span");
  name.textContent = field.label;
  row.appendChild(name);
  var input;
  if (field.kind === "bool") {
    input = document.createElement("input");
    input.type = "checkbox";
    // 只认明确的 true：值缺失时显示"未勾选"，不要假装它是开的
    input.checked = values[field.key] === true;
  } else if (field.kind === "secret") {
    input = document.createElement("input");
    input.type = "password";
    input.autocomplete = "new-password";
    input.placeholder = secrets[field.key] ? "已配置（留空则不修改）" : "未配置";
  } else {
    input = document.createElement("input");
    input.type = field.kind === "number" ? "number" : "text";
    if (field.step) input.step = field.step;
    input.value = values[field.key] === undefined || values[field.key] === null ? "" : String(values[field.key]);
  }
  input.id = fieldId(field.key);
  if (field.kind === "bool") input.className = "set-check";
  row.appendChild(input);
  return row;
}

function renderAppearance(container) {
  var group = document.createElement("div");
  group.className = "set-group";
  var head = document.createElement("h4");
  head.textContent = "外观";
  group.appendChild(head);
  var note = document.createElement("p");
  note.className = "hint";
  note.textContent = "只存在这台机器上（localStorage），不改配置文件";
  group.appendChild(note);

  var themeRow = document.createElement("div");
  themeRow.className = "set-row";
  var themeLabel = document.createElement("span");
  themeLabel.textContent = "主题";
  themeRow.appendChild(themeLabel);
  var seg = document.createElement("div");
  seg.className = "set-seg";
  THEME_CHOICES.forEach(function (pair) {
    var button = document.createElement("button");
    button.type = "button";
    button.textContent = pair[1];
    button.classList.toggle("is-on", state.theme === pair[0]);
    button.addEventListener("click", function () {
      state.theme = pair[0];
      try {
        localStorage.setItem(THEME_KEY, pair[0]);
      } catch (error) {
        /* 忽略 */
      }
      applyTheme(pair[0]);
      seg.querySelectorAll("button").forEach(function (other) {
        other.classList.toggle("is-on", other === button);
      });
    });
    seg.appendChild(button);
  });
  themeRow.appendChild(seg);
  group.appendChild(themeRow);

  var scaleRow = document.createElement("div");
  scaleRow.className = "set-row";
  var scaleLabel = document.createElement("span");
  scaleLabel.textContent = "界面缩放";
  scaleRow.appendChild(scaleLabel);
  var scaleInput = document.createElement("input");
  scaleInput.type = "range";
  scaleInput.min = "0.9";
  scaleInput.max = "1.25";
  scaleInput.step = "0.05";
  scaleInput.id = "set-ui-scale";
  scaleInput.value = String(loadUiScale());
  var scaleValue = document.createElement("span");
  scaleValue.className = "hint";
  scaleInput.addEventListener("input", function () {
    scaleValue.textContent = Math.round(applyUiScale(scaleInput.value) * 100) + "%";
  });
  scaleValue.textContent = Math.round(Number(scaleInput.value) * 100) + "%";
  var scaleWrap = document.createElement("div");
  scaleWrap.style.display = "flex";
  scaleWrap.style.gap = "8px";
  scaleWrap.style.alignItems = "center";
  scaleWrap.appendChild(scaleInput);
  scaleWrap.appendChild(scaleValue);
  scaleRow.appendChild(scaleWrap);
  group.appendChild(scaleRow);

  [["left", "左栏宽度"], ["right", "右栏宽度"]].forEach(function (pair) {
    var row = document.createElement("div");
    row.className = "set-row";
    var label = document.createElement("span");
    label.textContent = pair[1];
    row.appendChild(label);
    var input = document.createElement("input");
    input.type = "number";
    input.id = "set-width-" + pair[0];
    input.min = String(LAYOUT_MIN[pair[0]]);
    input.value = String(Math.round(state.layout[pair[0]]));
    row.appendChild(input);
    group.appendChild(row);
  });

  container.appendChild(group);
}

function renderUsage(group, payload) {
  var body = group.querySelector(".set-usage");
  body.textContent = "";
  var usage = (payload && payload.usage) || {};
  Object.keys(usage).forEach(function (name) {
    var row = document.createElement("div");
    row.className = "usage-row";
    var label = document.createElement("span");
    label.textContent = USAGE_LABELS[name] || name;
    label.title = usage[name].path || "";
    var size = document.createElement("span");
    size.className = "hint";
    size.textContent = fmtBytes(usage[name].bytes) + " · " + usage[name].files + " 个文件";
    row.appendChild(label);
    row.appendChild(size);
    body.appendChild(row);
  });
}

async function openSettings() {
  var payload = await scout.call("settings.get", {});
  var values = payload.values || {};
  var secrets = payload.secrets_configured || {};
  var body = el("settings-body");
  body.textContent = "";
  SETTINGS_SPEC.forEach(function (group) {
    var box = document.createElement("div");
    box.className = "set-group";
    var head = document.createElement("h4");
    head.textContent = group.title;
    box.appendChild(head);
    var note = document.createElement("p");
    note.className = "hint";
    note.textContent = group.hint;
    box.appendChild(note);
    if (group.providers) {
      // 状态表放在这组最前面：先让用户看到"哪些能用"，再看填 key 的地方
      var statusBox = document.createElement("div");
      statusBox.className = "provider-status";
      box.appendChild(statusBox);
      renderProviderStatus(statusBox);
    }
    group.fields.forEach(function (field) {
      box.appendChild(renderSettingsField(field, values, secrets));
    });
    body.appendChild(box);
  });

  renderAppearance(body);

  // MinerU：开关开着却没配 token ＝ 扫描版一定失败（免费通道当前不可用）。
  // 这会让人以为是"这个 PDF 没内容"，所以必须在设置里提前讲清楚，而不是等失败。
  if (values["features.enable_mineru"] !== false && !secrets["mineru.api_token"]) {
    var warn = document.createElement("p");
    warn.className = "hint set-bad";
    warn.textContent =
      "注意：扫描版走 MinerU 已打开，但没有配 MinerU Token —— 免 token 的 Agent 通道" +
      "实测当前返回 model service is temporarily unavailable，所以扫描版 PDF 会解析失败" +
      "（并如实记成 mineru_failed）。填一个 Token 就会改走精准接口。";
    body.appendChild(warn);
  }

  var dataGroup = document.createElement("div");
  dataGroup.className = "set-group";
  var dataHead = document.createElement("h4");
  dataHead.textContent = "数据";
  dataGroup.appendChild(dataHead);
  var dataNote = document.createElement("p");
  dataNote.className = "hint";
  dataNote.textContent = "资料本体（原件/全文/笔记）不在可清理范围内，只清缓存类目录";
  dataGroup.appendChild(dataNote);
  var usageBox = document.createElement("div");
  usageBox.className = "set-usage";
  dataGroup.appendChild(usageBox);
  var cleanRow = document.createElement("div");
  cleanRow.className = "set-row";
  var cleanLabel = document.createElement("span");
  cleanLabel.textContent = "清理";
  cleanRow.appendChild(cleanLabel);
  var buttons = document.createElement("div");
  buttons.className = "set-seg";
  Object.keys(payload.cleanable || {}).forEach(function (target) {
    var button = document.createElement("button");
    button.type = "button";
    button.textContent = target === "fetch_cache" ? "清 7 天前缓存" : "清 run/ 临时文件";
    button.title =
      target === "fetch_cache"
        ? "只删够旧的缓存：全清会让下次检索重新下载一遍"
        : payload.cleanable[target];
    button.addEventListener("click", async function () {
      if (!window.confirm("确定清理「" + button.title + "」？")) return;
      // 缓存按保留期删（默认 7 天）；run/ 临时文件是全清
      var params = { target: target };
      if (target === "fetch_cache") params.older_than_days = 7;
      var result = await scout.call("settings.cleanup", params);
      el("settings-note").textContent =
        "已清理 " + result.removed.files + " 个文件，释放 " + fmtBytes(result.removed.bytes);
      renderUsage(dataGroup, await scout.call("settings.usage", {}));
    });
    buttons.appendChild(button);
  });
  // 整理数据库：删会话/删资料不会让库文件变小，得显式 VACUUM（只整理、不删数据）
  var vacuum = document.createElement("button");
  vacuum.type = "button";
  vacuum.textContent = "整理数据库";
  vacuum.title = "VACUUM + PRAGMA optimize：只整理不删数据；大库会短暂锁库";
  vacuum.addEventListener("click", async function () {
    el("settings-note").textContent = "正在整理数据库（大库可能要几十秒）…";
    try {
      var result = await scout.call("settings.optimize", {});
      var saved = result.saved || 0;
      el("settings-note").textContent =
        "数据库已整理：" +
        fmtBytes(result.before) +
        " → " +
        fmtBytes(result.after) +
        (saved > 0 ? "（释放 " + fmtBytes(saved) + "）" : "（本来就没有碎片可收）");
    } catch (error) {
      el("settings-note").textContent = "整理失败：" + error.message;
    }
    renderUsage(dataGroup, await scout.call("settings.usage", {}));
  });
  buttons.appendChild(vacuum);
  cleanRow.appendChild(buttons);
  dataGroup.appendChild(cleanRow);
  body.appendChild(dataGroup);

  el("settings-path").textContent = "配置文件：" + (payload.config_path || "");
  el("settings-note").textContent = "";
  renderUsage(dataGroup, await scout.call("settings.usage", {}));
  body.scrollTop = 0; // 每次打开都从「模型」那一栏开始看，不留在上次滚到的位置
  el("settings").hidden = false;
}

function closeSettings() {
  el("settings").hidden = true;
}

/*: 来源不可用的原因 → 人话（后端给的是错误码风格，界面别直接甩给用户）。 */
var REASON_LABELS = {
  ok: "可用",
  missing_api_key: "缺 key",
  missing_config: "缺配置",
  blocked_by_robots: "被 robots 拦",
  off: "已关闭",
};

// 哪些搜索引擎能用、不能用的原因、怎么才能用 —— 填完 key 保存后会自动重画这张表
async function renderProviderStatus(box) {
  var payload = null;
  try {
    payload = await scout.call("search.providers", {});
  } catch (error) {
    box.textContent = "读取来源状态失败：" + error.message;
    return;
  }
  var items = payload.items || [];
  var head = document.createElement("p");
  head.className = "hint";
  head.textContent =
    "可用 " + payload.usable_count + " / " + items.length + " 个来源；" + (payload.note || "");
  box.appendChild(head);
  items.forEach(function (item) {
    var row = document.createElement("div");
    row.className = "provider-row" + (item.usable ? " is-usable" : "");
    var name = document.createElement("span");
    name.className = "provider-name";
    name.textContent = item.name;
    var kind = document.createElement("span");
    kind.className = "provider-kind";
    kind.textContent = item.kind;
    var badge = document.createElement("span");
    badge.className = "provider-badge" + (item.usable ? " is-ok" : " is-off");
    badge.textContent = REASON_LABELS[item.reason] || item.reason;
    var note = document.createElement("span");
    note.className = "provider-note";
    note.textContent =
      item.note + (item.env_var ? "（环境变量 " + item.env_var + "）" : "");
    row.appendChild(name);
    row.appendChild(kind);
    row.appendChild(badge);
    row.appendChild(note);
    box.appendChild(row);
  });
}

async function saveSettings() {
  var updates = {};
  var note = el("settings-note");
  SETTINGS_SPEC.forEach(function (group) {
    group.fields.forEach(function (field) {
      var input = el(fieldId(field.key));
      if (!input) return;
      var text = String(input.value === undefined ? "" : input.value).trim();
      if (field.kind === "bool") {
        setDeep(updates, field.key.split("."), input.checked);
        return;
      }
      if (field.kind === "secret" && !text) return; // 留空＝不修改
      if (!text) return;
      setDeep(updates, field.key.split("."), field.kind === "number" ? Number(text) : text);
    });
  });

  [["left", "left"], ["right", "right"]].forEach(function (pair) {
    var input = el("set-width-" + pair[0]);
    if (!input || !input.value) return;
    var width = Math.max(LAYOUT_MIN[pair[1]], Number(input.value) || 0);
    state.layout[pair[1]] = width;
  });
  applyUiScale(el("set-ui-scale") ? el("set-ui-scale").value : 1);
  applyLayout(state.layout.left, state.layout.right, true);

  if (!Object.keys(updates).length) {
    note.textContent = "没有要保存的改动";
    note.className = "hint set-bad";
    return;
  }
  try {
    var result = await scout.call("settings.update", { updates });
    var notes = (result.notes || []).join("；");
    note.textContent = "已保存并生效" + (notes ? "（" + notes + "）" : "");
    note.className = "hint set-ok";
    await openSettings();
  } catch (error) {
    note.textContent = "保存失败：" + error.message;
    note.className = "hint set-bad";
  }
}

/* ---------------------------------------------------------- 会话行菜单 */

function rowMenuButton(item, li) {
  var button = document.createElement("button");
  button.type = "button";
  button.className = "row-menu-btn";
  button.textContent = "⋯";
  button.title = "更多：置顶 / 重命名 / 分享 / 删除";
  button.addEventListener("click", function (event) {
    event.stopPropagation(); // 别顺手把会话打开/收起
    toggleRowMenu(item, li);
  });
  return button;
}

function toggleRowMenu(item, li) {
  var already = li.querySelector(".row-menu");
  closeRowMenus();
  if (already) return; // 再点一次就是收起
  var menu = document.createElement("div");
  menu.className = "row-menu";
  var rows = [
    [item.pinned ? "取消置顶" : "置顶", function () { pinSession(item); }, false],
    ["重命名", function () { closeRowMenus(); startRename(item, li); }, false],
    ["分享（导出 Markdown）", function () { exportSession(item); }, false],
    ["", null, false],
    ["删除会话", function () { deleteSession(item); }, true],
  ];
  rows.forEach(function (row) {
    if (!row[1]) {
      var sep = document.createElement("div");
      sep.className = "sep";
      menu.appendChild(sep);
      return;
    }
    var button = document.createElement("button");
    button.type = "button";
    button.textContent = row[0];
    button.className = row[2] ? "is-danger" : "";
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      row[1]();
    });
    menu.appendChild(button);
  });
  li.appendChild(menu);
  // 点别处就收起（菜单项自己会 stopPropagation，所以只有真的点到外面才关）
  setTimeout(function () {
    document.addEventListener("click", closeRowMenus, { once: true });
  }, 0);
}

function closeRowMenus() {
  document.querySelectorAll(".row-menu").forEach(function (node) {
    node.remove();
  });
}

async function pinSession(item) {
  closeRowMenus();
  try {
    await scout.call("session.pin", { session_id: item.session_id, pinned: !item.pinned });
  } catch (error) {
    el("status").textContent = "置顶失败：" + error.message;
  }
  await loadSessions();
}

async function exportSession(item) {
  closeRowMenus();
  el("status").textContent = "正在导出…";
  try {
    var result = await scout.call("session.export", { session_id: item.session_id });
    showTextModal(item.title || "会话", result.text, result.path);
    el("status").textContent = "已导出 " + result.chars + " 字";
  } catch (error) {
    el("status").textContent = "导出失败：" + error.message;
  }
}

async function deleteSession(item) {
  closeRowMenus();
  var what = "「" + (item.title || "未命名会话") + "」（" + (item.message_count || 0) + " 条消息）";
  var ok = window.confirm(
    "删除 " + what + "？\n\n" +
      "会同时删掉这次会话产生的笔记与产物；资料库里的资料、花费账本不受影响。\n" +
      "（删之前可以先用「分享」导出成 Markdown）"
  );
  if (!ok) return;
  try {
    var result = await scout.call("session.delete", { session_id: item.session_id });
    var removed = result.removed || {};
    var parts = [];
    if (removed.messages) parts.push(removed.messages + " 条消息");
    if (removed.notes) parts.push(removed.notes + " 条笔记");
    var artifacts = (removed.topic_maps || 0) + (removed.learning_routes || 0);
    if (artifacts) parts.push(artifacts + " 个产物");
    el("status").textContent = "已删除" + (parts.length ? "：" + parts.join("、") : "该会话");
    var list = await loadSessions();
    if (item.session_id === state.sessionId) {
      if (list.length) await openSession(list[0].session_id);
      else await newSession();
    }
  } catch (error) {
    el("status").textContent = "删除失败：" + error.message;
  }
}

/* 文本弹层：导出的 Markdown 既能一键复制，也能看到落盘位置 */
function showTextModal(title, text, path) {
  el("textmodal-title").textContent = title;
  el("textmodal-body").value = text || "";
  el("textmodal-path").textContent = path ? "已保存：" + path : "";
  el("textmodal-note").textContent = "";
  el("textmodal-note").className = "hint";
  el("textmodal").hidden = false;
  el("textmodal-body").focus();
  el("textmodal-body").select();
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

/** 产物按需生成：点按钮就是"我明确要了"，用一条真实的用户消息把请求发出去 ——
 *  这样"用户要了什么"在会话记录里看得见，不是偷偷调工具。 */
var ASK_TEXTS = {
  map: "请基于本次会话已经取到的资料，把主题整理成知识地图（调用 topic.build_map）。",
  route: "请基于本次会话已经取到的资料，生成一条 4 周学习路线（调用 route.plan）。",
  cards: "请把本次会话的关键概念做成抽背卡（调用 note.save，kind 用 card）。",
};

function askArtifact(kind) {
  var text = ASK_TEXTS[kind];
  if (!text) return;
  if (state.sending) {
    el("status").textContent = "上一轮还没结束，等它跑完再点";
    return;
  }
  send(text);
}

function wire() {
  el("quickbar").addEventListener("click", function (event) {
    var target = event.target.closest("button[data-ask]");
    if (target) askArtifact(target.dataset.ask);
  });
  el("btn-textmodal-close").addEventListener("click", closeTextModal);
  el("btn-textmodal-copy").addEventListener("click", copyTextModal);
  el("textmodal").addEventListener("click", function (event) {
    if (event.target === el("textmodal")) closeTextModal();
  });
  el("btn-settings").addEventListener("click", function () {
    openSettings().catch(function (error) {
      el("status").textContent = "打开设置失败：" + error.message;
    });
  });
  el("btn-settings-close").addEventListener("click", closeSettings);
  el("btn-settings-save").addEventListener("click", saveSettings);
  el("settings").addEventListener("click", function (event) {
    if (event.target === el("settings")) closeSettings();
  });
  el("btn-theme").addEventListener("click", cycleTheme);
  el("btn-history").addEventListener("click", toggleHistory);
  el("btn-note-kind-note").addEventListener("click", function () {
    loadNotes("note");
  });
  el("btn-note-kind-summary").addEventListener("click", function () {
    loadNotes("summary");
  });
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
      if (state.theme === "auto") applyTheme("auto");
    });
  }

  el("composer").addEventListener("submit", function (event) {
    event.preventDefault();
    if (state.sending) return;  // 上一轮没跑完别叠加（后端也会拦，但界面该先拦住）
    const input = el("input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    send(text);
  });

  el("btn-stop").addEventListener("click", stopTurn);

  el("input").addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      el("composer").dispatchEvent(new Event("submit"));
    }
  });

  el("btn-new").addEventListener("click", function () {
    newSession();
  });
  el("btn-refresh").addEventListener("click", async function () {
    await loadSessions();
    if (state.sessionId) await openSession(state.sessionId);
    await refreshSide();
  });
  el("btn-search").addEventListener("click", function () {
    loadSources(el("search").value.trim());
  });
  el("search").addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      loadSources(el("search").value.trim());
    }
  });

  document.querySelectorAll(".tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      document.querySelectorAll(".tab").forEach(function (other) {
        other.classList.toggle("is-active", other === tab);
      });
      document.querySelectorAll(".pane").forEach(function (pane) {
        pane.classList.toggle("is-active", pane.id === "pane-" + tab.dataset.tab);
      });
    });
  });
}

var saved = null;
try {
  saved = localStorage.getItem(THEME_KEY);
} catch (error) {
  saved = null;
}
applyTheme(saved === "light" || saved === "dark" ? saved : "auto");
loadUiScale(); /* 布局由 boot 里的 initLayout 负责，这里只管缩放 */
wire();
window.addEventListener("pywebviewready", boot);
