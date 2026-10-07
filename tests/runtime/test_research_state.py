"""A1 研究台账 / A2 缺口 / A3 查询判重 —— 口径测试。

这些测试钉的不是"代码能跑"，而是**数字可不可信**：
判重阈值只有一把尺子（与 scripts/diagnose_turns.py 共用）、解析不出来的状态标记不改任何东西、
缺口的每一种都从库里的可真值算出来。
"""

from __future__ import annotations

import pytest

from scholarscout.runtime import research_state as rs
from scholarscout.state.db import Database


def _add_source(
    db: Database,
    source_id: str,
    *,
    session_id: str = "ses_1",
    source_type: str = "arxiv",
    domain: str = "arxiv.org",
    year: int | None = 2024,
    with_summary: bool = False,
    with_doc: bool = False,
) -> None:
    db.execute(
        "INSERT INTO sources (id, url, url_canonical, domain, title, source_type, year,"
        " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            source_id,
            f"https://{domain}/{source_id}",
            f"https://{domain}/{source_id}",
            domain,
            f"标题 {source_id}",
            source_type,
            year,
            "2026-10-07T00:00:00+08:00",
            "2026-10-07T00:00:00+08:00",
        ),
    )
    db.execute(
        "INSERT INTO source_mentions (source_id, session_id, created_at) VALUES (?,?,?)",
        (source_id, session_id, "2026-10-07T00:00:00+08:00"),
    )
    if with_doc or with_summary:
        # summaries.doc_id 是 NOT NULL，所以有摘要就必有文档
        db.execute(
            "INSERT INTO documents (id, source_id, sha256, fetched_at) VALUES (?,?,?,?)",
            (f"doc_{source_id}", source_id, "0" * 64, "2026-10-07T00:00:00+08:00"),
        )
    if with_summary:
        db.execute(
            "INSERT INTO summaries (id, doc_id, source_id, kind, summary_short, prompt_version,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (
                f"sum_{source_id}",
                f"doc_{source_id}" if with_doc else None,
                source_id,
                "paper",
                "摘要内容",
                "test0000",
                "2026-10-07T00:00:00+08:00",
            ),
        )


# --------------------------------------------------------------- A3 判重（尺子只有一把）


def test_similarity_catches_word_swaps() -> None:
    """中文里"换词不换靶"必须抓得到 —— 完全相等匹配是抓不到的。"""
    assert rs.similarity("LLM Wiki 是什么", "什么是 LLM Wiki") > 0.6
    assert rs.similarity("介绍 openclaw", "openclaw 介绍") > 0.6
    assert rs.similarity("RAG 的评测方法", "东南大学计算机考研分数线") < 0.2
    assert rs.similarity("", "任意") == 0.0


def test_find_repeat_returns_previous_query() -> None:
    state = rs.empty_state("目标")
    rs.record_query(state, "LLM Wiki 是什么")
    assert rs.find_repeat(state, "什么是 LLM Wiki") == "LLM Wiki 是什么"
    assert rs.find_repeat(state, "GraphRAG 的实现") is None
    # 空查询不算重复（工具层另有参数校验）
    assert rs.find_repeat(state, "   ") is None


def test_record_query_is_idempotent() -> None:
    state = rs.empty_state("目标")
    rs.record_query(state, "同一个查询")
    rs.record_query(state, "同一个查询")
    assert state["used_queries"] == ["同一个查询"]


# --------------------------------------------------------------- A1 台账存取


def test_state_roundtrip_and_unknown_session(db: Database) -> None:
    assert rs.load_state(db, "ses_missing") is None
    state = rs.empty_state("调研 LLM Wiki")
    state["questions"] = [{"text": "是什么", "status": "open", "evidence": []}]
    rs.save_state(db, "ses_1", state)
    again = rs.load_state(db, "ses_1")
    assert again is not None
    assert again["goal"] == "调研 LLM Wiki"
    assert again["questions"][0]["text"] == "是什么"
    # 覆盖写：同一个会话只有一行
    rs.save_state(db, "ses_1", {**state, "goal": "改过的目标"})
    assert db.query_one("SELECT count(*) AS n FROM research_states")["n"] == 1
    assert rs.load_state(db, "ses_1")["goal"] == "改过的目标"


def test_load_state_survives_corrupt_payload(db: Database) -> None:
    """坏 JSON 不能让整个会话打不开 —— 回 None，按新台账重来。"""
    db.execute(
        "INSERT INTO research_states (session_id, payload_json, updated_at) VALUES (?,?,?)",
        ("ses_bad", "{不是 json", "2026-10-07T00:00:00+08:00"),
    )
    assert rs.load_state(db, "ses_bad") is None


def test_new_sources_counts_only_fresh() -> None:
    state = rs.empty_state("目标")
    assert rs.new_sources(state, ["a", "b"]) == ["a", "b"]
    assert rs.new_sources(state, ["b", "c"]) == ["c"]
    assert rs.new_sources(state, []) == []


