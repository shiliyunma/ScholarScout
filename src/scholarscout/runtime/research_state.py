"""跨轮研究台账（A1）+ 覆盖缺口（A2）+ 查询相似度（A3）。

## 这个模块解决什么

现在的循环是"每轮从零现推意图 → 按固定顺序搜一遍 → 出结论"，于是第 2 轮用户说"继续搜索"
时，模型只能**换词重搜同一批东西**（真机证据：3 个多轮会话里 2 个出现重复查询，
其中一个第 2 轮一个新来源都没发现）。业界成熟做法是三件事（见 docs/14 §2）：

1. **计划外置**（Anthropic 把计划存 Memory，理由正是"上下文超 20 万 token 会被截断"）；
2. **缺口驱动**：子 agent 评估质量、**识别缺口**、据此精炼下一条 query；
3. **查询改写**用上一轮命中的东西，而不是重发原查询（GuideCQR / ICR / Self-RAG）。

本模块把 ①②③ 里**确定性的部分**做掉：状态存取、缺口计算、查询判重。
让模型做的只有两件轻活：开局拆一次子问题、每轮末尾回填一行覆盖状态。

## 三条口径（写在最前面，因为它们决定了数字可不可信）

- **覆盖与否由模型回填**（回复末尾的状态标记）；解析不出来就**保持 open**，绝不猜；
- **判重只在本会话内**、且只与**已经执行过**的查询比；
- 相似度用**二字符组 Jaccard**（中文里"换词不换靶"用完全相等抓不到），
  与 `scripts/diagnose_turns.py` **共用同一个实现** —— 尺子只有一把。
"""

from __future__ import annotations

import json
import re
from typing import Any

from scholarscout.state.db import Database
from scholarscout.state.migrate import now_iso

#: 判重阈值（与诊断脚本口径一致）
DEFAULT_SIMILARITY = 0.6
#: 连续多少次"检索到 0 条新来源"就停（A7）
MAX_EMPTY_ROUNDS = 3
#: 注入提示词时最多列几条缺口（省 token）
MAX_GAPS_IN_NOTE = 4
#: 状态标记：模型在回复末尾用它回填覆盖情况
MARKER_RE = re.compile(r"<!--\s*research-state\s*:\s*(\{.*?\})\s*-->", re.DOTALL)


# ------------------------------------------------------------------ 相似度（与诊断共用）


def normalize_query(text: str) -> str:
    """归一化：去空白与标点、转小写。"""
    return re.sub(r"[\s\W_]+", "", str(text or "").lower())


def _bigrams(text: str) -> set[str]:
    return {text[index : index + 2] for index in range(max(0, len(text) - 1))}


def similarity(left: str, right: str) -> float:
    """二字符组 Jaccard 相似度（0~1）。空串一律回 0。"""
    a, b = _bigrams(normalize_query(left)), _bigrams(normalize_query(right))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_repeat(
    state: dict[str, Any], query: str, *, threshold: float = DEFAULT_SIMILARITY
) -> str | None:
    """这个查询是不是在"换词重搜"？是就返回之前那条，否则 None。"""
    normalized = normalize_query(query)
    if not normalized:
        return None
    for used in state.get("used_queries") or []:
        if similarity(normalized, str(used)) >= threshold:
            return str(used)
    return None


# ------------------------------------------------------------------ 状态存取


def empty_state(goal: str = "") -> dict[str, Any]:
    return {
        "goal": goal,
        "questions": [],
        "used_queries": [],
        "seen_sources": [],
        "empty_rounds": 0,
    }


def load_state(db: Database, session_id: str) -> dict[str, Any] | None:
    row = db.query_one(
        "SELECT payload_json FROM research_states WHERE session_id = ?", (session_id,)
    )
    if row is None:
        return None
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def save_state(db: Database, session_id: str, state: dict[str, Any]) -> None:
    db.execute(
        """
        INSERT INTO research_states (session_id, payload_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET payload_json = excluded.payload_json,
                                              updated_at = excluded.updated_at
        """,
        (session_id, json.dumps(state, ensure_ascii=False), now_iso()),
    )


def record_query(state: dict[str, Any], query: str) -> None:
    """记下一条已经执行的查询（判重的唯一数据源）。"""
    used = state.setdefault("used_queries", [])
    if query and query not in used:
        used.append(query)


def new_sources(state: dict[str, Any], source_ids: list[str]) -> list[str]:
    """把这一批来源并进台账，返回**这次真正新增**的那些。"""
    seen = state.setdefault("seen_sources", [])
    fresh: list[str] = []
    for source_id in source_ids:
        text = str(source_id or "")
        if text and text not in seen:
            seen.append(text)
            fresh.append(text)
    return fresh


