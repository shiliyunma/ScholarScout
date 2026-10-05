"""检索封装：FTS5(trigram) + 短查询 LIKE 兜底。

决议 6：只检索**摘要**与**会话记录**；正文不入库、不进 FTS。
本机实测（SQLite 3.42.0）：trigram 要求查询 >= 3 字符，两字中文（"教育""智能"）命不中，
因此短查询必须走 LIKE —— 见 docs/02 §4.1 与 §11 的硬验收项。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scholarscout.state.db import Database

#: 短于这个长度的查询走 LIKE（trigram 的硬限制）
FTS_MIN_QUERY_CHARS: int = 3

_LIKE_ESCAPE = "\\"


@dataclass(frozen=True)
class Hit:
    """统一的检索结果，两条路径返回同一结构，上层不需要知道自己走的是哪条。"""

    kind: str  # "summary" | "message"
    id: str
    score: float
    snippet: str
    path: str  # "fts" | "like"
    extra: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "id": self.id,
            "score": self.score,
            "snippet": self.snippet,
            "path": self.path,
            **self.extra,
        }


def normalize_query(query: str) -> str:
    return " ".join(query.split())


def query_path(query: str) -> str:
    """返回该查询会走哪条路：``"fts"`` 或 ``"like"``。"""
    return "fts" if len(normalize_query(query)) >= FTS_MIN_QUERY_CHARS else "like"


def _fts_phrase(query: str) -> str:
    """把用户输入包成一个 FTS5 短语，避免 ``-`` `*` `"` 之类被当成语法。"""
    return '"' + query.replace('"', '""') + '"'


def _like_pattern(query: str) -> str:
    """LIKE 模式：转义 ``%`` ``_`` 与转义符本身。"""
    escaped = (
        query.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def _snippet(*parts: str | None, limit: int = 120) -> str:
    text = " ".join(p.strip() for p in parts if p and p.strip())
    return text[:limit]


def search_summaries(db: Database, query: str, *, limit: int = 20) -> list[Hit]:
    """检索摘要（中文标题 + 短摘要 + 要点）。"""
    cleaned = normalize_query(query)
    if not cleaned or limit <= 0:
        return []

    if query_path(cleaned) == "fts":
        rows = db.query(
            """
            SELECT s.id AS id, s.title_zh, s.summary_short, bm25(summaries_fts) AS rank
            FROM summaries_fts
            JOIN summaries s ON s.rowid = summaries_fts.rowid
            WHERE summaries_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (_fts_phrase(cleaned), limit),
        )
        return [
            Hit(
                kind="summary",
                id=str(row["id"]),
                score=-float(row["rank"]),
                snippet=_snippet(row["title_zh"], row["summary_short"]),
                path="fts",
                extra={"title_zh": row["title_zh"] or ""},
            )
            for row in rows
        ]

    pattern = _like_pattern(cleaned)
    rows = db.query(
        """
        SELECT id, title_zh, summary_short
        FROM summaries
        WHERE title_zh LIKE ? ESCAPE ? OR summary_short LIKE ? ESCAPE ?
              OR points_json LIKE ? ESCAPE ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (pattern, _LIKE_ESCAPE, pattern, _LIKE_ESCAPE, pattern, _LIKE_ESCAPE, limit),
    )
    return [
        Hit(
            kind="summary",
            id=str(row["id"]),
            score=0.0,
            snippet=_snippet(row["title_zh"], row["summary_short"]),
            path="like",
            extra={"title_zh": row["title_zh"] or ""},
        )
        for row in rows
    ]


def search_messages(
    db: Database,
    query: str,
    *,
    session_id: str | None = None,
    limit: int = 20,
) -> list[Hit]:
    """检索会话消息（决议 6 的另一半）。"""
    cleaned = normalize_query(query)
    if not cleaned or limit <= 0:
        return []

    if query_path(cleaned) == "fts":
        params: list[object] = [_fts_phrase(cleaned)]
        session_filter = ""
        if session_id:
            session_filter = "AND m.session_id = ?"
            params.append(session_id)
        params.append(limit)
        rows = db.query(
            f"""
            SELECT m.id AS id, m.session_id, m.role, m.content, bm25(messages_fts) AS rank
            FROM messages_fts
            JOIN messages m ON m.rowid = messages_fts.rowid
            WHERE messages_fts MATCH ? {session_filter}
            ORDER BY rank
            LIMIT ?
            """,
            params,
        )
        return [
            Hit(
                kind="message",
                id=str(row["id"]),
                score=-float(row["rank"]),
                snippet=_snippet(row["content"]),
                path="fts",
                extra={"session_id": row["session_id"], "role": row["role"]},
            )
            for row in rows
        ]

    pattern = _like_pattern(cleaned)
    params = [pattern, _LIKE_ESCAPE]
    session_filter = ""
    if session_id:
        session_filter = "AND session_id = ?"
        params.append(session_id)
    params.append(limit)
    rows = db.query(
        f"""
        SELECT id, session_id, role, content
        FROM messages
        WHERE content LIKE ? ESCAPE ? {session_filter}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        params,
    )
    return [
        Hit(
            kind="message",
            id=str(row["id"]),
            score=0.0,
            snippet=_snippet(row["content"]),
            path="like",
            extra={"session_id": row["session_id"], "role": row["role"]},
        )
        for row in rows
    ]


def search_all(db: Database, query: str, *, limit: int = 20) -> dict[str, list[Hit]]:
    """一次查两处：摘要 + 会话。返回 ``path`` 便于评测区分检索算法。"""
    return {
        "summaries": search_summaries(db, query, limit=limit),
        "messages": search_messages(db, query, limit=limit),
    }
