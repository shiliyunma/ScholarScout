"""评测集契约：E2 守卫集必须全过；E1 判权集必须与规则表完全一致（回归护栏）。

E1 的两条在 2026-10-05 跑出的真问题（域名不认子域、缺 .gov.cn）已修，
这里把它们固化：**改规则表或改匹配逻辑导致 tier 判错，测试立刻红**。
"""

from __future__ import annotations

import pytest

from eval import run_eval
from scholarscout.rpc.methods import AppContext


def test_guard_set_passes_completely() -> None:
    result = run_eval.run_e2(run_eval.load_set("e2_guard"))
    failed = [case for case in result["cases"] if not case["passed"]]
    assert failed == [], f"守卫用例未通过：{failed}"
    assert result["invariant_violations"] == 0, "有越权来源逃过了守卫"
    assert 0 < result["coverage_guarded"] < 1.0, (
        "题面里本来就有无出处的条目，守卫后的覆盖率不该是 100%"
    )
    assert (
        result["coverage_raw"] is not None and result["coverage_raw"] > result["coverage_guarded"]
    ), "关掉守卫时出处覆盖率应当虚高（伪造 ID 也算有出处），否则这个对照臂没有信息量"


def test_guard_set_runs_without_duplicates_leaking() -> None:
    result = run_eval.run_e2(run_eval.load_set("e2_guard"))
    by_id = {case["id"]: case for case in result["cases"]}
    assert by_id["e2-10"]["got"]["dropped_duplicates"] == 1
    assert by_id["e2-12"]["got"]["minutes"] == [10, 45, 600]
    # e2-13：行动类条目（练习/产出）不计入 ungrounded —— 口径修正的锚点
    assert by_id["e2-13"]["got"]["ungrounded"] == 1
    assert by_id["e2-13"]["got"]["exercise_items"] == 2
    assert by_id["e2-13"]["passed"] is True
    assert by_id["e2-04"]["got"]["nodes"] == 0


def test_e1_tier_matches_rule_table_exactly(ctx: AppContext) -> None:
    result = run_eval.run_e1(ctx, run_eval.load_set("e1_grade"), ablation=True)

    bad_tier = [
        detail for detail in result["details"] if detail["tier"]["got"] != detail["tier"]["want"]
    ]
    assert bad_tier == [], f"判层级不一致：{bad_tier}"
    assert result["tier_agreement"] == 1.0
    assert result["fallback_counts"] == {"hit": 2, "total": 2}, "未命中规则的样本应全部落到 tier 4"
    assert result["lift_counts"]["lifted"] == 0, "低质来源不应被抬高到 tier ≤ 2"


def test_e1_rule_table_beats_no_rules(ctx: AppContext) -> None:
    """对照臂：把规则表撤掉，判权一致率必须显著下降（证明规则表在干活）。"""
    result = run_eval.run_e1(ctx, run_eval.load_set("e1_grade"), ablation=True)
    assert result["ablation_no_rules"]["tier_agreement"] < result["tier_agreement"]
    assert result["ablation_no_rules"]["tier_agreement"] < 0.5


def test_e1_verdict_disagreements_are_listed_not_hidden(ctx: AppContext) -> None:
    """verdict 分歧必须如实列出（本地打分只认字面重合，语义相邻题靠 E3 的模型路径）。"""
    result = run_eval.run_e1(ctx, run_eval.load_set("e1_grade"), ablation=False)
    listed = {detail["id"] for detail in result["disagreements"]}
    mismatched = {
        detail["id"]
        for detail in result["details"]
        if detail["verdict"]["got"] != detail["verdict"]["want"]
    }
    assert listed == mismatched
    assert len(listed) > 0, "本地打分的局限应当体现在报告里，而不是被抹平"


def test_report_renders_offline_sets(ctx: AppContext) -> None:
    results = {
        "generated_at": "test",
        "sets": {
            "e1": run_eval.run_e1(ctx, run_eval.load_set("e1_grade"), ablation=True),
            "e2": run_eval.run_e2(run_eval.load_set("e2_guard")),
        },
    }
    text = run_eval.report.render(results)
    assert "# ScholarScout 评测报告" in text
    assert "E1 判权一致性" in text
    assert "E2 反幻觉守卫" in text
    assert "分子/分母" in text
    assert "—" in text, "空值必须显示为 — 而不是 0"


def test_load_set_shape() -> None:
    for name, minimum in (("e1_grade", 20), ("e2_guard", 10), ("e3_tasks", 5)):
        rows = run_eval.load_set(name)
        assert len(rows) >= minimum
        assert all("id" in row for row in rows)
        assert len({row["id"] for row in rows}) == len(rows), "题号必须唯一"


def test_e3_rows_carry_runnable_request() -> None:
    for row in run_eval.load_set("e3_tasks"):
        assert row["request"].strip()
        assert row["topic"].strip()
        assert isinstance(row["weeks"], int) and row["weeks"] >= 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
