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
  body.appendChild(await profileBoard());
  el("profile-note").textContent = "汇总跨全部会话";
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
/*: 连接模式 / 群聊策略的中文（后端给的是枚举，界面别直接甩英文） */
var FEISHU_MODE_LABELS = {
  webhook: "webhook（事件回调）",
  websocket: "websocket（本项目尚未实现，仅记录）"
};
var FEISHU_GROUP_LABELS = { mention: "仅被 @ 时响应", disabled: "关闭群聊" };

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
      feishuLine(
        "渠道层监听",
        (status.listener_enabled ? "开" : "关") + "（端口 " + status.listener_port + "）",
      ),
    );
    statusBox.appendChild(
      feishuLine("连接模式", FEISHU_MODE_LABELS[status.connection_mode] || status.connection_mode),
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

  // 飞书接入卡片（凭据有无 / 渠道开关 / 当前策略 / 配对码与倒计时 / 「获取我的 id」引导 / 自检）
  await renderFeishuCard(body);

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
