/* 个人中心（路线汇总/日历/笔记/抽背/看板汇总）与设置面板
   从 app.js 拆出来（原来 3246 行一个文件）。同名函数仍是全局声明，
   跨文件调用与拆分前完全一样 —— 不需要打包器，也不需要 import。 */

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


/* 浮层两栏的「左侧表项 + 右侧内容」组件（设置与个人中心共用）。
 *
 * 为什么是表项而不是可折叠分组：用户要的是「点左边一项 → 右边整屏换成那一项的内容」。
 * 折叠分组做不到"同一时刻只呈现一组、左边又始终列着全部组"。
 *
 * 结构：`nav.ui-rail`（role=tablist，每项一个 `button[role=tab]`）
 *      + `div.ui-panes`（每项一个 `section[role=tabpanel]`）。
 * **切换只切可见性**（`hidden` + `is-active`），不重建、不重渲 ——
 * 重画会把用户正在填的输入框、展开的日历状态一起清掉。
 *
 * 键盘：表项本身是真按钮（Tab 可达、Enter/空格原生触发）；另加 ↑/↓/←/→ 与 Home/End
 * 在表项间移动并按 WAI-ARIA tabs 的惯例**同时激活**（不用再按一次 Enter）。
 *
 * 返回 `{ nav, panes, panels, buttons, select }`。分类名写在 `data-cat` 上（探针按它读），
 * 面板 id 是生成的 `ui-pane-<name>` —— 一个既有 id 都不动。
 */
function uiRail(specs) {
  var nav = document.createElement("nav");
  nav.className = "ui-rail";
  nav.setAttribute("role", "tablist");
  nav.setAttribute("aria-orientation", "vertical");
  var panes = document.createElement("div");
  panes.className = "ui-panes";
  var buttons = [];
  var panels = {};

  /* 选中一项：只切可见性 + 选中态，面板里的 DOM 一律不动。 */
  function select(name) {
    buttons.forEach(function (button) {
      var on = button.getAttribute("data-cat") === name;
      button.classList.toggle("is-active", on);
      button.setAttribute("aria-selected", on ? "true" : "false");
    });
    Object.keys(panels).forEach(function (key) {
      var on = key === name;
      panels[key].hidden = !on;
      panels[key].classList.toggle("is-active", on);
    });
    return name;
  }

  specs.forEach(function (spec, index) {
    var pane = document.createElement("section");
    pane.className = "ui-pane";
    pane.id = "ui-pane-" + spec.name;
    pane.setAttribute("role", "tabpanel");
    pane.setAttribute("data-cat", spec.name);
    pane.hidden = true;
    panes.appendChild(pane);
    panels[spec.name] = pane;

    var button = document.createElement("button");
    button.type = "button";
    button.className = "ui-rail-item";
    button.setAttribute("role", "tab");
    button.setAttribute("data-cat", spec.name);
    button.setAttribute("aria-controls", pane.id);
    button.setAttribute("aria-selected", "false");
    var title = document.createElement("strong");
    title.textContent = spec.title;
    button.appendChild(title);
    if (spec.hint) {
      var tip = document.createElement("span");
      tip.className = "ui-rail-hint";
      tip.textContent = spec.hint;
      button.appendChild(tip);
    }
    button.addEventListener("click", function () {
      select(spec.name);
    });
    button.addEventListener("keydown", function (event) {
      var key = event.key;
      var step = key === "ArrowDown" || key === "ArrowRight" ? 1 : 0;
      if (key === "ArrowUp" || key === "ArrowLeft") step = -1;
      var next = -1;
      if (step) next = (index + step + buttons.length) % buttons.length;
      else if (key === "Home") next = 0;
      else if (key === "End") next = buttons.length - 1;
      if (next < 0) return;
      event.preventDefault();
      var target = buttons[next];
      if (target.focus) target.focus();
      select(target.getAttribute("data-cat"));
    });
    nav.appendChild(button);
    buttons.push(button);
  });

  /* 默认选中第一项（用户要求：打开就落在第一组，不用先点一下）。 */
  select(specs[0].name);
  return { nav: nav, panes: panes, panels: panels, buttons: buttons, select: select };
}


/*: 个人中心的分类（用户反馈"也分类一下"）。
 *  三组：学习者画像 / 跨会话记忆 / 学习统计。前两组就是 index.html 里**静态**写着的两个区块 ——
 *  这里的做法是把**节点搬进**对应面板（appendChild 搬节点，id 一个都不变，
 *  board.js 的取值逻辑不受影响），第三组装原来由 renderProfile 现算的那几段。
 *
 *  为什么必须"只搭一次壳"：老实现是 `profile-body.innerHTML = ""` 再拼四段 —— 那会把静态的
 *  画像 / 记忆区块整块删掉（打开一次个人中心，画像与记忆就永远消失了）。现在改成：
 *  左侧表项 + 右侧面板的壳只建一次，每次刷新只重画「学习统计」那一格里的内容。
 */
function profileCategories() {
  var body = el("profile-body");
  var stats = body.querySelector(".ui-pane[data-cat='stats']");
  if (stats) return { stats: stats };

  var shell = uiRail(PROFILE_CATEGORIES);
  body.classList.add("ui-split");
  // 两栏壳放到最前面（万一有静态区块没被搬走，也不会掉在表项下面）
  body.insertBefore(shell.panes, body.firstChild);
  body.insertBefore(shell.nav, shell.panes);

  var portraitBlock = el("profile-overview") ? el("profile-overview").closest(".board-block") : null;
  var memoryBlock = el("memory-input") ? el("memory-input").closest(".board-block") : null;
  if (portraitBlock) shell.panels.portrait.appendChild(portraitBlock);
  if (memoryBlock) shell.panels.memory.appendChild(memoryBlock);
  return { stats: shell.panels.stats };
}


