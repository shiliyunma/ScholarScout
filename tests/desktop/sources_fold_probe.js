/* 资料库「分组默认折叠」的**真源码**用例集（node，最小假 DOM，不需要浏览器）。
 *
 * 用法：node sources_fold_probe.js <sources.js 路径>
 * 输出：一行 JSON —— [{name, ok, got}]，由 Python 侧断言（见 test_sources_default_fold.py）。
 *
 * 为什么要这份用例：用户要求「资料库列表每次启动都默认折叠、会话内展开过的可记住」。
 * 这两条都**只有真跑代码才区分得开**：
 *   · 若默认值写成"展开"（老行为），冷启动那一条会红；
 *   · 若折叠态写进 localStorage，模拟重启那一条会红（第二次求值仍记着"展开"）；
 *   · 若切换只翻 DOM 不记状态，重画列表之后展开态会丢（用例 ④ 覆盖）。
 *
 * 假 DOM 只需要 sourceGroupHeader 用到的能力：createElement / className / textContent /
 * setAttribute / getAttribute / appendChild / addEventListener / classList（带 force 的 toggle）/
 * click / 以及行链用的 nextElementSibling（这里手工串起来）。
 */
"use strict";

const fs = require("fs");
const path = require("path");

const srcPath = path.resolve(
  process.argv[2] || path.join(__dirname, "..", "..", "src", "scholarscout", "desktop", "ui", "sources.js")
);
const SRC = fs.readFileSync(srcPath, "utf8");

/** 按名字切出一个顶层函数声明（花括号配平）。切不到就抛 —— 宁可红，也不要静默测了个空。 */
function extractFunction(src, name) {
  const start = src.indexOf("function " + name + "(");
  if (start < 0) throw new Error("源码里找不到 function " + name + "(");
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

/** 状态表声明那一行（`var sourcesFoldState = {};`）。 */
function extractState(src) {
  const m = src.match(/var sourcesFoldState\s*=\s*\{[^}]*\}\s*;/);
  if (!m) throw new Error("源码里找不到 var sourcesFoldState = {…};");
  return m[0];
}

const STATE_DECL = extractState(SRC);
const IS_FOLDED = extractFunction(SRC, "sourcesIsFolded");
const SET_FOLDED = extractFunction(SRC, "sourcesSetFolded");
const LABEL = extractFunction(SRC, "sourceGroupLabel");
// 组头文案由 sourceGroupLabel 统一算（搜索时要报本组命中数）—— 少了它组头求值不出来
const HEADER = LABEL + "\n" + extractFunction(SRC, "sourceGroupHeader");

/* ------------------------------------------------------------ 最小假 DOM */

function makeClassList(node) {
  const list = () => String(node.className || "").split(/\s+/).filter(Boolean);
  return {
    contains: (name) => list().indexOf(name) >= 0,
    add(name) {
      if (!this.contains(name)) node.className = list().concat([name]).join(" ");
    },
    remove(name) {
      node.className = list().filter((x) => x !== name).join(" ");
    },
    toggle(name, force) {
      const on = force === undefined ? !this.contains(name) : !!force;
      if (on) this.add(name);
      else this.remove(name);
      return on;
    },
  };
}

function makeElement(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
    className: "",
    textContent: "",
    type: "",
    hidden: false,
    children: [],
    attrs: {},
    listeners: {},
    nextElementSibling: null,
    setAttribute(key, value) {
      this.attrs[String(key)] = String(value);
    },
    getAttribute(key) {
      return Object.prototype.hasOwnProperty.call(this.attrs, String(key)) ? this.attrs[String(key)] : null;
    },
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
    click(event) {
      const fns = this.listeners.click || [];
      if (!fns.length) throw new Error("这个节点上没有 click 监听（点了不会有反应）");
      fns.forEach((fn) => fn(event || { type: "click", stopPropagation() {} }));
    },
  };
  node.classList = makeClassList(node);
  return node;
}

globalThis.document = { createElement: (tag) => makeElement(tag) };

/** 每次调用都重新求值一份源码 → 等价于"进程刚启动"（内存里的状态表是空的）。 */
function freshRuntime() {
  const factory = new Function(
    "document",
    "return (function () {\n" + STATE_DECL + "\n" + IS_FOLDED + "\n" + SET_FOLDED + "\n" + HEADER +
      "\nreturn { isFolded: sourcesIsFolded, setFolded: sourcesSetFolded, header: sourceGroupHeader };\n})();"
  );
  return factory(globalThis.document);
}

const GROUP = { key: "s1", title: "教育智能体入门调研", items: [{}, {}, {}] };

