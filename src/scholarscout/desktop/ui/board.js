/* 研究看板（证据/缺口/时间线）、学习者画像、跨会话记忆
   从 app.js 拆出来（原来 3246 行一个文件）。同名函数仍是全局声明，
   跨文件调用与拆分前完全一样 —— 不需要打包器，也不需要 import。 */

async function loadBoard() {
  const summary = el("board-summary");
  if (!summary) return;
  if (!state.sessionId) {
    summary.textContent = "";
    return;
  }
  let board;
  try {
    board = await scout.call("research.board", { session_id: state.sessionId });
  } catch (err) {
    summary.textContent = "看板读取失败：" + (err && err.message ? err.message : String(err));
    return;
  }
  renderBoardSummary(board);
  renderBoardQuestions(board);
  renderBoardGaps(board);
  renderBoardEvidence(board);
  loadProfile();
  loadMemory();
}


async function loadMemory() {
  const list = el("memory-list");
  if (!list) return;
  let payload;
  try {
    payload = await scout.call("memory.list", {});
  } catch (error) {
    list.innerHTML = '<li class="hint">记忆读取失败</li>';
    return;
  }
  const items = payload.items || [];
  el("memory-note").textContent = items.length
    ? "（" + items.length + " 条；每条都能删，误抽不会一直污染下去）"
    : "（还没有记忆。攒够几轮对话后会自动整理一次，也可以点「从现在整理」）";
  list.innerHTML = "";
  items.forEach(function (item) {
    const labels = { preference: "偏好", fact: "背景", goal: "目标" };
    const li = boardItem(
      '<span class="badge">' +
        esc(labels[item.kind] || item.kind) +
        "</span> " +
        esc(item.text),
    );
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "ghost board-mini";
    remove.textContent = "删除";
    remove.addEventListener("click", async function (event) {
      event.stopPropagation();
      if (!window.confirm("删掉这条记忆？\n\n" + item.text)) return;
      await scout.call("memory.remove", { fact_id: item.id });
      loadMemory();
    });
    li.appendChild(remove);
    list.appendChild(li);
  });
}


async function addMemory() {
  const input = el("memory-input");
  const text = (input.value || "").trim();
  if (!text) return;
  try {
    const result = await scout.call("memory.add", { text: text, kind: "fact" });
    input.value = "";
    if (!result.created) {
      window.alert("已经有一条相近的记忆了，没重复添加。");
    }
    loadMemory();
  } catch (error) {
    window.alert("没记住：" + (error && error.message ? error.message : String(error)));
  }
}


async function extractMemory() {
  if (!state.sessionId) {
    window.alert("先打开一个会话。");
    return;
  }
  const button = el("btn-memory-extract");
  button.disabled = true;
  button.textContent = "整理中…";
  try {
    const result = await scout.call("memory.extract", { session_id: state.sessionId });
    const added = (result.added || []).length;
    const skipped = (result.skipped || []).length;
    window.alert(
      result.reason
        ? "这次没整理出东西：" + result.reason
        : "新增 " +
            added +
            " 条记忆" +
            (skipped ? "，跳过 " + skipped + " 条（已有相近的）" : "") +
            "。\n\n空表也是正常结果 —— 没有值得长期记住的东西就不该记。",
    );
    loadMemory();
  } catch (error) {
    window.alert("整理失败：" + (error && error.message ? error.message : String(error)));
  } finally {
    button.disabled = false;
    button.textContent = "从现在整理";
  }
}


async function loadProfile() {
  const overview = el("profile-overview");
  if (!overview) return;
  let profile;
  try {
    profile = await scout.call("learner.profile", {});
  } catch (error) {
    overview.textContent = "画像读取失败：" + (error && error.message ? error.message : String(error));
    return;
  }
  const data = profile.overview || {};
  const bits = [];
  if (data.topics) bits.push("做过 " + data.topics + " 个主题");
  if (data.routes) bits.push("路线 " + data.routes + " 条");
  if (data.maps) bits.push("地图 " + data.maps + " 份");
  if (data.reviewed) {
    bits.push(
      "练过 " +
        data.reviewed +
        "/" +
        data.cards +
        " 张" +
        (data.mastery_rate === null ? "" : "（熟 " + Math.round(data.mastery_rate * 100) + "%）"),
    );
  }
  if (data.active_days_recent) bits.push("近 " + data.window_days + " 天学了 " + data.active_days_recent + " 天");
  overview.textContent = bits.length
    ? bits.join("｜") + "。这份画像会随每轮请求注入，所以产物按你的水平来。"
    : "还没有学习痕迹 —— 做完一份地图/路线、或练几张抽背卡，这里才有东西可汇总。";
  el("profile-note").textContent = bits.length ? "（零模型调用，从库里的排期与产物算出来）" : "";

  const topics = el("profile-topics");
  topics.innerHTML = "";
  (profile.topics || []).forEach(function (item) {
    topics.appendChild(
      boardItem(
        esc(item.topic) +
          '<span class="meta">路线 ' +
          item.routes +
          " ／ 地图 " +
          item.maps +
          (item.weeks ? " ／ 最长 " + item.weeks + " 周" : "") +
          "</span>",
      ),
    );
  });

  const groups = el("profile-groups");
  groups.innerHTML = "";
  (profile.card_groups || []).forEach(function (item) {
    const rate = item.mastery_rate === null ? "—" : Math.round(item.mastery_rate * 100) + "%";
    groups.appendChild(
      boardItem(
        esc(item.group) +
          ' <span class="badge ' +
          (item.weak ? "badge-weak" : "badge-strong") +
          '">熟 ' +
          rate +
          "</span>" +
          '<span class="meta">熟 ' +
          item.mastered +
          " ／ 复习中 " +
          item.reviewing +
          " ／ 薄弱 " +
          item.weak +
          (item.cards - item.reviewed ? " ／ 未练 " + (item.cards - item.reviewed) : "") +
          // ① 类型化掌握度（口径在 learning/mastery.py，纯函数可复算）
          " ｜ 掌握度 " +
          item.level +
          "（" +
          item.knowledge_type +
          " 门槛 " +
          item.gate +
          "，达标 " +
          item.above_gate +
          "）" +
          (item.next ? "｜下一个：" + item.next.title : "") +
          "</span>",
      ),
    );
  });
}


