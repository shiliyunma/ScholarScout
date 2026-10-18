/* 搜索意图选择卡（`chat.needs_choice`）的**纯函数 + DOM 级**用例集。
 *
 * 用法：node choice_probe.js <app.js 路径>
 * 输出：一行 JSON —— [{name, ok, got}]，由 Python 侧断言（见 test_choice_card.py）。
 *
 * 为什么要抽函数源码：app.js 是浏览器脚本（顶层用 window / document / state），require 进来
 * 会当场炸。这里按名字把产品文件里的那段源码切出来，放进 `new Function` 里求值 —— 测的是
 * **产品文件里的源码**，不是复刻版。
 *
 * DOM 用一个**最小 shim**（createElement / appendChild / setAttribute / classList /
 * querySelectorAll('.cls') / click），只实现产品代码真用到的那几个 API。它不是浏览器，
 * 所以"真渲染 + 相邻栏位几何 + 截图"仍由 `run/ui_choice_probe.py`（无头 Edge）量 ——
 * 这里钉的是"事件→卡片、点击→发消息→禁用、重复事件去重"这几条**行为**不会被改坏。
 */
"use strict";

const fs = require("fs");
const path = require("path");

const appPath = path.resolve(
  process.argv[2] ||
    path.join(__dirname, "..", "..", "src", "scholarscout", "desktop", "ui", "app.js"),
);
const APP = fs.readFileSync(appPath, "utf8");

/* ------------------------------------------------------------ 源码抽取 */

/** 按名字切出一个顶层函数声明（花括号配平）。切不到就抛 —— 宁可红，也不要静默测了个空。 */
function extractFunction(src, name) {
  let start = src.indexOf("function " + name + "(");
  if (start < 0) throw new Error("源码里找不到 function " + name + "(");
  // `async function foo(` 也是顶层函数：不把 async 一起切出来，求值时会因为函数体里有
  // await 直接 SyntaxError（answerChoice 就是 async）。
  if (src.slice(Math.max(0, start - 6), start) === "async ") start -= 6;
  const open = src.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < src.length; i += 1) {
    const ch = src[i];
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(name + " 的花括号没配平");
}

/** 切出 `var NAME = {...};` 这样的顶层字面量（CHOICE_PHRASES）。 */
function extractVar(src, name) {
  const start = src.indexOf("var " + name + " = {");
  if (start < 0) throw new Error("源码里找不到 var " + name + " = {");
  const open = src.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < src.length; i += 1) {
    const ch = src[i];
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(name + " 的花括号没配平");
}

/** 依赖包：函数之间互相引用，所以先建一个可变的袋子，加载后往里填。 */
const bag = {};

function load(src, name, extraDeps) {
  const deps = Object.assign({}, bag, extraDeps || {});
  const keys = Object.keys(deps);
  const body = extractFunction(src, name);
  const fn = new Function(...keys, "return (" + body + ");")(...keys.map((k) => deps[k]));
  bag[name] = fn;
  return fn;
}

/* -------------------------------------------------------------- DOM shim */

function hasClass(node, name) {
  return String(node.className || "")
    .split(/\s+/)
    .filter(Boolean)
    .indexOf(name) >= 0;
}