async function renderProfile() {
  var cats = profileCategories();
  cats.stats.textContent = "";
  cats.stats.appendChild(await profileRoutes());
  cats.stats.appendChild(await profileNotes());
  cats.stats.appendChild(await profileCards());
  cats.stats.appendChild(await profileBoard());
  /* 画像与记忆这两格的内容原本只在 `loadBoard()` → `loadProfile()/loadMemory()` 里刷新，
     而 `loadBoard()` 只由 `refreshSide()`（**首轮发送之后**）调用 —— 启动后直接打开个人中心，
     「学习者画像」这一栏是空的（分成表项之后这一栏被单独看见，空面板特别明显）。
     这里补一次刷新：只往静态节点里填内容，不重建节点（`renderProfile` 也不碰 pane 结构）。 */
  await Promise.all([loadProfile(), loadMemory()]);
  el("profile-note").textContent = "汇总跨全部会话";
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
        "熟 " + rate + "｜熟 " + item.mastered + "／复习中 " + item.reviewing + "／薄弱 " + item.weak +
        "｜" + item.knowledge_type + " 掌握度 " + item.level + "（门槛 " + item.gate +
        "，达标 " + item.above_gate + "）" + (item.next ? "｜下一个：" + item.next.title : "");
      li.appendChild(meta);
      list.appendChild(li);
    });
    box.appendChild(list);
  }
  return box;
}


async function profileBoard() {
  var box = profileSection("研究看板汇总", "按会话汇总跨全部会话的调研：目标、子问题、检索次数与已产出物。");
  var payload;
  try {
    payload = await scout.call("research.overview", {});
  } catch (error) {
    box.appendChild(plainHint("读不到看板汇总：" + error.message));
    return box;
  }
  var totals = payload.totals || {};
  var nums = document.createElement("p");
  nums.className = "profile-nums";
  nums.textContent =
    totals.sessions + " 个会话" +
    "｜来源 " + totals.sources + "（无摘要 " + totals.without_summary + "）" +
    "｜子问题 " + totals.covered + "/" + totals.questions +
    (totals.coverage_rate === null || totals.coverage_rate === undefined
      ? ""
      : "（覆盖 " + Math.round(totals.coverage_rate * 100) + "%）") +
    "｜检索 " + totals.queries + " 次" +
    "｜地图 " + totals.maps + "｜路线 " + totals.routes +
    "｜笔记 " + totals.notes + "｜卡片 " + totals.cards;
  box.appendChild(nums);

  var items = payload.sessions || [];
  if (!items.length) {
    box.appendChild(plainHint("还没有带来源的会话：先去调研一个题目。"));
    return box;
  }
  var list = document.createElement("ul");
  list.className = "list profile-list";
  items.forEach(function (item) {
    var li = document.createElement("li");
    var name = document.createElement("span");
    name.className = "profile-session";
    name.textContent = item.title;
    name.title = item.goal || "";
    li.appendChild(name);
    var meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent =
      "来源 " + item.sources + "｜子问题 " + item.covered + "/" + item.questions +
      "｜检索 " + item.queries +
      "｜地图 " + item.maps + "｜路线 " + item.routes + "｜卡片 " + item.cards +
      (item.updated_at ? "｜" + String(item.updated_at).slice(0, 10) : "");
    li.appendChild(meta);
    list.appendChild(li);
  });
  box.appendChild(list);
  return box;
}


function fieldId(key) {
  return "set-" + key.replace(/\./g, "-");
}


/** 每日 token 上限旁边的「当前用量」：读 `chat.status` 的 `global_budget`（滚动 24h）。
 *
 *  未配上限时后端回 `enabled=false` + `used=null` —— **不假报 0**（0 会被读成"已经用了 0"、
 *  进而以为"上限 0"），这里如实写"不限量"。读不到就写读不到，不编一个数字。 */
async function renderBudgetUsage() {
  var box = el(fieldId("runtime.daily_token_budget") + "-usage");
  if (!box) return;
  var status;
  try {
    status = await scout.call("chat.status", {});
  } catch (error) {
    box.textContent = "当前用量：读不到（" + error.message + "）";
    return;
  }
  var global = (status && status.global_budget) || null;
  if (!global) {
    box.textContent = "当前用量：后端没有报 global_budget";
    return;
  }
  var tokens = global.tokens || {};
  if (global.enabled !== true || tokens.limit === null || tokens.limit === undefined) {
    var window = Number(global.window_hours || 24);
    box.textContent = "当前用量：不限量（滚动 " + window + " 小时统计，没设上限就不拦）";
    return;
  }
  var used = Number(tokens.used || 0);
  var limit = Number(tokens.limit || 0);
  box.textContent =
    "当前用量：已用 " +
    fmtTokens(used) +
    " / 上限 " +
    fmtTokens(limit) +
    " tokens（滚动 " +
    Number(global.window_hours || 24) +
    " 小时）" +
    (global.exceeded === true ? "｜已超限，新轮次会被拒绝" : "");
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
  } else if (field.kind === "select") {
    /* 档位下拉：选项来自 `models.list`（与输入框那个下拉同一份回包），label 里带真实档位。
       读不到清单时给一个占位项而不是空下拉 —— 空下拉会让人以为"没有可选值"。
       注意分支顺序：secret 分支必须紧邻最后的 else（守卫测试从 `secret` 切到 `} else {`
       检查"密钥框不许预填 value"）；select 放前面，别把那个切片撑大。 */
    input = document.createElement("select");
    const levels = (MODEL_CHOICES && MODEL_CHOICES.level_options) || [];
    if (!levels.length) {
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "（模型清单未读到，重新打开设置试试）";
      input.appendChild(placeholder);
    }
    levels.forEach(function (item) {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = item.label || item.value;
      option.disabled = item.supported === false;
      input.appendChild(option);
    });
    input.value =
      values[field.key] === undefined || values[field.key] === null
        ? ""
        : String(values[field.key]);
  } else if (field.kind === "secret") {
    input = document.createElement("input");
    input.type = "password";
    input.autocomplete = "new-password";
    input.placeholder = secrets[field.key] ? "已配置（留空则不修改）" : "未配置";
  } else {
    input = document.createElement("input");
    input.type = field.kind === "number" ? "number" : "text";
    if (field.step) input.step = field.step;
    // 可空数字框：值缺失就留空，并把"留空 = 不限"写在占位符上（不再靠用户猜）
    if (field.nullable) input.placeholder = "留空 = 不限";
    input.value = values[field.key] === undefined || values[field.key] === null ? "" : String(values[field.key]);
  }
  input.id = fieldId(field.key);
  if (field.kind === "bool") input.className = "set-check";
  row.appendChild(input);
  /* 只读说明（如"平台：飞书…"）与当前用量：跨两列放在输入下面，不作为可点区域的一部分。 */
  if (field.note) {
    var note = document.createElement("span");
    note.className = "hint set-field-note";
    note.textContent = field.note;
    row.appendChild(note);
  }
  if (field.usage) {
    var usage = document.createElement("span");
    usage.className = "hint set-field-usage";
    usage.id = fieldId(field.key) + "-usage";
    usage.setAttribute("data-usage-for", field.key);
    usage.textContent = "当前用量：读取中…"; // 读不到就如实说读不到（renderBudgetUsage 会改写）
    row.appendChild(usage);
  }
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

  /* 「恢复默认窗口大小」：入口放在**外观**里（用户原话：在设置里能恢复默认）。
     调的是桥上的真实方法 `win_reset_geometry`（bridge.Bridge.win_reset_geometry），
     不是 RPC —— 所以走 `bridgeCall`，与窗控按钮同一条路径（后端忙时也点得动）。
     只清 `ui.window`，**不动当前窗口**：把正在看的窗口突然拉成 1440x900 会顶掉内容，
     而「下次启动生效」也更好解释。 */
  var sizeRow = document.createElement("div");
  sizeRow.className = "set-row";
  var sizeLabel = document.createElement("span");
  sizeLabel.textContent = "窗口大小";
  sizeRow.appendChild(sizeLabel);
  var sizeWrap = document.createElement("div");
  sizeWrap.style.display = "flex";
  sizeWrap.style.gap = "8px";
  sizeWrap.style.alignItems = "center";
  var sizeButton = document.createElement("button");
  sizeButton.type = "button";
  sizeButton.id = "set-window-reset";
  sizeButton.className = "ghost";
  sizeButton.textContent = "恢复默认窗口大小";
  var sizeHint = document.createElement("span");
  sizeHint.className = "hint";
  sizeHint.id = "set-window-reset-hint";
  sizeHint.textContent = "记住当前大小与位置，下次启动照此打开";
  sizeButton.addEventListener("click", async function () {
    sizeHint.textContent = "正在恢复…";
    try {
      await bridgeCall("win_reset_geometry");
      sizeHint.textContent = "已恢复默认，下次启动生效";
    } catch (error) {
      sizeHint.textContent = "恢复失败：" + error.message;
    }
  });
  sizeWrap.appendChild(sizeButton);
  sizeWrap.appendChild(sizeHint);
  sizeRow.appendChild(sizeWrap);
  group.appendChild(sizeRow);

  container.appendChild(group);
}


