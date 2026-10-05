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
};

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

function short(text, n) {
  const value = String(text === null || text === undefined ? "" : text);
  return value.length > n ? value.slice(0, n) + "…" : value;
}

/* --------------------------------------------------------- 后端推来的事件 */

window.scoutEvent = function (event) {
  if (!event || !event.method) return;
  const p = event.params || {};
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
    li.innerHTML =
      esc(short(item.title || "未命名会话", 16)) +
      '<span class="meta">' +
      esc(short(item.updated_at || "", 16)) +
      "｜" +
      item.message_count +
      " 条</span>";
    li.addEventListener("click", function () {
      openSession(item.session_id);
    });
    list.appendChild(li);
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
    bubble("assistant", "ScholarScout").body.textContent = message.content;
  }
}

function makePending() {
  const node = bubble("assistant", "ScholarScout · 进行中");
  const traceBox = document.createElement("div");
  traceBox.className = "trace";
  node.body.appendChild(traceBox);
  const textBox = document.createElement("div");
  textBox.style.marginTop = "6px";
  node.body.appendChild(textBox);
  return {
    add: function (cls, text) {
      const line = document.createElement("div");
      line.className = cls;
      line.textContent = text;
      traceBox.appendChild(line);
      el("messages").scrollTop = el("messages").scrollHeight;
    },
    setText: function (text) {
      textBox.textContent = text;
    },
    node: node,
  };
}

async function send(text) {
  if (state.sending || !state.sessionId) return;
  state.sending = true;
  el("btn-send").disabled = true;
  bubble("user", "你").body.textContent = text;
  state.pending = makePending();

  try {
    const result = await scout.call("chat.send", { session_id: state.sessionId, text: text });
    state.pending.node.wrap.classList.remove("assistant");
    if (result.text) state.pending.setText(result.text);
    state.pending.add(
      result.stop_reason === "completed" ? "ok" : "warn",
      "stop_reason=" + result.stop_reason + "｜工具 " + (result.tool_calls || []).length + " 次"
    );
    renderRefs(result.refs || {});
    await refreshSide();
  } catch (error) {
    state.pending.add("bad", "调用失败：" + error.message);
    state.pending.node.wrap.classList.add("error");
  } finally {
    state.pending = null;
    state.sending = false;
    el("btn-send").disabled = false;
  }
}

function renderRefs(refs) {
  const chips = document.createElement("div");
  chips.className = "chips";
  let any = false;

  (refs.route_ids || []).forEach(function (id) {
    any = true;
    addChip(chips, "路线 " + id, function () {
      showRoute(id);
    });
  });
  (refs.note_ids || []).forEach(function (id) {
    any = true;
    addChip(chips, "卡片 " + id, function () {
      showCard(id);
    });
  });
  (refs.topic_ids || []).forEach(function (id) {
    any = true;
    addChip(chips, "主题图 " + id, async function () {
      const payload = await scout.call("topic.get", { topic_id: id });
      showTopic(payload);
    });
  });
  (refs.source_ids || []).forEach(function (id) {
    any = true;
    addChip(chips, "资料 " + id, function () {
      showSourceBySourceId(id);
    });
  });

  if (any && state.pending) state.pending.node.body.appendChild(chips);
  else if (any) {
    const last = el("messages").lastElementChild;
    if (last) last.appendChild(chips);
  }
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

/* ------------------------------------------------------------ 会话与轨迹 */

async function loadSessions() {
  const payload = await scout.call("session.list", { limit: 50 });
  renderSessions(payload.items || []);
  return payload.items || [];
}

async function newSession() {
  const created = await scout.call("session.create", { title: "新调研" });
  state.sessionId = created.session_id;
  el("messages").innerHTML =
    '<p class="empty">新会话已建好。试试：「帮我找教育智能体方向的入门资料」</p>';
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

async function refreshSide() {
  await loadSources(el("search").value.trim());
  await loadCards();
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
    renderHealth(await scout.call("system.health", {}));
    const items = await loadSessions();
    if (items.length) await openSession(items[0].session_id);
    else await newSession();
    await loadCards();
    await loadSources("");
  } catch (error) {
    el("status").textContent = "连接后端失败：" + error.message;
    el("status").classList.add("is-bad");
  }
}

function wire() {
  el("composer").addEventListener("submit", function (event) {
    event.preventDefault();
    const input = el("input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    send(text);
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

wire();
window.addEventListener("pywebviewready", boot);
