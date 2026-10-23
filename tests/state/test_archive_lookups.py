"""`state/archive.py` 的四个只读查询：搜索命中行 / 最早会话 / 下载上下文两路。

为什么单独钉这四条：它们原本写在 `rpc/archive.py` 里（白名单 `rpc/archive.py: 4`），
下沉时最容易走样的不是列，而是**口径**：

- 搜索命中行必须是「摘要 JOIN 它的资料」，不是「摘要表自己的行」——否则界面上资料路径全空；
- 「这份资料最早被哪个会话带出来」按 **写入顺序**（`rowid`），不是按 `created_at` 或会话 id 排序；
- 下载上下文两路（`doc_id` / `source_id`）回的列是回包形状的一部分：
  少一列上游就拿 `KeyError`，多一列上游 `_target_row` 也不看，但**列名一改**回包就变。

空库一律回 `None`（不是 `{}`）：上游 `if row is None` 判的是「这条不存在」。
"""

from __future__ import annotations

from scholarscout.state import archive
from scholarscout.state.db import Database

T0 = "2026-10-05T10:00:00+08:00"


def _source(db: Database, source_id: str = "src_a") -> str:
    db.execute(
        "INSERT INTO sources (id, url, url_canonical, domain, title, source_type,"
        " meta_json, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            source_id,
            f"https://{source_id}.test/a.pdf",
            f"https://{source_id}.test/a.pdf",
            f"{source_id}.test",
            f"来源 {source_id}",
            "paper",
            '{"published": "2025-03"}',
            T0,
            T0,
        ),
    )
    return source_id


def _document(
    db: Database,
    doc_id: str = "doc_a",
    *,
    source_id: str = "src_a",
    raw_path: str | None = "raw/a.pdf",
    mime: str | None = "application/pdf",
    http_status: int | None = 200,
) -> str:
    db.execute(
        "INSERT INTO documents (id, source_id, title, sha256, raw_path, text_path,"
        " extract_status, fetched_at, mime, http_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            doc_id,
            source_id,
            f"正文 {doc_id}",
            f"sha-{doc_id}",
            raw_path,
            f"text/{doc_id}.txt",
            "ok",
            T0,
            mime,
            http_status,
        ),
    )
    return doc_id


def _summary(db: Database, summary_id: str = "sum_a", *, doc_id: str = "doc_a") -> str:
    db.execute(
        "INSERT INTO summaries (id, doc_id, source_id, kind, title_zh, summary_short,"
        " prompt_version, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            summary_id,
            doc_id,
            "src_a",
            "paper",
            f"中文标题 {summary_id}",
            f"摘要 {summary_id}",
            "pv1",
            T0,
        ),
    )
    return summary_id


def _mention(db: Database, *, source_id: str, session_id: str, created_at: str) -> None:
    db.execute(
        "INSERT INTO source_mentions (source_id, session_id, created_at) VALUES (?,?,?)",
        (source_id, session_id, created_at),
    )


# ------------------------------------------------------------------ 搜索命中行


def test_summary_hit_row_joins_the_summary_with_its_document(db: Database) -> None:
    """命中行同时带摘要三列与资料路径/状态 —— 界面靠它们决定点不点得开原文。"""
    _source(db)
    _document(db, raw_path="raw/real.pdf")
    _summary(db, "sum_hit")

    row = archive.summary_hit_row(db, "sum_hit")

    assert row is not None
    assert set(row) == {
        "doc_id",
        "source_id",
        "title_zh",
        "summary_short",
        "raw_path",
        "text_path",
        "extract_status",
    }, "列清单是回包形状的一部分"
    assert row["doc_id"] == "doc_a"
    assert row["source_id"] == "src_a"
    assert row["title_zh"] == "中文标题 sum_hit"
    assert row["summary_short"] == "摘要 sum_hit"
    assert row["raw_path"] == "raw/real.pdf"
    assert row["text_path"] == "text/doc_a.txt"
    assert row["extract_status"] == "ok"


def test_summary_hit_row_is_none_on_empty_db_and_unknown_id(db: Database) -> None:
    """空库 / 查不到都回 `None`（不是空 dict）：上游 `if row is not None` 才敢跳过这一条。"""
    assert archive.summary_hit_row(db, "sum_missing") is None

    _source(db)
    _document(db)
    _summary(db, "sum_a")
    assert archive.summary_hit_row(db, "sum_missing") is None


