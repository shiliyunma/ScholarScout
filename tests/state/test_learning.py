"""学习产物仓储契约：主题地图、路线、笔记与卡片、反查。"""

from __future__ import annotations

import pytest

from scholarscout.state.db import Database
from scholarscout.state.learning import (
    get_note,
    get_route,
    get_topic_map,
    latest_route,
    latest_topic_map,
    list_notes,
    notes_for_source,
    save_note,
    save_route,
    save_topic_map,
)

GRAPH = {
    "nodes": [{"nid": "n1", "label": "检索增强生成", "kind": "method", "source_ids": ["src_a"]}],
    "edges": [],
    "stats": {"nodes": 1, "edges": 0},
}
PLAN = {
    "weeks": [
        {
            "idx": 1,
            "goal": "能跑起来",
            "checkpoint": "能解释每一步",
            "items": [
                {
                    "kind": "doc",
                    "title": "官方文档",
                    "source_id": "src_a",
                    "why": "先跑通",
                    "est_minutes": 60,
                }
            ],
        }
    ],
    "stats": {"weeks": 1, "items": 1},
}


def test_topic_map_roundtrip(db: Database) -> None:
    topic_id = save_topic_map(db, topic="教育智能体", graph=GRAPH, session_id="ses_x")
    assert topic_id.startswith("tpm_")

    payload = get_topic_map(db, topic_id)
    assert payload is not None
    assert payload["topic"] == "教育智能体"
    assert payload["graph"] == GRAPH
    assert payload["session_id"] == "ses_x"
    assert get_topic_map(db, "tpm_missing") is None


def test_latest_topic_map_picks_newest(db: Database) -> None:
    first = save_topic_map(db, topic="T", graph=GRAPH)
    second = save_topic_map(db, topic="T", graph={**GRAPH, "mode": "model"})
    assert first != second
    latest = latest_topic_map(db, "T")
    assert latest is not None and latest["id"] == second
    assert latest["graph"]["mode"] == "model"
    assert latest_topic_map(db, "不存在") is None


def test_route_roundtrip_and_meta(db: Database) -> None:
    topic_id = save_topic_map(db, topic="T", graph=GRAPH)
    route_id = save_route(db, topic="T", plan=PLAN, level="入门", weeks=1, topic_map_id=topic_id)
    assert route_id.startswith("rte_")

    payload = get_route(db, route_id)
    assert payload is not None
    assert payload["level"] == "入门"
    assert payload["weeks"] == 1
    assert payload["topic_map_id"] == topic_id
    assert payload["plan"]["weeks"][0]["checkpoint"] == "能解释每一步"
    assert get_route(db, "rte_missing") is None
    latest = latest_route(db, "T")
    assert latest is not None and latest["id"] == route_id


def test_route_rejects_unknown_topic_map(db: Database) -> None:
    with pytest.raises(Exception, match="FOREIGN KEY"):
        save_route(db, topic="T", plan=PLAN, topic_map_id="tpm_不存在")


def test_note_roundtrip_and_list(db: Database) -> None:
    card_id = save_note(
        db,
        kind="card",
        title="什么是 RAG",
        content="先检索再生成",
        refs={"source_ids": ["src_aaa"]},
        tags=["检索"],
        path="data/notes/not_x.md",
        session_id="ses_x",
    )
    note_id = save_note(db, kind="note", title="随手记", content="正文")

    card = get_note(db, card_id)
    assert card is not None
    assert card["refs"] == {"source_ids": ["src_aaa"]}
    assert card["tags"] == ["检索"]
    assert card["path"] == "data/notes/not_x.md"

    assert [row["id"] for row in list_notes(db, kind="card")] == [card_id]
    assert len(list_notes(db)) == 2
    assert note_id in {row["id"] for row in list_notes(db, kind="note")}
    assert get_note(db, "not_missing") is None


def test_note_rejects_unknown_kind(db: Database) -> None:
    with pytest.raises(ValueError, match="非法笔记类型"):
        save_note(db, kind="diary", content="x")


def test_notes_for_source_reverse_lookup(db: Database) -> None:
    save_note(db, kind="note", content="A", refs={"source_ids": ["src_aaa"]})
    save_note(db, kind="note", content="B", refs={"source_ids": ["src_bbb"]})
    save_note(db, kind="note", content="C", refs={"doc_ids": ["src_aaa_doc"]})

    hits = notes_for_source(db, "src_aaa")
    assert len(hits) == 1
    assert hits[0]["content"] == "A"
    assert notes_for_source(db, "src_zzz") == []
