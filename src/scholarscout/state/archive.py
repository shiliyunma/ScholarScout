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
               sm.summary_id, sm.title_zh, sm.summary_short,
               mn.session_id, se.title AS session_title
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
        -- 所属会话：一份资料可能被多个会话带出来过，这里取**最早**那一次（按写入顺序），
        -- 界面上按它分组；不去重合并（那会让"这条资料属于哪次调研"变得说不清）。
        LEFT JOIN (
            SELECT source_id, session_id,
                   row_number() OVER (PARTITION BY source_id ORDER BY rowid) AS rn
            FROM source_mentions
        ) mn ON mn.source_id = s.id AND mn.rn = 1
        LEFT JOIN sessions se ON se.id = mn.session_id
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


#: 正文一次最多回传多少字符。超了就截断并标记 —— 界面是给人看的，不是给人滚一天的。
TEXT_PREVIEW_LIMIT = 20000

#: 按扩展名判断怎么展示。md 走 Markdown 渲染，代码/数据原样等宽显示，pdf 只给打开原件。
_MARKDOWN_EXT = {"md", "markdown", "mdx"}
_PLAIN_EXT = {
    "txt",
    "py",
    "js",
    "ts",
    "json",
    "yaml",
    "yml",
    "toml",
    "ini",
    "cfg",
    "csv",
    "tsv",
    "ipynb",
    "c",
    "cc",
    "cpp",
    "h",
    "hpp",
    "java",
    "go",
    "rs",
    "rb",
    "sh",
    "bat",
    "sql",
    "html",
    "htm",
    "xml",
    "tex",
    "r",
    "m",
    "jl",
}


def display_format(url: str | None, raw_path: str | None) -> str:
    """这份资料该按什么格式展示：markdown / plain / pdf / other。"""
    for candidate in (url, raw_path):
        text = str(candidate or "").strip()
        if not text:
            continue
        name = text.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in _MARKDOWN_EXT:
            return "markdown"
        if ext == "pdf":
            return "pdf"
        if ext in _PLAIN_EXT:
            return "plain"
    return "other"


def _resolve_text_path(text_path: str, home: Path | None) -> Path:
    from scholarscout import paths

    candidate = Path(str(text_path))
    if candidate.is_absolute():
        return candidate
    return (home or paths.home()) / candidate


def read_text(
    db: Database,
    doc_id: str,
    *,
    limit: int = TEXT_PREVIEW_LIMIT,
    home: Path | None = None,
) -> dict[str, Any]:
    """读一份资料的正文（落盘的提取文本）。**只读**，不联网。

    为什么单独一个接口：资料库列表只带摘要，而用户想看的是**原文的样子**
    （md 就按 md 显示、代码就按代码显示）。正文早就落盘了（data/text/），
    以前只是没有路读它。读不到就如实说读不到（还没提取 / 文件不在），不假装有内容。
    """
    row = db.query_one(
        "SELECT d.text_path, d.raw_path, s.url, d.extract_status"
        " FROM documents d LEFT JOIN sources s ON s.id = d.source_id"
        " WHERE d.id = ?",
        (doc_id,),
    )
    if row is None:
        return {"doc_id": doc_id, "available": False, "reason": "库里没有这份资料", "text": ""}
    text = ""
    truncated = False
    if row["text_path"]:
        path = _resolve_text_path(str(row["text_path"]), home)
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return {
                    "doc_id": doc_id,
                    "available": False,
                    "reason": f"读正文失败：{exc}",
                    "text": "",
                }
        else:
            return {
                "doc_id": doc_id,
                "available": False,
                "reason": "正文文件不在（可能被清理过）",
                "text": "",
            }
    else:
        return {
            "doc_id": doc_id,
            "available": False,
            "reason": f"还没有正文（提取状态：{row['extract_status'] or '未知'}）",
            "text": "",
        }
    if len(text) > limit:
        text = text[:limit]
        truncated = True
    return {
        "doc_id": doc_id,
        "available": True,
        "format": display_format(row["url"], row["raw_path"]),
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        "reason": "",
    }
