/* 学习路线（含时间线/日历）、抽背卡、笔记
   从 app.js 拆出来（原来 3246 行一个文件）。同名函数仍是全局声明，
   跨文件调用与拆分前完全一样 —— 不需要打包器，也不需要 import。 */

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
  // 用户要求：一个对话只有一份学习路线，直接展示全部内容，不用再点按钮。
  // 列表留着（将来一份会话里要是有多份仍可切换），但不再把它当入口。
  list.hidden = true;
  if (routeIds.length && !state.routeShown) {
    state.routeShown = routeIds[0];
    await showRoute(routeIds[0]);
  }
}


async function showRoute(routeId) {
  const payload = await scout.call("route.get", { route_id: routeId });
  const plan = payload.plan || {};
  const box = el("route-detail");
  box.innerHTML = "";
  attachDetailClose(box, "从列表里点一条，看它的摘要与详情。");
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
    li.className = "card-row";
    const due = Number(item.card_due || 0);
    const total = Number(item.card_total || 0);
    // 组上显示到期张数（大浩要求）：看不出该练哪组，列表就只是个目录
    li.innerHTML =
      esc(short(item.title || "抽背卡", 22)) +
      '<span class="meta">' +
      (total ? "到期 " + due + " / 共 " + total : "无卡片") +
      "｜" + esc(short(item.created_at || "", 10)) +
      "</span>";
    // 点一组就练那一组（大浩选定）：有到期只练到期的，没到期就练整组
    li.addEventListener("click", function () {
      startPractice(item.id, { dueOnly: due > 0 });
    });
    const peek = document.createElement("button");
    peek.type = "button";
    peek.className = "ghost mini";
    peek.textContent = "看内容";
    peek.title = "只看这组卡片内容，不开练";
    peek.addEventListener("click", function (event) {
      event.stopPropagation();
      showCard(item.id);
    });
    li.appendChild(peek);
    list.appendChild(li);
  });
  refreshCardStats();
}


async function showCard(noteId) {
  const note = await scout.call("note.get", { note_id: noteId });
  const box = el("card-detail");
  box.innerHTML = "";
  attachDetailClose(box, "从列表里点一条，看它的摘要与详情。");
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
  // 大浩选定：保存进度 —— 上次练到第几张，这次接着练（按卡组分别记）
  restorePracticeProgress();
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
  // ③ 按题型给作答方式：选择题点字母、填空题填词 —— 都走本地判分（note.grade，零模型调用）
  // 所有题型都留输入区域（选择题另有选项按钮，点选项=填入并判定）
  body.appendChild(practiceAnswerInput(card));
  if (practice.revealed) {
    const back = document.createElement("div");
    back.className = "practice-back";
    // ② 答案不随题下发：只有点过「看答案」才会在本地缓存里出现
    back.textContent = practiceAnswer(card) || "（点「看答案」取答案）";
    body.appendChild(back);
  } else {
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = "先在心里回答，再点「看答案」（Enter）。";
    body.appendChild(hint);
  }
  // 大浩要求去掉「会/模糊/不会」自测：默认在输入框里作答即可。
  // 复习排期改由**判分结果自动推导**（见 autoReview），不再打扰用户。
  body.appendChild(practiceNav());
  if (card.rating) {
    const previous = document.createElement("p");
    previous.className = "hint";
    previous.textContent = "上次评价：" + (RATING_LABELS[card.rating] || card.rating);
    body.appendChild(previous);
  }
  // ③ 「不会」与「主动延伸」——只有这两个按钮会产生模型费用
  body.appendChild(practiceHelpPanel(card));
  el("btn-practice-reveal").textContent = practice.revealed ? "隐藏答案" : "看答案";
  savePracticeProgress();
}


/* ② 抽背卡的稳定编号与答案缓存（答案只在看过之后才在本地存在） */
function cardKey(card) {
  return String(card.card_id || card.note_id + "#" + card.card_index);
}


function practiceAnswer(card) {
  const practice = state.practice;
  if (!practice || !practice.answers) return "";
  return practice.answers[cardKey(card)] || "";
}


