"""抽背卡的两处模型调用：「不会时的求助」与「主动延伸」。

为什么单独一个模块（AGENTS.md 铁律 9：动手前先定层次与名字）：
这两件事都是**编排** —— 读卡片 → 读这张卡的复习记录 → 取该会话已入库资料的摘要 →
组装提示词 → 一次模型调用 → 把 usage 交回 RPC 记账。它不写库（除"延伸题入库"那一步走状态层），
也不做参数校验（那是 RPC 的事），所以既不属于 state/ 也不属于 rpc/。

成本口径：**只有点「不会求助」和「延伸」才会走到这里**。判分、选项、填空全在本地
（state/learning_cards.grade_card），不产生费用。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from scholarscout.providers.llm import LlmError
from scholarscout.state import learning, learning_cards, source_context
from scholarscout.state.db import Database

#: 送进提示词的资料条数上限 —— 够模型指认，又不会把上下文撑爆（每轮工具结果都在计费）。
MAX_SOURCES = 5
MAX_SUMMARY_CHARS = 220


def review_history(db: Database, note_id: str, card_index: int) -> str:
    """这张卡的复习记录（③ 要求"结合学习记录"）。

    只用**追加式日志**（card_review_log）：它是事实；汇总行是派生值，两者不一致时以日志为准。

    查询在 `state/learning_cards.review_log_rows`（state 是唯一碰 SQL 的地方）：
    「一次都没复习过」时那句话、档位怎么数、日期截几位这些**业务口径仍在这里**。
    """
    rows = learning_cards.review_log_rows(db, note_id, card_index)
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
    rows = source_context.session_sources_with_summary(db, session_id=session_id, limit=limit)
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
    card: dict[str, str],
    response: str,
    history: str,
    sources: list[dict[str, str]],
    *,
    stage: str = "full",
) -> str:
    """两种情况两套提示词（大浩选定「分两步」）：

    - stage="hint"：**不给答案**，只给一小段思路或回忆线索，把人往正确答案上推 ——
      直接给答案等于替他做了这道题；
    - stage="full"：完整讲解（正确项为什么对、其它选项错在哪、考点、建议、该补哪条资料）。
    """
    if stage == "hint":
        return (
            "学生做这张抽背卡时卡住了，**先不要给答案**，只给一小段思路提示把他推向正确答案。\n\n"
            f"{_card_block(card)}\n\n"
            f"【学生的作答】{response.strip() or '（没作答 —— 就是不会）'}\n\n"
            f"【学生的复习记录】{history}\n\n"
            "要求（用中文，80 字以内，最多 2 句）：\n"
            "1) 指一个方向或让他回忆相关的那个概念，不要直接说出答案；\n"
            "2) 结尾给一句「先自己试，不行再看详细讲解」。"
        )
    return (
        "学生做这张抽背卡时不会，请把它讲明白。\n\n"
        f"{_card_block(card)}\n\n"
        f"【学生的作答】{response.strip() or '（没作答 —— 就是不会）'}\n\n"
        f"【学生的复习记录】{history}\n\n"
        "【可以引用的资料】（只能引用下面这几条，不要编造来源；都不合适就说不必补）\n"
        f"{_source_block(sources)}\n\n"
        "要求（用中文，总长不超过 350 字，**分点写，不要只给一句结论**）：\n"
        "1) 正确答案是什么，一句话点明；\n"
        "2) 如果是选择题：正确项为什么对，**并逐个说清其它选项错在哪**；\n"
        "   如果是填空/简答：给出完整答案与它的关键点；\n"
        "3) 这道题实际在考什么（把知识点点出来）；\n"
        "4) 结合上面的复习记录给一句具体建议；\n"
        "5) 最后指出该补哪条资料，用上面的 source_id 指认。"
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
    stage: str = "full",
    on_delta: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """一次模型调用：答案 + 讲解 + 该补哪条资料。usage 一并带回给 RPC 记账。

    `on_delta` 非空且客户端支持流式时改走 :meth:`chat_stream`（**增量**逐段回调，
    界面据此实时显示）。中途断开：**已收到的部分原样交回**（`text`），真实原因放进 `error`，
    绝不让界面只剩半句话而无解释（见 providers/llm.py 的 chat_stream 约定）。
    """
    history = review_history(db, str(card.get("note_id") or ""), int(card.get("card_index") or 0))
    sources = gather_sources(db, session_id)
    prompt = help_prompt(card, response, history, sources, stage=stage)
    messages = [{"role": "user", "content": prompt}]
    if on_delta is not None and callable(getattr(llm, "chat_stream", None)):
        try:
            reply = llm.chat_stream(messages, on_delta=on_delta)
        except LlmError as exc:
            partial = str(getattr(exc, "partial", "") or "")
            if not partial:
                raise  # 一个字都没收到：照常抛，让 RPC 如实报错
            # 收到了半句：保留它 + 如实报原因，界面据此"先展示已讲到的、再附一行说明"。
            return {
                "text": partial,
                "stage": stage,
                "truncated": False,
                "sources": sources,
                "usage": {},
                "error": f"{exc.kind}: {exc}",
            }
    else:
        reply = llm.chat(messages)
    text = str(getattr(reply, "content", "") or "").strip()
    return {
        "text": text,
        "stage": stage,
        "truncated": bool(getattr(reply, "truncated", False)),
        "sources": sources,
        "usage": getattr(reply, "usage", None) or {},
    }


def repair_options_prompt(content: str) -> str:
    """老卡补选项（大浩选定）：把没有选项的选择题重写成带 A/B/C/D 的版本。

    硬要求：**只补选项、不改题意、不改答案**。模型顺手改写答案是最糟的结果 ——
    用户会照着一份被改过的答案复习。
    """
    return (
        "下面是一组抽背卡。其中**选择题没有写出选项**，导致界面上没法选。\n\n"
        f"{content}\n\n"
        "请输出**同样数量的卡片**，保持原来的题干与答案不变，只给选择题补上 A/B/C/D 四个选项"
        "（选项写进题干，答案写成选项字母），填空题保持「答案：<词>」写法。\n"
        "格式：\n"
        "## Q1. [选择] 题干\n"
        "A. …\n"
        "B. …\n"
        "C. …\n"
        "D. …\n"
        "答案：B\n"
        "**只输出卡片，不要解释。题意和正确答案都不许改。**"
    )


def build_option_repair(llm: Any, db: Database, *, note_id: str) -> dict[str, Any]:
    """给老卡补选项：一次模型调用，改好写回原笔记（正文是唯一真相，选项必须进正文）。"""
    note = learning.get_note(db, note_id) or {}
    content = str(note.get("content") or "")
    if not content:
        return {"ok": False, "reason": "这张笔记没有正文", "fixed": 0}
    reply = llm.chat([{"role": "user", "content": repair_options_prompt(content)}])
    text = str(getattr(reply, "content", "") or "").strip()
    cards = learning.parse_cards(text)
    before = learning.parse_cards(content)
    with_options = [
        card
        for card in cards
        if card.get("type") == learning.CARD_TYPE_CHOICE
        and any(
            part.strip().upper().startswith(("A.", "A、", "B.", "B、"))
            for part in (card.get("front") or "").splitlines()
        )
    ]
    if not cards or not with_options:
        return {
            "ok": False,
            "reason": "模型这次没有给出带选项的卡片（原卡未改动）",
            "fixed": 0,
            "usage": getattr(reply, "usage", None) or {},
        }
    body = "\n\n".join(
        "## Q{i}. [{tag}] {front}\n{back}".format(
            i=index + 1,
            tag={"choice": "选择", "blank": "填空"}.get(str(card.get("type") or ""), "简答"),
            front=card.get("front") or "",
            back=card.get("back") or "",
        )
        for index, card in enumerate(cards)
    )
    learning.save_note(db, kind="card", content=body, note_id=note_id)
    return {
        "ok": True,
        "fixed": len(with_options),
        "before": len(before),
        "after": len(cards),
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
