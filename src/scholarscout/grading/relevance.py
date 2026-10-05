"""相关性打分。

两种来源，**结果里必须标明是哪一种**（``relevance_source``）：

- ``local``：完全确定性（主题词覆盖 × 时效 × 信息量），无模型、可复现、零成本
- ``model``：把标题/摘要与主题交给模型判 0–5 分并给理由（提示词外置在 ``prompts/grade-relevance.md``）

**刻意不使用 tier 参与打分**（照 AIHOT：分级只用来调门槛，避免"来源名气"带偏相关性判断）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from scholarscout.summary.prompts import PromptPack

#: 本地打分的权重（和为 1）
WEIGHT_TOPIC = 0.7
WEIGHT_RECENCY = 0.3
ABSTRACT_BONUS = 0.5
#: 主题词一个都没命中时的分数上限（压到"排除"区间）
NO_HIT_CAP = 1.5
#: 主题词最少字符数（比这短的词太泛，不作为信号）
MIN_TERM_CHARS = 2

_TERM_SPLIT = re.compile(r"[\s,，、;；:：/|()（）\[\]【】]+")
_SCORE_LINE = re.compile(r"^\s*relevance\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)\s*$", re.MULTILINE)
_REASON_LINE = re.compile(r"^\s*reason\s*[:：]\s*(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class RelevanceResult:
    score: float | None
    source: str  # local | model | none
    signals: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None
    guard_failed: bool = False
    model: str | None = None
    prompt_version: str | None = None


def topic_terms(topic: str | None) -> list[str]:
    if not topic:
        return []
    terms = [part.strip() for part in _TERM_SPLIT.split(topic)]
    return [term for term in terms if len(term) >= MIN_TERM_CHARS]


def recency_score(year: int | None, *, current_year: int) -> float:
    if year is None:
        return 2.0
    gap = current_year - int(year)
    if gap <= 2:
        return 5.0
    if gap <= 5:
        return 3.5
    return 2.0


def term_coverage(term: str, haystack: str) -> float:
    """单个主题词的覆盖度（0–1）。

    - 整体命中 → 1.0
    - 否则退到**中文二元组**：一个长主题词（如「教育智能体」）在标题里只出现一半
      （「人工智能教育课程」）也算部分相关。这是 E1 评测逼出来的改进：
      纯整体匹配时中文单主题词几乎全灭，全体判成「排除」（见 docs/05 §3.1）。
    - 单字词或非中文词不做二元组拆解，避免过松
    """
    needle = term.lower()
    if not needle:
        return 0.0
    if needle in haystack:
        return 1.0
    if len(needle) < MIN_TERM_CHARS + 1:
        return 0.0
    bigrams = [needle[index : index + 2] for index in range(len(needle) - 1)]
    if not bigrams:
        return 0.0
    hits = sum(1 for bigram in bigrams if bigram in haystack)
    return hits / len(bigrams)


def local_relevance(
    *,
    title: str,
    abstract: str | None,
    topic: str | None,
    year: int | None,
    current_year: int,
) -> RelevanceResult:
    terms = topic_terms(topic)
    haystack = f"{title}\n{abstract or ''}".lower()
    coverages = {term: term_coverage(term, haystack) for term in terms}
    hits = [term for term, value in coverages.items() if value >= 1.0]
    partial = [term for term, value in coverages.items() if 0.0 < value < 1.0]
    recency = recency_score(year, current_year=current_year)
    bonus = ABSTRACT_BONUS if (abstract or "").strip() else 0.0

    if not terms:
        # 没有主题就**没有相关性可言**：返回 None，而不是把时效与信息量硬算成一个分
        return RelevanceResult(
            score=None,
            source="local",
            signals=[
                {"name": "topic_terms", "value": []},
                {"name": "recency", "value": recency, "year": year},
                {"name": "has_abstract", "value": bool(bonus)},
            ],
            reason="未给主题，相关性无法判断（只判权威性）。",
        )

    coverage = sum(coverages.values()) / len(terms)
    topic_score = coverage * 5.0
    score = min(5.0, max(0.0, WEIGHT_TOPIC * topic_score + WEIGHT_RECENCY * recency + bonus))
    if coverage <= 0.0:
        # 主题词与它的二元组全都没中：一票压到不相关区间（否则"新 + 有摘要"能白拿 2 分）
        score = min(score, NO_HIT_CAP)
    signals: list[dict[str, Any]] = [
        {"name": "topic_terms", "value": terms},
        {"name": "topic_hits", "value": hits},
        {"name": "topic_partial", "value": partial},
        {
            "name": "topic_coverage",
            "value": round(coverage, 3),
            "weight": WEIGHT_TOPIC,
            "detail": {term: round(value, 3) for term, value in coverages.items()},
        },
        {"name": "recency", "value": recency, "weight": WEIGHT_RECENCY, "year": year},
        {"name": "has_abstract", "value": bool(bonus)},
    ]
    return RelevanceResult(
        score=round(score, 2),
        source="local",
        signals=signals,
        reason=_local_reason(hits, terms, year, abstract, partial),
    )


def _local_reason(
    hits: list[str], terms: list[str], year: int | None, abstract: str | None, partial: list[str]
) -> str:
    if not terms:
        return "未给主题，相关性只能按时效与信息量粗判。"
    if not hits and not partial:
        return "标题与摘要里既没有主题词、也没有它的二元组，判为不相关。"
    parts = []
    if hits:
        parts.append(f"命中主题词：{'、'.join(hits)}")
    if partial:
        parts.append(f"部分命中（中文二元组）：{'、'.join(partial)}")
    if year is not None:
        parts.append(f"年份 {year}")
    if not (abstract or "").strip():
        parts.append("无摘要字段，信息量有限")
    return "；".join(parts) + "。"


def build_prompt(
    *,
    pack: PromptPack,
    title: str,
    abstract: str | None,
    topic: str,
    year: int | None,
    venue: str | None,
) -> str:
    return pack.compose(
        "grade-relevance",
        title=title,
        abstract=abstract or "（无摘要）",
        topic=topic,
        year=str(year or "未注明"),
        venue=venue or "未注明",
    )


def parse_model_output(text: str) -> tuple[float | None, str | None]:
    """解析 ``relevance:`` / ``reason:`` 两行；分数卡到 0–5。"""
    score_match = _SCORE_LINE.search(text or "")
    score: float | None = None
    if score_match is not None:
        score = min(5.0, max(0.0, float(score_match.group(1))))
    reason_match = _REASON_LINE.search(text or "")
    reason = reason_match.group(1).strip() if reason_match is not None else None
    return score, reason


def model_relevance(
    llm: Any,
    *,
    title: str,
    abstract: str | None,
    topic: str,
    year: int | None = None,
    venue: str | None = None,
    pack: PromptPack,
) -> RelevanceResult:
    """让模型判相关性；解析失败就**退回本地打分**并标记 guard_failed。"""
    prompt = build_prompt(
        pack=pack, title=title, abstract=abstract, topic=topic, year=year, venue=venue
    )
    try:
        reply = llm.chat([{"role": "user", "content": prompt}])
    except Exception:
        fallback = local_relevance(
            title=title, abstract=abstract, topic=topic, year=year, current_year=_current_year()
        )
        return RelevanceResult(
            score=fallback.score,
            source="local",
            signals=[*fallback.signals, {"name": "model_call_failed", "value": True}],
            reason=fallback.reason,
            guard_failed=True,
        )

    score, reason = parse_model_output(str(getattr(reply, "content", "")))
    if score is None:
        fallback = local_relevance(
            title=title, abstract=abstract, topic=topic, year=year, current_year=_current_year()
        )
        return RelevanceResult(
            score=fallback.score,
            source="local",
            signals=[*fallback.signals, {"name": "model_output_unparsable", "value": True}],
            reason=fallback.reason,
            guard_failed=True,
            model=str(getattr(reply, "model", "") or ""),
        )
    return RelevanceResult(
        score=round(score, 2),
        source="model",
        signals=[{"name": "model_scored", "value": True, "topic": topic}],
        reason=reason,
        model=str(getattr(reply, "model", "") or ""),
        prompt_version=pack.version_of("grade-relevance"),
    )


def _current_year() -> int:
    from datetime import UTC, datetime

    return datetime.now(UTC).year


#: 判定标签（答辩要能解释每个阈值）
VERDICT_LABELS = ("核心", "相关", "外围", "排除")


def verdict_for(authority: float, relevance: float | None) -> str:
    """阈值集中在函数里，便于在论文里给出可复现的判定规则。

    - 相关性缺失时只用权威性粗判：够权威的算"相关"，其余"外围"
    - ``排除`` 只在**权威性极低**或**明确不相关**时给出
    """
    if authority < 1.5:
        return "排除"
    if relevance is None:
        return "相关" if authority >= 4.0 else "外围"
    if relevance < 2.0:
        return "排除"
    if authority >= 4.0 and relevance >= 4.0:
        return "核心"
    if relevance >= 3.0:
        return "相关"
    return "外围"
