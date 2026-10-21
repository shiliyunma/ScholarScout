"""工具轨迹、用量与外部服务状态（表结构见 docs/02 §5）。

`tool_traces` 是论文里所有"调用次数 / 成功率 / 时延"指标的**唯一取数入口**，
所以每次工具调用都必须落一行，包括失败与未注册的情况。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from scholarscout.state.db import Database
from scholarscout.state.migrate import now_iso
from scholarscout.state.sessions import new_id

_DIGEST_LEN = 16


def digest(payload: Any) -> str:
    """稳定摘要，避免把大结果整段塞进轨迹表。"""
    text = (
        payload
        if isinstance(payload, str)
        else json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_DIGEST_LEN]


def record_trace(
    db: Database,
    *,
    session_id: str,
    turn_id: str,
    step: int,
    tool_name: str,
    ok: bool,
    args: Any = None,
    result: Any = None,
    error: str | None = None,
    latency_ms: int | None = None,
) -> str:
    trace_id = new_id("trc")
    db.execute(
        """
        INSERT INTO tool_traces (id, session_id, turn_id, step, tool_name, args_json,
                                 args_digest, result_digest, ok, error, latency_ms, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trace_id,
            session_id,
            turn_id,
            step,
            tool_name,
            json.dumps(args, ensure_ascii=False) if args is not None else None,
            digest(args) if args is not None else None,
            digest(result) if result is not None else None,
            1 if ok else 0,
            error,
            latency_ms,
            now_iso(),
        ),
    )
    return trace_id


def count_tool_calls(db: Database, *, session_id: str) -> int:
    """这个会话**累计发生过**多少次工具调用（含失败与未注册的，跨轮）。

    为什么用本表而不是另建计数表：`tool_traces` 是"调用次数"的唯一取数入口（见模块文档），
    每次调用都**当场**落一行 —— 于是会话级工具预算不需要内存计数器，也不需要新表，
    **重启进程后照样算得准**（`tests/runtime/test_session_tool_budget.py` 钉着这条）。

    口径边界（如实写清）：子任务（`research.delegate`）内部的调用也按主会话的 `session_id`
    落轨迹，所以**同样计入**本会话的额度 —— 它们烧的是同一个会话的钱。
    """
    row = db.query_one(
        "SELECT count(*) AS calls FROM tool_traces WHERE session_id = ?", (session_id,)
    )
    return int(row["calls"]) if row is not None else 0


def list_traces(
    db: Database,
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
    tool_name: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[object] = []
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if turn_id:
        clauses.append("turn_id = ?")
        params.append(turn_id)
    if tool_name:
        clauses.append("tool_name = ?")
        params.append(tool_name)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = db.query(
        f"""
        SELECT id, session_id, turn_id, step, tool_name, args_json, ok, error,
               latency_ms, created_at
        FROM tool_traces {where}
        ORDER BY created_at DESC, step DESC
        LIMIT ?
        """,
        params,
    )
    return [dict(r) for r in rows]


def record_usage(
    db: Database,
    *,
    model: str,
    purpose: str,
    session_id: str | None = None,
    turn_id: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cached_tokens: int | None = None,
    est_cost: float | None = None,
) -> str:
    usage_id = new_id("usg")
    db.execute(
        """
        INSERT INTO usage_records (id, session_id, turn_id, model, purpose,
                                   prompt_tokens, completion_tokens, cached_tokens, est_cost, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            usage_id,
            session_id,
            turn_id,
            model,
            purpose,
            prompt_tokens,
            completion_tokens,
            cached_tokens,
            est_cost,
            now_iso(),
        ),
    )
    return usage_id


def record_provider_status(
    db: Database,
    *,
    provider: str,
    ok: bool,
    http_status: int | None = None,
    latency_ms: int | None = None,
    note: str | None = None,
) -> str:
    status_id = new_id("pst")
    db.execute(
        """
        INSERT INTO provider_status (id, provider, at, ok, http_status, latency_ms, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (status_id, provider, now_iso(), 1 if ok else 0, http_status, latency_ms, note),
    )
    return status_id
