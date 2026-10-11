"""资料索引：``data/library/index.md`` 与各桶 ``data/library/<类型>/<年-月>/index.md``。

**纯函数优先**：给定资料行 → Markdown（:func:`render_index`）。写盘（:func:`write_indexes`）
只是把同样的行渲染后落文件，所以 RPC 重建的索引与迁移脚本重建的索引**逐字节一致**，
不会出现「界面看到的」与「文件里的」两套口径。

每行：``标题 ｜ 年-月 ｜ 类型 ｜ 来源域 ｜ 关联主题（多条用、分隔） ｜ 相对路径``（docs/19 §2）。
「一份资料属于多个主题」在这里既看得见、又不必搬文件。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scholarscout import paths
from scholarscout.library import naming
from scholarscout.state.db import Database

#: 年-月未知时在表里显示的标签（桶名仍是 ``unknown``）。
UNKNOWN_LABEL = "未知"

_DOCUMENT_QUERY = """
    SELECT d.id AS doc_id, d.title AS doc_title, d.sha256,
           d.raw_path, d.text_path,
           s.id AS source_id, s.title AS source_title, s.url,
           s.domain, s.source_type, s.meta_json
    FROM documents d JOIN sources s ON s.id = d.source_id
    ORDER BY d.fetched_at DESC, d.rowid DESC
"""


def _meta_of(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def session_titles(db: Database, source_id: str) -> list[str]:
    """这份来源在哪些会话里出现过（会话标题，去重、按标题排序）；没有就回空表。"""
    rows = db.query(
        """
        SELECT DISTINCT se.title AS title
        FROM source_mentions m JOIN sessions se ON se.id = m.session_id
        WHERE m.source_id = ? AND se.title IS NOT NULL AND se.title <> ''
        ORDER BY se.title
        """,
        (source_id,),
    )
    return [str(r["title"]) for r in rows]


def build_rows(db: Database) -> list[dict[str, Any]]:
    """把库里所有资料整理成索引行（不改盘、不调模型）。"""
    result: list[dict[str, Any]] = []
    for row in db.query(_DOCUMENT_QUERY):
        meta = _meta_of(row["meta_json"])
        url = str(row["url"] or "")
        domain = str(row["domain"] or "") or naming.domain_of(url)
        title = naming.pick_title(row["doc_title"], row["source_title"]) or domain
        result.append(
            {
                "doc_id": str(row["doc_id"]),
                "title": title,
                "year_month": naming.year_month_for(url, meta),
                "doc_type": paths.doc_type_for(str(row["source_type"] or "")),
                "domain": domain,
                "topics": session_titles(db, str(row["source_id"])),
                "rel_path": str(row["raw_path"] or row["text_path"] or ""),
            }
        )
    return result


def _cell(value: str) -> str:
    """表格单元格：转义竖线、压掉换行（标题里两样都常见）。"""
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def render_index(
    rows: list[dict[str, Any]],
    *,
    title: str = "资料索引",
    note: str = "",
) -> str:
    """给定索引行 → Markdown 全文（纯函数，无副作）。"""
    lines: list[str] = [f"# {title}", ""]
    if note:
        lines += [note, ""]
    lines += [
        "> 本文件由 `archive.reindex` / `scripts/migrate_raw_names.py` 生成，请勿手改。",
        "",
        f"共 {len(rows)} 条。",
        "",
        "| 标题 | 年-月 | 类型 | 来源域 | 关联主题 | 相对路径 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        year_month = str(row.get("year_month") or "")
        month_label = UNKNOWN_LABEL if year_month in ("", "unknown") else year_month
        lines.append(
            "| {} | {} | {} | {} | {} | {} |".format(
                _cell(str(row.get("title") or "")),
                month_label,
                _cell(str(row.get("doc_type") or "other")),
                _cell(str(row.get("domain") or "-")),
                _cell("、".join(row.get("topics") or []) or "-"),
                _cell(str(row.get("rel_path") or "-")),
            )
        )
    lines.append("")
    return "\n".join(lines)


def write_indexes(db: Database, *, root: Path | None = None) -> dict[str, Any]:
    """把全库清单与各桶清单写到 ``data/library/`` 下；返回写了哪些文件与桶数。"""
    rows = build_rows(db)
    written: list[str] = []

    total = paths.under_data(Path("library") / "index.md", root)
    total.parent.mkdir(parents=True, exist_ok=True)
    total.write_text(render_index(rows), encoding="utf-8")
    written.append(paths.rel_to_home(total, root))

    buckets = sorted({(str(r["doc_type"]), str(r["year_month"])) for r in rows})
    for doc_type, year_month in buckets:
        subset = [r for r in rows if r["doc_type"] == doc_type and r["year_month"] == year_month]
        target = paths.under_data(Path("library") / doc_type / year_month / "index.md", root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            render_index(
                subset,
                title=f"{doc_type} / {year_month}",
                note="本桶筛选口径见 `data/library/index.md` 与 `docs/19`。",
            ),
            encoding="utf-8",
        )
        written.append(paths.rel_to_home(target, root))

    return {
        "ok": True,
        "total": len(rows),
        "buckets": len(buckets),
        "index_path": written[0],
        "files": written,
    }
