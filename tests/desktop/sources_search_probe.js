/* 资料库「搜索结果可见性」的**真源码**用例集（node，最小假 DOM，不需要浏览器）。
 *
 * 用法：node sources_search_probe.js <sources.js 路径>
 * 输出：一行 JSON —— [{name, ok, got}]，由 Python 侧断言（见 test_sources_search_visibility.py）。
 *
 * 为什么要跑真代码而不是读文本：缺陷 D4 是"搜了等于没搜" —— 50 条命中全躺在默认折叠的
 * 分组里。要证明修好了，必须真的走一遍 `loadSources(词)`：
 *   · 有搜索词时 `folded` 是不是 false（组的 class / 行的 class 都要看）；
 *   · 顶部汇总行是不是**只在搜索时**出现、文案对不对；
 *   · 搜索态点组头能不能折回去、重画之后还折不折（状态不许写死成"永远展开"）；
 *   · 0 命中是不是给了"没找到包含「词」的资料"+ 可点出口，而不是一片空白。
 *
 * 假 DOM 只需 loadSources / 组头 / 空结果行用到的能力：createElement、className、
 * textContent、appendChild（顺手串好 nextElementSibling，组头折叠才带得动行）、
 * innerHTML（清空用）、addEventListener/click、classList（带 force 的 toggle）。
 * 浏览器里真渲染的 display 由 `run/library_search_probe.py` 量。
 */
"use strict";

const fs = require("fs");
const path = require("path");

const srcPath = path.resolve(
  process.argv[2] ||
    path.join(__dirname, "..", "..", "src", "scholarscout", "desktop", "ui", "sources.js")
);
const SRC = fs.readFileSync(srcPath, "utf8");

/** 按名字切出一个顶层函数声明（花括号配平）。切不到就抛 —— 宁可红，也不要静默测了个空。 */
function extractFunction(src, name) {
  const at = src.indexOf("function " + name + "(");
  if (at < 0) throw new Error("源码里找不到 function " + name + "(");
  // `async function` 也必须连 async 一起切走，否则求值时报「await 不在 async 里」
  const start = src.slice(Math.max(0, at - 6), at) === "async " ? at - 6 : at;

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

const STATE_DECL = (function () {
  const m = SRC.match(/var sourcesFoldState\s*=\s*\{[^}]*\}\s*;/);
  if (!m) throw new Error("源码里找不到 var sourcesFoldState = {…};");
  return m[0];
})();

