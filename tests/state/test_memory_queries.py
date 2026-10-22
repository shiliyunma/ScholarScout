"""记忆抽取用的 state 读函数：`facts` / `sessions` / `state.memory` 各一条最小用例。

这些函数是从 `runtime/memory.py` 下沉的 SQL，钉的是**口径**（列、过滤、排序、去重），
不是实现细节：写错过滤（例如给用户轮数加上 `active = 1`）会让「该不该抽一次记忆」
的判断悄悄变形，界面上看不出来。
"""

from __future__ import annotations

from scholarscout.state import facts, memory, sessions
from scholarscout.state.db import Database

T0 = "2026-10-07T00:00:00+08:00"


def _session(db: Database, session_id: str = "sess_001") -> str:
    db.execute(
        "INSERT INTO sessions (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
        (session_id, "测试会话", "stub", T0, T0),
    )
    return session_id


def _message(
    db: Database,
    msg_id: str,
    *,
    session_id: str = "sess_001",
    idx: int,
    role: str,
    content: str,
    created_at: str = T0,
    active: int = 1,
) -> None:
    db.execute(
        "INSERT INTO messages (id, session_id, idx, role, content, active, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (msg_id, session_id, idx, role, content, active, created_at),
    )


def _fact(db: Database, fact_id: str, *, text: str, created_at: str, session_id: str = "sess_001"):
    facts.insert_fact(
        db,
        fact_id=fact_id,
        text=text,
        kind="fact",
        target="user",
        source_session=session_id,
        created_at=created_at,
    )


def test_list_facts_returns_full_rows_newest_first(db: Database) -> None:
    _fact(db, "mem_001", text="较早的记忆", created_at="2026-10-01T00:00:00+08:00")
    _fact(db, "mem_002", text="较新的记忆", created_at="2026-10-05T00:00:00+08:00")
    rows = facts.list_facts(db)
    assert [row["id"] for row in rows] == ["mem_002", "mem_001"]
    assert set(rows[0]) == {
        "id",
        "text",
        "kind",
        "target",
        "source_session",
        "created_at",
        "updated_at",
    }, "列清单变了会悄悄改掉 memory.list 的返回形状"


def test_fact_exists_only_true_for_present_id(db: Database) -> None:
    _fact(db, "mem_001", text="在写毕设", created_at=T0)
    assert facts.fact_exists(db, "mem_001") is True
    assert facts.fact_exists(db, "mem_999") is False


def test_count_facts_from_session_counts_only_that_session(db: Database) -> None:
    _fact(db, "mem_001", text="这条会话记的", created_at=T0, session_id="sess_001")
    _fact(db, "mem_002", text="别的会话记的", created_at=T0, session_id="sess_002")
    assert facts.count_facts_from_session(db, "sess_001") == 1
    assert facts.count_facts_from_session(db, "sess_none") == 0


def test_user_message_texts_takes_only_user_rows_in_created_at_order(db: Database) -> None:
    _session(db)
    _message(
        db, "msg_1", idx=0, role="user", content="第二句", created_at="2026-10-07T00:00:02+08:00"
    )
    _message(db, "msg_2", idx=1, role="assistant", content="助手的长回答")
    _message(
        db, "msg_3", idx=2, role="user", content="第一句", created_at="2026-10-07T00:00:01+08:00"
    )
    # 界面已隐藏的消息仍算「用户说过」：记忆判据不看 active
    _message(
        db,
        "msg_4",
        idx=3,
        role="user",
        content="被折叠的",
        created_at="2026-10-07T00:00:03+08:00",
        active=0,
    )
    texts = sessions.user_message_texts(db, "sess_001")
    assert texts == ["第一句", "第二句", "被折叠的"]


def test_count_user_messages_ignores_assistant_and_other_sessions(db: Database) -> None:
    _session(db)
    _session(db, "sess_002")
    _message(db, "msg_1", idx=0, role="user", content="a")
    _message(db, "msg_2", idx=1, role="assistant", content="b")
    _message(db, "msg_3", idx=2, role="user", content="c", active=0)
    _message(db, "msg_4", idx=0, session_id="sess_002", role="user", content="d")
    assert sessions.count_user_messages(db, "sess_001") == 2
    assert sessions.count_user_messages(db, "sess_none") == 0


def test_session_topics_unions_routes_and_topic_maps(db: Database) -> None:
    _session(db)
    db.execute(
        "INSERT INTO topic_maps (id, session_id, topic, graph_json, created_at) VALUES (?,?,?,?,?)",
        ("tm_1", "sess_001", "注意力机制", "{}", T0),
    )
    db.execute(
        "INSERT INTO learning_routes (id, session_id, topic, plan_json, created_at)"
        " VALUES (?,?,?,?,?)",
        ("rte_1", "sess_001", "注意力机制", "{}", T0),
    )
    db.execute(
        "INSERT INTO learning_routes (id, session_id, topic, plan_json, created_at)"
        " VALUES (?,?,?,?,?)",
        ("rte_2", "sess_001", "RAG", "{}", T0),
    )
    # 同主题不重复（UNION 去重）；别的会话不算
    db.execute(
        "INSERT INTO topic_maps (id, session_id, topic, graph_json, created_at) VALUES (?,?,?,?,?)",
        ("tm_2", "sess_002", "别的会话主题", "{}", T0),
    )
    assert sorted(memory.session_topics(db, "sess_001")) == ["RAG", "注意力机制"]
    assert memory.session_topics(db, "sess_none") == []


def test_count_session_artifacts_sums_three_tables(db: Database) -> None:
    _session(db)
    db.execute(
        "INSERT INTO topic_maps (id, session_id, topic, graph_json, created_at) VALUES (?,?,?,?,?)",
        ("tm_1", "sess_001", "注意力机制", "{}", T0),
    )
    db.execute(
        "INSERT INTO learning_routes (id, session_id, topic, plan_json, created_at)"
        " VALUES (?,?,?,?,?)",
        ("rte_1", "sess_001", "注意力机制", "{}", T0),
    )
    for note_id in ("note_1", "note_2"):
        db.execute(
            "INSERT INTO notes (id, session_id, kind, content, created_at) VALUES (?,?,?,?,?)",
            (note_id, "sess_001", "note", "正文", T0),
        )
    assert memory.count_session_artifacts(db, "sess_001") == 4
    assert memory.count_session_artifacts(db, "sess_none") == 0
