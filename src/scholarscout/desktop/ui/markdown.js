/*!
 * ScoutMarkdown —— 极小 Markdown 渲染器（无依赖、无构建、离线）
 *
 * 为什么自己写而不是像 Hermes 那样装库：Hermes 桌面端用的是 streamdown + shiki + katex +
 * dompurify（React + 构建步骤）。我们这套 UI 是**无构建的原生 JS**，而且 `--check-ui`
 * 明令禁止界面文件里出现外链。所以"照搬 Hermes"搬的是它的**设计决策**：
 *
 *   1. 围栏代码内部不做任何行内转换（Hermes 的 preprocess 也是先按围栏切分再分段处理）；
 *   2. 裸 URL 自动链接，且**不吞掉紧邻的强调符**（Hermes 的 RAW_URL_RE 特意排除了 `*`，
 *      因为"**说明: 一条链接**"里 URL 紧贴 `**` 是模型最常见的写法）；
 *   3. 表格、`> [!NOTE]` 提示块、代码块带语言标签与复制按钮；
 *   4. 先剥掉  thinking/<reasoning> 这类推理块（我们跑的是推理模型，Hermes 有同样的 REASONING_BLOCK_RE）；
 *   5. **安全靠构造**：模型文本永远只经 textContent 落地，绝不拼 innerHTML —— 不引入
 *      dompurify 也不会被注入（不信任的只有模型文本，而我们从不把它当 HTML 解析）。
 *
 * 只在浏览器里用；纯函数部分（parseBlocks / parseInline）可被 node 直接 require 做单测。
 */
