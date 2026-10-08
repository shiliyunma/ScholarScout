"""③ 抽背卡（第一步）：题型号与本地判分的口径测试。

为什么值得钉：
- **做题型号不能改变老卡的行为** —— 没有标记的旧卡必须仍然解析成"简答"（否则用户
  历史抽背卡一夜之间全变成"判不了分"）；
- **能本地判的不许调模型** —— 选择题按字母、填空题精确比对，两者都不该产生模型费用；
- **判不了就如实说判不了**（简答 / 答案里认不出字母 / 没作答），不猜对错 ——
  抽背场景里"猜错判"比"不判"更伤：用户会按错误的反馈去复习。
"""

from __future__ import annotations

from scholarscout.state.learning_cards import (
    CARD_TYPE_CHOICE,
    CARD_TYPE_FILL,
    CARD_TYPE_SHORT,
    grade_card,
    parse_cards,
)

SAMPLE = """# 抽背卡

## Q1. [选择] GraphRAG 主要多了什么？
B. 实体与关系的社区摘要

## Q2. [填空] 中文检索用的 FTS5 分词器是？
答案：trigram

## Q3. [简答] 为什么正文不入库？
因为要控制库体积

## Q4. 旧格式没有标记
按简答走
"""


def test_types_are_parsed_and_marker_is_removed_from_front() -> None:
    cards = parse_cards(SAMPLE)
    assert [card["type"] for card in cards] == [
        CARD_TYPE_CHOICE,
        CARD_TYPE_FILL,
        CARD_TYPE_SHORT,
        CARD_TYPE_SHORT,
    ]
    # 标记本身不该留在题干里给用户看
    assert "[选择]" not in cards[0]["front"]
    assert cards[0]["front"].startswith("GraphRAG")


def test_explicit_answer_prefix_is_split_off() -> None:
    cards = parse_cards(SAMPLE)
    assert cards[1]["answer"] == "trigram"  # 「答案：」被摘掉
    assert cards[1]["back"].startswith("答案：")  # 但正文原样保留给人看


def test_choice_is_graded_locally_by_letter() -> None:
    card = parse_cards(SAMPLE)[0]
    right = grade_card(card, "B")
    assert right["graded"] is True and right["correct"] is True
    wrong = grade_card(card, "c")
    assert wrong["graded"] is True and wrong["correct"] is False


def test_choice_without_a_letter_in_the_answer_says_so() -> None:
    """答案里认不出字母就别猜（可能是模型把答案写成了一句话）。"""
    result = grade_card(
        {"type": CARD_TYPE_CHOICE, "back": "应该是社区摘要", "answer": "应该是社区摘要"}, "A"
    )
    assert result["graded"] is False and "字母" in str(result["reason"])


def test_fill_ignores_whitespace_and_punctuation() -> None:
    card = parse_cards(SAMPLE)[1]
    assert grade_card(card, "  Trigram，")["correct"] is True
    assert grade_card(card, "bigram")["correct"] is False


def test_short_answer_is_never_graded_locally() -> None:
    """简答一律交给模型 —— 本地硬判会制造错误反馈。"""
    result = grade_card(parse_cards(SAMPLE)[2], "随便写点东西")
    assert result["graded"] is False and "模型" in str(result["reason"])


def test_empty_response_is_not_counted_as_wrong() -> None:
    card = parse_cards(SAMPLE)[1]
    result = grade_card(card, "   ")
    assert result["graded"] is False
