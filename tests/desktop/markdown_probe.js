/* Markdown 解析器的用例集：在 node 下直接 require markdown.js 的纯函数部分（不碰 DOM）。
 *
 * 用法：node markdown_probe.js <markdown.js 的路径>
 * 输出：一行 JSON —— [{name, ok, got}]，由 Python 侧断言（见 test_markdown_js.py）。
 *
 * 为什么值得这么测：渲染器是"看着没问题、实际会静默出错"的典型 ——
 * 比如围栏里被当成正文解析、危险协议被放行、推理块没剥掉，界面都不会报错。
 */
"use strict";

const path = require("path");

const target = process.argv[2] || path.join(__dirname, "..", "..", "src", "scholarscout", "desktop", "ui", "markdown.js");
const md = require(path.resolve(target));

const cases = [];
function check(name, fn) {
  try {
    const detail = fn();
    cases.push({ name: name, ok: detail === true || detail === undefined, got: detail === true || detail === undefined ? "" : String(detail) });
  } catch (error) {
    cases.push({ name: name, ok: false, got: error && error.message ? error.message : String(error) });
  }
}
function expect(actual, expected, label) {
  const a = JSON.stringify(actual);
  const b = JSON.stringify(expected);
  return a === b ? true : `${label || "值"}:期望 ${b}，实际 ${a}`;
}

/* --------------------------------------------------------------- 代码围栏 */

check("围栏代码块：内部不做行内转换，原样保留", () => {
  const blocks = md.parseBlocks("```python\nprint('**not bold**')\n# <b>hi</b>\n```");
  if (blocks.length !== 1) return `块数应为 1，实际 ${blocks.length}`;
  if (blocks[0].type !== "code") return `类型应为 code，实际 ${blocks[0].type}`;
  if (blocks[0].lang !== "python") return `语言应为 python，实际 ${blocks[0].lang}`;
  return expect(blocks[0].code, "print('**not bold**')\n# <b>hi</b>", "代码内容");
});

check("没闭合的围栏也当代码块收尾（流式输出友好）", () => {
  const blocks = md.parseBlocks("说明\n```\npartial line");
  const last = blocks[blocks.length - 1];
  if (last.type !== "code") return `最后一块应为 code，实际 ${last.type}`;
  if (last.closed !== false) return "应标记 closed=false";
  return true;
});

check("~~~ 围栏也认", () => {
  const blocks = md.parseBlocks("~~~js\nlet a = 1;\n~~~");
  return blocks[0].type === "code" && blocks[0].lang === "js" ? true : "没认出 ~~~ 围栏";
});

/* ------------------------------------------------------------------ 块级 */

check("标题 1-6 级", () => {
  const blocks = md.parseBlocks("# 一级\n\n### 三级");
  return expect(
    blocks.map((b) => [b.type, b.level, b.text]),
    [["heading", 1, "一级"], ["heading", 3, "三级"]],
    "标题"
  );
});

check("表格：表头 / 对齐 / 数据行", () => {
  const blocks = md.parseBlocks("| 指标 | 值 | 说明 |\n|:---|---:|:---:|\n| A | 1 | x |\n| B | 2 | y |");
  const t = blocks[0];
  if (t.type !== "table") return `应为 table，实际 ${t.type}`;
  if (!expect(t.header, ["指标", "值", "说明"], "表头")) return `表头不对：${JSON.stringify(t.header)}`;
  if (!expect(t.align, ["left", "right", "center"], "对齐")) return `对齐不对：${JSON.stringify(t.align)}`;
  return expect(t.rows, [["A", "1", "x"], ["B", "2", "y"]], "数据行");
});

check("无序 + 有序 + 嵌套列表", () => {
  const blocks = md.parseBlocks("- 一\n- 二\n  - 二点一\n1. 甲\n2. 乙");
  const lists = blocks.filter((b) => b.type === "list");
  if (lists.length !== 2) return `应有 2 个列表块，实际 ${lists.length}`;
  const first = lists[0].items;
  if (first.length !== 3) return `第一个列表应有 3 项，实际 ${first.length}`;
  if (!first[2].indent > first[0].indent) return "嵌套项的 indent 应更大";
  if (lists[1].items[0].ordered !== true) return "第二个列表应识别为有序";
  return true;
});

