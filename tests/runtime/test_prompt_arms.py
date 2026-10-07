"""提示词两档：默认"按需生成产物"，对照臂"自动生成"（论文的一组对照）。

为什么把哈希也钉住：报告里的数字必须能追溯到"当时用的是哪版提示词"。
哈希一变，历史回答就不再可比（`system_prompt_hash` 会写进会话）。所以这里用**黄金哈希**锁住
2026-10-05 E3 第四轮用的那版 —— 改动这段文字会让测试红，提醒你这是在换实验条件。
"""

from __future__ import annotations

from scholarscout.runtime.prompts import (
    BASE_PROMPT,
    ON_DEMAND_PIPELINE_PROMPT,
    PIPELINE_PROMPT,
    build_system_prompt,
    system_prompt_hash,
)

#: **历史值**（2026-10-05 E3 第四轮用的提示词，当时的对照臂）：`af6f1b33e12c`
#:   —— 那时系统提示词里还没有 `RESEARCH_STATE_RULE`（A1/A2/A7 的多轮规矩）
GOLDEN_AUTO_HASH_V1 = "af6f1b33e12c"
#: **历史值**：默认档（按需）当时是 `f68b1b7abb03`
GOLDEN_ON_DEMAND_HASH_V1 = "f68b1b7abb03"
#: 现值（2026-10-07 加入 A1/A2/A7 的多轮规矩之后）。
#: **注意**：E3 那批数字（覆盖率 100%、ungrounded 0%）是用 V1 跑的；引用时必须写明，
#: 要拿新数字得按 docs/14 的口径重跑 E3。
GOLDEN_AUTO_HASH = "fa8522427ff6"
GOLDEN_ON_DEMAND_HASH = "58c966149165"


def test_auto_arm_is_byte_identical_to_the_old_experiment() -> None:
    """对照臂必须逐字钉死，否则"自动 vs 按需"就不是单变量对照。"""
    assert GOLDEN_AUTO_HASH != GOLDEN_AUTO_HASH_V1, "提示词变过就得留痕（旧值仍在上面的常量里）"
    assert system_prompt_hash(build_system_prompt(auto_artifacts=True)) == GOLDEN_AUTO_HASH


def test_on_demand_arm_hash_is_pinned() -> None:
    assert system_prompt_hash(build_system_prompt()) == GOLDEN_ON_DEMAND_HASH


def test_two_arms_differ_only_in_the_pipeline_section() -> None:
    """两档的差别**只**在流程那一段：换档不能顺手改别的东西。"""
    auto = build_system_prompt(auto_artifacts=True)
    on_demand = build_system_prompt()
    assert auto.replace(PIPELINE_PROMPT, "") == on_demand.replace(ON_DEMAND_PIPELINE_PROMPT, "")
    assert BASE_PROMPT in auto and BASE_PROMPT in on_demand


def test_on_demand_forbids_auto_artifacts() -> None:
    on_demand = build_system_prompt()
    assert "不要调 topic.build_map / route.plan / note.save" in on_demand
    assert "把产物的选择权交给用户" in on_demand
    assert "不许" in on_demand and "顺手" in on_demand


def test_on_demand_still_honours_explicit_requests() -> None:
    """用户明确要了就照做 —— 按需不是"一律不做"。"""
    on_demand = build_system_prompt()
    assert "明确要了" in on_demand
    assert "topic.build_map" in on_demand and "route.plan" in on_demand


def test_auto_arm_keeps_the_forced_pipeline() -> None:
    auto = build_system_prompt(auto_artifacts=True)
    assert "**必须做**" in auto
    assert "别只出一件就收工" in auto


def test_toolsets_and_clarify_are_still_wired() -> None:
    prompt = build_system_prompt(toolsets=["research", "note"], allow_clarify=False)
    assert "当前可用工具集：research, note" in prompt
    assert "澄清规则" not in prompt


def test_ask_wording_heuristic() -> None:
    """「模型有没有把选择权交出去」是启发式 —— 认得出来常见问法，也别把陈述句误判成问句。"""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval"))
    from run_eval import _looks_like_asking

    assert _looks_like_asking("要不要我把这个方向整理成知识地图和 4 周学习路线？")
    assert _looks_like_asking("需要我生成一份抽背卡吗？")
    assert not _looks_like_asking("下面是我整理的学习路线。")
    assert not _looks_like_asking("")
    assert not _looks_like_asking(None)  # type: ignore[arg-type]
