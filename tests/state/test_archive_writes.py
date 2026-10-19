"""`state/archive.py` 的三条写入：下载回填 / 位置对齐 / 迁移改路径。

钉的是"不该动的列没被动"：
- 下载回填**不改** ``sha256``（去重锚）与 ``extract_status``（有没有抽正文是另一件事）；
- 位置对齐**不改** ``fetched_at``（还是那次抓取的时间）；
- 迁移改路径**原件与全文一起改**。
"""

from __future__ import annotations

from scholarscout.state import archive
from scholarscout.state.db import Database


def _doc(db: Database, doc_id: str = "doc_001") -> str:
    db.execute(
        "INSERT INTO sources (id, url, url_canonical, domain, title, source_type,"
        " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            "src_001",
            "https://example.test/a.pdf",
            "https://example.test/a.pdf",
            "example.test",
            "示例",
            "web",
            "2026-10-01",
            "2026-10-01",
        ),
    )
    db.execute(
        "INSERT INTO documents (id, source_id, title, sha256, raw_path, text_path,"
        " extract_status, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
        (doc_id, "src_001", "示例", "sha-fixed", "raw/old.pdf", "text/old.txt", "ok", "T0"),
    )
    return doc_id


def _row(db: Database, doc_id: str) -> dict[str, object]:
    row = db.query_one("SELECT * FROM documents WHERE id = ?", (doc_id,))
    assert row is not None
    return dict(row)


def test_set_document_download_keeps_sha_and_extract_status(db: Database) -> None:
    doc_id = _doc(db)
    archive.set_document_download(
        db,
        doc_id,
        raw_path="raw/new.pdf",
        mime="application/pdf",
        http_status=200,
        fetched_at="T1",
    )
    row = _row(db, doc_id)
    assert row["raw_path"] == "raw/new.pdf"
    assert row["mime"] == "application/pdf"
    assert row["http_status"] == 200
    assert row["fetched_at"] == "T1"
    assert row["sha256"] == "sha-fixed", "sha256 是去重锚，下载不能改它"
    assert row["extract_status"] == "ok", "下载不改变'有没有抽正文'"


def test_set_document_raw_ref_keeps_fetched_at(db: Database) -> None:
    doc_id = _doc(db)
    archive.set_document_raw_ref(
        db, doc_id, raw_path="raw/moved.pdf", mime="application/pdf", http_status=304
    )
    row = _row(db, doc_id)
    assert row["raw_path"] == "raw/moved.pdf"
    assert row["http_status"] == 304
    assert row["fetched_at"] == "T0", "只是对齐位置，抓取时间照旧"


def test_set_document_paths_writes_both_columns(db: Database) -> None:
    doc_id = _doc(db)
    archive.set_document_paths(db, doc_id, raw_path="raw/new/a.pdf", text_path="text/new/a.txt")
    row = _row(db, doc_id)
    assert row["raw_path"] == "raw/new/a.pdf"
    assert row["text_path"] == "text/new/a.txt"
