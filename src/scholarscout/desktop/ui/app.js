/* 应用主体：全局状态、会话与消息、启动与接线。
   桥与小工具在 api.js、资料库在 sources.js、学习相关在 learning.js、
   看板在 board.js、个人中心与设置在 profile.js（引入顺序见 index.html）。 */

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


function loadUiScale() {
  var saved = null;
  try {
    saved = localStorage.getItem(UI_SCALE_KEY);
  } catch (error) {
    saved = null;
  }
  return applyUiScale(saved === null ? 1 : Number(saved));
}


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
  wireWindowControls();
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
  el("btn-side-toggle").addEventListener("click", toggleSidebarCollapsed);
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
  sidebarCollapsed: false,
};

/* ------------------------------------------------------------------ 主题 */


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

/* ---------------------------------------- ⑤ 无边框窗口：自绘窗控条 + 拉伸把手 */


var UI_SCALE_KEY = "scout:uiScale";


var THEME_CHOICES = [
  ["auto", "跟随系统"],
  ["light", "浅色"],
  ["dark", "深色"],
];


var saved = null;
try {
  saved = localStorage.getItem(THEME_KEY);
} catch (error) {
  saved = null;
}
applyTheme(saved === "light" || saved === "dark" ? saved : "auto");
loadUiScale(); /* 布局由 boot 里的 initLayout 负责，这里只管缩放 */
applySidebarCollapsed(readSidebarCollapsed(), false); /* 左栏收起态：与主题一样，加载时先按存档恢复 */


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


/* 前端版本标记：只用来确认真机跑的是哪一版（WebView2 会缓存 app.js） */
var APP_JS_REV = "ux-batch2-r2";


/** token 数按人看的写法：12345 → "12.3k"。 */
function fmtTokens(value) {
  const n = Number(value || 0);
  if (!n) return "0";
  if (n < 1000) return String(n);
  return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
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
    const due = await scout.call("note.due", { note_id: noteId, limit: 50 });
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


/* 资料库摘要区高度：可拖、可双击复位、记住（localStorage，不动配置文件） */
var SOURCE_HEIGHT_KEY = "scholarscout.sourcesDetailHeight";


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


/*: 「数据」栏的中文名 —— 后端给的是目录键名，界面别直接甩英文。 */
var USAGE_LABELS = {
  database: "数据库",
  fetch_cache: "抓取缓存",
  raw: "原件",
  text: "全文",
  notes: "笔记",
  run: "临时文件（run/）",
};


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


/** 产物按需生成：点按钮就是"我明确要了"，用一条真实的用户消息把请求发出去 ——
 *  这样"用户要了什么"在会话记录里看得见，不是偷偷调工具。 */
var ASK_TEXTS = {
  map: "请基于本次会话已经取到的资料，把主题整理成知识地图（调用 topic.build_map）。",
  route: "请基于本次会话已经取到的资料，生成一条 4 周学习路线（调用 route.plan）。",
  cards: "请把本次会话的关键概念做成抽背卡（调用 note.save，kind 用 card）。",
};


wire();
window.addEventListener("pywebviewready", boot);
