"""资料索引的读查询（`library/index.py` 的全库清单 + 会话提及顺序，以及 `rpc/channel_file.py`
发文件时挑目标的那一条）。

前两条原本写在 `library/index.py`（白名单 `library/index.py: 2`），这一轮只是**位置**搬进
`state/`：列清单、JOIN 条件、排序键一个字没动。**业务边界仍留在 library 层** —— 会话目录名怎么
取（`naming.session_dir_map`）、缺失写不写 `unknown`、桶怎么拼（`paths.layout_dir`）、「同一个
来源的会话去重保序」都在调用方；这里只回**原始行**，不替调用方猜形状。

两条口径写在这里，是为了能用空库 / 多行直接试：

- 资料行按 ``fetched_at DESC, rowid DESC``：同一时刻入库的按写入顺序倒序 —— 少了 ``rowid``
  兜底，SQLite 对相同排序键的次序不作保证，「RPC 重建的索引与迁移脚本重建的逐字节一致」就断了；
- 提及行按 ``rowid`` 升序：最早的调研排最前，目录取第一条、索引列全部（与
  `state/archive.py::list_documents` 的分组口径同一套）。
"""

from __future__ import annotations

from typing import Any

from scholarscout.state.db import Database

#: 全库资料行：``documents`` 连自己的 ``sources``（标题/URL/域/类型都从来源侧取）。
_DOCUMENT_QUERY = """
    SELECT d.id AS doc_id, d.title AS doc_title, d.sha256,
           d.raw_path, d.text_path, d.fetched_at,
           s.id AS source_id, s.title AS source_title, s.url,
           s.domain, s.source_type, s.meta_json
    FROM documents d JOIN sources s ON s.id = d.source_id
    ORDER BY d.fetched_at DESC, d.rowid DESC
"""

#: 会话提及：按写入顺序（``rowid``），**不聚合不去重** —— 去重保序是调用方的事。
_MENTIONS_QUERY = """
    SELECT source_id, session_id FROM source_mentions ORDER BY rowid
"""

#: 发文件用的资料行：``documents`` 左连 ``sources``（只取标题），按入库时间倒序。
#:
#: **为什么不复用 `documents_with_sources`**：那条是 ``JOIN`` + ``d.title AS doc_title`` +
#: ``ORDER BY d.fetched_at DESC, d.rowid DESC``。列名与排序键都不一样 —— 换过去要么改调用方
#: 读的键，要么在同一个 ``fetched_at`` 上从"未定序"变成"后写入优先"，那都是**行为变化**，
#: 不是本轮的纯搬运。两条查询各自服务一个口径，就地写清，不合成一条。
_CANDIDATE_QUERY = """
        SELECT d.id AS doc_id, d.title, d.raw_path, d.text_path, d.fetched_at,
               s.title AS source_title
        FROM documents d LEFT JOIN sources s ON s.id = d.source_id
        ORDER BY d.fetched_at DESC
        """


def documents_with_sources(db: Database) -> list[dict[str, Any]]:
    """全库资料行（``documents`` JOIN ``sources``），按入库时间倒序；空库回空列表。"""
    return [dict(row) for row in db.query(_DOCUMENT_QUERY)]


def source_mentions(db: Database) -> list[dict[str, Any]]:
    """全部 ``{source_id, session_id}`` 提及行，按写入顺序（最早在前）；空库回空列表。"""
    return [dict(row) for row in db.query(_MENTIONS_QUERY)]


def documents_with_source_titles(db: Database) -> list[dict[str, Any]]:
    """发文件挑目标用的资料行：``doc_id`` / ``title`` / ``raw_path`` / ``text_path`` / ``fetched_at`` / ``source_title``，按入库时间倒序；空库回空列表。

    回的是**原始行**：标题取 ``title or source_title``、原件在不在盘上、候选清单怎么排，
    全在调用方（`rpc/channel_file.py`），这里一个判断都不做。
    """
    return [dict(row) for row in db.query(_CANDIDATE_QUERY)]
