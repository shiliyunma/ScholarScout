"""入库（写库统一走这里，是 `state/db.py` 之外唯一允许写数据的地方）：

- ``upsert_source``：按 ``url_canonical`` 去重（同一篇论文被两个源命中只留一条）
- ``save_document``：按 ``(source_id, sha256)`` 去重
- ``save_summary``：按 ``(doc_id, prompt_version)`` 幂等（换提示词才产生新版本）
- ``record_mention``：记下"这条资料在哪次会话、哪一轮被用到"（docs/02 §6）
"""

from __future__ import annotations

import json
from typing import Any

from scholarscout.state.db import Database
from scholarscout.state.migrate import now_iso
from scholarscout.state.sessions import new_id

_SOURCE_UPSERT = """
INSERT INTO sources (id, url, url_canonical, domain, title, authors_json, year, venue, doi,
                     source_type, first_seen_at, last_seen_at, meta_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def upsert_source(
    db: Database, row: dict[str, Any], *, source_id: str | None = None
) -> tuple[str, bool]:
    """插入或复用来源，返回 ``(source_id, 是否新建)``。"""
    canonical = str(row["url_canonical"])
    existing = db.query_one("SELECT id FROM sources WHERE url_canonical = ?", (canonical,))
    stamp = now_iso()
    if existing is not None:
        db.execute(
            """
            UPDATE sources SET last_seen_at = ?,
                   title = coalesce(?, title),
                   year = coalesce(?, year),
                   venue = coalesce(?, venue),
                   doi = coalesce(?, doi)
            WHERE id = ?
            """,
            (
                stamp,
                row.get("title"),
                row.get("year"),
                row.get("venue"),
                row.get("doi"),
                existing["id"],
            ),
        )
        return str(existing["id"]), False

    new = source_id or new_id("src")
    db.execute(
        _SOURCE_UPSERT,
        (
            new,
            row["url"],
            canonical,
            row["domain"],
            row.get("title"),
            json.dumps(list(row.get("authors_json") or []), ensure_ascii=False),
            row.get("year"),
            row.get("venue"),
            row.get("doi"),
            row["source_type"],
            stamp,
            stamp,
            json.dumps(row.get("meta_json") or {}, ensure_ascii=False),
        ),
    )
    return new, True


def save_document(
    db: Database,
    *,
    source_id: str,
    sha256: str,
    title: str | None = None,
    abstract: str | None = None,
    mime: str | None = None,
    lang: str | None = None,
    chars: int = 0,
    raw_path: str | None = None,
    text_path: str | None = None,
    extract_status: str = "ok",
    extract_engine: str | None = None,
    truncated: bool = False,
    http_status: int | None = None,
    ok: bool = True,
    error: str | None = None,
    doc_id: str | None = None,
) -> tuple[str, bool]:
    """按 ``(source_id, sha256)`` 去重，返回 ``(doc_id, 是否新建)``。"""
    existing = db.query_one(
        "SELECT id FROM documents WHERE source_id = ? AND sha256 = ?", (source_id, sha256)
    )
    if existing is not None:
        return str(existing["id"]), False

    new = doc_id or new_id("doc")
    db.execute(
        """
        INSERT INTO documents (id, source_id, title, abstract, sha256, mime, lang, chars,
                               raw_path, text_path, extract_status, extract_engine, truncated,
                               fetched_at, http_status, ok, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new,
            source_id,
            title,
            abstract,
            sha256,
            mime,
            lang,
            chars,
            raw_path,
            text_path,
            extract_status,
            extract_engine,
            1 if truncated else 0,
            now_iso(),
            http_status,
            1 if ok else 0,
            error,
        ),
    )
    return new, True


def save_summary(
    db: Database, draft: Any, *, doc_id: str, source_id: str, session_id: str | None = None
) -> str:
    """写摘要；``(doc_id, prompt_version)`` 冲突时更新（重跑幂等，不堆垃圾行）。"""
    summary_id = new_id("sum")
    db.execute(
        """
        INSERT INTO summaries (id, doc_id, source_id, session_id, kind, title_zh, summary_short,
                               points_json, why_relevant, model, prompt_version, input_chars,
                               input_truncated, degraded, guard_failed, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (doc_id, prompt_version) DO UPDATE SET
            title_zh = excluded.title_zh,
            summary_short = excluded.summary_short,
            points_json = excluded.points_json,
            why_relevant = excluded.why_relevant,
            model = excluded.model,
            input_chars = excluded.input_chars,
            input_truncated = excluded.input_truncated,
            degraded = excluded.degraded,
            guard_failed = excluded.guard_failed,
            error = excluded.error,
            created_at = excluded.created_at
        """,
        (
            summary_id,
            doc_id,
            source_id,
            session_id,
            draft.kind,
            draft.title_zh,
            draft.summary_short,
            draft.points_json(),
            draft.why_relevant,
            draft.model,
            draft.prompt_version,
            draft.input_chars,
            1 if draft.input_truncated else 0,
            1 if draft.degraded else 0,
            1 if draft.guard_failed else 0,
            draft.error,
            now_iso(),
        ),
    )
    row = db.query_one(
        "SELECT id FROM summaries WHERE doc_id = ? AND prompt_version = ?",
        (doc_id, draft.prompt_version),
    )
    return str(row["id"]) if row is not None else summary_id


def record_mention(
    db: Database,
    *,
    source_id: str,
    session_id: str,
    doc_id: str | None = None,
    summary_id: str | None = None,
    message_id: str | None = None,
    turn_id: str | None = None,
    role: str | None = None,
) -> int:
    cursor = db.execute(
        """
        INSERT INTO source_mentions (source_id, doc_id, summary_id, session_id, message_id,
                                     turn_id, role, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (source_id, doc_id, summary_id, session_id, message_id, turn_id, role, now_iso()),
    )
    return int(cursor.lastrowid or 0)


def latest_summary(db: Database, doc_id: str) -> dict[str, Any] | None:
    row = db.query_one(
        "SELECT * FROM summaries WHERE doc_id = ? ORDER BY created_at DESC LIMIT 1", (doc_id,)
    )
    return dict(row) if row is not None else None