def apply_coverage(state: dict[str, Any], marker_text: str, fresh_source_ids: list[str]) -> bool:
    """按模型回填的状态标记更新子问题覆盖情况。返回是否解析成功。

    标记形如 `<!--research-state:{"covered":[1,3],"open":[2]}-->`（数字是子问题序号，1 起）。
    解析不出来**就什么都不改**（保持 open）—— 猜覆盖等于自欺。
    """
    found = MARKER_RE.search(str(marker_text or ""))
    if not found:
        return False
    try:
        payload = json.loads(found.group(1))
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    covered = {int(item) for item in payload.get("covered") or [] if str(item).isdigit()}
    questions = state.get("questions") or []
    for index, question in enumerate(questions, start=1):
        if not isinstance(question, dict):
            continue
        if index in covered:
            question["status"] = "covered"
            if fresh_source_ids:
                question["evidence"] = (question.get("evidence") or []) + fresh_source_ids
        elif str(question.get("status")) != "covered":
            question["status"] = "open"
    return True


def strip_marker(text: str) -> str:
    """把状态标记从**可见正文**里剥掉（它只是内部记账，不该给用户看到）。"""
    return MARKER_RE.sub("", str(text or "")).rstrip()


# ------------------------------------------------------------------ 缺口（A2）


def compute_gaps(
    db: Database,
    session_id: str,
    state: dict[str, Any],
    *,
    now_year: int | None = None,
) -> list[dict[str, str]]:
    """算出"还缺什么"——这是让第 2 轮有方向的关键。

    只做**能从库里确定性判定**的缺口（不猜）：
    未覆盖的子问题 / 只有标题没有摘要 / 缺近年资料 / 缺可上手资料 / 只搜到没看正文 / 来源过度集中。
    """
    import datetime

    year_now = now_year or datetime.date.today().year
    rows = [
        dict(row)
        for row in db.query(
            """
            SELECT s.id, s.title, s.source_type, s.domain, s.year,
                   (SELECT count(*) FROM documents d WHERE d.source_id = s.id) AS docs,
                   (SELECT count(*) FROM summaries sm WHERE sm.source_id = s.id) AS sums
            FROM sources s JOIN source_mentions m ON m.source_id = s.id
            WHERE m.session_id = ?
            GROUP BY s.id
            """,
            (session_id,),
        )
    ]
    gaps: list[dict[str, str]] = []

    open_questions = [
        str(item.get("text") or "")
        for item in state.get("questions") or []
        if isinstance(item, dict) and str(item.get("status")) != "covered"
    ]
    if open_questions:
        shown = "；".join(open_questions[:3])
        gaps.append(
            {
                "kind": "open_questions",
                "detail": f"还有 {len(open_questions)} 个子问题没有证据：{shown}",
                "suggest": "针对这些子问题单独搜（可换关键词/换来源/换语种），不要重搜已搜过的说法",
            }
        )

    weak = [row for row in rows if not row["sums"]]
    if weak:
        gaps.append(
            {
                "kind": "weak_evidence",
                "detail": f"{len(weak)} 条来源只有标题（没有摘要），引用它们下结论支撑弱",
                "suggest": "挑其中最有价值的 1–2 条 source.fetch 取正文，再决定要不要引用",
            }
        )

    if rows:
        years = [int(row["year"]) for row in rows if row.get("year")]
        if years and max(years) < year_now - 1:
            gaps.append(
                {
                    "kind": "year_gap",
                    "detail": f"现有资料最早/最新是 {min(years)}–{max(years)}，缺近两年的进展",
                    "suggest": f"加年份限定（如 year_from={year_now - 1}）再搜一轮",
                }
            )
        hands_on = [
            row for row in rows if str(row.get("source_type") or "") in ("github", "web", "doc")
        ]
        if not hands_on:
            gaps.append(
                {
                    "kind": "kind_gap",
                    "detail": "现有来源全是论文/文库，缺可上手的资料（代码仓库、官方文档、教程）",
                    "suggest": "换 GitHub / 官方文档 / 教程方向搜一轮，用它们补'怎么动手'",
                }
            )
        unfetched = [row for row in rows if not row["docs"]]
        if unfetched:
            gaps.append(
                {
                    "kind": "not_fetched",
                    "detail": f"{len(unfetched)} 条只是搜到了、还没看正文",
                    "suggest": "先 source.fetch 其中 1–2 条，看图谱/路线的'出处'是否只是标题",
                }
            )
        domains: dict[str, int] = {}
        for row in rows:
            key = str(row.get("domain") or "?")
            domains[key] = domains.get(key, 0) + 1
        if domains and len(rows) >= 4:
            top_domain, count = max(domains.items(), key=lambda item: item[1])
            if count / len(rows) >= 0.8:
                gaps.append(
                    {
                        "kind": "source_concentration",
                        "detail": f"来源集中在 {top_domain}（{count}/{len(rows)}），换来源能看到不同说法",
                        "suggest": "换一个来源族再搜（例如论文源换成代码/社区源）",
                    }
                )
    return gaps


