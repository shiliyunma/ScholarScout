"""多轮诊断脚本的判据（`scripts/diagnose_turns.py`）。

为什么值得测：这个脚本是我们**衡量"第 2 轮是否真的更有用"的同一把尺子** ——
以后每做一个改进都用它复测。尺子本身的判据写错，就会得出"改进了"的假结论。

判据口径（改口径必须同时改测试）：
- 查询先归一化（去空格/标点、转小写）；
- 重复＝与**之前任何一条**查询的二字符组 Jaccard ≥ 阈值（默认 0.6）—— 中文里"换词不换靶"
  的重复靠完全相等是抓不到的，所以用字符组重叠度。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "diagnose_turns.py"
spec = importlib.util.spec_from_file_location("diagnose_turns", SCRIPT)
assert spec and spec.loader
diagnose = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnose)


def test_normalize_ignores_case_spaces_and_punctuation() -> None:
    assert diagnose._norm("LLM Wiki：知识库？") == diagnose._norm("llm wiki 知识库")


def test_identical_after_normalization_are_duplicates() -> None:
    assert (
        diagnose._similar(diagnose._norm("Karpathy LLM wiki"), diagnose._norm("karpathy llm WIKI"))
        == 1.0
    )


def test_rewording_same_target_is_flagged_as_duplicate() -> None:
    """换词不换靶：这两条指同一个文档，应判为重复（阈值 0.6）。"""
    left = diagnose._norm("Karpathy llm-wiki.md gist original methodology document")
    right = diagnose._norm("Karpathy llm-wiki methodology original document raw sources")
    assert diagnose._similar(left, right) >= 0.6


def test_genuinely_different_queries_stay_below_threshold() -> None:
    left = diagnose._norm("教育智能体 入门 资料")
    right = diagnose._norm("trigram 索引 短查询 兜底")
    assert diagnose._similar(left, right) < 0.6


@pytest.mark.parametrize("text", ["", "a", "中"])
def test_short_inputs_do_not_crash(text: str) -> None:
    assert diagnose._similar(text, text) >= 0.0


def test_query_of_reads_query_or_url() -> None:
    assert diagnose._query_of('{"query": "教育智能体"}') == "教育智能体"
    assert diagnose._query_of('{"url": "https://a.example/x.pdf"}') == "https://a.example/x.pdf"
    assert diagnose._query_of("") == ""
    assert diagnose._query_of("{坏 json") == ""
