"""评测运行器：跑离线集（E1 判权 / E2 守卫）与端到端集（E3，需模型）。

    python eval/run_eval.py --set all --ablation --out eval/reports
    python eval/run_eval.py --set e3 --live --out eval/reports

口径见 docs/05-评测方案.md §3：**分母为 0 时指标为 null**（"没有样本"≠"全错"），
分母口径与文档逐条对应，报告里每一项都给出分子/分母。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterable
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

# 让 `python eval/run_eval.py` 直接可跑（与 scripts/scout.py 同样的做法）
ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT / "src", ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from eval import metrics, report  # noqa: E402
from scholarscout.app import build_context  # noqa: E402
from scholarscout.config import load_config  # noqa: E402
from scholarscout.grading.tiers import classify_source, load_rules  # noqa: E402
from scholarscout.providers.llm import create_client  # noqa: E402
from scholarscout.rpc.methods import dispatch  # noqa: E402
from scholarscout.runtime.prompts import build_system_prompt, system_prompt_hash  # noqa: E402
from scholarscout.runtime.toolctx import ToolContext  # noqa: E402
from scholarscout.state.ingest import upsert_source  # noqa: E402
from scholarscout.tools.route_plan import (  # noqa: E402
    RESOURCE_ITEM_KINDS,
    parse_plan,
)
from scholarscout.tools.route_plan import apply_guards as plan_guards  # noqa: E402
from scholarscout.tools.source_grade import source_grade  # noqa: E402
from scholarscout.tools.topic_map import apply_guards as map_guards  # noqa: E402
from scholarscout.tools.topic_map import parse_map  # noqa: E402

SETS_DIR = Path(__file__).resolve().parent / "sets"
#: E2 里 route 类题目的周数上限（超出即截断，行为写在题面的 label_note 里）
E2_ROUTE_WEEKS = 2
#: E3 允许一轮内跑完整链路（搜索→取原文→判权→地图→路线→卡片）
E3_TOOL_BUDGET = 12


def load_set(name: str) -> list[dict[str, Any]]:
    path = SETS_DIR / f"{name}.jsonl"
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _source_row(row: dict[str, Any]) -> dict[str, Any]:
    url = str(row["url"])
    domain = url.split("//", 1)[-1].split("/", 1)[0]
    return {
        "url": url,
        "url_canonical": url,
        "domain": domain,
        "title": row.get("title"),
        "source_type": row.get("source_type") or "web",
        "year": row.get("year"),
    }


def _tool_ctx(ctx: Any) -> ToolContext:
    return ToolContext(db=ctx.db, config=ctx.config, http=ctx.http, llm=ctx.llm)


# --------------------------------------------------------------------- E1 判权


def run_e1(ctx: Any, rows: list[dict[str, Any]], *, ablation: bool) -> dict[str, Any]:
    rules = load_rules(ctx.db)
    tier_pairs: list[tuple[str, str]] = []
    verdict_pairs: list[tuple[str, str]] = []
    norules_pairs: list[tuple[str, str]] = []
    details: list[dict[str, Any]] = []
    fallback_total = fallback_hit = 0
    low_quality_total = low_quality_lifted = 0

    for row in rows:
        source_id, _ = upsert_source(ctx.db, _source_row(row))
        result = source_grade(
            _tool_ctx(ctx), source_ids=[source_id], topic=row["topic"], use_model=False
        )
        item = result["data"]["items"][0]
        got_tier, got_verdict = str(item["tier"]), str(item["verdict"])
        want_tier, want_verdict = str(row["expect_tier"]), str(row["expect_verdict"])
        tier_pairs.append((got_tier, want_tier))
        verdict_pairs.append((got_verdict, want_verdict))

        matched = item.get("matched_rule")
        if not matched:
            fallback_total += 1
            if got_tier == "4":
                fallback_hit += 1
        if int(want_tier) >= 3:
            low_quality_total += 1
            if int(got_tier) <= 2:
                low_quality_lifted += 1

        details.append(
            {
                "id": row["id"],
                "url": row["url"],
                "matched_rule": matched,
                "tier": {"got": got_tier, "want": want_tier},
                "verdict": {"got": got_verdict, "want": want_verdict},
                "authority": item["authority"],
                "relevance": item["relevance"],
                "label_note": row.get("label_note", ""),
            }
        )

        if ablation:
            verdict = classify_source(
                ctx.db,
                {"url": row["url"], "domain": _source_row(row)["domain"]},
                [],
            )
            norules_pairs.append((str(verdict.tier), want_tier))

    disagreements = [
        detail
        for detail in details
        if detail["tier"]["got"] != detail["tier"]["want"]
        or detail["verdict"]["got"] != detail["verdict"]["want"]
    ]
    summary: dict[str, Any] = {
        "n": len(rows),
        "tier_agreement": metrics.agreement(tier_pairs),
        "verdict_agreement": metrics.agreement(verdict_pairs),
        "tier_kappa": metrics.cohens_kappa(tier_pairs),
        "verdict_kappa": metrics.cohens_kappa(verdict_pairs),
        "tier_confusion": metrics.confusion(tier_pairs, ["1", "2", "3", "4"]),
        "verdict_confusion": metrics.confusion(verdict_pairs, ["核心", "相关", "外围", "排除"]),
        "fallback_rate": metrics.float_rate(fallback_hit, fallback_total),
        "fallback_counts": {"hit": fallback_hit, "total": fallback_total},
        "lift_rate": metrics.float_rate(low_quality_lifted, low_quality_total),
        "lift_counts": {"lifted": low_quality_lifted, "total": low_quality_total},
        "disagreements": disagreements,
        "details": details,
        "rules_loaded": len(rules),
    }
    if ablation:
        summary["ablation_no_rules"] = {
            "tier_agreement": metrics.agreement(norules_pairs),
            "note": "判权退化为「全 tier 4」时的一致率（用来量化规则表的价值）",
        }
    return summary


# --------------------------------------------------------------------- E2 守卫


def run_e2(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    raw_grounded = raw_nodes = guarded_grounded = guarded_nodes = 0
    invariant_violations = 0

    for row in rows:
        allowed = set(row["allowed_ids"])
        expect = row["expect"]
        if row["kind"] == "topic_map":
            raw = parse_map(row["model_output"])
            guarded = map_guards(raw, allowed_ids=allowed, max_nodes=20)
            got = {
                "nodes": len(guarded["nodes"]),
                "ungrounded": guarded["stats"]["ungrounded_nodes"],
                "dropped_edges": guarded["stats"]["dropped_edges"],
            }
            parsed_empty = not raw["nodes"]
            raw_nodes += len(raw["nodes"])
            raw_grounded += sum(1 for node in raw["nodes"] if node.get("source_ids"))
            guarded_nodes += len(guarded["nodes"])
            guarded_grounded += sum(1 for node in guarded["nodes"] if node.get("source_ids"))
            for node in guarded["nodes"]:
                if not set(node["source_ids"]) <= allowed:
                    invariant_violations += 1
        else:
            raw = parse_plan(row["model_output"])
            guarded = plan_guards(raw, allowed_ids=allowed, weeks=E2_ROUTE_WEEKS)
            got = {
                "weeks": guarded["stats"]["weeks"],
                "items": guarded["stats"]["items"],
                "ungrounded": guarded["stats"]["ungrounded_items"],
                "dropped_duplicates": guarded["stats"]["dropped_duplicates"],
            }
            parsed_empty = not raw["weeks"]
            # 覆盖率只算资料类条目（exercise 不声称出处，计进去会虚低）—— 与 E3 同一口径
            raw_items = [
                item
                for week in raw["weeks"]
                for item in week["items"]
                if item.get("kind") in RESOURCE_ITEM_KINDS
            ]
            kept_items = [
                item
                for week in guarded["weeks"]
                for item in week["items"]
                if item.get("kind") in RESOURCE_ITEM_KINDS
            ]
            all_kept = [item for week in guarded["weeks"] for item in week["items"]]
            raw_nodes += len(raw_items)
            raw_grounded += sum(1 for item in raw_items if item.get("source_id"))
            guarded_nodes += len(kept_items)
            guarded_grounded += sum(1 for item in kept_items if item.get("source_id"))
            for item in all_kept:
                if item.get("source_id") is not None and item["source_id"] not in allowed:
                    invariant_violations += 1
            got["exercise_items"] = guarded["stats"].get("exercise_items")
            # route 类一律附上分钟数（题面可用它检查边界 clamp，而不必另设键名）
            got["minutes"] = sorted(item["est_minutes"] for item in all_kept)

        passed = all(
            got.get(key) == value for key, value in expect.items() if key != "parsed_empty"
        )
        if expect.get("parsed_empty"):
            passed = passed and parsed_empty
        cases.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "passed": passed,
                "expected": expect,
                "got": got,
                "label_note": row.get("label_note", ""),
            }
        )

    return {
        "n": len(rows),
        "passed": sum(1 for case in cases if case["passed"]),
        "pass_rate": metrics.float_rate(sum(1 for case in cases if case["passed"]), len(rows)),
        "invariant_violations": invariant_violations,
        "coverage_raw": metrics.float_rate(raw_grounded, raw_nodes),
        "coverage_guarded": metrics.float_rate(guarded_grounded, guarded_nodes),
        "counts": {"raw_entries": raw_nodes, "guarded_entries": guarded_nodes},
        "cases": cases,
    }


# ----------------------------------------------------------------------- E3 端到端


#: 用户"要产物"的措辞（判断模型有没有主动把选择权交出去 —— **启发式，不精确**）
ASK_PATTERNS = ("要不要", "需要我", "要我", "是否要", "需不需要", "要的话", "需要的话")
ARTIFACT_WORDS = ("地图", "路线", "抽背卡", "卡片", "知识图谱")

#: **题面**里"点名要产物"的措辞。用来分清楚两件完全不同的事：
#: "用户要了 → 模型照做"（正确）与"用户没要 → 模型擅自生成"（这才是要测的）。
#: 这是启发式，词表就在这儿，报告里要标注；分母只算"题面没要产物"的题。
REQUEST_ARTIFACT_WORDS = (
    "路线",
    "地图",
    "图谱",
    "抽背",
    "卡片",
    "清单",
    "计划",
    "顺序",
    "怎么安排",
    "怎么学",
    "从哪开始",
    "入门路径",
)

#: 按需档下"用户点单"的请求文本（对应界面上的三个按钮）
FOLLOW_UP_TEXT = (
    "请基于刚才取到的资料，把知识地图和学习路线都生成出来（topic.build_map + route.plan）。"
)


def _looks_like_asking(text: str) -> bool:
    """模型有没有在结尾问用户"要不要生成产物"。

    这是**措辞启发式**：既不是语义判断，也不是"用户是否满意"。报告里必须标注这一点，
    别把它当成用户满意度。确定性口径是 `on_demand_success_rate`（看库里有没有真多出产物）。
    """
    body = str(text or "")
    return any(pattern in body for pattern in ASK_PATTERNS) and any(
        word in body for word in ARTIFACT_WORDS
    )


def failure_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把失败的工具调用按工具名归并，带一次错误样本。

    为什么必须报这个：报告只给"工具成功率 88.9%"是**不够复算**的 —— 读者会追问"哪一次、哪个工具、
    报什么错"。`tool_traces` 里本来就存了 `error`，只是之前的报告没取（评测又跑在临时库上，
    跑完即删，事后无法回溯）。所以从这里就把明细带进报告。
    """
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("ok"):
            continue
        name = str(row.get("tool_name") or "?")
        item = grouped.setdefault(name, {"tool": name, "n": 0, "error": ""})
        item["n"] += 1
        if not item["error"] and row.get("error"):
            item["error"] = str(row["error"])[:160]
    return sorted(grouped.values(), key=lambda item: (-int(item["n"]), str(item["tool"])))


