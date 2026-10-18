/* 应用主体：全局状态、会话与消息、启动与接线。
   桥与小工具在 api.js、资料库在 sources.js、学习相关在 learning.js、
   看板在 board.js、个人中心与设置在 profile.js（引入顺序见 index.html）。 */

/* 输入区右侧的「模型 / 思考深度」下拉 —— 这两个是**真能改**的键（点一下就写 config.yaml）。
 *
 * 值不在这里写死：
 *   · 模型清单来自 `models.list`（后端先探 `GET /models`，探测不产生生成费用；失败就回落
 *     `config.yaml` 的候选 + 当前模型，并在 note 里如实说明"没探到"）。
 *   · 档位文案里括号中的真实档位也来自后端（"中（high）"）：本服务只有 low/high/max 三档，
 *     五档是并档映射，界面**不假装**五档各不相同。
 *   · 我们**不发明**强度等级、也不写死模型名 —— 另写一份必然与接口漂移。
 *
 * 选择写 `llm.chat.model` / `llm.chat.reasoning_effort`，**下一轮生效**：客户端在下一轮
 * 才按新配置建请求（正在跑的这一轮不会被中途换掉），所以提示语必须写"下一轮生效"。
 *
 * 设置面板里的「思考深度」也用同一份回包（MODEL_CHOICES），两处不各写一套。
 */
var MODEL_CHOICES = null;

/*: 选完后的确认文案停留时间（毫秒）：写盘是异步的，用户需要看得见"真的存了" */
var COMPOSER_NOTE_MS = 4000;
var _composerNoteTimer = null;


async function loadModelChoices(force) {
  if (MODEL_CHOICES && !force) return MODEL_CHOICES;
  MODEL_CHOICES = await scout.call("models.list", { remember: true });
  return MODEL_CHOICES;
}


function fillModelOptions(node, choices) {
  node.innerHTML = "";
  const models = {};
  (choices.models || []).forEach(function (item) {
    models[item.id] = item;
  });
  (choices.available_models || []).forEach(function (id) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = id;
    const info = models[id] || {};
    const levels = info.supported_levels || [];
    if (levels.length) {
      // 模型名后面不再堆档位（下拉太窄会被截断），放进 title 里照样可查
      option.title =
        id + "：思考档位 " + levels.join("/") + "，默认 " + (info.default_level || "high");
    }
    node.appendChild(option);
  });
  if (choices.current) node.value = choices.current;
}


function fillLevelOptions(node, choices) {
  node.innerHTML = "";
  (choices.level_options || []).forEach(function (item) {
    const option = document.createElement("option");
    option.value = item.value;
    // label 由后端给（"中（high）" / "关（不发思考参数）"）—— 前端不自己拼档位名
    option.textContent = item.label || item.value;
    option.disabled = item.supported === false;
    node.appendChild(option);
  });
  node.value = choices.current_level || "medium";
}


function composerNote(text) {
  const note = el("composer-note");
  if (!note) return;
  note.textContent = text;
  note.hidden = !text;
  if (_composerNoteTimer) window.clearTimeout(_composerNoteTimer);
  if (!text) return;
  _composerNoteTimer = window.setTimeout(function () {
    note.textContent = "";
    note.hidden = true;
  }, COMPOSER_NOTE_MS);
}


async function refreshComposerMeta() {
  const modelSelect = el("composer-model");
  const levelSelect = el("composer-thinking");
  if (!modelSelect || !levelSelect) return;
  let choices = null;
  try {
    choices = await loadModelChoices(true);
  } catch (error) {
    composerNote("模型清单读取失败：" + error.message);
    return;
  }
  fillModelOptions(modelSelect, choices);
  fillLevelOptions(levelSelect, choices);
  const plan = choices.current_plan || {};
  // 服务端没声明这个档位时（如只支持 low/high），如实说"会降级成哪一档"，不静默
  levelSelect.title =
    "思考深度（llm.chat.reasoning_effort）｜服务端声明：" +
    (choices.supported_levels || []).join("/") +
    "，默认 " +
    (choices.default_level || "high") +
    (plan.exact === false ? "｜当前档位会降级为 " + (plan.effort || "最低档") : "");
  modelSelect.title =
    "对话模型（llm.chat.model）｜来源：" +
    (choices.source === "probe" ? "/models 探测" : "config.yaml 回落") +
    (choices.note ? "｜" + choices.note : "");
}


async function saveComposerChoice(key, value, label) {
  const modelSelect = el("composer-model");
  const levelSelect = el("composer-thinking");
  const updates = { llm: { chat: {} } };
  updates.llm.chat[key] = value;
  modelSelect.disabled = true;
  levelSelect.disabled = true;
  try {
    const result = await scout.call("settings.update", { updates: updates });
    const notes = (result.notes || []).join("；");
    MODEL_CHOICES = await loadModelChoices(true);
    fillModelOptions(modelSelect, MODEL_CHOICES);
    fillLevelOptions(levelSelect, MODEL_CHOICES);
    await refreshComposerMeta();
    composerNote("已保存：" + label + "（下一轮生效）" + (notes ? "｜" + notes : ""));
  } catch (error) {
    composerNote("保存失败：" + error.message);
    // 失败要把界面拉回磁盘上的真实值，否则下拉停在一个没存住的选择上
    await refreshComposerMeta();
  } finally {
    modelSelect.disabled = false;
    levelSelect.disabled = false;
  }
}


