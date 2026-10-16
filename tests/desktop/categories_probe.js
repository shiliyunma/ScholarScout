/* 浮层分类组件 `uiCategory()` 的**真源码**用例集（node 下用最小假 DOM 跑，不需要浏览器）。
 *
 * 用法：node categories_probe.js <profile.js 路径>
 * 输出：一行 JSON —— [{name, ok, got}]，由 Python 侧断言（见 test_ui_categories.py）。
 *
 * 为什么不 require 整个 profile.js：它是浏览器脚本（顶层用 document / scout / el），
 * require 进来会当场炸。这里按名字把 `uiCategory` 的源码切出来放进 `new Function` 求值 ——
 * 测的仍是**产品文件里的那段源码**，不是复刻版（与 multisession_probe.js 同一手法）。
 *
 * 为什么要有它：分组是「点了才展开」的交互，光读代码文本只能证明"写了 click 监听"，
 * 证明不了**点下去真的翻了类、同步了 aria-expanded、并且把 body 的 hidden 解开了**。
 * 本项目在"类选择器上的 display 压掉 UA 的 [hidden] 规则"上踩过坑，所以这里逐条钉住。
 *
 * 假 DOM 只需要 uiCategory 用到的那些能力：createElement / className / classList
 * （含带 force 的 toggle）/ setAttribute / appendChild / addEventListener / click / hidden。
 */
"use strict";

const fs = require("fs");
const path = require("path");

const profilePath = path.resolve(
  process.argv[2] || path.join(__dirname, "..", "..", "src", "scholarscout", "desktop", "ui", "profile.js")
);
const SRC = fs.readFileSync(profilePath, "utf8");

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

function loadFunction(src, name) {
  return new Function("return (" + extractFunction(src, name) + ");")();
}

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
    setAttribute(key, value) {
      this.attrs[String(key)] = String(value);
    },
    getAttribute(key) {
      return Object.prototype.hasOwnProperty.call(this.attrs, String(key)) ? this.attrs[String(key)] : null;
    },
    appendChild(child) {
      this.children.push(child);
      child.parentNode = this;
      return child;
    },
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
    click() {
      const fns = this.listeners.click || [];
      if (!fns.length) throw new Error("这个节点上没有 click 监听（点了不会有反应）");
      fns.forEach((fn) => fn({ type: "click" }));
    },
  };
  node.classList = makeClassList(node);
  return node;
}

globalThis.document = {
  createElement: (tag) => makeElement(tag),
};

const uiCategory = loadFunction(SRC, "uiCategory");

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

function headOf(box) {
  return box.children.find((n) => String(n.className || "").indexOf("ui-cat-head") >= 0) || null;
}
function bodyOf(box) {
  return box.children.find((n) => String(n.className || "").indexOf("ui-cat-body") >= 0) || null;
}

/* ------------------------------------------------------------ ① 搭壳 */

check("uiCategory：返回 { box, panel } 两个节点，且 box 带 data-cat", () => {
  const made = uiCategory("model", "模型与生成", "对话与摘要模型", true);
  const box = made.box;
  return expect(
    [box.tagName, box.getAttribute("data-cat"), !!made.panel, made.panel.className],
    ["SECTION", "model", true, "ui-cat-body"],
    "容器"
  );
});

check("uiCategory：展开态带 is-open、aria-expanded=true、panel 不 hidden", () => {
  const made = uiCategory("model", "模型与生成", "hint", true);
  const head = headOf(made.box);
  return expect(
    [made.box.classList.contains("is-open"), head.getAttribute("aria-expanded"), made.panel.hidden],
    [true, "true", false],
    "展开态"
  );
});

check("uiCategory：收起态不带 is-open、aria-expanded=false、panel.hidden=true", () => {
  const made = uiCategory("memory", "跨会话记忆", "hint", false);
  const head = headOf(made.box);
  return expect(
    [made.box.classList.contains("is-open"), head.getAttribute("aria-expanded"), made.panel.hidden],
    [false, "false", true],
    "收起态"
  );
});

check("uiCategory：分组头是**真按钮**（type=button，不是 div）", () => {
  const made = uiCategory("x", "标题", "hint", true);
  const head = headOf(made.box);
  return expect([head.tagName, head.type], ["BUTTON", "button"], "分组头");
});

check("uiCategory：标题与说明都渲染出来（箭头 aria-hidden）", () => {
  const made = uiCategory("x", "标题文字", "说明文字", true);
  const head = headOf(made.box);
  const strong = head.children.find((n) => n.tagName === "STRONG");
  const caret = head.children.find((n) => String(n.className).indexOf("ui-cat-caret") >= 0);
  const hint = head.children.find((n) => String(n.className).indexOf("ui-cat-hint") >= 0);
  return expect(
    [strong && strong.textContent, caret && caret.getAttribute("aria-hidden"), hint && hint.textContent],
    ["标题文字", "true", "说明文字"],
    "头部内容"
  );
});

check("uiCategory：没有说明文字时不硬塞一个空的 hint 节点", () => {
  const made = uiCategory("x", "标题", "", true);
  const head = headOf(made.box);
  const hint = head.children.filter((n) => String(n.className).indexOf("ui-cat-hint") >= 0);
  return expect(hint.length, 0, "hint 节点数");
});

/* ------------------------------------------------------------ ② 点一下 = 切换 */

check("点分组头：收起 ↔ 展开双向都翻（类 / aria / hidden 三者同步）", () => {
  const made = uiCategory("model", "模型与生成", "hint", true);
  const head = headOf(made.box);
  const body = bodyOf(made.box);
  head.click();
  const closed = [
    made.box.classList.contains("is-open"),
    head.getAttribute("aria-expanded"),
    body.hidden,
  ];
  head.click();
  const open = [made.box.classList.contains("is-open"), head.getAttribute("aria-expanded"), body.hidden];
  return expect([closed, open], [[false, "false", true], [true, "true", false]], "两次点击");
});

check("默认收起的那一组，点一下就能展开（不是「点了没反应」）", () => {
  const made = uiCategory("stats", "学习统计", "hint", false);
  const head = headOf(made.box);
  head.click();
  return expect(
    [made.box.classList.contains("is-open"), head.getAttribute("aria-expanded"), made.panel.hidden],
    [true, "true", false],
    "展开"
  );
});

check("点 N 次之后状态与点击数对得上（不允许只翻一次）", () => {
  const made = uiCategory("x", "标题", "hint", true);
  const head = headOf(made.box);
  const seen = [];
  for (let i = 0; i < 4; i += 1) {
    head.click();
    seen.push(made.box.classList.contains("is-open"));
  }
  return expect(seen, [false, true, false, true], "四连击");
});

/* ------------------------------------------------------------ ③ 内容装进 panel */

check("内容必须 append 进 panel（装到 box 上就不受 hidden 管了）", () => {
  const made = uiCategory("model", "模型与生成", "hint", true);
  const card = makeElement("div");
  card.className = "set-group";
  made.panel.appendChild(card);
  return expect(
    [made.panel.children.length, made.box.children.indexOf(card)],
    [1, -1],
    "panel 子节点"
  );
});

process.stdout.write(JSON.stringify(cases));
