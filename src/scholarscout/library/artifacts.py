"""产物入库：``py.exec``（或导出）写完文件之后，把**新出现的产物**登记进资料库并刷新索引。

缺陷现场（``docs/23`` 缺陷 1）：模型用 ``py.exec`` 把 ``毕业论文.docx`` 写进 ``data/exports/``，
文件真的在，但资料库列表、``data/library/index.md``、学习路线与笔记的 ``source_id`` 全都看不到
—— 因为产物**从来没进过库**，``library/index.py`` 只渲染 ``documents JOIN sources``。

四条口径（本模块就是它们的实现）：

1. **事件驱动**：调用方（``tools/py_exec.py``）在产物落盘后立刻调 :func:`refresh_after_write`，
   不再等"重启/手工重建"；
2. **增量**：判据是 ``mtime_ns + size`` —— 两个都没变就连 sha256 都不算、``index.md`` 也不重写；
3. **幂等**：一个产物文件 = 一个 ``source`` + 一个 ``document``；内容变了就地改那一行，
   重复扫同一批文件不长行、不重复记 ``source_mentions``；
4. **失败隔离**：索引出问题只让回包标 ``indexed=false`` + ``index_error``，
   产物仍算成功（文件在盘上、库里有行，只是 ``index.md`` 没刷新）。

**不整库重建**：只扫调用方给的那个工作目录，逐文件比水位；``write_indexes`` 只在真有新增/改动时
才跑一次（它本身只读库渲染 Markdown，不再碰文件系统里的资料）。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import mimetypes
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from scholarscout import paths
from scholarscout.library.index import write_indexes
from scholarscout.state import artifact_files
from scholarscout.state.db import Database
from scholarscout.state.ingest import record_mention, save_document, upsert_source
from scholarscout.state.migrate import now_iso

logger = logging.getLogger(__name__)

#: 读文件算哈希的块大小（1MB）：大 docx/pdf 不整个读进内存。
_CHUNK = 1 << 20

#: 半成品 / 临时文件的扩展名：写到一半的产物不该被当成"成品"登记。
_SKIP_SUFFIXES = frozenset({".tmp", ".temp", ".part", ".crdownload", ".partial", ".swp", ".lock"})

#: 系统垃圾文件名（Word/Excel 的锁文件也在这里：``~$`` 前缀单独判）。
_SKIP_NAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini"})

#: ``documents.extract_engine`` 的取值：产物不是"抽取"出来的，如实标来源。
EXTRACT_ENGINE = "artifact"

#: ``meta_json.kind``：谁写进来的（以后加导出类调用方时便于区分）。
META_KIND = "py.exec"

#: 扫描结果的空账（失败/空目录共用，形状与成功时一致，调用方不必分支）。
_EMPTY_SCAN: dict[str, Any] = {"added": 0, "updated": 0, "unchanged": 0, "refreshed": False}


def _meta_of(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sha256(path: Path) -> str:
    """文件内容的 sha256（只在 mtime/size 变了之后才算）。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _generated_at(stat: Any) -> str:
    """文件的生成/修改时刻（写进 ``meta_json.created_at``，索引里的"发表年月"用它）。"""
    return datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")


def _mime_for(path: Path) -> str | None:
    return mimetypes.guess_type(path.name)[0]