function makeEl(tag) {
  const node = {
    tagName: tag,
    children: [],
    parentNode: null,
    attrs: {},
    listeners: {},
    className: "",
    textContent: "",
    disabled: false,
    hidden: false,
    title: "",
    type: "",
    appendChild(child) {
      if (child.parentNode) child.parentNode.removeChild(child);
      child.parentNode = node;
      node.children.push(child);
      return child;
    },
    removeChild(child) {
      node.children = node.children.filter((c) => c !== child);
      child.parentNode = null;
      return child;
    },
    remove() {
      if (node.parentNode) node.parentNode.removeChild(node);
    },
    setAttribute(key, value) {
      node.attrs[key] = String(value);
      if (key === "class") node.className = String(value);
    },
    getAttribute(key) {
      return Object.prototype.hasOwnProperty.call(node.attrs, key) ? node.attrs[key] : null;
    },
    addEventListener(type, fn) {
      (node.listeners[type] = node.listeners[type] || []).push(fn);
    },
    click() {
      (node.listeners.click || []).forEach((fn) => fn({ target: node, preventDefault() {} }));
    },
    querySelectorAll(selector) {
      const cls = String(selector || "");
      if (!cls.startsWith(".") || cls.indexOf(".") !== cls.lastIndexOf(".")) {
        throw new Error("shim 只支持单类选择器：" + selector);
      }
      const want = cls.slice(1);
      const out = [];
      (function walk(el) {
        el.children.forEach((child) => {
          if (hasClass(child, want)) out.push(child);
          walk(child);
        });
      })(node);
      return out;
    },
    querySelector(selector) {
      const found = node.querySelectorAll(selector);
      return found.length ? found[0] : null;
    },
  };
  node.classList = {
    add(name) {
      if (!hasClass(node, name)) node.className = (String(node.className) + " " + name).trim();
    },
    contains(name) {
      return hasClass(node, name);
    },
    toggle(name, on) {
      if (on) node.classList.add(name);
      else node.className = String(node.className).split(/\s+/).filter((c) => c && c !== name).join(" ");
    },
  };
  return node;
}

const document = { createElement: (tag) => makeEl(tag) };

/* --------------------------------------------------- 产品的状态与桩依赖 */

/*: 会话流表：`streamBox(session_id)` 返回它（每会话一条，与产品同构）。 */
const streams = {};
function streamBox(sessionId) {
  const key = String(sessionId || "");
  if (!streams[key]) streams[key] = makeEl("div");
  return streams[key];
}

const state = { sessionId: "s1", choices: {}, streams: {} };
let sent = [];
let notices = [];
let opened = [];
let running = false;
/*: 桩命中的**顺序**日志。为什么除了 sent/opened 还要它：跨会话那条用例要证明
    “先 openSession(s2) 再 send”这个次序，而不是两个调用各自发生过一次就算过。 */
let order = [];

function resetSpies() {
  sent.length = 0;
  notices.length = 0;
  opened.length = 0;
  order.length = 0;
}

const deps = {
  document,
  state,
  streamBox,
  choiceCards: null,
  el: () => makeEl("div"),
  sessionRunning: () => running,
  notice: (text, sessionId) => notices.push({ text, sessionId }),
  send: async (text) => {
    sent.push(text);
    order.push("send:" + text);
  },
  openSession: async (sessionId) => {
    opened.push(sessionId);
    order.push("open:" + String(sessionId));
  },
  Promise,
};

bag.state = state;
bag.streamBox = streamBox;
bag.document = document;
bag.el = deps.el;
bag.sessionRunning = deps.sessionRunning;
bag.notice = deps.notice;
bag.send = deps.send;
bag.openSession = deps.openSession;
bag.CHOICE_PHRASES = new Function(extractVar(APP, "CHOICE_PHRASES") + "\nreturn CHOICE_PHRASES;")();

load(APP, "choiceMessage");
load(APP, "choiceCardKey");
load(APP, "choiceCards");
load(APP, "choiceTitleText");
/* 顺序不能反：renderChoiceCard 的闭包里直接调 answerChoice（点击回调），
   先 load answerChoice 才拿得到真函数 —— 反了会把 undefined 捕获进去，点击时才炸。 */
load(APP, "answerChoice");
load(APP, "renderChoiceCard");

/* --------------------------------------------------------------- 用例集 */

/*: 与 `runtime/search_intent.options()` **一字对应**的 7 条（后端是唯一来源，
    前端只照载荷渲染；这里照抄是为了钉住"7 条 + 含 none"这个契约）。 */
