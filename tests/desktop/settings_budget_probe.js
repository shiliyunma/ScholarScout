/* 设置面板里两个**可编辑项**的行为探针：`saveSettings` 的"留空/0 → null"与 `renderBudgetUsage`。
 *
 * 用法：node settings_budget_probe.js <app.js> <profile.js>
 * 输出：一行 JSON —— {checks:[{name, ok, got}], updates:[...]}（任一条 ok=false 时退出码 1）
 *
 * 为什么必须有它（静态读文本查不出来）：
 * - "留空"在 `saveSettings` 里原本是 `if (!text) return;` —— 跳过。用户清空输入框想删掉上限，
 *   结果只是"没改"，面板关掉再打开还是旧上限。文本断言最多证明代码里有 `null` 这个词。
 * - "填 0"必须**真的写 null**（跟 `runtime/budget.py::_positive_int` 同一口径），
 *   而不是把 0 当上限发出去 —— 那会让每一轮都被拒（stop_reason=budget），用户以为程序坏了。
 * - 断言的是**桩桥收到的入参**（`settings.update` 的 updates），不是界面文字：
 *   界面显示"已保存"完全可以在一次请求都没发出去时成立。
 *
 * 做法：`profile.js` 顶层只有 var 与函数声明（没有可执行语句），所以用 `vm` 加载后直接取函数；
 * `SETTINGS_SPEC` 从 app.js 里**切出真实的那一段**（不在这里抄一份 —— 抄一份就会漂），
 * DOM 用极简桩，桥用 `scout.call` 桩并记录每次调用。
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const appPath =
  process.argv[2] ||
  path.join(__dirname, "..", "..", "src", "scholarscout", "desktop", "ui", "app.js");
const profilePath =
  process.argv[3] ||
  path.join(__dirname, "..", "..", "src", "scholarscout", "desktop", "ui", "profile.js");

const APP = fs.readFileSync(path.resolve(appPath), "utf8");
const PROFILE = fs.readFileSync(path.resolve(profilePath), "utf8");

/* ------------------------------------------------------------ SETTINGS_SPEC（真源头） */
const specStart = APP.indexOf("var SETTINGS_SPEC = [");
if (specStart < 0) {
  console.error("app.js 里找不到 `var SETTINGS_SPEC = [` —— 锚点变了，探针拒绝猜");
  process.exit(2);
}
const specEnd = APP.indexOf("\n];", specStart);
if (specEnd < 0) {
  console.error("SETTINGS_SPEC 没有结束标记 `\\n];`");
  process.exit(2);
}
const SPEC_TEXT = APP.slice(specStart, specEnd + 3);

/* ---------------------------------------------------------------------- 极简 DOM 桩 */
const registry = {}; // id -> node（`el(id)` 就查它）

function makeEl(tag) {
  const node = {
    tag,
    className: "",
    style: {},
    type: "",
    title: "",
    hidden: false,
    value: "",
    checked: false,
    placeholder: "",
    step: "",
    disabled: false,
    _text: "",
    children: [],
    parents: [],
    _attrs: {},
    _handlers: {},
    classList: {
      _on: {},
      toggle(cls, on) {
        this._on[cls] = on === undefined ? !this._on[cls] : !!on;
      },
      add(cls) {
        this._on[cls] = true;
      },
      remove(cls) {
        this._on[cls] = false;
      },
      contains(cls) {
        return !!this._on[cls];
      },
    },
    appendChild(child) {
      this.children.push(child);
      if (child) child.parents.push(this);
      return child;
    },
    removeChild(child) {
      return child;
    },
    setAttribute(name, value) {
      this._attrs[name] = String(value);
    },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null;
    },
    addEventListener(name, fn) {
      (this._handlers[name] = this._handlers[name] || []).push(fn);
    },
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    focus() {},
    click() {
      (this._handlers.click || []).forEach((fn) => fn({}));
    },
  };
  Object.defineProperty(node, "textContent", {
    get() {
      return this._text;
    },
    set(v) {
      this._text = v === undefined || v === null ? "" : String(v);
    },
  });
  Object.defineProperty(node, "id", {
    get() {
      return this._id || "";
    },
    set(v) {
      this._id = String(v);
      if (this._id) registry[this._id] = this;
    },
  });
  return node;
}

const document = {
  createElement: (tag) => makeEl(tag),
  createTextNode: (text) => ({ textContent: String(text) }),
  querySelector: () => null,
  querySelectorAll: () => [],
  body: makeEl("body"),
};

