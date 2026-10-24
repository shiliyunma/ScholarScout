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
from eval import report  # noqa: E402

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
                    # 第二把尺子：以「最小充分轨迹」为基准（1 search + 1 grade + ≤K fetch）。
                    # 只增字段，上面的原口径字段一个不改。
                    "redundant_traj": dm.traj_turn_excess(plain, ref),
                    "tca_traj": dm.tool_call_accuracy_traj(plain, ref),
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
                # --- 第二把尺子（轨迹口径）：与原口径并列，不影响上面任何字段 ---
                "redundant_calls_traj": sum(t["redundant_traj"] for t in per_turn),
                "tca_traj_mean": _mean([x["tca_traj"] for x in per_turn]),
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
        # --- 第二把尺子（轨迹口径，1 search + 1 grade + ≤K fetch）：只在末尾追加 ---
        "redundant_calls_traj": sum(d["redundant_calls_traj"] for d in ds),
        "tool_call_accuracy_traj": _mean([t["tca_traj"] for t in turns]),
    }


def rescore(src_json: Path) -> dict[str, Any]:
    """从**已有报告**的 ``per_turn`` 重算两个口径 —— **零成本**：不建上下文、不发请求、不读库。

    只读 ``per_turn[].actual`` / ``per_turn[].gold``；旧口径的每个数也在此**重算一遍**
    并与报告里存的值逐字段比对（``old_mismatch`` 必须为 0），用来证明"改代码没动旧指标"。
    """
    src = json.loads(src_json.read_text(encoding="utf-8"))
    if src.get("mode") != "live":
        raise ValueError(f"只能重算 live 报告（这份的 mode={src.get('mode')!r}）")
    old_mismatch: list[str] = []
    rows: list[dict[str, Any]] = []
    all_turns: list[dict[str, Any]] = []
    for d in src["dialogues"]:
        per_turn: list[dict[str, Any]] = []
        for t in d["per_turn"]:
            actual = [{"name": c["name"], "args": c.get("args") or {}} for c in t["actual"]]
            gold = [{"name": c["name"], "args": c.get("args") or {}} for c in t["gold"]]
            row: dict[str, Any] = {
                "i": t["i"],
                "user": t["user"],
                "gold": [c["name"] for c in gold],
                "actual": [c["name"] for c in actual],
                "redundant": max(0, len(actual) - len(gold)),
                "tca_both": dm.tool_call_accuracy(actual, gold),
                "tca_name": dm.tool_call_accuracy(actual, gold, match="name"),
                "tc_f1_both": dm.tool_call_f1(actual, gold),
                "tc_f1_name": dm.tool_call_f1(actual, gold, match="name"),
                "redundant_traj": dm.traj_turn_excess(actual, gold),
                "tca_traj": dm.tool_call_accuracy_traj(actual, gold),
            }
            for key in ("redundant", "tca_both", "tca_name", "tc_f1_both", "tc_f1_name"):
                if t.get(key) != row[key]:
                    old_mismatch.append(
                        f"{d['id']}/t{t['i']}:{key} 存={t.get(key)!r} 算={row[key]!r}"
                    )
            per_turn.append(row)
        all_turns.extend(per_turn)
        rows.append(
            {
                "id": d["id"],
                "topic": d["topic"],
                "n_turns": len(per_turn),
                "n_calls_actual": len([c for t in per_turn for c in t["actual"]]),
                "n_calls_gold": len([c for t in per_turn for c in t["gold"]]),
                "redundant_calls": sum(t["redundant"] for t in per_turn),
                "redundant_calls_traj": sum(t["redundant_traj"] for t in per_turn),
                "tca_name_mean": _mean([t["tca_name"] for t in per_turn]),
                "tca_traj_mean": _mean([t["tca_traj"] for t in per_turn]),
                "trajectory_exact_match": d.get("trajectory_exact_match"),
                "trajectory_inclusion": d.get("trajectory_inclusion"),
                "goal_accuracy": d.get("goal_accuracy"),
                "per_turn": per_turn,
            }
        )
    summary = {
        "n_dialogues": len(rows),
        "n_turns": len(all_turns),
        "n_calls_actual": sum(r["n_calls_actual"] for r in rows),
        "n_calls_gold": sum(r["n_calls_gold"] for r in rows),
        "redundant_calls": sum(r["redundant_calls"] for r in rows),
        "tool_call_accuracy_both": _mean([t["tca_both"] for t in all_turns]),
        "tool_call_accuracy_name": _mean([t["tca_name"] for t in all_turns]),
        "tool_call_f1_both": _mean([t["tc_f1_both"] for t in all_turns]),
        "tool_call_f1_name": _mean([t["tc_f1_name"] for t in all_turns]),
        "tool_usage_mean": src.get("summary", {}).get("tool_usage_mean"),
        "trajectory_exact_match_rate": _mean(
            [1.0 if r["trajectory_exact_match"] else 0.0 for r in rows]
        ),
        "trajectory_inclusion_mean": _mean([r["trajectory_inclusion"] for r in rows]),
        "agent_goal_accuracy": _mean([1.0 if r["goal_accuracy"] else 0.0 for r in rows]),
        "redundant_calls_traj": sum(r["redundant_calls_traj"] for r in rows),
        "tool_call_accuracy_traj": _mean([t["tca_traj"] for t in all_turns]),
    }
    stored = src.get("summary") or {}
    for key, value in summary.items():
        if key in stored and stored[key] != value:
            old_mismatch.append(f"summary:{key} 存={stored[key]!r} 算={value!r}")
    return {
        "mode": "rescore",
        "source": str(src_json),
        "source_summary": stored,
        "summary": summary,
        "dialogues": rows,
        "old_mismatch": old_mismatch,
    }