const PARTS = [
  "normalize",
  "loadSources",
  "sourcesIsFolded",
  "sourcesSetFolded",
  "sourceGroupHeader",
  "sourceGroupLabel",
  "sourceSummaryRow",
  "sourceEmptyRow",
  "detailActions",
].map((name) => extractFunction(SRC, name));

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
    title: "",
    value: "",
    children: [],
    attrs: {},
    listeners: {},
    nextElementSibling: null,
    get innerHTML() {
      return this._html || "";
    },
    set innerHTML(value) {
      this._html = String(value);
      // 真实的 `list.innerHTML = ""` 会连子节点一起扔掉 —— 假 DOM 也得扔，否则重画会叠加
      this.children = [];
    },
    setAttribute(key, value) {
      this.attrs[String(key)] = String(value);
    },
    getAttribute(key) {
      return Object.prototype.hasOwnProperty.call(this.attrs, String(key))
        ? this.attrs[String(key)]
        : null;
    },
    appendChild(child) {
      this.children.push(child);
      const n = this.children.length;
      // 真 DOM 里同一个父节点下的兄弟是串起来的：组头折叠要靠这条链带走后面的行
      if (n > 1) this.children[n - 2].nextElementSibling = child;
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

/* ------------------------------------------------------------ 桩桥 + 运行时 */

const LIST = makeElement("ul");
const SEARCH_INPUT = makeElement("input");
const DOC = {
  createElement: (tag) => makeElement(tag),
};

/** 3 个会话组，条数 3/2/1，共 6 条（组内条数不同才能验出组头报的是**本组**命中数）。 */
const GROUPS = [
  { key: "s1", title: "教育智能体入门调研", count: 3 },
  { key: "s2", title: "课程知识库与 RAG", count: 2 },
  { key: "s3", title: "指针与内存模型", count: 1 },
];
function items() {
  const rows = [];
  let n = 0;
  GROUPS.forEach((group) => {
    for (let k = 0; k < group.count; k += 1) {
      n += 1;
      rows.push({
        doc_id: "d" + n,
        source_id: "src-" + n,
        title_zh: group.title + " 资料 " + (k + 1),
        summary_short: "摘要",
        tier: 1,
        authority: 4.5,
        domain: "arxiv.org",
        url: "https://arxiv.org/abs/2501.0000" + n,
        session_id: group.key,
        session_title: group.title,
      });
    }
  });
  return rows;
}

/** 每次求值一份源码 → 等价于「进程刚启动」（内存里的状态表是空的）。 */
function freshRuntime() {
  LIST.innerHTML = "";
  LIST.children = [];
  const calls = [];
  const factory = new Function(
    "document",
    "el",
    "scout",
    "return (function () {\n" +
      STATE_DECL +
      "\n" +
      PARTS.join("\n") +
      "\nreturn {load: loadSources, isFolded: sourcesIsFolded, setFolded: sourcesSetFolded," +
      " header: sourceGroupHeader, label: sourceGroupLabel, summary: sourceSummaryRow," +
      " empty: sourceEmptyRow};\n})();"
  );
  const runtime = factory(
    DOC,
    (id) => (id === "source-list" ? LIST : id === "search" ? SEARCH_INPUT : null),
    {
      call: (method, params) => {
        calls.push({ method: method, params: params || {} });
        const q = String((params && params.query) || "").trim();
        if (!q) return Promise.resolve({ items: items(), total: items().length });
        if (q.indexOf("指针") < 0)
          return Promise.resolve({ items: [], total: 0, search_path: "fts" });
        return Promise.resolve({ items: items(), total: items().length, search_path: "fts" });
      },
    }
  );
  runtime.calls = calls;
  return runtime;
}

/* ------------------------------------------------------------ 断言小工具 */

const cases = [];
async function check(name, fn) {
  try {
    let detail = fn();
    if (detail && typeof detail.then === "function") detail = await detail;
    const ok = detail === true || detail === undefined;
    cases.push({ name: name, ok: ok, got: ok ? "" : String(detail) });
  } catch (error) {
    cases.push({
      name: name,
      ok: false,
      got: error && error.message ? error.message : String(error),
    });
  }
}
function expect(actual, expected, label) {
  const a = JSON.stringify(actual);
  const b = JSON.stringify(expected);
  return a === b ? true : `${label || "值"}：期望 ${b}，实际 ${a}`;
}
/** 列表快照：组头文案 / 折叠数 / 顶部汇总 / 空结果文案。 */
function snapshot() {
  const rows = LIST.children.filter((c) => String(c.className).indexOf("source-row") >= 0);
  const groups = LIST.children.filter((c) => String(c.className).indexOf("source-group") >= 0);
  const summary = LIST.children.find((c) => String(c.className).indexOf("source-summary") >= 0);
  const empty = LIST.children.find((c) => String(c.className).indexOf("source-empty") >= 0);
  return {
    total: LIST.children.length,
    rows: rows.length,
    foldedRows: rows.filter((r) => r.classList.contains("is-folded-row")).length,
    groups: groups.length,
    foldedGroups: groups.filter((g) => g.classList.contains("is-folded")).length,
    headers: groups.map((g) => g.children[0].textContent),
    summary: summary ? summary.textContent : null,
    summaryIsFirst:
      !!LIST.children[0] && String(LIST.children[0].className).indexOf("source-summary") >= 0,
    empty: empty ? empty.textContent : null,
    emptyText: empty ? collectText(empty) : null,
    clearButton: empty ? empty.children.find((c) => c.className.indexOf("source-empty-clear") >= 0) : null,
  };
}
function collectText(node) {
  let text = String(node.textContent || "");
  node.children.forEach((child) => {
    text += "|" + collectText(child);
  });
  return text;
}

const SEARCH_HEADERS = ["▾ 教育智能体入门调研（3）", "▾ 课程知识库与 RAG（2）", "▾ 指针与内存模型（1）"];
const PLAIN_HEADERS = ["▸ 教育智能体入门调研（3 条）", "▸ 课程知识库与 RAG（2 条）", "▸ 指针与内存模型（1 条）"];

async function run() {
  const SEARCH = "指针";
  const MISS = "量子";

  /* ------------------------------------------------ ① 有搜索词：命中分组默认展开 */
  const rt = freshRuntime();
  await rt.load(SEARCH);
  await check("搜索时命中分组默认展开（行/组都不带折叠态），组头报本组命中数", () => {
    const s = snapshot();
    return expect(
      [s.rows, s.foldedRows, s.foldedGroups, s.groups, s.headers],
      [6, 0, 0, 3, SEARCH_HEADERS],
      "搜索结果"
    );
  });
  await check("搜索时列表第一条是汇总行：共 6 条，分布在 3 组", () => {
    const s = snapshot();
    return expect([s.summaryIsFirst, s.summary], [true, "共 6 条，分布在 3 组"], "汇总行");
  });

  /* ------------------------------------------------ ② 搜索态点组头仍能折叠 */
  await check("搜索态点组头：该组折起来（组头 ▸ + 组内行同步），其余组仍展开", () => {
    const s0 = snapshot();
    const firstGroup = LIST.children.find((c) => String(c.className).indexOf("source-group") >= 0);
    firstGroup.children[0].click();
    const s = snapshot();
    return expect(
      [s0.foldedGroups, s.foldedGroups, s.foldedRows, s.headers[0], s.headers[1]],
      [0, 1, 3, "▸ 教育智能体入门调研（3）", "▾ 课程知识库与 RAG（2）"],
      "点一次后"
    );
  });

  /* ------------------------------------------------ ③ 重画不会把手动折叠写死回去 */
  await check("同一搜索词重画：手动折叠的那组仍折叠（状态没被写死成永远展开）", async () => {
    const rt2 = freshRuntime();
    await rt2.load(SEARCH);
    const firstGroup = LIST.children.find((c) => String(c.className).indexOf("source-group") >= 0);
    firstGroup.children[0].click();
    await rt2.load(SEARCH);
    const s = snapshot();
    return expect([s.foldedGroups, s.foldedRows], [1, 3], "重画后");
  });

  /* ------------------------------------------------ ④ 无搜索词：老习惯不变 */
  const rt3 = freshRuntime();
  await rt3.load("");
  await check("无搜索词：仍然默认折叠，且不画汇总行（老行为不被搜索改动带偏）", () => {
    const s = snapshot();
    return expect(
      [s.rows, s.foldedRows, s.groups, s.foldedGroups, s.summary, s.headers],
      [6, 6, 3, 3, null, PLAIN_HEADERS],
      "无搜索词"
    );
  });

  /* ------------------------------------------------ ⑤ 0 命中：说清 + 可操作 */
  const rt4 = freshRuntime();
  await rt4.load(MISS);
  await check("0 命中：给「没找到包含「量子」的资料」+ 换词建议 + 清空搜索按钮（不留空白）", () => {
    const s = snapshot();
    return expect(
      [
        s.rows,
        s.groups,
        s.summary,
        String(s.emptyText).indexOf("没找到包含「量子」的资料") >= 0,
        String(s.emptyText).indexOf("换个更短的词") >= 0,
        !!s.clearButton && s.clearButton.textContent === "清空搜索",
      ],
      [0, 0, null, true, true, true],
      "空结果"
    );
  });
  await check("点「清空搜索」：清掉输入框并回到全部资料（无搜索词 = 默认折叠）", async () => {
    const s0 = snapshot();
    SEARCH_INPUT.value = MISS;
    s0.clearButton.click();
    await new Promise((r) => setTimeout(r, 0));
    const s = snapshot();
    const lastCall = rt4.calls[rt4.calls.length - 1];
    return expect(
      [
        SEARCH_INPUT.value,
        "query" in (lastCall.params || {}),
        s.rows,
        s.foldedRows,
        s.summary,
      ],
      ["", false, 6, 6, null],
      "清空后"
    );
  });

  /* ------------------------------------------------ ⑥ 无搜索词时库是空的：保留原话 */
  await check("无搜索词且库是空的：保留原来那句提示（不冒充「没找到包含…」）", () => {
    const rt5 = freshRuntime();
    LIST.innerHTML = "";
    const node = rt5.empty("");
    return expect(
      [node.className, node.textContent],
      ["hint", "库里还没有资料。先在左边让它去搜一轮。"],
      "空库提示"
    );
  });

  /* ------------------------------------------------ ⑦ 折叠态仍然只在内存里 */
  await check("搜索态也不把折叠态写进 localStorage / sessionStorage", () => {
    const block = [STATE_DECL, extractFunction(SRC, "sourcesIsFolded"), extractFunction(SRC, "sourcesSetFolded")].join("\n");
    const banned = ["localStorage", "sessionStorage"].filter((name) => block.indexOf(name) >= 0);
    return expect(banned, [], "持久化调用");
  });

  process.stdout.write(JSON.stringify(cases));
}

run().catch((error) => {
  cases.push({ name: "用例集自身没跑起来", ok: false, got: String((error && error.stack) || error) });
  process.stdout.write(JSON.stringify(cases));
});
