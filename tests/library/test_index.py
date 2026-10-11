"""资料索引：渲染（纯函数）与写盘（docs/19 §2/§5）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scholarscout.library import index as library_index
from scholarscout.state.db import Database

from ..conftest import T0


def _row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "T",
        "year_month": "2024-10",
        "doc_type": "paper",
        "domain": "arxiv.org",
        "topics": [],
        "rel_path": "data/raw/paper/2024-10/x.pdf",
    }
    base.update(overrides)
    return base


def test_render_lists_rows_and_joins_topics_with_ideographic_comma() -> None:
    rows = [
        _row(topics=["主题 A", "主题 B"]),
        _row(title="第二份", year_month="unknown", doc_type="doc", domain="juejin.cn"),
    ]
    text = library_index.render_index(rows)
    assert "共 2 条。" in text
    assert "主题 A、主题 B" in text
    # 未知年份单列一桶（显示成「未知」，不编一个年份）
    assert "未知" in text


def test_render_empty_library_does_not_error() -> None:
    text = library_index.render_index([])
    assert "共 0 条。" in text


def test_render_escapes_table_pipe_and_newline() -> None:
    text = library_index.render_index([_row(title="a | b\nc")])
    assert "a \\| b c" in text


def _seed(
    db: Database,
    *,
    doc_id: str,
    source_id: str,
    title: str,
    source_type: str,
    url: str,
    domain: str,
    sha: str,
    meta: str = "{}",
    topics: tuple[str, ...] = (),
) -> None:
    db.execute(
        "INSERT INTO sources (id, url, url_canonical, domain, title, source_type,"
        " first_seen_at, last_seen_at, meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (source_id, url, url, domain, title, source_type, T0, T0, meta),
    )
    db.execute(
        "INSERT INTO documents (id, source_id, title, sha256, chars, raw_path, text_path,"
        " extract_status, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (doc_id, source_id, title, sha, 1, "data/raw/x", "data/text/x", "ok", T0),
    )
    for index, topic in enumerate(topics):
        session_id = f"ses_{doc_id}_{index}"
        db.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?,?,?,?)",
            (session_id, topic, T0, T0),
        )
        db.execute(
            "INSERT INTO source_mentions (source_id, session_id, created_at) VALUES (?,?,?)",
            (source_id, session_id, T0),
        )


def test_write_indexes_creates_total_and_per_bucket_files(db: Database, tmp_path: Path) -> None:
    _seed(
        db,
        doc_id="doc_a",
        source_id="src_a",
        title="教育智能体",
        source_type="arxiv",
        url="https://arxiv.org/abs/2410.24164",
        domain="arxiv.org",
        sha="a" * 64,
        topics=("调研一",),
    )
    _seed(
        db,
        doc_id="doc_b",
        source_id="src_b",
        title="OpenClaw",
        source_type="web",
        url="https://openclaw.ai/",
        domain="openclaw.ai",
        sha="b" * 64,
    )

    payload = library_index.write_indexes(db, root=tmp_path)

    assert payload["total"] == 2
    assert payload["buckets"] == 2
    assert payload["index_path"] == "data/library/index.md"
    total = tmp_path / "data" / "library" / "index.md"
    body = total.read_text(encoding="utf-8")
    assert "教育智能体" in body
    assert "调研一" in body
    # 两个桶：paper/2024-10 与 doc/unknown
    assert (tmp_path / "data" / "library" / "paper" / "2024-10" / "index.md").is_file()
    assert (tmp_path / "data" / "library" / "doc" / "unknown" / "index.md").is_file()


def test_write_indexes_on_empty_db_writes_header_only(db: Database, tmp_path: Path) -> None:
    payload = library_index.write_indexes(db, root=tmp_path)
    assert payload["total"] == 0
    assert payload["buckets"] == 0
    assert "共 0 条。" in (tmp_path / "data" / "library" / "index.md").read_text(encoding="utf-8")
