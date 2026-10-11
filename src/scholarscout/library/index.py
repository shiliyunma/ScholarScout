"""资料索引：``data/library/index.md`` 与各桶 ``data/library/<桶目录>/index.md``。

**纯函数优先**：给定资料行 → Markdown（:func:`render_index`）。写盘（:func:`write_indexes`）
只是把同样的行渲染后落文件，所以 RPC 重建的索引与迁移脚本重建的索引**逐字节一致**，
不会出现「界面看到的」与「文件里的」两套口径。

每行：``标题 ｜ 收录年月 ｜ 发表年月 ｜ 格式 ｜ 来源域 ｜ 关联会话 ｜ 相对路径``（docs/19 §2）。

两个时间列**分开单列、各自缺失写 ``unknown``**（用户 2026-10-11 明确）：

- **收录年月** = ``documents.fetched_at``（我们什么时候采到它）；
- **发表年月** = 资料自己的时间戳（arXiv 号 / meta 日期），即原来的「年-月」桶口径。

「关联会话」列**列出全部**关联会话（不只落到目录里的最早那次）—— 一份资料常被好几次调研带出来，
只留最早那次会让"它还出现在哪几次调研"丢掉。列的是**会话目录名**（与 ``data/raw`` 下的目录一一对应；
同名会话带短 id），所以这一列能直接当下钻路径用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scholarscout import paths
from scholarscout.library import naming
from scholarscout.state.db import Database

#: 时间/会话缺失时写的标签（与 ``paths.UNKNOWN_BUCKET`` 同口径：不编一个看着像真的值）。
UNKNOWN = "unknown"

_DOCUMENT_QUERY = """
    SELECT d.id AS doc_id, d.title AS doc_title, d.sha256,
           d.raw_path, d.text_path, d.fetched_at,
           s.id AS source_id, s.title AS source_title, s.url,
           s.domain, s.source_type, s.meta_json
    FROM documents d JOIN sources s ON s.id = d.source_id
    ORDER BY d.fetched_at DESC, d.rowid DESC
"""

#: 会话提及：按写入顺序（``rowid``）—— 与 ``state/archive.py::list_documents`` 的分组口径同一套
#: （「一份资料被多次调研带出取最早那次，不去重合并」）。目录取第一个，索引列全部。
_MENTIONS_QUERY = """
    SELECT source_id, session_id FROM source_mentions ORDER BY rowid
"""


def _meta_of(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def session_dirs_by_source(db: Database, session_dirs: dict[str, str]) -> dict[str, list[str]]:
    """``{source_id: [会话目录名, …]}`` —— 按提及顺序**去重保留先后**（最早那次在最前）。"""
    result: dict[str, list[str]] = {}
    for row in db.query(_MENTIONS_QUERY):
        source_id = str(row["source_id"])
        name = session_dirs.get(str(row["session_id"]), UNKNOWN)
        seen = result.setdefault(source_id, [])
        if name not in seen:
            seen.append(name)
    return result


def build_rows(
    db: Database, *, layout: str | tuple[str, ...] | None = None
) -> list[dict[str, Any]]:
    """把库里所有资料整理成索引行（不改盘、不调模型）。"""
    session_dirs = naming.session_dir_map(db)
    mentions = session_dirs_by_source(db, session_dirs)
    result: list[dict[str, Any]] = []
    for row in db.query(_DOCUMENT_QUERY):
        meta = _meta_of(row["meta_json"])
        url = str(row["url"] or "")
        domain = str(row["domain"] or "") or naming.domain_of(url)
        title = naming.pick_title(row["doc_title"], row["source_title"]) or domain
        source_type = str(row["source_type"] or "")
        doc_type = paths.doc_type_for(source_type)
        collected = naming.collected_month(row["fetched_at"])
        published = naming.year_month_for(url, meta)
        sessions = list(mentions.get(str(row["source_id"]), []))
        # 目录取**最早收录它的那次会话**；一次都没记过的落 unknown（不编）。
        earliest = sessions[0] if sessions else UNKNOWN
        result.append(
            {
                "doc_id": str(row["doc_id"]),
                "title": title,
                "collected": collected,
                "published": published,
                "doc_type": doc_type,
                "domain": domain,
                "sessions": sessions,
                "session": earliest,
                "rel_path": str(row["raw_path"] or row["text_path"] or ""),
                "bucket": paths.layout_dir(
                    layout,
                    session=earliest,
                    fmt=doc_type,
                    collected=collected,
                    published=published,
                ),
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
        "| 标题 | 收录年月 | 发表年月 | 格式 | 来源域 | 关联会话 | 相对路径 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} |".format(
                _cell(str(row.get("title") or "")),
                _cell(str(row.get("collected") or UNKNOWN)),
                _cell(str(row.get("published") or UNKNOWN)),
                _cell(str(row.get("doc_type") or "other")),
                _cell(str(row.get("domain") or "-")),
                _cell("、".join(row.get("sessions") or []) or "-"),
                _cell(str(row.get("rel_path") or "-")),
            )
        )
    lines.append("")
    return "\n".join(lines)


def _prune_stale_indexes(root: Path, written: set[Path]) -> list[str]:
    """删掉**不再是当前桶**的旧 ``index.md`` 并清空目录（换布局后旧桶不该继续挂着）。"""
    library_root = paths.data_dir(root) / "library"
    removed: list[str] = []
    if not library_root.is_dir():
        return removed
    for stale in sorted(library_root.rglob("index.md")):
        if stale not in written:
            stale.unlink()
    for directory in sorted(
        (p for p in library_root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        if not any(directory.iterdir()):
            directory.rmdir()
            removed.append(paths.rel_to_home(directory, root))
    return removed


def write_indexes(
    db: Database,
    *,
    root: Path | None = None,
    layout: str | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """把全库清单与各桶清单写到 ``data/library/`` 下；返回写了哪些文件与桶数。"""
    segments = paths.normalize_layout(layout)
    rows = build_rows(db, layout=segments)
    written: list[str] = []
    written_abs: set[Path] = set()

    total = paths.library_index_path("", root)
    total.parent.mkdir(parents=True, exist_ok=True)
    total.write_text(render_index(rows, note=_note_for(segments)), encoding="utf-8")
    written.append(paths.rel_to_home(total, root))
    written_abs.add(total)

    buckets = sorted({row["bucket"] for row in rows}, key=lambda p: p.as_posix())
    for bucket in buckets:
        subset = [row for row in rows if row["bucket"] == bucket]
        target = paths.library_index_path(bucket, root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            render_index(
                subset,
                title=bucket.as_posix(),
                note="本桶筛选口径见 `data/library/index.md` 与 `docs/19`。",
            ),
            encoding="utf-8",
        )
        written.append(paths.rel_to_home(target, root))
        written_abs.add(target)

    return {
        "ok": True,
        "total": len(rows),
        "buckets": len(buckets),
        "layout": "/".join(segments),
        "index_path": written[0],
        "files": written,
        "removed": _prune_stale_indexes(root or paths.home(), written_abs),
    }


def _note_for(segments: tuple[str, ...]) -> str:
    return f"> 当前布局：`<{'/'.join(segments)}>`（配置 `library.layout`，默认 `<会话>/<格式>`）。"
