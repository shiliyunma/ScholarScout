"""跨会话记忆的落库（`user_facts` 表，只读字段口径见 docs/02 §5）。

本模块只做「一条记忆的一行」的增删改：入参是领域值（文本/类别/账本/时间），
返回空或行数，不暴露 ``sqlite3.Connection``。抽取、判重、预算裁剪都在
``runtime/memory.py``，它们属于领域判断，不属于状态层。
"""

from __future__ import annotations

from scholarscout.state.db import Database


def insert_fact(
    db: Database,
    *,
    fact_id: str,
    text: str,
    kind: str,
    target: str,
    source_session: str | None,
    created_at: str,
) -> None:
    """新增一条记忆。同 id 重复插入由主键拒绝（调用方负责生成新 id）。"""
    db.execute(
        "INSERT INTO user_facts (id, text, kind, target, source_session, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (fact_id, text, kind, target, source_session, created_at),
    )


def update_fact(
    db: Database, fact_id: str, *, text: str, kind: str | None, updated_at: str
) -> None:
    """改一条记忆的正文；``kind=None`` 表示**类别不动**（SQL 里也就不出现该列）。"""
    if kind is not None:
        db.execute(
            "UPDATE user_facts SET text = ?, kind = ?, updated_at = ? WHERE id = ?",
            (text, kind, updated_at, fact_id),
        )
    else:
        db.execute(
            "UPDATE user_facts SET text = ?, updated_at = ? WHERE id = ?",
            (text, updated_at, fact_id),
        )


def delete_fact(db: Database, fact_id: str) -> None:
    """删一条记忆（调用方负责先确认它存在，好给出准确的 404 文案）。"""
    db.execute("DELETE FROM user_facts WHERE id = ?", (fact_id,))