function wireComposerChoices() {
  const modelSelect = el("composer-model");
  const levelSelect = el("composer-thinking");
  if (!modelSelect || !levelSelect) return;
  modelSelect.addEventListener("change", function () {
    saveComposerChoice("model", modelSelect.value, "模型 " + modelSelect.value);
  });
  levelSelect.addEventListener("change", function () {
    const option = levelSelect.options[levelSelect.selectedIndex];
    const label = option ? option.textContent : levelSelect.value;
    saveComposerChoice("reasoning_effort", levelSelect.value, "思考深度 " + label);
  });
}


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
    // 角标按 session_id 找它自己的槽（渲染列表时也要带上，否则刷一次列表角标就没了）
    li.setAttribute("data-session", item.session_id);
    const slot = turnSlot(item.session_id, false);
    if (item.session_id === state.sessionId) li.className = "is-active";
    if (item.pinned) li.classList.add("is-pinned");
    if (slot && slot.unread) li.classList.add("is-unread");
    // 哪个会话在跑，一眼看得见（每会话自己的进行中状态，不是全局的）
    if (sessionRunning(item.session_id)) li.classList.add("is-running");
    // 置顶要有可见标记：不然用户点了置顶只看到顺序变了，不知道是不是生效了
    const badge = item.pinned ? '<span class="pin-flag" title="已置顶">顶</span>' : "";
    const dot =
      '<span class="unread-dot" title="有新回复"' +
      (slot && slot.unread ? "" : " hidden") +
      "></span>" +
      '<span class="running-dot" title="这一轮正在跑"></span>';
    li.innerHTML =
      badge +
      dot +
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

/* ── 每会话一个槽 + 每会话一条消息流（docs/20 §3.3，第 5–6 步）

   用户原话：「在其中一个会话运行时，我想切换到其他会话，会导致本次会话出现异常」。
   根因是界面只有**全局单飞闸 + 单一进行中槽**：A 在跑时切到 B，A 的事件会写进"此刻挂着的
   那个气泡"（切走后那个气泡已经不在显示里），B 一旦开跑，A 的收尾就落到 B 的气泡上。

   做法（两件配套的事，缺一不可）：
     ① `state.turns[session_id]`：**每会话一个**进行中槽（trace / running / turnId / 未读）；
     ② `state.streams[session_id]`：**每会话一条**消息流容器，切会话只切"哪条在显示"，
        **不重建、不清屏** —— 进行中的气泡留在它自己那条流里，别的事件也只会写进自己的流。
   于是"串会话"在结构上不可能：A 的正文物理上不在 B 的那条流里。 */

/** 会话 → 消息流容器（`#messages` 的子节点；同一时刻只有一条带 .is-active）。 */
function streamBox(sessionId) {
  const key = String(sessionId || "");
  if (!key) return el("messages");
  let entry = state.streams[key];
  if (!entry) {
    const node = document.createElement("div");
    node.className = "msg-stream";
    node.setAttribute("data-session", key);
    el("messages").appendChild(node);
    entry = state.streams[key] = { node: node, rendered: false, pendingHistory: false };
  }
  return entry.node;
}

/** 切到某个会话的流：只动 `.is-active`，**不做任何 DOM 重建**。 */
function activateStream(sessionId) {
  const key = String(sessionId || "");
  const box = el("messages");
  // index.html 里的占位说明（不是 .msg-stream）在第一次挂流时清掉
  Array.prototype.slice.call(box.children).forEach(function (child) {
    if (!child.classList.contains("msg-stream")) child.remove();
  });
  /* 顺序不能反：**先建出这条流**，再统一切 `.is-active`。
     反了的话，首次打开某个会话时它的节点是在切类的循环**之后**才创建的，
     于是它永远拿不到 `.is-active` → `.msg-stream{display:none}` 让整个消息区空白
     （真机表现：刚进应用看不到任何消息，再点一次同一个会话才出来）。 */
  const target = streamBox(key);
  Object.keys(state.streams).forEach(function (other) {
    state.streams[other].node.classList.toggle("is-active", other === key);
  });
  return target;
}

/** 每会话一个「进行中」槽（键 = session_id）。create=true 时没有就建。 */
function turnSlot(sessionId, create) {
  const key = String(sessionId || "");
  if (!key) return null;
  let slot = state.turns[key];
  if (!slot && create) {
    slot = state.turns[key] = {
      turnId: "",
      messageId: "",
      trace: null,
      running: false,
      unread: false,
      spinner: null,
    };
  }
  return slot || null;
}

/** 这个会话自己有没有一轮在跑（别的会话在跑不算）。 */
function sessionRunning(sessionId) {
  const slot = state.turns[String(sessionId || "")];
  return !!(slot && slot.running);
}

/** 发送钮 / 停止钮跟着**当前显示的这个会话**的槽走。 */
function syncSendState() {
  const running = sessionRunning(state.sessionId);
  el("btn-send").disabled = running;
  el("btn-stop").hidden = !running;
  renderRunning();
}

/** 左栏「这一轮正在跑」标记：从 `state.turns` 现算，**起跑与收尾都刷**。
    只在列表重渲染时算会让它落后一整轮（真机表现：B 已经开跑，左栏还没标记；
    探针在"B 也开跑了"那条断言上抓到过）。 */