/* ------------------------------------------------------------------------ 桩桥 */
const calls = []; // [{method, params}]
const VALUES = {
  "llm.chat.model": "deepseek-chat",
  "runtime.daily_token_budget": 200000, // 打开时**已有上限** —— 清空才有"删掉"可言
  "runtime.notify_turn_done": false,
};
const STATUS = {
  global_budget: {
    enabled: true,
    window_hours: 24,
    tokens: { used: 123456, limit: 200000, remaining: 76544, prompt: 100000, completion: 23456 },
    cost: { used: null, limit: null, remaining: null, priced_calls: 0 },
    exceeded: false,
    reason: null,
  },
};
const scout = {
  call: async function (method, params) {
    calls.push({ method: method, params: params === undefined ? null : params });
    if (method === "settings.get") {
      return { values: Object.assign({}, VALUES), secrets_configured: {}, config_path: "config.yaml" };
    }
    if (method === "settings.update") {
      const updates = (params && params.updates) || {};
      Object.keys(updates).forEach(function (key) {
        if (updates[key] === null) delete VALUES[key];
        else VALUES[key] = updates[key];
      });
      return { notes: [] };
    }
    if (method === "chat.status") return STATUS;
    if (method === "settings.usage") return { items: [] };
    return {};
  },
};

const sandbox = {
  console,
  JSON,
  Promise,
  Object,
  Array,
  Number,
  String,
  Math,
  Date,
  setTimeout,
  clearTimeout,
  document,
  el: (id) => registry[id] || null,
  scout,
  state: { layout: { left: 280, right: 380 } }, // 布局字段与本次无关，给个可写的壳就行
  fmtTokens: (v) => String(v), // 格式化有它自己的用例；这里只看接线
  applyUiScale: () => {},
  applyLayout: () => {},
  refreshComposerMeta: async () => {},
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
const ctx = vm.createContext(sandbox);

vm.runInContext(SPEC_TEXT, ctx, { filename: "SETTINGS_SPEC(app.js)" });
vm.runInContext(PROFILE, ctx, { filename: "profile.js" });

/* `saveSettings` 会往 #settings-note 写结果文案 —— 桩里先放一个 */
const noteEl = makeEl("p");
noteEl.id = "settings-note";

/* ---------------------------------------------------------------- 断言小工具 */
const checks = [];
function check(name, ok, got) {
  checks.push({ name: name, ok: !!ok, got: got });
}
function specOf(key) {
  const groups = ctx.SETTINGS_SPEC || [];
  for (const group of groups) {
    for (const field of group.fields || []) {
      if (field.key === key) return field;
    }
  }
  return null;
}

/* 渲染两行（真 `renderSettingsField`），再让 `saveSettings` 去读它们 */
async function renderRow(key, values) {
  const field = specOf(key);
  if (!field) throw new Error("SETTINGS_SPEC 里没有 " + key);
  ctx.renderSettingsField(field, values, {});
  return field;
}

function lastUpdate() {
  for (let i = calls.length - 1; i >= 0; i -= 1) {
    if (calls[i].method === "settings.update") return calls[i].params;
  }
  return null;
}

function countUpdates() {
  return calls.filter((c) => c.method === "settings.update").length;
}

/* ---------------------------------------------------------------- 1. 字段契约 */
check("面板上有 runtime.daily_token_budget 且标了 nullable", !!specOf("runtime.daily_token_budget") && specOf("runtime.daily_token_budget").nullable === true,
  specOf("runtime.daily_token_budget"));
check("面板上有 runtime.notify_turn_done 且是布尔开关",
  !!specOf("runtime.notify_turn_done") && specOf("runtime.notify_turn_done").kind === "bool",
  specOf("runtime.notify_turn_done"));

/* ---------------------------------------------------------------- 2. 回填 */
(async function main() {
  await renderRow("runtime.daily_token_budget", VALUES);
  await renderRow("runtime.notify_turn_done", VALUES);
  const budgetInput = registry["set-runtime-daily_token_budget"];
  const notifyInput = registry["set-runtime-notify_turn_done"];
  check("数字框回填了当前上限 200000", budgetInput && budgetInput.value === "200000",
    budgetInput && budgetInput.value);
  check("数字框是 type=number 且占位符写了「留空 = 不限」",
    budgetInput && budgetInput.type === "number" && /留空/.test(budgetInput.placeholder || ""),
    budgetInput && { type: budgetInput.type, placeholder: budgetInput.placeholder });
  check("提醒开关按 false 显示未勾选（缺失/未配置都不许显示成开）",
    notifyInput && notifyInput.checked === false, notifyInput && notifyInput.checked);

  /* ------------------------------------------------------------ 3. 留空 → null */
  const before = countUpdates();
  budgetInput.value = ""; // 用户清空
  sandbox.openSettings = async () => {}; // 保存后会重画面板，这里只关心入参
  await ctx.saveSettings();
  let sent = lastUpdate();
  check("留空保存真的发了一次 settings.update", countUpdates() === before + 1,
    { before: before, after: countUpdates() });
  check("留空保存 → 入参含 runtime.daily_token_budget: null（不是跳过）",
    sent && sent.updates.runtime && Object.prototype.hasOwnProperty.call(sent.updates.runtime, "daily_token_budget") &&
      sent.updates.runtime.daily_token_budget === null,
    sent && sent.updates);

  /* ------------------------------------------------------------ 4. 填 0 → null */
  await renderRow("runtime.daily_token_budget", VALUES);
  registry["set-runtime-daily_token_budget"].value = "0";
  await ctx.saveSettings();
  sent = lastUpdate();
  check("填 0 → 入参仍是 null（0 不等于「上限 0，一律拒绝」）",
    sent && sent.updates.runtime.daily_token_budget === null, sent && sent.updates);

  /* ------------------------------------------------------------ 5. 填数字 → int */
  await renderRow("runtime.daily_token_budget", VALUES);
  registry["set-runtime-daily_token_budget"].value = "500000";
  await ctx.saveSettings();
  sent = lastUpdate();
  const got = sent && sent.updates.runtime.daily_token_budget;
  check("填 500000 → 入参是数字 500000（不是字符串）",
    typeof got === "number" && got === 500000, { got: got, type: typeof got });

  /* ------------------------------------------------------------ 6. 提醒开关 */
  await renderRow("runtime.notify_turn_done", { "runtime.notify_turn_done": false });
  registry["set-runtime-notify_turn_done"].checked = true; // 拨开
  await ctx.saveSettings();
  sent = lastUpdate();
  check("拨开提醒 → 入参 runtime.notify_turn_done: true",
    sent && sent.updates.runtime.notify_turn_done === true, sent && sent.updates);
  await renderRow("runtime.notify_turn_done", { "runtime.notify_turn_done": true });
  registry["set-runtime-notify_turn_done"].checked = false; // 拨回去
  await ctx.saveSettings();
  sent = lastUpdate();
  check("拨回去 → 入参 false（关不掉就是坏开关）",
    sent && sent.updates.runtime.notify_turn_done === false, sent && sent.updates);

  /* ------------------------------------------------------------ 7. 用量接线 */
  const usageEl = makeEl("span");
  usageEl.id = "set-runtime-daily_token_budget-usage";
  await ctx.renderBudgetUsage();
  let text = usageEl.textContent;
  check("用量行显示已用/上限（来自 chat.status.global_budget）",
    /已用/.test(text) && /123456/.test(text) && /200000/.test(text), text);
  check("用量行没在未超限时说「超限」", !/超限/.test(text), text);

  STATUS.global_budget.exceeded = true;
  STATUS.global_budget.reason = "daily_token_budget";
  await ctx.renderBudgetUsage();
  text = usageEl.textContent;
  check("超限时如实写「已超限」", /已超限/.test(text), text);

  STATUS.global_budget = {
    enabled: false,
    window_hours: 24,
    tokens: { used: null, limit: null, remaining: null },
    cost: { used: null, limit: null, remaining: null, priced_calls: 0 },
    exceeded: false,
    reason: null,
  };
  await ctx.renderBudgetUsage();
  text = usageEl.textContent;
  check("未设上限时写「不限量」而不是假报 0", /不限量/.test(text) && !/已用 0/.test(text), text);

  /* ------------------------------------------------------------ 输出 */
  const bad = checks.filter((c) => !c.ok);
  process.stdout.write(
    JSON.stringify({
      checks: checks,
      failed: bad.length,
      updates: calls.filter((c) => c.method === "settings.update").map((c) => c.params),
      methods: calls.map((c) => c.method),
    }) + "\n"
  );
  if (bad.length) process.exitCode = 1;
})().catch(function (error) {
  process.stderr.write("探针跑挂：" + (error && error.stack ? error.stack : error) + "\n");
  process.exit(3);
});
