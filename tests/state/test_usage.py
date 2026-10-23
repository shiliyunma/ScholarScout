"""`state/usage.py`：`session.usage` 两条读查询的 state 侧钉子。

原本 `rpc/session.py::session_usage` 自己写这两条 SQL（白名单 `rpc/session.py: 2`），
这一轮纯搬运。这几条只钉搬运之后**必须仍然成立**的口径，不测 RPC 回包形状
（那是 `tests/rpc/test_cancel_and_session_usage.py` 的事）：

- 没有记录时合计全为 0、按用途拆分为空 —— 不是 `None`、不是报错；
- `count(cached_tokens)` 只数**服务端报过缓存字段**的行：混合写入时这个数小于 `calls`，
  换成 `count(*)` 就会把"没报"算成"命中 0%"；
- `WHERE session_id = ?` 真的隔离：别的会话的行一条都不许进来；
- `GROUP BY 1` 绑的是 `purpose` 列、`ORDER BY calls DESC`：调用次数多的用途排前面。
"""

from __future__ import annotations

from scholarscout.state import traces, usage
from scholarscout.state.db import Database


def test_totals_and_breakdown_are_empty_without_records(db: Database) -> None:
    totals = usage.session_usage_totals(db, "sess_none")

    assert totals["calls"] == 0
    assert totals["prompt_tokens"] == 0
    assert totals["completion_tokens"] == 0
    assert totals["cached_tokens"] == 0
    assert totals["est_cost"] == 0.0
    assert usage.session_usage_by_purpose(db, "sess_none") == []


def test_totals_only_count_rows_that_reported_cache_fields(db: Database) -> None:
    session_id = "sess_usage"
    traces.record_usage(
        db,
        model="m",
        purpose="chat",
        session_id=session_id,
        prompt_tokens=100,
        completion_tokens=20,
        cached_tokens=40,
        est_cost=0.5,
    )
    traces.record_usage(
        db,
        model="m",
        purpose="chat",
        session_id=session_id,
        prompt_tokens=10,
        completion_tokens=5,
    )

    totals = usage.session_usage_totals(db, session_id)

    assert totals["calls"] == 2
    assert totals["prompt_tokens"] == 110
    assert totals["completion_tokens"] == 25
    assert totals["cached_tokens"] == 40
    assert totals["calls_with_cache_field"] == 1, (
        "只有一行报了缓存字段：count(cached_tokens) 不是 count(*)"
    )
    assert totals["est_cost"] == 0.5


def test_totals_do_not_leak_other_sessions(db: Database) -> None:
    traces.record_usage(db, model="m", purpose="chat", session_id="sess_a", prompt_tokens=100)
    traces.record_usage(db, model="m", purpose="chat", session_id="sess_b", prompt_tokens=999)

    totals = usage.session_usage_totals(db, "sess_a")

    assert totals["prompt_tokens"] == 100
    assert [row["calls"] for row in usage.session_usage_by_purpose(db, "sess_a")] == [1]


def test_by_purpose_groups_nulls_under_question_mark_and_sorts_by_calls(db: Database) -> None:
    session_id = "sess_purpose"
    for _ in range(2):
        traces.record_usage(
            db,
            model="m",
            purpose="chat",
            session_id=session_id,
            prompt_tokens=10,
            completion_tokens=1,
        )
    traces.record_usage(
        db,
        model="m",
        purpose="memory",
        session_id=session_id,
        prompt_tokens=7,
        completion_tokens=2,
    )
    # purpose 为 NULL 的行必须归到 '?'（不是被丢掉，也不是自成一列 None）
    db.execute(
        "INSERT INTO usage_records (id, session_id, model, purpose, prompt_tokens, created_at)"
        " VALUES ('usg_null_purpose', ?, 'm', NULL, 1, '2026-10-23T00:00:00.000000+08:00')",
        (session_id,),
    )

    rows = usage.session_usage_by_purpose(db, session_id)

    assert rows[0]["purpose"] == "chat", "调用次数最多的用途排最前（ORDER BY calls DESC）"
    assert rows[0]["calls"] == 2
    assert rows[0]["prompt_tokens"] == 20
    assert rows[0]["completion_tokens"] == 2
    # 同调用次数的两行之间**次序不钉**（SQLite 对相同排序键不作保证）：
    # 只要『?』这一列在，就说明 NULL 被 coalesce 归到了 '?'，没被丢掉也没自成一列 None。
    assert sorted(row["purpose"] for row in rows[1:]) == ["?", "memory"]
