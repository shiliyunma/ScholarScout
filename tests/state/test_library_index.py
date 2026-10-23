"""`state/library_index.py`：资料索引的两条读查询（原本写在 `library/index.py`，白名单 2 处）。

下沉时最容易走样的是**排序键**，不是列：

- 资料行 ``ORDER BY d.fetched_at DESC, d.rowid DESC``：同一时刻入库的必须按写入顺序**倒序** ——
  少了 ``rowid`` 兜底，SQLite 对相同排序键的次序不作保证，「RPC 重建的索引与迁移脚本重建的
  逐字节一致」这条性质就断了（`library/index.py` 的模块 docstring 明确要它逐字节一致）；
- 提及行 ``ORDER BY rowid`` **升序**且**不聚合**：同一来源被多次调研带出时两条都要回，去重保序
  是调用方的事（目录取第一条，索引列全部）。

每条都钉空库（回空列表，不是编一行）。
"""

from __future__ import annotations

from scholarscout.state import library_index
from scholarscout.state.db import Database

from ..conftest import T0

T1 = "2026-10-06T10:00:00+08:00"


def _source(db: Database, source_id: str, *, domain: str = "arxiv.org") -> str:
    db.execute(
        "INSERT INTO sources (id, url, url_canonical, domain, title, source_type,"
        " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            source_id,
            f"https://{domain}/abs/{source_id}",
            f"https://{domain}/abs/{source_id}",
            domain,
            f"来源 {source_id}",
            "arxiv",
            T0,
            T0,
        ),
    )
    return source_id


def _document(db: Database, doc_id: str, source_id: str, *, fetched_at: str = T0) -> str:
    db.execute(
        "INSERT INTO documents (id, source_id, title, sha256, chars, raw_path, text_path,"
        " extract_status, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            doc_id,
            source_id,
            f"资料 {doc_id}",
            (doc_id.encode().hex() + "0" * 64)[:64],
            10,
            f"data/raw/{doc_id}.pdf",
            f"data/text/{doc_id}.txt",
            "ok",
            fetched_at,
        ),
    )
    return doc_id


def _mention(db: Database, source_id: str, session_id: str, *, created_at: str = T0) -> None:
    db.execute(
        "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?,?,?,?)",
        (session_id, session_id, T0, T0),
    )
    db.execute(
        "INSERT INTO source_mentions (source_id, session_id, created_at) VALUES (?,?,?)",
        (source_id, session_id, created_at),
    )


def test_documents_with_sources_empty_db_returns_empty_list(db: Database) -> None:
    assert library_index.documents_with_sources(db) == []


def test_source_mentions_empty_db_returns_empty_list(db: Database) -> None:
    assert library_index.source_mentions(db) == []


def test_documents_with_sources_joins_source_columns(db: Database) -> None:
    """来源侧的 url/domain/source_type/meta_json 必须连出来：library 层靠它们推标题与年份。"""
    _source(db, "src_a", domain="arxiv.org")
    _document(db, "doc_a", "src_a")

    rows = library_index.documents_with_sources(db)

    assert len(rows) == 1
    row = rows[0]
    assert row["doc_id"] == "doc_a"
    assert row["source_id"] == "src_a"
    assert row["url"] == "https://arxiv.org/abs/src_a"
    assert row["domain"] == "arxiv.org"
    assert row["source_type"] == "arxiv"
    assert row["raw_path"] == "data/raw/doc_a.pdf"
    assert row["text_path"] == "data/text/doc_a.txt"
    assert row["fetched_at"] == T0


def test_documents_with_sources_orders_by_fetched_at_desc_then_rowid_desc(db: Database) -> None:
    """同一 ``fetched_at`` 的两条：**后写入的排前面**（``rowid DESC`` 兜底，不是不确定次序）。"""
    _source(db, "src_a")
    _source(db, "src_b")
    _document(db, "doc_old", "src_a", fetched_at=T1)
    _document(db, "doc_tie_first", "src_b", fetched_at=T0)
    _document(db, "doc_tie_last", "src_a", fetched_at=T0)

    ids = [row["doc_id"] for row in library_index.documents_with_sources(db)]

    assert ids == ["doc_old", "doc_tie_last", "doc_tie_first"]


def test_source_mentions_keeps_write_order_and_does_not_aggregate(db: Database) -> None:
    """同一来源的两次提及**两条都回**，按 ``rowid`` 升序（最早的在前）。"""
    _source(db, "src_a")
    _mention(db, "src_a", "ses_e")
    _mention(db, "src_a", "ses_l")
    _mention(db, "src_a", "ses_z")

    rows = library_index.source_mentions(db)

    assert [(row["source_id"], row["session_id"]) for row in rows] == [
        ("src_a", "ses_e"),
        ("src_a", "ses_l"),
        ("src_a", "ses_z"),
    ]


def test_documents_with_sources_column_list_is_exact(db: Database) -> None:
    """列清单逐字钉住 —— `library/migrate.py` 与 `library/index.py` **共用这一条**。

    迁移靠 ``raw_path`` / ``text_path`` / ``sha256`` / ``fetched_at`` / ``meta_json`` 决定搬去哪儿，
    索引靠同一批列 + 来源侧标题渲染；少一列就是一个 ``KeyError``，多一列是没人读的死重量。
    列顺序也钉：``SELECT d.*`` 这类写法一换，键序就变了。
    """
    _source(db, "src_a")
    _document(db, "doc_a", "src_a")

    row = library_index.documents_with_sources(db)[0]

    assert list(row) == [
        "doc_id",
        "doc_title",
        "sha256",
        "raw_path",
        "text_path",
        "fetched_at",
        "source_id",
        "source_title",
        "url",
        "domain",
        "source_type",
        "meta_json",
    ]


def test_source_mentions_orders_by_write_order_not_created_at(db: Database) -> None:
    """先写但 ``created_at`` 更晚的那条仍排在最前 —— 排序键是 ``rowid``。

    迁移用它取「每个来源最早那次会话」（`library/migrate.py::earliest_sessions` 的 ``setdefault``，
    第一条赢）。要是有人把排序改成 ``created_at``，目录名就会落到另一次会话上，文件得整体再搬一遍 ——
    而 `created_at` 是**抓到资料那一刻**，与「谁先把这份资料带进库」本来就不是一回事。
    """
    _source(db, "src_a")
    _mention(db, "src_a", "ses_written_first", created_at="2026-10-09T00:00:00+08:00")
    _mention(db, "src_a", "ses_written_last", created_at="2026-10-01T00:00:00+08:00")

    rows = library_index.source_mentions(db)

    assert [row["session_id"] for row in rows] == ["ses_written_first", "ses_written_last"]
