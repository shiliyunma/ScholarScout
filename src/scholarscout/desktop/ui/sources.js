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
  // 按会话分组：同一会话的资料聚在一起，可折叠（用户要求不再一锅平铺）
  const groups = [];
  const seen = {};
  items.forEach(function (item) {
    const key = String(item.sessionId || "");
    if (seen[key] === undefined) {
      seen[key] = groups.length;
      groups.push({ title: item.sessionTitle || "（未记录会话）", items: [] });
    }
    groups[seen[key]].items.push(item);
  });
  groups.forEach(function (group) {
    list.appendChild(sourceGroupHeader(group));
    group.items.forEach(function (item) {
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
  });
}




/* 会话分组头：点一下折叠/展开（默认展开 —— 最近一次调研在最上面，先看的通常就是它） */
function sourceGroupHeader(group) {
  const li = document.createElement("li");
  li.className = "source-group";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "group-toggle";
  button.textContent = "▾ " + group.title + "（" + group.items.length + " 条）";
  button.title = "点一下折叠/展开这一组";
  button.addEventListener("click", function (event) {
    event.stopPropagation();
    const folded = li.classList.toggle("is-folded");
    button.textContent =
      (folded ? "▸ " : "▾ ") + group.title + "（" + group.items.length + " 条）";
    let node = li.nextElementSibling;
    while (node && node.classList.contains("source-row")) {
      // 用 class 而不是 hidden 属性：.source-row 自己带 display，CSS 的 display 会盖过 [hidden]，
      // 于是"点了三角没反应"—— 三角明明切换了状态，行却照样显示。
      node.classList.toggle("is-folded-row", folded);
      node = node.nextElementSibling;
    }
  });
  li.appendChild(button);
  return li;
}


async function showSource(docId) {
  const payload = await scout.call("archive.get", { doc_id: docId });
  const doc = payload.document || {};
  const summaries = payload.summaries || [];
  const first = summaries[0] || {};
  const points = safePoints(first.points_json);

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
