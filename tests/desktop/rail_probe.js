/* 浮层「左侧表项 + 右侧内容」组件 `uiRail()` 的**真源码**用例集
 * （node 下用最小假 DOM 跑，不需要浏览器）。
 *
 * 用法：node rail_probe.js <profile.js 路径>
 * 输出：一行 JSON —— [{name, ok, got}]，由 Python 侧断言（见 test_ui_categories.py）。
 *
 * 为什么不 require 整个 profile.js：它是浏览器脚本（顶层用 document / scout / el），
 * require 进来会当场炸。这里按名字把 `uiRail` 的源码切出来放进 `new Function` 求值 ——
 * 测的仍是**产品文件里的那段源码**，不是复刻版（与 multisession_probe.js 同一手法）。
 *
 * 为什么要有它：切换表项是「点了右边要换屏」的交互，光读代码文本只能证明"写了 click 监听"，
 * 证明不了**点下去真的换了面板、选中态跟着走、而且没有把面板里的节点重建掉**（重建会丢
 * 用户正在填的输入框内容）。本项目在「类选择器上的 display 压掉 UA 的 [hidden]」上踩过坑，
 * 所以这里也钉住 pane 的 hidden 翻转。
 *
 * 假 DOM 只需要 uiRail 用到的那些能力：createElement / className / textContent / id /
 * hidden / setAttribute / getAttribute / appendChild / addEventListener（click + keydown）/
 * classList（含带 force 的 toggle）/ focus / dispatch。
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
    id: "",
    type: "",
    hidden: false,
    focused: false,
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
    focus() {
      this.focused = true;
    },
    dispatch(type, event) {
      const fns = this.listeners[type] || [];
      fns.forEach((fn) => fn(event || { type: type }));
      return fns.length;
    },
    click() {
      const n = this.dispatch("click", { type: "click" });
      if (!n) throw new Error("这个节点上没有 click 监听（点了不会有反应）");
    },
    keydown(key) {
      let prevented = false;
      const n = this.dispatch("keydown", {
        type: "keydown",
        key: key,
        preventDefault() {
          prevented = true;
        },
      });
      if (!n) throw new Error("这个节点上没有 keydown 监听（键盘走不通）");
      return prevented;
    },
  };
  node.classList = makeClassList(node);
  return node;
}

globalThis.document = {
  createElement: (tag) => makeElement(tag),
};

const uiRail = loadFunction(SRC, "uiRail");

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

const SPECS = [
  { name: "model", title: "模型与生成", hint: "对话与摘要模型" },
  { name: "channel", title: "渠道", hint: "把飞书接进来" },
  { name: "search", title: "检索与网络", hint: "搜索与预算" },
];
const names = () => SPECS.map((s) => s.name);

/* ------------------------------------------------------------ ① 结构 */

check("uiRail：nav 是 NAV.ui-rail，role=tablist 且竖向", () => {
  const shell = uiRail(SPECS);
  return expect(
    [shell.nav.tagName, shell.nav.className, shell.nav.getAttribute("role"), shell.nav.getAttribute("aria-orientation")],
    ["NAV", "ui-rail", "tablist", "vertical"],
    "左栏容器"
  );
});

check("uiRail：表项是**真按钮**（Tab 可达、Enter 原生触发），每项带 data-cat", () => {
  const shell = uiRail(SPECS);
  return expect(
    [
      shell.buttons.map((b) => b.tagName),
      shell.buttons.map((b) => b.type),
      shell.buttons.map((b) => b.getAttribute("data-cat")),
      shell.buttons.map((b) => b.getAttribute("role")),
    ],
    [["BUTTON", "BUTTON", "BUTTON"], ["button", "button", "button"], names(), ["tab", "tab", "tab"]],
    "表项"
  );
});

check("uiRail：右侧每项一个 section[role=tabpanel]，id = ui-pane-<name>，与 aria-controls 对得上", () => {
  const shell = uiRail(SPECS);
  const paneIds = shell.panes.children.map((p) => p.id);
  const controls = shell.buttons.map((b) => b.getAttribute("aria-controls"));
  return expect(
    [
      shell.panes.className,
      shell.panes.children.map((p) => p.tagName),
      shell.panes.children.map((p) => p.getAttribute("role")),
      paneIds,
      controls,
      shell.panes.children.map((p) => p.getAttribute("data-cat")),
    ],
    [
      "ui-panes",
      ["SECTION", "SECTION", "SECTION"],
      ["tabpanel", "tabpanel", "tabpanel"],
      ["ui-pane-model", "ui-pane-channel", "ui-pane-search"],
      ["ui-pane-model", "ui-pane-channel", "ui-pane-search"],
      names(),
    ],
    "右侧面板"
  );
});

check("uiRail：标题与说明都渲染出来；没有说明就不硬塞空节点", () => {
  const withHint = uiRail(SPECS);
  const noHint = uiRail([{ name: "a", title: "只有标题" }]);
  const hints = withHint.buttons[0].children.filter((n) => String(n.className).indexOf("ui-rail-hint") >= 0);
  const none = noHint.buttons[0].children.filter((n) => String(n.className).indexOf("ui-rail-hint") >= 0);
  return expect(
    [withHint.buttons[0].children[0].textContent, hints.length, hints[0].textContent, none.length],
    ["模型与生成", 1, "对话与摘要模型", 0],
    "头部内容"
  );
});