const OPTIONS = [
  { type: "paper", label: "论文", hint: "arXiv / OpenAlex 的预印本与期刊" },
  { type: "code", label: "代码", hint: "GitHub 仓库与可复现实现" },
  { type: "tutorial", label: "教程", hint: "入门指南 / 官方文档 / 视频课" },
  { type: "news", label: "新闻", hint: "发布与更新动态" },
  { type: "data", label: "数据", hint: "数据集 / benchmark / 榜单" },
  { type: "other", label: "都看看", hint: "不筛，全部来源都查" },
  { type: "none", label: "先不检索", hint: "直接用已有资料回答" },
];

function payload(over) {
  return Object.assign(
    { session_id: "s1", turn_id: "trn_1", query: "帮我找点教育智能体的资料", options: OPTIONS, intent: null },
    over || {},
  );
}

const cases = [];
function check(name, fn) {
  try {
    const detail = fn();
    const ok = detail === true || detail === undefined;
    cases.push({ name: name, ok: ok, got: ok ? "" : String(detail) });
  } catch (error) {
    cases.push({ name: name, ok: false, got: error && error.message ? error.message : String(error) });
  }
}
function expect(actual, expected, label) {
  const a = JSON.stringify(actual);
  const b = JSON.stringify(expected);
  return a === b ? true : `${label || "值"}：期望 ${b}，实际 ${a}`;
}

/* ---------------------------------------------- ① 选项 → 用户消息正文（纯函数） */

check("choiceMessage：7 个选项各有确定正文，且都能被后端显式规则认出", () => {
  const got = OPTIONS.map((opt) => bag.choiceMessage(opt));
  return expect(
    got,
    [
      "只看论文",
      "只看代码",
      "只看教程",
      "只看新闻",
      "只看数据",
      "都可以，全部来源都查",
      "先不检索，直接回答",
    ],
    "正文",
  );
});

check("choiceMessage：none 明确含「不搜索」语义（先不检索 + 直接回答）", () => {
  const text = bag.choiceMessage({ type: "none", label: "先不检索" });
  return /先不检索/.test(text) && /直接回答/.test(text) ? true : "none 的正文丢了不检索语义：" + text;
});

check("choiceMessage：未知类型退化成 label，不发空串", () => {
  return expect(bag.choiceMessage({ type: "video", label: "视频" }), "只看视频", "正文");
});

check("choiceMessage：没有类型也没有 label → 空串（调用方据此不发）", () => {
  return expect([bag.choiceMessage({}), bag.choiceMessage(null)], ["", ""], "正文");
});

/* ------------------------------------------------------------- ② 去重键 */

check("choiceCardKey：同一轮同一 query → 同一个键（重复事件据此不重插）", () => {
  return expect(
    bag.choiceCardKey({ turn_id: "t1", query: "q1" }) === bag.choiceCardKey({ turn_id: "t1", query: "q1" }),
    true,
    "同键",
  );
});

check("choiceCardKey：同一轮不同 query → 不同键（一轮问两件事各得一张卡）", () => {
  return expect(
    bag.choiceCardKey({ turn_id: "t1", query: "q1" }) !== bag.choiceCardKey({ turn_id: "t1", query: "q2" }),
    true,
    "不同键",
  );
});

check("choiceCardKey：缺字段也不炸（退化成空串）", () => {
  return expect([bag.choiceCardKey({}), bag.choiceCardKey(null)], ["|", "|"], "键");
});

/* ------------------------------------- ③ 事件 → 卡片（DOM 级，7 个选项齐） */

check("renderChoiceCard：落进该会话的流，7 个选项按钮齐、label 照载荷", () => {
  const card = bag.renderChoiceCard("s1", payload());
  if (!card) return "没有建出卡片";
  if (streams.s1.children.indexOf(card) < 0) return "卡片没落进 s1 的流";
  const buttons = card.querySelectorAll(".choice-option");
  if (buttons.length !== 7) return "选项数不对：" + buttons.length;
  const labels = buttons.map((b) => b.querySelectorAll(".choice-option-label")[0].textContent);
  return expect(labels, OPTIONS.map((o) => o.label), "选项 label");
});

