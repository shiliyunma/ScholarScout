"""相关性契约：本地确定性打分、模型打分与解析、失败退回、判定阈值。"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholarscout.grading import relevance as rel
from scholarscout.providers.llm import ChatReply, LlmError
from scholarscout.summary.prompts import PromptPack

from ..conftest import StubLlm

PROMPTS = Path(__file__).resolve().parents[2] / "prompts"


class FailingLlm:
    def chat(self, messages, *, tools=None):
        raise LlmError("timeout", "桩模型超时")


def test_topic_terms_filters_short_pieces() -> None:
    assert rel.topic_terms("教育智能体 / RAG, 检索") == ["教育智能体", "RAG", "检索"]
    assert rel.topic_terms("a b c") == []
    assert rel.topic_terms(None) == []


@pytest.mark.parametrize(
    ("year", "expected"),
    [(2026, 5.0), (2024, 5.0), (2021, 3.5), (2015, 2.0), (None, 2.0)],
)
def test_recency_score(year: int | None, expected: float) -> None:
    assert rel.recency_score(year, current_year=2026) == expected


def test_local_relevance_full_coverage_scores_high() -> None:
    result = rel.local_relevance(
        title="教育智能体入门",
        abstract="面向教育智能体的检索增强方法。",
        topic="教育智能体",
        year=2025,
        current_year=2026,
    )
    assert result.source == "local"
    assert result.score is not None and result.score >= 4.0
    signals = {signal["name"]: signal["value"] for signal in result.signals}
    assert signals["topic_hits"] == ["教育智能体"]
    assert signals["has_abstract"] is True


def test_local_relevance_without_topic_has_no_score() -> None:
    result = rel.local_relevance(
        title="任何标题", abstract=None, topic=None, year=2020, current_year=2026
    )
    assert result.score is None, "没有主题就不该编出一个分"
    assert result.reason is not None and "未给主题" in result.reason


def test_local_relevance_no_hits_is_capped_into_irrelevant() -> None:
    result = rel.local_relevance(
        title="Unrelated title",
        abstract="Nothing to do with the topic.",
        topic="教育智能体",
        year=2026,
        current_year=2026,
    )
    assert result.score is not None and result.score <= rel.NO_HIT_CAP
    assert rel.verdict_for(5.0, result.score) == "排除"
    assert result.reason is not None and "没有出现主题词" in result.reason


def test_local_relevance_ignores_tier_entirely() -> None:
    """分级不参与打分（照 AIHOT：分级只调门槛）—— 同名输入必须得到同分。"""
    a = rel.local_relevance(title="t", abstract="a", topic="x", year=2026, current_year=2026)
    b = rel.local_relevance(title="t", abstract="a", topic="x", year=2026, current_year=2026)
    assert a.score == b.score


@pytest.mark.parametrize(
    ("authority", "relevance", "expected"),
    [
        (1.0, 5.0, "排除"),
        (5.0, 1.0, "排除"),
        (5.0, 4.5, "核心"),
        (4.0, 4.0, "核心"),
        (4.0, 3.0, "相关"),
        (3.0, 3.5, "相关"),
        (3.0, 2.5, "外围"),
        (2.5, None, "外围"),
        (5.0, None, "相关"),
    ],
)
def test_verdict_thresholds(authority: float, relevance: float | None, expected: str) -> None:
    assert rel.verdict_for(authority, relevance) == expected


def test_parse_model_output_ok() -> None:
    score, reason = rel.parse_model_output("relevance: 4.5\nreason: 命中数据集与任务")
    assert score == 4.5
    assert reason == "命中数据集与任务"


def test_parse_model_output_clamps_and_tolerates_missing_reason() -> None:
    assert rel.parse_model_output("relevance: 9")[0] == 5.0
    assert rel.parse_model_output("relevance: -3") == (None, None), (
        "负数属格式错误，走兜底而不是当 0 分"
    )
    assert rel.parse_model_output("完全不是这个格式") == (None, None)


def test_model_relevance_uses_model_score() -> None:
    llm = StubLlm(ChatReply(content="relevance: 5\nreason: 直接解决该主题", model="stub"))
    result = rel.model_relevance(
        llm,
        title="教育智能体",
        abstract="检索增强",
        topic="教育智能体",
        pack=PromptPack(PROMPTS),
    )
    assert result.source == "model"
    assert result.score == 5.0
    assert result.reason == "直接解决该主题"
    assert result.prompt_version is not None and len(result.prompt_version) == 12
    assert result.guard_failed is False


def test_model_relevance_prompt_has_no_placeholders() -> None:
    llm = StubLlm(ChatReply(content="relevance: 3\nreason: x"))
    rel.model_relevance(llm, title="t", abstract=None, topic="主题", pack=PromptPack(PROMPTS))
    prompt = str(llm.calls[0]["messages"][0]["content"])
    assert "{{" not in prompt
    assert "反幻觉规则" in prompt
    assert "（无摘要）" in prompt


def test_model_relevance_falls_back_when_output_unparsable() -> None:
    llm = StubLlm(ChatReply(content="我觉得挺相关的"))
    result = rel.model_relevance(
        llm,
        title="教育智能体入门",
        abstract="检索增强",
        topic="教育智能体",
        pack=PromptPack(PROMPTS),
    )
    assert result.source == "local"
    assert result.guard_failed is True
    assert result.score is not None


def test_model_relevance_falls_back_when_model_fails() -> None:
    result = rel.model_relevance(
        FailingLlm(),
        title="教育智能体",
        abstract=None,
        topic="教育智能体",
        pack=PromptPack(PROMPTS),
    )
    assert result.source == "local"
    assert result.guard_failed is True
    assert any(signal["name"] == "model_call_failed" for signal in result.signals)


def test_build_prompt_fills_metadata() -> None:
    prompt = rel.build_prompt(
        pack=PromptPack(PROMPTS),
        title="T",
        abstract="A",
        topic="主题",
        year=2025,
        venue="NeurIPS",
    )
    assert "2025" in prompt and "NeurIPS" in prompt


def test_verdict_labels_complete() -> None:
    assert set(rel.VERDICT_LABELS) == {"核心", "相关", "外围", "排除"}