async function revealPractice() {
  const practice = state.practice;
  if (!practice) return;
  if (practice.revealed) {
    practice.revealed = false;
    renderPractice();
    return;
  }
  const card = practice.cards[practice.index];
  const key = card ? cardKey(card) : "";
  practice.answers = practice.answers || {};
  if (card && !practice.answers[key]) {
    try {
      // ② 答案谜底分离：点「看答案」时单独取一次，题目里从来没有它
      const payload = await scout.call("note.reveal", {
        note_id: card.note_id,
        card_index: card.card_index,
      });
      practice.answers[key] = payload.back || "";
    } catch (error) {
      el("status").textContent = "取答案失败：" + error.message;
      return;
    }
  }
  practice.revealed = true;
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
  // 练完小结（大浩选定）：练了几张、对几个、下次复习时间 —— 只是"到此为止"太干巴
  const done = practiceDone();
  const answered = Object.keys(done).filter(function (key) {
    return done[key];
  });
  const right = answered.filter(function (key) {
    return done[key] === "good";
  }).length;
  const stat = document.createElement("p");
  stat.className = "practice-summary";
  stat.textContent =
    "本轮 " + (state.practice ? state.practice.cards.length : 0) + " 张｜作答 " + answered.length + "｜答对 " + right +
    (state.practice && state.practice.lastSchedule
      ? "｜最近一张下次复习：" + state.practice.lastSchedule.label
      : "");
  body.appendChild(stat);
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
  attachDetailClose(box, "从列表里点一条，看它的摘要与详情。");

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


/* ② 学习路线只有一个 → 「放大」时把已经渲染好的详情区**整个 DOM 搬进浮层**，关闭时搬回原位。
   不重新取数、不重建视图 —— 面板里是时间线还是日历，浮层里就是同一份，零形状假设。 */
function zoomRoute() {
  const overlay = el("route-zoom");
  const body = el("route-zoom-body");
  const detail = el("route-detail");
  if (!overlay || !body || !detail) return;
  detail.dataset.homeIndex = String(Array.prototype.indexOf.call(detail.parentNode.children, detail));
  body.appendChild(detail);
  overlay.hidden = false;
}


function closeRouteZoom() {
  const overlay = el("route-zoom");
  const pane = el("pane-routes");
  const detail = el("route-detail");
  if (overlay) overlay.hidden = true;
  if (!pane || !detail) return;
  const index = Number(detail.dataset.homeIndex || 0);
  const siblings = Array.prototype.slice.call(pane.children).filter(function (node) {
    return node !== detail && node.id !== "route-zoom-body";
  });
  const before = siblings[index] || null;
  if (before && before.nextSibling) pane.insertBefore(detail, before.nextSibling);
  else pane.appendChild(detail);
}


function wireRouteZoom() {
  const zoom = el("btn-route-zoom");
  if (zoom) zoom.addEventListener("click", zoomRoute);
  const closer = el("btn-route-zoom-close");
  if (closer) closer.addEventListener("click", closeRouteZoom);
}


if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wireRouteZoom);
else wireRouteZoom();


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

/* ③ 抽背的作答方式与模型求助。分层：渲染与本地判分在这里，判分逻辑在后端
   （state/learning_cards.grade_card），模型编排在 runtime/practice.py —— 界面只负责调用与展示。 */

