"""A2：按 source_id 定向查一条（界面用它替代"拉 200 条再筛"）。

这条口径值得钉住的原因：界面按 source_id 找资料时，原来是 `limit: 200` 全表拉回来再 find，
库一大就是白扫；而且 `source_id` 传空串时绝不能退化成"筛 source_id 为空"（那会把整库筛没）。
"""

from __future__ import annotations

from scholarscout.state import archive


def _add_source(db, source_id: str) -> None:
    db.execute(
        "INSERT INTO sources (id, url, url_canonical, domain, title, source_type,"
        " first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source_id,
            f"https://{source_id}.test",
            f"https://{source_id}.test",
            f"{source_id}.test",
            f"标题 {source_id}",
            "paper",
            "2026-10-01T00:00:00+08:00",
            "2026-10-01T00:00:00+08:00",
        ),
    )


def _add_document(db, doc_id: str, source_id: str) -> None:
    db.execute(
        "INSERT INTO documents (id, source_id, title, sha256, raw_path, extract_status,"
        " fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            doc_id,
            source_id,
            f"正文 {doc_id}",
            f"sha-{doc_id}",
            f"raw/{doc_id}",
            "ok",
            "2026-10-01T00:00:00+08:00",
        ),
    )


def test_filter_by_source_id_returns_only_that_source(db) -> None:
    _add_source(db, "src_a")
    _add_source(db, "src_b")
    _add_document(db, "doc_a", "src_a")
    _add_document(db, "doc_b", "src_b")

    rows, total = archive.list_documents(db, source_id="src_a", limit=1)
    assert total == 1, "按 source_id 筛出来应当只有 1 条"
    assert [row["source_id"] for row in rows] == ["src_a"]

    everything, all_total = archive.list_documents(db, limit=50)
    assert all_total == 2 and len(everything) == 2, "不传 source_id 时仍然看全库"


def test_empty_source_id_does_not_filter_away_everything(db) -> None:
    """空串按"没传"处理。空串若被当成筛选条件，界面会显示"这条资料没抓正文"。"""
    _add_source(db, "src_a")
    _add_document(db, "doc_a", "src_a")
    rows, total = archive.list_documents(db, source_id="", limit=50)
    assert total == 1 and len(rows) == 1
