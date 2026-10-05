"""确定性守卫：清洗、截断、长度检查与压缩、数字可溯源。

照 AIHOT 的做法 —— **长度不合规用确定性裁剪，不发起第二次"修复"调用**
（`answerFirstSummaryLengthOk` / `compactAnswerFirstSummary`，见 docs/04 §2）。
"""

from __future__ import annotations

import html
import re

_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_URL = re.compile(r"https?://\S+")
_SENTENCE_SPLIT = re.compile(r"[。！？!?]+")
_CLAUSE_SPLIT = re.compile(r"[^，；：、,;:]+[，；：、,;:]?")
#: 数字、百分比、版本号、参数量、指标分数（摘要里出现就必须能在原文找到）
_NUMBER = re.compile(r"\d+(?:\.\d+)?%?|\bv?\d+\.\d+(?:\.\d+)?\b|\b\d+[BbMmKk]\b")


def clean_text(text: str | None) -> str:
    """去 script/style、去所有标签、去 URL、解实体、压空白（对齐 AIHOT 的清洗规则）。"""
    if not text:
        return ""
    value = _SCRIPT_STYLE.sub(" ", text)
    value = _TAG.sub(" ", value)
    value = _URL.sub(" ", value)
    value = html.unescape(value)
    value = value.replace("\u00a0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"[ \t]*\n[ \t]*", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def clamp_text(text: str, max_chars: int) -> str:
    """按**码点**截断（不是字节），超出加省略号。"""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def count_sentences(text: str) -> int:
    return len([part for part in _SENTENCE_SPLIT.split(text) if part.strip()])


def length_ok(
    summary: str,
    *,
    source_chars: int,
    min_chars: int = 80,
    max_chars: int = 160,
    max_sentences: int = 3,
) -> bool:
    """AIHOT 的规则：原文够长（>=500 字）时要求更严（>=min_chars 且至少 2 句）。"""
    trimmed = summary.strip()
    if not trimmed:
        return False
    rich = source_chars >= 500
    lower = min_chars if rich else 50
    sentences = count_sentences(trimmed)
    if len(trimmed) > max_chars or len(trimmed) < lower:
        return False
    if sentences > max_sentences:
        return False
    return not (rich and sentences < 2)


def compact_summary(summary: str, *, max_chars: int = 160) -> str:
    """确定性压缩：按句裁剪，单句过长则按小句裁剪，绝不在数字或名称中间切断。"""
    text = summary.strip()
    if len(text) <= max_chars:
        return text

    sentences = [part for part in re.split(r"(?<=[。！？!?])", text) if part.strip()]
    result = ""
    for sentence in sentences:
        if len(result + sentence) > max_chars:
            break
        result += sentence
        if len(result) >= 80:
            break
    if len(result) >= 50:
        return result.strip()

    clauses = _CLAUSE_SPLIT.findall(text) or [text]
    result = ""
    for clause in clauses:
        if len(result + clause) + 1 > max_chars:
            break
        result += clause
        if len(result) >= 80:
            break
    if len(result) >= 50:
        return result.rstrip("，；：、,;:") + "。"
    return clamp_text(text, max_chars)


def numbers_in(text: str) -> set[str]:
    return set(_NUMBER.findall(text or ""))


def ungrounded_numbers(summary: str, source: str) -> list[str]:
    """摘要里出现、但原文里找不到的数字 —— 反幻觉规则的**可执行版本**。"""
    allowed = numbers_in(source)
    return sorted(number for number in numbers_in(summary) if number not in allowed)
