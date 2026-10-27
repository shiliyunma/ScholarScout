/* 资料库：列表、摘要区（宽度可伸缩）、主题地图
   从 app.js 拆出来（原来 3246 行一个文件）。同名函数仍是全局声明，
   跨文件调用与拆分前完全一样 —— 不需要打包器，也不需要 import。 */

function normalize(item) {
  return {
    docId: item.doc_id,
    sourceId: item.source_id,
    title: item.title_zh || item.title || item.source_title || "(无标题)",
    text: item.summary_short || item.snippet || "",
    tier: item.tier,
    authority: item.authority,
    year: item.year,
    domain: item.domain,
    url: item.url,
    // 资料库按会话分组要用：所属会话与它的标题（后端 archive.list 已经给了，
    // 之前这里做白名单映射时把它们漏掉了 —— 结果 34 条全挤进"（未记录会话）"一组）
    sessionId: item.session_id || "",
    sessionTitle: item.session_title || "",
  };
}


async function loadSources(query) {
  // 有没有搜索词要贯穿整次画列表：命中分组默认展开、组头只报命中数、顶部加汇总行
  // —— 这三件事都只在"搜"的时候成立，不搜时保持原来的折叠习惯（用户要求启动即收起）。
  const searchText = String(query || "").trim();
  const searching = !!searchText;
  const params = { limit: 50 };
  if (searching) params.query = searchText;
  const payload = await scout.call("archive.list", params);
  const items = (payload.items || []).map(normalize);
  const list = el("source-list");
  list.innerHTML = "";
  if (!items.length) {
    // 0 命中：有搜索词时必须说清"搜了但没命中"并给下一步（换个词 / 清空），
    // 只留一片空白列表的话用户会以为搜索坏了。
    list.appendChild(sourceEmptyRow(searchText));
    return;
  }
  // 按会话分组：同一会话的资料聚在一起，可折叠（用户要求不再一锅平铺）
  const groups = [];
  const seen = {};
  items.forEach(function (item) {
    const key = String(item.sessionId || "");
    if (seen[key] === undefined) {
      seen[key] = groups.length;
      groups.push({ key: key, title: item.sessionTitle || "（未记录会话）", items: [] });
    }
    groups[seen[key]].items.push(item);
  });
  // 搜索时先给一行总览：命中数 + 分散在几组。否则屏幕上只有一排收起的分组头，
  // 看着跟"没搜到"一样（实测 50 条命中全躺在 13 个折叠组里）。
  if (searching) {
    list.appendChild(sourceSummaryRow(items.length, groups.length));
  }
  groups.forEach(function (group) {
    // 不搜时默认折叠（用户要求：每次启动列表都是收起的）；有搜索词时命中分组默认展开。
    // 用户在本会话内手动展开/折过的组，状态记在 sourcesFoldState 里 ——
    // 搜索时也以这张表为准（点过组头就按点过的来），只是"没记录过的"改成展开。
    const folded = sourcesIsFolded(group.key, searching);
    list.appendChild(sourceGroupHeader(group, folded, searching));
    group.items.forEach(function (item) {
      const li = document.createElement("li");
      li.className = "source-row" + (folded ? " is-folded-row" : "");
      // 标题不再按字符数截断（原来 short(title, 22) 会把长标题砍一半），
      // 改成占满剩余宽度、窄了才由 CSS 省略 —— 面板变宽它就跟着变宽。
      const title = document.createElement("span");
      title.className = "row-title";
      title.textContent = item.title || "(无标题)";
      title.title = item.title || "";
      li.appendChild(title);
      const meta = document.createElement("span");
      meta.className = "meta row-meta";
      meta.textContent =
        (item.domain || "") +
        "｜tier " +
        (item.tier === null || item.tier === undefined ? "未判权" : item.tier) +
        (payload.search_path ? "｜" + payload.search_path : "");
      li.appendChild(meta);
      // 「⋯」按用户要求跟着条目走（原来放在摘要区右上）：打开文件 / 在文件管理器定位 / 打开源网站
      li.appendChild(detailActions(item.docId, item));
      li.addEventListener("click", function () {
        showSource(item.docId);
      });
      list.appendChild(li);
    });
  });
}