function renderBoardSummary(board) {
  const counts = board.counts || {};
  const evidence = board.evidence || {};
  el("board-summary").textContent = [
    "来源 " + (counts.sources || 0),
    "有摘要 " + (counts.with_summary || 0),
    "未取正文 " + (counts.without_text || 0),
    "已搜查询 " + (counts.queries || 0),
    "未覆盖子问题 " + (counts.open_questions || 0),
    "弱证据 " + (evidence.weak_count || 0),
  ].join(" ／ ");
}


function boardItem(html) {
  const li = document.createElement("li");
  li.innerHTML = html;
  return li;
}


function renderBoardQuestions(board) {
  const list = el("board-questions");
  list.innerHTML = "";
  const questions = board.questions || [];
  el("board-goal").textContent = board.goal ? "目标：" + board.goal : "（还没有记录目标）";
  if (!questions.length) {
    list.innerHTML =
      '<li class="hint">还没有子问题清单。开局拆解未跑到时（或关掉了 plan_by_model），' +
      "这里会是空的，缺口清单仍按年份/来源族给出。</li>";
    return;
  }
  questions.forEach(function (item) {
    const covered = item.status === "covered";
    list.appendChild(
      boardItem(
        '<span class="board-idx">' +
          item.index +
          ".</span> " +
          esc(item.text) +
          ' <span class="badge ' +
          (covered ? "badge-strong" : "badge-weak") +
          '">' +
          (covered ? "已覆盖" : "缺口") +
          "</span>",
      ),
    );
  });
}


function renderBoardGaps(board) {
  const list = el("board-gaps");
  list.innerHTML = "";
  const gaps = board.gaps || [];
  if (!gaps.length) {
    list.innerHTML = '<li class="hint">暂时没有明显缺口。</li>';
    return;
  }
  gaps.forEach(function (gap) {
    list.appendChild(
      boardItem(
        '<code class="board-kind">' +
          esc(gap.kind) +
          "</code> " +
          esc(gap.detail) +
          '<div class="hint board-suggest">→ ' +
          esc(gap.suggest) +
          "</div>",
      ),
    );
  });
}


function sourceChips(sources) {
  const wrap = document.createElement("div");
  wrap.className = "board-srcs";
  (sources || []).forEach(function (item) {
    const chip = document.createElement("span");
    chip.className = "chip" + (item.has_summary ? " chip-strong" : "");
    chip.textContent =
      short(item.title || item.source_id, 26) + (item.year ? "（" + item.year + "）" : "");
    chip.title = (item.has_summary ? "有摘要" : "只有标题") + " ／ 点击看这条来源的详情";
    chip.addEventListener("click", function (event) {
      event.stopPropagation();
      showSourceBySourceId(item.source_id);
    });
    wrap.appendChild(chip);
  });
  if (!wrap.childNodes.length) {
    wrap.innerHTML = '<span class="hint">没有可解析的来源</span>';
  }
  return wrap;
}


function renderBoardEvidence(board) {
  const list = el("board-evidence");
  const note = el("board-evidence-note");
  list.innerHTML = "";
  const evidence = board.evidence || {};
  const rows = evidence.rows || [];
  if (evidence.mode === "support_checks") {
    note.textContent = "（来自引用核查：每条断言都审过支撑力度）";
  } else if (evidence.mode === "ledger") {
    note.textContent = "（本会话还没做过引用核查，这里按「子问题 × 已收集来源」聚合）";
  } else {
    note.textContent = "";
  }
  if (!rows.length) {
    list.innerHTML = '<li class="hint">还没有可展示的证据行。</li>';
    return;
  }
  rows.forEach(function (row) {
    const strong = row.strength === "强" || String(row.strength).indexOf("中") === 0;
    const verdicts = {
      supported: "已被支撑",
      partial: "部分支撑",
      unsupported: "支撑不足",
      unknown: "未判定",
    };
    const li = boardItem(
      '<div class="board-claim">' +
        esc(short(row.claim || "", 110)) +
        "</div>" +
        '<span class="badge ' +
        (strong ? "badge-strong" : "badge-weak") +
        '">' +
        esc(row.strength) +
        "</span>" +
        '<span class="meta">' +
        esc(row.evidence_level === "summary" ? "有摘要" : "仅标题") +
        (row.verdict && row.verdict !== "unknown" ? " ／ " + esc(verdicts[row.verdict] || row.verdict) : "") +
        "</span>",
    );
    li.appendChild(sourceChips(row.sources));
    if (row.reason) {
      const why = document.createElement("div");
      why.className = "hint";
      why.textContent = row.reason;
      li.appendChild(why);
    }
    list.appendChild(li);
  });
}


/* ------------------------------------------- 学习路线可视化（⑧ 时间线 + 日历） */