/* 选择题点字母 / 填空题填词 */
function practiceAnswerInput(card) {
  const box = document.createElement("div");
  box.className = "practice-answer";
  const store = practiceResponses();
  const key = cardKey(card);
  if (card.type === "choice") {
    const row = document.createElement("div");
    row.className = "practice-options";
    choiceOptions(card).forEach(function (option) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "ghost practice-option";
      // 选项按钮显示**真实选项文字**（用户：要给正确答案和错误答案里面选一个）
      button.textContent = option.text ? option.letter + ". " + option.text : option.letter;
      button.addEventListener("click", function () {
        store[key] = option.letter; // 交上去的仍是字母，判分在后端本地做
        const field = box.querySelector("input");
        if (field) field.value = option.letter;
        gradePractice(card, option.letter, box);
      });
      row.appendChild(button);
    });
    box.appendChild(row);
  }
  const input = document.createElement("input");
  input.placeholder = card.type === "choice" ? "也可以直接输入选项字母（回车判定）" : "在这里作答，回车判定";
  input.value = store[key] || "";
  input.addEventListener("keydown", function (event) {
    if (event.key !== "Enter") return;
    event.preventDefault();
    store[key] = input.value;
    gradePractice(card, input.value, box);
  });
  box.appendChild(input);
  return box;
}


/* 从题干里把选项拆出来（A. 文本／B、文本）—— **让用户在正确与错误选项里挑一个**。
   题干里认不出选项才退回字母按钮（那说明这张卡的题干没按格式写）。 */
function choiceOptions(card) {
  const text = String(card.front || "")
    .split("\r")
    .join(" ")
    .split("\n")
    .join(" ");
  const marks = [];
  const marker = /\s*([A-D])[.、)）]\s*/g;
  let hit = marker.exec(text);
  while (hit !== null) {
    marks.push({ letter: hit[1], start: hit.index, end: marker.lastIndex });
    hit = marker.exec(text);
  }
  const found = [];
  marks.forEach(function (mark, index) {
    const stop = index + 1 < marks.length ? marks[index + 1].start : text.length;
    const label = text.slice(mark.end, stop).trim().split(/\s+/).join(" ");
    if (!label) return;
    if (
      found.some(function (item) {
        return item.letter === mark.letter;
      })
    )
      return;
    found.push({ letter: mark.letter, text: label });
  });
  if (found.length) return found;
  return ["A", "B", "C", "D"].map(function (letter) {
    return { letter: letter, text: "" };
  });
}


async function gradePractice(card, response, box) {
  let verdict = box.querySelector(".practice-verdict");
  if (!verdict) {
    verdict = document.createElement("p");
    verdict.className = "hint practice-verdict";
    box.appendChild(verdict);
  }
  verdict.className = "hint practice-verdict";
  verdict.textContent = "判定中…";
  try {
    const result = await scout.call("note.grade", {
      note_id: card.note_id,
      card_index: card.card_index,
      response: response,
    });
    if (result.graded) {
      verdict.textContent = (result.correct ? "✓ 对了" : "✗ 不对") + "｜参考答案：" + (result.expected || "");
      verdict.className = "hint practice-verdict " + (result.correct ? "is-right" : "is-wrong");
      // 判分结果自动记一次复习（自测已去掉，排期不能因此断掉）
      await autoReview(card, result.correct ? "good" : "bad", response, result.correct);
    } else {
      verdict.textContent = (result.reason || "这道题本地判不了") + "——可以点「不会，讲一下」让模型看看。";
    }
  } catch (error) {
    verdict.textContent = "判定失败：" + error.message;
  }
}


function practiceResponses() {
  if (!state.practice) return {};
  state.practice.responses = state.practice.responses || {};
  return state.practice.responses;
}


/* 「不会」与「主动延伸」；只有这两个按钮会产生模型费用 */
function practiceHelpPanel(card) {
  const box = document.createElement("div");
  box.className = "practice-help";
  const row = document.createElement("div");
  row.className = "practice-help-buttons";
  const ask = document.createElement("button");
  ask.type = "button";
  ask.textContent = "不会，讲一下";
  ask.addEventListener("click", function () {
    runPracticeHelp(card, box, ask);
  });
  const more = document.createElement("button");
  more.type = "button";
  more.className = "ghost";
  more.textContent = "主动延伸（2 道）";
  more.addEventListener("click", function () {
    runPracticeExtension(card, box, more);
  });
  row.appendChild(ask);
  row.appendChild(more);
  const history = document.createElement("button");
  history.type = "button";
  history.className = "ghost";
  history.textContent = "作答历史";
  history.title = "看这张卡每次答了什么、对不对（不花 token）";
  history.addEventListener("click", function () {
    runPracticeHistory(card, box, history);
  });
  row.appendChild(history);
  box.appendChild(row);
  const note = document.createElement("p");
  note.className = "hint";
  note.textContent = "这两个按钮会调用模型（带上这张卡的复习记录与该会话资料）；判分不会。";
  box.appendChild(note);
  return box;
}