function renderRunning() {
  document.querySelectorAll("#session-list li[data-session]").forEach(function (li) {
    li.classList.toggle("is-running", sessionRunning(li.getAttribute("data-session")));
  });
}

/** 有未读的会话数（读 `state.turns`，不问后端）。 */
function unreadCount() {
  return Object.keys(state.turns).filter(function (key) {
    return state.turns[key].unread === true;
  }).length;
}

/** 角标 + 顶栏计数：全部从 `state.turns` 同步算出来，不依赖后端往返。
    清除时机：切到那个会话（openSession 里清）或点顶栏计数跳过去。 */
function renderUnread() {
  document.querySelectorAll("#session-list li[data-session]").forEach(function (li) {
    const slot = turnSlot(li.getAttribute("data-session"), false);
    const unread = !!(slot && slot.unread);
    li.classList.toggle("is-unread", unread);
    const dot = li.querySelector(".unread-dot");
    if (dot) dot.hidden = !unread;
  });
  const badge = el("unread-count");
  const count = unreadCount();
  if (badge) {
    badge.hidden = count === 0;
    badge.textContent = count ? count + " 个会话有新回复" : "";
  }
  return count;
}

function renderMessage(message, sessionId) {
  if (message.role === "user") {
    bubble("user", "你", sessionId).body.textContent = message.content || "";
    return;
  }
  if (message.role === "tool") return; // 工具结果在轨迹里看，不铺在正文
  if (message.role === "assistant" && (message.content || "").trim()) {
    ScoutMarkdown.renderInto(bubble("assistant", "ScholarScout", sessionId).body, message.content);
  }
}


