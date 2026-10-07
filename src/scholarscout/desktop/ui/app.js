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
  practice: null,
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
  // 主题开关在设置面板里（顶栏那个按钮已按用户要求去掉）
}

/* 主题循环：设置面板的主题分段控件在用（顶栏那个按钮已去掉） */
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
        (usage.cached_tokens ? "（命中缓存 " + fmtTokens(usage.cached_tokens) + "）" : ""),
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
      // 缓存命中的输入单独说：它和未命中的输入单价差一个量级，合在一起看会误判省钱效果
      (tokens.cached ? "（其中缓存命中 " + fmtTokens(tokens.cached) + "）" : "") +
      " ／ 模型调用 " + usage.calls + " 次" + (usage.est_cost ? " ／ 约 " + usage.est_cost : "");
    node.title =
      "数据来自 usage_records；未配置单价故不折算金额" +
      (usage.cache_hit_rate === null || usage.cache_hit_rate === undefined
        ? "。本次服务端未返回缓存字段，故不给命中率（不按 0 算）"
        : "。输入缓存命中率 " + Math.round(usage.cache_hit_rate * 100) + "%（前缀稳定时该值应随轮次上升）");
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
      esc(item.tier === null || item.tier === undefined ? "未判权" : item.tier) +
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
  // 「⋯」放在最前面：它是这一块的工具栏，固定在右上，不随内容滚走
  // （先清空再插 —— 顺序反了的话 clear 会把它一起抹掉）
  box.appendChild(detailActions(docId, doc));
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
}

/* 摘要区右上角的「⋯」：三个打开方式收在同一个按钮里。
   放在**顶部**而不是末尾 —— 放末尾时内容一长就得滚动才看得见，等于没有这个按钮。 */
function detailActions(docId, doc) {
  const actions = document.createElement("div");
  actions.className = "detail-actions";
  const menuButton = document.createElement("button");
  menuButton.type = "button";
  menuButton.className = "mini";
  menuButton.textContent = "⋯";
  menuButton.title = "打开文件 / 在文件管理器定位 / 打开源网站";
  menuButton.addEventListener("click", function (event) {
    event.stopPropagation();
    openFloatingMenu(menuButton, [
      ["打开文件", function () {
        scout.call("archive.open_raw", { doc_id: docId });
      }],
      ["在文件管理器中定位", function () {
        scout.call("archive.reveal", { doc_id: docId, which: "raw" });
      }],
      ["打开源网站", function () {
        openExternalUrl(doc.url);
      }],
    ]);
  });
  actions.appendChild(menuButton);
  return actions;
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
  // ⑧：时间线/日历视图（纯前端，数据就是这条路线的 plan）
  const visual = document.createElement("div");
  visual.className = "route-visual";

  const title = document.createElement("h3");
  title.textContent = "学习路线：" + (payload.topic || "");
  box.appendChild(title);

  const meta = document.createElement("div");
  meta.className = "kv";
  const stats = plan.stats || {};
  const version = payload.version || 1;
  meta.textContent =
    "水平 " + (payload.level || "-") +
    "｜周数 " + (payload.weeks || (plan.weeks || []).length) +
    "｜生成方式 " + (plan.mode || "-") +
    "｜条目 " + (stats.items === undefined ? "-" : stats.items) +
    "｜无出处 " + (stats.ungrounded_items === undefined ? "-" : stats.ungrounded_items) +
    // 改过就显示"第 N 版"：增量修改就地升版，用户得看得见自己改过几轮
    (version > 1 ? "｜第 " + version + " 版（改过 " + (version - 1) + " 次）" : "｜第 1 版");
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

  // ⑧ 可视化：默认时间线，可切日历（点某天看那一周要做什么）
  renderRouteVisual(visual, [{
    id: routeId,
    topic: payload.topic || "",
    created_at: payload.created_at || "",
    weeks: plan.weeks || [],
  }]);
  box.appendChild(visual);

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
  refreshCardStats();
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

/* ------------------------------------------------------------ 抽背「练」模式 */

/*: 三档评价的中文（与后端 card_reviews.rating 的取值一一对应） */
var RATING_LABELS = { good: "会了", fuzzy: "模糊", bad: "不会" };

// 卡片栏那行小字：练过几张、记住率（分母只算评过的卡）
async function refreshCardStats() {
  const node = el("practice-stats");
  if (!node) return;
  try {
    const stats = await scout.call("note.stats", {});
    const bits = [];
    // 间隔重复（B6）：先说"今天该练多少"——这是用户唯一需要现在做决定的信息
    if (stats.due) {
      bits.push(
        "今天该练 " + stats.due + " 张" + (stats.due_new ? "（新卡 " + stats.due_new + "）" : ""),
      );
    } else if (stats.total_cards) {
      bits.push("今天没有到期的卡（共 " + stats.total_cards + " 张）");
    }
    if (stats.reviewed) {
      bits.push(
        "练过 " +
          stats.reviewed +
          " 张｜记住率 " +
          (stats.recall_rate === null ? "—" : Math.round(stats.recall_rate * 100) + "%"),
      );
    }
    node.textContent = bits.join("｜");
  } catch (error) {
    node.textContent = "";
  }
}

// 开始抽背：不给 noteId ＝ 把所有卡片行的卡串成一轮；只练「不会/模糊」是可选的第二轮
async function startPractice(noteId, options) {
  const opts = options || {};
  const cards = [];
  const collect = function (id, note) {
    (note.cards || []).forEach(function (card) {
      cards.push({
        note_id: id,
        index: card.index,
        front: card.front,
        back: card.back,
        rating: card.rating,
      });
    });
  };
  if (opts.dueOnly) {
    // B6：只练到期的。直接用 `note.due` 的结果 —— 它已经带了正反面，
    // 不必再按笔记逐条 `note.get`（少 n 次往返，卡多时差别明显）。
    const due = await scout.call("note.due", { limit: 50 });
    (due.items || []).forEach(function (item) {
      cards.push({
        note_id: item.note_id,
        index: item.card_index,
        front: item.front,
        back: item.back,
        rating: null,
        is_new: item.is_new,
      });
    });
  } else if (noteId) {
    collect(noteId, await scout.call("note.get", { note_id: noteId }));
  } else {
    const payload = await scout.call("note.list", { kind: "card", limit: 50 });
    for (const item of payload.items || []) {
      collect(item.id, await scout.call("note.get", { note_id: item.id }));
    }
  }
  const pool = opts.onlyWeak
    ? cards.filter(function (card) { return card.rating === "bad" || card.rating === "fuzzy"; })
    : cards;
  if (!pool.length) {
    window.alert(
      opts.onlyWeak
        ? "没有「不会 / 模糊」的卡了 —— 这一轮不用练。"
        : opts.dueOnly
          ? "今天没有到期的卡。想全练一遍就点「开始抽背」。"
          : "还没有抽背卡。先让它「把要点做成抽背卡」。"
    );
    return;
  }
  state.practice = {
    cards: pool,
    index: 0,
    revealed: false,
    onlyWeak: !!opts.onlyWeak,
    round: { good: 0, fuzzy: 0, bad: 0 },
  };
  el("practice").hidden = false;
  renderPractice();
}

function closePractice() {
  state.practice = null;
  el("practice").hidden = true;
  refreshCardStats();
}

function renderPractice() {
  const practice = state.practice;
  const body = el("practice-body");
  if (!practice) return;
  body.innerHTML = "";
  el("btn-practice-reveal").disabled = false;
  if (practice.index >= practice.cards.length) {
    renderPracticeSummary();
    return;
  }
  const card = practice.cards[practice.index];
  el("practice-progress").textContent = practice.index + 1 + " / " + practice.cards.length;
  // B6：上一张评完后的排期一直显示，直到下一张评完 —— 让"间隔重复在起作用"看得见
  if (practice.lastSchedule) {
    const line = document.createElement("p");
    line.className = "hint";
    line.textContent =
      "上一张「" +
      short(practice.lastSchedule.front || "", 14) +
      "」下次复习：" +
      practice.lastSchedule.label +
      (practice.lastSchedule.lapses
        ? "（累计答错 " + practice.lastSchedule.lapses + " 次）"
        : "");
    body.appendChild(line);
  }
  const front = document.createElement("div");
  front.className = "practice-front";
  front.textContent = card.front;
  body.appendChild(front);
  if (practice.revealed) {
    const back = document.createElement("div");
    back.className = "practice-back";
    back.textContent = card.back;
    body.appendChild(back);
  } else {
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = "先在心里回答，再点「看答案」（Enter）。";
    body.appendChild(hint);
  }
  const ratings = document.createElement("div");
  ratings.className = "practice-ratings";
  [
    ["bad", "不会（1）"],
    ["fuzzy", "模糊（2）"],
    ["good", "会了（3）"],
  ].forEach(function (pair) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "rate-" + pair[0];
    button.textContent = pair[1];
    // 没看答案不许自评：否则会一路点"会了"，数据就没有意义
    button.disabled = !practice.revealed;
    button.addEventListener("click", function () {
      ratePractice(pair[0]);
    });
    ratings.appendChild(button);
  });
  body.appendChild(ratings);
  if (card.rating) {
    const previous = document.createElement("p");
    previous.className = "hint";
    previous.textContent = "上次评价：" + (RATING_LABELS[card.rating] || card.rating);
    body.appendChild(previous);
  }
  el("btn-practice-reveal").textContent = practice.revealed ? "隐藏答案" : "看答案";
}

