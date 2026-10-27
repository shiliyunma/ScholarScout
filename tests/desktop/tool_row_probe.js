/* 工具行「说人话」+ 实时进度行的用例集（纯函数真值表 + DOM 级行为）。
 *
 * 用法：node tool_row_probe.js <app.js 路径>
 * 输出：一行 JSON —— [{name, ok, got}]，由 Python 侧断言（见 test_tool_row_language.py）。
 *
 * 为什么这么测：app.js 是浏览器脚本（顶层用 window / document / state），require 进来会当场炸，
 * 所以按名字把产品文件里的那段源码切出来、放进 new Function 里求值 —— 测的是**产品文件里的源码**，
 * 不是复刻版。DOM 用最小 shim（与 choice_probe.js 同一手法），并带一个**假时钟 + 假定时器**，
 * 于是"每秒走秒""工具结束停表"">20s 追加提示""首个正文到达淡出"这些时间行为可以确定性地断言。
 *
 * 真渲染/截图仍归无头浏览器探针；这里钉的是"文案映射不倒退、等待可读、淡出不跳"这几条行为。
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

/** 按名字切出一个顶层函数声明。
 *  切片用"顶格收尾 `\n}`"而不是花括号配平：函数体里有 `/^[\[\{<"']/` 这种**带花括号的正则
 *  字符类**，配平法会把 `\{` 当成块开始，一路切到文件末尾（与 test_turn_spinner 同一手法）。 */
function extractFunction(src, name) {
  let start = src.indexOf("function " + name + "(");
  if (start < 0) throw new Error("源码里找不到 function " + name + "(");
  if (src.slice(Math.max(0, start - 6), start) === "async ") start -= 6;
  const end = src.indexOf("\n}", start);
  if (end < 0) throw new Error(name + " 没有顶格收尾的 }");
  return src.slice(start, end + 2);
}

