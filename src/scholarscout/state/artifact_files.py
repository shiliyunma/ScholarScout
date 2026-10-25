"""产物（``py.exec`` 写出的 docx/xlsx/pdf…）的登记账本：读已登记行 + 就地写回扫描水位。

产物和"抓回来的资料"共用 ``sources`` / ``documents`` 两张表 —— 资料库列表（``state/archive``）、
资料索引（``library/index.py``）、路线与笔记的 ``source_id`` 都只认这两张表，所以**产物要能被
看见就必须真的入库**（缺陷现场见 ``docs/23``：文件在 ``data/exports``，库里一条都没有）。

与抓取资料的区别只有一个：``sources.source_type = 'artifact'``，且 ``sources.meta_json`` 里存
**扫描水位**（``size`` / ``mtime_ns`` / ``sha256`` / ``created_at``）：

- 水位是增量的依据：``size`` 与 ``mtime_ns`` 都没变就既不重算哈希、也不重写索引；
- 一个产物文件 = 一个 ``source`` + 一个 ``document``：内容变了**就地改那一行**（版本不堆行，
  资料库里永远只看到这个文件的最新一版）；
- 为什么不新开一张"扫描日志"表：``documents`` 已经有 ``raw_path`` 与 ``(source_id, sha256)``，
  水位放 ``meta_json`` 就够用，不必为一次索引再动 DDL（迁移文件是人工 review 路径）。

写库仍然只走 ``state/ingest.py`` 那两条（``upsert_source`` / ``save_document``）与这里的
``UPDATE``；``library/`` 层只做"扫哪个目录、算不算变化"。
"""

from __future__ import annotations

import json
from typing import Any

from scholarscout.state.db import Database
from scholarscout.state.migrate import now_iso

#: 产物在 ``sources.source_type`` 里的取值（资料库按它筛，布局按它折算 doc_type）。
ARTIFACT_SOURCE_TYPE = "artifact"

_SELECT_ARTIFACTS = """
SELECT s.id AS source_id, s.url, s.url_canonical, s.domain, s.title AS source_title,
       s.meta_json, d.id AS doc_id, d.title AS doc_title, d.sha256, d.raw_path, d.chars,
       d.mime, d.fetched_at
FROM sources s JOIN documents d ON d.source_id = s.id
WHERE s.source_type = ?
ORDER BY d.fetched_at DESC, d.rowid DESC
"""


def artifact_documents(db: Database) -> list[dict[str, Any]]:
    """所有已登记产物（一条 = 一个文件），``meta_json`` 原样带回给调用方解析水位。"""
    return [dict(row) for row in db.query(_SELECT_ARTIFACTS, (ARTIFACT_SOURCE_TYPE,))]


def update_artifact(
    db: Database,
    *,
    source_id: str,
    doc_id: str,
    title: str,
    sha256: str,
    path: str,
    mime: str | None,
    chars: int,
    meta: dict[str, Any],
    fetched_at: str,
) -> None:
    """内容变了：**就地**改这一行资料，并把扫描水位推到新的 mtime/size。

    为什么不 ``save_document`` 插新行：``UNIQUE (source_id, sha256)`` 会把每次改稿都变成一条新资料，
    资料库列表里同一个文件出现十几次，用户反而更找不到"最新那版"。
    """
    db.execute(
        "UPDATE sources SET title = ?, last_seen_at = ?, meta_json = ? WHERE id = ?",
        (title, now_iso(), json.dumps(meta, ensure_ascii=False), source_id),
    )
    db.execute(
        """
        UPDATE documents SET title = ?, sha256 = ?, raw_path = ?, mime = ?, chars = ?,
               extract_status = 'ok', extract_engine = 'artifact', fetched_at = ?
        WHERE id = ?
        """,
        (title, sha256, path, mime, chars, fetched_at, doc_id),
    )


def touch_artifact(db: Database, *, source_id: str, meta: dict[str, Any]) -> None:
    """只挪扫描水位（mtime/size 变了但内容 sha 没变）：``documents`` 行一个字不动。"""
    db.execute(
        "UPDATE sources SET last_seen_at = ?, meta_json = ? WHERE id = ?",
        (now_iso(), json.dumps(meta, ensure_ascii=False), source_id),
    )


def mention_exists(db: Database, *, source_id: str, session_id: str) -> bool:
    """这个来源有没有在这个会话里被记过（重复扫同一份产物不该堆 ``source_mentions`` 行）。"""
    row = db.query_one(
        "SELECT 1 AS hit FROM source_mentions WHERE source_id = ? AND session_id = ? LIMIT 1",
        (source_id, session_id),
    )
    return row is not None