function revealPractice() {
  if (!state.practice) return;
  state.practice.revealed = !state.practice.revealed;
  renderPractice();
}

async function ratePractice(rating) {
  const practice = state.practice;
  if (!practice || !practice.revealed || practice.index >= practice.cards.length) return;
  const card = practice.cards[practice.index];
  practice.round[rating] = (practice.round[rating] || 0) + 1;
  practice.index += 1;
  practice.revealed = false;
  renderPractice();
  try {
    const result = await scout.call("note.review", {
      note_id: card.note_id,
      card_index: card.index,
      rating: rating,
    });
    // B6：把 SM-2 算出来的下一次如实回显（不显示的话，用户根本不知道间隔重复有没有生效）
    if (state.practice && result && result.interval_label) {
      state.practice.lastSchedule = {
        front: card.front,
        label: result.interval_label,
        lapses: result.lapses || 0,
      };
      renderPractice();
    }
  } catch (error) {
    // 记不上要说出来：否则用户以为练过了、其实没记上
    const warn = document.createElement("p");
    warn.className = "hint set-bad";
    warn.textContent = "这一张的评价没记上：" + error.message;
    el("practice-body").appendChild(warn);
  }
}

function renderPracticeSummary() {
  const practice = state.practice;
  const body = el("practice-body");
  const total = practice.cards.length;
  const good = practice.round.good || 0;
  el("practice-progress").textContent = "本轮结束";
  const line = document.createElement("div");
  line.className = "practice-front";
  line.textContent =
    "本轮 " +
    total +
    " 张：会了 " +
    good +
    "／模糊 " +
    (practice.round.fuzzy || 0) +
    "／不会 " +
    (practice.round.bad || 0) +
    "（记住率 " +
    (total ? Math.round((good / total) * 100) : 0) +
    "%）";
  body.appendChild(line);
  const actions = document.createElement("div");
  actions.className = "practice-ratings";
  addButton(actions, "再来一轮（全部）", function () {
    startPractice(null, {});
  });
  addButton(actions, "只练不会 / 模糊的", function () {
    startPractice(null, { onlyWeak: true });
  });
  addButton(actions, "结束", closePractice);
  body.appendChild(actions);
  el("btn-practice-reveal").disabled = true;
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

/* 跨会话产物列表。原本由「显示全部历史产物」按钮触发，那个按钮已按用户要求去掉
   （改为按会话自动更新）。这个函数暂时没有调用点 —— 留给「个人中心」的汇总复用。 */
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

async function refreshSide() {
  await loadSources(el("search").value.trim());
  await loadCards();
  await loadNotes();
  await loadBoard();
  const payload = await scout.call("session.messages", {
    session_id: state.sessionId,
    limit: 200,
  });
  const refs = collectRefs(payload.items || []);
  await renderRoutes(refs.route_ids || []);
}

/* ------------------------------------------------- 研究看板（B2 证据表 / B4 时间线 / B7） */

async function loadBoard() {
  const summary = el("board-summary");
  if (!summary) return;
  if (!state.sessionId) {
    summary.textContent = "";
    return;
  }
  let board;
  try {
    board = await scout.call("research.board", { session_id: state.sessionId });
  } catch (err) {
    summary.textContent = "看板读取失败：" + (err && err.message ? err.message : String(err));
    return;
  }
  renderBoardSummary(board);
  renderBoardQuestions(board);
  renderBoardGaps(board);
  renderBoardEvidence(board);
  renderBoardTimeline(board);
  renderBoardArtifacts(board);
  loadProfile();
  loadMemory();
}

async function loadMemory() {
  const list = el("memory-list");
  if (!list) return;
  let payload;
  try {
    payload = await scout.call("memory.list", {});
  } catch (error) {
    list.innerHTML = '<li class="hint">记忆读取失败</li>';
    return;
  }
  const items = payload.items || [];
  el("memory-note").textContent = items.length
    ? "（" + items.length + " 条；每条都能删，误抽不会一直污染下去）"
    : "（还没有记忆。攒够几轮对话后会自动整理一次，也可以点「从现在整理」）";
  list.innerHTML = "";
  items.forEach(function (item) {
    const labels = { preference: "偏好", fact: "背景", goal: "目标" };
    const li = boardItem(
      '<span class="badge">' +
        esc(labels[item.kind] || item.kind) +
        "</span> " +
        esc(item.text),
    );
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "ghost board-mini";
    remove.textContent = "删除";
    remove.addEventListener("click", async function (event) {
      event.stopPropagation();
      if (!window.confirm("删掉这条记忆？\n\n" + item.text)) return;
      await scout.call("memory.remove", { fact_id: item.id });
      loadMemory();
    });
    li.appendChild(remove);
    list.appendChild(li);
  });
}

async function addMemory() {
  const input = el("memory-input");
  const text = (input.value || "").trim();
  if (!text) return;
  try {
    const result = await scout.call("memory.add", { text: text, kind: "fact" });
    input.value = "";
    if (!result.created) {
      window.alert("已经有一条相近的记忆了，没重复添加。");
    }
    loadMemory();
  } catch (error) {
    window.alert("没记住：" + (error && error.message ? error.message : String(error)));
  }
}

async function extractMemory() {
  if (!state.sessionId) {
    window.alert("先打开一个会话。");
    return;
  }
  const button = el("btn-memory-extract");
  button.disabled = true;
  button.textContent = "整理中…";
  try {
    const result = await scout.call("memory.extract", { session_id: state.sessionId });
    const added = (result.added || []).length;
    const skipped = (result.skipped || []).length;
    window.alert(
      result.reason
        ? "这次没整理出东西：" + result.reason
        : "新增 " +
            added +
            " 条记忆" +
            (skipped ? "，跳过 " + skipped + " 条（已有相近的）" : "") +
            "。\n\n空表也是正常结果 —— 没有值得长期记住的东西就不该记。",
    );
    loadMemory();
  } catch (error) {
    window.alert("整理失败：" + (error && error.message ? error.message : String(error)));
  } finally {
    button.disabled = false;
    button.textContent = "从现在整理";
  }
}

async function loadProfile() {
  const overview = el("profile-overview");
  if (!overview) return;
  let profile;
  try {
    profile = await scout.call("learner.profile", {});
  } catch (error) {
    overview.textContent = "画像读取失败：" + (error && error.message ? error.message : String(error));
    return;
  }
  const data = profile.overview || {};
  const bits = [];
  if (data.topics) bits.push("做过 " + data.topics + " 个主题");
  if (data.routes) bits.push("路线 " + data.routes + " 条");
  if (data.maps) bits.push("地图 " + data.maps + " 份");
  if (data.reviewed) {
    bits.push(
      "练过 " +
        data.reviewed +
        "/" +
        data.cards +
        " 张" +
        (data.mastery_rate === null ? "" : "（熟 " + Math.round(data.mastery_rate * 100) + "%）"),
    );
  }
  if (data.active_days_recent) bits.push("近 " + data.window_days + " 天学了 " + data.active_days_recent + " 天");
  overview.textContent = bits.length
    ? bits.join("｜") + "。这份画像会随每轮请求注入，所以产物按你的水平来。"
    : "还没有学习痕迹 —— 做完一份地图/路线、或练几张抽背卡，这里才有东西可汇总。";
  el("profile-note").textContent = bits.length ? "（零模型调用，从库里的排期与产物算出来）" : "";

  const topics = el("profile-topics");
  topics.innerHTML = "";
  (profile.topics || []).forEach(function (item) {
    topics.appendChild(
      boardItem(
        esc(item.topic) +
          '<span class="meta">路线 ' +
          item.routes +
          " ／ 地图 " +
          item.maps +
          (item.weeks ? " ／ 最长 " + item.weeks + " 周" : "") +
          "</span>",
      ),
    );
  });

  const groups = el("profile-groups");
  groups.innerHTML = "";
  (profile.card_groups || []).forEach(function (item) {
    const rate = item.mastery_rate === null ? "—" : Math.round(item.mastery_rate * 100) + "%";
    groups.appendChild(
      boardItem(
        esc(item.group) +
          ' <span class="badge ' +
          (item.weak ? "badge-weak" : "badge-strong") +
          '">熟 ' +
          rate +
          "</span>" +
          '<span class="meta">熟 ' +
          item.mastered +
          " ／ 复习中 " +
          item.reviewing +
          " ／ 薄弱 " +
          item.weak +
          (item.cards - item.reviewed ? " ／ 未练 " + (item.cards - item.reviewed) : "") +
          "</span>",
      ),
    );
  });
}

function renderBoardArtifacts(board) {
  const list = el("board-artifacts");
  list.innerHTML = "";
  const artifacts = board.artifacts || {};
  const rows = [];
  (artifacts.routes || []).forEach(function (item) {
    rows.push("学习路线：" + (item.topic || "") + "（" + item.weeks + " 周，第 " + (item.version || 1) + " 版）");
  });
  (artifacts.maps || []).forEach(function (item) {
    rows.push("知识地图：" + (item.topic || ""));
  });
  (artifacts.notes || []).forEach(function (item) {
    const label = item.kind === "card" ? "抽背卡" : item.kind === "summary" ? "资料摘要" : "学习笔记";
    rows.push(label + "：" + (item.title || ""));
  });
  if (!rows.length) {
    list.innerHTML =
      '<li class="hint">本会话还没有产物。要产物就在输入框上方点对应的按钮（默认不会自动生成）。</li>';
    return;
  }
  rows.forEach(function (text) {
    list.appendChild(boardItem(esc(text)));
  });
}

function renderBoardSummary(board) {
  const counts = board.counts || {};
  const evidence = board.evidence || {};
  el("board-summary").textContent = [
    "来源 " + (counts.sources || 0),
    "有摘要 " + (counts.with_summary || 0),
    "未取正文 " + (counts.without_text || 0),
    "已搜查询 " + (counts.queries || 0),
    "未覆盖子问题 " + (counts.open_questions || 0),
    "弱证据 " + (evidence.weak_count || 0),
  ].join(" ／ ");
}

function boardItem(html) {
  const li = document.createElement("li");
  li.innerHTML = html;
  return li;
}

function renderBoardQuestions(board) {
  const list = el("board-questions");
  list.innerHTML = "";
  const questions = board.questions || [];
  el("board-goal").textContent = board.goal ? "目标：" + board.goal : "（还没有记录目标）";
  if (!questions.length) {
    list.innerHTML =
      '<li class="hint">还没有子问题清单。开局拆解未跑到时（或关掉了 plan_by_model），' +
      "这里会是空的，缺口清单仍按年份/来源族给出。</li>";
    return;
  }
  questions.forEach(function (item) {
    const covered = item.status === "covered";
    list.appendChild(
      boardItem(
        '<span class="board-idx">' +
          item.index +
          ".</span> " +
          esc(item.text) +
          ' <span class="badge ' +
          (covered ? "badge-strong" : "badge-weak") +
          '">' +
          (covered ? "已覆盖" : "缺口") +
          "</span>",
      ),
    );
  });
}

function renderBoardGaps(board) {
  const list = el("board-gaps");
  list.innerHTML = "";
  const gaps = board.gaps || [];
  if (!gaps.length) {
    list.innerHTML = '<li class="hint">暂时没有明显缺口。</li>';
    return;
  }
  gaps.forEach(function (gap) {
    list.appendChild(
      boardItem(
        '<code class="board-kind">' +
          esc(gap.kind) +
          "</code> " +
          esc(gap.detail) +
          '<div class="hint board-suggest">→ ' +
          esc(gap.suggest) +
          "</div>",
      ),
    );
  });
}

function sourceChips(sources) {
  const wrap = document.createElement("div");
  wrap.className = "board-srcs";
  (sources || []).forEach(function (item) {
    const chip = document.createElement("span");
    chip.className = "chip" + (item.has_summary ? " chip-strong" : "");
    chip.textContent =
      short(item.title || item.source_id, 26) + (item.year ? "（" + item.year + "）" : "");
    chip.title = (item.has_summary ? "有摘要" : "只有标题") + " ／ 点击看这条来源的详情";
    chip.addEventListener("click", function (event) {
      event.stopPropagation();
      showSourceBySourceId(item.source_id);
    });
    wrap.appendChild(chip);
  });
  if (!wrap.childNodes.length) {
    wrap.innerHTML = '<span class="hint">没有可解析的来源</span>';
  }
  return wrap;
}

function renderBoardEvidence(board) {
  const list = el("board-evidence");
  const note = el("board-evidence-note");
  list.innerHTML = "";
  const evidence = board.evidence || {};
  const rows = evidence.rows || [];
  if (evidence.mode === "support_checks") {
    note.textContent = "（来自引用核查：每条断言都审过支撑力度）";
  } else if (evidence.mode === "ledger") {
    note.textContent = "（本会话还没做过引用核查，这里按「子问题 × 已收集来源」聚合）";
  } else {
    note.textContent = "";
  }
  if (!rows.length) {
    list.innerHTML = '<li class="hint">还没有可展示的证据行。</li>';
    return;
  }
  rows.forEach(function (row) {
    const strong = row.strength === "强" || String(row.strength).indexOf("中") === 0;
    const verdicts = {
      supported: "已被支撑",
      partial: "部分支撑",
      unsupported: "支撑不足",
      unknown: "未判定",
    };
    const li = boardItem(
      '<div class="board-claim">' +
        esc(short(row.claim || "", 110)) +
        "</div>" +
        '<span class="badge ' +
        (strong ? "badge-strong" : "badge-weak") +
        '">' +
        esc(row.strength) +
        "</span>" +
        '<span class="meta">' +
        esc(row.evidence_level === "summary" ? "有摘要" : "仅标题") +
        (row.verdict && row.verdict !== "unknown" ? " ／ " + esc(verdicts[row.verdict] || row.verdict) : "") +
        "</span>",
    );
    li.appendChild(sourceChips(row.sources));
    if (row.reason) {
      const why = document.createElement("div");
      why.className = "hint";
      why.textContent = row.reason;
      li.appendChild(why);
    }
    list.appendChild(li);
  });
}

function renderBoardTimeline(board) {
  const list = el("board-timeline");
  const note = el("board-timeline-note");
  list.innerHTML = "";
  const timeline = board.timeline || {};
  const groups = timeline.groups || [];
  const missing = timeline.missing_recent_years || [];
  const internal = timeline.internal_gap_labels || [];
  const bits = [];
  if (missing.length) bits.push("最新之后缺：" + missing.join("、"));
  if (internal.length) bits.push("区间内部空档：" + internal.join("、"));
  if (timeline.undated) bits.push(timeline.undated + " 条年份未知");
  note.textContent = bits.length ? "（" + bits.join("；") + "）" : "";
  if (!groups.length) {
    list.innerHTML = '<li class="hint">这个会话还没有带出任何来源。</li>';
    return;
  }
  groups.forEach(function (group) {
    const label = group.year ? group.year + " 年" : "年份未知";
    const head = boardItem(
      '<span class="board-year">' +
        esc(label) +
        '</span><span class="meta">' +
        group.items.length +
        " 条</span>",
    );
    head.className = "is-year";
    list.appendChild(head);
    group.items.forEach(function (item) {
      const li = boardItem(
        '<span class="board-dot">·</span> ' +
          esc(short(item.title || item.source_id, 34)) +
          '<span class="meta">' +
          esc(item.source_type || "") +
          (item.tier ? " ／ tier " + item.tier : "") +
          (item.has_summary ? " ／ 有摘要" : " ／ 仅标题") +
          "</span>",
      );
      li.addEventListener("click", function () {
        showSourceBySourceId(item.source_id);
      });
      list.appendChild(li);
    });
  });
}

/* ------------------------------------------- 学习路线可视化（⑧ 时间线 + 日历） */

var WEEK_MS = 7 * 24 * 3600 * 1000;
var CAL_VIEW_KEY = "scholarscout.routeView"; // "timeline" | "calendar"

function weekRanges(route) {
  /* 路线的第 1 周从它创建那天算起，每周 7 天。
     **按周对齐、不摊到天上**：计划本身只有周粒度，把一周的条目摊到 7 天就是编一份
     我们并不掌握的天级计划（详见 docs/01 §5.15 的口径）。 */
  var start = new Date(route.created_at || Date.now());
  start.setHours(0, 0, 0, 0);
  var weeks = (route.weeks || []).slice();
  return weeks.map(function (week) {
    var idx = Number(week.idx || 1);
    var from = new Date(start.getTime() + (idx - 1) * WEEK_MS);
    var to = new Date(from.getTime() + WEEK_MS - 1);
    return { week: week, from: from, to: to, topic: route.topic || "", route_id: route.id };
  });
}

function sameDay(a, b) {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

function fmtMonthDay(date) {
  return date.getMonth() + 1 + "月" + date.getDate() + "日";
}

function coveredBy(ranges, day) {
  return ranges.filter(function (range) {
    var t = day.getTime();
    return t >= range.from.getTime() && t <= range.to.getTime();
  });
}

function loadRouteView() {
  try {
    return localStorage.getItem(CAL_VIEW_KEY) === "calendar" ? "calendar" : "timeline";
  } catch (error) {
    return "timeline";
  }
}

function saveRouteView(view) {
  try {
    localStorage.setItem(CAL_VIEW_KEY, view);
  } catch (error) {
    /* 隐私模式 */
  }
}

function viewToggle(onChange) {
  var row = document.createElement("div");
  row.className = "cal-toggle";
  [["timeline", "时间线"], ["calendar", "日历"]].forEach(function (pair) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "mini" + (loadRouteView() === pair[0] ? " is-on" : "");
    button.textContent = pair[1];
    button.addEventListener("click", function () {
      saveRouteView(pair[0]);
      onChange();
    });
    row.appendChild(button);
  });
  return row;
}

function renderRouteTimeline(box, routes, rerender) {
  routes.forEach(function (route) {
    var wrap = document.createElement("div");
    wrap.className = "timeline";
    var ranges = weekRanges(route);
    if (routes.length > 1) {
      var name = document.createElement("h5");
      name.textContent = route.topic || "";
      wrap.appendChild(name);
    }
    ranges.forEach(function (range) {
      var row = document.createElement("div");
      row.className = "timeline-row";
      var dot = document.createElement("span");
      dot.className = "timeline-dot";
      row.appendChild(dot);
      var body = document.createElement("div");
      body.className = "timeline-body";
      var head = document.createElement("div");
      head.className = "timeline-head";
      head.textContent =
        "第 " + range.week.idx + " 周 · " +
        fmtMonthDay(range.from) + " ～ " + fmtMonthDay(range.to);
      body.appendChild(head);
      var goal = document.createElement("div");
      goal.className = "timeline-goal";
      goal.textContent = range.week.goal || "";
      body.appendChild(goal);
      (range.week.items || []).forEach(function (item) {
        var line = document.createElement("div");
        line.className = "kv";
        line.textContent =
          "· [" + (item.kind || "") + "] " + (item.title || "") +
          "（约 " + (item.est_minutes || 0) + " 分钟）";
        body.appendChild(line);
      });
      if (range.week.checkpoint) {
        var check = document.createElement("div");
        check.className = "timeline-check";
        check.textContent = range.week.checkpoint;
        body.appendChild(check);
      }
      row.appendChild(body);
      wrap.appendChild(row);
    });
    box.appendChild(wrap);
  });
}

function renderRouteCalendar(box, routes, rerender) {
  var all = [];
  routes.forEach(function (route) {
    all = all.concat(weekRanges(route));
  });
  var cursor = box.__cursor ? new Date(box.__cursor) : new Date();
  cursor.setDate(1);

  var bar = document.createElement("div");
  bar.className = "cal-bar";
  var prev = document.createElement("button");
  prev.type = "button";
  prev.className = "mini";
  prev.textContent = "‹";
  prev.title = "上个月";
  prev.addEventListener("click", function () {
    box.__cursor = new Date(cursor.getFullYear(), cursor.getMonth() - 1, 1);
    rerender();
  });
  var label = document.createElement("span");
  label.className = "cal-label";
  label.textContent = cursor.getFullYear() + " 年 " + (cursor.getMonth() + 1) + " 月";
  var today = document.createElement("button");
  today.type = "button";
  today.className = "mini";
  today.textContent = "今天";
  today.addEventListener("click", function () {
    box.__cursor = new Date();
    rerender();
  });
  var next = document.createElement("button");
  next.type = "button";
  next.className = "mini";
  next.textContent = "›";
  next.title = "下个月";
  next.addEventListener("click", function () {
    box.__cursor = new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1);
    rerender();
  });
  bar.appendChild(prev);
  bar.appendChild(label);
  bar.appendChild(next);
  bar.appendChild(today);
  box.appendChild(bar);

  var grid = document.createElement("div");
  grid.className = "cal-grid";
  ["一", "二", "三", "四", "五", "六", "日"].forEach(function (name) {
    var cell = document.createElement("div");
    cell.className = "cal-head";
    cell.textContent = name;
    grid.appendChild(cell);
  });
  var firstWeekday = (new Date(cursor.getFullYear(), cursor.getMonth(), 1).getDay() + 6) % 7;
  for (var blank = 0; blank < firstWeekday; blank += 1) {
    var empty = document.createElement("div");
    empty.className = "cal-empty";
    grid.appendChild(empty);
  }
  var daysInMonth = new Date(cursor.getFullYear(), cursor.getMonth() + 1, 0).getDate();
  var now = new Date();
  for (var day = 1; day <= daysInMonth; day += 1) {
    (function (dayNumber) {
      var date = new Date(cursor.getFullYear(), cursor.getMonth(), dayNumber);
      var hits = coveredBy(all, date);
      var cell = document.createElement("div");
      cell.className = "cal-cell";
      if (hits.length) cell.classList.add("is-plan");
      if (sameDay(date, now)) cell.classList.add("is-today");
      if (box.__selected && sameDay(date, box.__selected)) cell.classList.add("is-selected");
      var num = document.createElement("span");
      num.className = "cal-day";
      num.textContent = String(dayNumber);
      cell.appendChild(num);
      if (hits.length) {
        var tag = document.createElement("span");
        tag.className = "cal-tag";
        tag.textContent = "第 " + hits[0].week.idx + " 周";
        cell.appendChild(tag);
      }
      cell.title = hits.length
        ? "第 " + hits[0].week.idx + " 周：" + (hits[0].week.goal || "")
        : "这一天不在已纳入的路线里";
      cell.addEventListener("click", function () {
        box.__selected = date;
        rerender();
      });
      grid.appendChild(cell);
    })(day);
  }
  box.appendChild(grid);

  var detail = document.createElement("div");
  detail.className = "cal-detail";
  if (!all.length) {
    detail.appendChild(plainHint("还没有纳入日历的路线：在上面勾选要纳入的路线。"));
  } else if (!box.__selected) {
    detail.appendChild(plainHint("点日历上的任意一天，看它属于哪一周、那一周要做什么。"));
  } else {
    var hits = coveredBy(all, box.__selected);
    var head = document.createElement("div");
    head.className = "cal-detail-head";
    head.textContent = fmtMonthDay(box.__selected) + (hits.length ? "" : "（没有安排）");
    detail.appendChild(head);
    hits.forEach(function (range) {
      var line = document.createElement("div");
      line.className = "cal-detail-week";
      line.textContent = "第 " + range.week.idx + " 周 · " + (range.topic || "");
      detail.appendChild(line);
      var goal = document.createElement("div");
      goal.className = "timeline-goal";
      goal.textContent = range.week.goal || "";
      detail.appendChild(goal);
      (range.week.items || []).forEach(function (item) {
        var itemLine = document.createElement("div");
        itemLine.className = "kv";
        itemLine.textContent = "· [" + (item.kind || "") + "] " + (item.title || "");
        detail.appendChild(itemLine);
      });
      if (range.week.checkpoint) {
        var check = document.createElement("div");
        check.className = "timeline-check";
        check.textContent = range.week.checkpoint;
        detail.appendChild(check);
      }
    });
  }
  box.appendChild(detail);
}

