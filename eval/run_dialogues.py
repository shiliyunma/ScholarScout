"""跑 E4 六对话集，并对**对话过程**评分（轨迹级 + 轮级）。

    python eval/run_dialogues.py --live --out eval/reports          # 真跑（需密钥与网络）
    python eval/run_dialogues.py --plan                             # 只看题面与 gold，不跑模型

与 `run_eval.py` 的分工：
- `run_eval.py` 管 E1（判权，离线确定性）/ E2（守卫，离线）/ E3（单轮端到端）。
- **本脚本管 E4：多轮对话**，补齐"**对话过程**"这一层 —— E3 只发一条消息，
  量不出"第 3 轮有没有引用第 2 轮的 route_id"这类**跨轮状态依赖**。

口径不在这里自定，三条来源逐条对齐：
- RAGAS *Agents or Tool use*：`ToolCallAccuracy` / `ToolCallF1` / `AgentGoalAccuracy` / `TopicAdherence`
- TRAJECT-Bench（arXiv:2510.04550）：`Trajectory Exact-Match` / `Trajectory Inclusion` / `Tool-Usage`
- τ-bench（arXiv:2406.12045）：`pass^k`（本脚本留出多轮试验的接口，默认 1 次）

沿用项目约定：**分母为 0 的指标为 null 而不是 0**（`docs/05 §3`）。
E4 与 E3 同类：**要密钥与网络，逐次不承诺复现，不进门槛**。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# 让 `python eval/run_dialogues.py` 直接可跑（与 run_eval.py 同样的做法）
ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT / "src", ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from eval import dialogue_metrics as dm  # noqa: E402

SETS_DIR = Path(__file__).resolve().parent / "sets"
#: 每个对话的产物下限（code-based 的目标验收，不请模型判）—— 数据源：题目的 artifact_min
#: ⚠️ `documents` **没有 session_id 列**（表结构：id/source_id/…/fetched_at），
#: 所以"本会话抓到了文档没有"必须经 `source_mentions` 关联（它有 session_id 与 doc_id）。
#: 直接用 `documents WHERE session_id=?` 会抛异常 → 被吞成 None → **把做对的判成失败**（已踩过）。
ARTIFACT_SQL = {
    "topic_maps": "SELECT count(*) FROM topic_maps WHERE session_id = ?",
    "learning_routes": "SELECT count(*) FROM learning_routes WHERE session_id = ?",
    "notes": "SELECT count(*) FROM notes WHERE session_id = ?",
    "documents": "SELECT count(*) FROM source_mentions WHERE session_id = ? AND doc_id IS NOT NULL",
}


def load_dialogues() -> list[dict[str, Any]]:
    path = SETS_DIR / "e4_dialogues.jsonl"
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _traces_to_calls(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 `tool_traces` 行转成评分用的调用列表。

    `args_json` 是落库的字符串，这里解析回 dict；解析失败也不丢记录
    （记成空 args，由 `arg_valid` 判为参数不合法，不静默跳过）。
    """
    calls: list[dict[str, Any]] = []
    for r in rows:
        raw = r.get("args_json")
        try:
            args = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
        except json.JSONDecodeError:
            args = {}
        calls.append(
            {
                "name": r.get("tool_name"),
                "args": args,
                "_ok": bool(r.get("ok")),
                "_ms": r.get("latency_ms"),
                "_error": r.get("error"),
                "_step": r.get("step"),
                "_turn": r.get("turn_id"),
            }
        )
    return calls