def test_apply_coverage_marks_only_what_model_confirmed() -> None:
    state = rs.empty_state("目标")
    state["questions"] = [
        {"text": "q1", "status": "open", "evidence": []},
        {"text": "q2", "status": "open", "evidence": []},
        {"text": "q3", "status": "open", "evidence": []},
    ]
    marker = '<!--research-state:{"covered":[1,3],"open":[2]}-->'
    assert rs.apply_coverage(state, f"正文\n{marker}", ["src_1"]) is True
    assert state["questions"][0]["status"] == "covered"
    assert state["questions"][0]["evidence"] == ["src_1"]
    assert state["questions"][1]["status"] == "open"
    assert state["questions"][2]["status"] == "covered"
    assert rs.strip_marker(f"正文\n{marker}") == "正文"


@pytest.mark.parametrize(
    "text",
    ["没有标记", '<!--research-state:{"covered": -->', "<!--research-state:[1,2]-->"],
)
def test_apply_coverage_leaves_state_alone_when_unparseable(text: str) -> None:
    """解析不出来就什么都不改 —— 猜覆盖会让缺口清单失效（这是最坏的静默错误）。"""
    state = rs.empty_state("目标")
    state["questions"] = [{"text": "q1", "status": "open", "evidence": []}]
    assert rs.apply_coverage(state, text, ["src_1"]) is False
    assert state["questions"][0] == {"text": "q1", "status": "open", "evidence": []}


def test_parse_decompose_accepts_fence_and_bare_json() -> None:
    fenced = '```json\n{"goal": "学 RAG", "questions": ["是什么", "怎么建"]}\n```'
    assert rs.parse_decompose(fenced) == {"goal": "学 RAG", "questions": ["是什么", "怎么建"]}
    bare = '{"goal": "g", "questions": ["a", "b", "c", "d", "e", "f", "g", "h"]}'
    parsed = rs.parse_decompose(bare)
    assert parsed is not None and len(parsed["questions"]) == rs.MAX_QUESTIONS
    assert rs.parse_decompose("模型跑题了，什么也没输出") is None
    assert rs.parse_decompose('{"goal": "g", "questions": []}') is None


# --------------------------------------------------------------- A2 缺口


def test_gaps_open_questions_weak_evidence_and_not_fetched(db: Database) -> None:
    _add_source(db, "src_1", with_summary=False)
    state = rs.empty_state("调研")
    state["questions"] = [{"text": "怎么建", "status": "open", "evidence": []}]
    gaps = {gap["kind"] for gap in rs.compute_gaps(db, "ses_1", state, now_year=2026)}
    assert "open_questions" in gaps
    assert "weak_evidence" in gaps  # 只有标题没有摘要
    assert "not_fetched" in gaps  # 搜到但没取正文
    assert "year_gap" in gaps  # 2024 < 2026-1
    assert "kind_gap" in gaps  # 全是 arxiv


def test_gaps_disappear_when_evidence_is_solid(db: Database) -> None:
    for index in range(4):
        _add_source(
            db,
            f"src_{index}",
            source_type="github" if index else "arxiv",
            domain=f"d{index}.org",
            year=2026,
            with_summary=True,
            with_doc=True,
        )
    state = rs.empty_state("调研")
    state["questions"] = [{"text": "已覆盖的题", "status": "covered", "evidence": ["src_0"]}]
    gaps = {gap["kind"] for gap in rs.compute_gaps(db, "ses_1", state, now_year=2026)}
    assert gaps == set()


def test_gaps_only_see_this_session(db: Database) -> None:
    _add_source(db, "src_other", session_id="ses_2")
    state = rs.empty_state("调研")
    assert rs.compute_gaps(db, "ses_1", state, now_year=2026) == []


def test_render_note_carries_goal_queries_and_gaps() -> None:
    state = rs.empty_state("搞懂 GraphRAG")
    rs.record_query(state, "GraphRAG 论文")
    state["questions"] = [
        {"text": "它比 RAG 强在哪", "status": "open", "evidence": []},
        {"text": "怎么上手", "status": "covered", "evidence": ["s1"]},
    ]
    note = rs.render_note(state, [{"kind": "year_gap", "detail": "缺近两年", "suggest": "加年份"}])
    assert "搞懂 GraphRAG" in note
    assert "GraphRAG 论文" in note
    assert "1.它比 RAG 强在哪（缺口）" in note
    assert "2.怎么上手（已覆盖）" in note
    assert "加年份" in note


def test_render_note_says_so_when_no_gap() -> None:
    state = rs.empty_state("目标")
    note = rs.render_note(state, [])
    assert "暂时没有明显缺口" in note
    assert "不要换词重搜" in note or "如实说明" in note
