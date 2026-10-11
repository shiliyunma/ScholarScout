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
        "collected": "2026-10",
        "published": "2024-10",
        "doc_type": "paper",
        "domain": "arxiv.org",
        "sessions": [],
        "rel_path": "data/raw/调研一/paper/x.pdf",
    }
    base.update(overrides)
    return base


def test_render_lists_rows_and_joins_sessions_with_ideographic_comma() -> None:
    rows = [
        _row(sessions=["调研一", "调研二"]),
        _row(
            title="第二份",
            collected="unknown",
            published="unknown",
            doc_type="doc",
            domain="juejin.cn",
        ),
    ]
    text = library_index.render_index(rows)
    assert "共 2 条。" in text
    assert "调研一、调研二" in text
    # 两个年月都取不到就都写 unknown（显示层不再译成「未知」，直接如实写 unknown）
    assert "| 第二份 | unknown | unknown | doc | juejin.cn |" in text


def test_render_header_lists_all_seven_columns() -> None:
    """七列口径：标题｜收录年月｜发表年月｜格式｜来源域｜关联会话｜相对路径。"""
    text = library_index.render_index([])
    assert "| 标题 | 收录年月 | 发表年月 | 格式 | 来源域 | 关联会话 | 相对路径 |" in text


def test_render_writes_seven_cells_per_row() -> None:
    row = _row(title="教育智能体", sessions=["调研一"])
    line = next(
        line
        for line in library_index.render_index([row]).splitlines()
        if line.startswith("| 教育智能体")
    )
    cells = [cell.strip() for cell in line.strip("|").split("|")]
    assert cells == [
        "教育智能体",
        "2026-10",
        "2024-10",
        "paper",
        "arxiv.org",
        "调研一",
        "data/raw/调研一/paper/x.pdf",
    ]


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
    fetched_at: str = T0,
    sessions: tuple[tuple[str, str], ...] = (),
) -> None:
    """``sessions`` = ``(session_id, 标题)``，**按给的顺序**写进 ``source_mentions``（rowid 决定先后）。"""
    db.execute(
        "INSERT INTO sources (id, url, url_canonical, domain, title, source_type,"
        " first_seen_at, last_seen_at, meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (source_id, url, url, domain, title, source_type, T0, T0, meta),
    )
    db.execute(
        "INSERT INTO documents (id, source_id, title, sha256, chars, raw_path, text_path,"
        " extract_status, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (doc_id, source_id, title, sha, 1, "data/raw/x", "data/text/x", "ok", fetched_at),
    )
    for session_id, session_title in sessions:
        db.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?,?,?,?)",
            (session_id, session_title, T0, T0),
        )
        db.execute(
            "INSERT INTO source_mentions (source_id, session_id, created_at) VALUES (?,?,?)",
            (source_id, session_id, T0),
        )


def _cells(body: str, title: str) -> list[str]:
    line = next(line for line in body.splitlines() if line.startswith(f"| {title} "))
    return [cell.strip() for cell in line.strip("|").split("|")]


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
        sessions=(("ses_a1", "调研一"),),
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
    assert payload["layout"] == "session/format"
    assert payload["index_path"] == "data/library/index.md"
    body = (tmp_path / "data" / "library" / "index.md").read_text(encoding="utf-8")
    assert "教育智能体" in body
    assert "调研一" in body
    # 两个桶：调研一/paper 与 unknown/doc（一条会话都没有的落 unknown）
    assert (tmp_path / "data" / "library" / "调研一" / "paper" / "index.md").is_file()
    assert (tmp_path / "data" / "library" / "unknown" / "doc" / "index.md").is_file()


def test_index_row_carries_collected_and_published_separately(db: Database, tmp_path: Path) -> None:
    """收录年月（``fetched_at``）与发表年月（arXiv 号）是**两列**，各自独立。"""
    _seed(
        db,
        doc_id="doc_a",
        source_id="src_a",
        title="教育智能体",
        source_type="arxiv",
        url="https://arxiv.org/abs/2410.24164",
        domain="arxiv.org",
        sha="a" * 64,
        fetched_at="2026-07-01T09:00:00+08:00",
        sessions=(("ses_a1", "调研一"),),
    )

    library_index.write_indexes(db, root=tmp_path)
    body = (tmp_path / "data" / "library" / "index.md").read_text(encoding="utf-8")

    cells = _cells(body, "教育智能体")
    assert cells[1] == "2026-07", "收录年月 = documents.fetched_at"
    assert cells[2] == "2024-10", "发表年月 = arXiv 号自带"
    assert cells[3] == "paper"
    assert cells[4] == "arxiv.org"
    assert cells[5] == "调研一"


def test_earliest_session_decides_bucket_but_all_sessions_listed(
    db: Database, tmp_path: Path
) -> None:
    """目录取**最早**收录它的那次会话；索引「关联会话」列**全部**列出。"""
    _seed(
        db,
        doc_id="doc_a",
        source_id="src_a",
        title="教育智能体",
        source_type="arxiv",
        url="https://arxiv.org/abs/2410.24164",
        domain="arxiv.org",
        sha="a" * 64,
        sessions=(("ses_e", "第一次调研"), ("ses_l", "第二次调研")),
    )

    payload = library_index.write_indexes(db, root=tmp_path)

    assert payload["buckets"] == 1
    assert (tmp_path / "data" / "library" / "第一次调研" / "paper" / "index.md").is_file()
    assert not (tmp_path / "data" / "library" / "第二次调研").exists()
    body = (tmp_path / "data" / "library" / "index.md").read_text(encoding="utf-8")
    assert _cells(body, "教育智能体")[5] == "第一次调研、第二次调研"


def test_write_indexes_on_empty_db_writes_header_only(db: Database, tmp_path: Path) -> None:
    payload = library_index.write_indexes(db, root=tmp_path)
    assert payload["total"] == 0
    assert payload["buckets"] == 0
    assert "共 0 条。" in (tmp_path / "data" / "library" / "index.md").read_text(encoding="utf-8")