/* ==========================================================================
   飞书接入卡片（设置页）
   —— 凭据有无 / 渠道开关 / 当前策略 / 配对码与倒计时 / 「获取我的 id」引导 / 自检
   三条硬约定都守住：
   1) 不认识的键报错：这里只调 feishu.* 专用 RPC，绝不把飞书键塞进 settings.update
      （settings_io 会拒未知键；飞书凭据与渠道开关本就只进 .env，见 docs/03 §2 边界）；
   2) 密钥只写不回读：界面**只显示「已配置／未配置」**，任何密钥内容都不回显、不预填；
   3) 可清理的只有缓存：本卡片**不提供**任何清理入口。
   ========================================================================== */

/*: 配对码有效期秒数（与 state/pairing.py::CODE_TTL_SECONDS 一致，仅用于本地倒计时显示） */
var FEISHU_CODE_TTL = 3600;
/*: DM 授权策略的中文（与 rpc/feishu.py::DM_POLICIES 一一对应） */
var FEISHU_DM_OPTIONS = [
  ["pairing", "配对审批（推荐）"],
  ["all", "放行全部私聊"],
  ["allowlist", "仅白名单"]
];
/*: 连接方式的中文（后端现在**只有 websocket 长连接**一条路：webhook 路线已删，不需要公网地址/回调/隧道） */
var FEISHU_MODE_LABELS = {
  websocket: "websocket（长连接，不需要隧道）"
};
var FEISHU_GROUP_LABELS = { mention: "仅被 @ 时响应", disabled: "关闭群聊" };  // 群聊策略的中文

/*: 倒计时用的定时器与「这一枚刚生成的配对码」（明文只在此刻可见，刷新页面即丢） */
var feishuPairTimer = null;
var feishuPair = null;


function feishuClearTimer() {
  if (feishuPairTimer) {
    clearInterval(feishuPairTimer);
    feishuPairTimer = null;
  }
}


function feishuPad(n) {
  return (n < 10 ? "0" : "") + n;
}


function feishuCountdownText(left) {
  return (
    "去飞书私聊机器人，把这条码发给它（" +
    feishuPad(Math.floor(left / 60)) +
    ":" +
    feishuPad(left % 60) +
    " 后过期）"
  );
}


function feishuLine(label, value, kind) {
  var row = document.createElement("div");
  row.className = "feishu-line";
  var name = document.createElement("span");
  name.textContent = label;
  var val = document.createElement("span");
  val.className = "feishu-value" + (kind ? " is-" + kind : "");
  val.textContent = value;
  row.appendChild(name);
  row.appendChild(val);
  return row;
}


function feishuButton(parent, label, handler) {
  var button = document.createElement("button");
  button.type = "button";
  button.className = "ghost";
  button.textContent = label;
  button.addEventListener("click", function () {
    Promise.resolve(handler()).catch(function (error) {
      el("settings-note").textContent = "飞书操作失败：" + error.message;
    });
  });
  parent.appendChild(button);
  return button;
}


/* ==========================================================================
   渠道中心卡片（docs/21 §3 第 1/2/3/7 项 + §5.1 的 `channel.*` 接口）

   为什么单独一张卡而不是把飞书卡改大：`channel.*` 是**把 platform 提成参数**的统一命名空间
   （docs/21 §5.1），卡片形状与平台无关（§7 的三平台同构骨架）；飞书那张卡保留原样，
   老调用方与老测试一行都不用改。落在**现有设置浮层**里（`#settings-body`），不新增浮层。

   四条硬约定（与后端同一套，见 docs/21 §5.2/§6）：
   ① 凭据**只写不回读**：界面只显示「已配置 ｜ 尾 4 位」，输入框是 `type=password`
      且保存后**立即清空**；
   ② 没在跑时点「停止」显示「当前未运行」，**不假装成功**；
   ③ 引导清单的每一勾由后端 `channel.guide` 探测决定，探不到就如实显示未完成；
   ④ 自动建应用的链接由飞书服务端签发（`verification_uri_complete`），本项目**不伪造**；
      成功后的 App Secret 由后端直接落 `.env`，界面永远拿不到它。
   ========================================================================== */

