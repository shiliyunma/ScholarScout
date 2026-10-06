"""引用语义支持度校验（命令行）。

    # 先看要审哪些对（不计费）
    python eval/verify_support.py --session ses_xxx --dry-run

    # 真审（默认复用库里已有判定，不重复计费）
    python eval/verify_support.py --session ses_xxx --out eval/reports

**为什么默认复用已有判定**：判一条要花一次模型调用，而同一件产物可能被反复审
（每次调提示词/改口径都重跑）。库里已有同 (artifact, item) 的判定就跳过 ——
要强制重审加 `--refresh`。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scholarscout.app import build_context  # noqa: E402
from scholarscout.config import load_config  # noqa: E402
from scholarscout.grading import support  # noqa: E402


def load_artifacts(
    db: Any, session_id: str | None, *, kind: str | None, artifact_id: str | None
) -> list[tuple[str, str]]:
    """返回 ``[(kind, id)]``。按会话取全部产物，或直接指定一件。"""
    if artifact_id:
        if kind not in {"topic_map", "route"}:
            raise SystemExit("--id 需要同时给 --kind topic_map|route")
        return [(kind, artifact_id)]
    found: list[tuple[str, str]] = []
    for table, name in (("topic_maps", "topic_map"), ("learning_routes", "route")):
        sql = f"SELECT id FROM {table}"
        params: tuple[object, ...] = ()
        if session_id:
            sql += " WHERE session_id = ?"
            params = (session_id,)
        sql += " ORDER BY rowid DESC"
        found.extend((name, str(row["id"])) for row in db.query(sql, params))
    return found if session_id else found[:2]


def render(report: dict[str, Any]) -> str:
    lines = ["# 引用语义支持度校验", ""]
    lines.append(f"- 判定模型：`{report['model']}`｜提示词版本：`{report['prompt_version']}`")
    lines.append(
        f"- 待审对：{report['pairs']}｜已判定：{report['checked']}｜无法解析：{report['unknown']}"
    )
    lines.append("")
    lines.append("| 产物 | 条目 | 支持 | 部分支持 | 不支持 | 支持率 | 证据等级 |")
    lines.append("|---|---|---|---|---|---|---|")
    for row in report["items"]:
        summary = row["summary"]
        lines.append(
            "| {kind} `{artifact}` | {items} | {s} | {p} | {u} | {rate} | {level} |".format(
                kind=row["artifact_kind"],
                artifact=row["artifact_id"],
                items=summary["checked"],
                s=summary["counts"]["supported"],
                p=summary["counts"]["partial"],
                u=summary["counts"]["unsupported"],
                rate=(
                    "—"
                    if summary["supported_rate"] is None
                    else f"{summary['supported_rate'] * 100:.1f}%"
                ),
                level=row["level_mix"],
            )
        )
    levels = report["by_evidence_level"]
    lines += [
        "",
        "按证据等级分层（**必须分开看**：标题级证据算进来的支持率会被字面重合污染）：",
        "",
    ]
    lines.append("| 证据等级 | 已判定 | 支持 | 部分支持 | 不支持 | 支持率（含部分支持） |")
    lines.append("|---|---|---|---|---|---|")
    for level, data in levels.items():
        lines.append(
            "| {name} | {n} | {s} | {p} | {u} | {rate} |".format(
                name="强（含摘要与要点）" if level == "summary" else "弱（仅标题与元数据）",
                n=data["checked"],
                s=data["counts"]["supported"],
                p=data["counts"]["partial"],
                u=data["counts"]["unsupported"],
                rate=(
                    "—"
                    if data["supported_or_partial_rate"] is None
                    else f"{data['supported_or_partial_rate'] * 100:.1f}%"
                ),
            )
        )
    if report["weak_cases"]:
        lines += ["", "## 弱支持清单（交人工复核）", ""]
        lines.append("| 产物 | 条目 | 判定 | 证据等级 | 断言 | 理由 |")
        lines.append("|---|---|---|---|---|---|")
        for case in report["weak_cases"]:
            lines.append(
                "| {kind} | {item} | {verdict} | {level} | {claim} | {reason} |".format(
                    kind=case["artifact_kind"],
                    item=case["item_id"],
                    verdict=case["verdict"],
                    level=case["evidence_level"],
                    claim=str(case["claim"])[:60],
                    reason=str(case["reason"])[:60],
                )
            )
    lines += [
        "",
        "> 口径：`supported_rate = supported / checked`；`unknown`（输出不可解析）**不计入**分子分母。",
        "> 这是**模型判定**，不是人工金标准；论文里只能作为「语义支持度的模型估计」，并给出抽样核对方案。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify_support", description="引用语义支持度校验")
    parser.add_argument("--session", default=None, help="审这个会话的全部产物")
    parser.add_argument("--kind", default=None, choices=["topic_map", "route"])
    parser.add_argument("--id", dest="artifact_id", default=None, help="只审这一件产物")
    parser.add_argument("--limit", type=int, default=20, help="最多判多少条（控制花费），默认 20")
    parser.add_argument("--refresh", action="store_true", help="忽略库里已有判定，强制重审")
    parser.add_argument("--dry-run", action="store_true", help="只列待审对，不调模型")
    parser.add_argument("--out", default=str(ROOT / "eval" / "reports"))
    parser.add_argument("--db", default=None, help="数据库路径，默认 run/state.db")
    args = parser.parse_args(argv)

    cfg = load_config()
    ctx = build_context(config=cfg, db_path=args.db or cfg.db_path)
    try:
        artifacts = load_artifacts(
            ctx.db, args.session, kind=args.kind, artifact_id=args.artifact_id
        )
        if not artifacts:
            print("没有找到产物（换 --session 或 --id）")
            return 1

        prompt = support.load_prompt()
        version = support.prompt_version(prompt)
        report: dict[str, Any] = {
            "generated_at": time.strftime("%Y%m%d-%H%M%S"),
            "model": cfg.chat.model,
            "prompt_version": version,
            "pairs": 0,
            "checked": 0,
            "unknown": 0,
            "counts": {verdict: 0 for verdict in support.VERDICTS},
            "by_evidence_level": {},
            "weak_cases": [],
            "items": [],
        }

        # 汇总所有产物（报告按产物列一行，同时给全量分层口径）
        merged: list[tuple[support.SupportPair, support.SupportVerdict]] = []
        for kind, artifact_id in artifacts:
            pairs = support.build_pairs(ctx.db, kind, artifact_id)
            done = (
                {}
                if args.refresh
                else {
                    str(row["item_id"]): str(row["verdict"])
                    for row in support.list_checks(ctx.db, artifact_id)
                }
            )
            rows: list[tuple[support.SupportPair, support.SupportVerdict]] = []
            level_mix = {"summary": 0, "title": 0}
            for pair in pairs[: args.limit]:
                level_mix[pair.evidence_level] += 1
                if pair.item_id in done:
                    verdict = done[pair.item_id]
                    rows.append(
                        (pair, support.SupportVerdict(verdict=verdict, reason="（复用库中判定）"))
                    )
                    continue
                if args.dry_run:
                    print(
                        f"[待审] {kind} {pair.item_id} 证据={pair.evidence_level} 断言={pair.claim[:60]}"
                    )
                    continue
                result = support.judge(ctx.llm, pair, prompt, version=version)
                support.record_check(ctx.db, pair, result, session_id=args.session)
                rows.append((pair, result))
            if args.dry_run:
                continue
            summary = support.summarise(rows)
            merged.extend(rows)
            report["items"].append(
                {
                    "artifact_kind": kind,
                    "artifact_id": artifact_id,
                    "summary": summary,
                    "level_mix": "强 {} / 弱 {}".format(level_mix["summary"], level_mix["title"]),
                }
            )

        if args.dry_run:
            print(f"\n提示词版本 {version}｜以上为待审清单（dry-run 未调用模型）")
            return 0

        overall = support.summarise(merged)
        report.update(
            {
                "pairs": overall["pairs"],
                "checked": overall["checked"],
                "unknown": overall["unknown"],
                "counts": overall["counts"],
                "supported_rate": overall["supported_rate"],
                "supported_or_partial_rate": overall["supported_or_partial_rate"],
                "by_evidence_level": overall["by_evidence_level"],
                "weak_cases": [{"artifact_kind": kind, **case} for kind, case in _tag(merged)],
            }
        )
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{report['generated_at']}-support.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        markdown = render(report)
        (out_dir / f"{report['generated_at']}-support.md").write_text(markdown, encoding="utf-8")
        print(markdown)
        print(f"报告已写入 {out_dir / (report['generated_at'] + '-support.md')}")
        return 0
    finally:
        ctx.db.close()


def _tag(
    rows: list[tuple[support.SupportPair, support.SupportVerdict]],
) -> list[tuple[str, dict[str, Any]]]:
    """把弱支持清单带上产物类型（报告里要能看出是地图还是路线）。"""
    out: list[tuple[str, dict[str, Any]]] = []
    for pair, result in rows:
        if result.verdict in {"partial", "unsupported"}:
            out.append(
                (
                    pair.artifact_kind,
                    {
                        "item_id": pair.item_id,
                        "claim": pair.claim,
                        "source_ids": list(pair.source_ids),
                        "evidence_level": pair.evidence_level,
                        "verdict": result.verdict,
                        "reason": result.reason,
                    },
                )
            )
    return out


if __name__ == "__main__":
    sys.exit(main())
