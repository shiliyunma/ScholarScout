"""「擅自生成产物」的口径必须与「题面要了就做」分开。

背景（2026-10-05 实测返工）：第一次跑按需档，指标报"83.3% 的任务首轮就出产物"，
看起来像模型不听"先问再生成"的指令。实际是**指标错了** —— E3 六道题的题面全都点名要了产物
（"再排一条 4 周学习路线""怎么安排四周"…），"用户要了就做"是正确行为，却被算成了违规。

所以这里把两件事钉开：题面是否点名要产物（启发式，词表在 `run_eval`），
以及"题面没要却仍出产物"的分母怎么取。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from run_eval import REQUEST_ARTIFACT_WORDS  # noqa: E402


def load_set(name: str) -> list[dict]:
    path = ROOT / "eval" / "sets" / f"{name}.jsonl"
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def asks_artifact(request: str) -> bool:
    return any(word in request for word in REQUEST_ARTIFACT_WORDS)


def test_e3_tasks_all_ask_for_artifacts() -> None:
    """E3 的题面都点名要产物 —— 所以拿这个集子算"擅自生成"是算不出来的（分母为 0）。"""
    rows = load_set("e3_tasks")
    assert rows, "题集不该为空"
    assert all(asks_artifact(row["request"]) for row in rows)


def test_probe_tasks_do_not_ask_for_artifacts() -> None:
    """探针题的题面只要回答、不要产物 —— 它们才有"擅自生成"的分母。"""
    rows = load_set("e3_probe_tasks")
    assert len(rows) >= 3
    for row in rows:
        assert not asks_artifact(row["request"]), row["id"]


def test_probe_tasks_are_well_formed() -> None:
    for row in load_set("e3_probe_tasks"):
        assert row["id"] and row["topic"] and row["request"]


def test_unrequested_metric_uses_the_right_denominator() -> None:
    """分母必须是"题面没要产物的题数"，不能拿全题数当分母。"""
    source = (ROOT / "eval" / "run_eval.py").read_text(encoding="utf-8")
    assert "unrequested_artifact_rate" in source
    assert "unrequested_denominator" in source
    assert "request_asked_artifact" in source
    block = source[source.index('"unrequested_artifact_rate"') :]
    assert 'if not task["request_asked_artifact"]' in block[:400]


def test_report_shows_the_denominator() -> None:
    """报告里这条要带分母，否则"0%"和"没有样本"分不出来。"""
    report = (ROOT / "eval" / "report.py").read_text(encoding="utf-8")
    assert "题面没要却仍出产物" in report
    assert "分母" in report