/*: 渠道状态 → 状态点颜色（**只用本设计真实存在的 `--ui-*` 令牌**） */
var CHANNEL_DOT = {
  running: "--ui-green",
  starting: "--ui-yellow",
  error: "--ui-red",
  stopped: "--ui-text-tertiary"
};
/*: 渠道状态的中文（不认识的取值照实显示原值，不猜） */
var CHANNEL_STATE_LABELS = {
  stopped: "已停止",
  starting: "启动中",
  running: "运行中",
  error: "错误"
};
/*: 平台显示名（后端 `channel.status` 会回 `label`，这里只作兜底） */
var CHANNEL_LABELS = { feishu: "飞书" };
/*: 单次设备码轮询窗口（秒）。**必须小于桌面传输层 180s 的默认等待上限** ——
   超了会把「人还没点确认」误判成「调用失败」。 */
var CHANNEL_ONBOARD_WINDOW = 60;
/*: 短密钥的脱敏尾号占位（与后端 `rpc/channel.py::_tail` 同一口径，别在界面里补齐成 4 位） */
var CHANNEL_TAIL_HIDDEN = "****";

/*: 正在进行的「自动创建应用」：{device_code, interval, deadline}；为 null = 没在跑 */
var channelOnboard = null;
var channelOnboardTimer = null;


function channelClearOnboard() {
  if (channelOnboardTimer) {
    clearTimeout(channelOnboardTimer);
    channelOnboardTimer = null;
  }
  channelOnboard = null;
}


function channelDot(state) {
  var dot = document.createElement("span");
  dot.className = "channel-dot";
  dot.style.background = "var(" + (CHANNEL_DOT[state] || "--ui-text-tertiary") + ")";
  dot.title = CHANNEL_STATE_LABELS[state] || String(state || "");
  return dot;
}


function channelLine(label, value, kind) {
  var row = document.createElement("div");
  row.className = "channel-line";
  var name = document.createElement("span");
  name.textContent = label;
  var val = document.createElement("span");
  val.className = "channel-value" + (kind ? " is-" + kind : "");
  val.textContent = value;
  row.appendChild(name);
  row.appendChild(val);
  return row;
}


function channelButton(parent, label, handler) {
  var button = document.createElement("button");
  button.type = "button";
  button.className = "ghost";
  button.textContent = label;
  button.addEventListener("click", function () {
    Promise.resolve(handler()).catch(function (error) {
      el("settings-note").textContent = "渠道操作失败：" + error.message;
    });
  });
  parent.appendChild(button);
  return button;
}


function channelAgo(iso) {
  /* 把 ISO 时间戳翻成「多久以前」。读不到就说「还没收到过消息」，不编一个时间。 */
  if (!iso) return "还没收到过消息";
  var ts = Date.parse(iso);
  if (isNaN(ts)) return String(iso);
  var seconds = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (seconds < 60) return seconds + " 秒前";
  if (seconds < 3600) return Math.round(seconds / 60) + " 分钟前";
  if (seconds < 86400) return Math.round(seconds / 3600) + " 小时前";
  return Math.round(seconds / 86400) + " 天前";
}


function channelSecretText(credentials) {
  /* 凭据的**只读文案**：只有「已配置／未配置」与脱敏尾号，任何密钥值都不许进来。 */
  if (!credentials || !credentials.secret_set) return "未配置";
  var tail = String(credentials.tail || "");
  if (!tail || tail === CHANNEL_TAIL_HIDDEN) return "已配置";
  return "已配置 " + CHANNEL_TAIL_HIDDEN + tail;
}


