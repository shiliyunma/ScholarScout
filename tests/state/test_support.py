"""`state/support.py`：落库（`upsert_check`）与读侧五个查询。

钉两件（写）：列与值一一对应（`source_ids_json` 往返不丢），以及**同一条重审是覆盖、不是追加**
（否则"复核同一批产物不重复计费"这条就废了）。

钉三件（读）：这些 SQL 原本长在 `grading/support.py` 里，口径是"取哪些列 / 过滤什么 / 取最新一条"——
写错列会让证据文本悄悄变形，界面上看不出来（只是一段中文少了年份）。每条搬走的查询至少一条用例，
含空库、多条排序、按来源/产物过滤。
"""

from __future__ import annotations

import json

from scholarscout.state import support
from scholarscout.state.db import Database

from ..conftest import insert_source


def _upsert(
    db: Database,
    *,
    check_id: str = "sup_001",
    verdict: str = "supported",
    reason: str = "摘要里明说",
    evidence_level: str = "summary",
    artifact_id: str = "tm_001",
    item_id: str = "n1",
) -> None:
    support.upsert_check(
        db,
        check_id=check_id,
        artifact_kind="topic_map",
        artifact_id=artifact_id,
        session_id="sess_001",
        item_id=item_id,
        claim="知识追踪需要建模遗忘",
        source_ids=["src_001", "src_002"],
        evidence_level=evidence_level,
        verdict=verdict,
        reason=reason,
        model="stub-model",
        prompt_version="pv1",
        created_at="2026-10-01T00:00:00+08:00",
    )


def test_upsert_check_writes_every_column(db: Database) -> None:
    _upsert(db)
    row = db.query_one("SELECT * FROM support_checks WHERE artifact_id = ?", ("tm_001",))
    assert row is not None
    assert row["id"] == "sup_001"
    assert row["artifact_kind"] == "topic_map"
    assert row["session_id"] == "sess_001"
    assert row["item_id"] == "n1"
    assert row["claim"] == "知识追踪需要建模遗忘"
    assert json.loads(row["source_ids_json"]) == ["src_001", "src_002"]
    assert row["evidence_level"] == "summary"
    assert row["verdict"] == "supported"
    assert row["reason"] == "摘要里明说"
    assert row["model"] == "stub-model"
    assert row["prompt_version"] == "pv1"
    assert row["created_at"] == "2026-10-01T00:00:00+08:00"


def test_upsert_check_overwrites_the_same_item_instead_of_appending(db: Database) -> None:
    """同一个 (artifact, item) 重审 = 覆盖判定；行数仍是 1（重复审不重复计费）。"""
    _upsert(db)
    _upsert(
        db,
        check_id="sup_002",
        verdict="unsupported",
        reason="只在标题里出现",
        evidence_level="title",
    )

    rows = db.query("SELECT * FROM support_checks WHERE artifact_id = ?", ("tm_001",))
    assert len(rows) == 1, "同一 (artifact_id, item_id) 只该留一行"
    assert rows[0]["verdict"] == "unsupported"
    assert rows[0]["reason"] == "只在标题里出现"
    assert rows[0]["evidence_level"] == "title"


# ------------------------------------- 读侧：source_row / latest_source_summary


def _summary(
    db: Database,
    summary_id: str,
    source_id: str,
    *,
    created_at: str = "2026-10-01T00:00:00+08:00",
    text: str = "摘要正文",
    points_json: str = '["要点一"]',
) -> None:
    # 每篇摘要配一张文档：`documents` 上 (source_id, sha256) 唯一，sha 用 id 派生保证不撞
    db.execute(
        "INSERT INTO documents (id, source_id, title, sha256, chars, raw_path, text_path,"
        " extract_status, fetched_at) VALUES (?,?,?,?,?,'','', 'ok', '2026-10-01T00:00:00+08:00')",
        (f"doc_{summary_id}", source_id, "A Test Paper", summary_id, 100),
    )
    db.execute(
        "INSERT INTO summaries (id, doc_id, source_id, kind, title_zh, summary_short,"
        " points_json, prompt_version, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            summary_id,
            f"doc_{summary_id}",
            source_id,
            "paper",
            "中文标题",
            text,
            points_json,
            "pv1",
            created_at,
        ),
    )


def test_source_row_is_none_when_the_source_is_not_in_the_library(db: Database) -> None:
    """空库/查不到 → `None`（调用方据此退回"弱证据 + 库里没有这条来源"，不许抛）。"""
    assert support.source_row(db, "src_missing") is None