/* 一份可视化组件，两个地方用：学习路线页（单条）与个人中心（勾选的多条） */
function renderRouteVisual(box, routes) {
  box.innerHTML = "";
  if (!routes.length) {
    box.appendChild(plainHint("还没有可可视化的路线。"));
    return;
  }
  var rerender = function () {
    renderRouteVisual(box, routes);
  };
  box.appendChild(viewToggle(rerender));
  if (loadRouteView() === "calendar") renderRouteCalendar(box, routes, rerender);
  else renderRouteTimeline(box, routes, rerender);
}

async function loadRoutesForVisual(ids) {
  var out = [];
  for (var i = 0; i < ids.length; i += 1) {
    try {
      var payload = await scout.call("route.get", { route_id: ids[i] });
      out.push({
        id: String(payload.id || ids[i]),
        topic: payload.topic || "",
        created_at: payload.created_at || "",
        weeks: (payload.plan || {}).weeks || [],
      });
    } catch (error) {
      /* 某条读不到就不纳入，不阻塞其余的 */
    }
  }
  return out;
}

/* ---------------------------------------------------- 个人中心（⑥，内容见⑦） */

var PLAN_PICK_KEY = "scholarscout.calendarRoutes";

function loadRoutePicks() {
  try {
    var raw = JSON.parse(localStorage.getItem(PLAN_PICK_KEY) || "[]");
    return Array.isArray(raw) ? raw.map(String) : [];
  } catch (error) {
    return [];
  }
}

