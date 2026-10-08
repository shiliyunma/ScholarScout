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