def test_source_row_returns_exactly_the_five_evidence_columns(db: Database) -> None:
    """多一列少一列都会改掉证据文本的形状（年份/域名都靠这五列拼）。"""
    insert_source(db, "src_001")
    db.execute("UPDATE sources SET year = 2025 WHERE id = ?", ("src_001",))
    row = support.source_row(db, "src_001")
    assert row is not None
    assert set(row) == {"id", "title", "url", "year", "source_type"}
    assert row["title"] == "A Test Paper"
    assert row["year"] == 2025
    assert row["source_type"] == "arxiv"


def test_latest_source_summary_is_none_when_the_source_has_no_summary(db: Database) -> None:
    insert_source(db, "src_001")
    assert support.latest_source_summary(db, "src_001") is None


def test_latest_source_summary_skips_blank_and_takes_the_newest(db: Database) -> None:
    """空摘要不算强证据，也不能把更晚的**空**行顶上来（`coalesce(...) != ''` 就是这条）。"""
    insert_source(db, "src_001")
    _summary(db, "sum_old", "src_001", created_at="2026-10-01T00:00:00+08:00", text="旧摘要")
    _summary(db, "sum_new", "src_001", created_at="2026-10-02T00:00:00+08:00", text="新摘要")
    _summary(db, "sum_blank", "src_001", created_at="2026-10-03T00:00:00+08:00", text="")

    row = support.latest_source_summary(db, "src_001")
    assert row is not None
    assert row["summary_short"] == "新摘要"
    assert set(row) == {"title_zh", "summary_short", "points_json"}


def test_latest_source_summary_is_scoped_to_one_source(db: Database) -> None:
    insert_source(db, "src_001")
    insert_source(db, "src_002", url="https://arxiv.org/abs/2501.00002")
    _summary(db, "sum_a", "src_001", text="甲篇摘要")
    _summary(db, "sum_b", "src_002", text="乙篇摘要")

    assert support.latest_source_summary(db, "src_001")["summary_short"] == "甲篇摘要"
    assert support.latest_source_summary(db, "src_002")["summary_short"] == "乙篇摘要"


# ------------------------------------- 读侧：topic_map_row / route_row


def test_topic_map_row_is_none_when_the_map_is_missing(db: Database) -> None:
    assert support.topic_map_row(db, "tpm_missing") is None


def test_topic_map_row_returns_the_raw_graph_json(db: Database) -> None:
    """**原文**返回：JSON 解析留在 grading 层，state 不替调用方猜形状。"""
    db.execute(
        "INSERT INTO topic_maps (id, session_id, topic, graph_json, created_at) VALUES (?,?,?,?,?)",
        ("tpm_001", "ses_001", "图算法", '{"nodes": [{"nid": "n1"}]}', "2026-10-01T00:00:00+08:00"),
    )
    row = support.topic_map_row(db, "tpm_001")
    assert row is not None
    assert set(row) == {"id", "graph_json"}
    assert row["graph_json"] == '{"nodes": [{"nid": "n1"}]}'


def test_route_row_is_none_when_the_route_is_missing(db: Database) -> None:
    assert support.route_row(db, "rt_missing") is None


def test_route_row_returns_the_raw_plan_json(db: Database) -> None:
    db.execute(
        "INSERT INTO learning_routes (id, session_id, topic, weeks, plan_json, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (
            "rt_001",
            "ses_001",
            "图算法",
            1,
            '{"weeks": [{"idx": 1, "items": []}]}',
            "2026-10-01T00:00:00+08:00",
        ),
    )
    row = support.route_row(db, "rt_001")
    assert row is not None
    assert set(row) == {"id", "plan_json"}
    assert row["plan_json"] == '{"weeks": [{"idx": 1, "items": []}]}'


# ------------------------------------- 读侧：checks_for_artifact


def test_checks_for_artifact_is_empty_on_an_empty_db(db: Database) -> None:
    assert support.checks_for_artifact(db, "tm_001") == []


def test_checks_for_artifact_orders_by_item_id_and_scopes_to_one_artifact(
    db: Database,
) -> None:
    """按 `item_id` 排序是报告与看板的读序；串到别的产物上会多出别人的判定。"""
    _upsert(db, check_id="sup_n2", item_id="n2")
    _upsert(db, check_id="sup_n1", item_id="n1")
    _upsert(db, check_id="sup_other", artifact_id="tm_002", item_id="n1")

    rows = support.checks_for_artifact(db, "tm_001")
    assert [row["item_id"] for row in rows] == ["n1", "n2"]
    assert {row["artifact_id"] for row in rows} == {"tm_001"}
    assert [row["id"] for row in rows] == ["sup_n1", "sup_n2"]