def _source_meta(db: Any, source_ids: Iterable[str]) -> list[dict[str, Any]]:
    """取这些来源的年份/类型/域名。

    **取不到就返回空**，绝不在报告里给来源安一个年份 —— 时效性指标一旦混入猜测值，
    整张表就不可信了。
    """
    ids = sorted({str(item) for item in source_ids if item})
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = db.query(
        f"SELECT id, year, source_type, domain FROM sources WHERE id IN ({placeholders})",
        tuple(ids),
    )
    return [dict(row) for row in rows]


def run_e3(  # pragma: no cover - 需真模型
    ctx: Any,
    rows: list[dict[str, Any]],
    *,
    auto_artifacts: bool = False,
    ask_artifacts: bool = False,
) -> dict[str, Any]:
    """E3 端到端真机跑。

    ``auto_artifacts=True`` = 对照臂（旧行为：提示词要求每轮自动出主题图 + 路线）。
    默认 False = 按需档：模型只在用户明确要了（或点了界面按钮）时才出产物。
    ``ask_artifacts=True`` = 首轮后再发一条"请生成"的消息，量**按需生成成功率** ——
    这个是确定性口径（看库里有没多出产物），不依赖措辞启发式。
    """
    from scholarscout.state import traces

    ctx.config = replace(
        ctx.config,
        runtime=replace(ctx.config.runtime, max_tool_calls_per_turn=E3_TOOL_BUDGET),
        generation=replace(ctx.config.generation, auto_artifacts=auto_artifacts),
    )
    tasks: list[dict[str, Any]] = []

    for row in rows:
        started = time.perf_counter()
        session_id = dispatch(ctx, "session.create", {"title": f"E3 {row['id']}"})["session_id"]
        result = dispatch(ctx, "chat.send", {"session_id": session_id, "text": row["request"]})
        first_text = str(result.get("text") or "")
        # 题面是不是"点名要了产物"？—— 这个判断决定后面那个指标的分母
        request_asks_artifact = any(word in str(row["request"]) for word in REQUEST_ARTIFACT_WORDS)
        artifact_count_sql = (
            "SELECT (SELECT count(*) FROM topic_maps WHERE session_id = ?)"
            " + (SELECT count(*) FROM learning_routes WHERE session_id = ?)"
            " + (SELECT count(*) FROM notes WHERE session_id = ?)"
        )
        first_artifacts = ctx.db.scalar_int(
            artifact_count_sql, (session_id, session_id, session_id)
        )
        addressed = False
        if ask_artifacts:
            dispatch(ctx, "chat.send", {"session_id": session_id, "text": FOLLOW_UP_TEXT})
            after = ctx.db.scalar_int(artifact_count_sql, (session_id, session_id, session_id))
            addressed = after > first_artifacts
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        traces_rows = [
            dict(item) for item in traces.list_traces(ctx.db, session_id=session_id, limit=200)
        ]
        maps = ctx.db.query("SELECT * FROM topic_maps WHERE session_id = ?", (session_id,))
        plans = ctx.db.query("SELECT * FROM learning_routes WHERE session_id = ?", (session_id,))

        grounded = total = ungrounded = exercises = 0
        sources_seen: set[str] = set()
        modes: list[str] = []
        reasons: list[str] = []
        for row_map in maps:
            graph = json.loads(str(row_map["graph_json"]))
            modes.append(str(graph.get("mode")))
            if graph.get("mode_reason"):
                reasons.append(str(graph["mode_reason"]))
            stats = graph.get("stats") or {}
            ungrounded += int(stats.get("ungrounded_nodes") or 0)
            nodes = graph.get("nodes") or []
            total += len(nodes)
            grounded += sum(1 for node in nodes if node.get("source_ids"))
            sources_seen |= {str(ref) for node in nodes for ref in (node.get("source_ids") or [])}
        for row_plan in plans:
            plan = json.loads(str(row_plan["plan_json"]))
            modes.append(str(plan.get("mode")))
            if plan.get("mode_reason"):
                reasons.append(str(plan["mode_reason"]))
            stats = plan.get("stats") or {}
            ungrounded += int(stats.get("ungrounded_items") or 0)
            # 分母只算**资料类**条目：行动类（exercise）不声称出处，
            # 计进去会把"安排了练习"误报成"没有出处"（口径见 docs/05 §4）
            items = [item for week in plan.get("weeks") or [] for item in week.get("items") or []]
            exercises += int(stats.get("exercise_items") or 0)
            resources = int(stats.get("resource_items") or 0)
            total += resources if stats.get("resource_items") is not None else len(items)
            grounded += (
                int(stats.get("grounded_resource_items") or 0)
                if stats.get("grounded_resource_items") is not None
                else sum(1 for item in items if item.get("source_id"))
            )
            sources_seen |= {str(item["source_id"]) for item in items if item.get("source_id")}

        usage = ctx.db.query_one(
            "SELECT coalesce(sum(prompt_tokens),0) AS pin, coalesce(sum(completion_tokens),0) AS pout"
            " FROM usage_records WHERE session_id = ?",
            (session_id,),
        )
        # 来源时效性与多样性：从**库里的元数据**算（引用到的来源的年份/类型/域名）。
        # 口径：未知年份单列、不进"近两年占比"的分母（取不到就不编）。
        meta = _source_meta(ctx.db, sources_seen)
        years = [item.get("year") for item in meta]
        now_year = date.today().year
        tasks.append(
            {
                "id": row["id"],
                "topic": row["topic"],
                "stop_reason": result["stop_reason"],
                "error": result.get("error"),
                "tool_calls": result["tool_calls"],
                "elapsed_ms": elapsed_ms,
                "traces": len(traces_rows),
                "failures": failure_summary(traces_rows),
                "tool_success_rate": metrics.success_rate(traces_rows),
                "latency_p50": metrics.percentile(
                    [r["latency_ms"] or 0 for r in traces_rows if r["ok"]], 50
                ),
                "latency_p95": metrics.percentile(
                    [r["latency_ms"] or 0 for r in traces_rows if r["ok"]], 95
                ),
                "artifacts": {"topic_maps": len(maps), "routes": len(plans)},
                "first_turn_artifacts": first_artifacts,
                "request_asked_artifact": request_asks_artifact,
                "unrequested_artifacts": bool((not request_asks_artifact) and first_artifacts >= 1),
                "asked": _looks_like_asking(first_text),
                "addressed_on_demand": addressed,
                "modes": modes,
                "degrade_reasons": reasons,
                "coverage": metrics.float_rate(grounded, total),
                "ungrounded_ratio": metrics.float_rate(ungrounded, total),
                "exercise_items": exercises,
                "distinct_sources": len(sources_seen),
                "source_years": metrics.year_mix(years, now_year=now_year),
                "source_fresh_share": metrics.fresh_share(years, now_year=now_year),
                "source_kinds": metrics.kind_mix(
                    [str(item.get("source_type") or "?") for item in meta]
                ),
                "distinct_domains": len(
                    {str(item["domain"]) for item in meta if item.get("domain")}
                ),
                "counts": {"grounded": grounded, "total": total, "ungrounded": ungrounded},
                "tokens": {"prompt": int(usage["pin"]), "completion": int(usage["pout"])},
                "refs": result.get("refs", {}),
            }
        )

    all_traces: list[dict[str, Any]] = []
    for row in rows:
        session_id = ctx.db.query_one(
            "SELECT id FROM sessions WHERE title = ? ORDER BY created_at DESC LIMIT 1",
            (f"E3 {row['id']}",),
        )
        if session_id is not None:
            all_traces.extend(
                dict(item)
                for item in traces.list_traces(ctx.db, session_id=str(session_id["id"]), limit=200)
            )

    entries = [task["traces"] for task in tasks]
    return {
        "n": len(tasks),
        "model": ctx.config.chat.model,
        "tool_success_rate": metrics.success_rate(all_traces),
        "latency_p50": metrics.percentile(
            [r["latency_ms"] or 0 for r in all_traces if r["ok"]], 50
        ),
        "latency_p95": metrics.percentile(
            [r["latency_ms"] or 0 for r in all_traces if r["ok"]], 95
        ),
        "tool_calls_per_task": metrics.mean(entries),
        "coverage": metrics.mean([t["coverage"] for t in tasks if t["coverage"] is not None]),
        "ungrounded_ratio": metrics.mean(
            [t["ungrounded_ratio"] for t in tasks if t["ungrounded_ratio"] is not None]
        ),
        "degraded_rate": metrics.float_rate(
            sum(1 for task in tasks if any(mode in {"flat", "skeleton"} for mode in task["modes"])),
            len(tasks),
        ),
        "exercise_items": sum(t["exercise_items"] for t in tasks),
        "distinct_sources": metrics.mean([t["distinct_sources"] for t in tasks]),
        # 来源时效性与多样性（口径：未知年份单列、不进"近两年占比"的分母）
        "source_years": metrics.sum_mixes([t["source_years"] for t in tasks]),
        "source_fresh_share": metrics.mean([t["source_fresh_share"] for t in tasks]),
        "source_kinds": metrics.sum_mixes([t["source_kinds"] for t in tasks]),
        "distinct_domains": metrics.mean([t["distinct_domains"] for t in tasks]),
        # 产物完整率：同时出主题图与路线的任务占比。
        # 真机第三轮有两道题只出路线没出地图 —— 光看"任务完成 6/6"会完全漏掉这件事。
        "artifact_completeness": metrics.float_rate(
            sum(
                1
                for task in tasks
                if task["artifacts"]["topic_maps"] >= 1 and task["artifacts"]["routes"] >= 1
            ),
            len(tasks),
        ),
        # 「首轮就出产物」比例：**含题面明确要求的情形**，所以它只说明"工作流跑通了"，
        # 不能拿它证明"模型擅自生成"。擅自生成看下面那条。
        "auto_artifact_rate": metrics.float_rate(
            sum(1 for task in tasks if task["first_turn_artifacts"] >= 1), len(tasks)
        ),
        # **「题面没要、模型却出了产物」的比例** —— 这才是"擅自生成"。
        # 分母只算"题面没点名要产物"的题：拿要了产物的题当分母会把正确行为算成违规（踩过）。
        "unrequested_artifact_rate": metrics.float_rate(
            sum(1 for task in tasks if task["unrequested_artifacts"]),
            sum(1 for task in tasks if not task["request_asked_artifact"]),
        ),
        "unrequested_denominator": sum(1 for task in tasks if not task["request_asked_artifact"]),
        # 失败明细：哪个工具、几次、一次错误样本（报告要能"指出是哪一次"，不然 88.9% 这种数字没法复算）
        "failures": [
            {"task": task["id"], **item} for task in tasks for item in task.get("failures", [])
        ],
        "failed_tool_calls": sum(
            int(item["n"]) for task in tasks for item in task.get("failures", [])
        ),
        # 「模型有没有把选择权交出去」：启发式（措辞匹配），报告里要标注不精确。
        "asked_rate": metrics.float_rate(sum(1 for task in tasks if task["asked"]), len(tasks)),
        # 按需生成成功率：用户点单之后真出产物的比例（确定性口径，没跑 ask 就是 None）。
        "on_demand_success_rate": (
            metrics.float_rate(sum(1 for task in tasks if task["addressed_on_demand"]), len(tasks))
            if ask_artifacts
            else None
        ),
        "arm": {
            "auto_artifacts": auto_artifacts,
            "asked_followup": ask_artifacts,
            "thinking": bool(getattr(getattr(ctx.config, "chat", None), "thinking", True)),
            "system_prompt_hash": system_prompt_hash(
                build_system_prompt(auto_artifacts=auto_artifacts)
            ),
        },
        "counts": {
            "grounded": sum(t["counts"]["grounded"] for t in tasks),
            "total": sum(t["counts"]["total"] for t in tasks),
            "ungrounded": sum(t["counts"]["ungrounded"] for t in tasks),
        },
        "tokens": {
            "prompt": sum(task["tokens"]["prompt"] for task in tasks),
            "completion": sum(task["tokens"]["completion"] for task in tasks),
        },
        "tasks": tasks,
    }


