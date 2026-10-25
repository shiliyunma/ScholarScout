"""提示词两档：默认"按需生成产物"，对照臂"自动生成"（论文的一组对照）。

为什么把哈希也钉住：报告里的数字必须能追溯到"当时用的是哪版提示词"。
哈希一变，历史回答就不再可比（`system_prompt_hash` 会写进会话）。所以这里用**黄金哈希**锁住
2026-10-05 E3 第四轮用的那版 —— 改动这段文字会让测试红，提醒你这是在换实验条件。
"""

from __future__ import annotations

from pathlib import Path

from scholarscout.runtime import registry
from scholarscout.runtime.prompts import (
    BASE_PROMPT,
    ON_DEMAND_PIPELINE_PROMPT,
    PIPELINE_PROMPT,
    build_system_prompt,
    system_prompt_hash,
)

#: 仓库根（本文件在 ``tests/runtime/`` 下，往上两级）—— 示例配置里的默认值也是契约的一部分
CONFIG_EXAMPLE = Path(__file__).resolve().parents[2] / "config.example.yaml"

#: **历史值**（2026-10-05 E3 第四轮用的提示词，当时的对照臂）：`af6f1b33e12c`
#:   —— 那时系统提示词里还没有 `RESEARCH_STATE_RULE`（A1/A2/A7 的多轮规矩）
GOLDEN_AUTO_HASH_V1 = "af6f1b33e12c"
#: **历史值**：默认档（按需）当时是 `f68b1b7abb03`
GOLDEN_ON_DEMAND_HASH_V1 = "f68b1b7abb03"
#: **历史值**（2026-10-07 加入 A1/A2/A7 多轮规矩之后那版）：`58c966149165` / `fa8522427ff6`
#:   —— 是 2026-10-08 引入 `research.delegate`（docs/01 §5.18）之前的现值。
GOLDEN_AUTO_HASH_V2 = "fa8522427ff6"
GOLDEN_ON_DEMAND_HASH_V2 = "58c966149165"
#: **历史值**（2026-10-10 加 `DELEGATE_RULE` 之后、2026-10-22 之前）：委派那一档的哈希。
GOLDEN_AUTO_HASH_V3 = "1ab0c25756dc"
GOLDEN_ON_DEMAND_HASH_V3 = "beaa67c49be0"
#: **历史值**（2026-10-22 加入 `py.exec` 的「沙箱规矩」`PY_EXEC_RULE` 之后那一版）：
#: 工具数 8 → 9 时换的哈希。现在保留成 `_V4` 是因为 **工具数又变了（9 → 11）**，见下面的现值说明。
GOLDEN_AUTO_HASH_V4 = "bd2b946b4e5d"
GOLDEN_ON_DEMAND_HASH_V4 = "b9ac0c043bf2"
#: **历史值**（2026-10-22 加入 `SKILL_RULE` 之后、2026-10-25 之前那一版）：
#: `c21d781d15d5` / `20b7b97e452e`。那是「11 个工具、四段规矩、无检索节制」的最后一版。
#: 2026-10-25 之后它**再也复现不出来** —— 检索节制规则 `SEARCH_PACE_RULE` 加在两档共用的位置上，
#: 连「开关全关」的兜底臂也带着它（旧实验数字因此只能读旧报告，不能在新代码上重跑比对）。
GOLDEN_AUTO_HASH_V8 = "c21d781d15d5"
GOLDEN_ON_DEMAND_HASH_V8 = "20b7b97e452e"
#: **历史值**（2026-10-25 加 `USER_FACING_RULE`（回答里不许出现内部工具名 / 参数名 / 错误码 /
#: 异常类名）**之前**那一版）：兜底臂 `14ea4d9dd806` / `07325099d9ac`，
#: 产品默认臂 `7319b19e98e3` / `65172e7e589b`。
#: 换哈希的唯一理由：新增了那四行表达纪律 —— 真机缺陷"模型在回答里报内部工具名"（缺陷 4），
#: 属于**实验条件**（提示词内容）变了，不是格式化。
GOLDEN_AUTO_HASH_V9 = "14ea4d9dd806"
GOLDEN_ON_DEMAND_HASH_V9 = "07325099d9ac"
GOLDEN_PRODUCT_AUTO_HASH_V9 = "7319b19e98e3"
GOLDEN_PRODUCT_ON_DEMAND_HASH_V9 = "65172e7e589b"

#: 现值（2026-10-25 加 `USER_FACING_RULE` 之后）：**兜底臂**（`build_system_prompt()`，
#: 即 `files` 与披露都关掉、只有 `research` 工具集）。
GOLDEN_AUTO_HASH = "1326bbee1e3c"
GOLDEN_ON_DEMAND_HASH = "43e654887606"

