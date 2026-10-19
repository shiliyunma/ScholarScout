"""`state/support.py::upsert_check`：一条核查判定的一行（`(artifact_id, item_id)` 唯一）。

钉两件：列与值一一对应（`source_ids_json` 往返不丢），以及**同一条重审是覆盖、不是追加**
（否则"复核同一批产物不重复计费"这条就废了）。
"""

from __future__ import annotations

import json

from scholarscout.state import support
from scholarscout.state.db import Database


def _upsert(
    db: Database,
    *,
    check_id: str = "sup_001",
    verdict: str = "supported",
    reason: str = "摘要里明说",
    evidence_level: str = "summary",
) -> None:
    support.upsert_check(
        db,
        check_id=check_id,
        artifact_kind="topic_map",
        artifact_id="tm_001",
        session_id="sess_001",
        item_id="n1",
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
