"""`state/source_context.py`：候选来源的五条查询（按 id / 按 URL / 按主题判权 / 最近入库 / 未判权批次）。

前三条原本写在 `tools/source_context.py` 里（白名单 `tools/source_context.py: 3`），后两条原来在
`tools/source_grade.py`（白名单 `tools/source_grade.py: 3`）。
下沉时最容易走样的是**口径**，不是列：

- 判权那一路必须只留 `verdict IN (?, ?)` 里点名的那两档（`核心`/`相关`），且**按最近一次判权**
  排序 —— 写成 `ORDER BY g.created_at` 又不聚合，同一来源会出多行，地图上就重复挂同一个 id；
- `graded_at` 是别名，也是排序键：同一来源两条判权只出一行，且取**更晚**的那条；
- 最近入库按 `last_seen_at` 倒序（不是 `first_seen_at`）。

每个函数都钉空库（回 `None` / 空列表，不是编造一行）。
"""

from __future__ import annotations

from scholarscout.state import source_context
from scholarscout.state.db import Database

T0 = "2026-10-05T10:00:00+08:00"


def _source(db: Database, source_id: str, *, last_seen: str = T0, title: str | None = None) -> str:
    db.execute(
        "INSERT INTO sources (id, url, url_canonical, domain, title, source_type,"
        " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            source_id,
            f"https://{source_id}.test/a.pdf",
            f"https://{source_id}.test/a.pdf",
            f"{source_id}.test",
            title or f"来源 {source_id}",
            "paper",
            T0,
            last_seen,
        ),
    )
    return source_id


def _grade(
    db: Database,
    grade_id: str,
    source_id: str,
    *,
    topic: str = "教育智能体",
    verdict: str = "核心",
    created_at: str = T0,
) -> str:
    db.execute(
        "INSERT INTO source_grades (id, source_id, topic, tier, tier_label, authority,"
        " verdict, guard_failed, created_at) VALUES (?,?,?,?,?,?,?,0,?)",
        (grade_id, source_id, topic, 1, "一手权威", 1.0, verdict, created_at),
    )
    return grade_id


def test_source_by_id_empty_db_returns_none(db: Database) -> None:
    assert source_context.source_by_id(db, "src_nope") is None


def test_source_by_id_returns_row_and_unknown_is_none(db: Database) -> None:
    _source(db, "src_a", title="知识追踪")
    row = source_context.source_by_id(db, "src_a")
    assert row is not None
    assert row["id"] == "src_a"
    assert row["title"] == "知识追踪"
    assert source_context.source_by_id(db, "src_b") is None


def test_graded_sources_empty_db_is_empty_list(db: Database) -> None:
    assert (
        source_context.graded_sources_for_topic(
            db, topic="教育智能体", verdicts=("核心", "相关"), limit=25
        )
        == []
    )


def test_graded_sources_keeps_only_usable_verdicts_for_that_topic(db: Database) -> None:
    _source(db, "src_core")
    _source(db, "src_rel")
    _source(db, "src_out")
    _source(db, "src_other_topic")
    _grade(db, "g1", "src_core", verdict="核心")
    _grade(db, "g2", "src_rel", verdict="相关")
    _grade(db, "g3", "src_out", verdict="排除")
    _grade(db, "g4", "src_other_topic", topic="别的主题", verdict="核心")

    rows = source_context.graded_sources_for_topic(
        db, topic="教育智能体", verdicts=("核心", "相关"), limit=25
    )
    assert {row["id"] for row in rows} == {"src_core", "src_rel"}