def write_two_scales(res: dict[str, Any], src_json: Path) -> tuple[Path, Path]:
    """把两把尺子并排写成 ``<原报告名>-two-scales.{json,md}``（**不覆盖**原报告）。"""
    stem = src_json.name[: -len(".json")] if src_json.name.endswith(".json") else src_json.name
    json_path = src_json.with_name(f"{stem}-two-scales.json")
    md_path = src_json.with_name(f"{stem}-two-scales.md")
    json_path.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    s = res["summary"]
    old_mismatch = res["old_mismatch"]
    lines = [
        f"# E4 两个口径并排（源报告 {src_json.name}）",
        "",
        "> **零成本重算**：本文件由 `python eval/run_dialogues.py --rescore "
        f"{src_json.as_posix()}` 从源报告的 `per_turn[].actual` 直接重算，"
        "**未调用模型、未读库**。原报告一个字节未动。",
        ">",
        f"> 旧字段重算比对：`{len(old_mismatch)}` 处不一致"
        + ("（与源报告逐字段相同）" if not old_mismatch else f"：{old_mismatch}"),
        "",
        f"## 总计（{s['n_dialogues']} 对话 / {s['n_turns']} 轮，"
        f"实际 {s['n_calls_actual']} 次调用 / gold {s['n_calls_gold']} 次）",
        "",
        "| 口径 | 冗余 | TCA(名字) | 说明 |",
        "|---|---|---|---|",
        f"| 原口径（gold 即上限） | {s['redundant_calls']} | {_fmt(s['tool_call_accuracy_name'])} | "
        "冗余 = 实际调用数 − gold 调用数；gold **只标了每轮第一步**（1 次 `research.search`） |",
        f"| **轨迹口径**（1 search + 1 grade + ≤{dm.TRAJ_FETCH_K} fetch） | "
        f"**{s['redundant_calls_traj']}** | **{_fmt(s['tool_call_accuracy_traj'])}** | "
        "冗余只数**超出阶段预算**的调用；预算内的检索/判权/抓取不算冗余 |",
        "",
        "**两句必读**：",
        "",
        "1. **两种尺子量的是不同东西**：原口径量「与 gold 标注的吻合度」，轨迹口径量"
        "「一趟最小充分轨迹有没有被撑爆」。前者不是系统做错了，而是 gold 只标了第一步 —— "
        "拿 gold 当上限，任何认真做事的系统都必然 3~5 倍冗余。",
        "2. 轨迹口径**不是给系统开脱**：同一批对话仍有 "
        f"{s['redundant_calls_traj']} 次超额调用（同一轮搜 4~5 次、抓取超 {dm.TRAJ_FETCH_K} 次），"
        "这部分是真冗余，两条口径都算在账上。",
        "",
        "## 逐对话",
        "",
        "| 对话 | 轮数 | 实际/gold | 原口径冗余 | 轨迹口径冗余 | 原 TCA(名) | 轨迹 TCA(名) |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in res["dialogues"]:
        lines.append(
            f"| {d['id']} | {d['n_turns']} | {d['n_calls_actual']}/{d['n_calls_gold']} | "
            f"{d['redundant_calls']} | {d['redundant_calls_traj']} | "
            f"{_fmt(d['tca_name_mean'])} | {_fmt(d['tca_traj_mean'])} |"
        )
    lines += ["", "## 逐轮（重算明细）", ""]
    for d in res["dialogues"]:
        lines += [
            f"### {d['id']} · {d['topic']}",
            "",
            "| 轮 | 用户 | gold | 实际 | 原冗余 | 轨迹冗余 | 原 TCA(名) | 轨迹 TCA(名) |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for t in d["per_turn"]:
            lines.append(
                f"| {t['i']} | {t['user']} | {_names(t['gold'])} | "
                f"{_names(t['actual']) or '（无调用）'} | {t['redundant']} | "
                f"{t['redundant_traj']} | {_fmt(t['tca_name'])} | {_fmt(t['tca_traj'])} |"
            )
        lines.append("")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="E4 六对话集：对话过程评分")
    ap.add_argument("--live", action="store_true", help="真跑模型（需 .env 密钥与网络）")
    ap.add_argument("--plan", action="store_true", help="只看题面与 gold，不跑模型")
    ap.add_argument("--limit", type=int, default=None, help="最多跑前 N 个对话")
    ap.add_argument("--only", default=None, help="只跑这些对话 id（逗号分隔，如 c4,c5）")
    ap.add_argument(
        "--rescore",
        default=None,
        metavar="报告.json",
        help="不跑模型：从已有报告的 per_turn 重算轨迹口径，写 <同名>-two-scales.{md,json}",
    )
    ap.add_argument(
        "--out",
        default="eval/reports",
        help="报告输出位置：以 .json 结尾＝文件路径（同名 .md 并列）；否则＝目录",
    )
    ap.add_argument(
        "--keep-data",
        action="store_true",
        help="评测的临时根（库 + data/）跑完不删，留在系统临时目录里供排查（默认删掉）",
    )
    args = ap.parse_args(argv)

    if args.rescore:
        src = Path(args.rescore)
        res = rescore(src)
        written = write_two_scales(res, src)
        print(f"写出 {written[0].resolve()} / {written[1].resolve()}")
        print(f"  旧字段重算不一致 {len(res['old_mismatch'])} 处")
        for k in ("redundant_calls", "redundant_calls_traj"):
            print(f"  {k} = {res['summary'][k]}")
        for k in ("tool_call_accuracy_name", "tool_call_accuracy_traj"):
            print(f"  {k} = {_fmt(res['summary'][k])}")
        return 0

    rows = load_dialogues()
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        rows = [r for r in rows if r.get("id") in wanted]
        if not rows:
            print("--only 没匹配到任何对话 id")
            return 1
    out = Path(args.out)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if args.plan or not args.live:
        res = run_plan(rows)
        written = _write_reports(res, out, stamp)
    else:
        from dataclasses import replace

        from scholarscout import paths
        from scholarscout.app import build_context
        from scholarscout.config import load_config

        # **先读配置与密钥，再换根。** `home()` 一换，`config.yaml` 与 `.env` 的默认位置
        # 也跟着换（两者都拼在 `paths.home()` 上）—— 换完再 `load_config()` 就是「参数全默认、
        # 密钥全没有」。所以这一步显式用**换根之前**的家目录读配置；`load_config()` 内部
        # `load_dotenv()` 会把密钥灌进 `os.environ`，之后换根只动路径解析，不碰进程环境。
        base_home = paths.home()
        cfg = load_config(base_home / "config.yaml")

        # 隔离：临时库与临时 data/ 同源（都从被覆盖后的 `home()` 推出来）。
        # 不这么做的话库在 temp、`source.fetch` 的原件/全文却写进用户真实的 `data/raw`，
        # 跑完库没了、文件留下成孤儿（实测一次 E4 全跑留 43 个）。
        with paths.isolated_home(prefix="scs-eval-", keep=args.keep_data) as tmp_path:
            cfg = replace(cfg, data_dir=paths.data_dir(), db_path=paths.db_path())
            ctx = build_context(config=cfg, db_path=cfg.db_path)
            try:
                res = run_live(ctx, rows, limit=args.limit)
                res["summary"] = summarize(res)
                res["db_mode"] = "temporary (paths.isolated_home)"
                if args.keep_data:
                    # 保留时把位置写进报告：否则没人知道去哪儿找
                    res["data_root"] = str(tmp_path)
                    res["db_path"] = str(cfg.db_path)
            finally:
                # 先关库再退出 with，减少 Windows 上的句柄残留
                # （关库失败不该盖掉上面的评分结果，所以只抑制、不抛）
                with contextlib.suppress(Exception):
                    ctx.db.close()
            written = _write_reports(res, out, stamp)
        if args.keep_data:
            print(f"临时根（--keep-data 保留）：{tmp_path}")

    print(f"写出 {written[0].resolve()} / {written[1].resolve()}")
    if res["mode"] == "live":
        for k, v in res["summary"].items():
            print(f"  {k} = {_fmt(v)}")
    else:
        print(f"  轮数 {res['total_turns']}，gold 参数问题 {len(res['arg_problems'])} 处")
    return 0


def _write_reports(res: dict[str, Any], out: Path, stamp: str) -> tuple[Path, Path]:
    """落盘 json + md，返回**实际写出的两个路径**（调用方据此打印，别自己拼路径猜）。

    `--out` 的解析在 `report.resolve_report_paths`：以 `.json` 结尾就是文件路径
    （`.md` 同名换后缀），否则按目录处理（`<out>/<时间戳>-<集名>.{json,md}`）。
    """
    json_path, md_path = report.resolve_report_paths(out, f"{stamp}-e4dialogues")
    json_path.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

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
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def _fmt(v: Any) -> str:
    return "—" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))


def _names(names: list[str]) -> str:
    """把工具名列表渲染成「每个名各包一层反引号、空格分隔」，表格里不会粘成一串。"""
    return " ".join(f"`{n}`" for n in names)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