/** 切出 `var NAME = {...}` / `var NAME = [...]` 这样的顶层字面量（简单标量取到行尾）。 */
function extractVar(src, name) {
  const head = "var " + name + " = ";
  const start = src.indexOf(head);
  if (start < 0) throw new Error("源码里找不到 var " + name + " = ");
  if (!/^[{[[]/.test(src.slice(start + head.length))) {
    const end = src.indexOf("\n", start);
    return src.slice(start, end < 0 ? src.length : end).replace(/\r$/, "");
  }
  const open = src.indexOf(/\{|\[/.exec(src.slice(start))[0], start);
  const pair = src[open] === "{" ? ["{", "}"] : ["[", "]"];
  let depth = 0;
  for (let i = open; i < src.length; i += 1) {
    if (src[i] === pair[0]) depth += 1;
    else if (src[i] === pair[1]) {
      depth -= 1;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(name + " 的括号没配平");
}

/** 依赖包：函数之间互相引用，所以先建一个可变的袋子，加载后往里填。 */
const bag = {};

function load(src, name) {
  const deps = Object.assign({}, bag);
  const keys = Object.keys(deps);
  const body = extractFunction(src, name);
  const fn = new Function(...keys, "return (" + body + ");")(...keys.map((k) => deps[k]));
  bag[name] = fn;
  return fn;
}

function loadVar(src, name) {
  const decl = extractVar(src, name);
  bag[name] = new Function(decl + "\nreturn " + name + ";")();
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
    className: "",
    textContent: "",
    title: "",
    hidden: false,
    scrollTop: 0,
    scrollHeight: 0,
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
    insertBefore(child, ref) {
      if (child.parentNode) child.parentNode.removeChild(child);
      const at = ref ? node.children.indexOf(ref) : -1;
      if (at < 0) {
        node.children.push(child);
      } else {
        node.children.splice(at, 0, child);
      }
      child.parentNode = node;
      return child;
    },
    get firstChild() {
      return node.children.length ? node.children[0] : null;
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
  };
  return node;
}

const document = { createElement: (tag) => makeEl(tag) };

/* ------------------------------------------------------- 假时钟 / 假定时器 */

let nowMs = 0;
const FakeDate = {
  now: () => nowMs,
};
const timers = { intervals: [], timeouts: [] };

function setInterval(fn) {
  const id = { kind: "interval", fn: fn };
  timers.intervals.push(id);
  return id;
}
function clearInterval(id) {
  timers.intervals = timers.intervals.filter((t) => t !== id);
}
function setTimeout(fn) {
  const id = { kind: "timeout", fn: fn };
  timers.timeouts.push(id);
  return id;
}
function clearTimeout(id) {
  timers.timeouts = timers.timeouts.filter((t) => t !== id);
}
/** 假时钟前进 seconds 秒并让走秒回调跑一次（真实实现是 1000ms 一次）。 */
function advance(seconds) {
  nowMs += seconds * 1000;
  timers.intervals.slice().forEach((t) => t.fn());
}
function flushTimeouts() {
  const due = timers.timeouts.slice();
  timers.timeouts = [];
  due.forEach((t) => t.fn());
}
function resetClock() {
  nowMs = 0;
  timers.intervals = [];
  timers.timeouts = [];
}

/* --------------------------------------------------- 产品 makePending 的桩依赖 */

/** 产品里 bubble() 返回 {body, who, wrap}（气泡本体 / 作者名 / 消息流节点）。 */
function bubble() {
  return { body: makeEl("div"), who: makeEl("span"), wrap: makeEl("div") };
}

bag.document = document;
bag.bubble = bubble;
bag.el = () => makeEl("div");
bag.ScoutMarkdown = { renderInto: () => undefined };
bag.state = { sessionId: "s1", streams: {} };
bag.Date = FakeDate;
bag.setInterval = setInterval;
bag.clearInterval = clearInterval;
bag.setTimeout = setTimeout;
bag.clearTimeout = clearTimeout;

/* ------------------------------------------------------------ 加载产品源码 */

loadVar(APP, "TOOL_ACTIONS");
loadVar(APP, "TOOL_ACTION_PREFIXES");
loadVar(APP, "TOOL_PROGRESS_HINT_S");

/* 有几个依赖住在 api.js（工具结果摘要 / 耗时 / 分段纯函数）—— 同一个界面目录，按需从那边切。 */
const API = fs.readFileSync(path.join(path.dirname(appPath), "api.js"), "utf8");
function loadAny(name) {
  return load(APP.indexOf("function " + name + "(") >= 0 ? APP : API, name);
}

load(APP, "toolUnescapeJson");
load(APP, "toolArgsObject");
load(APP, "toolDomainText");
load(APP, "toolFileText");
load(APP, "toolDetailUsable");
load(APP, "toolHumanParts");
load(APP, "toolProgressText");
loadVar(API, "TOOL_REF_LABELS");
loadAny("toolResultSummary");
loadAny("fmtDuration");
loadAny("toolLatencyText");
loadAny("replyRoundNext");
load(APP, "makePending");

/* --------------------------------------------------------------- 用例集 */

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

/* ---------------------------------------------- ① 工具名 → 人话（映射表真值表） */

/*: 每条都是**真后端会推过来的载荷形状**（args_preview 就是 call.arguments[:200]）。 */
const MAP_CASES = [
  ["research.search", '{"query": "教育智能体综述", "max_results": 8}', "正在检索：教育智能体综述"],
  ["research.search", '{"question": "什么是教育智能体"}', "正在检索：什么是教育智能体"],
  ["kb.search", '{"query": "我以前收过的资料"}', "正在检索资料库：我以前收过的资料"],
  ["source.fetch", '{"url": "https://arxiv.org/abs/2401.00001"}', "正在抓取：arxiv.org"],
  ["source.fetch", '{"url": "https://www.example.com:8443/a/b.pdf"}', "正在抓取：example.com"],
  ["archive.download", '{"url": "http://files.example.org/x.zip"}', "正在抓取：files.example.org"],
  ["file.read", '{"path": "data/text/src_001.txt"}', "正在读取：src_001.txt"],
  ["file.read", '{"path": "D:\\\\AI+教育毕设毕设\\\\ScholarScout\\\\prompts\\\\chat.md"}', "正在读取：chat.md"],
  ["file.list", '{"path": "data/text"}', "正在浏览目录：text"],
  ["note.save", '{"kind": "note", "title": "第 3 周笔记"}', "正在整理笔记：第 3 周笔记"],
  ["py.exec", '{"code": "print(1)"}', "正在运行代码"],
  ["topic.build_map", '{"topic": "教育智能体"}', "正在梳理主题地图：教育智能体"],
  ["route.plan", '{"topic": "多智能体", "weeks": 4}', "正在规划路线：多智能体"],
  ["route.revise", '{"route_id": "r1", "instruction": "把第 2 周换成本地库"}', "正在修订路线：把第 2 周换成本地库"],
  ["source.grade", '{"topic": "教育智能体"}', "正在评估来源：教育智能体"],
  ["research.delegate", '{"goals": ["a", "b", "c"]}', "正在拆分调研：3 个子问题"],
  ["tool.search", '{"query": "检索工具"}', "正在查找工具：检索工具"],
  ["tool.describe", '{"name": "research.search"}', "正在读取工具说明：research.search"],
  ["skill.list", "{}", "正在查看技能"],
  ["skill.load", '{"name": "hermes-agent"}', "正在加载技能：hermes-agent"],
  ["note.update", '{"title": "改一下"}', "正在整理笔记：改一下"],
];

check("映射表：每个登记过的工具都有中文动作 + 可读摘要", () => {
  const got = MAP_CASES.map((item) => bag.toolHumanParts(item[0], item[1]).text);
  const want = MAP_CASES.map((item) => item[2]);
  const bad = got
    .map((text, i) => (text === want[i] ? null : `${MAP_CASES[i][0]}：期望「${want[i]}」，实际「${text}」`))
    .filter(Boolean);
  return bad.length ? bad.join("；") : true;
});

check("映射表：登记表覆盖后端注册表的全部工具名（漏一个就会退化成英文）", () => {
  const names = [
    "file.list",
    "file.read",
    "kb.search",
    "note.save",
    "py.exec",
    "research.delegate",
    "research.search",
    "route.plan",
    "route.revise",
    "skill.list",
    "skill.load",
    "source.fetch",
    "source.grade",
    "tool.describe",
    "tool.search",
    "topic.build_map",
  ];
  const missing = names.filter((name) => !Object.prototype.hasOwnProperty.call(bag.TOOL_ACTIONS, name));
  return missing.length ? "映射表缺少：" + missing.join(", ") : true;
});

/* ------------------------------------------------------ ② 未知工具的回退 */

check("未知工具：回退成「正在执行：<工具名>」，不显示参数", () => {
  const parts = bag.toolHumanParts("quantum.entangle", '{"qubit": "q0", "theta": 0.5}');
  if (parts.text !== "正在执行：quantum.entangle") return "文案不对：" + parts.text;
  if (parts.action !== "正在执行") return "动作不对：" + parts.action;
  return true;
});

check("未知工具：连工具名都没有时给「正在执行：未知工具」，不炸", () => {
  return expect(
    [bag.toolHumanParts("", '{"x":1}').text, bag.toolHumanParts(null, null).text],
    ["正在执行：未知工具", "正在执行：未知工具"],
    "文案",
  );
});

/* ------------------------------------------------- ③ 绝不渲染原始 JSON */

check("不渲染原始 JSON：所有用例的输出里都没有花括号 / 引号 / 原样参数", () => {
  const offenders = [];
  MAP_CASES.concat([
    ["quantum.entangle", '{"qubit": "q0"}', ""],
    ["research.search", "{}", ""],
    ["research.search", "", ""],
    ["research.search", '{"max_results": 8}', ""],
    ["note.save", '{"cards": [{"front": "a", "back": "b"}]}', ""],
    ["source.fetch", '{"url": ""}', ""],
  ]).forEach((item) => {
    const text = bag.toolHumanParts(item[0], item[1]).text;
    if (/[{}[\]"]/.test(text)) offenders.push(`${item[0]} → ${text}`);
    if (String(item[1]).length > 4 && text.indexOf(String(item[1])) >= 0) offenders.push(`${item[0]} 把原参数铺出来了`);
  });
  return offenders.length ? offenders.join("；") : true;
});

check("参数里没有可读信息时只显示动作名（不拿字段名 / ID 凑数）", () => {
  return expect(
    [
      bag.toolHumanParts("research.search", "{}").text,
      bag.toolHumanParts("research.search", "").text,
      bag.toolHumanParts("py.exec", "").text,
      bag.toolHumanParts("source.grade", '{"max_results": 3}').text,
    ],
    ["正在检索", "正在检索", "正在运行代码", "正在评估来源"],
    "文案",
  );
});

check("截断的 args_preview（后端只给前 200 字）也能读出前面那几对", () => {
  const truncated = '{"query": "教育智能体综述", "max_results": 8, "year_from": 2024, "topi';
  const parts = bag.toolHumanParts("research.search", truncated);
  return parts.text === "正在检索：教育智能体综述" ? true : "读不出来：" + parts.text;
});

check("参数值本身是 JSON 串时不展示（宁可不显示，也不铺花括号）", () => {
  const parts = bag.toolHumanParts("research.search", '{"query": "{\\"a\\": 1}"}');
  return parts.text === "正在检索" ? true : "把 JSON 值显示出来了：" + parts.text;
});

/* ------------------------------------------ ④ 进度行：出现 / 走秒 / 停表 */

check("进度行：工具调用时出现在工具区**顶部**并开始走秒（0s → 12s）", () => {
  resetClock();
  const trace = bag.makePending("s1");
  const flow = trace.node.body.children.filter((c) => hasClass(c, "tool-flow"))[0];
  if (!flow) return "没找到 .tool-flow";
  trace.addToolCall({ call_id: "c1", tool: "research.search", args_preview: '{"query": "教育智能体"}' });
  const row = flow.children[0];
  if (!hasClass(row, "tool-progress")) return "工具区第一个子节点不是进度行：" + (row && row.className);
  if (row.hidden) return "进度行没亮";
  if (timers.intervals.length !== 1) return "没有起走秒定时器：" + timers.intervals.length;
  const first = row.children.map((c) => c.textContent).join("|");
  if (first.indexOf("0s") < 0) return "初始没有 0s：" + first;
  advance(12);
  const later = row.children.map((c) => c.textContent).join("|");
  if (later.indexOf("12s") < 0) return "没有走秒到 12s：" + later;
  if (later.indexOf("正在检索…") < 0) return "动作没说人话：" + later;
  return true;
});

check("进度行：工具结束就停表（秒数不跳），第二个工具显示「第 2 步」", () => {
  resetClock();
  const trace = bag.makePending("s1");
  const flow = trace.node.body.children.filter((c) => hasClass(c, "tool-flow"))[0];
  trace.addToolCall({ call_id: "c1", tool: "research.search", args_preview: '{"query": "教育智能体"}' });
  advance(7);
  trace.addToolResult({ call_id: "c1", ok: true, refs: {} });
  if (timers.intervals.length !== 0) return "工具结束后还在走秒：" + timers.intervals.length;
  const progress = flow.children[0];
  const frozen = progress.children.map((c) => c.textContent).join("|");
  if (frozen.indexOf("7s") < 0) return "停表时没有停在 7s：" + frozen;
  advance(30); // 假时钟继续跑，但没有回调 → 文案必须不动
  const still = progress.children.map((c) => c.textContent).join("|");
  if (still !== frozen) return `停表之后还在跳：「${frozen}」→「${still}」`;
  trace.addToolCall({ call_id: "c2", tool: "source.fetch", args_preview: '{"url": "https://arxiv.org/abs/1"}' });
  const second = progress.children.map((c) => c.textContent).join("|");
  if (second.indexOf("第 2 步") < 0) return "第二个工具没标第 2 步：" + second;
  if (second.indexOf("正在抓取…") < 0) return "第二个工具的动作不对：" + second;
  return true;
});

check("进度行：超过 20s 追加「还在跑，可继续等或按停止」（19s 时不说）", () => {
  resetClock();
  const trace = bag.makePending("s1");
  const flow = trace.node.body.children.filter((c) => hasClass(c, "tool-flow"))[0];
  trace.addToolCall({ call_id: "c1", tool: "research.search", args_preview: '{"query": "长任务"}' });
  const progress = flow.children[0];
  const hintEl = progress.children.filter((c) => hasClass(c, "tool-progress-hint"))[0];
  advance(19);
  if (!hintEl.hidden) return "19s 就把提示亮出来了：" + hintEl.textContent;
  advance(2);
  if (hintEl.hidden) return "21s 了提示还是藏着的";
  return hintEl.textContent === "还在跑，可继续等或按停止" ? true : "提示文案不对：" + hintEl.textContent;
});

/* -------------------------------------- ⑤ 进度行：正文到了淡出，收尾摘干净 */

check("进度行：首个正文到达 → 先加 is-leaving 淡出，再摘节点（不是突然消失）", () => {
  resetClock();
  const trace = bag.makePending("s1");
  const flow = trace.node.body.children.filter((c) => hasClass(c, "tool-flow"))[0];
  trace.addToolCall({ call_id: "c1", tool: "research.search", args_preview: '{"query": "教育智能体"}' });
  advance(5);
  const progress = flow.children[0];
  trace.pushDelta("正文第一句");
  if (!hasClass(progress, "is-leaving")) return "正文到了却没进入淡出状态";
  if (timers.intervals.length !== 0) return "淡出时还在走秒";
  if (progress.parentNode !== flow) return "淡出是立即摘节点（会让人以为出错）";
  flushTimeouts();
  if (progress.parentNode) return "淡出结束后节点还挂在树上";
  return true;
});

check("进度行：收尾（finish）后不残留节点，也不残留走秒定时器", () => {
  resetClock();
  const trace = bag.makePending("s1");
  const flow = trace.node.body.children.filter((c) => hasClass(c, "tool-flow"))[0];
  trace.addToolCall({ call_id: "c1", tool: "source.fetch", args_preview: '{"url": "https://arxiv.org/x"}' });
  trace.addToolCall({ call_id: "c2", tool: "py.exec", args_preview: '{"code": "1"}' });
  trace.finish();
  if (flow.querySelectorAll(".tool-progress").length) return "收尾后进度行还在";
  if (timers.intervals.length) return "收尾后定时器还在：" + timers.intervals.length;
  if (timers.timeouts.length) return "收尾后淡出定时器还在：" + timers.timeouts.length;
  return true;
});

check("进度行：第二轮工具 + 第二轮正文 → 进度行插回顶部并再次淡出", () => {
  resetClock();
  const trace = bag.makePending("s1");
  const flow = trace.node.body.children.filter((c) => hasClass(c, "tool-flow"))[0];
  trace.addToolCall({ call_id: "c1", tool: "research.search", args_preview: '{"query": "第一轮"}' });
  trace.pushDelta("第一段正文");
  flushTimeouts();
  trace.addToolCall({ call_id: "c2", tool: "source.fetch", args_preview: '{"url": "https://arxiv.org/x"}' });
  const progress = flow.children[0];
  if (!hasClass(progress, "tool-progress")) return "第二轮工具调用后进度行没回到工具区顶部：" + (progress && progress.className);
  if (progress.parentNode !== flow) return "进度行没挂回来";
  if (hasClass(progress, "is-leaving")) return "新一轮等待开始时还带着上一次的淡出状态";
  trace.pushDelta("第二段正文");
  if (!hasClass(progress, "is-leaving")) return "第二轮正文到了却没淡出（进度行会一直挡在那儿）";
  return true;
});

check("工具行本身也说人话：动作 + 摘要分两段，title 里只有工具名", () => {
  resetClock();
  const trace = bag.makePending("s1");
  const flow = trace.node.body.children.filter((c) => hasClass(c, "tool-flow"))[0];
  trace.addToolCall({ call_id: "c1", tool: "source.fetch", args_preview: '{"url": "https://arxiv.org/abs/2401.00001"}' });
  const row = flow.children.filter((c) => hasClass(c, "tool-row"))[0];
  if (!row) return "没建出工具行";
  const name = row.querySelectorAll(".tool-row-name")[0].textContent;
  const args = row.querySelectorAll(".tool-row-args")[0].textContent;
  if (name !== "正在抓取") return "动作不对：" + name;
  if (args !== "：arxiv.org") return "摘要不对：" + args;
  if (row.title !== "工具：source.fetch") return "title 里不该有参数：" + row.title;
  if (row.getAttribute("data-tool") !== "source.fetch") return "data-tool 丢了（排查要用）";
  return true;
});

/* ------------------------------------------------------------------ 输出 */

process.stdout.write(JSON.stringify(cases) + "\n");
