"""FTS 触发器契约：插入/更新/删除后索引必须立刻一致。

对应 docs/02 §11 的验收项；触发器是索引的唯一维护者，这里就是守它的门。
"""

from __future__ import annotations

from scholarscout.state.db import Database
from scholarscout.state.search import search_messages, search_summaries

OLD_TEXT = "这篇论文讨论了传统的检索方法，与智能体无关。"
NEW_TEXT = "这篇论文提出图检索增强的教育智能体，在 MATH 上准确率 58%。"


def _fts_rowids(db: Database, table: str) -> list[int]:
    return [int(row[0]) for row in db.query(f"SELECT rowid FROM {table}")]


def test_summary_insert_is_searchable(seeded: Database) -> None:
    hits = search_summaries(seeded, "智能体")
    assert [hit.id for hit in hits] == ["sum_test0001"]
    assert hits[0].path == "fts"


def test_summary_points_are_indexed(seeded: Database) -> None:
    hits = search_summaries(seeded, "MATH")
    assert hits, "points_json 里的内容也应可检索"


def test_summary_update_replaces_index(seeded: Database) -> None:
    seeded.execute("UPDATE summaries SET summary_short = ? WHERE id = 'sum_test0001'", (NEW_TEXT,))
    assert search_summaries(seeded, "图检索增强") != [], "更新后的新内容应能搜到"
    assert search_summaries(seeded, OLD_TEXT[:8]) == [], "更新前的旧内容不应再命中"


def test_summary_delete_drops_from_index(seeded: Database) -> None:
    assert search_summaries(seeded, "智能体") != []
    seeded.execute("DELETE FROM summaries WHERE id = 'sum_test0001'")
    assert search_summaries(seeded, "智能体") == []
    assert _fts_rowids(seeded, "summaries_fts") == []


def test_message_insert_is_searchable(seeded: Database) -> None:
    hits = search_messages(seeded, "入门资料")
    assert [hit.id for hit in hits] == ["msg_test0001"]
    assert hits[0].extra["session_id"] == "ses_test0001"


def test_message_update_replaces_index(seeded: Database) -> None:
    seeded.execute(
        "UPDATE messages SET content = '换个话题：微调与蒸馏的差别' WHERE id = 'msg_test0001'"
    )
    assert search_messages(seeded, "微调与蒸馏") != []
    assert search_messages(seeded, "入门资料") == []


def test_message_delete_drops_from_index(seeded: Database) -> None:
    seeded.execute("DELETE FROM messages WHERE id = 'msg_test0001'")
    assert search_messages(seeded, "入门资料") == []
    assert _fts_rowids(seeded, "messages_fts") == []


def test_cascade_delete_also_clears_summary_index(seeded: Database) -> None:
    seeded.execute("DELETE FROM sources WHERE id = 'src_test0001'")
    assert search_summaries(seeded, "智能体") == []