/** 产物入库事件（`library.updated`）触发的「原地刷新」：只重拉列表数据、只换 `#source-list`。

    与 `loadSources()` 的差别只有两点，而这两点正是用户能不能看出抖动的地方：

    1. **沿着当前搜索词重查**（`#search` 里已有的字）—— 事件刷新不该偷偷清掉用户正在筛的条件；
    2. **记住/还原列表滚动位置** —— 用户正翻到中间时后端推来新产物，视线不该被打回顶部。

    折叠态本来就活在 `sourcesFoldState` 里（见下面 `loadSources` 的注释），重画不掉；
    页面其余部分（会话流、输入框、右侧摘要区）**一个节点都不碰** —— 这就是不整页重渲染的意思。
    没有 `#source-list`（界面还没建好）就直接掉头，不当成错误。 */
async function refreshSourcesInPlace() {
  const list = el("source-list");
  if (!list) return;
  const top = list.scrollTop;
  const query = el("search") ? el("search").value.trim() : "";
  await loadSources(query);
  // 列表在 await 期间可能被另一次刷新换成新节点 —— 重新取一次再还原滚动
  const after = el("source-list");
  if (after) after.scrollTop = top;
}




/* 资料库分组的折叠态（按会话组的 key 记）。
 *
 * 为什么要一个模块级的状态表，而不是把折叠态只写在 DOM 上：
 * `loadSources()` 每次搜索 / 刷新都会整块重画列表 —— 折叠态只存在 DOM 里的话，
 * 重画一次就把用户刚展开的那一组又折回去，看起来像"点了没用"。
 *
 * 为什么默认折叠：用户要求「每次启动列表都是收起的」。未记录过的 key 一律折算作折叠，
 * 而这张表只活在内存里（不落 localStorage）→ 进程重启后表是空的 ⇒ 全折叠；
 * 同一会话内用户手动展开过的组因为还在表里 ⇒ 保留。
 */
var sourcesFoldState = {};


/** 该组当前是否折叠。
 *
 * 两档默认值，共用一张手动状态表：
 *   · 没搜索词（平时）：**没记录过就折叠** —— 这条就是「启动默认折叠」的唯一判据；
 *   · 有搜索词（`expandedByDefault=true`）：**没记录过就展开** —— 50 条命中全躺在
 *     13 个折叠组里时，屏幕看着跟没搜到一样（实测缺陷 D4）。
 * 手动点过组头的组在两档里都以表为准，所以搜索时照样能点着折回去，状态不写死。
 */
function sourcesIsFolded(key, expandedByDefault) {
  if (expandedByDefault === true) return sourcesFoldState[key] === true;
  return sourcesFoldState[key] !== false;
}


function sourcesSetFolded(key, folded) {
  sourcesFoldState[key] = !!folded;
  return sourcesFoldState[key];
}


/* 会话分组头：点一下折叠/展开（平时默认折叠；有搜索词时命中分组默认展开） */
function sourceGroupHeader(group, folded, searching) {
  const li = document.createElement("li");
  li.className = "source-group" + (folded ? " is-folded" : "");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "group-toggle";
  button.textContent = sourceGroupLabel(group, folded, searching);
  button.title = "点一下折叠/展开这一组";
  button.setAttribute("aria-expanded", folded ? "false" : "true");
  button.addEventListener("click", function (event) {
    event.stopPropagation();
    const next = !li.classList.contains("is-folded");
    li.classList.toggle("is-folded", next);
    // 记进状态表：列表重画之后这一组仍保持用户选的态
    sourcesSetFolded(group.key, next);
    button.setAttribute("aria-expanded", next ? "false" : "true");
    button.textContent = sourceGroupLabel(group, next, searching);
    let node = li.nextElementSibling;
    while (node && node.classList.contains("source-row")) {
      // 用 class 而不是 hidden 属性：.source-row 自己带 display，CSS 的 display 会盖过 [hidden]，
      // 于是"点了三角没反应"—— 三角明明切换了状态，行却照样显示。
      node.classList.toggle("is-folded-row", next);
      node = node.nextElementSibling;
    }
  });
  li.appendChild(button);
  return li;
}