/* ------------------------------------------------------------ ② 默认选中第一项 */

check("默认选中第一项：只有第 1 项 is-active / aria-selected=true，且只有它的面板可见", () => {
  const shell = uiRail(SPECS);
  return expect(
    [
      shell.buttons.map((b) => b.classList.contains("is-active")),
      shell.buttons.map((b) => b.getAttribute("aria-selected")),
      shell.panes.children.map((p) => p.hidden),
      shell.panes.children.map((p) => p.classList.contains("is-active")),
    ],
    [[true, false, false], ["true", "false", "false"], [false, true, true], [true, false, false]],
    "默认态"
  );
});

check("默认选中第一项不是写死的第 0 个索引：换成别的顺序也选第一个", () => {
  const shell = uiRail([{ name: "z", title: "Z" }, { name: "a", title: "A" }]);
  return expect(
    [shell.buttons[0].classList.contains("is-active"), shell.panels.z.hidden, shell.panels.a.hidden],
    [true, false, true],
    "顺序无关"
  );
});

/* ------------------------------------------------------------ ③ 点表项 = 换屏 */

check("点第 2 项：选中态与前后的面板可见性整体翻转（不是「再加一个」）", () => {
  const shell = uiRail(SPECS);
  shell.buttons[1].click();
  return expect(
    [
      shell.buttons.map((b) => b.classList.contains("is-active")),
      shell.buttons.map((b) => b.getAttribute("aria-selected")),
      shell.panes.children.map((p) => p.hidden),
    ],
    [[false, true, false], ["false", "true", "false"], [true, false, true]],
    "点第 2 项"
  );
});

check("连点 3 项之后，每次只有被点的那一项可见（不存在「两项都开着」）", () => {
  const shell = uiRail(SPECS);
  const seen = [];
  [2, 0, 1].forEach((i) => {
    shell.buttons[i].click();
    seen.push([
      shell.buttons.map((b) => b.classList.contains("is-active")),
      shell.panes.children.map((p) => !p.hidden),
    ]);
  });
  return expect(seen, [
    [[false, false, true], [false, false, true]],
    [[true, false, false], [true, false, false]],
    [[false, true, false], [false, true, false]],
  ], "三次点击");
});

check("点同一项两次：状态不变（幂等，不会把自己关掉）", () => {
  const shell = uiRail(SPECS);
  shell.buttons[1].click();
  shell.buttons[1].click();
  return expect(
    [shell.buttons.map((b) => b.classList.contains("is-active")), shell.panes.children.map((p) => p.hidden)],
    [[false, true, false], [true, false, true]],
    "幂等"
  );
});

/* ------------------------------------------------------------ ④ 只切可见性，不重建 */

check("换屏不重建面板里的节点：切换前后是**同一批对象**（否则用户填的输入框会被清空）", () => {
  const shell = uiRail(SPECS);
  const card = makeElement("div");
  card.className = "set-group";
  card.textContent = "用户正在填的内容";
  shell.panels.channel.appendChild(card);
  const before = shell.panels.channel.children.slice();
  shell.buttons[1].click();
  shell.buttons[0].click();
  const after = shell.panels.channel.children.slice();
  return expect(
    [after.length === before.length && after.every((n, i) => n === before[i]), card.textContent, shell.panes.children.length],
    [true, "用户正在填的内容", 3],
    "节点身份"
  );
});

/* ------------------------------------------------------------ ⑤ 键盘 */

check("↓ 键：焦点与选中一起移到下一项（表项本身就可 Tab 到达，不需要额外入口）", () => {
  const shell = uiRail(SPECS);
  const prevented = shell.buttons[0].keydown("ArrowDown");
  return expect(
    [prevented, shell.buttons[1].focused, shell.buttons[1].classList.contains("is-active"), shell.panes.children[1].hidden],
    [true, true, true, false],
    "↓"
  );
});

check("↑ 键：从第一项往上走**绕回最后一项**（不许原地不动）", () => {
  const shell = uiRail(SPECS);
  const prevented = shell.buttons[0].keydown("ArrowUp");
  return expect(
    [prevented, shell.buttons[2].focused, shell.buttons[2].classList.contains("is-active")],
    [true, true, true],
    "↑ 环绕"
  );
});

check("Home / End：跳到第一项 / 最后一项", () => {
  const shell = uiRail(SPECS);
  shell.buttons[1].keydown("End");
  const atEnd = shell.buttons.map((b) => b.classList.contains("is-active"));
  shell.buttons[2].keydown("Home");
  const atHome = shell.buttons.map((b) => b.classList.contains("is-active"));
  return expect([atEnd, atHome], [[false, false, true], [true, false, false]], "Home/End");
});

check("不认识的方向键（如 Tab）不拦截默认行为，焦点由浏览器自己走", () => {
  const shell = uiRail(SPECS);
  const prevented = shell.buttons[0].keydown("Tab");
  return expect(
    [prevented, shell.buttons[0].classList.contains("is-active")],
    [false, true],
    "Tab 放行"
  );
});

process.stdout.write(JSON.stringify(cases));