# ------------------------------------------------------------------ 最早会话


def test_first_session_for_source_follows_insert_order_not_created_at(db: Database) -> None:
    """按 `rowid` 取**最早写入**那一次 —— 与 `list_documents` 的分组口径同一套。"""
    _source(db, "src_x")
    # 先写入的会话 created_at 反而更晚：证明口径是写入顺序，不是时间戳排序
    _mention(db, source_id="src_x", session_id="ses_first", created_at="2026-10-09T00:00:00+08:00")
    _mention(db, source_id="src_x", session_id="ses_second", created_at="2026-10-01T00:00:00+08:00")

    assert archive.first_session_for_source(db, "src_x") == "ses_first"


def test_first_session_for_source_is_none_without_mentions(db: Database) -> None:
    """空库、以及「有来源但没被任何会话带出来过」都回 `None`（下载时 `session_id=None`）。"""
    assert archive.first_session_for_source(db, "src_none") is None

    _source(db, "src_y")
    assert archive.first_session_for_source(db, "src_none") is None
    assert archive.first_session_for_source(db, "src_y") is None


# ------------------------------------------------------------------ 下载上下文：doc_id 一路


def test_download_target_by_doc_carries_source_columns_and_meta(db: Database) -> None:
    """`documents` 的现有位置/MIME/状态码 + `sources` 的地址/类型/标题/meta，一次取齐。"""
    _source(db)
    _document(db)

    row = archive.download_target_by_doc(db, "doc_a")

    assert row is not None
    assert set(row) == {
        "doc_id",
        "source_id",
        "raw_path",
        "mime",
        "http_status",
        "title",
        "url",
        "source_type",
        "source_title",
        "meta_json",
    }
    assert row["doc_id"] == "doc_a"
    assert row["source_id"] == "src_a"
    assert row["raw_path"] == "raw/a.pdf"
    assert row["mime"] == "application/pdf"
    assert row["http_status"] == 200
    assert row["title"] == "正文 doc_a"
    assert row["source_title"] == "来源 src_a"
    assert row["url"] == "https://src_a.test/a.pdf"
    assert row["source_type"] == "paper"
    assert row["meta_json"] == '{"published": "2025-03"}'


def test_download_target_by_doc_returns_only_the_requested_row(db: Database) -> None:
    """同一来源下多份资料时只回被点名的那一行（`LIMIT`/`WHERE` 写丢就会串行）。"""
    _source(db)
    _document(db, "doc_one", raw_path="raw/one.pdf")
    _document(db, "doc_two", raw_path="raw/two.pdf", http_status=304)

    row = archive.download_target_by_doc(db, "doc_two")

    assert row is not None
    assert row["doc_id"] == "doc_two"
    assert row["raw_path"] == "raw/two.pdf"
    assert row["http_status"] == 304


def test_download_target_by_doc_is_none_for_unknown_doc(db: Database) -> None:
    """未知 `doc_id` 回 `None` —— 上游据此回 `ok:false`，不能抛。"""
    assert archive.download_target_by_doc(db, "doc_missing") is None
    _source(db)
    _document(db)
    assert archive.download_target_by_doc(db, "doc_missing") is None


# ------------------------------------------------------------------ 下载上下文：source_id 一路


def test_download_target_by_source_returns_exactly_the_needed_columns(db: Database) -> None:
    """只取下载解析用得到的五列；列名就是上游 `src["id"]` 那套。"""
    _source(db, "src_s")

    row = archive.download_target_by_source(db, "src_s")

    assert row is not None
    assert set(row) == {"id", "url", "title", "source_type", "meta_json"}
    assert row["id"] == "src_s"
    assert row["url"] == "https://src_s.test/a.pdf"
    assert row["title"] == "来源 src_s"
    assert row["source_type"] == "paper"
    assert row["meta_json"] == '{"published": "2025-03"}'


def test_download_target_by_source_is_none_for_unknown_source(db: Database) -> None:
    assert archive.download_target_by_source(db, "src_missing") is None
    _source(db, "src_s")
    assert archive.download_target_by_source(db, "src_missing") is None
