"""多轮有效性诊断：从真实会话里量化"为什么只有第一轮有用"。

只看三件事（都能从库里直接算，不靠感觉）：

1. **第 2 轮起的检索查询有多少与前面重复** —— 重复率高＝"继续搜索"只是在原地打转；
2. **每轮新发现多少条来源** —— 新增趋近 0＝这一轮对结论没有贡献；
3. **每轮的工具构成** —— 第 1 轮会 search→fetch→grade，第 2 轮若只剩 search，
   说明它没有"换策略"的能力（也没有任何机制告诉它该换）。

用法：``.venv/Scripts/python.exe scripts/diagnose_turns.py [--db run/state.db] [--limit 20]``
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "src"))

from scholarscout.runtime import research_state as rs  # noqa: E402


# 判据与产品代码**共用同一个实现**（A3 的判重闸门用的就是它）：
# 两处各写一份"相似度"迟早会给出两套数字，"用同一把尺子复测改进"就无从谈起。
def _norm(query: str) -> str:
    return rs.normalize_query(query)


def _similar(a: str, b: str) -> float:
    """二字符组 Jaccard —— 与产品代码同一实现（A3 的判重闸门调的就是它）。"""
    return rs.similarity(a, b)


def _query_of(args_json: str) -> str:
    try:
        payload = json.loads(args_json or "{}")
    except json.JSONDecodeError:
        return ""
    return str(payload.get("query") or payload.get("url") or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="多轮有效性诊断")
    parser.add_argument("--db", default=str(ROOT / "run" / "state.db"))
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument(
        "--threshold", type=float, default=rs.DEFAULT_SIMILARITY, help="查询相似度阈值（判为重复）"
    )
    args = parser.parse_args()
    if not Path(args.db).exists():
        print(f"[FAIL] 找不到库：{args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    sessions = conn.execute(
        """
        SELECT s.id, s.title, count(m.id) AS user_msgs
        FROM sessions s JOIN messages m ON m.session_id = s.id AND m.role = 'user'
        GROUP BY s.id HAVING user_msgs >= 2
        ORDER BY s.updated_at DESC LIMIT ?
        """,
        (args.limit,),
    ).fetchall()

    print(f"多轮会话（用户消息 ≥ 2）：{len(sessions)} 个\n")
    totals: Counter[str] = Counter()
    print(f"{'会话':<16}{'轮次':>4}{'工具调用':>7}{'重复查询':>9}{'新来源':>7}  第 2 轮起工具构成")
    for session in sessions:
        # 轮次边界用**时间戳**：`tool_traces.message_id` 目前是空的（追溯链缺口，另外记一笔），
        # 所以按"这条用户消息到最后一条用户消息之前"的时间区间取工具调用。
        user_rows = conn.execute(
            "SELECT rowid, created_at FROM messages WHERE session_id = ? AND role = 'user' ORDER BY rowid",
            (session["id"],),
        ).fetchall()
        bounds = [str(row["created_at"]) for row in user_rows] + ["\uffff"]
        seen_queries: list[str] = []
        seen_sources: set[str] = set()
        dup_here = new_here = 0
        per_turn_tools: list[Counter[str]] = []
        for index in range(len(user_rows)):
            calls = conn.execute(
                """
                SELECT tool_name, args_json, result_digest FROM tool_traces
                WHERE session_id = ? AND created_at >= ? AND created_at < ?
                ORDER BY step
                """,
                (session["id"], bounds[index], bounds[index + 1]),
            ).fetchall()
            tools: Counter[str] = Counter(row["tool_name"] for row in calls)
            per_turn_tools.append(tools)
            queries = [
                _query_of(row["args_json"])
                for row in calls
                if row["tool_name"] == "research.search"
            ]
            for query in filter(None, queries):
                norm = _norm(query)
                if index > 0 and any(
                    _similar(norm, prev) >= args.threshold for prev in seen_queries
                ):
                    dup_here += 1
                else:
                    totals["fresh_queries"] += 1
                seen_queries.append(norm)
            for row in calls:
                _ = row
            # 新来源从 `source_mentions` 取（`result_digest` 是摘要，不含 id —— 这也是个可改进点）
            mentions = conn.execute(
                """
                SELECT source_id FROM source_mentions
                WHERE session_id = ? AND created_at >= ? AND created_at < ?
                """,
                (session["id"], bounds[index], bounds[index + 1]),
            ).fetchall()
            for mention in mentions:
                found = str(mention["source_id"] or "")
                if found and found not in seen_sources:
                    seen_sources.add(found)
                    if index > 0:
                        new_here += 1
        tools_after = sum((per_turn_tools[i] for i in range(1, len(per_turn_tools))), Counter())
        tools_first = per_turn_tools[0] if per_turn_tools else Counter()
        totals["sessions"] += 1
        totals["dup_queries"] += dup_here
        totals["new_sources_after_first"] += new_here
        if len(bounds) > 2 and tools_after:
            totals["later_turns"] += 1
            if dup_here > 0:
                totals["later_turns_with_dup"] += 1
        mix = " ".join(
            f"{name.split('.')[-1]}×{count}" for name, count in tools_after.most_common()
        )
        print(
            f"{(session['title'] or session['id'])[:14]:<16}{len(bounds) - 1:>4}"
            f"{sum(sum(t.values()) for t in per_turn_tools):>7}{dup_here:>9}{new_here:>7}  {mix or '-'}"
        )
        _ = tools_first

    print("\n---- 汇总 ----")
    print(
        f"第 2 轮起的**重复查询**条数：{totals['dup_queries']}（判据：与前面查询二字符组相似度 ≥ {args.threshold}）"
    )
    print(f"第 2 轮起**新发现的来源**条数：{totals['new_sources_after_first']}")
    if totals["later_turns"]:
        ratio = totals["later_turns_with_dup"] / totals["later_turns"]
        print(
            f"有过重复查询的多轮会话占比：{ratio:.0%}（{totals['later_turns_with_dup']}/{totals['later_turns']}）"
        )
    print(f"第 1 轮之后**首次出现的新查询**总数：{totals['fresh_queries']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