function makePending(sessionId) {
  const node = bubble("assistant", "ScholarScout · 进行中", sessionId);
  /* 工具调用信息条：一次调用**一条**（工具名 + 参数摘要 + 结果/状态 + 耗时）。
     多条按发生顺序依次往下排，互不覆盖（见 addToolCall/addToolResult）。 */
  const toolFlow = document.createElement("div");
  toolFlow.className = "tool-flow";
  node.body.appendChild(toolFlow);
  const traceBox = document.createElement("div");
  traceBox.className = "trace";
  node.body.appendChild(traceBox);
  const textBox = document.createElement("div");
  textBox.className = "reply";
  node.body.appendChild(textBox);
  /* 加载圈：**在会话消息流内部**（进行中气泡的最后一行），不再是输入框上方的浮层。
     轮次收尾时它被真正**移除**（hideProgress / turnSpinnerSweep），不是藏起来。 */
  const progress = document.createElement("div");
  progress.className = "msg-progress";
  progress.setAttribute("role", "status");
  progress.setAttribute("aria-live", "polite");
  progress.hidden = true;
  const progressDot = document.createElement("span");
  progressDot.className = "msg-progress-dot";
  progressDot.setAttribute("aria-hidden", "true");
  const progressText = document.createElement("span");
  progressText.className = "msg-progress-text";
  progress.appendChild(progressDot);
  progress.appendChild(progressText);
  node.body.appendChild(progress);

  /* 流式渲染的节流：模型每秒能吐几十个增量，每个都重排一遍 DOM 会卡。
     120ms 一次肉眼已经"实时"了，末次结果直接落（见 flush）。 */
  const THROTTLE_MS = 120;
  let lastPaint = 0;
  let timer = null;
  let roundText = "";        // 当前段的累计正文（chat.delta 给的是**本轮累计值**）
  let currentRound = null;   // 当前段的 DOM 节点（一段 = 一轮模型回复）
  let roundClosed = false;   // 当前段是否已被一次工具调用收尾（= 下一个增量属于新一轮）
  let finished = false;
  const openRows = {};       // call_id → 还没拿到结果的工具行
  let lastRow = null;        // 最近一条待结果的工具行（call_id 对不上时兜底）

  function scrollDown() {
    el("messages").scrollTop = el("messages").scrollHeight;
  }

  function paint() {
    lastPaint = Date.now();
    if (currentRound) ScoutMarkdown.renderInto(currentRound, roundText);
    scrollDown();
  }

  function flush() {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    paint();
  }

  function newRound() {
    currentRound = document.createElement("div");
    currentRound.className = "reply-round";
    textBox.appendChild(currentRound);
    return currentRound;
  }

  /** 建一条工具行（草稿态：状态「运行中…」，耗时先空着）。 */
  function createRow(tool) {
    const row = document.createElement("div");
    row.className = "tool-row";
    row.setAttribute("data-tool", tool);
    const dot = document.createElement("span");
    dot.className = "tool-row-dot";
    dot.setAttribute("aria-hidden", "true");
    const name = document.createElement("span");
    name.className = "tool-row-name";
    name.textContent = tool;
    const args = document.createElement("span");
    args.className = "tool-row-args";
    const status = document.createElement("span");
    status.className = "tool-row-status";
    status.textContent = "运行中…";
    const latency = document.createElement("span");
    latency.className = "tool-row-latency";
    row.appendChild(dot);
    row.appendChild(name);
    row.appendChild(args);
    row.appendChild(status);
    row.appendChild(latency);
    toolFlow.appendChild(row);
    return { row: row, args: args, status: status, latency: latency, startedAt: Date.now() };
  }

  function showProgress(text, kind) {
    // 被兜底清扫摘掉过也还能补回来（正常路径不会走到这里）
    if (!progress.parentNode) node.body.appendChild(progress);
    progress.hidden = false;
    progress.setAttribute("data-kind", kind || "");
    progressText.textContent = text || "";
    scrollDown();
  }

  /** 「结束后必消失」的正常路径：**把节点从会话流里摘掉**。 */
  function hideProgress() {
    if (progress.parentNode) progress.remove();
  }

  return {
    add: function (cls, text) {
      const line = document.createElement("div");
      line.className = cls;
      line.textContent = text;
      traceBox.appendChild(line);
      el("messages").scrollTop = el("messages").scrollHeight;
    },
    /** chat.tool_call → 落成一条工具行（工具名 + 参数摘要）。 */
    addToolCall: function (params) {
      const p = params || {};
      const tool = p.tool ? String(p.tool) : "未知工具";
      const entry = createRow(tool);
      const argsText = toolArgsSummary(p.args_preview, 120);
      entry.args.textContent = argsText;
      entry.row.title = tool + "（参数：" + argsText + "）";
      if (p.call_id) openRows[String(p.call_id)] = entry;
      lastRow = entry;
      roundClosed = true; // 一次工具调用 = 这一轮的正文流到此收尾
      scrollDown();
    },
    /** chat.tool_result → 补齐这条工具的返回摘要/状态与耗时（按 call_id 配对）。 */
    addToolResult: function (params) {
      const p = params || {};
      const key = p.call_id ? String(p.call_id) : "";
      let entry = key ? openRows[key] : null;
      if (entry) delete openRows[key];
      else entry = lastRow; // call_id 缺失/对不上时的兜底
      if (!entry) {
        /* 后端只推了结果没推调用（理论上不会）：**补一条**，绝不让结果凭空消失 ——
           「工具调用一条都不丢」是这个界面的契约。 */
        entry = createRow("未知工具");
      }
      entry.status.textContent = toolResultSummary(p);
      entry.latency.textContent = toolLatencyText(p, entry.startedAt, Date.now());
      entry.row.classList.add(p.ok === false ? "is-bad" : "is-ok");
      entry.row.setAttribute("data-status", p.ok === false ? "bad" : "ok");
      if (entry === lastRow) lastRow = null;
      scrollDown();
    },
    /** chat.delta → 每轮正文各自成段：同一轮是替换（累计值），新一轮是新开一段。 */
    pushDelta: function (text) {
      const next = replyRoundNext(roundClosed, text);
      if (next.isNew || !currentRound) newRound();
      roundClosed = false;
      roundText = next.text;
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
    /** 收尾：后端给的**权威全文**落到最后一段（覆盖标记被剥掉等情况，界面必须与落库一致）。 */
    setFinal: function (text) {
      const value = String(text || "");
      if (!value) return;
      if (!currentRound) newRound();
      roundText = value;
      roundClosed = true; // 轮次已收尾，万一再来增量也该新开一段
      finished = true;
      flush();
    },
    showProgress: showProgress,
    hideProgress: hideProgress,
    finish: function () {
      finished = true;
      flush();
      node.who.textContent = "ScholarScout";
      hideProgress();
    },
    node: node,
  };
}


/** 一轮收尾（`chat.done`）：**只落进它自己会话的槽**，角标与计数也在这里判。

    为什么收尾搬到这里：`chat.send(mode="async")` 立刻返回 `turn_id`、整轮还没结束，
    最后的权威正文（`text`）与落库消息 id 只有 `chat.done` 带得回来。
    `chat.done` 的 `session_id` 由 `bind_emitter` 注入（docs/20 §3.1），所以"这一轮的结论
    属于谁"是**机制保证**，不靠猜。 */
function finishTurn(sessionId, params) {
  const p = params || {};
  const slot = turnSlot(sessionId, true);
  const trace = slot.trace;
  if (trace) {
    trace.node.wrap.classList.remove("assistant");
    // 后端给的是权威全文（可能与流式末次不同）→ 落到最后一段，界面与落库必须一致
    trace.setFinal(p.text);
    trace.finish();
    // 工具次数从这条流自己的工具行数出来（async 下 chat.done 不带 tool_calls）
    const rows = trace.node.wrap.querySelectorAll(".tool-row").length;
    trace.add(
      p.stop_reason === "completed" ? "ok" : "warn",
      "stop_reason=" + (p.stop_reason || "unknown") + "｜工具 " + rows + " 次",
    );
    const usage = p.usage || {};
    if (usage.prompt_tokens || usage.completion_tokens) {
      // 把每一轮的真实 token 摆出来 —— 用户抱怨"缓存没效果、账单很高"，界面就该给得出数字
      trace.add(
        "ok",
        "token 输入 " + fmtTokens(usage.prompt_tokens) + " ／ 输出 " + fmtTokens(usage.completion_tokens) +
        (usage.cached_tokens ? "（命中缓存 " + fmtTokens(usage.cached_tokens) + "）" : ""),
      );
    }
    if (p.error) trace.add("bad", "本轮错误：" + p.error);
  }
  slot.running = false;
  /* 收加载圈必须在 `slot.trace` 置空**之前**：它要顺着**这个会话自己的**气泡摘进度行；
     置空之后再收，清扫就只能退回到"当前显示的那条流"→ 会摘掉**别人**正在转的圈
     （探针在"A 收尾不该影响 B"上抓到过）。 */
  turnSpinnerReset(slot, sessionId);
  slot.trace = null;
  slot.messageId = p.message_id || "";
  /* 提醒（应用内那部分）：这一轮不是**当前显示**的会话完成的 → 左栏打角标 + 顶栏计数。
     切过去或点顶栏计数即清除（清除在 openSession / 计数按钮里）。 */
  slot.unread = unreadFromDone(sessionId, state.sessionId, slot.unread);
  renderUnread();
  syncSendState();
  refreshSessionUsage();
  // 列表里的消息条数变了 → 刷一次（刷不动也不影响这一轮的收尾）
  loadSessions().catch(function () {
    /* 后端不可用时列表不刷新，界面仍停在已收尾的状态 */
  });
  const entry = state.streams[String(sessionId)];
  if (entry && entry.pendingHistory && sessionId === state.sessionId) {
    // 极端时序（历史还没渲染完就又发了一轮）：轮次已收尾，这时重绘历史不会再抹掉进行中气泡
    entry.pendingHistory = false;
    renderHistory(sessionId).catch(function () {
      /* 重绘失败就保留现场 */
    });
  }
}


/* ------------------------------------- 搜索意图选择卡（`chat.needs_choice`）

   后端 `research.search` 在"用户没说清要哪类资料"时**不猜也不搜**：回 `needs_choice` 并把
   7 个选项随 `chat.needs_choice` 推上来。载荷形状（`tools/research_search.py::_choice_event`）：
   `{session_id, turn_id, query, options: [{type, label, hint}], intent}` —— 选项由
   `runtime/search_intent.options()` 生成，**前端不自己造选项**，一律照载荷渲染。

   卡片挂在 `streamBox(session_id)` 上，于是三件事是结构保证的：
     - 不是当前显示的会话也收下（切回去能看、能点）；
     - 物理上不在别人的流里 → 不可能串会话；
     - 不改变消息流宽度（卡是流里的一个 flex 子项，宽度随流走，不写死 px）。

   点一条 = 用户**下一条消息**（`send()`），内容见 `CHOICE_PHRASES`：刻意用后端
   `search_intent._EXPLICIT_RULES` 认得的说法，下一轮直接命中规则，不再花一次分类调用。
   同一张卡只生效一次（同步置 `data-answered` + 全部按钮 disabled）；
   同一轮同一个 query 的重复事件不重插（键见 `choiceCardKey`）。

   为什么点击要判 `sessionRunning`：needs_choice 是**轮次中途**由工具推上来的，那一刻这个
   会话自己的一轮还在跑（界面按会话闸门不许并行）。这时点选只提示"等它结束再选"、**不禁用**
   —— 否则会变成"已选但什么都没发"。轮次收尾后卡还在，那时点才真正发出去。 */

/*: 选项类型 → 发出去的那句话。**必须能被后端的显式规则认出**（`runtime/search_intent.py`
    的 `_EXPLICIT_RULES`）：paper/code/tutorial/news/data 用「只看X」，other 用「都可以」，
    none 用「先不检索」。这样下一轮起手就是确定行为，不用再分类一次。 */
var CHOICE_PHRASES = {
  paper: "只看论文",
  code: "只看代码",
  tutorial: "只看教程",
  news: "只看新闻",
  data: "只看数据",
  other: "都可以，全部来源都查",
  none: "先不检索，直接回答",
};

/** 选项 → 用户消息正文（纯函数，node 直测）。未知类型退化成它的 label —— 宁可笨，不发空串。 */
function choiceMessage(option) {
  var opt = option || {};
  var type = String(opt.type || "");
  if (Object.prototype.hasOwnProperty.call(CHOICE_PHRASES, type)) return CHOICE_PHRASES[type];
  var label = String(opt.label || "").trim();
  return label ? "只看" + label : "";
}

/** 去重键：**同一轮 + 同一个 query**。一轮里问了两个不同问题就各得一张卡。 */
function choiceCardKey(params) {
  var p = params || {};
  return String(p.turn_id || "") + "|" + String(p.query || "");
}

/** 这个会话的选择卡表（键 = choiceCardKey）。与 `state.streams` 同寿命。 */
function choiceCards(sessionId) {
  var key = String(sessionId || "");
  if (!state.choices[key]) state.choices[key] = {};
  return state.choices[key];
}

function choiceTitleText(params) {
  var query = String((params || {}).query || "").trim();
  return query ? "这次要搜哪类资料？针对「" + query + "」" : "这次要搜哪类资料？";
}

/** `chat.needs_choice` → 会话流里的一张卡（重复事件返回已存在的那张，不重插）。 */
function renderChoiceCard(sessionId, params) {
  var key = String(sessionId || "");
  if (!key) return null; // 拿不到会话 = 不落任何人的流（宁可不动，也不落错）
  var p = params || {};
  var options = (Array.isArray(p.options) ? p.options : []).filter(function (opt) {
    return opt && (opt.type || opt.label);
  });
  if (!options.length) return null; // 没有选项就不出卡（不猜）
  var cardKey = choiceCardKey(p);
  var bucket = choiceCards(key);
  var existing = bucket[cardKey];
  if (existing && existing.parentNode) return existing;

  var wrap = document.createElement("div");
  wrap.className = "msg msg-choice";
  wrap.setAttribute("data-session", key);
  wrap.setAttribute("data-turn", String(p.turn_id || ""));
  wrap.setAttribute("data-choice-key", cardKey);

  var who = document.createElement("div");
  who.className = "who";
  who.textContent = "ScholarScout · 需要你先定一下";
  wrap.appendChild(who);

  var body = document.createElement("div");
  body.className = "body";
  var title = document.createElement("p");
  title.className = "choice-title";
  title.textContent = choiceTitleText(p);
  body.appendChild(title);

  var list = document.createElement("div");
  list.className = "choice-options";
  options.forEach(function (opt) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "choice-option";
    button.setAttribute("data-type", String(opt.type || ""));
    var label = document.createElement("span");
    label.className = "choice-option-label";
    label.textContent = String(opt.label || opt.type || "");
    button.appendChild(label);
    var hint = String(opt.hint || "").trim();
    if (hint) {
      var hintNode = document.createElement("span");
      hintNode.className = "choice-option-hint";
      hintNode.textContent = hint;
      button.appendChild(hintNode);
      button.title = hint;
    }
    button.addEventListener("click", function () {
      Promise.resolve(answerChoice(wrap, button, opt)).catch(function (error) {
        el("status").textContent = "选择没能发出去：" + error.message;
      });
    });
    list.appendChild(button);
  });
  body.appendChild(list);
  wrap.appendChild(body);
  streamBox(key).appendChild(wrap);
  bucket[cardKey] = wrap;
  // 只在这个会话显示时才滚：后台会话的卡不许动用户正在看的滚动位置
  if (key === String(state.sessionId || "")) {
    el("messages").scrollTop = el("messages").scrollHeight;
  }
  return wrap;
}