function saveRoutePicks(ids) {
  try {
    localStorage.setItem(PLAN_PICK_KEY, JSON.stringify(ids));
  } catch (error) {
    /* 隐私模式：只当次生效 */
  }
}

async function openProfile() {
  el("profile").hidden = false;
  await renderProfile();
}

function closeProfile() {
  el("profile").hidden = true;
}

function profileSection(title, hint) {
  var box = document.createElement("section");
  box.className = "profile-block";
  var head = document.createElement("h4");
  head.textContent = title;
  box.appendChild(head);
  if (hint) {
    var note = document.createElement("p");
    note.className = "hint";
    note.textContent = hint;
    box.appendChild(note);
  }
  return box;
}

async function renderProfile() {
  var body = el("profile-body");
  body.innerHTML = "";
  body.appendChild(await profileRoutes());
  body.appendChild(await profileNotes());
  body.appendChild(await profileCards());
  el("profile-note").textContent = "汇总跨全部会话；日历与时间线见下一步";
}

async function profileRoutes() {
  var box = profileSection(
    "学习路线汇总 / 日历",
    "勾选要纳入日历的路线（默认不纳入）。日历按周对齐：一条路线的第 1 周从它生成那天算起。",
  );
  var payload;
  try {
    payload = await scout.call("route.list", { limit: 50 });
  } catch (error) {
    box.appendChild(plainHint("读不到路线：" + error.message));
    return box;
  }
  var items = payload.items || [];
  if (!items.length) {
    box.appendChild(plainHint("还没有学习路线。在对话里点「生成学习路线」即可。"));
    return box;
  }
  var picks = loadRoutePicks();
  var list = document.createElement("ul");
  list.className = "list profile-list";
  items.forEach(function (item) {
    var li = document.createElement("li");
    var label = document.createElement("label");
    var check = document.createElement("input");
    check.type = "checkbox";
    check.checked = picks.indexOf(String(item.route_id)) >= 0;
    check.addEventListener("change", function () {
      var now = loadRoutePicks();
      var id = String(item.route_id || item.id || "");
      var at = now.indexOf(id);
      if (check.checked && at < 0) now.push(id);
      if (!check.checked && at >= 0) now.splice(at, 1);
      saveRoutePicks(now);
      renderProfile();
    });
    label.appendChild(check);
    label.appendChild(document.createTextNode(" " + (item.topic || "未命名")));
    li.appendChild(label);
    var meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent =
      (item.weeks ? item.weeks + " 周" : "") +
      (item.version ? "｜第 " + item.version + " 版" : "") +
      (item.created_at ? "｜" + String(item.created_at).slice(0, 10) : "");
    li.appendChild(meta);
    list.appendChild(li);
  });
  box.appendChild(list);
  var picked = loadRoutePicks();
  box.appendChild(plainHint("已勾选 " + picked.length + " 条 / 共 " + items.length + " 条"));
  var visual = document.createElement("div");
  visual.className = "route-visual";
  box.appendChild(visual);
  renderRouteVisual(visual, await loadRoutesForVisual(picked));
  return box;
}