#: **现值 —— 产品默认那一臂**（2026-10-25 起 `features.enable_files` 默认开、
#: `features.enable_tool_disclosure` 默认**关**）。上面两枚 `GOLDEN_*` 代表
#: **「files 也关掉」的兜底臂**（`build_system_prompt()` 不传 toolsets、不传 disclosure），
#: 所以它们仍是「关掉之后能不能回到无条件段那版」的靶子 —— 见
#: `test_switching_files_off_restores_the_no_files_arm`。
#: 默认路径与兜底臂的差别**只有一样**：`files` 工具集 → 多一段 `FILES_RULE`
#: （「读文件用 file.* 不用 py.exec / 引用带行号 / 越界就停」）+ 工具集那行多一个 `files`。
#: 披露默认关所以默认档用逐工具点名的那一版（`DELEGATE_RULE` / `PY_EXEC_RULE` / `SKILL_RULE` 都在）。
GOLDEN_PRODUCT_AUTO_HASH = "246e7c92a6fe"
GOLDEN_PRODUCT_ON_DEMAND_HASH = "bd131f552e6c"


def _product_prompt(*, auto_artifacts: bool = False) -> str:
    """产品默认那一臂的提示词：`research, files` 两个工具集、**不开**披露（与 `loop.build_messages` 同参）。"""
    return build_system_prompt(
        toolsets=["research", "files"], auto_artifacts=auto_artifacts, disclosure=False
    )


def test_product_default_arm_hash_is_pinned() -> None:
    """默认路径（files 开、披露关）的哈希：它才是用户现在真正吃到的那份提示词。"""
    assert system_prompt_hash(_product_prompt()) == GOLDEN_PRODUCT_ON_DEMAND_HASH
    assert system_prompt_hash(_product_prompt(auto_artifacts=True)) == GOLDEN_PRODUCT_AUTO_HASH
    # 与兜底臂必须不同 —— 相同就说明 `files` 开关没接线（默认值改了、行为没改）
    assert GOLDEN_PRODUCT_ON_DEMAND_HASH != GOLDEN_ON_DEMAND_HASH, (
        "默认开 files 后提示词必然多一段 FILES_RULE；若相同说明开关没接线"
    )
    assert GOLDEN_PRODUCT_AUTO_HASH != GOLDEN_AUTO_HASH
    # 差别只在 FILES_RULE（与工具集那行），不该顺带把披露档的文本带进来
    product = _product_prompt()
    assert "什么时候用只读文件工具" in product
    assert "什么时候用只读文件工具" not in build_system_prompt()
    assert "工具是先查后用的" not in product, "披露默认关，默认档不该出现披露档文本"


def test_switching_files_off_restores_the_no_files_arm() -> None:
    """**关掉 `enable_files` 必须逐字回到无条件段那一版** —— 这是「默认值只有一个可变量」的依据。"""
    off = build_system_prompt(
        toolsets=list(registry.enabled_toolsets([], enable_kb=False, enable_files=False)),
        disclosure=False,
    )
    assert off == build_system_prompt(), "关掉 files 后就不该再有条件段"
    assert system_prompt_hash(off) == GOLDEN_ON_DEMAND_HASH
    # 旧版（加检索节制之前）的新旧关系必须留痕：现值不可能等于 _V8
    assert GOLDEN_ON_DEMAND_HASH != GOLDEN_ON_DEMAND_HASH_V8
    assert GOLDEN_AUTO_HASH != GOLDEN_AUTO_HASH_V8


def test_the_shipped_defaults_are_files_on_and_disclosure_off() -> None:
    """默认值只有一处可改（`Features`），示例配置必须一致 —— 否则用户照抄就跑到别的臂上。"""
    from scholarscout.config import Features

    assert Features().enable_files is True
    assert Features().enable_tool_disclosure is False, (
        "披露默认必须关：A/B 实测开着把调用数 19 → 25、冗余 7 → 15 且包含率无收益"
    )
    example = CONFIG_EXAMPLE.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "\n  enable_files: true\n" in example
    assert "\n  enable_tool_disclosure: false\n" in example


def test_search_pace_rule_is_in_both_arms() -> None:
    """检索节制规则两档都要有：差别只该在「工具怎么找」，不该在「检索怎么走」。"""
    from scholarscout.runtime.prompts import SEARCH_PACE_RULE

    for prompt in (
        build_system_prompt(),
        build_system_prompt(auto_artifacts=True),
        build_system_prompt(toolsets=["research", "files"], disclosure=True),
    ):
        assert SEARCH_PACE_RULE in prompt
        assert "先做一次检索，看结果再决定要不要抓正文" in prompt