/** 点某一条选项：作为**下一条用户消息**发出去，并把这张卡置为已选/禁用（只生效一次）。 */
async function answerChoice(card, button, option) {
  if (!card || card.getAttribute("data-answered")) return false;
  var sessionId = card.getAttribute("data-session") || String(state.sessionId || "");
  var text = choiceMessage(option);
  if (!sessionId || !text) return false;
  if (sessionRunning(sessionId)) {
    // 轮次还在跑：不禁用卡片，只提示（见本节开头"为什么点击要判 sessionRunning"）
    notice("这一轮还在跑：等它结束再选（选完会作为你的下一条消息发出）。", sessionId);
    return false;
  }
  /* 先同步禁用再 await：连点两下也只发一条（判据不依赖异步返回）。 */
  var answered = String((option || {}).type || "");
  card.setAttribute("data-answered", answered);
  card.classList.add("is-answered");
  Array.prototype.slice.call(card.querySelectorAll(".choice-option")).forEach(function (node) {
    node.disabled = true;
    if (node === button) {
      node.classList.add("is-chosen");
      node.setAttribute("aria-pressed", "true");
    }
  });
  var title = card.querySelector(".choice-title");
  if (title) title.textContent = "已选：" + String((option || {}).label || text) + "（已作为你的下一条消息发出）";
  if (sessionId !== String(state.sessionId || "")) await openSession(sessionId);
  await send(text);
  return true;
}