# ------------------------------------------------------------------------ 入口


def filter_rows(
    rows: list[dict[str, Any]], *, only: str | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    """按题号/数量裁剪任务集（调提示词时只想跑一题，不必等全套 15 分钟）。

    `only` 支持逗号分隔；匹配不到任何题目时返回空列表 —— 调用方要如实报"没匹配到"，
    不能悄悄跑全套（那会让人以为改的是那一题）。
    """
    picked = rows
    if only:
        wanted = {item.strip() for item in only.split(",") if item.strip()}
        picked = [row for row in picked if str(row.get("id")) in wanted]
    if limit is not None and limit > 0:
        picked = picked[:limit]
    return picked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_eval", description="ScholarScout 评测运行器")
    parser.add_argument("--set", default="all", choices=["e1", "e2", "e3", "e3probe", "all"])
    parser.add_argument("--live", action="store_true", help="跑 E3（需要模型密钥与网络）")
    parser.add_argument("--ablation", action="store_true", help="跑对照臂")
    parser.add_argument(
        "--auto-artifacts",
        action="store_true",
        help="E3 对照臂：提示词要求每轮自动出产物（旧行为）；默认是按需档",
    )
    parser.add_argument(
        "--ask-artifacts",
        action="store_true",
        help="E3：首轮之后再发一条「请生成」，量按需生成成功率",
    )
    parser.add_argument(
        "--no-thinking",
        action="store_true",
        help="E3：关掉推理模型的深度思考（请求体带 thinking=disabled）—— 省钱杠杆的 A/B",
    )
    parser.add_argument("--out", default=str(ROOT / "eval" / "reports"))
    parser.add_argument(
        "--only",
        default=None,
        help="只跑这些题号（逗号分隔，例如 e3-01,e3-04）—— 调提示词时省时间",
    )
    parser.add_argument("--limit", type=int, default=None, help="最多跑前 N 题")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")

    results: dict[str, Any] = {"generated_at": timestamp, "sets": {}}
    if args.only or args.limit:
        # 部分跑必须留痕：报告里要能看出"这不是全套"，否则会被当成完整结论引用
        results["selection"] = {"only": args.only, "limit": args.limit}

    def pick(name: str) -> list[dict[str, Any]]:
        rows = filter_rows(load_set(name), only=args.only, limit=args.limit)
        if not rows:
            print(f"[!] {name} 里没有匹配「{args.only}」的题号 —— 这一组跳过")
        return rows

    with __import__("tempfile").TemporaryDirectory(prefix="scout-eval-") as tmp:
        tmp_path = Path(tmp)
        cfg = replace(load_config(), data_dir=tmp_path / "data", db_path=tmp_path / "state.db")
        ctx = build_context(config=cfg, db_path=cfg.db_path)
        try:
            if args.set in {"e1", "all"}:
                results["sets"]["e1"] = run_e1(ctx, pick("e1_grade"), ablation=args.ablation)
                print(f"E1 完成：{results['sets']['e1']['n']} 条")
            if args.set in {"e2", "all"}:
                results["sets"]["e2"] = run_e2(pick("e2_guard"))
                print(f"E2 完成：{results['sets']['e2']['n']} 条")
            if args.set in {"e3", "e3probe"} or (args.set == "all" and args.live):
                if ctx.llm is None:
                    results["sets"]["e3"] = {"skipped": "未配置模型（.env 里的 LLM_API_KEY）"}
                    print("E3 跳过：未配置模型")
                else:
                    # 省钱 A/B：只在需要时关思考，且必须**重建客户端**（配置改了不重建＝没生效）
                    if args.no_thinking:
                        ctx.config = replace(
                            ctx.config, chat=replace(ctx.config.chat, thinking=False)
                        )
                        ctx.llm = create_client(ctx.config.chat)
                    # e3probe = 题面**不点名要产物**的探针题：量"模型会不会擅自生成"
                    rows = pick("e3_probe_tasks" if args.set == "e3probe" else "e3_tasks")
                    results["sets"]["e3"] = run_e3(
                        ctx,
                        rows,
                        auto_artifacts=args.auto_artifacts,
                        ask_artifacts=args.ask_artifacts,
                    )
                    print(f"E3 完成：{results['sets']['e3']['n']} 条")
        finally:
            # 必须显式关库：Windows 上 WAL 句柄没释放会让临时目录删不掉（踩过）
            ctx.db.close()

    (out_dir / f"{timestamp}.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown = report.render(results)
    (out_dir / f"{timestamp}.md").write_text(markdown, encoding="utf-8")
    print(f"报告已写入 {out_dir / (timestamp + '.md')}")
    print()
    print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
