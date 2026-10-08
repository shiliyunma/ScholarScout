"""资料库分组与"读正文"的口径测试。

钉三件事：
- 列表条目带**所属会话**（同一会话的资料才能聚在一起）；
- display_format 按 URL/文件名后缀判展示方式（md 走渲染、pdf 只给打开原件）；
- read_text 读不到就**如实说读不到**（没提取/文件不在/没有正文），返回空 text 而不是编内容。
"""

from __future__ import annotations

from pathlib import Path

from scholarscout.state import archive
from scholarscout.state.db import Database


def _doc(db: Database, doc_id: str, *, text_path: str | None, url: str, session: str) -> None:
    db.execute(
        "INSERT INTO sources (id, url, url_canonical, domain, title, source_type,"
        " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
        (f"src_{doc_id}", url, url, "example.test", "标题", "repo", "2026-10-01", "2026-10-01"),
    )
    db.execute(
        "INSERT OR IGNORE INTO sessions (id, session_key, title, created_at, updated_at)"
        " VALUES (?,?,?,?,?)",
        (session, session, "会话标题", "2026-10-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00"),
    )
    db.execute(
        "INSERT INTO documents (id, source_id, title, sha256, raw_path, extract_status, fetched_at,"
        " text_path) VALUES (?,?,?,?,?,?,?,?)",
        (
            doc_id,
            f"src_{doc_id}",
            "文档",
            f"sha-{doc_id}",
            "raw/x.md",
            "ok",
            "2026-10-01",
            text_path,
        ),
    )
    db.execute(
        "INSERT INTO source_mentions (session_id, source_id, created_at) VALUES (?, ?, ?)",
        (session, f"src_{doc_id}", "2026-10-01T00:00:00+08:00"),
    )


def test_list_carries_the_owning_session(db: Database) -> None:
    _doc(db, "doc_a", text_path=None, url="https://example.test/a.md", session="ses_one")
    rows, _ = archive.list_documents(db, limit=10)
    assert rows[0]["session_id"] == "ses_one"
    assert rows[0]["session_title"] == "会话标题"


def test_display_format_follows_the_extension() -> None:
    assert archive.display_format("https://x.test/a.md", None) == "markdown"
    assert archive.display_format("https://x.test/a.py", None) == "plain"
    assert archive.display_format("https://x.test/paper.pdf", None) == "pdf"
    assert archive.display_format("https://x.test/page", None) == "other"
    # URL 没有后缀时看落盘文件名
    assert archive.display_format("https://x.test/page", "raw/notes/readme.md") == "markdown"


def test_read_text_returns_the_original_file(db: Database, tmp_path: Path) -> None:
    text_file = tmp_path / "a.md"
    text_file.write_text("# 标题\n\n正文内容。\n", encoding="utf-8")
    _doc(db, "doc_ok", text_path=str(text_file), url="https://example.test/a.md", session="ses_one")

    payload = archive.read_text(db, "doc_ok")
    assert payload["available"] is True
    assert payload["format"] == "markdown"
    assert "正文内容" in payload["text"]


def test_read_text_truncates_and_says_so(db: Database, tmp_path: Path) -> None:
    text_file = tmp_path / "long.txt"
    text_file.write_text("x" * 500, encoding="utf-8")
    _doc(db, "doc_long", text_path=str(text_file), url="https://e.test/long.txt", session="ses_one")

    payload = archive.read_text(db, "doc_long", limit=100)
    assert payload["chars"] == 100 and payload["truncated"] is True


def test_read_text_reports_missing_instead_of_faking(db: Database, tmp_path: Path) -> None:
    """正文文件不在 / 还没提取 → 如实报，不返回编出来的内容。"""
    _doc(
        db, "doc_gone", text_path=str(tmp_path / "nope.md"), url="https://e.test/x.md", session="s"
    )
    assert archive.read_text(db, "doc_gone")["available"] is False
    assert "不在" in archive.read_text(db, "doc_gone")["reason"]

    _doc(db, "doc_none", text_path=None, url="https://e.test/y.md", session="s")
    payload = archive.read_text(db, "doc_none")
    assert payload["available"] is False and payload["text"] == ""
    assert "还没有正文" in payload["reason"]