async function send(text) {
  const sessionId = state.sessionId;
  if (!sessionId) return;
  /* 闸**按会话判**：只拦"这个会话自己在跑"。
     原来是全局单飞闸（全应用一份布尔）→ A 在跑时 B 的发送被静默丢掉，
     这就是用户看到的第一个症状（切到别的会话发不出去）。 */
  if (sessionRunning(sessionId)) {
    notice("这个会话已经有一轮在跑：等它结束或点「停止」。想同时干活，切到别的会话另起一轮。", sessionId);
    return;
  }
  const slot = turnSlot(sessionId, true);
  slot.unread = false;
  slot.messageId = "";
  bubble("user", "你", sessionId).body.textContent = text;
  slot.trace = makePending(sessionId);
  slot.running = true;
  slot.spinner = null;
  // 发起一轮就点亮加载圈（合成事件 "turn.start" —— 后端没有这个事件）。
  // 之后 chat.tool_call / chat.tool_result / chat.delta / chat.done / chat.error 各自接棒。
  turnSpinnerUpdate(slot, "turn.start");
  syncSendState();
  renderUnread();

  try {
    /* `mode="async"`：**立即**返回 `turn_id`，不再阻塞整轮（一轮 3–12 分钟）——
       于是 A 在跑时能立刻切到 B 发问。收尾走 chat.done → finishTurn()。
       后端仍然同会话不许两轮并行，且上限 3 个会话（拒绝时这里抛错，如实落进本会话的流）。 */
    const accepted = await scout.call("chat.send", {
      session_id: sessionId,
      text: text,
      mode: "async",
    });
    slot.turnId = accepted && accepted.turn_id ? String(accepted.turn_id) : "";
    refreshSide().catch(function () {
      /* 右侧面板刷不动不影响这一轮 */
    });
  } catch (error) {
    // 本轮根本没跑起来（turn_in_progress / at_capacity / 缺 key）→ 落在**本会话**的流里
    slot.running = false;
    slot.trace.add("bad", "调用失败：" + error.message);
    slot.trace.node.wrap.classList.add("error");
    el("status").textContent = "本轮未发出：" + error.message;
  } finally {
    /* async 下**不能**在这里无条件复位加载圈：整轮还在跑（收尾在 finishTurn）。
       只有"这一轮没在跑"（发起就失败）时才收 —— 防"永久转圈"这条保险仍然成立。 */
    if (!slot.running) turnSpinnerReset(slot, sessionId);
    syncSendState();
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
  const stream = activateStream(created.session_id);
  // 新会话历史本来就是空的：标成"已渲染"，后面切回来不会清屏重绘
  state.streams[String(created.session_id)].rendered = true;
  const empty = document.createElement("p");
  empty.className = "empty";
  empty.textContent = "新会话已建好（标题会在你发出第一句后自动生成）。试试：「帮我找教育智能体方向的入门资料」";
  stream.appendChild(empty);
  await loadSessions();
  renderUnread();
  syncSendState();
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


/** 把一个会话的历史从库里渲染进它自己的流（**只做一次**）。
    之后这个会话的消息都靠事件直接写进这条流 —— 再清屏重绘会把**进行中气泡**抹掉，
    而那正是用户报的"切会话出异常"的老根源。 */
async function renderHistory(sessionId) {
  const key = String(sessionId);
  const stream = streamBox(key);
  const payload = await scout.call("session.messages", { session_id: key, limit: 200 });
  const items = payload.items || [];
  /* **不清屏**：这条流里可能已经有事件先落下的节点 —— 最典型的是别的会话推来的
     选择卡（`chat.needs_choice` 落在它自己那条流里，用户切过去时它必须还在）。
     历史上永远比事件早，所以先把已有节点记下来，渲染完历史再挪到末尾。 */
  const ahead = Array.prototype.slice.call(stream.childNodes);
  items.forEach(function (message) {
    renderMessage(message, key);
  });
  ahead.forEach(function (node) {
    stream.appendChild(node);
  });
  if (!items.length && !ahead.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "这个会话还没有消息。";
    stream.appendChild(empty);
  }
  const entry = state.streams[key];
  if (entry) entry.rendered = true;
  const refs = collectRefs(items);
  await renderRoutes(refs.route_ids || []);
}


async function openSession(sessionId) {
  state.sessionId = sessionId;
  const slot = turnSlot(sessionId, false);
  // 切到它 = 看过了 → 角标与计数即清
  if (slot) slot.unread = false;
  /* 切流：只换"哪条在显示"，**不重建**。A 的进行中气泡留在它自己那条流里，
     切回来时连同已经收到的工具条目/正文一起原样在那（这就是"切会话不丢也不混"）。 */
  activateStream(sessionId);
  const entry = state.streams[String(sessionId)];
  if (entry && !entry.rendered) {
    if (slot && slot.trace) {
      // 这一轮已经在跑、历史还没渲染过：**不能**此刻重绘（会抹掉进行中气泡）
      entry.pendingHistory = true;
    } else {
      await renderHistory(sessionId);
    }
  }
  await loadSessions();
  renderUnread();
  await refreshSessionUsage();  // 累计用量跟着会话走，不是全局
  syncSendState();
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
    // 输入框右侧的模型 / 思考深度下拉：先接线再拉选项（选项来自 models.list 的真实声明）
    wireComposerChoices();
    await refreshComposerMeta();
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
  // 闸按会话判（原来读全局单飞闸：别的会话在跑会把这里也拦住）
  if (sessionRunning(state.sessionId)) {
    el("status").textContent = "本会话有一轮还没结束，等它跑完再点";
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
    // 只拦"这个会话自己在跑"（后端也会拦同会话两轮，界面该先拦住）
    if (sessionRunning(state.sessionId)) {
      notice("这个会话已经有一轮在跑：等它结束或点「停止」（别的会话不受影响）。", state.sessionId);
      return;
    }
    const input = el("input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    send(text);
  });

  el("btn-stop").addEventListener("click", stopTurn);
  // 顶栏计数：点它跳到第一个有新回复的会话（跳过去即清除角标）
  el("unread-count").addEventListener("click", function () {
    const key = Object.keys(state.turns).filter(function (sessionId) {
      return state.turns[sessionId].unread === true;
    })[0];
    if (key) openSession(key);
  });

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
  /*: 每会话一个「进行中」槽（键 = session_id，docs/20 §3.3）：
      `{ turnId, messageId, trace, running, unread, spinner }`。
      **没有全局单飞闸、没有单一进行中槽**了 —— A 在跑时切到 B，A 的事件仍然只落进 A 的槽。
      （这两个老坑的名字已从代码里消失，回归即为 bug，测试按名字钉着。） */
  turns: {},
  /*: 每会话一条消息流容器：`{ [session_id]: { node, rendered, pendingHistory } }`。
      切会话只切"哪条在显示"，绝不重建 —— 进行中气泡留在它自己那条流里。 */
  streams: {},
  /*: 每会话一张「搜索意图选择卡」表：`{ [session_id]: { [turn_id|query]: 卡片节点 } }`。
      卡片本身在**它自己那条流**里（切走/切回来都在），这张表只用于**去重**
      —— 同一轮同一个 query 的重复 `chat.needs_choice` 不再插第二张。 */
  choices: {},
  routes: [],
  cards: [],
  practice: null,
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


// 「停止」：**只取消当前显示的这个会话**（按 session_id 传，后端按会话找它那一轮的取消 Event）。
// 别的会话在跑不受影响；这个会话没有在跑的轮次时如实说（并且提示还有谁在跑）。
// 生效时机照旧如实写：**步间检查** —— 当前这一步结束后才停。
async function stopTurn() {
  const sessionId = state.sessionId;
  if (!sessionId) return;
  const button = el("btn-stop");
  button.disabled = true;
  try {
    const result = await scout.call("chat.cancel", { session_id: sessionId });
    const ok = !!(result && result.ok);
    const note = (result && result.note) || (ok ? "已请求取消" : "这个会话没有在跑的轮次");
    const slot = turnSlot(sessionId, false);
    if (ok && slot && slot.trace) slot.trace.add("warn", note);
    // 这一轮已经收尾（没有「进行中」气泡可挂）就落一条会话流提示 —— 绝不静默丢掉反馈
    else notice(note, sessionId);
    if (!ok) {
      const others = Object.keys(state.turns).filter(function (other) {
        return sessionRunning(other) && other !== sessionId;
      });
      if (others.length) {
        el("status").textContent =
          "「停止」只停当前会话；另外还有 " + others.length + " 个会话在跑（分别切过去停）。";
      }
    }
  } catch (error) {
    const slot = turnSlot(sessionId, false);
    if (slot && slot.trace) slot.trace.add("bad", "取消失败：" + error.message);
    else notice("取消失败：" + error.message, sessionId);
  } finally {
    button.disabled = false;
    syncSendState();
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
        // 卡的编号只有一种含义：它在 `cards[]` 里的下标（0 起）。前端所有 note.* 调用都读
        // `card_index`，所以队列里必须存这个键 —— 只存 `index` 会让 `card.card_index`
        // 取到 undefined（JSON 里整个键消失），后端就落到默认值上（真踩过：review 恒 -32602）。
        card_index: card.card_index,
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
        card_index: item.card_index,
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
      {
        key: "llm.chat.reasoning_effort",
        label: "思考深度（五档并到真实三档；关＝不发思考参数）",
        kind: "select",
      },
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
  {
    title: "用量与预算",
    hint:
      "滚动 24 小时统计（不是自然日：自然日会在午夜突然\"回血\"）；超限会如实收口并拒绝新轮次。" +
      "留空或填 0 表示不限，默认就是不限。",
    fields: [
      {
        key: "runtime.daily_token_budget",
        label: "每日 token 上限",
        kind: "number",
        nullable: true, // 留空 = 明确表达"不限"（传 null），不是"不修改"
        note: "滚动 24 小时统计，超限会如实收口（stop_reason=budget）并拒绝新轮次。",
        usage: true, // 这一行要显示 chat.status 的 global_budget（已用/上限/是否超限）
      },
    ],
  },
  {
    title: "跑完提醒",
    hint: "开启后：非当前会话跑完时，用飞书私聊给你推一条摘要（标题 + 首行 + 收口原因）。",
    fields: [
      {
        key: "runtime.notify_turn_done",
        label: "跑完提醒",
        kind: "bool",
        note: "平台：飞书（私聊；仅非当前会话；60 秒内最多一条）",
      },
    ],
  },
];


/*: 设置浮层的**分类**（用户反馈"一个大页面太臃肿"）。
 *  `sections` 是 SETTINGS_SPEC 里的组标题（按标题归位，字段名只写一遍）；
 *  空 `sections` 的类别由 openSettings 按顺序塞卡片（渠道 / 数据 / 外观与关于）。
 *  没在任何 `sections` 里出现过的组会被放进「外观与关于」—— 不静默丢内容。 */
var SETTINGS_CATEGORIES = [
  {
    name: "model",
    title: "模型与生成",
    hint: "对话与摘要模型、生成策略",
    sections: ["模型", "生成策略", "用量与预算"],
  },
  {
    name: "channel",
    title: "渠道",
    hint: "把飞书等渠道接进桌面应用",
    sections: ["跑完提醒"],
  },
  {
    name: "search",
    title: "检索与网络",
    hint: "搜索引擎、预算与抓取",
    sections: ["搜索引擎", "检索与预算"],
  },
  {
    name: "data",
    title: "数据与存储",
    hint: "占用、清理、数据库整理与解析兜底",
    sections: [],
  },
  {
    name: "appearance",
    title: "外观与关于",
    hint: "只在本机生效的外观设置，以及版本信息",
    sections: [],
  },
];


/*: 个人中心的三项（与设置同一套「左侧表项 + 右侧内容」组件）。
 *  前两项分别装 index.html 里的静态区块（画像 / 记忆），第三项装现算的学习统计。
 *  name 同时是面板的 `data-cat` 与 `ui-pane-<name>` 的 id 后缀。
 */
var PROFILE_CATEGORIES = [
  {
    name: "portrait",
    title: "学习者画像",
    hint: "跨会话长期画像：主题、路线、地图与掌握度",
  },
  {
    name: "memory",
    title: "跨会话记忆",
    hint: "长期记住的偏好 / 背景 / 目标，可增可删",
  },
  {
    name: "stats",
    title: "学习统计",
    hint: "路线汇总 / 笔记 / 抽背卡 / 研究看板（跨全部会话）",
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