def _iter_files(workdir: Path) -> Iterator[Path]:
    """工作目录下的普通文件：跳过隐藏文件/目录、系统垃圾名、半成品扩展名。"""
    for path in sorted(workdir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(workdir)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if path.name.startswith("~$") or path.name.lower() in _SKIP_NAMES:
            continue
        if path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        yield path


def _known(db: Database) -> dict[str, dict[str, Any]]:
    """``{相对路径: 已登记行}`` —— 一次查询，不按文件逐个打库。"""
    result: dict[str, dict[str, Any]] = {}
    for row in artifact_files.artifact_documents(db):
        rel = str(row["raw_path"] or "")
        if rel:
            result[rel] = row
    return result


def _level(stat: Any) -> dict[str, Any]:
    return {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


def _unchanged(row: dict[str, Any], stat: Any) -> bool:
    """水位判据：``size`` 与 ``mtime_ns`` 都没变 → 这个文件不用看内容。"""
    meta = _meta_of(row.get("meta_json"))
    return meta.get("size") == int(stat.st_size) and meta.get("mtime_ns") == int(stat.st_mtime_ns)


def _meta_for(rel: str, stat: Any, digest: str) -> dict[str, Any]:
    return {
        "kind": META_KIND,
        "path": rel,
        "sha256": digest,
        "created_at": _generated_at(stat),
        **_level(stat),
    }


def _register(
    db: Database, *, path: Path, rel: str, stat: Any, digest: str, domain: str
) -> dict[str, Any]:
    """新产物：``upsert_source`` + ``save_document``（两条都按唯一键去重，重复调用不会长行）。"""
    title = path.stem or path.name
    meta = _meta_for(rel, stat, digest)
    source_id, _created = upsert_source(
        db,
        {
            "url": artifact_url(rel),
            "url_canonical": artifact_url(rel),
            "domain": domain,
            "title": title,
            "source_type": artifact_files.ARTIFACT_SOURCE_TYPE,
            "meta_json": meta,
        },
    )
    doc_id, _doc_created = save_document(
        db,
        source_id=source_id,
        sha256=digest,
        title=title,
        mime=_mime_for(path),
        raw_path=rel,
        extract_status="ok",
        extract_engine=EXTRACT_ENGINE,
    )
    return {
        "path": rel,
        "source_id": source_id,
        "doc_id": doc_id,
        "title": title,
        "size": int(stat.st_size),
        "change": "added",
    }


def _update(
    db: Database, *, row: dict[str, Any], path: Path, rel: str, stat: Any, digest: str
) -> dict[str, Any]:
    """同一个文件被改过：就地更新那一行资料 + 把水位推到新 mtime/size。"""
    title = path.stem or path.name
    source_id = str(row["source_id"])
    doc_id = str(row["doc_id"])
    artifact_files.update_artifact(
        db,
        source_id=source_id,
        doc_id=doc_id,
        title=title,
        sha256=digest,
        path=rel,
        mime=_mime_for(path),
        chars=int(row["chars"] or 0),
        meta=_meta_for(rel, stat, digest),
        fetched_at=now_iso(),
    )
    return {
        "path": rel,
        "source_id": source_id,
        "doc_id": doc_id,
        "title": title,
        "size": int(stat.st_size),
        "change": "updated",
    }


def _mention(db: Database, *, source_id: str, session_id: str | None) -> None:
    """记一次"这个会话用到过这份产物"（资料索引的「关联会话」列靠它；同一个会话只记一次）。"""
    if not session_id:
        return
    if artifact_files.mention_exists(db, source_id=source_id, session_id=session_id):
        return
    record_mention(db, source_id=source_id, session_id=session_id)


def scan_artifacts(
    db: Database,
    workdir: Path,
    *,
    session_id: str | None = None,
    root: Path | None = None,
    layout: str | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """扫一个工作目录，把新/变文件登记进资料库；**只有真有变化时才重写索引**。

    返回 ``{indexed, index_error, added, updated, unchanged, refreshed, artifacts, index_path}``：
    ``artifacts`` 只列本次真正新增/更新的文件（形状给模型看：``path`` / ``source_id`` / ``doc_id``）。
    """
    base = (root or paths.home()).resolve()
    target = Path(workdir)
    if not target.is_dir():
        return {
            "indexed": True,
            "index_error": "",
            "index_path": None,
            "artifacts": [],
            **_EMPTY_SCAN,
        }

    known = _known(db)
    domain = target.name or "exports"
    added = updated = unchanged = 0
    changed: list[dict[str, Any]] = []
    for path in _iter_files(target):
        try:
            rel = paths.rel_to_home(path, base)
            stat = path.stat()
        except (OSError, ValueError):
            # 越界（不同盘符）/ 刚被删掉：跳过这一个文件，不拖垮整次扫描
            continue
        if stat.st_size <= 0:
            continue
        row = known.get(rel)
        if row is not None and _unchanged(row, stat):
            unchanged += 1
            continue
        digest = _sha256(path)
        if row is not None and str(row["sha256"]) == digest:
            # 内容没变，只是 mtime 动了（复制/触摸/编辑器重存）：只挪水位，不算"更新"。
            # 为什么仍写一次库：不挪水位的话，每次扫描都会把这个文件的 sha256 重算一遍
            # （大 docx/pdf 就是白白读一遍盘）。代价只有一条轻量 UPDATE（``sources.meta_json`` /
            # ``last_seen_at``）—— 不动 ``documents``、不重写索引、不发 ``library.updated``。
            # 取舍口径：幂等指"资料库看到的没有变化"，不是"一条 SQL 都不跑"。
            artifact_files.touch_artifact(
                db, source_id=str(row["source_id"]), meta=_meta_for(rel, stat, digest)
            )
            unchanged += 1
            continue
        if row is None:
            entry = _register(db, path=path, rel=rel, stat=stat, digest=digest, domain=domain)
            added += 1
        else:
            entry = _update(db, row=row, path=path, rel=rel, stat=stat, digest=digest)
            updated += 1
        changed.append(entry)
        _mention(db, source_id=str(entry["source_id"]), session_id=session_id)

    index_path: str | None = None
    refreshed = False
    indexed = True
    index_error = ""
    if added or updated:
        try:
            written = write_indexes(db, root=root, layout=layout)
        except Exception as exc:  # 索引只是渲染：失败不推翻已入库的行
            logger.warning("产物索引重写失败（workdir=%s）：%s", workdir, exc)
            indexed = False
            index_error = f"{type(exc).__name__}: {exc}"
        else:
            index_path = str(written["index_path"])
            refreshed = True
    return {
        "indexed": indexed,
        "index_error": index_error,
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "refreshed": refreshed,
        "artifacts": changed,
        "index_path": index_path,
    }


def refresh_after_write(
    db: Database,
    workdir: Path,
    *,
    session_id: str | None = None,
    root: Path | None = None,
    layout: str | tuple[str, ...] | None = None,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """产物落盘后的一次刷新；**任何失败都收成回包字段，不冒泡**。

    回包形状固定为 ``{indexed, index_error, artifacts, artifact_scan}``：
    索引挂了也要让调用方（``py.exec``）如实告诉模型/界面"产物成了、索引没刷新"。
    ``emit`` 给了就在真有新增/改动时推一条 ``library.updated``（界面据此立刻重拉资料库/
    路线/笔记，不必等切页签）。
    """
    try:
        scan = scan_artifacts(db, workdir, session_id=session_id, root=root, layout=layout)
    except Exception as exc:  # 索引是"附加动作"：失败只记 warning，产物仍算成功
        logger.warning("产物索引失败（workdir=%s）：%s", workdir, exc)
        return {
            "indexed": False,
            "index_error": f"{type(exc).__name__}: {exc}",
            "artifacts": [],
            "artifact_scan": dict(_EMPTY_SCAN),
        }
    if emit is not None and (scan["added"] or scan["updated"]):
        with contextlib.suppress(Exception):
            emit(
                "library.updated",
                {
                    "session_id": session_id or "",
                    "added": scan["added"],
                    "updated": scan["updated"],
                    "index_path": scan["index_path"] or "",
                },
            )
    return {
        "indexed": bool(scan["indexed"]),
        "index_error": str(scan["index_error"]),
        "artifacts": scan["artifacts"],
        "artifact_scan": {
            "added": scan["added"],
            "updated": scan["updated"],
            "unchanged": scan["unchanged"],
            "refreshed": scan["refreshed"],
        },
    }


def artifact_url(rel: str) -> str:
    """产物来源的规范 URL（``upsert_source`` 按它去重；单独导出便于排障与测试）。"""
    return f"artifact://{rel}"
