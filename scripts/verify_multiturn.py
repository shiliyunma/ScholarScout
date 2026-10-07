"""真机多轮验证：A1/A2/A3/A6/A7 到底有没有用（跑真实模型 + 真实检索）。

用法（**会花真钱**，一次两轮约几分钱）：

    .venv/Scripts/python.exe scripts/verify_multiturn.py
    .venv/Scripts/python.exe scripts/verify_multiturn.py --goal "调研 X" --follow "继续找更多资料"

判据（四个都从库里取真值，不靠模型自述）：
  1. 第 2 轮有没有**新增来源**（改进前真机是 0）；
  2. 第 2 轮的查询有没有**重复**第 1 轮（判据与产品代码同一实现）；
  3. 服务端**缓存命中 token > 0**（A6 的可验证断言：前缀稳定才可能命中）；
  4. 结束原因是不是"有意义的停"（completed / no_new_evidence），而不是烧完预算。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scholarscout.app import build_context  # noqa: E402
from scholarscout.config import load_config  # noqa: E402
from scholarscout.runtime import loop  # noqa: E402
from scholarscout.runtime import research_state as rs  # noqa: E402
from scholarscout.state import sessions  # noqa: E402


def _search_calls(db, session_id: str) -> list[tuple[str, str]]:
    """这一会话的检索调用（时间序）：(turn_id, query)。"""
    rows = db.query(
        "SELECT turn_id, args_json FROM tool_traces"
        " WHERE session_id = ? AND tool_name = 'research.search' ORDER BY step",
        (session_id,),
    )
    out = []
    for row in rows:
        try:
            payload = json.loads(str(row["args_json"]) or "{}")
        except json.JSONDecodeError:
            payload = {}
        out.append((str(row["turn_id"] or ""), str(payload.get("query") or "")))
    return out


def _source_ids(db, session_id: str) -> list[str]:
    return [
        str(row["source_id"])
        for row in db.query(
            "SELECT DISTINCT source_id FROM source_mentions WHERE session_id = ?", (session_id,)
        )
    ]


def _usage(db, session_id: str) -> dict:
    row = db.query_one(
        "SELECT count(*) AS calls, coalesce(sum(prompt_tokens),0) AS prompt,"
        " coalesce(sum(completion_tokens),0) AS completion,"
        " coalesce(sum(cached_tokens),0) AS cached FROM usage_records WHERE session_id = ?",
        (session_id,),
    )
    return dict(row) if row else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多轮有效性真机验证")
    parser.add_argument("--goal", default="帮我调研一下 LLM Wiki 是什么、怎么用，值不值得投入")
    parser.add_argument("--follow", default="继续搜索更多资料")
    args = parser.parse_args(argv)

    config = load_config(ROOT / "config.yaml")
    ctx = build_context(config=config, db_path=config.db_path)
    session_id = sessions.create_session(ctx.db, model=config.chat.model)
    print(f"会话：{session_id}\n目标：{args.goal}\n追问：{args.follow}\n")

    before = set(_source_ids(ctx.db, session_id))
    turn1 = loop.run_turn(ctx.turn_context(), session_id=session_id, text=args.goal)
    after1 = set(_source_ids(ctx.db, session_id))
    calls1 = _search_calls(ctx.db, session_id)
    print(f"【第 1 轮】stop={turn1.stop_reason} 工具={len(turn1.tool_calls)} 检索={len(calls1)}")
    for _turn, query in calls1:
        print(f"    搜索：{query}")
    print(f"    新来源：{len(after1 - before)}")

    state = rs.load_state(ctx.db, session_id) or {}
    print(
        f"    子问题：{len(state.get('questions') or [])} 条 "
        f"{json.dumps(state.get('questions'), ensure_ascii=False)[:300]}"
    )
    if state.get("plan_error"):
        print(f"    ⚠ 子问题拆分失败：{state['plan_error']}")

    before2 = set(after1)
    turn2 = loop.run_turn(ctx.turn_context(), session_id=session_id, text=args.follow)
    after2 = set(_source_ids(ctx.db, session_id))
    calls2 = [item for item in _search_calls(ctx.db, session_id) if item not in calls1]
    print(f"\n【第 2 轮】stop={turn2.stop_reason} 工具={len(turn2.tool_calls)} 检索={len(calls2)}")
    for _turn, query in calls2:
        print(f"    搜索：{query}")
    fresh2 = after2 - before2
    print(f"    新来源：{len(fresh2)}")

    # 判据 ② 重复查询（与产品代码同一实现）
    repeats = []
    for _turn, query in calls2:
        prior = [q for _t, q in calls1 if q]
        match = next((p for p in prior if rs.similarity(query, p) >= rs.DEFAULT_SIMILARITY), None)
        if match:
            repeats.append((query, match))
    usage = _usage(ctx.db, session_id)
    gaps = rs.compute_gaps(ctx.db, session_id, {**state, "questions": state.get("questions") or []})
    print(f"\n台账缺口（{len(gaps)} 条）：")
    for gap in gaps:
        print(f"    [{gap['kind']}] {gap['detail']}")

    print(
        f"\n用量：调用 {usage.get('calls')} 次 / 输入 {usage.get('prompt')} / 输出 {usage.get('completion')}"
        f" / 缓存命中 {usage.get('cached')}"
    )

    checks = [
        ("第 2 轮有新来源（改进前实测 0）", len(fresh2) >= 1),
        ("第 2 轮没有重复查询", not repeats),
        ("服务端缓存命中 > 0（A6 前缀稳定）", int(usage.get("cached") or 0) > 0),
        ("结束原因是有意义的停", turn2.stop_reason in {"completed", "no_new_evidence"}),
        ("开局拆出了子问题清单（A1）", len(state.get("questions") or []) >= 3),
    ]
    print("\n判据：")
    for label, ok in checks:
        print(f"    [{'PASS' if ok else 'FAIL'}] {label}")
    if repeats:
        print(f"    重复明细：{repeats}")

    ctx.db.close()
    return 0 if all(ok for _label, ok in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