async function renderChannelCard(body) {
  channelClearOnboard();
  var group = document.createElement("div");
  group.className = "set-group channel-card";
  var head = document.createElement("h4");
  head.textContent = "渠道（channel.*）";
  group.appendChild(head);
  group.appendChild(
    plainHint(
      "渠道跑在**独立子进程**里：关掉窗口也继续收消息，崩了不影响界面。" +
        "凭据只写 `.env`（原子写），这里**只显示「已配置／尾号」**，任何密钥值都不回显；" +
        "「停止」在没在跑时会明确告诉你「当前未运行」。",
    ),
  );

  var headRow = document.createElement("div");
  headRow.className = "channel-head";
  group.appendChild(headRow);

  var credBox = document.createElement("div");
  credBox.className = "channel-cred";
  group.appendChild(credBox);

  var connBox = document.createElement("div");
  connBox.className = "channel-conn";
  group.appendChild(connBox);

  var guideBox = document.createElement("div");
  guideBox.className = "channel-guide";
  group.appendChild(guideBox);

  var probeOut = document.createElement("p");
  probeOut.className = "hint channel-probe-out";

  var appIdInput = document.createElement("input");
  appIdInput.type = "password";
  appIdInput.placeholder = "App ID（cli_…，只写不回读）";
  appIdInput.autocomplete = "off";
  var secretInput = document.createElement("input");
  secretInput.type = "password";
  secretInput.placeholder = "App Secret（只写不回读）";
  secretInput.autocomplete = "off";

  var onboardBox = document.createElement("div");
  onboardBox.className = "channel-onboard";
  group.appendChild(onboardBox);
  var onboardOut = document.createElement("p");
  onboardOut.className = "hint channel-onboard-out";

  body.appendChild(group);

  async function saveCredentials() {
    var fields = {};
    var appId = String(appIdInput.value || "").trim();
    var secret = String(secretInput.value || "").trim();
    /* 空串在后端 = 清除，所以要保存就**只提交填了的字段**（否则会顺手清掉另一个）。 */
    if (appId) fields.app_id = appId;
    if (secret) fields.app_secret = secret;
    if (!Object.keys(fields).length) {
      el("settings-note").textContent = "没有要保存的内容（要清除凭据请点「清除凭据」）";
      return;
    }
    try {
      var result = await scout.call("channel.set_credentials", {
        platform: "feishu",
        fields: fields
      });
      appIdInput.value = "";
      secretInput.value = ""; // 保存后立即清空：绝不把密钥留在界面里
      el("settings-note").textContent =
        "已写入 .env：" +
        (result.written || []).join("、") +
        "（值不回读、不回显）" +
        (result.restart_required ? "；渠道正在跑，改完请点「停止」再「启动」" : "");
      await refresh();
    } catch (error) {
      el("settings-note").textContent = "保存失败：" + error.message;
    }
  }

  async function clearCredentials() {
    try {
      var result = await scout.call("channel.set_credentials", {
        platform: "feishu",
        fields: { app_id: "", app_secret: "" }
      });
      el("settings-note").textContent =
        "已清除 " + (result.cleared || []).join("、") + "（`.env` 里连键名一起删掉）";
      await refresh();
    } catch (error) {
      el("settings-note").textContent = "清除失败：" + error.message;
    }
  }

  async function startChannel() {
    try {
      var result = await scout.call("channel.start", { platform: "feishu" });
      el("settings-note").textContent =
        result.note === "already_running"
          ? "渠道已经在跑（pid " + result.pid + "）—— 不会起第二个实例"
          : "已起渠道子进程（pid " +
            result.pid +
            "）：关掉窗口它也会继续收消息。日志：" +
            result.log_path;
      await refresh();
    } catch (error) {
      el("settings-note").textContent = "启动失败：" + error.message;
    }
  }

  async function stopChannel() {
    try {
      var result = await scout.call("channel.stop", { platform: "feishu" });
      if (!result.ok) {
        /* 铁律：没在跑就说没在跑，不假装成功 */
        el("settings-note").textContent =
          result.reason === "not_running"
            ? "当前未运行（没有可停的渠道进程）"
            : "停止失败：" + (result.note || result.reason || "未知原因");
      } else {
        el("settings-note").textContent = "已停止渠道子进程（pid " + result.pid + "）";
      }
      await refresh();
    } catch (error) {
      el("settings-note").textContent = "停止失败：" + error.message;
    }
  }

  async function selfCheck() {
    probeOut.textContent = "正在联网自检（只探测，不发消息）…";
    try {
      var result = await scout.call("channel.test", { platform: "feishu", mode: "probe" });
      probeOut.textContent = result.ok
        ? "自检通过：机器人名 " +
          result.bot_name +
          "（耗时 " +
          result.latency_ms +
          " ms；token 值不回传）"
        : "自检未通过：" + (result.error || "未知原因");
    } catch (error) {
      probeOut.textContent = "自检失败：" + error.message;
    }
  }

  function paintStatus(status) {
    headRow.textContent = "";
    headRow.appendChild(channelDot(status.state));
    var name = document.createElement("strong");
    name.textContent = status.label || CHANNEL_LABELS[status.platform] || status.platform;
    headRow.appendChild(name);
    var stateText = document.createElement("span");
    stateText.className = "hint channel-state";
    stateText.textContent = CHANNEL_STATE_LABELS[status.state] || String(status.state || "");
    headRow.appendChild(stateText);
    var actions = document.createElement("div");
    actions.className = "set-seg channel-actions";
    channelButton(actions, "启动", startChannel);
    channelButton(actions, "停止", stopChannel);
    channelButton(actions, "自检", selfCheck);
    headRow.appendChild(actions);

    var cred = status.credentials || {};
    var ok = cred.app_id_set && cred.secret_set;
    credBox.textContent = "";
    credBox.appendChild(
      channelLine(
        "凭据",
        "App ID " +
          (cred.app_id_set ? "已配置" : "未配置") +
          " ｜ App Secret " +
          channelSecretText(cred),
        ok ? "ok" : "off",
      ),
    );
    var inputs = document.createElement("div");
    inputs.className = "channel-inputs";
    inputs.appendChild(appIdInput);
    inputs.appendChild(secretInput);
    var clearBtn = document.createElement("button");
    clearBtn.type = "button";
    clearBtn.className = "ghost";
    clearBtn.textContent = "清除凭据";
    clearBtn.addEventListener("click", clearCredentials);
    var credActions = document.createElement("div");
    credActions.className = "set-seg";
    channelButton(credActions, "保存凭据", saveCredentials);
    credActions.appendChild(clearBtn);
    credBox.appendChild(inputs);
    credBox.appendChild(credActions);

    connBox.textContent = "";
    connBox.appendChild(
      channelLine(
        "连接",
        status.connected
          ? "长连已建立"
          : status.state === "running"
            ? "进程在跑，但长连还没建立"
            : "未连接",
        status.connected ? "ok" : "off",
      ),
    );
    connBox.appendChild(channelLine("重连次数", String(status.reconnects || 0)));
    connBox.appendChild(
      channelLine(
        "最后一条消息",
        channelAgo(status.last_message_at) +
          (status.last_message_from ? "（来自 " + status.last_message_from + "）" : ""),
      ),
    );
    connBox.appendChild(
      channelLine(
        "最后一次错误",
        status.last_error ? String(status.last_error) : "无",
        status.last_error ? "bad" : "",
      ),
    );
    connBox.appendChild(
      channelLine(
        "渠道进程",
        status.pid ? "pid " + status.pid + "（日志 " + status.log_path + "）" : "未运行",
      ),
    );
    if (!status.sdk_ready) {
      connBox.appendChild(
        channelLine("长连接 SDK", status.sdk_detail || "未就绪", "bad"),
      );
    }
    connBox.appendChild(
      channelLine(
        "平台硬限",
        "重置 App Secret 没有 API（只能人工重置）；开机器人能力 / 勾事件 / 发布都要在后台点",
      ),
    );
    connBox.appendChild(probeOut);
  }

  function paintGuide(guide) {
    guideBox.textContent = "";
    guideBox.appendChild(
      plainHint("接入清单（每一步的完成状态由后端 channel.guide 探测，探不到就如实显示未完成）："),
    );
    (guide.steps || []).forEach(function (step) {
      var row = document.createElement("div");
      row.className = "channel-step";
      var mark = document.createElement("span");
      mark.className = "channel-step-mark";
      mark.textContent = step.done ? "✅" : "⬜";
      row.appendChild(mark);
      var text = document.createElement("div");
      text.className = "channel-step-text";
      var title = document.createElement("div");
      title.textContent = step.title;
      text.appendChild(title);
      var detail = document.createElement("div");
      detail.className = "hint";
      detail.textContent = step.detail;
      text.appendChild(detail);
      row.appendChild(text);
      if (step.deeplink) {
        var open = document.createElement("button");
        open.type = "button";
        open.className = "ghost channel-deeplink";
        open.textContent = "打开后台";
        open.addEventListener("click", function () {
          openExternalUrl(step.deeplink);
        });
        row.appendChild(open);
      }
      guideBox.appendChild(row);
    });
    if ((guide.blockers || []).length) {
      var list = document.createElement("ul");
      list.className = "list channel-blockers";
      guide.blockers.forEach(function (item) {
        var li = document.createElement("li");
        li.textContent = item;
        list.appendChild(li);
      });
      guideBox.appendChild(list);
    }
  }

  function paintOnboardIdle() {
    onboardBox.textContent = "";
    onboardBox.appendChild(
      plainHint(
        "也可以点下面这个让**飞书签发凭据**（设备码流程）：链接由飞书服务端生成，" +
          "你在飞书里点一下确认，拿到凭据后自动写进 `.env`（App Secret 只写不回读）。",
      ),
    );
    var actions = document.createElement("div");
    actions.className = "set-seg";
    channelButton(actions, "自动创建应用", beginOnboard);
    onboardBox.appendChild(actions);
    onboardBox.appendChild(onboardOut);
  }

  async function beginOnboard() {
    channelClearOnboard();
    onboardOut.textContent = "正在向飞书申请设备码…";
    var begin;
    try {
      begin = await scout.call("feishu.onboard.begin", { platform: "feishu" });
    } catch (error) {
      onboardOut.textContent = "发起自动创建失败：" + error.message + "（可以走上面的手工清单）";
      return;
    }
    onboardOut.textContent = "请在飞书里打开这条链接并点「确认」（链接由飞书签发，本项目不伪造）：";
    var link = document.createElement("a");
    link.className = "channel-link";
    link.href = "#";
    link.textContent = begin.qr_url;
    link.addEventListener("click", function (event) {
      event.preventDefault(); // 点链接不许把整个界面顶掉（走桥接的 webbrowser.open）
      openExternalUrl(begin.qr_url);
    });
    onboardOut.appendChild(link);
    var state = {
      device_code: begin.device_code,
      interval: Math.max(1, Number(begin.interval || 5)),
      deadline: Date.now() + Math.max(60, Number(begin.expire_in || 600)) * 1000
    };
    channelOnboard = state;
    channelOnboardTimer = setTimeout(pollOnboard, 0);
  }

  function scheduleOnboard(state) {
    channelOnboardTimer = setTimeout(pollOnboard, state.interval * 1000);
  }

  async function pollOnboard() {
    var state = channelOnboard;
    if (!state) return;
    var result;
    try {
      /* 一次调用只等一个短窗口（< 传输层 180s 上限），没完成就下一轮 —— 免得「人还没点」
         被当成「调用失败」。总时长由飞书给的 expire_in 兜住。 */
      result = await scout.call("feishu.onboard.poll", {
        platform: "feishu",
        device_code: state.device_code,
        interval: state.interval,
        expire_in: CHANNEL_ONBOARD_WINDOW
      });
    } catch (error) {
      channelClearOnboard();
      onboardOut.textContent = "轮询失败：" + error.message + "（可重试，或走手工清单）";
      return;
    }
    if (channelOnboard !== state) return; // 期间被取消/重开：丢掉这次结果
    if (result.ok) {
      channelClearOnboard();
      onboardOut.textContent =
        "已自动创建并写入 .env：app_id " +
        result.app_id +
        "（App Secret 只写不回读，界面只显示「已配置」）";
      el("settings-note").textContent = "自动创建完成：凭据已写进 .env，卡片已刷新";
      await refresh();
      return;
    }
    if (Date.now() >= state.deadline) {
      channelClearOnboard();
      onboardOut.textContent =
        "创建未在有效期内完成（" + (result.reason || "超时") + "）—— 可重试或走手工清单";
      return;
    }
    scheduleOnboard(state);
  }

  async function refresh() {
    var status;
    try {
      status = await scout.call("channel.status", { platform: "feishu" });
    } catch (error) {
      headRow.textContent = "读不到渠道状态：" + error.message;
      return;
    }
    paintStatus(status);
    var guide;
    try {
      guide = await scout.call("channel.guide", { platform: "feishu" });
    } catch (error) {
      guideBox.textContent = "读不到接入清单：" + error.message;
      return;
    }
    paintGuide(guide);
  }

  paintOnboardIdle();
  await refresh();
}


