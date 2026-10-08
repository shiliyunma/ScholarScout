"""资料库读取（只读，供 archive.* RPC 方法）。表结构见 docs/02 §4/§7。

正文不在库里 —— 这里只返回**路径**，由调用方决定是否打开或读取文件
（"正文不入库，但路径必须可发现"，见 docs/01 §3.3）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scholarscout.state.db import Database


def list_documents(
    db: Database,
    *,
    source_type: str | None = None,
    tier: int | None = None,
    year_from: int | None = None,
    source_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """列出资料（带当前摘要）。查询文本走 FTS 的入口在 state/search.py，不在这里。"""
    clauses: list[str] = []
    params: list[object] = []
    if source_type:
        clauses.append("s.source_type = ?")
        params.append(source_type)
    if tier is not None:
        clauses.append("s.tier = ?")
        params.append(tier)
    if year_from is not None:
        clauses.append("s.year >= ?")
        params.append(year_from)
    if source_id:
        # 定向取一条：界面按 source_id 找资料时不必把整库拉回来再筛（原来 limit=200 全表扫）
        clauses.append("s.id = ?")
        params.append(source_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    total = db.scalar_int(
        f"SELECT count(*) FROM documents d JOIN sources s ON s.id = d.source_id {where}",
        params,
    )
    rows = db.query(
        f"""
        SELECT d.id AS doc_id, d.source_id, d.title, d.chars, d.lang, d.extract_status,
               d.extract_engine, d.truncated, d.raw_path, d.text_path, d.fetched_at,
               s.title AS source_title, s.url, s.domain, s.source_type, s.tier, s.authority,
               sm.summary_id, sm.title_zh, sm.summary_short
        FROM documents d
        JOIN sources s ON s.id = d.source_id
        LEFT JOIN (
            SELECT doc_id,
                   id AS summary_id,
                   title_zh,
                   summary_short,
                   row_number() OVER (PARTITION BY doc_id ORDER BY created_at DESC) AS rn
            FROM summaries
        ) sm ON sm.doc_id = d.id AND sm.rn = 1
        {where}
        ORDER BY d.fetched_at DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    )
    return [dict(r) for r in rows], total


def get_document(db: Database, doc_id: str) -> dict[str, Any] | None:
    row = _one(db, doc_id)
    if row is None:
        return None
    summaries = db.query(
        """
        SELECT id, kind, title_zh, summary_short, points_json, why_relevant,
               model, prompt_version, degraded, created_at
        FROM summaries WHERE doc_id = ? ORDER BY created_at DESC
        """,
        (doc_id,),
    )
    return {"document": row, "summaries": [dict(r) for r in summaries]}


def _one(db: Database, doc_id: str) -> dict[str, Any] | None:
    row = db.query_one(
        """
        SELECT d.id AS doc_id, d.source_id, d.title, d.chars, d.lang, d.extract_status,
               d.extract_engine, d.truncated, d.raw_path, d.text_path, d.fetched_at,
               s.title AS source_title, s.url, s.domain, s.source_type, s.tier, s.authority
        FROM documents d JOIN sources s ON s.id = d.source_id
        WHERE d.id = ?
        """,
        (doc_id,),
    )
    return dict(row) if row is not None else None


def document_paths(db: Database, doc_id: str, *, home: Path | None = None) -> dict[str, Any] | None:
    """返回资料在磁盘上的位置（相对项目根）与实际存在性。"""
    row = _one(db, doc_id)
    if row is None:
        return None
    from scholarscout import paths as paths_mod

    base = home or paths_mod.home()
    result: dict[str, Any] = {
        "doc_id": doc_id,
        "raw_path": row["raw_path"],
        "text_path": row["text_path"],
    }
    exists: dict[str, bool] = {}
    for key in ("raw_path", "text_path"):
        rel = row[key]
        exists[key] = bool(rel) and (base / str(rel)).is_file()
    result["exists"] = exists
    return result