async function profileNotes() {
  var box = profileSection("笔记汇总", "学习笔记与资料摘要（跨会话）。");
  var payload;
  try {
    payload = await scout.call("note.list", { limit: 50 });
  } catch (error) {
    box.appendChild(plainHint("读不到笔记：" + error.message));
    return box;
  }
  var items = (payload.items || []).filter(function (item) {
    return item.kind !== "card";
  });
  if (!items.length) {
    box.appendChild(plainHint("还没有笔记或摘要。"));
    return box;
  }
  var list = document.createElement("ul");
  list.className = "list profile-list";
  items.slice(0, 20).forEach(function (item) {
    var li = document.createElement("li");
    li.textContent = (item.kind === "summary" ? "摘要：" : "笔记：") + (item.title || "(无标题)");
    var meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = String(item.created_at || "").slice(0, 16).replace("T", " ");
    li.appendChild(meta);
    list.appendChild(li);
  });
  box.appendChild(list);
  box.appendChild(plainHint("共 " + items.length + " 条，上面显示最近 " + Math.min(items.length, 20) + " 条。"));
  return box;
}

async function profileCards() {
  var box = profileSection("抽背汇总", "掌握度按 SM-2 的排期算：熟＝连续答对 2 次或间隔 ≥6 天。");
  var stats;
  try {
    stats = await scout.call("note.stats", {});
  } catch (error) {
    box.appendChild(plainHint("读不到抽背统计：" + error.message));
    return box;
  }
  var line = document.createElement("p");
  line.className = "profile-nums";
  line.textContent =
    "卡片 " + (stats.total_cards || 0) + " 张" +
    "｜练过 " + (stats.reviewed || 0) + " 张" +
    "｜今天该练 " + (stats.due || 0) + " 张" +
    "（新卡 " + (stats.due_new || 0) + "）" +
    "｜记住率 " + (stats.recall_rate === null || stats.recall_rate === undefined
      ? "—"
      : Math.round(stats.recall_rate * 100) + "%") +
    (stats.next_due_at ? "｜下次复习 " + String(stats.next_due_at).slice(0, 16).replace("T", " ") : "");
  box.appendChild(line);
  var profile = null;
  try {
    profile = await scout.call("learner.profile", {});
  } catch (error) {
    profile = null;
  }
  var groups = (profile && profile.card_groups) || [];
  if (groups.length) {
    var list = document.createElement("ul");
    list.className = "list profile-list";
    groups.forEach(function (item) {
      var li = document.createElement("li");
      var rate = item.mastery_rate === null ? "—" : Math.round(item.mastery_rate * 100) + "%";
      li.textContent = item.group;
      var meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent =
        "熟 " + rate + "｜熟 " + item.mastered + "／复习中 " + item.reviewing + "／薄弱 " + item.weak;
      li.appendChild(meta);
      list.appendChild(li);
    });
    box.appendChild(list);
  }
  return box;
}

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

