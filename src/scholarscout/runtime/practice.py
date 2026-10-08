"""抽背卡的两处模型调用：「不会时的求助」与「主动延伸」。

为什么单独一个模块（AGENTS.md 铁律 9：动手前先定层次与名字）：
这两件事都是**编排** —— 读卡片 → 读这张卡的复习记录 → 取该会话已入库资料的摘要 →
组装提示词 → 一次模型调用 → 把 usage 交回 RPC 记账。它不写库（除"延伸题入库"那一步走状态层），
也不做参数校验（那是 RPC 的事），所以既不属于 state/ 也不属于 rpc/。

成本口径：**只有点「不会求助」和「延伸」才会走到这里**。判分、选项、填空全在本地
（state/learning_cards.grade_card），不产生费用。
"""

from __future__ import annotations

from typing import Any

from scholarscout.state import learning
from scholarscout.state.db import Database

#: 送进提示词的资料条数上限 —— 够模型指认，又不会把上下文撑爆（每轮工具结果都在计费）。
MAX_SOURCES = 5
MAX_SUMMARY_CHARS = 220


def review_history(db: Database, note_id: str, card_index: int) -> str:
    """这张卡的复习记录（③ 要求"结合学习记录"）。

    只用**追加式日志**（card_review_log）：它是事实；汇总行是派生值，两者不一致时以日志为准。
    """
    rows = db.query(
        "SELECT rating, reviewed_at FROM card_review_log"
        " WHERE note_id = ? AND card_index = ? ORDER BY id",
        (note_id, card_index),
    )
    if not rows:
        return "（这张卡还没有复习记录）"
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row["rating"] or "?")
        counts[key] = counts.get(key, 0) + 1
    tally = "／".join(
        f"{name} {counts[name]} 次" for name in ("good", "fuzzy", "bad") if name in counts
    )
    last = str(rows[-1]["reviewed_at"] or "")[:10]
    return f"共复习 {len(rows)} 次（{tally}），最近一次 {last}"


def gather_sources(
    db: Database, session_id: str | None, *, limit: int = MAX_SOURCES
) -> list[dict[str, str]]:
    """这个会话已经入库的资料（带最新摘要），按 tier 优先。

    之所以要喂给模型：用户要的是"结合知识库给出答案" —— 让模型**指认到具体哪一条资料**，
    比让它凭记忆讲更有依据，也便于点回原文。
    """
    if not session_id:
        return []
    rows = db.query(
        "SELECT s.id AS source_id, s.title, s.url,"
        " (SELECT summary_short FROM summaries WHERE source_id = s.id"
        "  ORDER BY created_at DESC LIMIT 1) AS summary_short"
        " FROM source_mentions m JOIN sources s ON s.id = m.source_id"
        " WHERE m.session_id = ? ORDER BY s.tier IS NULL, s.tier LIMIT ?",
        (session_id, limit),
    )
    out: list[dict[str, str]] = []
    for row in rows:
        summary = str(row["summary_short"] or "").strip().replace("\n", " ")
        out.append(
            {
                "source_id": str(row["source_id"]),
                "title": str(row["title"] or row["url"] or "")[:80],
                "summary": summary[:MAX_SUMMARY_CHARS],
            }
        )
    return out


def _card_block(card: dict[str, str]) -> str:
    kind = {"choice": "选择题", "blank": "填空题"}.get(str(card.get("type") or ""), "简答题")
    return (
        f"【卡片（{kind}）】\n"
        f"题干：{card.get('front') or ''}\n"
        f"参考答案：{card.get('answer') or card.get('back') or ''}"
    )


def _source_block(sources: list[dict[str, str]]) -> str:
    if not sources:
        return "（这个会话还没有入库资料，可以直接凭概念讲，但不要编造来源）"
    return "\n".join(
        f"- {item['source_id']}｜{item['title']}｜{item['summary']}" for item in sources
    )


def help_prompt(
    card: dict[str, str], response: str, history: str, sources: list[dict[str, str]]
) -> str:
    return (
        "学生做这张抽背卡时不会，请把它讲明白。\n\n"
        f"{_card_block(card)}\n\n"
        f"【学生的作答】{response.strip() or '（没作答 —— 就是不会）'}\n\n"
        f"【学生的复习记录】{history}\n\n"
        "【可以引用的资料】（只能引用下面这几条，不要编造来源；都不合适就说不必补）\n"
        f"{_source_block(sources)}\n\n"
        "要求（用中文，总长不超过 300 字）：\n"
        "1) 先直接给正确答案，一两句话讲清；\n"
        "2) 再讲为什么这么做（给可执行的理解路径，别说空话）；\n"
        "3) 最后指出该补哪条资料，用上面的 source_id 指认。"
    )


def extend_prompt(card: dict[str, str], response: str, history: str, count: int) -> str:
    return (
        f"基于下面这张抽背卡和学生的情况，出 {count} 道**延伸题**（比原题再深入一步，不要重复原题）。\n\n"
        f"{_card_block(card)}\n\n"
        f"【学生的作答】{response.strip() or '（没作答）'}\n\n"
        f"【学生的复习记录】{history}\n\n"
        "严格按我们的卡片格式输出，每题一段：\n"
        "## Q1. [选择] 题干\n"
        "答案：B\n"
        "（选择题把选项写在答案里；也可用 [填空] 或 [简答]）\n"
        "**只输出卡片，不要任何解释或开场白。**"
    )


def build_help(
    llm: Any,
    db: Database,
    *,
    session_id: str | None,
    card: dict[str, str],
    response: str,
) -> dict[str, Any]:
    """一次模型调用：答案 + 讲解 + 该补哪条资料。usage 一并带回给 RPC 记账。"""
    history = review_history(db, str(card.get("note_id") or ""), int(card.get("card_index") or 0))
    sources = gather_sources(db, session_id)
    prompt = help_prompt(card, response, history, sources)
    reply = llm.chat([{"role": "user", "content": prompt}])
    text = str(getattr(reply, "content", "") or "").strip()
    return {
        "text": text,
        "truncated": bool(getattr(reply, "truncated", False)),
        "sources": sources,
        "usage": getattr(reply, "usage", None) or {},
    }


def build_extension(
    llm: Any,
    db: Database,
    *,
    session_id: str | None,
    card: dict[str, str],
    response: str,
    count: int = 2,
    store: bool = True,
) -> dict[str, Any]:
    """出延伸题；默认**存成一张新卡笔记**（用户要的"主动延伸"要能落进复习计划里）。"""
    history = review_history(db, str(card.get("note_id") or ""), int(card.get("card_index") or 0))
    prompt = extend_prompt(card, response, history, count)
    reply = llm.chat([{"role": "user", "content": prompt}])
    raw = str(getattr(reply, "content", "") or "")
    questions = learning.parse_cards(raw)[:count]
    note_id = None
    if questions and store:
        body = "\n\n".join(
            "## Q{i}. [{tag}] {front}\n{back}".format(
                i=index + 1,
                tag={"choice": "选择", "blank": "填空"}.get(str(item.get("type") or ""), "简答"),
                front=item.get("front") or "",
                back=item.get("back") or "",
            )
            for index, item in enumerate(questions)
        )
        note_id = learning.save_note(
            db,
            kind="card",
            content=body,
            title=f"延伸题（来自「{(card.get('front') or '')[:18]}」）",
            tags=["derived"],
            session_id=session_id,
        )
    return {
        "questions": questions,
        "raw": raw,
        "note_id": note_id,
        "stored": bool(note_id),
        "usage": getattr(reply, "usage", None) or {},
    }
