"""抽背「练」模式：卡片解析、评价记账、统计口径。

三件事是这组测试的重点（都是"看起来对、其实是错的"那类）：

1. **解析容错**：卡片格式是我们自己的约定（`## Q…` / `**A.**`），认不出答案的段落必须**跳过**，
   不能把整段当成答案 —— 宁可少一张卡，也不要给用户一张错卡；
2. **按卡记而不是按笔记行记**：一行卡片笔记通常有好几张卡，按行记等于把 10 张压成一个数；
3. **记住率的分母只算评过的卡**：没评过的卡不能被算成"不会"（否则第一轮记住率永远很低，
   用户会以为系统在打击他）。
"""

from __future__ import annotations

import pytest

from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state import learning

from ..conftest import insert_source, insert_summary  # noqa: F401 - 夹具可用性占位


def _card_note(ctx: AppContext, content: str, *, title: str = "抽背卡") -> str:
    return learning.save_note(ctx.db, kind="card", content=content, title=title, session_id=None)


DEMO = """# 抽背卡

## Q1. 什么是 RAG？

**A.** 检索增强生成：先检索再生成。

## Q2、trigram 索引有什么坑？

A：短查询（<3 字符）返回 0 条，要靠 LIKE 兜底。

## 这一问没有答案

（模型忘了写答案）
"""


# ------------------------------------------------------------------ 解析


def test_parse_cards_handles_all_three_prefix_styles() -> None:
    cards = learning.parse_cards(DEMO)
    assert [card["front"] for card in cards][:2] == ["什么是 RAG？", "trigram 索引有什么坑？"]
    assert cards[0]["back"] == "检索增强生成：先检索再生成。"
    assert cards[1]["back"].startswith("短查询")


def test_parse_cards_keeps_placeholder_answers_but_drops_empty_ones() -> None:
    """口径（写清楚是因为它有取舍）：

    - 问题段之后的第一段非空正文就是答案 —— 模型常不带 `A.` 就写一段，只认前缀会丢好卡；
    - 所以"（模型忘了写答案）"这类**占位文字会被保留成一张卡**：抽背时一眼看得出它无效，
      比默默少一张卡好（少卡会让人以为系统漏了）；
    - 但正文**只有标点**的段落丢掉（那种"答案"没有信息）。
    """
    cards = learning.parse_cards(DEMO)
    assert len(cards) == 3, "带占位文字的段保留成卡"
    assert cards[2]["front"] == "这一问没有答案"
    assert "忘了写答案" in cards[2]["back"]

    only_punct = learning.parse_cards("## Q1. 只有标点的答案？\n\n**A.** —— 。\n")
    assert only_punct == [], "只有标点不算答案"


def test_parse_cards_on_empty_or_junk() -> None:
    assert learning.parse_cards("") == []
    assert learning.parse_cards("# 抽背卡\n\n（什么都没写）") == []


def test_parse_cards_keeps_multiline_answers() -> None:
    cards = learning.parse_cards("## Q1. 多行答案？\n\n**A.** 第一行\n第二行\n")
    assert cards[0]["back"] == "第一行\n第二行"


# ------------------------------------------------------------------ 记账


def test_review_upserts_and_counts_attempts(ctx: AppContext) -> None:
    note_id = _card_note(ctx, DEMO)
    learning.review_card(ctx.db, note_id, 0, "bad")
    learning.review_card(ctx.db, note_id, 0, "good")  # 同一张再评一次＝更新，不是新增一行
    rows = ctx.db.query("SELECT rating, reviews FROM card_reviews WHERE note_id = ?", (note_id,))
    assert len(rows) == 1
    assert rows[0]["rating"] == "good"
    assert rows[0]["reviews"] == 2


def test_review_rejects_unknown_rating_and_negative_index(ctx: AppContext) -> None:
    note_id = _card_note(ctx, DEMO)
    with pytest.raises(ValueError, match="非法评价"):
        learning.review_card(ctx.db, note_id, 0, "perfect")
    with pytest.raises(ValueError, match="不能为负"):
        learning.review_card(ctx.db, note_id, -1, "good")


def test_reviews_are_per_card_not_per_note(ctx: AppContext) -> None:
    """一行笔记两张卡 → 两条记录（按行记就说不清哪张没记住）。"""
    note_id = _card_note(ctx, DEMO)
    learning.review_card(ctx.db, note_id, 0, "good")
    learning.review_card(ctx.db, note_id, 1, "bad")
    stats = learning.card_stats(ctx.db)
    assert stats["reviewed"] == 2
    assert stats["by_rating"]["good"] == 1
    assert stats["by_rating"]["bad"] == 1


def test_deleting_a_note_removes_its_reviews(ctx: AppContext) -> None:
    """删笔记要连带删评价（外键 ON DELETE CASCADE），不留孤儿行。"""
    note_id = _card_note(ctx, DEMO)
    learning.review_card(ctx.db, note_id, 0, "good")
    ctx.db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    assert ctx.db.query("SELECT * FROM card_reviews") == []


# ------------------------------------------------------------------ 统计口径


def test_recall_rate_denominator_is_reviewed_cards_only(ctx: AppContext) -> None:
    """10 张卡只评了 2 张（1 会 1 不会）→ 记住率 50%，而不是 10%。"""
    note_id = _card_note(ctx, DEMO)
    learning.review_card(ctx.db, note_id, 0, "good")
    learning.review_card(ctx.db, note_id, 1, "bad")
    stats = learning.card_stats(ctx.db)
    assert stats["recall_rate"] == pytest.approx(0.5)


def test_recall_rate_is_none_when_nothing_reviewed(ctx: AppContext) -> None:
    """一张没练过 → None（"没有样本"和"全不会"是两件事）。"""
    assert learning.card_stats(ctx.db)["recall_rate"] is None
    assert learning.card_stats(ctx.db)["reviewed"] == 0


def test_get_note_exposes_parsed_cards_with_ratings(ctx: AppContext) -> None:
    """界面拿 `cards[]` 直接开练：带 index 与已有评价。"""
    note_id = _card_note(ctx, DEMO)
    learning.review_card(ctx.db, note_id, 1, "fuzzy")
    note = learning.get_note(ctx.db, note_id)
    assert note is not None
    assert [card["index"] for card in note["cards"]] == [0, 1, 2]  # DEMO 里有三张（含占位那张）
    assert note["cards"][1]["rating"] == "fuzzy"
    assert note["cards"][0]["rating"] is None


# ------------------------------------------------------------------ RPC


def test_rpc_review_and_stats(ctx: AppContext) -> None:
    note_id = _card_note(ctx, DEMO)
    result = dispatch(ctx, "note.review", {"note_id": note_id, "card_index": 0, "rating": "good"})
    assert result["ok"] is True
    assert result["stats"]["reviewed"] == 1
    assert result["stats"]["recall_rate"] == pytest.approx(1.0)
    assert dispatch(ctx, "note.stats", {})["by_rating"]["good"] == 1


def test_rpc_review_rejects_bad_rating(ctx: AppContext) -> None:
    from scholarscout.rpc.errors import RpcError

    note_id = _card_note(ctx, DEMO)
    with pytest.raises(RpcError):
        dispatch(ctx, "note.review", {"note_id": note_id, "card_index": 0, "rating": "?"})