check("renderChoiceCard：标题里带上查询，且没有选项时不出卡（不猜）", () => {
  const title = bag.renderChoiceCard("s1", payload({ turn_id: "t2", query: "再找点资料" }));
  const text = title.querySelectorAll(".choice-title")[0].textContent;
  const empty = bag.renderChoiceCard("s1", payload({ turn_id: "t3", options: [] }));
  if (text.indexOf("再找点资料") < 0) return "标题里没有查询：" + text;
  return expect(empty, null, "空选项");
});

check("renderChoiceCard：**同一轮同一 query 的重复事件不重插**", () => {
  const before = streams.s1.querySelectorAll(".msg-choice").length;
  bag.renderChoiceCard("s1", payload());
  bag.renderChoiceCard("s1", payload());
  const after = streams.s1.querySelectorAll(".msg-choice").length;
  return expect([before, after], [before, before], "卡片数（重复前后应相同）");
});

check("renderChoiceCard：**非当前会话的卡不落到当前会话的流里**", () => {
  state.sessionId = "s1";
  const beforeS1 = streams.s1.querySelectorAll(".msg-choice").length;
  const card = bag.renderChoiceCard("s2", payload({ session_id: "s2", turn_id: "trn_b1" }));
  if (!card) return "s2 的卡没建出来";
  if (streams.s2.children.indexOf(card) < 0) return "卡没落进 s2 自己的流";
  return expect(streams.s1.querySelectorAll(".msg-choice").length, beforeS1, "s1 的卡片数（不该变）");
});

check("renderChoiceCard：没有 session_id → 不落任何人的流", () => {
  const total = streams.s1.querySelectorAll(".msg-choice").length + streams.s2.querySelectorAll(".msg-choice").length;
  const card = bag.renderChoiceCard(null, payload({ session_id: null }));
  const after = streams.s1.querySelectorAll(".msg-choice").length + streams.s2.querySelectorAll(".msg-choice").length;
  return expect([card, after], [null, total], "空会话");
});

/* ---------------------------------------------- ④ 点击 → 发消息 → 禁用（只能生效一次） */

/* ------------------------------------------------------------------------
   ④ 点击 → 发消息 → 禁用（只能生效一次）

   **这四条必须串行跑，不能 `Promise.all`。** 它们共用 `sent` / `notices` / `running` /
   `state.sessionId` 这组可观测面；每条用例开头清空 spy 时，会把**别的用例刚 push 进去的
   那一条**一起清掉。上一版就是并发跑的，于是同一份产品代码同时给出一个假红
   （"应发出 1 条，实际 []"）和一个假绿（"再点没发"其实是从没发过）—— 现场的
   `order` 日志显示 send 确实被调到，只是被下一条用例的 `sent.length = 0` 抹掉了。
   串行之后每条用例的现场只属于自己。

   桩怎么生效（**不改产品签名**）：`load()` 把 `send` / `openSession` 作为 `new Function`
   的**形参**注入进去 —— 产品函数体里出现的 `send(...)` 在求值时解析到的就是这个形参。
   这一点不是靠读代码自证，而是由下面「桩的调用序列」断言反过来钉住的。
   ---------------------------------------------------------------------- */

const asyncCases = [];
function checkAsync(name, fn) {
  asyncCases.push({ name: name, fn: fn });
}

/** 等桩里的微任务落地：answerChoice 是 async，点击那一拍之后还要跑几个 await。 */
function settle() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