var ScoutMarkdown = (function () {
  "use strict";

  var REASONING_RE = /<(think|thinking|reasoning|scratchpad|analysis)>[\s\S]*?<\/\1>\s*/gi;
  var FENCE_RE = /^[ \t]*(`{3,}|~{3,})[ \t]*([^\s`]*)[ \t]*$/;
  var HEADING_RE = /^(#{1,6})[ \t]+(.*)$/;
  var HR_RE = /^[ \t]*([-*_])[ \t]*(\1[ \t]*){2,}$/;
  var QUOTE_RE = /^[ \t]*>[ \t]?(.*)$/;
  var LIST_RE = /^([ \t]*)([-*+]|\d{1,9}[.)])[ \t]+(.*)$/;
  var TABLE_SEP_RE = /^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$/;
  var ALERT_RE = /^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*(.*)$/i;

  // 行内：代码 > 图片/链接 > 粗体 > 斜体 > 删除线 > 裸链接
  var INLINE_RE = new RegExp(
    [
      "(`[^`\\n]+`)", // 1 行内代码
      "(!?\\[[^\\]\\n]*\\]\\([^)\\s]+\\))", // 2 链接/图片
      "(\\*\\*[^*\\n]+\\*\\*)", // 3 粗体
      "(__[^_\\n]+__)", // 4 粗体
      "(~~[^~\\n]+~~)", // 5 删除线
      "(\\*[^*\\n]+\\*)", // 6 斜体
      "(_[^_\\n]+_)", // 7 斜体
      "(https?:\\/\\/[^\\s<>\"'`*]+[^\\s<>\"'`*.,;:!?。，、；：！？）】」』》])", // 8 裸链接
    ].join("|"),
    "g"
  );

  var ALERT_LABEL = {
    NOTE: "说明",
    TIP: "提示",
    IMPORTANT: "重要",
    WARNING: "注意",
    CAUTION: "当心",
  };

  /* ------------------------------------------------------------ 纯函数部分 */

  function stripReasoning(text) {
    return String(text == null ? "" : text).replace(REASONING_RE, "");
  }

  /** 只放行 http/https：拦 javascript:/data: 这类伪协议。 */
  function isSafeUrl(url) {
    return /^https?:\/\/[^\s]+$/i.test(String(url || "").trim());
  }

  function splitTableRow(line) {
    var trimmed = String(line).trim().replace(/^\|/, "").replace(/\|$/, "");
    return trimmed.split("|").map(function (cell) {
      return cell.trim();
    });
  }

  function parseAlignments(separatorLine) {
    return splitTableRow(separatorLine).map(function (cell) {
      var left = cell.charAt(0) === ":";
      var right = cell.charAt(cell.length - 1) === ":";
      if (left && right) return "center";
      if (right) return "right";
      if (left) return "left";
      return null;
    });
  }

  function isTableStart(lines, index) {
    if (index + 1 >= lines.length) return false;
    if (String(lines[index]).indexOf("|") < 0) return false;
    return TABLE_SEP_RE.test(lines[index + 1]);
  }

  /**
   * 切成块。返回纯数据（不碰 DOM）—— 这样能在 node 里直接单测。
   * 流式友好：没闭合的围栏按代码块收尾（正在输出的代码不应该被当正文解析）。
   */
  function parseBlocks(rawText) {
    var text = stripReasoning(rawText).replace(/\r\n?/g, "\n");
    var lines = text.split("\n");
    var blocks = [];
    var index = 0;

    function flushList(items) {
      if (items.length) blocks.push({ type: "list", items: items });
    }

    while (index < lines.length) {
      var line = lines[index];

      if (!line.trim()) {
        index += 1;
        continue;
      }

      var fence = FENCE_RE.exec(line);
      if (fence) {
        var marker = fence[1].charAt(0);
        var lang = (fence[2] || "").trim();
        var code = [];
        index += 1;
        var closed = false;
        while (index < lines.length) {
          var closing = FENCE_RE.exec(lines[index]);
          if (closing && closing[1].charAt(0) === marker) {
            closed = true;
            index += 1;
            break;
          }
          code.push(lines[index]);
          index += 1;
        }
        blocks.push({ type: "code", lang: lang, code: code.join("\n"), closed: closed });
        continue;
      }

      var heading = HEADING_RE.exec(line);
      if (heading) {
        blocks.push({ type: "heading", level: heading[1].length, text: heading[2].trim() });
        index += 1;
        continue;
      }

      if (HR_RE.test(line)) {
        blocks.push({ type: "hr" });
        index += 1;
        continue;
      }

      if (isTableStart(lines, index)) {
        var header = splitTableRow(lines[index]);
        var align = parseAlignments(lines[index + 1]);
        var rows = [];
        index += 2;
        while (index < lines.length && lines[index].indexOf("|") >= 0 && lines[index].trim()) {
          rows.push(splitTableRow(lines[index]));
          index += 1;
        }
        blocks.push({ type: "table", header: header, align: align, rows: rows });
        continue;
      }

      var quote = QUOTE_RE.exec(line);
      if (quote) {
        var quoted = [];
        while (index < lines.length) {
          var inner = QUOTE_RE.exec(lines[index]);
          if (!inner) break;
          quoted.push(inner[1]);
          index += 1;
        }
        var alert = ALERT_RE.exec(quoted.length ? quoted[0] : "");
        if (alert) {
          var first = alert[2];
          var rest = quoted.slice(1);
          if (first) rest.unshift(first);
          blocks.push({
            type: "alert",
            kind: alert[1].toUpperCase(),
            text: rest.join("\n").trim(),
          });
        } else {
          blocks.push({ type: "quote", text: quoted.join("\n") });
        }
        continue;
      }

      if (LIST_RE.test(line)) {
        var items = [];
        var baseOrdered = null;
        var baseIndent = 0;
        while (index < lines.length) {
          var item = LIST_RE.exec(lines[index]);
          if (item) {
            var indent = item[1].replace(/\t/g, "    ").length;
            var ordered = /\d/.test(item[2]);
            if (baseOrdered === null) {
              baseOrdered = ordered;
              baseIndent = indent;
            } else if (ordered !== baseOrdered && indent <= baseIndent) {
              // 同一层从无序换成有序（或反过来）= **另一个列表**，
              // 不是同一个列表里的项（不然有序列表会被画成圆点）
              break;
            }
            items.push({ indent: indent, ordered: ordered, marker: item[2], text: item[3] });
            index += 1;
            continue;
          }
          // 列表项里的续行（缩进对齐的普通文本）
          if (lines[index].trim() && items.length && /^[ \t]{2,}/.test(lines[index])) {
            items[items.length - 1].text += "\n" + lines[index].trim();
            index += 1;
            continue;
          }
          break;
        }
        flushList(items);
        continue;
      }

      var paragraph = [line.trim()];
      index += 1;
      while (
        index < lines.length &&
        lines[index].trim() &&
        !FENCE_RE.test(lines[index]) &&
        !HEADING_RE.test(lines[index]) &&
        !HR_RE.test(lines[index]) &&
        !QUOTE_RE.test(lines[index]) &&
        !LIST_RE.test(lines[index]) &&
        !isTableStart(lines, index)
      ) {
        paragraph.push(lines[index].trim());
        index += 1;
      }
      blocks.push({ type: "paragraph", text: paragraph.join("\n") });
    }
    return blocks;
  }

  /** 行内切成 token（纯数据）：{type:"text"|"code"|"strong"|"em"|"strike"|"link"|"image", ...} */
  function parseInline(text) {
    var tokens = [];
    var source = String(text == null ? "" : text);
    var last = 0;
    var match;
    INLINE_RE.lastIndex = 0;
    while ((match = INLINE_RE.exec(source)) !== null) {
      if (match.index > last) {
        tokens.push({ type: "text", text: source.slice(last, match.index) });
      }
      var raw = match[0];
      if (match[1]) {
        tokens.push({ type: "code", text: raw.slice(1, -1) });
      } else if (match[2]) {
        var m = /^(!?)\[([^\]]*)\]\(([^)\s]+)\)$/.exec(raw);
        if (m && isSafeUrl(m[3])) {
          tokens.push({ type: m[1] ? "image" : "link", text: m[2], url: m[3].trim() });
        } else {
          tokens.push({ type: "text", text: raw });
        }
      } else if (match[3] || match[4]) {
        tokens.push({ type: "strong", text: raw.slice(2, -2) });
      } else if (match[5]) {
        tokens.push({ type: "strike", text: raw.slice(2, -2) });
      } else if (match[6] || match[7]) {
        tokens.push({ type: "em", text: raw.slice(1, -1) });
      } else if (match[8]) {
        tokens.push({ type: "link", text: raw, url: raw });
      }
      last = match.index + raw.length;
    }
    if (last < source.length) {
      tokens.push({ type: "text", text: source.slice(last) });
    }
    return tokens;
  }

  /** 段落里的软换行按硬换行处理：模型常用来分行，压成空格会难读。 */
  function parseParagraph(text) {
    var parts = [];
    var lines = String(text).split("\n");
    lines.forEach(function (line, i) {
      if (i > 0) parts.push({ type: "break" });
      parts.push({ type: "inline", tokens: parseInline(line) });
    });
    return parts;
  }

  /* -------------------------------------------------------------- DOM 部分 */

  var dom = null;

  function doc() {
    if (dom) return dom;
    dom = typeof document !== "undefined" ? document : null;
    return dom;
  }

  function textNode(value) {
    return doc().createTextNode(String(value));
  }

  function renderInlineTokens(parent, tokens) {
    tokens.forEach(function (token) {
      var node;
      switch (token.type) {
        case "code":
          node = doc().createElement("code");
          node.className = "md-code";
          node.textContent = token.text;
          break;
        case "strong":
          node = doc().createElement("strong");
          appendInlineInto(node, token.text);
          break;
        case "em":
          node = doc().createElement("em");
          appendInlineInto(node, token.text);
          break;
        case "strike":
          node = doc().createElement("del");
          appendInlineInto(node, token.text);
          break;
        case "link":
          node = makeLink(token.url, token.text, token.text);
          break;
        case "image":
          // 界面要离线可用：图片一律降级成链接，点了交给系统浏览器
          node = makeLink(token.url, "[图片] " + (token.text || token.url), token.text);
          break;
        default:
          node = textNode(token.text);
      }
      parent.appendChild(node);
    });
  }

  function appendInlineInto(parent, text) {
    // 嵌套内容仍走 token 解析（**a `b` c** 这类混合写法不会丢格式）
    parseInline(text).forEach(function (token) {
      if (token.type === "text") {
        parent.appendChild(textNode(token.text));
        return;
      }
      renderInlineTokens(parent, [token]);
    });
  }

  function makeLink(url, label, title) {
    var a = doc().createElement("a");
    a.className = "md-link";
    a.href = url;
    a.textContent = label;
    if (title && title !== label) a.title = title;
    a.rel = "noreferrer noopener";
    // pywebview 里默认点击会把窗口顶掉（界面直接变成那个网页）→ 一律拦下来交给系统浏览器
    a.addEventListener("click", function (event) {
      event.preventDefault();
      openExternal(url);
    });
    return a;
  }

  function openExternal(url) {
    if (typeof window === "undefined") return;
    var bridge = window.pywebview && window.pywebview.api;
    if (bridge && typeof bridge.open_url === "function") {
      bridge.open_url(url);
      return;
    }
    if (typeof window.open === "function") window.open(url, "_blank");
  }

  function renderBlock(block) {
    var node;
    switch (block.type) {
      case "heading":
        node = doc().createElement("h" + block.level);
        node.className = "md-h";
        renderInlineTokens(node, parseInline(block.text));
        return node;
      case "code":
        return renderCode(block);
      case "hr":
        node = doc().createElement("hr");
        node.className = "md-hr";
        return node;
      case "alert":
        node = doc().createElement("div");
        node.className = "md-alert md-alert-" + block.kind.toLowerCase();
        var alertTitle = doc().createElement("div");
        alertTitle.className = "md-alert-title";
        alertTitle.textContent = ALERT_LABEL[block.kind] || block.kind;
        node.appendChild(alertTitle);
        parseParagraph(block.text).forEach(function (part) {
          if (part.type === "break") {
            node.appendChild(doc().createElement("br"));
            return;
          }
          var span = doc().createElement("span");
          renderInlineTokens(span, part.tokens);
          node.appendChild(span);
        });
        return node;
      case "quote":
        node = doc().createElement("blockquote");
        node.className = "md-quote";
        parseParagraph(block.text).forEach(function (part) {
          if (part.type === "break") {
            node.appendChild(doc().createElement("br"));
            return;
          }
          var span = doc().createElement("span");
          renderInlineTokens(span, part.tokens);
          node.appendChild(span);
        });
        return node;
      case "list":
        return renderList(block.items);
      case "table":
        return renderTable(block);
      default:
        node = doc().createElement("p");
        node.className = "md-p";
        parseParagraph(block.text).forEach(function (part) {
          if (part.type === "break") {
            node.appendChild(doc().createElement("br"));
            return;
          }
          renderInlineTokens(node, part.tokens);
        });
        return node;
    }
  }

  function renderList(items) {
    var firstOrdered = !!(items.length && items[0].ordered);
    var root = doc().createElement(firstOrdered ? "ol" : "ul");
    root.className = "md-list";
    if (!items.length) return root;

    /* 缩进 → 层级。sentinel 用**第一项的缩进**当第 0 层，
       否则第一项自己就会被当成"比根还深"而多套一层。 */
    var stack = [{ indent: items[0].indent, node: root }];

    items.forEach(function (item) {
      while (stack.length > 1 && item.indent < stack[stack.length - 1].indent) {
        stack.pop();
      }
      var top = stack[stack.length - 1];
      if (item.indent > top.indent) {
        var child = doc().createElement(item.ordered ? "ol" : "ul");
        child.className = "md-list";
        var lastLi = top.node.lastElementChild;
        (lastLi || top.node).appendChild(child);
        stack.push({ indent: item.indent, node: child });
        top = stack[stack.length - 1];
      }
      var li = doc().createElement("li");
      renderInlineTokens(li, parseInline(String(item.text).replace(/\n/g, " ")));
      top.node.appendChild(li);
    });
    return root;
  }

  function renderTable(block) {
    var wrap = doc().createElement("div");
    wrap.className = "md-table-wrap";
    var table = doc().createElement("table");
    table.className = "md-table";

    var thead = doc().createElement("thead");
    var headRow = doc().createElement("tr");
    block.header.forEach(function (cell, i) {
      var th = doc().createElement("th");
      if (block.align && block.align[i]) th.style.textAlign = block.align[i];
      renderInlineTokens(th, parseInline(cell));
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = doc().createElement("tbody");
    block.rows.forEach(function (row) {
      var tr = doc().createElement("tr");
      block.header.forEach(function (_cell, i) {
        var td = doc().createElement("td");
        if (block.align && block.align[i]) td.style.textAlign = block.align[i];
        renderInlineTokens(td, parseInline(row[i] == null ? "" : row[i]));
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  function renderCode(block) {
    var wrap = doc().createElement("div");
    wrap.className = "md-code-block";

    var head = doc().createElement("div");
    head.className = "md-code-head";
    var lang = doc().createElement("span");
    lang.className = "md-code-lang";
    lang.textContent = block.lang || "text";
    head.appendChild(lang);

    var copy = doc().createElement("button");
    copy.type = "button";
    copy.className = "md-code-copy";
    copy.textContent = "复制";
    copy.addEventListener("click", function () {
      copyCode(block.code, copy);
    });
    head.appendChild(copy);
    wrap.appendChild(head);

    var pre = doc().createElement("pre");
    pre.className = "md-pre";
    var code = doc().createElement("code");
    code.textContent = block.code;
    pre.appendChild(code);
    wrap.appendChild(pre);
    return wrap;
  }

  function copyCode(text, button) {
    function done(okLabel) {
      var original = button.textContent;
      button.textContent = okLabel;
      setTimeout(function () {
        button.textContent = original;
      }, 1200);
    }
    // file:// 下 navigator.clipboard 常常不可用 → 回退到选区 + execCommand
    if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () {
          done("已复制");
        },
        function () {
          done(fallbackCopy(text) ? "已复制" : "复制失败");
        }
      );
      return;
    }
    done(fallbackCopy(text) ? "已复制" : "复制失败");
  }

  function fallbackCopy(text) {
    try {
      var area = doc().createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      doc().body.appendChild(area);
      area.select();
      var ok = doc().execCommand("copy");
      doc().body.removeChild(area);
      return ok;
    } catch (error) {
      return false;
    }
  }

  /** 把 Markdown 渲染进指定元素（先清空）。 */
  function renderInto(target, text) {
    if (!target || !doc()) return target;
    target.textContent = "";
    target.classList.add("md-body");
    parseBlocks(text).forEach(function (block) {
      target.appendChild(renderBlock(block));
    });
    return target;
  }

  return {
    parseBlocks: parseBlocks,
    parseInline: parseInline,
    stripReasoning: stripReasoning,
    isSafeUrl: isSafeUrl,
    renderInto: renderInto,
    ALERT_LABEL: ALERT_LABEL,
  };
})();

/* node 下直接 require 做单测（浏览器里 module 未定义，这行不执行） */
if (typeof module !== "undefined" && module.exports) module.exports = ScoutMarkdown;