function practiceHelpOutput(box, placeholder) {
  let out = box.querySelector(".practice-help-out");
  if (!out) {
    out = document.createElement("div");
    out.className = "practice-help-out";
    box.appendChild(out);
  }
  out.textContent = placeholder;
  return out;
}


async function runPracticeHelp(card, box, button) {
  // 大浩选定「分两步」：第一次点给**思路提示**（不给答案），再点一次才给完整讲解
  const key = cardKey(card);
  const stages = practiceHelpStage();
  stages[key] = stages[key] === "hint" ? "full" : "hint";
  const stage = stages[key];
  button.disabled = true;
  button.textContent = stage === "hint" ? "还是不会，详细讲" : "再看一遍思路";
  const out = practiceHelpOutput(box, stage === "hint" ? "正在给思路提示…" : "正在给完整讲解…");
  try {
    const result = await scout.call("note.help", {
      note_id: card.note_id,
      card_index: card.card_index,
      response: practiceResponses()[key] || "",
      stage: stage,
    });
    out.textContent =
      (stage === "hint" ? "【思路提示】" : "【完整讲解】") +
      (result.text || result.reason || "（模型没有返回内容）");
    // 求助过 = 这张卡不会：排期记一次 fuzzy（没自测按钮之后，靠这个保证复习计划不断）
    await autoReview(card, "fuzzy", practiceResponses()[key] || "", null);
  } catch (error) {
    out.textContent = "问模型失败：" + error.message;
  } finally {
    button.disabled = false;
  }
}


/* 作答历史（大浩选定：每题每次作答都留痕、可回看）。只读接口，零模型调用。 */
async function runPracticeHistory(card, box, button) {
  button.disabled = true;
  const out = practiceHelpOutput(box, "正在读历史…");
  try {
    const payload = await scout.call("note.history", {
      note_id: card.note_id,
      card_index: card.card_index,
    });
    const items = payload.items || [];
    if (!items.length) {
      out.textContent = "这张卡还没有作答记录。";
      return;
    }
    out.textContent =
      "共 " + items.length + " 次（按时间正序，最早在前）：" +
      items
        .map(function (item, index) {
          const when = String(item.reviewed_at || "").slice(0, 16).replace("T", " ");
          const verdict = item.correct === 1 ? "✓ 对" : item.correct === 0 ? "✗ 错" : "— 未判对错";
          const answer = item.response ? "答：" + item.response : "答：（这条老记录没留作答）";
          return index + 1 + ". " + when + "｜" + item.rating + "｜" + verdict + "｜" + answer;
        })
        .join(String.fromCharCode(10));
  } catch (error) {
    out.textContent = "读历史失败：" + error.message;
  } finally {
    button.disabled = false;
  }
}


async function runPracticeExtension(card, box, button) {
  button.disabled = true;
  const out = practiceHelpOutput(box, "正在出延伸题…");
  try {
    const result = await scout.call("note.extend", {
      note_id: card.note_id,
      card_index: card.card_index,
      response: practiceResponses()[cardKey(card)] || "",
    });
    const questions = result.questions || [];
    if (!questions.length) {
      out.textContent = "模型这次没有按格式出题（没有往库里塞废卡），可以再试一次。";
      return;
    }
    out.textContent =
      "已出 " + questions.length + " 道延伸题" +
      (result.stored ? "，并已存成新卡（在卡片页能看到）" : "") + "：\n" +
      questions.map(function (item, index) {
        return (index + 1) + ". " + item.front;
      }).join(String.fromCharCode(10));
  } catch (error) {
    out.textContent = "延伸失败：" + error.message;
  } finally {
    button.disabled = false;
  }
}

