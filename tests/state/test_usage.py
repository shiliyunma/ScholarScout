"""`state/usage.py`：`usage_records` 三条读查询的 state 侧钉子。

前两条原本在 `rpc/session.py::session_usage`（白名单 `rpc/session.py: 2`），第三条原本在
`runtime/budget.py::usage_since`（白名单 `runtime/budget.py: 1`），都是纯搬运。这几条只钉搬运之后
**必须仍然成立**的口径，不测回包形状（那是 `tests/rpc/test_cancel_and_session_usage.py` 与
`tests/runtime/test_notify_and_budget.py` 的事）：

- 没有记录时合计全为 0、按用途拆分为空 —— 不是 `None`、不是报错；
- `count(cached_tokens)` 只数**服务端报过缓存字段**的行：混合写入时这个数小于 `calls`，
  换成 `count(*)` 就会把"没报"算成"命中 0%"；
- `WHERE session_id = ?` 真的隔离：别的会话的行一条都不许进来；
- `GROUP BY 1` 绑的是 `purpose` 列、`ORDER BY calls DESC`：调用次数多的用途排前面；
- 滚动时间窗那条**跨会话**（没有 session_id 过滤）、按 `julianday` 挡掉窗口外的老行，
  且 `est_cost` 不兜底：空表回 `None`、只数 `count(est_cost)` 非空的行。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


# ------------------------------------------------- 滚动时间窗（每日封顶的取数口）


def _bucket_row(
    db: Database,
    usage_id: str,
    *,
    created_at: str,
    session_id: str = "sess_w",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    est_cost: float | None = None,
) -> None:
    """直接写行：`record_usage` 的 `created_at` 只能是「现在」，测不了窗口边界。"""
    db.execute(
        "INSERT INTO usage_records (id, session_id, model, purpose, prompt_tokens,"
        " completion_tokens, est_cost, created_at) VALUES (?, ?, 'm', 'chat', ?, ?, ?, ?)",
        (usage_id, session_id, prompt_tokens, completion_tokens, est_cost, created_at),
    )


def _iso_hours_ago(hours: float) -> str:
    moment = datetime.now(UTC).astimezone() - timedelta(hours=hours)
    return moment.isoformat(timespec="microseconds")


def test_window_totals_on_empty_table_keep_est_cost_null(db: Database) -> None:
    """一条记录都没有时合计全 0，但金额是 ``None`` 而不是 ``0.0``（没配单价 ≠ 花了 0 元）。"""
    row = usage.usage_window_totals(db, _iso_hours_ago(24))

    assert row["calls"] == 0
    assert row["prompt_tokens"] == 0
    assert row["completion_tokens"] == 0
    assert row["est_cost"] is None
    assert row["priced_calls"] == 0


def test_window_totals_span_sessions_and_drop_rows_before_since(db: Database) -> None:
    """跨会话汇总，且 ``julianday(created_at) >= julianday(?)`` 真的把窗口外的老行挡掉。"""
    _bucket_row(db, "usg_a", created_at=_iso_hours_ago(1), prompt_tokens=100, completion_tokens=10)
    _bucket_row(
        db,
        "usg_b",
        created_at=_iso_hours_ago(23),
        session_id="sess_other",
        prompt_tokens=200,
        completion_tokens=20,
    )
    _bucket_row(
        db, "usg_old", created_at=_iso_hours_ago(30), session_id="sess_other", prompt_tokens=9999
    )

    row = usage.usage_window_totals(db, _iso_hours_ago(24))

    assert row["calls"] == 2, "窗口外的老行不许进来"
    assert row["prompt_tokens"] == 300, "两个会话的行都要算（跨会话，没有 session_id 过滤）"
    assert row["completion_tokens"] == 30


def test_window_totals_count_only_priced_rows_for_cost(db: Database) -> None:
    """``sum(est_cost)`` 跳过空单价的行，``count(est_cost)`` 单独给出「有价格的行数」。"""
    _bucket_row(db, "usg_priced", created_at=_iso_hours_ago(1), est_cost=0.5)
    _bucket_row(db, "usg_free", created_at=_iso_hours_ago(1), est_cost=None)

    row = usage.usage_window_totals(db, _iso_hours_ago(24))

    assert row["calls"] == 2
    assert row["est_cost"] == 0.5
    assert row["priced_calls"] == 1, "count(est_cost) 只数非空 —— 两行里只有一行有价格"