def test_graded_sources_ordered_by_latest_grade_and_one_row_per_source(db: Database) -> None:
    _source(db, "src_a")
    _source(db, "src_b")
    _grade(db, "g1", "src_a", created_at="2026-10-01T00:00:00+08:00")
    _grade(db, "g2", "src_a", created_at="2026-10-09T00:00:00+08:00")
    _grade(db, "g3", "src_b", created_at="2026-10-05T00:00:00+08:00")

    rows = source_context.graded_sources_for_topic(
        db, topic="教育智能体", verdicts=("核心", "相关"), limit=25
    )
    assert [row["id"] for row in rows] == ["src_a", "src_b"]
    assert len(rows) == 2, "同一来源的两条判权只能出一行（GROUP BY s.id）"
    assert rows[0]["graded_at"] == "2026-10-09T00:00:00+08:00"


def test_graded_sources_respects_limit(db: Database) -> None:
    for i in range(3):
        _source(db, f"src_{i}")
        _grade(db, f"g_{i}", f"src_{i}", created_at=f"2026-10-0{i + 1}T00:00:00+08:00")

    rows = source_context.graded_sources_for_topic(
        db, topic="教育智能体", verdicts=("核心", "相关"), limit=2
    )
    assert len(rows) == 2


def test_recent_sources_empty_db_is_empty_list(db: Database) -> None:
    assert source_context.recent_sources(db, limit=25) == []


def test_recent_sources_ordered_by_last_seen_desc_and_limit(db: Database) -> None:
    _source(db, "src_old", last_seen="2025-01-01T00:00:00+08:00")
    _source(db, "src_new", last_seen="2026-10-09T00:00:00+08:00")
    _source(db, "src_mid", last_seen="2026-03-01T00:00:00+08:00")

    rows = source_context.recent_sources(db, limit=2)
    assert [row["id"] for row in rows] == ["src_new", "src_mid"]


def test_source_by_url_canonical_empty_db_returns_none(db: Database) -> None:
    assert source_context.source_by_url_canonical(db, "https://none.test/a.pdf") is None


def test_source_by_url_canonical_matches_stored_column_only(db: Database) -> None:
    """state 只认库里那一列：不做规范化，带 `?utm=` 的原文查不到就是查不到。"""
    _source(db, "src_a")
    row = source_context.source_by_url_canonical(db, "https://src_a.test/a.pdf")
    assert row is not None
    assert row["id"] == "src_a"
    assert (
        source_context.source_by_url_canonical(db, "https://src_a.test/a.pdf?utm_source=x") is None
    )


def test_source_by_url_canonical_picks_the_named_row_among_several(db: Database) -> None:
    _source(db, "src_a")
    _source(db, "src_b")
    _source(db, "src_c")
    row = source_context.source_by_url_canonical(db, "https://src_b.test/a.pdf")
    assert row is not None and row["id"] == "src_b"


def test_ungated_sources_empty_db_is_empty_list(db: Database) -> None:
    assert source_context.ungated_sources(db, limit=20) == []


def test_ungated_sources_keeps_only_null_tier_in_last_seen_order(db: Database) -> None:
    """`tier IS NULL` 才进批：已判权的（哪怕是空串 tier）不再重复判。"""
    _source(db, "src_new_ungated", last_seen="2026-10-09T00:00:00+08:00")
    _source(db, "src_old_ungated", last_seen="2025-01-01T00:00:00+08:00")
    _source(db, "src_graded", last_seen="2026-10-10T00:00:00+08:00")
    db.execute("UPDATE sources SET tier = 1 WHERE id = 'src_graded'")

    rows = source_context.ungated_sources(db, limit=20)
    assert [row["id"] for row in rows] == ["src_new_ungated", "src_old_ungated"]


def test_ungated_sources_respects_limit(db: Database) -> None:
    _source(db, "src_1", last_seen="2026-10-01T00:00:00+08:00")
    _source(db, "src_2", last_seen="2026-10-02T00:00:00+08:00")
    _source(db, "src_3", last_seen="2026-10-03T00:00:00+08:00")

    rows = source_context.ungated_sources(db, limit=2)
    assert [row["id"] for row in rows] == ["src_3", "src_2"]

    # 夹取不在这里做：state 照传（limit=0 就是 0 条），`max(1, …)` 留在工具层
    assert source_context.ungated_sources(db, limit=0) == []
