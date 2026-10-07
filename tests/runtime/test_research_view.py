"""研究看板（B2 证据表 / B4 时间线 / B7 看板）的口径测试。

钉的是"数字可不可信"：
- 证据强度按**确定性规则**判（有摘要+已支持=强），不让模型打分；
- 没有年份的来源单列、**不进"近两年"的分母**；
- 没做过引用核查时**降级为"子问题×来源"并标注 `mode`**，不假装有核查结论。
"""

from __future__ import annotations

import json

import pytest

from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.runtime import research_state as rs
from scholarscout.runtime import research_view
from scholarscout.state.db import Database


def _add_source(
    db: Database,
    source_id: str,
    *,
    session_id: str = "ses_view",
    year: int | None = 2025,
    tier: int | None = 2,
    source_type: str = "arxiv",
    with_summary: bool = False,
    with_doc: bool = False,
) -> None:
    db.execute(
        "INSERT INTO sources (id, url, url_canonical, domain, title, source_type, tier, year,"
        " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            source_id,
            f"https://arxiv.org/{source_id}",
            f"https://arxiv.org/{source_id}",
            "arxiv.org",
            f"标题 {source_id}",
            source_type,
            tier,
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
                f"doc_{source_id}",
                source_id,
                "paper",
                "摘要",
                "t000",
                "2026-10-07T00:00:00+08:00",
            ),
        )


def _add_check(
    db: Database,
    *,
    item_id: str,
    claim: str,
    source_ids: list[str],
    evidence_level: str,
    verdict: str,
    session_id: str = "ses_view",
) -> None:
    db.execute(
        "INSERT INTO support_checks (id, artifact_kind, artifact_id, session_id, item_id, claim,"
        " source_ids_json, evidence_level, verdict, model, prompt_version, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"chk_{item_id}",
            "route",
            "rt_1",
            session_id,
            item_id,
            claim,
            json.dumps(source_ids),
            evidence_level,
            verdict,
            "stub",
            "p000",
            "2026-10-07T00:00:00+08:00",
        ),
    )


# ------------------------------------------------------------------ B4 时间线


def test_timeline_groups_by_year_and_flags_recent_gaps() -> None:
    sources = [
        {"source_id": "a", "year": 2025, "title": "A"},
        {"source_id": "b", "year": 2025, "title": "B"},
        {"source_id": "c", "year": 2023, "title": "C"},
        {"source_id": "d", "year": None, "title": "D"},
    ]
    view = research_view.build_timeline(sources, now_year=2026)
    assert [group["year"] for group in view["groups"]] == [2025, 2023, None]
    assert [item["source_id"] for item in view["groups"][0]["items"]] == ["a", "b"]
    assert view["known_years"] == [2025, 2023]
    # 2023 与 2025 之间的 2024 是**区间内部**空档；2026 就是"现在"，不算缺
    assert view["internal_gap_labels"] == ["2024"]
    assert view["missing_recent_years"] == []
    assert view["undated"] == 1


def test_timeline_reports_internal_gaps_as_ranges() -> None:
    """区间内部的空档也要报（2019–2023 一条都没有，不该只字不提）。

    只报**区间内部**：区间之外（比最新更早之前）的空白不叫缺口 —— 可能本来就没资料。
    """
    sources = [
        {"source_id": "new", "year": 2025, "title": "N"},
        {"source_id": "old", "year": 2018, "title": "O"},
    ]
    view = research_view.build_timeline(sources, now_year=2026)
    assert view["internal_gap_labels"] == ["2019–2024"]  # 2019..2024 之间一条都没有
    # 区间外的一律不提（比如 2017 及更早）
    assert "2017" not in " ".join(view["internal_gap_labels"])


def test_timeline_without_gaps_reports_none() -> None:
    sources = [{"source_id": "a", "year": 2025}, {"source_id": "b", "year": 2024}]
    view = research_view.build_timeline(sources, now_year=2026)
    assert view["missing_recent_years"] == [] and view["internal_gap_labels"] == []


def test_timeline_without_any_year_reports_nothing_missing() -> None:
    """全是未知年份 → 不编"缺哪年"（取不到就不猜，与 eval 的口径一致）。"""
    view = research_view.build_timeline([{"source_id": "a", "year": None}], now_year=2026)
    assert view["groups"] == [{"year": None, "items": [{"source_id": "a", "year": None}]}]
    assert view["known_years"] == [] and view["missing_recent_years"] == []


# ------------------------------------------------------------------ B2 证据表


def test_evidence_falls_back_to_ledger_with_explicit_mode(db: Database) -> None:
    _add_source(db, "src_a", with_summary=True)
    _add_source(db, "src_b")  # 只有标题
    state = rs.empty_state("目标")
    state["questions"] = [
        {"text": "有摘要的题", "status": "covered", "evidence": ["src_a"]},
        {"text": "只有标题的题", "status": "open", "evidence": ["src_b"]},
        {"text": "没证据的题", "status": "open", "evidence": []},
    ]
    sources = research_view._load_session_sources(db, "ses_view")
    view = research_view.build_evidence(db, "ses_view", state=state, sources=sources)

    assert view["mode"] == "ledger"  # 没做过引用核查 → 明确标注降级
    strengths = [row["strength"] for row in view["rows"]]
    assert strengths == ["中（有摘要）", "弱（只有标题）", "缺"]
    assert view["weak_count"] == 1
    assert view["rows"][0]["sources"][0]["title"] == "标题 src_a"