/** 组头文案。搜索时括号里只报**命中数**（`RAG 综述（3）`）；
 *  不搜时维持原来的「（N 条）」—— 那时括号表达的是"这一组攒了多少资料"。 */
function sourceGroupLabel(group, folded, searching) {
  const mark = folded ? "▸ " : "▾ ";
  const count = searching ? "（" + group.items.length + "）" : "（" + group.items.length + " 条）";
  return mark + group.title + count;
}


/** 列表顶部那行汇总：搜索命中多少条、散在多少个会话组里（`共 50 条，分布在 13 组`）。
 *  没有它的话用户只看到一排收起的三角，判断不了"到底搜到没有"。 */
function sourceSummaryRow(hits, groupCount) {
  const li = document.createElement("li");
  li.className = "source-summary";
  li.textContent = "共 " + hits + " 条，分布在 " + groupCount + " 组";
  return li;
}


/** 0 命中的列表内容：**说清搜了没命中** + 给两个可操作出口（换词 / 清空搜索）。
 *  有搜索词时绝不返回空列表 —— 一片空白会让人以为搜索功能坏了（缺陷 D4）。 */
function sourceEmptyRow(searchText) {
  const li = document.createElement("li");
  if (!searchText) {
    li.className = "hint";
    li.textContent = "库里还没有资料。先在左边让它去搜一轮。";
    return li;
  }
  li.className = "hint source-empty";
  const headline = document.createElement("div");
  headline.className = "source-empty-title";
  headline.textContent = "没找到包含「" + searchText + "」的资料";
  li.appendChild(headline);
  const tip = document.createElement("div");
  tip.className = "source-empty-tip";
  tip.textContent = "换个更短的词试试（比如只留核心术语），或者直接看全部资料。";
  li.appendChild(tip);
  const clear = document.createElement("button");
  clear.type = "button";
  clear.className = "mini source-empty-clear";
  clear.textContent = "清空搜索";
  clear.title = "清掉搜索框里的词，重新列出全部资料";
  clear.addEventListener("click", function () {
    const input = el("search");
    if (input) input.value = "";
    loadSources("");
  });
  li.appendChild(clear);
  return li;
}


/* 资料正文：摘要段 + 要点列表 + 「为什么相关」—— **资料库详情与知识地图浮层共用这一份**。
 *
 * 为什么必须共用：这两处原来各写一份渲染器 —— 资料库那份有要点，地图浮层那份只有
 * `summary_short`，于是同一条资料在两处"长短不一"，看着像浮层把摘要截了。
 * 库里本来就没有更长的摘要字段（`summaries` 只有 summary_short / points_json /
 * why_relevant），所以"复用资料库的摘要"= 复用这一份渲染（含要点），
 * 而不是去找一个不存在的长字段。
 *
 * 只负责**正文**，返回 DocumentFragment，挂到哪由调用方决定；标题、元数据、按钮、
 * 原件路径这些"外壳"仍归各自视图（它们本来就该不同）。
 *
 * `opts.emptyText` 可换掉"还没出摘要"那句提示，其余交给同一份排版。
 */
