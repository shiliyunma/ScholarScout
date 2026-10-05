"""诊断：E3 产物为什么 ungrounded（把产物原文和丢弃的引用 dump 出来）。

E3 报告只给一个 `ungrounded 比例`，看不出失败形态。这个脚本跑真模型 + 真出网，
把每个任务的主题图/路线**原文**落盘，逐节点打印：
    label / 保留的 source_ids / **被丢掉的（不存在的）source_ids**

两种失败形态的修法完全不同：
- `被丢掉的` 非空 → 模型**编了 source_id**（或格式不对）→ 修引用的解析/约束
- `被丢掉的` 为空 → 模型**根本没给出处**（拿 [无出处] 顶着）→ 修提示词的目标函数

用法：
    .venv/Scripts/python.exe scripts/diagnose_grounding.py --task e3-04
    .venv/Scripts/python.exe scripts/diagnose_grounding.py --limit 1
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scholarscout.app import build_context  # noqa: E402
from scholarscout.config import load_config, load_dotenv  # noqa: E402
from scholarscout.rpc.methods import dispatch  # noqa: E402


def load_tasks(task_id: str | None, limit: int) -> list[dict]:
    rows = [
        json.loads(line)
        for line in (ROOT / "eval" / "sets" / "e3_tasks.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("//")
    ]
    if task_id:
        rows = [row for row in rows if row.get("id") == task_id]
    return rows[:limit]


def dump_task(ctx, row: dict, out: list[dict]) -> None:
    session_id = dispatch(ctx, "session.create", {"title": f"diag {row['id']}"})["session_id"]
    started = time.perf_counter()
    dispatch(ctx, "chat.send", {"session_id": session_id, "text": row["request"]})
    elapsed_s = round(time.perf_counter() - started, 1)

    report: dict = {
        "id": row["id"],
        "topic": row.get("topic"),
        "elapsed_s": elapsed_s,
        "maps": [],
        "routes": [],
    }
    for map_row in ctx.db.query("SELECT * FROM topic_maps WHERE session_id = ?", (session_id,)):
        graph = json.loads(str(map_row["graph_json"]))
        nodes = [
            {
                "nid": node["nid"],
                "label": node["label"],
                "kept": node.get("source_ids") or [],
                "dropped": node.get("dropped_source_ids") or [],
            }
            for node in graph.get("nodes") or []
        ]
        report["maps"].append(
            {
                "mode": graph.get("mode"),
                "stats": graph.get("stats"),
                "nodes": nodes,
                "edges": len(graph.get("edges") or []),
            }
        )
    for plan_row in ctx.db.query(
        "SELECT * FROM learning_routes WHERE session_id = ?", (session_id,)
    ):
        plan = json.loads(str(plan_row["plan_json"]))
        items = [
            {
                "kind": item.get("kind"),
                "title": item.get("title"),
                "kept": item.get("source_id"),
                "dropped": item.get("dropped_source_id"),
            }
            for week in plan.get("weeks") or []
            for item in week.get("items") or []
        ]
        report["routes"].append(
            {"mode": plan.get("mode"), "stats": plan.get("stats"), "items": items}
        )

    out.append(report)
    print_task(report)


def print_task(report: dict) -> None:
    print(f"\n########## {report['id']}（{report['topic']}）耗时 {report['elapsed_s']}s ##########")
    for index, graph in enumerate(report["maps"], start=1):
        stats = graph["stats"] or {}
        print(
            f"--- 主题图 #{index} mode={graph['mode']} 节点={stats.get('nodes')} 边={graph['edges']} "
            f"无出处={stats.get('ungrounded_nodes')} 丢弃边={stats.get('dropped_edges')}"
        )
        for node in graph["nodes"]:
            mark = "无出处" if not node["kept"] else "有出处"
            dropped = f"  ← 丢了 {node['dropped']}" if node["dropped"] else ""
            print(f"    [{mark}] {node['label'][:40]:42s} kept={len(node['kept'])}{dropped}")
    for index, plan in enumerate(report["routes"], start=1):
        stats = plan["stats"] or {}
        print(
            f"--- 路线 #{index} mode={plan['mode']} 条目={stats.get('items')} "
            f"资料类={stats.get('resource_items')} 行动类={stats.get('exercise_items')} "
            f"无出处={stats.get('ungrounded_items')}"
        )
        for item in plan["items"]:
            if item["kept"]:
                mark = "有出处"
            elif item.get("kind") == "exercise":
                mark = "行动类"  # 练习/产出不声称出处，不是"无出处"
            else:
                mark = "无出处"
            print(f"    [{mark}] {str(item['title'])[:44]:46s} kept={item['kept']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="", help="只跑某一道（如 e3-04）")
    parser.add_argument("--limit", type=int, default=1, help="跑前 N 道（默认 1）")
    parser.add_argument("--out", default="run")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    base = load_config(ROOT / "config.yaml")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = replace(
        base,
        data_dir=out_dir / f"diag-{stamp}" / "data",
        db_path=out_dir / f"diag-{stamp}" / "state.db",
    )
    ctx = build_context(config=cfg, db_path=cfg.db_path)
    results: list[dict] = []
    try:
        for row in load_tasks(args.task or None, args.limit):
            dump_task(ctx, row, results)
    finally:
        ctx.db.close()
        if ctx.http is not None:
            ctx.http.close()

    path = out_dir / f"grounding-diag-{stamp}.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n原文已落盘：{path}")
    ungrounded = sum(
        int((graph["stats"] or {}).get("ungrounded_nodes") or 0)
        for report in results
        for graph in report["maps"]
    ) + sum(
        int((plan["stats"] or {}).get("ungrounded_items") or 0)
        for report in results
        for plan in report["routes"]
    )
    print(f"本次无出处条目合计：{ungrounded}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