async function renderFeishuCard(body) {
  feishuClearTimer();
  feishuPair = null; // 明文码只在此刻可见：重开设置面板就不再显示旧的
  var group = document.createElement("div");
  group.className = "set-group";
  var head = document.createElement("h4");
  head.textContent = "飞书接入";
  group.appendChild(head);
  group.appendChild(
    plainHint(
      "凭据与渠道开关都写 .env（不写 config.yaml）。这里**只显示「已配置／未配置」**，" +
        "绝不回显任何密钥内容；可清理的永远只有缓存。",
    ),
  );

  var statusBox = document.createElement("div");
  statusBox.className = "feishu-status";
  group.appendChild(statusBox);

  var policyRow = document.createElement("div");
  policyRow.className = "set-row";
  group.appendChild(policyRow);

  var probeBox = document.createElement("div");
  probeBox.className = "feishu-probe";
  group.appendChild(probeBox);

  var pairBox = document.createElement("div");
  pairBox.className = "feishu-pairing";
  group.appendChild(pairBox);

  body.appendChild(group);

  function paintStatus(status) {
    statusBox.textContent = "";
    statusBox.appendChild(
      feishuLine("App ID", status.app_id_set ? "已配置" : "未配置", status.app_id_set ? "ok" : "off"),
    );
    statusBox.appendChild(
      feishuLine(
        "App Secret",
        status.secret_set ? "已配置" : "未配置",
        status.secret_set ? "ok" : "off",
      ),
    );
    statusBox.appendChild(
      feishuLine("连接方式", FEISHU_MODE_LABELS[status.connection_mode] || status.connection_mode),
    );
    statusBox.appendChild(
      feishuLine("群聊策略", FEISHU_GROUP_LABELS[status.group_policy] || status.group_policy),
    );
    statusBox.appendChild(
      feishuLine("临时放行", status.temp_open ? "开（还没批准任何人，渠道层先放行）" : "关"),
    );
  }

  function paintPolicy(status) {
    policyRow.textContent = "";
    var label = document.createElement("span");
    label.textContent = "私聊授权策略";
    policyRow.appendChild(label);
    var seg = document.createElement("div");
    seg.className = "set-seg";
    FEISHU_DM_OPTIONS.forEach(function (pair) {
      var button = document.createElement("button");
      button.type = "button";
      button.textContent = pair[1];
      button.classList.toggle("is-on", status.dm_policy === pair[0]);
      button.addEventListener("click", function () {
        scout
          .call("feishu.dm_policy", { policy: pair[0] })
          .then(function () {
            el("settings-note").textContent =
              "已切换私聊策略：" + pair[1] + "（写 .env，重启应用即生效）";
            return refresh();
          })
          .catch(function (error) {
            el("settings-note").textContent = "切换失败：" + error.message;
          });
      });
      seg.appendChild(button);
    });
    policyRow.appendChild(seg);
  }

  function paintPairing(listing) {
    feishuClearTimer();
    pairBox.textContent = "";
    pairBox.appendChild(
      plainHint(
        "配对码：待批 " +
          (listing.pending || []).length +
          " 枚｜白名单 " +
          (listing.approved || []).length +
          " 个｜临时放行 " +
          (listing.temp_open ? "开" : "关"),
      ),
    );

    var actions = document.createElement("div");
    actions.className = "set-seg";
    feishuButton(actions, (listing.pending || []).length ? "再生成一枚配对码" : "生成配对码", function () {
      return createCode();
    });
    feishuButton(actions, "刷新待批 / 白名单", function () {
      return refresh();
    });
    pairBox.appendChild(actions);

    var codeBox = document.createElement("div");
    codeBox.className = "feishu-code";
    pairBox.appendChild(codeBox);
    if (feishuPair) {
      // 刷新时若刚生成过码，把倒计时从存下的剩余秒数接着画（不丢）
      var code = document.createElement("div");
      code.className = "feishu-code-value";
      code.textContent = feishuPair.code;
      codeBox.appendChild(code);
      var tip = document.createElement("div");
      tip.className = "hint feishu-code-tip";
      codeBox.appendChild(tip);
      feishuStartCountdown(tip);
    }

    pairBox.appendChild(
      plainHint(
        "获取你的 open_id：把上面的配对码私聊发给机器人，机器人回执后你的 open_id 会自动落库，" +
          "点「刷新」即可在下面看到（白名单模式要的就是它；直接粘贴机器人整条回复也行）。",
      ),
    );

    var pending = listing.pending || [];
    if (pending.length) {
      var plist = document.createElement("ul");
      plist.className = "list feishu-pending";
      pending.forEach(function (row) {
        var li = document.createElement("li");
        li.textContent = row.claimed
          ? "待批：" + (row.user_name || row.user_id)
          : "待批：还没收到你的码（未绑定身份）";
        var meta = document.createElement("span");
        meta.className = "meta";
        var left = Math.max(0, FEISHU_CODE_TTL - Number(row.age_seconds || 0));
        meta.textContent = "剩余 " + Math.floor(left / 60) + " 分";
        li.appendChild(meta);
        plist.appendChild(li);
      });
      pairBox.appendChild(plist);
    }

    var approved = listing.approved || [];
    if (approved.length) {
      var alist = document.createElement("ul");
      alist.className = "list feishu-approved";
      approved.forEach(function (row) {
        var li = document.createElement("li");
        li.textContent = (row.user_name ? row.user_name + " " : "") + row.user_id;
        var revoke = document.createElement("button");
        revoke.type = "button";
        revoke.className = "ghost";
        revoke.textContent = "撤销";
        revoke.addEventListener("click", function () {
          scout
            .call("feishu.pairing.revoke", { user_id: row.user_id })
            .then(function () {
              return refresh();
            })
            .catch(function (error) {
              el("settings-note").textContent = "撤销失败：" + error.message;
            });
        });
        li.appendChild(revoke);
        alist.appendChild(li);
      });
      pairBox.appendChild(alist);
    }
  }

  function feishuStartCountdown(tip) {
    feishuClearTimer();
    var render = function () {
      tip.textContent =
        feishuPair.left > 0 ? feishuCountdownText(feishuPair.left) : "配对码已过期，请重新生成。";
    };
    render();
    feishuPairTimer = setInterval(function () {
      feishuPair.left -= 1;
      if (feishuPair.left <= 0) {
        feishuClearTimer();
        render();
        return;
      }
      render();
    }, 1000);
  }

  async function createCode() {
    var created;
    try {
      created = await scout.call("feishu.pairing.create", {});
    } catch (error) {
      el("settings-note").textContent = "生成配对码失败：" + error.message;
      return;
    }
    feishuPair = {
      code: String(created.code || ""),
      left: Math.max(0, Math.floor(created.ttl_seconds || FEISHU_CODE_TTL))
    };
    el("settings-note").textContent = "已生成配对码（明文只在此刻可见，库里只存 sha256(salt+code)）";
    await refresh();
  }

  async function runProbe() {
    probeOut.textContent = "正在联网探测…";
    var result;
    try {
      result = await scout.call("feishu.probe", {});
    } catch (error) {
      probeOut.textContent = "自检失败：" + error.message;
      return;
    }
    if (!result.ok) {
      probeOut.textContent = "机器人名 unknown —— " + (result.reason || "");
      return;
    }
    probeOut.textContent =
      "机器人名：" +
      result.bot_name +
      "｜bot open_id：" +
      (result.bot_open_id || "—") +
      "｜token 有效期约 " +
      Math.round(Number(result.token_expire || 0) / 60) +
      " 分钟（token 值不回传）";
  }

  // 自检（联网）：机器人名与 token 有效期 —— 失败只显示 unknown，不阻塞
  probeBox.appendChild(plainHint("自检：联网探测机器人名与 token 有效期；失败不影响接入，显示 unknown。"));
  var probeActions = document.createElement("div");
  probeActions.className = "set-seg";
  feishuButton(probeActions, "自检（联网）", function () {
    return runProbe();
  });
  probeBox.appendChild(probeActions);
  var probeOut = document.createElement("p");
  probeOut.className = "hint feishu-probe-out";
  probeBox.appendChild(probeOut);

  async function refresh() {
    var status;
    try {
      status = await scout.call("feishu.status", {});
    } catch (error) {
      statusBox.textContent = "读不到飞书状态：" + error.message;
      return;
    }
    var listing;
    try {
      listing = await scout.call("feishu.pairing.list", {});
    } catch (error) {
      listing = { pending: [], approved: [], temp_open: status.temp_open };
    }
    paintStatus(status);
    paintPolicy(status);
    paintPairing(listing);
  }

  await refresh();
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


/** 「关于」块：配置文件路径 + 真实版本 / 工具数 / 库路径（读 system.health）。
 *  读不到就如实说读不到 —— 不编一个版本号出来。 */
async function settingsAboutBlock(configPath) {
  var group = document.createElement("div");
  group.className = "set-group";
  var head = document.createElement("h4");
  head.textContent = "关于";
  group.appendChild(head);
  var rows = [["配置文件", configPath || "（后端没报路径）"]];
  try {
    var health = await scout.call("system.health", {});
    rows.push(["版本", String(health.version || "未知")]);
    rows.push(["工具", ((health.tools || []).length || 0) + " 个"]);
    rows.push(["数据库", String(health.db_path || "未知")]);
    rows.push(["模型密钥", health.llm_configured ? "已配置" : "未配置（只能搜，不能生成）"]);
  } catch (error) {
    rows.push(["版本", "读取失败：" + error.message]);
  }
  rows.forEach(function (pair) {
    var row = document.createElement("div");
    row.className = "set-row";
    var label = document.createElement("span");
    label.textContent = pair[0];
    var value = document.createElement("span");
    value.textContent = pair[1];
    row.appendChild(label);
    row.appendChild(value);
    group.appendChild(row);
  });
  return group;
}


async function openSettings() {
  var payload = await scout.call("settings.get", {});
  var values = payload.values || {};
  var secrets = payload.secrets_configured || {};
  var body = el("settings-body");
  body.textContent = "";

  // 两栏壳先搭好：左侧表项（5 项）+ 右侧面板，默认选中第一项。
  // 之后所有内容一次性 append 进各自面板 —— **切表项只切可见性**，不重建、不重渲。
  var shell = uiRail(SETTINGS_CATEGORIES);
  var panels = shell.panels;
  body.classList.add("ui-split");
  body.appendChild(shell.nav);
  body.appendChild(shell.panes);
  // 组标题 → 面板。没在声明里出现的组一律进「外观与关于」，绝不静默丢内容。
  var hostOf = {};
  SETTINGS_CATEGORIES.forEach(function (spec) {
    spec.sections.forEach(function (title) {
      hostOf[title] = panels[spec.name];
    });
  });

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
    (hostOf[group.title] || panels.appearance).appendChild(box);
  });

  /* 每日 token 上限旁边的「当前用量」：等字段真的进了 DOM 再读（元素不在就啥也不做）。
     用量来自 `chat.status.global_budget`，**不在这里自己算** —— 界面算一份、后端算一份，
     两边迟早会给出不同的数。 */
  await renderBudgetUsage();

  renderAppearance(panels.appearance);

  // 渠道（独立分类）：飞书接入卡片（凭据有无 / 渠道开关 / 当前策略 / 配对码与倒计时 /
  // 「获取我的 id」引导 / 自检）+ 渠道中心卡片（channel.* 统一命名空间）
  await renderFeishuCard(panels.channel);

  await renderChannelCard(panels.channel);

  // MinerU：开关开着但没配 token —— 现在**不是错误**（扫描版走免 Token 的预签名直传也能用），
  // 但要说清能力边界：免 Token 的 Agent 通道限 ≤10MB / ≤20 页，更大或要更高精度才需要 Token。
  if (values["features.enable_mineru"] !== false && !secrets["mineru.api_token"]) {
    var warn = document.createElement("p");
    warn.className = "hint";
    warn.textContent =
      "扫描版走免 Token 通道（官方 Agent 轻量接口，实测可用）：限单文件 ≤10MB / ≤20 页，" +
      "直传本地字节、不需要公网 URL。超过这个规模或要更高精度（表格/公式/多格式导出），" +
      "再填 MinerU Token 换精准接口（≤200MB / ≤200 页）。解析失败会如实记成 mineru_failed，不会假装成功。";
    panels.data.appendChild(warn);
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
  panels.data.appendChild(dataGroup);

  // 「外观与关于」里的关于块：配置文件路径 + 真实版本/工具数（读 system.health；读不到就说读不到）
  panels.appearance.appendChild(await settingsAboutBlock(payload.config_path));

  el("settings-path").textContent = "配置文件：" + (payload.config_path || "");
  el("settings-note").textContent = "";
  renderUsage(dataGroup, await scout.call("settings.usage", {}));
  body.scrollTop = 0; // 每次打开都从「模型」那一栏开始看，不留在上次滚到的位置
  shell.panes.scrollTop = 0;
  shell.select(SETTINGS_CATEGORIES[0].name); // 左侧表项回到第一项（打开始终落在「模型与生成」）
  el("settings").hidden = false;
}


function closeSettings() {
  el("settings").hidden = true;
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
      if (field.kind === "number" && field.nullable) {
        /* 可空数字（目前只有每日 token 上限）：留空或填 0 → **显式传 null**，不是跳过。
           跳过等于"保留原来的上限"，而用户清空输入框的意思恰恰是"把上限去掉"；
           0 也算不限，与 `runtime/budget.py::_positive_int` 同一口径（两处给出不同解释最糟）。 */
        var unlimited = !text || Number(text) === 0;
        setDeep(updates, field.key.split("."), unlimited ? null : Number(text));
        return;
      }
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
    // 输入框里显示的模型/深度思考跟着刷新（改的就是这两个键，不刷新会显示旧值）
    await refreshComposerMeta();
  } catch (error) {
    note.textContent = "保存失败：" + error.message;
    note.className = "hint set-bad";
  }
}

/* ---------------------------------------------------------- 会话行菜单 */