def run_plan(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """--plan：不跑模型，只输出题面、gold 轨迹、并校验 gold 参数是否合法。"""
    out = []
    for d in rows:
        if d.get("type") != "dialogue":
            continue
        turns = []
        for t in d["turns"]:
            bad: list[str] = []
            for c in t["gold_tools"]:
                good, probs = dm.arg_valid(c)
                if not good:
                    bad.extend(probs)
            turns.append(
                {
                    "i": t["i"],
                    "user": t["user"],
                    "gold_tools": [c["name"] for c in t["gold_tools"]],
                    "bad_args": bad,
                }
            )
        out.append(
            {
                "id": d["id"],
                "topic": d["topic"],
                "direction_src": d.get("direction_src"),
                "turns": turns,
                "gold_trajectory": d["gold_trajectory"],
                "n_turns": len(d["turns"]),
            }
        )
    return {
        "mode": "plan",
        "dialogues": out,
        "total_turns": sum(x["n_turns"] for x in out),
        "arg_problems": [
            (x["id"], t["i"], t["bad_args"]) for x in out for t in x["turns"] if t["bad_args"]
        ],
    }


def run_live(ctx: Any, rows: list[dict[str, Any]], *, limit: int | None = None) -> dict[str, Any]:
    """真跑：每个对话一个 session，逐轮发问，按轮收集 tool_traces 并评分。"""
    from scholarscout.rpc.methods import dispatch
    from scholarscout.state import traces

    results: list[dict[str, Any]] = []
    for d in rows:
        if d.get("type") != "dialogue":
            continue
        if limit is not None and len(results) >= limit:
            break
        session_id = dispatch(ctx, "session.create", {"title": f"E4 {d['id']} {d['topic']}"})[
            "session_id"
        ]
        per_turn: list[dict[str, Any]] = []
        # 逐轮归属：用 trace 的 **id 差集**，不用 step 号 ——
        # `step` 是**轮内**编号（每轮从 1 重数），拿它做跨轮游标会把后面几轮的调用全漏掉；
        # 且 `list_traces` 是 `ORDER BY created_at DESC, step DESC`（新的在前），
        # 所以取完差集还要按 (created_at, step) **升序**还原真实调用顺序。
        seen_ids: set[str] = set()
        for t in d["turns"]:
            dispatch(ctx, "chat.send", {"session_id": session_id, "text": t["user"]})
            all_rows = [
                dict(r) for r in traces.list_traces(ctx.db, session_id=session_id, limit=500)
            ]
            new_rows = [r for r in all_rows if str(r.get("id")) not in seen_ids]
            seen_ids = {str(r.get("id")) for r in all_rows}
            new_rows.sort(key=lambda r: (str(r.get("created_at") or ""), int(r.get("step") or 0)))
            actual = _traces_to_calls(new_rows)
            ref = t["gold_tools"]
            plain = [{k: v for k, v in c.items() if not k.startswith("_")} for c in actual]
            per_turn.append(
                {
                    "i": t["i"],
                    "user": t["user"],
                    "actual": plain,
                    "gold": [{"name": c["name"], "args": c["args"]} for c in ref],
                    "n_traces": len(new_rows),
                    # 两种口径都算：both = 名字+参数逐字相等；name = 只看工具选择
                    "tca_both": dm.tool_call_accuracy(plain, ref),
                    "tca_name": dm.tool_call_accuracy(plain, ref, match="name"),
                    "tc_f1_both": dm.tool_call_f1(plain, ref),
                    "tc_f1_name": dm.tool_call_f1(plain, ref, match="name"),
                    "tool_usage": dm.tool_usage_rate(actual),
                    # TRAJECT-Bench 的第三类失败模式「冗余调用」：实际调用数 − gold 调用数
                    "redundant": max(0, len(plain) - len(ref)),
                    "errors": [c["_error"] for c in actual if c["_error"]],
                }
            )
        flat_actual = [
            {"name": c["name"], "args": c["args"]} for turn in per_turn for c in turn["actual"]
        ]
        flat_gold = [
            {"name": c["name"], "args": c["args"]} for turn in per_turn for c in turn["gold"]
        ]
        # `None` 表示"这一件查不出来"（表/列不存在），与"查到 0 件"必须分得开，
        # 所以值类型是 `int | None`，不能靠循环体推成纯 `int`。
        artifact: dict[str, int | None] = {}
        for key, sql in ARTIFACT_SQL.items():
            try:
                artifact[key] = int(ctx.db.scalar_int(sql, (session_id,)) or 0)
            except Exception:
                artifact[key] = None
        need = d.get("artifact_min") or {}
        goal_ok = bool(need) and all((artifact.get(k) or 0) >= v for k, v in need.items())
        results.append(
            {
                "id": d["id"],
                "topic": d["topic"],
                "session_id": session_id,
                "per_turn": per_turn,
                "trajectory_exact_match": dm.trajectory_exact_match(flat_actual, flat_gold),
                "trajectory_inclusion": dm.trajectory_inclusion(flat_actual, flat_gold),
                "n_calls_actual": len(flat_actual),
                "n_calls_gold": len(flat_gold),
                "redundant_calls": sum(t["redundant"] for t in per_turn),
                "tca_both_mean": _mean([x["tca_both"] for x in per_turn]),
                "tca_name_mean": _mean([x["tca_name"] for x in per_turn]),
                "tc_f1_both_mean": _mean([x["tc_f1_both"] for x in per_turn]),
                "tc_f1_name_mean": _mean([x["tc_f1_name"] for x in per_turn]),
                "tool_usage_mean": _mean([x["tool_usage"] for x in per_turn]),
                "artifact": artifact,
                "artifact_min": need,
                "goal_accuracy": goal_ok,
            }
        )
    return {"mode": "live", "dialogues": results}


def _mean(values: list[float | None]) -> float | None:
    """均值；无有效样本返回 None（沿用项目约定，不返 0）。"""
    items = [v for v in values if v is not None]
    if not items:
        return None
    return sum(items) / len(items)


def summarize(res: dict[str, Any]) -> dict[str, Any]:
    """汇总成报告用的小结。"""
    if res["mode"] != "live":
        return {}
    ds = res["dialogues"]
    turns = [t for d in ds for t in d["per_turn"]]
    return {
        "n_dialogues": len(ds),
        "n_turns": len(turns),
        "n_calls_actual": sum(d["n_calls_actual"] for d in ds),
        "n_calls_gold": sum(d["n_calls_gold"] for d in ds),
        "redundant_calls": sum(d["redundant_calls"] for d in ds),
        "tool_call_accuracy_both": _mean([t["tca_both"] for t in turns]),
        "tool_call_accuracy_name": _mean([t["tca_name"] for t in turns]),
        "tool_call_f1_both": _mean([t["tc_f1_both"] for t in turns]),
        "tool_call_f1_name": _mean([t["tc_f1_name"] for t in turns]),
        "tool_usage_mean": _mean([t["tool_usage"] for t in turns]),
        "trajectory_exact_match_rate": _mean(
            [1.0 if d["trajectory_exact_match"] else 0.0 for d in ds]
        ),
        "trajectory_inclusion_mean": _mean([d["trajectory_inclusion"] for d in ds]),
        "agent_goal_accuracy": _mean([1.0 if d["goal_accuracy"] else 0.0 for d in ds]),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="E4 六对话集：对话过程评分")
    ap.add_argument("--live", action="store_true", help="真跑模型（需 .env 密钥与网络）")
    ap.add_argument("--plan", action="store_true", help="只看题面与 gold，不跑模型")
    ap.add_argument("--limit", type=int, default=None, help="最多跑前 N 个对话")
    ap.add_argument("--only", default=None, help="只跑这些对话 id（逗号分隔，如 c4,c5）")
    ap.add_argument("--out", default="eval/reports")
    args = ap.parse_args(argv)

    rows = load_dialogues()
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        rows = [r for r in rows if r.get("id") in wanted]
        if not rows:
            print("--only 没匹配到任何对话 id")
            return 1
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if args.plan or not args.live:
        res = run_plan(rows)
        _write_reports(res, out_dir, stamp)
    else:
        import tempfile
        from dataclasses import replace

        from scholarscout.app import build_context
        from scholarscout.config import load_config

        # 与 run_eval.py 一致：**跑在临时库上**，跑完即删，不污染 data/state.db。
        # `ignore_cleanup_errors=True`：Windows 下 SQLite 句柄未及时释放会让清理抛
        # NotADirectoryError（把 state.db 当目录递归），**不能让它把报告冲掉**。
        with tempfile.TemporaryDirectory(prefix="scout-eval-", ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            cfg = replace(load_config(), data_dir=tmp_path / "data", db_path=tmp_path / "state.db")
            ctx = build_context(config=cfg, db_path=cfg.db_path)
            try:
                res = run_live(ctx, rows, limit=args.limit)
                res["summary"] = summarize(res)
                res["db_mode"] = "temporary (tempfile.TemporaryDirectory)"
            finally:
                # 先关库再退出 with，减少 Windows 上的句柄残留
                # （关库失败不该盖掉上面的评分结果，所以只抑制、不抛）
                with contextlib.suppress(Exception):
                    ctx.db.close()
            _write_reports(res, out_dir, stamp)

    print(f"写出 {out_dir}/{stamp}-e4dialogues.{{json,md}}")
    if res["mode"] == "live":
        for k, v in res["summary"].items():
            print(f"  {k} = {_fmt(v)}")
    else:
        print(f"  轮数 {res['total_turns']}，gold 参数问题 {len(res['arg_problems'])} 处")
    return 0


def _write_reports(res: dict[str, Any], out_dir: Path, stamp: str) -> None:
    """落盘 json + md（与 run_eval.py 的产物命名一致：`<时间戳>-<集名>.{json,md}`）。"""
    (out_dir / f"{stamp}-e4dialogues.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [f"# E4 六对话过程评分（{stamp}）", ""]
    if res["mode"] == "plan":
        lines += [
            f"题面校验：{res['total_turns']} 轮，gold 参数问题 {len(res['arg_problems'])} 处",
            "",
        ]
        for d in res["dialogues"]:
            lines.append(f"## {d['id']} · {d['topic']}  ({d['n_turns']} 轮)")
            lines.append(f"方向来源：{d['direction_src']}")
            lines.append("")
            lines.append("| 轮 | 用户 | gold 工具 |")
            lines.append("|---|---|---|")
            for t in d["turns"]:
                lines.append(f"| {t['i']} | {t['user']} | `{'` `'.join(t['gold_tools'])}` |")
            lines.append("")
    else:
        s = res["summary"]
        lines += [
            f"数据库模式：{res.get('db_mode', '—')}（跑完即删，未污染 data/state.db）",
            "",
            "| 指标 | 值 | 来源 |",
            "|---|---|---|",
            f"| 对话数 / 轮数 | {s['n_dialogues']} / {s['n_turns']} | — |",
            f"| 工具调用次数（实际 / gold） | {s['n_calls_actual']} / {s['n_calls_gold']} | TRAJECT-Bench 冗余诊断 |",
            f"| **冗余调用数**（实际 − gold） | **{s['redundant_calls']}** | TRAJECT-Bench「Redundant tool calling」 |",
            f"| Tool Call Accuracy（轮均，名字+参数，严格序） | {_fmt(s['tool_call_accuracy_both'])} | RAGAS（code-based 口径）|",
            f"| Tool Call Accuracy（轮均，**只看工具选择**） | {_fmt(s['tool_call_accuracy_name'])} | RAGAS（code-based 口径）|",
            f"| Tool Call F1（轮均，名字+参数） | {_fmt(s['tool_call_f1_both'])} | RAGAS（code-based 近似）|",
            f"| Tool Call F1（轮均，只看工具选择） | {_fmt(s['tool_call_f1_name'])} | RAGAS（code-based 近似）|",
            f"| Tool-Usage 参数合法率 | {_fmt(s['tool_usage_mean'])} | TRAJECT-Bench |",
            f"| Trajectory Exact-Match 率 | {_fmt(s['trajectory_exact_match_rate'])} | TRAJECT-Bench |",
            f"| Trajectory Inclusion（均值） | {_fmt(s['trajectory_inclusion_mean'])} | TRAJECT-Bench |",
            f"| Agent Goal Accuracy（产物验收，code-based） | {_fmt(s['agent_goal_accuracy'])} | RAGAS |",
            "",
            '> ⚠️ **口径提醒**：RAGAS 官方 `ToolCallF1` 由 **LLM 判**"两次调用算不算同一次"；',
            "> 本表是 **code-based 近似**（名字+参数逐字相等 / 只看工具名两版），可复算但更严。",
            "> 两套数字**不要混着比**（arXiv:2507.21504 §4.3）。",
            ">",
            "> 未纳入本表的（需 LLM-as-judge 或人工）：",
            "> Faithfulness、Response Relevancy、TopicAdherence、Context Precision/Recall。",
            "",
        ]
        for d in res["dialogues"]:
            lines.append(f"## {d['id']} · {d['topic']}")
            lines.append(
                f"TEM={d['trajectory_exact_match']}  "
                f"Inclusion={_fmt(d['trajectory_inclusion'])}  "
                f"GoalAccuracy={d['goal_accuracy']}  "
                f"调用 {d['n_calls_actual']}/{d['n_calls_gold']}（冗余 {d['redundant_calls']}）  "
                f"产物={d['artifact']}（下限 {d['artifact_min']}）"
            )
            lines.append("")
            lines.append(
                "| 轮 | 用户 | gold | 实际 | TCA(名+参) | TCA(名) | F1(名) | 参数 | 冗余 |"
            )
            lines.append("|---|---|---|---|---|---|---|---|---|")
            for t in d["per_turn"]:
                g = "` `".join(c["name"] for c in t["gold"])
                a = "` `".join(c["name"] for c in t["actual"]) or "（无调用）"
                lines.append(
                    f"| {t['i']} | {t['user']} | {g} | {a} | "
                    f"{_fmt(t['tca_both'])} | {_fmt(t['tca_name'])} | {_fmt(t['tc_f1_name'])} | "
                    f"{_fmt(t['tool_usage'])} | {t['redundant']} |"
                )
            lines.append("")
    (out_dir / f"{stamp}-e4dialogues.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fmt(v: Any) -> str:
    return "—" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