checkAsync("点「只看论文」→ **桩 send 真被调到 1 次**、内容就是「只看论文」，整卡禁用", async () => {
  resetSpies();
  running = false;
  state.sessionId = "s1";
  const card = bag.renderChoiceCard("s1", payload({ turn_id: "trn_click", query: "点选测试" }));
  const buttons = card.querySelectorAll(".choice-option");
  buttons[0].click();
  await settle();
  // 先看桩的现场：没有这一条，后面"没发消息"的用例全都可能是从没发过。
  if (order.join(",") !== "send:只看论文") return "桩的调用序列不对：" + JSON.stringify(order);
  if (sent.length !== 1) return "应发出 1 条，实际 " + JSON.stringify(sent);
  if (sent[0] !== "只看论文") return "发出去的内容不对：" + sent[0];
  if (card.getAttribute("data-answered") !== "paper") return "卡片没标记已选：" + card.getAttribute("data-answered");
  const stillLive = buttons.filter((b) => b.disabled !== true);
  if (stillLive.length) return "还有按钮没禁用：" + stillLive.length;
  if (!buttons[0].classList.contains("is-chosen")) return "被点中的那条没标 is-chosen";
  if (opened.length) return "当前会话的卡不该触发 openSession：" + JSON.stringify(opened);
  return true;
});

checkAsync("同一张卡连点两次（先论文后先不检索）→ 桩只收到 1 条，已选状态不被改写", async () => {
  resetSpies();
  running = false;
  state.sessionId = "s1";
  const card = bag.renderChoiceCard("s1", payload({ turn_id: "trn_twice", query: "连点两次" }));
  const buttons = card.querySelectorAll(".choice-option");
  buttons[0].click();
  buttons[6].click(); // 第二下在同一个 tick 里（连点最常见的形态）
  await settle();
  if (sent.length !== 1) return "连点应只发 1 条，实际 " + JSON.stringify(sent);
  if (order.join(",") !== "send:只看论文") return "第二下也调了桩：" + JSON.stringify(order);
  if (card.getAttribute("data-answered") !== "paper") return "已选状态被改写了：" + card.getAttribute("data-answered");
  return true;
});

checkAsync("轮次还在跑 → 只提示、不发消息、也不禁用（否则会「已选但什么都没发」）", async () => {
  resetSpies();
  running = true;
  const card = bag.renderChoiceCard("s1", payload({ turn_id: "trn_running", query: "跑着的时候点的" }));
  card.querySelectorAll(".choice-option")[0].click();
  await settle();
  running = false;
  if (sent.length) return "轮次在跑时不该发消息：" + JSON.stringify(sent);
  if (card.getAttribute("data-answered")) return "轮次在跑时不该把卡置为已选";
  if (!notices.length) return "没有提示用户「等它结束再选」";
  return true;
});

/* ---------------------------------------------- ⑤ 点击非当前会话的卡 → 先切过去 */

checkAsync("点非当前会话的卡：**先 openSession(s2) 再 send**（次序也要对，不串会话）", async () => {
  resetSpies();
  running = false;
  state.sessionId = "s1";
  const card = bag.renderChoiceCard("s2", payload({ session_id: "s2", turn_id: "trn_other", query: "别处的卡" }));
  card.querySelectorAll(".choice-option")[5].click();
  await settle();
  const want = "open:s2,send:都可以，全部来源都查";
  if (order.join(",") !== want) return "桩的调用序列不对：期望 " + want + "，实际 " + order.join(",");
  return expect(sent, ["都可以，全部来源都查"], "发出去的内容");
});

/*: 串行执行：一条跑完再跑下一条（理由见本节开头）。 */
(async function runAsyncCases() {
  for (const item of asyncCases) {
    try {
      const detail = await item.fn();
      const ok = detail === true || detail === undefined;
      cases.push({ name: item.name, ok: ok, got: ok ? "" : String(detail) });
    } catch (error) {
      cases.push({ name: item.name, ok: false, got: error && error.message ? error.message : String(error) });
    }
  }
  process.stdout.write(JSON.stringify(cases) + "\n");
})();
