"""把评测结果渲染成能直接贴进论文的 Markdown 报告。

规则：
- **空值显示 "—"**，不显示 0（"没有样本"与"全错"必须在纸面上分得开）
- 每个比率都跟分子/分母，便于复算
- 分歧与失败样本一律列出，不许只报好看的数字
"""

from __future__ import annotations

from typing import Any


def _pct(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def _num(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _ratio(hit: int | None, total: int | None) -> str:
    if hit is None or total is None:
        return "—"
    return f"{hit}/{total}"


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _confusion_table(matrix: dict[str, dict[str, int]]) -> str:
    labels = list(matrix)
    rows = []
    for want in labels:
        rows.append([want] + [str(matrix[want].get(got, 0)) for got in labels])
    return _table(["期望 \\ 实际", *labels], rows)


def render(results: dict[str, Any]) -> str:
    parts: list[str] = [
        "# ScholarScout 评测报告",
        "",
        f"- 生成时间：`{results.get('generated_at', '-')}`",
        "- 口径：`docs/05-评测方案.md`（改口径须同步改该文档并重跑）",
        "",
    ]

    e1 = results.get("sets", {}).get("e1")
    if e1:
        parts += ["## E1 判权一致性（离线，规则表 + 本地确定性打分）", ""]
        parts.append(
            _table(
                ["指标", "值", "分子/分母"],
                [
                    ["样本数", str(e1["n"]), "—"],
                    ["tier 一致率", _pct(e1["tier_agreement"]), "—"],
                    ["verdict 一致率", _pct(e1["verdict_agreement"]), "—"],
                    ["tier κ", _num(e1["tier_kappa"], 3), "—"],
                    ["verdict κ", _num(e1["verdict_kappa"], 3), "—"],
                    [
                        "兜底命中率（无规则命中 → tier 4）",
                        _pct(e1["fallback_rate"]),
                        _ratio(e1["fallback_counts"]["hit"], e1["fallback_counts"]["total"]),
                    ],
                    [
                        "误升率（低质来源被判 tier ≤ 2）",
                        _pct(e1["lift_rate"]),
                        _ratio(e1["lift_counts"]["lifted"], e1["lift_counts"]["total"]),
                    ],
                    ["已加载规则数", str(e1["rules_loaded"]), "—"],
                ],
            )
        )
        parts += [
            "",
            "tier 混淆矩阵（行=期望，列=实际）：",
            "",
            _confusion_table(e1["tier_confusion"]),
        ]
        parts += [
            "",
            "verdict 混淆矩阵（行=期望，列=实际）：",
            "",
            _confusion_table(e1["verdict_confusion"]),
            "",
        ]
        if e1.get("ablation_no_rules"):
            parts += [
                "**对照臂 A_no_rules**（判权退化为全 tier 4）："
                f"tier 一致率 {_pct(e1['ablation_no_rules']['tier_agreement'])}"
                f"（完整规则表为 {_pct(e1['tier_agreement'])}）",
                "",
            ]
        if e1["disagreements"]:
            parts += [f"### 分歧样本（{len(e1['disagreements'])} 条，保留不改）", ""]
            rows = [
                [
                    item["id"],
                    item["url"].replace("|", "/"),
                    f"{item['tier']['got']} / {item['tier']['want']}",
                    f"{item['verdict']['got']} / {item['verdict']['want']}",
                    item["label_note"] or "—",
                ]
                for item in e1["disagreements"][:12]
            ]
            parts += [
                _table(["题号", "URL", "tier 实际/期望", "verdict 实际/期望", "标注说明"], rows),
                "",
            ]
        else:
            parts += ["分歧样本：无。", ""]

    e2 = results.get("sets", {}).get("e2")
    if e2:
        parts += ["## E2 反幻觉守卫（离线，同一份模型输出开/关守卫对比）", ""]
        parts.append(
            _table(
                ["指标", "值", "分子/分母"],
                [
                    ["样本数", str(e2["n"]), "—"],
                    ["守卫行为符合预期", _pct(e2["pass_rate"]), _ratio(e2["passed"], e2["n"])],
                    ["不变量违规（越权来源逃过守卫）", str(e2["invariant_violations"]), "—"],
                    ["出处覆盖率（关闭守卫）", _pct(e2["coverage_raw"]), _ratio(None, None)],
                    ["出处覆盖率（开启守卫）", _pct(e2["coverage_guarded"]), _ratio(None, None)],
                    [
                        "条目数 原始 → 守卫后",
                        f"{e2['counts']['raw_entries']} → {e2['counts']['guarded_entries']}",
                        "—",
                    ],
                ],
            )
        )
        parts += [
            "",
            "> 关闭守卫时出处覆盖率**虚高**：伪造 ID 也算「有出处」。两个数的差就是守卫的价值。",
            "",
        ]
        failed = [case for case in e2["cases"] if not case["passed"]]
        if failed:
            parts += [f"### 未通过的守卫用例（{len(failed)} 条）", ""]
            rows = [
                [
                    case["id"],
                    case["kind"],
                    str(case["expected"]),
                    str(case["got"]),
                    case["label_note"],
                ]
                for case in failed
            ]
            parts += [_table(["题号", "类型", "期望", "实际", "说明"], rows), ""]

    e3 = results.get("sets", {}).get("e3")
    if e3:
        parts += ["## E3 端到端（真模型 + 真出网）", ""]
        if e3.get("skipped"):
            parts += [f"本次未运行：{e3['skipped']}", ""]
        else:
            parts.append(
                _table(
                    ["指标", "值", "分子/分母"],
                    [
                        ["任务数", str(e3["n"]), "—"],
                        ["模型", str(e3["model"]), "—"],
                        ["工具成功率", _pct(e3["tool_success_rate"]), "—"],
                        [
                            "工具时延 P50 / P95 (ms)",
                            f"{_num(e3['latency_p50'], 0)} / {_num(e3['latency_p95'], 0)}",
                            "—",
                        ],
                        ["单任务工具调用次数", _num(e3["tool_calls_per_task"], 2), "—"],
                        [
                            "出处覆盖率（资料类，只算 paper/doc/video/code）",
                            _pct(e3["coverage"]),
                            _ratio(
                                e3.get("counts", {}).get("grounded"),
                                e3.get("counts", {}).get("total"),
                            ),
                        ],
                        [
                            "ungrounded 比例（同分母）",
                            _pct(e3["ungrounded_ratio"]),
                            _ratio(
                                e3.get("counts", {}).get("ungrounded"),
                                e3.get("counts", {}).get("total"),
                            ),
                        ],
                        [
                            "行动类条目（exercise，不声称出处，不计入上两行）",
                            str(e3.get("exercise_items", "—")),
                            "—",
                        ],
                        ["降级率（flat/skeleton）", _pct(e3["degraded_rate"]), "—"],
                        [
                            "token（prompt / completion）",
                            f"{e3['tokens']['prompt']} / {e3['tokens']['completion']}",
                            "—",
                        ],
                    ],
                )
            )
            parts += ["", "逐任务明细：", ""]
            rows = [
                [
                    task["id"],
                    task["topic"],
                    task["stop_reason"],
                    str(task["traces"]),
                    _pct(task["coverage"]),
                    str(task["artifacts"]["topic_maps"]) + "/" + str(task["artifacts"]["routes"]),
                    f"{task['elapsed_ms'] / 1000:.1f}s",
                    (task.get("error") or "—")[:60],
                ]
                for task in e3["tasks"]
            ]
            parts += [
                _table(
                    [
                        "题号",
                        "主题",
                        "stop_reason",
                        "工具调用",
                        "出处覆盖率",
                        "主题图/路线",
                        "端到端耗时",
                        "错误",
                    ],
                    rows,
                ),
                "",
            ]

            degraded = [task for task in e3["tasks"] if task.get("degrade_reasons")]
            if degraded:
                reasons_count: dict[str, int] = {}
                for task in degraded:
                    for reason in task["degrade_reasons"]:
                        reasons_count[reason] = reasons_count.get(reason, 0) + 1
                parts += [
                    f"### ⚠️ 有 {len(degraded)}/{e3['n']} 个任务的产物走了兜底模式（原因必须列出）",
                    "",
                ]
                for reason, count in sorted(reasons_count.items(), key=lambda item: -item[1]):
                    parts += [f"- `{count}` 次：{reason}"]
                parts += [""]

            failed = [task for task in e3["tasks"] if task["stop_reason"] != "completed"]
            if failed:
                reasons: dict[str, int] = {}
                for task in failed:
                    key = (task.get("error") or "未记录原因").split("：")[0]
                    reasons[key] = reasons.get(key, 0) + 1
                parts += [
                    f"### ⚠️ 有 {len(failed)}/{e3['n']} 个任务没有正常完成（原因必须显式列出）",
                    "",
                ]
                for reason, count in sorted(reasons.items(), key=lambda item: -item[1]):
                    parts += [f"- `{count}` 次：{reason}"]
                parts += [""]

    parts += [
        "## 结论口径提醒",
        "",
        f"- 样本量：E1 {_num(float(e1['n']) if e1 else None, 0)} 条、"
        f"E2 {_num(float(e2['n']) if e2 else None, 0)} 条"
        "—— 结论是方向性的，不做统计显著性主张（docs/05 §7）。",
        "- 本地确定性打分不等于语义相关；语义相关性以 E3 的模型路径为准。",
        "- 指标均为「可复算」：报告里每个比率都能从 `eval/reports/<同一时间戳>.json` 重新算出。",
        "",
    ]
    return "\n".join(parts)
