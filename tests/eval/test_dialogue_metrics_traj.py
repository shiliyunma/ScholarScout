"""轨迹口径（第二把尺子）的契约。

为什么单独立文件：这条口径的意义全在"**规则预先定死、不逐题调**"。
所以测试钉的是**规则本身**（预算 1 search + 1 grade + ≤K fetch），不是某一批数字。
另有一条回归：旧口径的每个字段必须与改动前**逐字段相同**（拿今天那份归档报告当金标）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eval import dialogue_metrics as dm
from eval import run_dialogues

#: 归档金标（从今天真机那份报告裁剪，2026-10-24）—— ``eval/reports/`` 被 .gitignore 忽略，
#: 所以回归用的金标必须放在被跟踪的 fixtures 里，否则干净检出时这条回归等于没跑。
ARCHIVE = Path(__file__).resolve().parents[1] / "fixtures" / "e4_two_scales_archive.json"


def _call(name: str, **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args}


def _turns(n: int, name: str) -> list[dict[str, Any]]:
    return [_call(name, **{"url": f"https://arxiv.org/abs/2501.0000{i}"}) for i in range(1, n + 1)]


def test_traj_zero_when_minimal_trajectory_is_exactly_met() -> None:
    """1 search + 1 grade + 3 fetch（K=3）→ 轨迹口径冗余 0；原口径仍算 4（gold 只有 1 次 search）。"""
    actual = [
        _call("research.search", query="rag"),
        _call("source.grade"),
        *_turns(3, "source.fetch"),
    ]
    gold = [_call("research.search", query="rag")]
    assert dm.traj_turn_excess(actual, gold) == 0
    assert max(0, len(actual) - len(gold)) == 4, "原口径不受影响：仍是 实际 − gold"
    assert dm.tool_call_accuracy_traj(actual, gold) == 1.0


def test_traj_counts_only_fetch_beyond_k() -> None:
    """5 次 fetch → 轨迹口径冗余 2（只多算超出 K 的那 2 次，K 是常量 3）。"""
    actual = [
        _call("research.search", query="rag"),
        _call("source.grade"),
        *_turns(5, "source.fetch"),
    ]
    gold = [_call("research.search", query="rag")]
    assert dm.TRAJ_FETCH_K == 3, "K 必须写死为 3；改它等于换口径"
    assert dm.traj_turn_excess(actual, gold) == 2
    assert dm.traj_turn_excess(actual[:-1], gold) == 1
    assert dm.tool_call_accuracy_traj(actual, gold) == 0.0, "超预算就不是最小充分轨迹"


def test_traj_still_counts_extra_searches() -> None:
    """预算内的检索不算冗余 ≠ 检索可以随便多调：搜 4 次要算 3 次冗余。"""
    actual = [_call("research.search", query=str(i)) for i in range(4)]
    gold = [_call("research.search", query="rag")]
    assert dm.traj_turn_excess(actual, gold) == 3
    assert dm.tool_call_accuracy_traj(actual, gold) == 0.0


def test_non_retrieval_turn_identical_under_both_scales() -> None:
    """非检索轮（gold = note.save）两个口径必须一致 —— 轨迹口径不改写这类轮。"""
    actual = [_call("note.save", kind="note"), _call("note.save", kind="card")]
    gold = [_call("note.save", kind="note")]
    assert dm.is_retrieval_turn(actual) is False
    assert dm.traj_turn_excess(actual, gold) == max(0, len(actual) - len(gold)) == 1
    assert (
        dm.tool_call_accuracy_traj(actual, gold)
        == dm.tool_call_accuracy(actual, gold, match="name")
        == 0.0
    )


def test_traj_does_not_forgive_missing_gold_tool() -> None:
    """只搜了一下、该排的路线没排 → 轨迹口径也给 0（宽松只放宽"检索几步"，不放宽工具选错）。"""
    actual = [_call("research.search", query="rag")]
    gold = [_call("route.plan", topic="rag")]
    assert dm.traj_turn_excess(actual, gold) == 0
    assert dm.tool_call_accuracy_traj(actual, gold) == 0.0


def test_empty_turns_and_missing_gold_do_not_crash() -> None:
    """空轮 / 无 gold：不崩，且沿用"分母为 0 → None"的约定。"""
    assert dm.traj_turn_excess([], []) == 0
    assert dm.tool_call_accuracy_traj([], []) is None
    assert dm.tool_call_accuracy_traj([_call("research.search")], []) is None, (
        "无 gold → 无判定（不报 0）"
    )
    empty = {"mode": "live", "dialogues": []}
    summary = run_dialogues.summarize(empty)
    assert summary["redundant_calls_traj"] == 0
    assert summary["tool_call_accuracy_traj"] is None


def test_old_fields_bit_identical_on_archived_report() -> None:
    """回归：用**现在的**代码重算归档报告的旧字段，必须与报告里存的值逐个相等（不是近似）。"""
    src = json.loads(ARCHIVE.read_text(encoding="utf-8"))
    turns = [t for d in src["dialogues"] for t in d["per_turn"]]
    assert len(turns) == 15, "E4 = 6 对话 / 15 轮"
    for t in turns:
        assert max(0, len(t["actual"]) - len(t["gold"])) == t["redundant"]
        assert dm.tool_call_accuracy(t["actual"], t["gold"]) == t["tca_both"]
        assert dm.tool_call_accuracy(t["actual"], t["gold"], match="name") == t["tca_name"]
        assert dm.tool_call_f1(t["actual"], t["gold"]) == t["tc_f1_both"]
        assert dm.tool_call_f1(t["actual"], t["gold"], match="name") == t["tc_f1_name"]
        assert dm.tool_usage_rate(t["actual"]) == t["tool_usage"]
    s = src["summary"]
    assert sum(t["redundant"] for t in turns) == s["redundant_calls"]
    assert run_dialogues._mean([t["tca_both"] for t in turns]) == s["tool_call_accuracy_both"]
    assert run_dialogues._mean([t["tca_name"] for t in turns]) == s["tool_call_accuracy_name"]
    assert run_dialogues._mean([t["tc_f1_both"] for t in turns]) == s["tool_call_f1_both"]
    assert run_dialogues._mean([t["tc_f1_name"] for t in turns]) == s["tool_call_f1_name"]
    # 旧报告里不该出现新口径的字段（证明归档没被新代码改写）
    assert "redundant_calls_traj" not in s
    assert "tool_call_accuracy_traj" not in s


def test_rescore_matches_archive_and_reports_no_mismatch() -> None:
    """``--rescore`` 走的就是纯函数：零模型调用，且自己报告"旧字段不一致 0 处"。"""
    res = run_dialogues.rescore(ARCHIVE)
    assert res["old_mismatch"] == []
    assert res["summary"]["redundant_calls"] == 49, "原口径总数不许变"
    assert res["summary"]["redundant_calls_traj"] == 24
    assert abs(res["summary"]["tool_call_accuracy_name"] - 0.0667) < 1e-3
    assert abs(res["summary"]["tool_call_accuracy_traj"] - 0.2) < 1e-9
    assert sum(d["redundant_calls_traj"] for d in res["dialogues"]) == 24
