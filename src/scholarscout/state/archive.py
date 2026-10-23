"""资料库读写（供 archive.* RPC 与资料库迁移使用）。表结构见 docs/02 §4/§7。

正文不在库里 —— 读侧只返回**路径**，由调用方决定是否打开或读取文件
（"正文不入库，但路径必须可发现"，见 docs/01 §3.3）。

写侧只承担"一行几列"的更新（下载回填 / 迁移改路径），不判断该不该写：
下载去重、sha 不可变、事务里同时改原件与全文这些口径由调用方持有。
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


# --------------------------------------------- 读（搜索命中 / 下载上下文：RPC 只调函数，不写 SQL）


def summary_hit_row(db: Database, summary_id: str) -> dict[str, Any] | None:
    """一条摘要命中的展示行：摘要字段 + 它所属资料的路径与抽取状态；查不到回 None。

    搜索命中的 ``snippet`` / ``path`` 不在库里（由 FTS 查询给出），所以这里只回库里的那几列。
    """
    row = db.query_one(
        """
        SELECT s.doc_id, s.source_id, s.title_zh, s.summary_short,
               d.raw_path, d.text_path, d.extract_status
        FROM summaries s JOIN documents d ON d.id = s.doc_id
        WHERE s.id = ?
        """,
        (summary_id,),
    )
    return dict(row) if row is not None else None


def first_session_for_source(db: Database, source_id: str) -> str | None:
    """这份资料**最早**被哪个会话带出来（与 `list_documents` 的分组口径同一套，docs/19 §2）。"""
    row = db.query_one(
        "SELECT session_id FROM source_mentions WHERE source_id = ? ORDER BY rowid LIMIT 1",
        (source_id,),
    )
    return str(row["session_id"]) if row is not None else None


def download_target_by_doc(db: Database, doc_id: str) -> dict[str, Any] | None:
    """``doc_id`` 对应的下载上下文**原始行**（documents × sources）；没有这一行回 None。

    列是 `archive.download` 要用的那几列（现有原件位置 / MIME / 状态码 / 标题 / 源地址 / meta）。
    """
    row = db.query_one(
        """
        SELECT d.id AS doc_id, d.source_id, d.raw_path, d.mime, d.http_status,
               d.title, s.url, s.source_type, s.title AS source_title, s.meta_json
        FROM documents d JOIN sources s ON s.id = d.source_id
        WHERE d.id = ?
        """,
        (doc_id,),
    )
    return dict(row) if row is not None else None


def download_target_by_source(db: Database, source_id: str) -> dict[str, Any] | None:
    """``source_id`` 对应的来源行（下载解析目标用得到的那几列）；没有这一行回 None。"""
    row = db.query_one(
        "SELECT id, url, title, source_type, meta_json FROM sources WHERE id = ?",
        (source_id,),
    )
    return dict(row) if row is not None else None


# ------------------------------------------------------------------ 写（下载回填 / 迁移改路径）


def set_document_download(
    db: Database,
    doc_id: str,
    *,
    raw_path: str,
    mime: str,
    http_status: int,
    fetched_at: str,
) -> None:
    """把一次下载的结果回填到已有行：位置 / MIME / 状态码 / 时间。

    **不动** ``sha256``（去重与不可变的锚）与 ``extract_status``（有没有抽正文是另一件事）。
    """
    db.execute(
        "UPDATE documents SET raw_path = ?, mime = ?, http_status = ?, fetched_at = ? WHERE id = ?",
        (raw_path, mime, http_status, fetched_at, doc_id),
    )


def set_document_raw_ref(
    db: Database, doc_id: str, *, raw_path: str, mime: str, http_status: int
) -> None:
    """只把行的原件位置对齐到盘上的新位置（``fetched_at`` 保持原有那次抓取时间）。"""
    db.execute(
        "UPDATE documents SET raw_path = ?, mime = ?, http_status = ? WHERE id = ?",
        (raw_path, mime, http_status, doc_id),
    )


def set_document_paths(db: Database, doc_id: str, *, raw_path: str, text_path: str) -> None:
    """原件与全文的路径**在同一个事务里**更新（要么都改，要么都不改）。"""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE documents SET raw_path = ?, text_path = ? WHERE id = ?",
            (raw_path, text_path, doc_id),
        )
