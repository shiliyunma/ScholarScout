"""建库契约：表/索引/触发器齐全，外键与 WAL 生效，迁移幂等。

对应 docs/02 §11 的验收项。
"""

from __future__ import annotations

import sqlite3

import pytest

from scholarscout.state.db import DOMAIN_TABLES, Database
from scholarscout.state.migrate import apply_migrations, current_version, discover

from ..conftest import T0

EXPECTED_TABLES = set(DOMAIN_TABLES) | {
    "schema_version",
    "state_meta",
    "source_tier_rules",
    "source_grades",
    "summaries_fts",
    "messages_fts",
}

EXPECTED_TRIGGERS = {
    "summaries_fts_ai",
    "summaries_fts_ad",
    "summaries_fts_au",
    "messages_fts_ai",
    "messages_fts_ad",
    "messages_fts_au",
}


def test_all_expected_tables_exist(db: Database) -> None:
    missing = EXPECTED_TABLES - db.table_names()
    assert not missing, f"缺表：{sorted(missing)}"


def test_all_triggers_created(db: Database) -> None:
    missing = EXPECTED_TRIGGERS - db.trigger_names()
    assert not missing, f"缺触发器：{sorted(missing)}"


def test_fts_indexes_use_trigram(db: Database) -> None:
    for name in ("summaries_fts", "messages_fts"):
        sql = db.scalar("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,))
        assert sql is not None, f"{name} 不存在"
        assert "trigram" in str(sql), f"{name} 没用 trigram 分词器"


def test_foreign_keys_pragma_is_on(db: Database) -> None:
    assert db.scalar("PRAGMA foreign_keys") == 1


def test_journal_mode_is_wal(db: Database) -> None:
    assert str(db.scalar("PRAGMA journal_mode")).lower() == "wal"


def test_foreign_keys_are_enforced(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO documents (id, source_id, sha256, fetched_at)
            VALUES ('doc_orphan', 'src_not_exist', 'cafe', ?)
            """,
            (T0,),
        )


def test_cascade_delete_removes_documents_and_summaries(db: Database) -> None:
    db.execute(
        "INSERT INTO sources (id, url, url_canonical, domain, source_type, first_seen_at, last_seen_at)"
        " VALUES ('src_x', 'https://a.example/x', 'https://a.example/x', 'a.example', 'web', ?, ?)",
        (T0, T0),
    )
    db.execute(
        "INSERT INTO documents (id, source_id, sha256, fetched_at) VALUES ('doc_x', 'src_x', 'aa', ?)",
        (T0,),
    )
    db.execute(
        "INSERT INTO summaries (id, doc_id, source_id, kind, prompt_version, created_at)"
        " VALUES ('sum_x', 'doc_x', 'src_x', 'web', 'v1', ?)",
        (T0,),
    )
    db.execute("DELETE FROM sources WHERE id = 'src_x'")
    assert db.scalar("SELECT count(*) FROM documents") == 0
    assert db.scalar("SELECT count(*) FROM summaries") == 0


def test_migration_is_idempotent(db: Database) -> None:
    first = current_version(db)
    assert first == 7
    applied_again = apply_migrations(db)
    assert applied_again == [], "重复执行迁移不应再次应用"
    assert current_version(db) == first
    assert db.scalar("SELECT count(*) FROM schema_version") == 7


def test_migration_files_are_well_named() -> None:
    versions = [number for number, _ in discover()]
    assert versions == sorted(versions)
    assert versions == [1, 2, 3, 4, 5, 6, 7]


def test_tier_rules_are_seeded(db: Database) -> None:
    """分层规则是**数据**：迁移里就要有种子，建库即可用（docs/02 §3）。"""
    total = db.scalar_int("SELECT count(*) FROM source_tier_rules")
    assert total >= 30
    row = db.query_one(
        "SELECT tier, weight FROM source_tier_rules WHERE match_type = 'domain' AND pattern = 'arxiv.org'"
    )
    assert row is not None
    assert row["tier"] == 1
    assert (
        db.scalar_int("SELECT count(*) FROM source_tier_rules WHERE match_type = 'url_regex'") >= 1
    )


def test_target_limits_applied_version(tmp_path) -> None:
    database = Database(tmp_path / "partial.db")
    try:
        applied = apply_migrations(database, target=0)
        assert applied == []
        assert current_version(database) == 0
    finally:
        database.close()


def test_summary_unique_per_prompt_version(db: Database) -> None:
    from ..conftest import insert_document, insert_source, insert_summary

    insert_source(db)
    insert_document(db)
    insert_summary(db, prompt_version="v1")
    # 同文档同版本 -> 冲突
    with pytest.raises(sqlite3.IntegrityError):
        insert_summary(db, summary_id="sum_dup", prompt_version="v1")
    # 同文档换版本 -> 允许（多版本共存是刻意的）
    insert_summary(db, summary_id="sum_v2", prompt_version="v2")
    assert db.scalar("SELECT count(*) FROM summaries") == 2


def test_counts_empty_on_fresh_db(db: Database) -> None:
    counts = db.counts()
    assert set(counts) == set(DOMAIN_TABLES)
    assert all(value == 0 for value in counts.values())