/** 造一个组头 + 3 行，并按真实 DOM 的样子串好 nextElementSibling。 */
function buildGroup(runtime, folded) {
  const li = runtime.header(GROUP, folded);
  const rows = [makeElement("li"), makeElement("li"), makeElement("li")];
  rows.forEach((row) => {
    row.className = "source-row" + (folded ? " is-folded-row" : "");
  });
  li.nextElementSibling = rows[0];
  rows[0].nextElementSibling = rows[1];
  rows[1].nextElementSibling = rows[2];
  return { li: li, rows: rows };
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

/* ------------------------------------------------------------ ① 冷启动默认折叠 */

check("冷启动：没记录过的组一律**折叠**（这条就是「每次启动默认折叠」）", () => {
  const rt = freshRuntime();
  return expect([rt.isFolded("s1"), rt.isFolded("")], [true, true], "默认值");
});

check("冷启动：造出来的组头是折叠态（is-folded + ▸ + aria-expanded=false）", () => {
  const rt = freshRuntime();
  const { li } = buildGroup(rt, rt.isFolded(GROUP.key));
  const button = li.children[0];
  return expect(
    [li.classList.contains("is-folded"), button.textContent, button.getAttribute("aria-expanded")],
    [true, "▸ 教育智能体入门调研（3 条）", "false"],
    "折叠态组头"
  );
});

/* ------------------------------------------------------------ ② 会话内记住 */

check("展开一次之后记住：同一份运行里再问，已经不是折叠态", () => {
  const rt = freshRuntime();
  const { li } = buildGroup(rt, rt.isFolded(GROUP.key));
  li.children[0].click();
  return expect([rt.isFolded("s1"), li.classList.contains("is-folded")], [false, false], "展开后");
});

check("再点一次折回去：状态跟着点（不是只翻一次）", () => {
  const rt = freshRuntime();
  const { li } = buildGroup(rt, rt.isFolded(GROUP.key));
  const button = li.children[0];
  const seen = [];
  for (let i = 0; i < 3; i += 1) {
    button.click();
    seen.push([li.classList.contains("is-folded"), button.getAttribute("aria-expanded")]);
  }
  return expect(seen, [[false, "true"], [true, "false"], [false, "true"]], "三连击");
});

check("展开/收起同时带走这一组的**所有**行（含最后一行，不能漏一个）", () => {
  const rt = freshRuntime();
  const { li, rows } = buildGroup(rt, rt.isFolded(GROUP.key));
  const button = li.children[0];
  button.click();
  const expanded = rows.map((r) => r.classList.contains("is-folded-row"));
  button.click();
  const foldedAgain = rows.map((r) => r.classList.contains("is-folded-row"));
  return expect([expanded, foldedAgain], [[false, false, false], [true, true, true]], "行态");
});

check("两个组互不干扰（按 key 记，不是一个全局开关）", () => {
  const rt = freshRuntime();
  const a = buildGroup(rt, rt.isFolded("s1"));
  const b = buildGroup(rt, rt.isFolded("s2"));
  a.li.children[0].click();
  return expect([rt.isFolded("s1"), rt.isFolded("s2"), b.li.classList.contains("is-folded")], [false, true, true], "分组独立");
});

check("列表重画（重新求值 = 同一进程里再画一次不清状态）之后，展开态还在", () => {
  const rt = freshRuntime();
  buildGroup(rt, rt.isFolded(GROUP.key)).li.children[0].click();
  // 「重画」= 按同一份运行的状态表重新造一遍组
  const again = buildGroup(rt, rt.isFolded(GROUP.key));
  return expect([again.li.classList.contains("is-folded"), rt.isFolded(GROUP.key)], [false, false], "重画后");
});

/* ------------------------------------------------------------ ③ 重启 */

check("重启（重新求值一份新源码）之后回到折叠 —— 状态不落 localStorage", () => {
  const first = freshRuntime();
  buildGroup(first, first.isFolded(GROUP.key)).li.children[0].click();
  const second = freshRuntime();
  return expect([first.isFolded(GROUP.key), second.isFolded(GROUP.key)], [false, true], "重启后");
});

check("状态表不碰 localStorage / sessionStorage（否则重启就不是默认折叠了）", () => {
  const block = [STATE_DECL, IS_FOLDED, SET_FOLDED].join("\n");
  const banned = ["localStorage", "sessionStorage"].filter((name) => block.indexOf(name) >= 0);
  return expect(banned, [], "持久化调用");
});

process.stdout.write(JSON.stringify(cases));