# ------------------------------------------------------------------ 开局分解（A1）

DECOMPOSE_PROMPT = "research-decompose"
MAX_QUESTIONS = 6


def parse_decompose(content: str) -> dict[str, Any] | None:
    """从模型回复里取出 `{"goal","questions"}`；取不到就回 None（**不猜**）。"""
    text = str(content or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    questions = [
        str(item).strip()
        for item in (payload.get("questions") or [])
        if isinstance(item, str) and str(item).strip()
    ]
    if not questions:
        return None
    return {
        "goal": str(payload.get("goal") or "").strip(),
        "questions": questions[:MAX_QUESTIONS],
    }


def decompose_with_reason(
    llm: Any, request: str, *, prompts_dir: Any
) -> tuple[list[dict[str, Any]], str | None]:
    """把用户目标拆成子问题，并**把失败原因带出来**。

    为什么返回原因而不是静默回空表：拆不出来 = 缺口粒度退化，用户应当看得见。
    真机上就是栽在这里 —— 读取处漏了一行（`runtime.plan_by_model` 没接进 load_config），
    `except Exception` 把真相吃掉，表现只是"子问题 0 条"，查了一轮才发现。
    """
    from scholarscout.summary.prompts import PromptPack

    try:
        prompt = PromptPack(prompts_dir).compose(DECOMPOSE_PROMPT, request=request.strip()[:1200])
        reply = llm.chat([{"role": "user", "content": prompt}])
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"
    parsed = parse_decompose(str(getattr(reply, "content", "")))
    if parsed is None:
        return [], "模型没有按要求回 JSON（子问题清单缺失，缺口粒度退化为按年份/来源族判断）"
    return [
        {"text": question, "status": "open", "evidence": []} for question in parsed["questions"]
    ], None


def decompose(llm: Any, request: str, *, prompts_dir: Any) -> list[dict[str, Any]]:
    """只取子问题（失败回空表）。需要知道为什么失败时用 :func:`decompose_with_reason`。"""
    questions, _reason = decompose_with_reason(llm, request, prompts_dir=prompts_dir)
    return questions


def render_note(state: dict[str, Any], gaps: list[dict[str, str]]) -> str:
    """把台账与缺口渲染成一段紧凑的注入文本（放在本轮消息的最后）。"""
    lines = ["【本轮研究台账】（系统自动维护，用来避免重复劳动）"]
    goal = str(state.get("goal") or "").strip()
    if goal:
        lines.append(f"目标：{goal}")
    questions = state.get("questions") or []
    if questions:
        marks = []
        for index, question in enumerate(questions, start=1):
            status = "已覆盖" if str(question.get("status")) == "covered" else "缺口"
            marks.append(f"{index}.{question.get('text')}（{status}）")
        lines.append("子问题：" + "；".join(marks))
    elif state.get("plan_error"):
        lines.append(f"（子问题清单缺失：{state['plan_error']}）")
    used = state.get("used_queries") or []
    if used:
        recent = "；".join(str(item) for item in used[-6:])
        lines.append(f"已搜过（**不要换词重搜**，会被工具拒绝）：{recent}")
    if gaps:
        lines.append("缺口清单（优先按这个定检索方向）：")
        for gap in gaps[:MAX_GAPS_IN_NOTE]:
            lines.append(f"- [{gap['kind']}] {gap['detail']} → 建议：{gap['suggest']}")
    else:
        lines.append("缺口清单：暂时没有明显缺口（若本轮仍搜不到新东西，就如实说明并停止）。")
    lines.append("要求：先处理缺口清单；本轮若无新增来源，直接如实说明并停止，不要换词重搜。")
    return "\n".join(lines)


def set_question_status(db: Database, session_id: str, index: int, status: str) -> dict[str, Any]:
    """改一个子问题的覆盖状态（大浩选定：看板可编辑、改动落库）。

    index 是**从 1 开始**的序号（与界面上的编号一致）；status 只认 covered / open ——
    其余值直接拒绝，不写库。返回改好后的整份 state，界面直接重渲染。
    """
    if status not in ("covered", "open"):
        raise ValueError("status 只支持 covered / open")
    state = load_state(db, session_id)
    if state is None:  # 这个会话还没有研究状态：从空的一份开始，别让类型检查裸奔
        state = {"questions": []}
    questions = [item for item in (state.get("questions") or []) if isinstance(item, dict)]
    if index < 1 or index > len(questions):
        raise ValueError("没有第 " + str(index) + " 个子问题")
    questions[index - 1]["status"] = status
    state["questions"] = questions
    save_state(db, session_id, state)
    return state