check("提示块 > [!NOTE] 与普通引用区分开", () => {
  const note = md.parseBlocks("> [!WARNING]\n> 别这么干")[0];
  if (note.type !== "alert") return `应为 alert，实际 ${note.type}`;
  if (note.kind !== "WARNING") return `kind 应为 WARNING，实际 ${note.kind}`;
  if (!expect(note.text, "别这么干", "提示正文")) return `提示正文不对：${note.text}`;
  const quote = md.parseBlocks("> 只是引用")[0];
  return quote.type === "quote" ? true : `普通引用应为 quote，实际 ${quote.type}`;
});

check("分隔线与段落", () => {
  const blocks = md.parseBlocks("---\n\n正文一\n正文二");
  if (blocks[0].type !== "hr") return `第一块应为 hr，实际 ${blocks[0].type}`;
  const paragraph = blocks[1];
  if (paragraph.type !== "paragraph") return `第二块应为 paragraph，实际 ${paragraph.type}`;
  return paragraph.text === "正文一\n正文二" ? true : `段落内容被改写：${paragraph.text}`;
});

/* ------------------------------------------------------------------ 行内 */

function tokenTypes(text) {
  return md.parseInline(text).map((t) => t.type);
}

check("行内：代码 / 粗体 / 斜体 / 删除线", () => {
  return expect(
    tokenTypes("`code` **粗** *斜* ~~删~~ 普通"),
    ["code", "text", "strong", "text", "em", "text", "strike", "text"],
    "token 序列"
  );
});

check("链接只放行 http/https，伪协议降级成纯文本", () => {
  const safe = md.parseInline("[文档](https://example.org/a)")[0];
  if (safe.type !== "link" || safe.url !== "https://example.org/a") return `安全链接没解析对：${JSON.stringify(safe)}`;
  const evil = md.parseInline("[点我](javascript:alert(1))")[0];
  return evil.type === "text" ? true : `javascript: 伪协议竟被当成链接：${JSON.stringify(evil)}`;
});

check("裸 URL 自动链接，且剥掉句尾标点", () => {
  const tokens = md.parseInline("见 https://example.org/x, 还有 https://b.org/y。");
  const links = tokens.filter((t) => t.type === "link").map((t) => t.url);
  return expect(links, ["https://example.org/x", "https://b.org/y"], "自动链接");
});

check("URL 紧贴 ** 时不吃掉强调符", () => {
  const tokens = md.parseInline("**说明: https://example.org/x**");
  const strong = tokens.filter((t) => t.type === "strong");
  if (strong.length !== 1) return `应有一个 strong，实际 ${strong.length}：${JSON.stringify(tokens.map((t) => t.type))}`;
  return strong[0].text === "说明: https://example.org/x" ? true : `strong 内容不对：${strong[0].text}`;
});

check("图片降级成链接（界面要离线可用）", () => {
  const tokens = md.parseInline("![图](https://example.org/a.png)");
  return tokens[0].type === "image" ? true : `应识别为 image token，实际 ${tokens[0].type}`;
});

/* --------------------------------------------------------------- 安全性 */

check("HTML 标签只会是纯文本 token（不会被当 HTML 解析）", () => {
  const tokens = md.parseInline("<img src=x onerror=alert(1)>");
  if (tokens.length !== 1 || tokens[0].type !== "text") return `应只有一个 text token：${JSON.stringify(tokens)}`;
  return tokens[0].text.indexOf("<img") === 0 ? true : `文本内容不符：${tokens[0].text}`;
});

check("段落里夹的 script 标签同样是文本", () => {
  const blocks = md.parseBlocks('<script>alert("x")</script>');
  const paragraph = blocks[0];
  const tokens = paragraph.type === "paragraph" ? md.parseInline(paragraph.text) : [];
  if (!tokens.length) return `段落解析异常：${JSON.stringify(blocks)}`;
  return tokens.every((t) => t.type === "text") ? true : `出现了非文本 token：${JSON.stringify(tokens)}`;
});

check("推理块被剥掉（我们跑的是推理模型）", () => {
  const cleaned = md.stripReasoning("<thinking>内部推理</thinking>真正的回答");
  return cleaned.trim() === "真正的回答" ? true : `没剥干净：${JSON.stringify(cleaned)}`;
});

check("isSafeUrl 的边界", () => {
  const okList = ["https://a.b", "http://a.b/c?d=1"];
  const badList = ["javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "ftp://a.b", ""];
  for (const url of okList) {
    if (!md.isSafeUrl(url)) return `应放行：${url}`;
  }
  for (const url of badList) {
    if (md.isSafeUrl(url)) return `不应放行：${url}`;
  }
  return true;
});

/* ------------------------------------------------------------------ 输出 */

process.stdout.write(JSON.stringify(cases));