function openExternalUrl(url) {
  var target = String(url || "").trim();
  if (!target) {
    alert("这条资料没有记录源地址。");
    return;
  }
  try {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.open_url) {
      window.pywebview.api.open_url(target);
      return;
    }
  } catch (error) {
    /* 落到下面的兜底 */
  }
  window.open(target, "_blank");
}

/* 资料库摘要区高度：可拖、可双击复位、记住（localStorage，不动配置文件） */
var SOURCE_HEIGHT_KEY = "scholarscout.sourcesDetailHeight";

function applySourceHeight(pixels) {
  var detail = el("source-detail");
  if (!detail) return;
  // 用 flex-basis 而不是 height：这个区域在弹性列里，height 会被 flex 规则盖掉
  if (!pixels) {
    detail.style.flex = "";
    detail.style.maxHeight = "";
    return;
  }
  detail.style.flex = "0 0 " + pixels + "px";
  detail.style.maxHeight = "none";
}

function wireHeightSplitter(splitterId, targetId, key) {
  var bar = el(splitterId);
  var target = el(targetId);
  if (!bar || !target) return;
  var saved = null;
  try {
    saved = parseInt(localStorage.getItem(key) || "", 10);
  } catch (error) {
    saved = null;
  }
  if (saved && saved > 0) applySourceHeight(saved);

  var dragging = false;
  bar.addEventListener("mousedown", function (event) {
    dragging = true;
    event.preventDefault();
    document.body.style.cursor = "row-resize";
  });
  window.addEventListener("mousemove", function (event) {
    if (!dragging) return;
    var pane = target.parentElement || target;
    var box = pane.getBoundingClientRect();
    // 用"离底部的距离"当高度：往下拖＝摘要区变小
    var height = Math.round(box.bottom - event.clientY);
    var limit = Math.max(120, Math.round(box.height - 120));
    applySourceHeight(Math.min(Math.max(height, 120), limit));
  });
  window.addEventListener("mouseup", function () {
    if (!dragging) return;
    dragging = false;
    document.body.style.cursor = "";
    try {
      var height = parseInt((target.style.flex || "").replace(/[^0-9]/g, "") || "0", 10);
      if (height > 0) localStorage.setItem(key, String(height));
      else localStorage.removeItem(key);
    } catch (error) {
      /* 隐私模式：只当次生效 */
    }
  });
  bar.addEventListener("dblclick", function () {
    applySourceHeight(null);
    try {
      localStorage.removeItem(key);
    } catch (error) {
      /* 忽略 */
    }
  });
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
      {
        key: "generation.style",
        label: "风格档（default 默认／academic 严谨学术／tutor 亲切助教／concise 简洁）",
        kind: "text",
      },
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
      { key: "runtime.compact_after_chars", label: "D3 压缩阈值（字符）", kind: "number" },
      {
        key: "generation.compact_by_model",
        label: "历史用模型摘要式压缩（关＝只做规则裁剪）",
        kind: "bool",
      },
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

  // MinerU：开关开着但没配 token —— 现在**不是错误**（扫描版走免 Token 的预签名直传也能用），
  // 但要说清能力边界：免 Token 的 Agent 通道限 ≤10MB / ≤20 页，更大或要更高精度才需要 Token。
  if (values["features.enable_mineru"] !== false && !secrets["mineru.api_token"]) {
    var warn = document.createElement("p");
    warn.className = "hint";
    warn.textContent =
      "扫描版走免 Token 通道（官方 Agent 轻量接口，实测可用）：限单文件 ≤10MB / ≤20 页，" +
      "直传本地字节、不需要公网 URL。超过这个规模或要更高精度（表格/公式/多格式导出），" +
      "再填 MinerU Token 换精准接口（≤200MB / ≤200 页）。解析失败会如实记成 mineru_failed，不会假装成功。";
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
    ["导出单页 HTML（可直接打开/发送）", function () { exportSession(item, "html"); }, false],
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

async function exportSession(item, fmt) {
  closeRowMenus();
  el("status").textContent = "正在导出…";
  try {
    var result = await scout.call("session.export", {
      session_id: item.session_id,
      fmt: fmt || "md",
    });
    if (result.fmt === "html") {
      // 单页 HTML 是给人双击看的：这里只报路径，不弹正文（正文有几十万字符）
      el("status").textContent =
        "已导出单页 HTML：" + result.path + "（在文件夹里双击即可打开，断网也能看）";
      return;
    }
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
  el("btn-profile").addEventListener("click", function () {
    openProfile().catch(function (error) {
      el("status").textContent = "个人中心打开失败：" + error.message;
    });
  });
  el("btn-profile-close").addEventListener("click", closeProfile);
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
  el("btn-memory-add").addEventListener("click", addMemory);
  el("btn-memory-extract").addEventListener("click", extractMemory);
  el("memory-input").addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      addMemory();
    }
  });
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

  // 抽背「练」模式：开始 / 关闭 / 看答案，以及键盘 1/2/3（会了/模糊/不会 的顺序见按钮）
  el("btn-practice").addEventListener("click", function () {
    startPractice(null, {});
  });
  el("btn-practice-due").addEventListener("click", function () {
    startPractice(null, { dueOnly: true });
  });
  el("btn-practice-close").addEventListener("click", closePractice);
  el("btn-practice-reveal").addEventListener("click", revealPractice);
  document.addEventListener("keydown", function (event) {
    if (!state.practice || el("practice").hidden) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      revealPractice();
      return;
    }
    if (!state.practice.revealed) return; // 没看答案不许自评（与按钮的禁用一致）
    if (event.key === "1") ratePractice("bad");
    else if (event.key === "2") ratePractice("fuzzy");
    else if (event.key === "3") ratePractice("good");
  });

  el("input").addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      el("composer").dispatchEvent(new Event("submit"));
    }
  });

  el("btn-new").addEventListener("click", function () {
    newSession();
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
      // 切到哪个页签就刷新哪个 —— 数据永远跟着当前会话走（用户要求去掉手动刷新按钮）
      var loaders = {
        sources: function () { loadSources(el("search").value.trim()); },
        cards: loadCards,
        notes: loadNotes,
        board: loadBoard,
      };
      if (loaders[tab.dataset.tab]) loaders[tab.dataset.tab]();
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