function renderSourceBody(summary, opts) {
  const record = summary || {};
  const options = opts || {};
  const frag = document.createDocumentFragment();

  // 摘要：整段照原样渲染。**不许截断**（不 slice / 不 short）——
  // 资料库详情与浮层都靠外层容器自己滚，砍字只会让"摘要太短"再回来一次。
  const paragraph = document.createElement("p");
  paragraph.className = "source-body-text";
  const summaryText = String(record.summary_short || "").trim();
  if (summaryText) {
    paragraph.textContent = summaryText;
  } else {
    paragraph.textContent = options.emptyText || "（这条资料还没出摘要）";
    paragraph.classList.add("is-empty");
  }
  frag.appendChild(paragraph);

  // 要点：库里 points_json 存的是 JSON 数组字符串，坏了就当没有（safePoints）
  const points = safePoints(record.points_json);
  if (points.length) {
    const heading = document.createElement("div");
    heading.className = "source-body-points-title";
    heading.textContent = "要点";
    frag.appendChild(heading);
    const list = document.createElement("ul");
    list.className = "source-body-points";
    points.forEach(function (point) {
      const li = document.createElement("li");
      li.textContent = point;
      list.appendChild(li);
    });
    frag.appendChild(list);
  }

  // 「为什么相关」：AI 判权时的理由。有就整行显示，没有就不占高度。
  const why = String(record.why_relevant || "").trim();
  if (why) {
    const line = document.createElement("div");
    line.className = "source-body-why";
    const label = document.createElement("b");
    label.textContent = "为什么相关：";
    line.appendChild(label);
    line.appendChild(document.createTextNode(why));
    frag.appendChild(line);
  }
  return frag;
}


async function showSource(docId) {
  const payload = await scout.call("archive.get", { doc_id: docId });
  const doc = payload.document || {};
  const summaries = payload.summaries || [];
  const first = summaries[0] || {};

  const box = el("source-detail");
  box.innerHTML = "";
  attachDetailClose(box, "从列表里点一条，看它的摘要与详情。");
  // 下载状态行挂在**详情区之外**（id 在 index.html 静态声明，见那里的注释）：
  // 详情区重画 / 「关闭」都会清空 innerHTML，挂在里面就会连 id 一起消失。
  // 这里只清掉上一条资料的下载提示，避免串台。
  const dlStatusLine = el("source-download-status");
  if (dlStatusLine) {
    dlStatusLine.textContent = "";
    dlStatusLine.hidden = true;
  }
  // 「⋯」按用户要求挪到列表条目那一行去了（跟着条目走，这里不再重复）
  const title = document.createElement("h3");
  title.textContent = first.title_zh || doc.title || "(无标题)";
  box.appendChild(title);

  const meta = document.createElement("div");
  meta.className = "kv";
  meta.textContent =
    (doc.domain || "") +
    "｜tier " +
    (doc.tier === null || doc.tier === undefined ? "未判权" : doc.tier) +
    "｜authority " +
    (doc.authority === null || doc.authority === undefined ? "-" : doc.authority) +
    "｜" +
    (doc.extract_status || "") +
    (doc.truncated ? "｜超长已截断" : "");
  box.appendChild(meta);

  // 两个出口（docs/19 §8.4）：「去原站」用 sources.url 走系统浏览器新窗口打开；
  // 「下载」调 archive.download（后端默认关；大文件后端先拒绝、这里二次确认后带 force 重试）。
  const actions = document.createElement("div");
  actions.className = "actions";
  const openBtn = document.createElement("button");
  openBtn.type = "button";
  openBtn.className = "ghost";
  openBtn.id = "btn-source-open-origin";
  openBtn.textContent = "去原站";
  openBtn.title = "用系统浏览器打开资料的原始网址（新窗口）";
  openBtn.addEventListener("click", function () {
    openExternalUrl(doc.url);
  });
  const downloadBtn = document.createElement("button");
  downloadBtn.type = "button";
  downloadBtn.className = "ghost";
  downloadBtn.id = "btn-source-download";
  downloadBtn.textContent = "下载";
  downloadBtn.title = "把原件下载到本地 data/raw/（大文件需二次确认）";
  downloadBtn.addEventListener("click", function () {
    downloadSource(downloadBtn, docId, doc);
  });
  actions.appendChild(openBtn);
  actions.appendChild(downloadBtn);
  box.appendChild(actions);

  // 正文（摘要 + 要点 + 为什么相关）：与地图浮层**共用** renderSourceBody，
  // 一份渲染两处用 —— 两处各写一份就是这次"浮层摘要太短"的根源。
  box.appendChild(renderSourceBody(first));

  const paths = await scout.call("archive.paths", { doc_id: docId });
  const pathInfo = document.createElement("div");
  pathInfo.className = "kv";
  pathInfo.textContent =
    "原件 " + (paths.exists && paths.exists.raw_path ? "√ " : "× ") + paths.raw_path +
    "\n全文 " + (paths.exists && paths.exists.text_path ? "√ " : "× ") + paths.text_path;
  box.appendChild(pathInfo);
}

