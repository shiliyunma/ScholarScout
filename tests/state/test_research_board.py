"""研究看板的读侧仓储（`state/research_board.py` / `state/support.py::list_checks`）。

只钉**领域行为**，不钉 SQL 原文（SQL 是 `state/` 的内部实现，改了列顺序不该让用例红）：
同一张表里"有正文/有摘要"要变成布尔值、年份未知的排最后、产物按时间倒序、
`cards` 与 `notes` 两档不重叠、坏 JSON 退化成空列表而不是抛。
"""

from __future__ import annotations

from scholarscout.state import research_board, support
from scholarscout.state.db import Database

T1 = "2026-10-01T00:00:00+08:00"
T2 = "2026-10-02T00:00:00+08:00"
T3 = "2026-10-03T00:00:00+08:00"


def _source(
    db: Database,
    source_id: str,
    *,
    year: int | None = None,
    tier: int = 2,
    title: str = "A Paper",
    source_type: str = "arxiv",
) -> str:
    db.execute(
        """
        INSERT INTO sources (id, url, url_canonical, domain, title, year, source_type, tier,
                             first_seen_at, last_seen_at)
        VALUES (?, ?, ?, 'arxiv.org', ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            f"https://arxiv.org/abs/{source_id}",
            f"https://arxiv.org/abs/{source_id}",
            title,
            year,
            source_type,
            tier,
            T1,
            T1,
        ),
    )
    return source_id


def _mention(db: Database, source_id: str, session_id: str = "ses_001") -> None:
    db.execute(
        "INSERT INTO source_mentions (source_id, session_id, created_at) VALUES (?, ?, ?)",
        (source_id, session_id, T1),
    )


def _document(db: Database, doc_id: str, *, source_id: str) -> str:
    db.execute(
        """
        INSERT INTO documents (id, source_id, title, sha256, chars, fetched_at)
        VALUES (?, ?, 'A Paper', ?, 1000, ?)
        """,
        (doc_id, source_id, f"sha-{doc_id}", T1),
    )
    return doc_id


def _summary(db: Database, summary_id: str, *, doc_id: str, source_id: str) -> str:
    db.execute(
        """
        INSERT INTO summaries (id, doc_id, source_id, kind, prompt_version, created_at)
        VALUES (?, ?, ?, 'paper', 'pv1', ?)
        """,
        (summary_id, doc_id, source_id, T1),
    )
    return summary_id


def _map(
    db: Database, map_id: str, *, topic: str, created_at: str, session_id: str = "ses_001"
) -> None:
    db.execute(
        "INSERT INTO topic_maps (id, session_id, topic, graph_json, created_at) VALUES (?, ?, ?, '{}', ?)",
        (map_id, session_id, topic, created_at),
    )


def _route(
    db: Database,
    route_id: str,
    *,
    topic: str,
    created_at: str,
    session_id: str = "ses_001",
    level: str = "beginner",
    weeks: int = 4,
) -> None:
    db.execute(
        """
        INSERT INTO learning_routes (id, session_id, topic, level, weeks, plan_json, created_at)
        VALUES (?, ?, ?, ?, ?, '{}', ?)
        """,
        (route_id, session_id, topic, level, weeks, created_at),
    )


def _note(
    db: Database,
    note_id: str,
    *,
    kind: str,
    title: str,
    created_at: str,
    session_id: str = "ses_001",
) -> None:
    db.execute(
        "INSERT INTO notes (id, session_id, kind, title, content, created_at) VALUES (?, ?, ?, ?, 'x', ?)",
        (note_id, session_id, kind, title, created_at),
    )


def _session(db: Database, session_id: str, *, title: str, updated_at: str) -> None:
    db.execute(
        "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (session_id, title, T1, updated_at),
    )


# --------------------------------------------------------------- session_sources


def test_session_sources_empty_when_session_has_nothing(db: Database) -> None:
    _source(db, "src_a", year=2023)
    assert research_board.session_sources(db, "ses_missing") == []


def test_session_sources_puts_unknown_year_last_then_newest_first(db: Database) -> None:
    """年份已知的排前面且倒序；年份未知（NULL）的排最后 —— 中间夹一级 id 兜底。"""
    _source(db, "src_old", year=2019)
    _source(db, "src_no_year", year=None)
    _source(db, "src_new", year=2024)
    _source(db, "src_other_session", year=2025)
    for source_id in ("src_old", "src_no_year", "src_new"):
        _mention(db, source_id)
    _mention(db, "src_other_session", "ses_002")

    rows = research_board.session_sources(db, "ses_001")

    assert [row["source_id"] for row in rows] == ["src_new", "src_old", "src_no_year"]
    assert rows[0]["year"] == 2024
    assert rows[2]["year"] is None


def test_session_sources_reports_has_text_and_has_summary(db: Database) -> None:
    """两列计数变成布尔值，且计数列本身不再出现在返回值里。"""
    _source(db, "src_doc", year=2024)
    _source(db, "src_bare", year=2024)
    _mention(db, "src_doc")
    _mention(db, "src_bare")
    _document(db, "doc_1", source_id="src_doc")
    _summary(db, "sum_1", doc_id="doc_1", source_id="src_doc")

    rows = {row["source_id"]: row for row in research_board.session_sources(db, "ses_001")}

    assert rows["src_doc"]["has_text"] is True
    assert rows["src_doc"]["has_summary"] is True
    assert rows["src_bare"]["has_text"] is False
    assert rows["src_bare"]["has_summary"] is False
    assert "docs" not in rows["src_doc"]
    assert "sums" not in rows["src_doc"]


def test_session_sources_separates_text_without_summary(db: Database) -> None:
    """有正文没摘要与「只有标题」必须分开：缺口层靠这两列判「弱证据」和「还没取正文」。

    `runtime/research_state.py::compute_gaps` 的两条缺口正好是这一对：`not has_summary` → 弱证据、
    `not has_text` → 只搜到没看正文。若这里把「有正文没摘要」也折成两列皆假，两条缺口就串味了
    （取过正文却被报成「还没看正文」）。
    """
    _source(db, "src_text", year=2024, source_type="github")
    _mention(db, "src_text")
    _document(db, "doc_1", source_id="src_text")

    rows = research_board.session_sources(db, "ses_001")

    assert len(rows) == 1
    row = rows[0]
    assert row["has_text"] is True
    assert row["has_summary"] is False
    # 缺口层还要读的三列：类型、域、年份，都跟着同一次查询回来（不用为判定再查一遍库）
    assert (row["source_type"], row["domain"], row["year"]) == ("github", "arxiv.org", 2024)


def test_session_sources_does_not_duplicate_a_source_mentioned_twice(db: Database) -> None:
    _source(db, "src_a", year=2024)
    _mention(db, "src_a")
    _mention(db, "src_a")

    rows = research_board.session_sources(db, "ses_001")

    assert [row["source_id"] for row in rows] == ["src_a"]


# ---------------------------------------------------------------- sources_by_ids


def test_sources_by_ids_returns_empty_for_no_ids(db: Database) -> None:
    _source(db, "src_a", year=2024)
    assert research_board.sources_by_ids(db, []) == {}
    assert research_board.sources_by_ids(db, ["", ""]) == {}, "空 id 不该拼出 IN () 这种坏 SQL"


def test_sources_by_ids_keys_by_id_and_flags_summary(db: Database) -> None:
    """键是字符串 id；摘要标记是布尔；不存在的 id 不出现在结果里（而不是占位 None）。"""
    _source(db, "src_sum", year=2024)
    _source(db, "src_plain", year=2022)
    _document(db, "doc_1", source_id="src_sum")
    _summary(db, "sum_1", doc_id="doc_1", source_id="src_sum")

    got = research_board.sources_by_ids(db, ["src_sum", "src_plain", "src_missing"])

    assert set(got) == {"src_sum", "src_plain"}
    assert got["src_sum"]["has_summary"] is True
    assert got["src_plain"]["has_summary"] is False
    assert got["src_plain"]["title"] == "A Paper"


def test_sources_by_ids_looks_across_sessions(db: Database) -> None:
    """来源不属于本会话（甚至没有任何 mention）也要能查到 —— 引用核查的证据行靠这条。"""
    _source(db, "src_far", year=2021)

    got = research_board.sources_by_ids(db, ["src_far"])

    assert list(got) == ["src_far"]


# ------------------------------------------------------------ session_artifacts


def test_session_artifacts_empty_session_is_three_empty_lists(db: Database) -> None:
    assert research_board.session_artifacts(db, "ses_missing") == {
        "maps": [],
        "routes": [],
        "notes": [],
    }


def test_session_artifacts_orders_each_kind_newest_first(db: Database) -> None:
    _map(db, "map_old", topic="旧主题", created_at=T1)
    _map(db, "map_new", topic="新主题", created_at=T3)
    _route(db, "route_old", topic="旧主题", created_at=T1, level="advanced", weeks=8)
    _route(db, "route_new", topic="新主题", created_at=T2)
    _note(db, "note_old", kind="note", title="旧笔记", created_at=T1)
    _note(db, "note_new", kind="summary", title="新摘要", created_at=T2)

    got = research_board.session_artifacts(db, "ses_001")

    assert [row["id"] for row in got["maps"]] == ["map_new", "map_old"]
    assert [row["id"] for row in got["routes"]] == ["route_new", "route_old"]
    assert [row["id"] for row in got["notes"]] == ["note_new", "note_old"]
    assert got["routes"][1]["level"] == "advanced"
    assert got["routes"][1]["weeks"] == 8
    assert got["notes"][0]["kind"] == "summary"


def test_session_artifacts_ignores_other_sessions(db: Database) -> None:
    _map(db, "map_mine", topic="我的", created_at=T1)
    _map(db, "map_theirs", topic="别人的", created_at=T2, session_id="ses_002")
    _route(db, "route_theirs", topic="别人的", created_at=T2, session_id="ses_002")
    _note(db, "note_theirs", kind="note", title="别人的", created_at=T2, session_id="ses_002")

    got = research_board.session_artifacts(db, "ses_001")

    assert [row["id"] for row in got["maps"]] == ["map_mine"]
    assert got["routes"] == []
    assert got["notes"] == []


# -------------------------------------------------------------- artifact_counts


def test_artifact_counts_on_empty_session_is_all_zero(db: Database) -> None:
    assert research_board.artifact_counts(db, "ses_missing") == {
        "maps": 0,
        "routes": 0,
        "notes": 0,
        "cards": 0,
    }


def test_artifact_counts_splits_cards_out_of_notes(db: Database) -> None:
    """`notes` 是非卡片那一档、`cards` 是 `kind='card'` 那一档，两者不重叠也不漏。"""
    _map(db, "map_1", topic="主题", created_at=T1)
    _map(db, "map_2", topic="主题", created_at=T2)
    _route(db, "route_1", topic="主题", created_at=T1)
    _note(db, "note_1", kind="note", title="笔记", created_at=T1)
    _note(db, "note_2", kind="summary", title="摘要", created_at=T2)
    _note(db, "card_1", kind="card", title="卡片", created_at=T1)
    _map(db, "map_other", topic="别人的", created_at=T1, session_id="ses_002")

    got = research_board.artifact_counts(db, "ses_001")

    assert got == {"maps": 2, "routes": 1, "notes": 2, "cards": 1}
    assert got["notes"] + got["cards"] == 3, "两档相加正好等于 notes 表里本会话的行数"


# --------------------------------------------------------------- recent_sessions


def test_recent_sessions_empty(db: Database) -> None:
    assert research_board.recent_sessions(db) == []


def test_recent_sessions_orders_by_updated_at_desc_and_honors_limit(db: Database) -> None:
    """只按 `updated_at` 倒序 —— 不看 `pinned`（看板要的是"最近在做什么"）。"""
    _session(db, "ses_old", title="旧的", updated_at=T1)
    _session(db, "ses_mid", title="中间", updated_at=T2)
    _session(db, "ses_new", title="新的", updated_at=T3)
    db.execute("UPDATE sessions SET pinned = 1 WHERE id = 'ses_old'")

    rows = research_board.recent_sessions(db)

    assert [row["session_id"] for row in rows] == ["ses_new", "ses_mid", "ses_old"]
    assert rows[0]["title"] == "新的"
    assert rows[0]["updated_at"] == T3

    assert [row["session_id"] for row in research_board.recent_sessions(db, limit=2)] == [
        "ses_new",
        "ses_mid",
    ]


# ------------------------------------------------------------------ list_checks


def _check(
    db: Database,
    check_id: str,
    *,
    session_id: str = "ses_001",
    artifact_kind: str = "topic_map",
    artifact_id: str = "tm_001",
    item_id: str = "n1",
    source_ids: list[str] | None = None,
    verdict: str = "supported",
) -> None:
    support.upsert_check(
        db,
        check_id=check_id,
        artifact_kind=artifact_kind,
        artifact_id=artifact_id,
        session_id=session_id,
        item_id=item_id,
        claim="知识追踪需要建模遗忘",
        source_ids=source_ids if source_ids is not None else ["src_a"],
        evidence_level="summary",
        verdict=verdict,
        reason="摘要里明说",
        model="stub-model",
        prompt_version="pv1",
        created_at=T1,
    )


def test_list_checks_empty_when_session_has_none(db: Database) -> None:
    assert support.list_checks(db, "ses_missing") == []


def test_list_checks_parses_source_ids_and_orders_by_kind_then_item(db: Database) -> None:
    _check(
        db,
        "ck_2",
        artifact_kind="route",
        artifact_id="rt_001",
        item_id="w1-1",
        source_ids=["src_b"],
    )
    _check(
        db,
        "ck_1",
        artifact_kind="topic_map",
        artifact_id="tm_001",
        item_id="n2",
        source_ids=["src_a", "src_b"],
    )
    _check(db, "ck_other", session_id="ses_002", item_id="n9")

    rows = support.list_checks(db, "ses_001")

    assert [(row["artifact_kind"], row["item_id"]) for row in rows] == [
        ("route", "w1-1"),
        ("topic_map", "n2"),
    ]
    assert rows[1]["source_ids"] == ["src_a", "src_b"]
    assert rows[0]["source_ids"] == ["src_b"]
    assert rows[0]["verdict"] == "supported"
    assert rows[0]["claim"] == "知识追踪需要建模遗忘"
    assert "source_ids_json" in rows[0], "原料列原样保留，调用方可能还要用"


def test_list_checks_degrades_broken_json_to_empty_list(db: Database) -> None:
    """一条判定把 ids 写坏了，只让那一行显示"没有可解析的来源"，不让整张看板抛。"""
    _check(db, "ck_ok", source_ids=["src_a"])
    db.execute(
        """
        INSERT INTO support_checks (id, artifact_kind, artifact_id, session_id, item_id, claim,
                                    source_ids_json, evidence_level, verdict, created_at)
        VALUES ('ck_bad', 'topic_map', 'tm_002', 'ses_001', 'n1', '坏行', '{not json', 'title',
                'unknown', ?)
        """,
        (T1,),
    )

    rows = support.list_checks(db, "ses_001")

    assert [row["source_ids"] for row in rows] == [["src_a"], []]