def test_user_facing_rule_is_in_every_arm() -> None:
    """缺陷 4 的硬约束（回答里不许出现内部工具名 / 错误码 / 异常类名）：**每档都得有**。

    只写进某一档 = 换个配置就复发，而这类文本又最难在真机评测里被发现（模型很会"礼貌地"照抄）。
    这里连同那句"用户关心的是什么"一起钉住：只钉"不许"会让模型不知道**该说什么**。
    """
    from scholarscout.runtime.prompts import USER_FACING_RULE

    arms = {
        "兜底-按需": build_system_prompt(),
        "兜底-自动": build_system_prompt(auto_artifacts=True),
        "产品默认-按需": _product_prompt(),
        "产品默认-自动": _product_prompt(auto_artifacts=True),
        "披露-按需": build_system_prompt(toolsets=["research", "files"], disclosure=True),
        "披露-自动": build_system_prompt(
            toolsets=["research", "files"], auto_artifacts=True, disclosure=True
        ),
    }
    for name, prompt in arms.items():
        assert USER_FACING_RULE in prompt, f"{name} 缺了表达纪律这一段"
        assert "回答用户时不要出现内部标识" in prompt, name
        assert "不要出现内部工具名、参数名、错误码或异常类名" in prompt, name
        assert "我检索了哪些来源、查到了什么" in prompt, name
        assert "先检索了一遍 arXiv 和 OpenAlex，再从中筛出 3 篇精读" in prompt, name


def test_hashes_were_re_released_for_the_user_facing_rule() -> None:
    """换哈希必须留痕：现值 == 新钉的；且现值 ≠ `_V9`（加规则前），`_V9` ≠ `_V8`（链不断）。

    换的理由只有一个：提示词里多了一整段 `USER_FACING_RULE`。它不是文风微调，是缺陷 4 的硬约束 ——
    但**内容变了哈希就必须变**，否则报告里分不清哪批回答出自"会说人话"的那一版。
    """
    assert system_prompt_hash(build_system_prompt()) == GOLDEN_ON_DEMAND_HASH
    assert system_prompt_hash(build_system_prompt(auto_artifacts=True)) == GOLDEN_AUTO_HASH
    assert system_prompt_hash(_product_prompt()) == GOLDEN_PRODUCT_ON_DEMAND_HASH
    assert system_prompt_hash(_product_prompt(auto_artifacts=True)) == GOLDEN_PRODUCT_AUTO_HASH

    assert GOLDEN_ON_DEMAND_HASH != GOLDEN_ON_DEMAND_HASH_V9, (
        "多了一段 USER_FACING_RULE 却还是同一个哈希 → 那段没进提示词"
    )
    assert GOLDEN_AUTO_HASH != GOLDEN_AUTO_HASH_V9
    assert GOLDEN_PRODUCT_ON_DEMAND_HASH != GOLDEN_PRODUCT_ON_DEMAND_HASH_V9
    assert GOLDEN_PRODUCT_AUTO_HASH != GOLDEN_PRODUCT_AUTO_HASH_V9
    # 留痕链本身也要成立：V9（加规则前）≠ V8（加检索节制前）—— 每次换哈希都是独立的一次实验条件变更
    assert GOLDEN_ON_DEMAND_HASH_V9 != GOLDEN_ON_DEMAND_HASH_V8
    assert GOLDEN_AUTO_HASH_V9 != GOLDEN_AUTO_HASH_V8
    assert GOLDEN_PRODUCT_ON_DEMAND_HASH_V9 != GOLDEN_PRODUCT_ON_DEMAND_HASH


def test_auto_arm_is_byte_identical_to_the_old_experiment() -> None:
    """对照臂必须逐字钉死，否则"自动 vs 按需"就不是单变量对照。"""
    assert GOLDEN_AUTO_HASH != GOLDEN_AUTO_HASH_V1, "提示词变过就得留痕（旧值仍在上面的常量里）"
    assert GOLDEN_AUTO_HASH != GOLDEN_AUTO_HASH_V2, "加入委派规矩又换了一次哈希，旧值要留痕"
    assert GOLDEN_AUTO_HASH != GOLDEN_AUTO_HASH_V3, (
        "加入 py.exec 沙箱规矩又换了一次哈希，旧值要留痕"
    )
    assert GOLDEN_AUTO_HASH != GOLDEN_AUTO_HASH_V4, (
        "加入技能库规矩（skill.list / skill.load，工具数 9 → 11）又换了一次哈希，旧值要留痕"
    )
    assert GOLDEN_AUTO_HASH != GOLDEN_AUTO_HASH_V9, (
        "加表达纪律（不许报内部标识）又换了一次，旧值要留痕"
    )
    assert system_prompt_hash(build_system_prompt(auto_artifacts=True)) == GOLDEN_AUTO_HASH


def test_on_demand_arm_hash_is_pinned() -> None:
    assert GOLDEN_ON_DEMAND_HASH != GOLDEN_ON_DEMAND_HASH_V4, "默认档也加了 SKILL_RULE，旧值要留痕"
    assert GOLDEN_ON_DEMAND_HASH != GOLDEN_ON_DEMAND_HASH_V9, (
        "默认档也加了 USER_FACING_RULE（不许报内部标识），旧值要留痕"
    )
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