def test_evidence_uses_support_checks_when_present(db: Database) -> None:
    _add_source(db, "src_a", with_summary=True)
    _add_source(db, "src_b")
    _add_check(
        db,
        item_id="1",
        claim="有摘要且被支持",
        source_ids=["src_a"],
        evidence_level="summary",
        verdict="supported",
    )
    _add_check(
        db,
        item_id="2",
        claim="只有标题",
        source_ids=["src_b"],
        evidence_level="title",
        verdict="partial",
    )
    _add_check(
        db,
        item_id="3",
        claim="有摘要但对不上",
        source_ids=["src_a"],
        evidence_level="summary",
        verdict="unsupported",
    )
    _add_check(
        db,
        item_id="4",
        claim="引用了别的会话的来源",
        source_ids=["src_other"],
        evidence_level="summary",
        verdict="supported",
    )
    sources = research_view._load_session_sources(db, "ses_view")
    view = research_view.build_evidence(db, "ses_view", state={}, sources=sources)

    assert view["mode"] == "support_checks"
    by_item = {row["item_id"]: row for row in view["rows"]}
    assert by_item["1"]["strength"] == "强"
    assert by_item["2"]["strength"] == "弱"  # 只有标题
    assert by_item["3"]["strength"] == "弱"  # 摘要对不上
    assert by_item["4"]["sources"] == []  # 库里根本没这个 id → 不硬塞
    assert view["weak_count"] == 2 and view["total"] == 4


def test_evidence_is_empty_not_fake_for_a_fresh_session(db: Database) -> None:
    view = research_view.build_evidence(db, "ses_view", state=rs.empty_state(), sources=[])
    assert view["mode"] == "empty" and view["rows"] == [] and view["total"] == 0


# ------------------------------------------------------------------ B7 看板


def test_board_returns_every_section(db: Database) -> None:
    _add_source(db, "src_a", year=2026, with_summary=True)
    _add_source(db, "src_b", year=2024)
    state = rs.empty_state("搞懂 GraphRAG")
    state["questions"] = [{"text": "q1", "status": "open", "evidence": ["src_a"]}]
    rs.record_query(state, "GraphRAG 原理")
    rs.save_state(db, "ses_view", state)
    db.execute(
        "INSERT INTO learning_routes (id, session_id, topic, level, weeks, plan_json, version,"
        " created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("rt_1", "ses_view", "GraphRAG", "入门", 4, "{}", 2, "2026-10-07T00:00:00+08:00"),
    )

    board = research_view.build_board(db, "ses_view", now_year=2026)

    assert board["goal"] == "搞懂 GraphRAG"
    assert board["questions"] == [{"index": 1, "text": "q1", "status": "open"}]
    assert board["queries"] == ["GraphRAG 原理"]
    assert board["counts"]["sources"] == 2
    assert board["counts"]["with_summary"] == 1
    assert board["counts"]["without_text"] == 1
    assert board["counts"]["open_questions"] == 1
    assert board["artifacts"]["routes"][0]["version"] == 2
    assert board["timeline"]["known_years"] == [2026, 2024]
    assert board["evidence"]["mode"] == "ledger"
    assert board["gaps"]  # 有年份缺口/弱证据等


def test_board_on_a_fresh_session_does_not_crash(db: Database) -> None:
    board = research_view.build_board(db, "ses_none", now_year=2026)
    assert board["counts"]["sources"] == 0
    assert board["questions"] == [] and board["evidence"]["mode"] == "empty"
    assert board["artifacts"] == {"maps": [], "routes": [], "notes": []}


def test_board_rpc_requires_session_id(ctx: AppContext) -> None:
    from scholarscout.rpc.errors import RpcError

    with pytest.raises(RpcError):
        dispatch(ctx, "research.board", {})


def test_evidence_resolves_sources_outside_this_session(db: Database) -> None:
    """核查引用的来源可能是别的会话带出来的 —— 也要能解析出来。

    真机渲染核对时抓到：只在会话内解析时，23 条证据行里有 15 条显示"没有可解析的来源"，
    因为那些来源是生成产物那一轮带出来的、没被本会话 mention 过。
    """
    _add_source(db, "src_elsewhere", session_id="ses_other", year=2023, with_summary=False)
    _add_check(
        db,
        item_id="1",
        claim="引用了别会话的来源",
        source_ids=["src_elsewhere"],
        evidence_level="title",
        verdict="supported",
    )
    sources = research_view._load_session_sources(db, "ses_view")
    view = research_view.build_evidence(db, "ses_view", state={}, sources=sources)
    row = view["rows"][0]
    assert len(row["sources"]) == 1
    assert row["sources"][0]["source_id"] == "src_elsewhere"
    assert row["sources"][0]["year"] == 2023