/* 摘要区右上角的「⋯」：三个打开方式收在同一个按钮里。
   放在**顶部**而不是末尾 —— 放末尾时内容一长就得滚动才看得见，等于没有这个按钮。 */


/* 「下载」按钮：调 archive.download。
   后端默认关（features.enable_download=false）时会回 ok:false + reason，这里原样报出来；
   大文件后端先拒绝（large:true）→ 二次确认后再带 force:true 重试；
   成功显示落盘的可读路径 + 字节数，失败如实报原因（绝不假装成功）。 */
async function downloadSource(button, docId, doc) {
  const status = el("source-download-status");
  const show = function (text) {
    if (!status) return;
    status.textContent = text;
    status.hidden = !text; // 空闲时收起，不占高度（.hint 有上下 8px 外边距）
  };
  const params = {
    doc_id: docId,
    source_id: (doc && doc.source_id) || undefined,
    url: (doc && doc.url) || undefined,
  };
  button.disabled = true;
  show("下载中…");
  try {
    const result = await scout.call("archive.download", params);
    if (result && result.ok) {
      show((result.already ? "本地已有（未重复下载）：" : "已下载：") + result.path +
        "（" + fmtBytes(result.bytes) + "）");
      return;
    }
    if (result && result.large) {
      const limit = fmtBytes(result.threshold_bytes);
      if (!window.confirm(
        "这个文件不小（" + fmtBytes(result.bytes) + "，超过抽取阈值 " + limit + "）。仍要下载到本地吗？"
      )) {
        show("已取消（文件超过 " + limit + " 阈值）");
        return;
      }
      const forced = await scout.call("archive.download", Object.assign({}, params, { force: true }));
      if (forced && forced.ok) {
        show("已下载：" + forced.path + "（" + fmtBytes(forced.bytes) + "）");
      } else {
        show("下载失败：" + ((forced && forced.reason) || "未知原因"));
      }
      return;
    }
    show("下载失败：" + ((result && result.reason) || "未知原因"));
  } catch (error) {
    show("下载失败：" + ((error && error.message) || String(error)));
  } finally {
    button.disabled = false;
  }
}


function detailActions(docId, doc) {
  const actions = document.createElement("div");
  actions.className = "detail-actions";
  const menuButton = document.createElement("button");
  menuButton.type = "button";
  menuButton.className = "mini";
  menuButton.textContent = "⋯";
  menuButton.title = "打开文件 / 在文件管理器定位 / 打开源网站";
  menuButton.addEventListener("click", function (event) {
    event.stopPropagation();
    openFloatingMenu(menuButton, [
      ["打开文件", function () {
        scout.call("archive.open_raw", { doc_id: docId });
      }],
      ["在文件管理器中定位", function () {
        scout.call("archive.reveal", { doc_id: docId, which: "raw" });
      }],
      ["打开源网站", function () {
        openExternalUrl(doc.url);
      }],
    ]);
  });
  actions.appendChild(menuButton);
  return actions;
}


async function showSourceBySourceId(sourceId) {
  // 定向查这一条（原来拉 200 条回来再 find，库一大就是白扫）
  const payload = await scout.call("archive.list", { source_id: sourceId, limit: 1 });
  const hit = (payload.items || [])[0];
  if (hit && hit.doc_id) {
    await showSource(hit.doc_id);
    return true;
  }
  // 没有正文时**不弹 alert**（打断式弹窗把页签切换和地图详情一起挡住，用户只觉得"点了没反应"）：
  // 写进详情区下面那行提示（#source-download-status 就是为这件事静态声明的）。
  // 回 false 给调用方（地图的 openMapSource 据此决定要不要滚详情区）；忽略返回值的调用方不受影响。
  const status = el("source-download-status");
  if (status) {
    status.textContent = "这条资料还没有正文（只有 source_id：" + sourceId + "），可在地图里点「获取正文」";
    status.className = "hint set-bad";
    status.hidden = false;
  }
  return false;
}


