/* py.exec 审批卡（`chat.needs_approval`）的**纯函数 + DOM 级**用例集。
 *
 * 用法：node approval_probe.js <app.js 路径>
 * 输出：一行 JSON —— [{name, ok, got}]，由 Python 侧断言（见 test_approval_card.py）。
 *
 * 为什么抽函数源码：app.js 是浏览器脚本（顶层用 window / document / state），require 进来
 * 会当场炸。这里按名字把产品文件里的那段源码切出来，放进 `new Function` 里求值 —— 测的是
 * **产品文件里的源码**，不是复刻版。
 *
 * DOM 用一个**最小 shim**（createElement / appendChild / setAttribute / classList /
 * querySelectorAll('.cls') / click），只实现产品代码真用到的那几个 API。它不是浏览器，
 * 所以"真渲染 + 代码框真的滚 + 相邻栏位几何 + 截图"仍由 `run/ui_approval_probe.py`
 *（无头 Edge）量 —— 这里钉的是"事件→卡片、点→调 sandbox.approve→禁卡、重复事件去重"
 * 这几条**行为**不会被改坏。
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
  // `async function foo(` 也是顶层函数：不把 async 一起切出来，求值时会因为有 await
  // 直接 SyntaxError（answerApproval 就是 async）。
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

/** 切出 `var NAME = {...};` / `var NAME = [...];` / `var NAME = "...";` 这样的顶层字面量。 */
function extractVar(src, name) {
  let start = src.indexOf("var " + name + " = ");
  if (start < 0) throw new Error("源码里找不到 var " + name + " = ");
  const opener = src[start + ("var " + name + " = ").length];
  if (opener === "{") {
    const open = src.indexOf("{", start);
    let depth = 0;
    for (let i = open; i < src.length; i += 1) {
      if (src[i] === "{") depth += 1;
      else if (src[i] === "}") {
        depth -= 1;
        if (depth === 0) return src.slice(open, i + 1);
      }
    }
    throw new Error(name + " 的花括号没配平");
  }
  if (opener === "[") {
    const open = src.indexOf("[", start);
    let depth = 0;
    for (let i = open; i < src.length; i += 1) {
      if (src[i] === "[") depth += 1;
      else if (src[i] === "]") {
        depth -= 1;
        if (depth === 0) return src.slice(open, i + 1);
      }
    }
    throw new Error(name + " 的方括号没配平");
  }
  // 字符串常量：切到本行末尾的分号
  const end = src.indexOf(";\n", start);
  if (end < 0) throw new Error(name + " 没有行尾分号");
  return src.slice(start + ("var " + name + " = ").length, end);
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

function loadVar(src, name) {
  const literal = extractVar(src, name);
  bag[name] = new Function("return (" + literal + ");")();
  return bag[name];
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
    removeAttribute(key) {
      delete node.attrs[key];
      if (key === "class") node.className = "";
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
    remove(name) {
      node.className = String(node.className)
        .split(/\s+/)
        .filter((c) => c && c !== name)
        .join(" ");
    },
    contains(name) {
      return hasClass(node, name);
    },
    toggle(name, on) {
      if (on) node.classList.add(name);
      else node.classList.remove(name);
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

const state = { sessionId: "s1", approvals: {} };
let approvedCalls = [];   // 桩桥收到的 sandbox.approve 入参
let approveResult = { ok: true, approval: "", error: "", data: { stdout: "", returncode: 0 } };
let approveThrows = "";

const deps = {
  document,
  state,
  streamBox,
  el: () => makeEl("div"),
  Promise,
};

/* `scout.call` 是产品里唯一的后端入口（approval 卡只用到它）。桩记下方法名与入参，
   并按 `approveResult` / `approveThrows` 造回包 —— 这两条够了：要验的是"点了发什么"，
   不是后端的执行结果。 */
const scout = {
  async call(method, params) {
    approvedCalls.push({ method, params: params || {} });
    if (approveThrows) throw new Error(approveThrows);
    return approveResult;
  },
};
deps.scout = scout;

bag.state = state;
bag.streamBox = streamBox;
bag.document = document;
bag.el = deps.el;
bag.scout = scout;
bag.Promise = Promise;

loadVar(APP, "APPROVAL_CATEGORY_TEXT");
loadVar(APP, "APPROVAL_SCOPE_TEXT");
loadVar(APP, "APPROVAL_LABELS");
loadVar(APP, "APPROVAL_ORDER");
loadVar(APP, "APPROVAL_DENY_NOTE");

load(APP, "approvalScopeText");
load(APP, "approvalRiskLabel");
load(APP, "approvalRiskDetails");
load(APP, "approvalOptionValues");
load(APP, "approvalWorkdirParam");
load(APP, "approvalCardKey");
load(APP, "approvalCards");
load(APP, "approvalTitleText");
/* 顺序不能反：renderApprovalCard 的闭包里直接调 answerApproval（点击回调），
   先 load answerApproval 才拿得到真函数 —— 反了会把 undefined 捕获进去，点击时才炸。 */
load(APP, "answerApproval");
load(APP, "renderApprovalCard");

/* --------------------------------------------------------------- 用例集 */

/*: 后端 `sandbox/approval.py::approval_options()` 的四条（value + 说明性 label）。
    这里照抄 value 集合是为了钉住"四个按钮各对应一个真入参"这个契约。 */
const OPTIONS = [
  { value: "deny", label: "不许执行" },
  { value: "once", label: "只允许这一次" },
  { value: "session", label: "本会话内允许同类操作" },
  { value: "always", label: "永远允许同类操作（内容级风险只到本会话）" },
];

const LONG_CODE = [
  "import docx",
  "from pathlib import Path",
  "",
  "src = Path('论文.docx')",
  "doc = docx.Document(str(src))",
  "for i, para in enumerate(doc.paragraphs):",
  "    if not para.text.strip():",
  "        continue",
  "    para.paragraph_format.space_after = 0",
  "    print(i, para.text[:20])",
].join("\n");

function payload(over) {
  return Object.assign(
    {
      session_id: "s1",
      turn_id: "trn_1",
      code: LONG_CODE,
      reason: "要写文件（exports/**）",
      workdir: "data/exports",
      pending: ["exec:@cwd", "write:@cwd"],
      risks: [
        { category: "exec", scope: "@cwd", detail: "在工作目录里执行 Python" },
        { category: "write", scope: "@cwd", detail: "open('out.xlsx', 'w')" },
      ],
      options: OPTIONS,
    },
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

/* ------------------------------------------------ ① 风险标签（pending → 人话） */

check("风险标签：exec:@cwd → 「执行代码：工作目录」，write:@cwd → 「写文件：工作目录」", () => {
  return expect(
    [bag.approvalRiskLabel("exec:@cwd"), bag.approvalRiskLabel("write:@cwd")],
    ["执行代码：工作目录", "写文件：工作目录"],
    "标签",
  );
});

check("风险标签：:@any → 「任意路径」，其余类别/范围各自有正文", () => {
  const got = [
    bag.approvalRiskLabel("exec:@any"),
    bag.approvalRiskLabel("network:@any"),
    bag.approvalRiskLabel("subprocess:@any"),
    bag.approvalRiskLabel("delete:@cwd"),
    bag.approvalRiskLabel("outside_path:outside:C:/Users/x"),
  ];
  const want = [
    "执行代码：任意路径",
    "网络访问：任意路径",
    "起子进程：任意路径",
    "删文件：工作目录",
    "访问工作目录外：工作目录外的 C:/Users/x",
  ];
  return expect(got, want, "标签");
});

check("风险标签：具体目录（exports/**）原样露出来，不抹成「某个目录」", () => {
  return expect(bag.approvalRiskLabel("write:exports/**"), "写文件：exports/**", "标签");
});

check("风险标签：未知类别退化成类别本身；空键 → 空串（不编一句「有风险」）", () => {
  return expect(
    [bag.approvalRiskLabel("weird:@cwd"), bag.approvalRiskLabel(""), bag.approvalRiskLabel(null)],
    ["weird：工作目录", "", ""],
    "标签",
  );
});

check("风险明细：risks[].detail 按规则键配对（具体命令单独标出来的来源）", () => {
  const got = bag.approvalRiskDetails(payload().risks);
  return expect(
    [got["write:@cwd"], got["exec:@cwd"]],
    ["open('out.xlsx', 'w')", "在工作目录里执行 Python"],
    "明细",
  );
});

/* ------------------------------------------------------------- ② 去重键 */

check("去重键：同一轮同一段代码 → 同键；改一行代码 → 另一个键", () => {
  const a = bag.approvalCardKey({ turn_id: "t1", code: "print(1)" });
  const b = bag.approvalCardKey({ turn_id: "t1", code: "print(1)" });
  const c = bag.approvalCardKey({ turn_id: "t1", code: "print(2)" });
  const d = bag.approvalCardKey({ turn_id: "t2", code: "print(1)" });
  return expect([a === b, a !== c, a !== d], [true, true, true], "同键/不同键");
});

check("去重键：缺字段也不炸（退化成「|」）", () => {
  return expect([bag.approvalCardKey({}), bag.approvalCardKey(null)], ["|", "|"], "键");
});

/* ---------------------------------------------------- ③ workdir 基准换算 */

check("workdir：载荷的 data/exports（相对项目根）→ 参数的 exports（相对 data/）", () => {
  return expect(
    [
      bag.approvalWorkdirParam("data/exports"),
      bag.approvalWorkdirParam("data/"),
      bag.approvalWorkdirParam("data"),
      bag.approvalWorkdirParam(""),
      bag.approvalWorkdirParam(null),
    ],
    ["exports", "", "", "", ""],
    "workdir",
  );
});

check("workdir：不带 data/ 前缀的形态原样发（后端自己会收窄并如实报错）", () => {
  return expect(
    [bag.approvalWorkdirParam("run/x"), bag.approvalWorkdirParam("data\\exports\\")],
    ["run/x", "exports"],
    "workdir",
  );
});

/* ----------------------------------- ④ 事件 → 卡片（固定高代码框 + 四选项） */

check("渲染：卡片落进该会话的流，代码框里是**完整**代码（一行不截）", () => {
  const card = bag.renderApprovalCard("s1", payload({ turn_id: "trn_render" }));
  if (!card) return "没有建出卡片";
  if (streams.s1.children.indexOf(card) < 0) return "卡片没落进 s1 的流";
  if (!card.classList.contains("msg-approval")) return "卡片没有 msg-approval 类（样式钩子）";
  const box = card.querySelector(".approval-code");
  if (!box) return "没有代码框（.approval-code）";
  return expect(box.textContent === LONG_CODE, true, "代码框正文 == 载荷 code");
});

check("渲染：四个选项齐、文案是人话、顺序 once→session→always→deny（拒绝最后）", () => {
  const card = bag.renderApprovalCard("s1", payload({ turn_id: "trn_opts" }));
  const buttons = card.querySelectorAll(".approval-option");
  const values = buttons.map((b) => b.getAttribute("data-approval"));
  const labels = buttons.map((b) => b.textContent);
  return expect(
    [values, labels],
    [
      ["once", "session", "always", "deny"],
      ["允许一次", "本会话允许", "永久允许", "拒绝"],
    ],
    "按钮",
  );
});

check("渲染：风险标签按 pending 逐条出，具体命令单独一块（.approval-risk-detail）", () => {
  const card = bag.renderApprovalCard("s1", payload({ turn_id: "trn_risks" }));
  const items = card.querySelectorAll(".approval-risk");
  if (items.length !== 2) return "风险条数不对：" + items.length;
  const labels = items.map((n) => n.querySelector(".approval-risk-label").textContent);
  const details = items.map((n) => {
    const d = n.querySelector(".approval-risk-detail");
    return d ? d.textContent : "";
  });
  return expect(
    [labels, details],
    [
      ["执行代码：工作目录", "写文件：工作目录"],
      ["在工作目录里执行 Python", "open('out.xlsx', 'w')"],
    ],
    "风险",
  );
});

check("渲染：**同一轮同一段代码的重复事件不重插**", () => {
  const before = streams.s1.querySelectorAll(".msg-approval").length;
  const first = bag.renderApprovalCard("s1", payload({ turn_id: "trn_dup" }));
  const again = bag.renderApprovalCard("s1", payload({ turn_id: "trn_dup" }));
  const after = streams.s1.querySelectorAll(".msg-approval").length;
  if (first !== again) return "重复事件返回的不是同一张卡";
  return expect([before + 1, after], [before + 1, before + 1], "卡片数（重复前后应相同）");
});

check("渲染：**非当前会话的卡不落到当前会话的流里**", () => {
  state.sessionId = "s1";
  const beforeS1 = streams.s1.querySelectorAll(".msg-approval").length;
  const card = bag.renderApprovalCard("s2", payload({ session_id: "s2", turn_id: "trn_b1" }));
  if (!card) return "s2 的卡没建出来";
  if (streams.s2.children.indexOf(card) < 0) return "卡没落进 s2 自己的流";
  return expect(streams.s1.querySelectorAll(".msg-approval").length, beforeS1, "s1 的卡片数（不该变）");
});

check("渲染：没有 session_id / 没有代码 → 不落任何人的流（不出一张空卡）", () => {
  const total =
    streams.s1.querySelectorAll(".msg-approval").length + streams.s2.querySelectorAll(".msg-approval").length;
  const noSession = bag.renderApprovalCard(null, payload({ session_id: null, turn_id: "trn_x1" }));
  const noCode = bag.renderApprovalCard("s1", payload({ turn_id: "trn_x2", code: "   " }));
  const after =
    streams.s1.querySelectorAll(".msg-approval").length + streams.s2.querySelectorAll(".msg-approval").length;
  return expect([noSession, noCode, after], [null, null, total], "空会话/空代码");
});

check("渲染：载荷没给 options 时四条仍在（少一个按钮 = 少一条路）", () => {
  const card = bag.renderApprovalCard("s1", payload({ turn_id: "trn_noopts", options: [] }));
  const values = card.querySelectorAll(".approval-option").map((b) => b.getAttribute("data-approval"));
  return expect(values, ["once", "session", "always", "deny"], "按钮");
});

/* ------------------------------------- ⑤ 点击 → sandbox.approve → 禁卡 */

const asyncCases = [];
function checkAsync(name, fn) {
  asyncCases.push({ name: name, fn: fn });
}

/** 等桩里的微任务落地：answerApproval 是 async，点击那一拍之后还要跑几个 await。 */
function settle() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function resetSpies() {
  approvedCalls = [];
  approveResult = { ok: true, approval: "", error: "", data: { stdout: "", returncode: 0 } };
  approveThrows = "";
  state.sessionId = "s1";
}

checkAsync("点「允许一次」→ 桩收到 sandbox.approve{approval:once, code, session_id, workdir}，整卡禁用", async () => {
  resetSpies();
  const card = bag.renderApprovalCard("s1", payload({ turn_id: "trn_once" }));
  const buttons = card.querySelectorAll(".approval-option");
  buttons[0].click();
  await settle();
  if (approvedCalls.length !== 1) return "应调用 1 次 sandbox.approve，实际 " + JSON.stringify(approvedCalls);
  const call = approvedCalls[0];
  if (call.method !== "sandbox.approve") return "调的方法不对：" + call.method;
  if (call.params.approval !== "once") return "approval 入参不对：" + call.params.approval;
  if (call.params.code !== LONG_CODE) return "code 没原样带回去";
  if (call.params.session_id !== "s1") return "session_id 不对：" + call.params.session_id;
  if (call.params.workdir !== "exports") return "workdir 基准没换算：" + call.params.workdir;
  if (card.getAttribute("data-approved") !== "once") return "卡片没标记已选：" + card.getAttribute("data-approved");
  const live = buttons.filter((b) => b.disabled !== true);
  if (live.length) return "还有按钮没禁用：" + live.length;
  if (!buttons[0].classList.contains("is-chosen")) return "被点中的那条没标 is-chosen";
  const note = card.querySelector(".approval-note");
  if (!note || note.hidden !== false) return "没有给出放行结果";
  if (note.textContent.indexOf("允许一次") < 0) return "结果行没说清选了哪条：" + note.textContent;
  return true;
});

checkAsync("点「永久允许」→ 入参 always（四个按钮各自的 value 就是 RPC 的 approval）", async () => {
  resetSpies();
  const card = bag.renderApprovalCard("s1", payload({ turn_id: "trn_always" }));
  card.querySelectorAll(".approval-option")[2].click();
  await settle();
  if (approvedCalls.length !== 1) return "应调用 1 次，实际 " + approvedCalls.length;
  return expect(approvedCalls[0].params.approval, "always", "approval");
});

checkAsync("点「拒绝」→ 入参 deny，且显示「不要换写法重试，也不要换条路达成同一目的」", async () => {
  resetSpies();
  const card = bag.renderApprovalCard("s1", payload({ turn_id: "trn_deny" }));
  card.querySelectorAll(".approval-option")[3].click();
  await settle();
  if (approvedCalls.length !== 1) return "应调用 1 次，实际 " + approvedCalls.length;
  if (approvedCalls[0].params.approval !== "deny") return "approval 入参不对：" + approvedCalls[0].params.approval;
  const note = card.querySelector(".approval-note");
  if (!note || note.hidden !== false) return "拒绝之后没有文案行";
  if (note.textContent.indexOf("不要换写法重试，也不要换条路达成同一目的") < 0) {
    return "拒绝文案不对：" + note.textContent;
  }
  if (note.getAttribute("data-kind") !== "deny") return "文案行没标 data-kind=deny";
  return true;
});

checkAsync("同一张卡连点两次（先允许一次、再拒绝）→ 桩只收到 1 次，已选不被改写", async () => {
  resetSpies();
  const card = bag.renderApprovalCard("s1", payload({ turn_id: "trn_twice" }));
  const buttons = card.querySelectorAll(".approval-option");
  buttons[0].click();
  buttons[3].click(); // 第二下在同一个 tick 里（连点最常见的形态）
  await settle();
  if (approvedCalls.length !== 1) return "连点应只调用 1 次，实际 " + approvedCalls.length;
  if (approvedCalls[0].params.approval !== "once") return "第二下也发了：" + approvedCalls[0].params.approval;
  if (card.getAttribute("data-approved") !== "once") return "已选状态被改写：" + card.getAttribute("data-approved");
  return true;
});

checkAsync("后端回包失败（RPC 抛错）→ 卡片**恢复可点**并说明原因（不是一张死卡）", async () => {
  resetSpies();
  approveThrows = "连接已断开";
  const card = bag.renderApprovalCard("s1", payload({ turn_id: "trn_fail" }));
  const buttons = card.querySelectorAll(".approval-option");
  buttons[0].click();
  await settle();
  if (card.getAttribute("data-approved")) return "失败之后卡片还锁着：" + card.getAttribute("data-approved");
  const live = buttons.filter((b) => b.disabled !== true);
  if (live.length !== buttons.length) return "失败之后只恢复了一部分按钮：" + live.length + "/" + buttons.length;
  const note = card.querySelector(".approval-note");
  if (!note || note.hidden !== false) return "失败之后没有任何说明";
  return note.textContent.indexOf("连接已断开") >= 0 ? true : "说明里没有原因：" + note.textContent;
});

/*: 串行执行：一条跑完再跑下一条（理由同 choice_probe：共享 spy，并发会互相清空现场）。 */
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
