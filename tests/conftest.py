"""测试公共夹具：每个用例一个临时库，建表走真实的迁移路径（不打桩）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from scholarscout.state.db import Database
from scholarscout.state.migrate import apply_migrations

#: 固定时间，避免用例依赖当前时钟
T0 = "2026-10-05T10:00:00+08:00"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "state.db")
    apply_migrations(database)
    try:
        yield database
    finally:
        database.close()


def insert_source(
    db: Database, source_id: str = "src_test0001", *, url: str = "https://arxiv.org/abs/2501.00001"
) -> str:
    db.execute(
        """
        INSERT INTO sources (id, url, url_canonical, domain, title, source_type, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, 'arxiv.org', 'A Test Paper', 'arxiv', ?, ?)
        """,
        (source_id, url, url, T0, T0),
    )
    return source_id


def insert_document(
    db: Database, doc_id: str = "doc_test0001", source_id: str = "src_test0001"
) -> str:
    db.execute(
        """
        INSERT INTO documents (id, source_id, title, sha256, chars, raw_path, text_path,
                               extract_status, fetched_at)
        VALUES (?, ?, 'A Test Paper', 'deadbeef', 1234, 'data/raw/de/deadbeef.pdf',
                ?, 'ok', ?)
        """,
        (doc_id, source_id, f"data/text/{doc_id}.txt", T0),
    )
    return doc_id


def insert_summary(
    db: Database,
    summary_id: str = "sum_test0001",
    *,
    doc_id: str = "doc_test0001",
    source_id: str = "src_test0001",
    title_zh: str = "教育智能体的检索增强方法",
    summary_short: str = "该工作提出一种教育智能体框架，在 MATH 数据集上把准确率从 42% 提到 58%。",
    points_json: str | None = None,
    prompt_version: str = "abc123def456",
) -> str:
    db.execute(
        """
        INSERT INTO summaries (id, doc_id, source_id, kind, title_zh, summary_short, points_json,
                               model, prompt_version, created_at)
        VALUES (?, ?, ?, 'paper', ?, ?, ?, 'test-model', ?, ?)
        """,
        (
            summary_id,
            doc_id,
            source_id,
            title_zh,
            summary_short,
            points_json if points_json is not None else '["在 MATH 上 42% -> 58%"]',
            prompt_version,
            T0,
        ),
    )
    return summary_id


def insert_session(db: Database, session_id: str = "ses_test0001") -> str:
    db.execute(
        "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, '测试会话', ?, ?)",
        (session_id, T0, T0),
    )
    return session_id


def insert_message(
    db: Database,
    message_id: str = "msg_test0001",
    *,
    session_id: str = "ses_test0001",
    idx: int = 0,
    content: str = "帮我找教育智能体方向的入门资料",
) -> str:
    db.execute(
        """
        INSERT INTO messages (id, session_id, idx, role, content, created_at)
        VALUES (?, ?, ?, 'user', ?, ?)
        """,
        (message_id, session_id, idx, content, T0),
    )
    return message_id


@pytest.fixture
def seeded(db: Database) -> Database:
    """一条完整链路：source -> document -> summary，外加一个会话与一条消息。"""
    insert_source(db)
    insert_document(db)
    insert_summary(db)
    insert_session(db)
    insert_message(db)
    return db
