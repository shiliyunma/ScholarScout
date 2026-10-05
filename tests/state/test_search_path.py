"""检索路径契约：>=3 字走 FTS，<3 字必须走 LIKE 兜底。

这是 docs/02 §11 的第二条硬验收项：
**搜"教育"（两字）必须能命中** —— 过不了说明没实现兜底。
"""

from __future__ import annotations

import pytest

from scholarscout.state.db import Database
from scholarscout.state.search import (
    FTS_MIN_QUERY_CHARS,
    _like_pattern,
    normalize_query,
    query_path,
    search_all,
    search_messages,
    search_summaries,
)


def test_query_path_threshold() -> None:
    assert FTS_MIN_QUERY_CHARS == 3
    assert query_path("教") == "like"
    assert query_path("教育") == "like"
    assert query_path("智能体") == "fts"
    assert query_path("RAG") == "fts"


def test_normalize_query_collapses_whitespace() -> None:
    assert normalize_query("  智能体 \n 教育 ") == "智能体 教育"


def test_like_pattern_escapes_wildcards() -> None:
    assert _like_pattern("100%") == "%100\\%%"
    assert _like_pattern("a_b") == "%a\\_b%"
    assert _like_pattern("back\\slash") == "%back\\\\slash%"


def test_two_char_query_hits_via_like(seeded: Database) -> None:
    hits = search_summaries(seeded, "教育")
    assert hits, "两字查询必须能命中（否则就是没实现 LIKE 兜底）"
    assert {hit.path for hit in hits} == {"like"}


def test_three_char_query_hits_via_fts(seeded: Database) -> None:
    hits = search_summaries(seeded, "智能体")
    assert hits
    assert {hit.path for hit in hits} == {"fts"}


def test_like_wildcards_do_not_match_everything(seeded: Database) -> None:
    assert search_summaries(seeded, "%_") == []


def test_empty_and_blank_queries_return_empty(seeded: Database) -> None:
    assert search_summaries(seeded, "") == []
    assert search_summaries(seeded, "   ") == []
    assert search_messages(seeded, "") == []


def test_fts_special_characters_do_not_raise(seeded: Database) -> None:
    for query in ['"智能体', "智能体*", "a-b-c", "(){}[]"]:
        search_summaries(seeded, query)


def test_zero_or_negative_limit_returns_empty(seeded: Database) -> None:
    assert search_summaries(seeded, "教育", limit=0) == []
    assert search_messages(seeded, "资料", limit=-1) == []


def test_messages_two_char_query_hits_via_like(seeded: Database) -> None:
    hits = search_messages(seeded, "资料")
    assert hits and {hit.path for hit in hits} == {"like"}


def test_messages_session_filter(seeded: Database) -> None:
    assert search_messages(seeded, "资料", session_id="ses_test0001") != []
    assert search_messages(seeded, "资料", session_id="ses_other") == []


def test_search_all_shape(seeded: Database) -> None:
    result = search_all(seeded, "教育")
    assert set(result) == {"summaries", "messages"}
    assert result["summaries"]
    assert result["messages"]


@pytest.mark.parametrize("query", ["教育", "智能体", "MATH", "该工作提出"])
def test_hit_is_json_friendly(seeded: Database, query: str) -> None:
    for hit in search_summaries(seeded, query):
        payload = hit.as_dict()
        assert payload["kind"] == "summary"
        assert payload["path"] in {"fts", "like"}
        assert isinstance(payload["snippet"], str) and payload["snippet"]