function safePoints(raw) {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch (error) {
    return [];
  }
}


function showTopic(payload) {
  const graph = payload.graph || {};
  const nodes = graph.nodes || [];
  const edges = graph.edges || [];
  const stats = graph.stats || {};
  const box = el("source-detail");
  box.innerHTML = "";
  attachDetailClose(box, "从列表里点一条，看它的摘要与详情。");
  // 切到主题地图也把上一次的下载提示清掉（状态行在详情区之外，不会被这次重画清掉）
  const topicStatus = el("source-download-status");
  if (topicStatus) {
    topicStatus.textContent = "";
    topicStatus.hidden = true;
  }
  const title = document.createElement("h3");
  title.textContent = "主题地图：" + (payload.topic || "");
  box.appendChild(title);
  const meta = document.createElement("div");
  meta.className = "kv";
  meta.textContent =
    "节点 " + nodes.length + "｜边 " + edges.length +
    "｜无出处节点 " + (stats.ungrounded_nodes === undefined ? "-" : stats.ungrounded_nodes) +
    "｜生成方式 " + (graph.mode || "-");
  box.appendChild(meta);
  nodes.forEach(function (node) {
    const line = document.createElement("div");
    line.className = "kv";
    line.textContent =
      "· " + node.label + " [" + node.kind + "] " +
      ((node.source_ids || []).join(", ") || "无出处");
    box.appendChild(line);
  });
  edges.forEach(function (edge) {
    const line = document.createElement("div");
    line.className = "kv";
    line.textContent = "→ " + edge.src + " " + edge.rel + " " + edge.dst;
    box.appendChild(line);
  });
}


function applySourceHeight(pixels) {
  var detail = el("source-detail");
  if (!detail) return;
  // 用 flex-basis 而不是 height：这个区域在弹性列里，height 会被 flex 规则盖掉
  if (!pixels) {
    detail.style.flex = "";
    detail.style.maxHeight = "";
    return;
  }
  detail.style.flex = "0 0 " + pixels + "px";
  detail.style.maxHeight = "none";
}


function wireHeightSplitter(splitterId, targetId, key) {
  var bar = el(splitterId);
  var target = el(targetId);
  if (!bar || !target) return;
  var saved = null;
  try {
    saved = parseInt(localStorage.getItem(key) || "", 10);
  } catch (error) {
    saved = null;
  }
  if (saved && saved > 0) applySourceHeight(saved);

  var dragging = false;
  bar.addEventListener("mousedown", function (event) {
    dragging = true;
    event.preventDefault();
    document.body.style.cursor = "row-resize";
  });
  window.addEventListener("mousemove", function (event) {
    if (!dragging) return;
    var pane = target.parentElement || target;
    var box = pane.getBoundingClientRect();
    // 用"离底部的距离"当高度：往下拖＝摘要区变小
    var height = Math.round(box.bottom - event.clientY);
    var limit = Math.max(120, Math.round(box.height - 120));
    applySourceHeight(Math.min(Math.max(height, 120), limit));
  });
  window.addEventListener("mouseup", function () {
    if (!dragging) return;
    dragging = false;
    document.body.style.cursor = "";
    try {
      var height = parseInt((target.style.flex || "").replace(/[^0-9]/g, "") || "0", 10);
      if (height > 0) localStorage.setItem(key, String(height));
      else localStorage.removeItem(key);
    } catch (error) {
      /* 隐私模式：只当次生效 */
    }
  });
  bar.addEventListener("dblclick", function () {
    applySourceHeight(null);
    try {
      localStorage.removeItem(key);
    } catch (error) {
      /* 忽略 */
    }
  });
}

/* ------------------------------------------------------------------ 启动 */
