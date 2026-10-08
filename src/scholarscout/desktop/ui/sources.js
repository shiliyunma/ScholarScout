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
  };
}


async function loadSources(query) {
  const params = { limit: 50 };
  if (query) params.query = query;
  const payload = await scout.call("archive.list", params);
  const items = (payload.items || []).map(normalize);
  const list = el("source-list");
  list.innerHTML = "";
  if (!items.length) {
    list.innerHTML = '<li class="hint">库里还没有资料。先在左边让它去搜一轮。</li>';
    return;
  }
  items.forEach(function (item) {
    const li = document.createElement("li");
    li.className = "source-row";
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
}


async function showSource(docId) {
  const payload = await scout.call("archive.get", { doc_id: docId });
  const doc = payload.document || {};
  const summaries = payload.summaries || [];
  const first = summaries[0] || {};
  const points = safePoints(first.points_json);

  const box = el("source-detail");
  box.innerHTML = "";
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

  const body = document.createElement("div");
  body.textContent = first.summary_short || "（这条资料还没出摘要）";
  box.appendChild(body);

  if (points.length) {
    const ul = document.createElement("ul");
    points.forEach(function (point) {
      const li = document.createElement("li");
      li.textContent = point;
      ul.appendChild(li);
    });
    box.appendChild(ul);
  }

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
  if (hit && hit.doc_id) await showSource(hit.doc_id);
  else alert("这条资料还没有抓取正文（只有 source_id：" + sourceId + "）");
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