/* 上一题 / 下一题 / 点击题号跳转；做过的题有标识（本次会话内） */
function practiceNav() {
  const practice = state.practice;
  const box = document.createElement("div");
  box.className = "practice-nav";
  const index = document.createElement("div");
  index.className = "practice-index";
  practice.cards.forEach(function (item, position) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost practice-jump";
    button.textContent = String(position + 1);
    if (practiceDone()[cardKey(item)]) button.classList.add("is-done");
    if (position === practice.index) button.classList.add("is-current");
    button.addEventListener("click", function () {
      practice.index = position;
      practice.revealed = false;
      renderPractice();
    });
    index.appendChild(button);
  });
  box.appendChild(index);
  const row = document.createElement("div");
  row.className = "practice-nav-buttons";
  const prev = document.createElement("button");
  prev.type = "button";
  prev.className = "ghost";
  prev.textContent = "上一题";
  prev.disabled = practice.index <= 0;
  prev.addEventListener("click", function () {
    practice.index = Math.max(0, practice.index - 1);
    practice.revealed = false;
    renderPractice();
  });
  const next = document.createElement("button");
  next.type = "button";
  next.className = "ghost";
  next.textContent = practice.index + 1 >= practice.cards.length ? "完成" : "下一题";
  next.addEventListener("click", function () {
    practice.index = Math.min(practice.cards.length, practice.index + 1);
    practice.revealed = false;
    renderPractice();
  });
  row.appendChild(prev);
  row.appendChild(next);
  box.appendChild(row);
  return box;
}


/* 抽背进度：按卡组记住练到第几张（localStorage），下次打开接着练。
   用第一张卡的 note_id 当键 —— 一组卡共享一个 note_id，跨会话也不会串。 */
function practiceProgressKey() {
  const practice = state.practice;
  const first = practice && practice.cards && practice.cards[0];
  return "scout.practice.progress." + ((first && first.note_id) || "all");
}


function restorePracticeProgress() {
  const practice = state.practice;
  if (!practice || practice.restored) return;
  practice.restored = true;
  try {
    const saved = Number(window.localStorage.getItem(practiceProgressKey()) || "0");
    // 只在没动过的时候恢复：用户自己翻过页就别把他拽回去
    if (saved > 0 && practice.index === 0 && saved < practice.cards.length) {
      practice.index = saved;
    }
  } catch (error) {
    /* localStorage 不可用就当没存过 —— 不该因为记进度失败而打不开抽背 */
  }
}


function savePracticeProgress() {
  const practice = state.practice;
  if (!practice || !practice.cards) return;
  try {
    window.localStorage.setItem(practiceProgressKey(), String(practice.index));
  } catch (error) {
    /* 同上：记不上就算了 */
  }
}


/* 两步求助走到哪一档了（按卡片记住：第一档思路、第二档完整讲解） */
function practiceHelpStage() {
  if (!state.practice) return {};
  state.practice.helpStage = state.practice.helpStage || {};
  return state.practice.helpStage;
}


/* 本次练习里"做过的题"：判过分 / 求助过都算 —— 题号上给个标识，跳回去也知道哪些碰过 */
function practiceDone() {
  if (!state.practice) return {};
  state.practice.done = state.practice.done || {};
  return state.practice.done;
}


/* 判分结果 → 记一次复习（排期不再问用户，但记录不能断） */
async function autoReview(card, rating, response, correct) {
  if (!state.practice) return;
  const key = cardKey(card);
  if (practiceDone()[key]) return;      // 同一张卡一次练习里只记一次，避免反复回车刷多笔
  practiceDone()[key] = rating;
  try {
    await scout.call("note.review", {
      note_id: card.note_id,
      card_index: card.card_index,
      rating: rating,
      // 每题每次作答都留痕（大浩选定）：把当时答了什么、对不对一起记下来
      response: response === undefined || response === null ? "" : String(response),
      correct: typeof correct === "boolean" ? correct : null,
    });
  } catch (error) {
    practiceDone()[key] = "";
  }
}

