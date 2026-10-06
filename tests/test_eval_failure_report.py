"""评测报告必须能指出"是哪一次工具失败"。

昨天探针跑出 88.9% 的工具成功率，但报告只给一个数字 —— 事后想追是哪一次、哪个工具、
报什么错，**追不到**（评测跑在临时库上、跑完即删）。而 `tool_traces.error` 里本来就有。

所以这一组测试钉两件事：
1. 失败明细要按工具归并、带一次错误样本，并如实标记属于哪道题；
2. **报告渲染出来**要真的带上这一行（只改数据结构不渲染 = 白改）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from report import _failure_text, _task_failure_text, render  # noqa: E402
from run_eval import failure_summary  # noqa: E402


def trace(tool: str, ok: bool, error: str | None = None) -> dict:
    return {"tool_name": tool, "ok": 1 if ok else 0, "error": error}


def test_failure_summary_groups_by_tool() -> None:
    rows = [
        trace("research.search", True),
        trace("source.fetch", False, "Failed to fetch: 403"),
        trace("source.fetch", False, "timeout"),
        trace("research.search", False, "rate limited"),
    ]
    got = failure_summary(rows)
    assert [item["tool"] for item in got] == ["source.fetch", "research.search"]
    assert got[0]["n"] == 2
    assert got[0]["error"] == "Failed to fetch: 403", "只留第一处错误样本"
    assert got[1]["n"] == 1


def test_failure_summary_ignores_successes() -> None:
    assert failure_summary([trace("a", True), trace("b", True)]) == []


def test_failure_summary_truncates_long_errors() -> None:
    got = failure_summary([trace("x", False, "e" * 500)])
    assert len(got[0]["error"]) == 160


def test_failure_text_reads_naturally() -> None:
    assert _failure_text([]) == "无"
    text = _failure_text([{"tool": "source.fetch", "n": 2, "error": "403", "task": "e3p-02"}])
    assert "source.fetch ×2" in text and "e3p-02" in text and "403" in text


def test_task_failure_text_combines_conversation_error_and_tools() -> None:
    assert _task_failure_text({"error": None, "failures": []}) == "—"
    assert _task_failure_text({"error": "boom", "failures": []}) == "boom"
    both = _task_failure_text({"error": "boom", "failures": [{"tool": "t", "n": 3}]})
    assert "boom" in both and "t ×3" in both


def e3_payload(**overrides: object) -> dict:
    """一份最小的 E3 结果（键名与 run_e3 的返回一致）。"""
    task = {
        "id": "e3p-02",
        "topic": "RAG",
        "stop_reason": "completed",
        "error": None,
        "tool_calls": 7,
        "elapsed_ms": 153100,
        "traces": 7,
        "tool_success_rate": 0.857,
        "latency_p50": 14376,
        "latency_p95": 35924,
        "artifacts": {"topic_maps": 1, "routes": 1},
        "first_turn_artifacts": 1,
        "request_asked_artifact": False,
        "unrequested_artifacts": False,
        "asked": True,
        "addressed_on_demand": True,
        "modes": ["model"],
        "degrade_reasons": [],
        "coverage": 0.928,
        "ungrounded_ratio": 0.071,
        "exercise_items": 4,
        "distinct_sources": 3,
        "counts": {"grounded": 13, "total": 14, "ungrounded": 1},
        "tokens": {"prompt": 30000, "completion": 5000},
        "refs": {},
        "failures": [{"tool": "source.fetch", "n": 1, "error": "Failed to fetch: 403"}],
    }
    payload = {
        "n": 1,
        "model": "deepseek-flash",
        "tool_success_rate": 0.857,
        "latency_p50": 14376,
        "latency_p95": 35924,
        "tool_calls_per_task": 7.0,
        "coverage": 0.928,
        "ungrounded_ratio": 0.071,
        "degraded_rate": 0.0,
        "exercise_items": 4,
        "distinct_sources": 3.0,
        "artifact_completeness": 1.0,
        "auto_artifact_rate": 0.0,
        "unrequested_artifact_rate": 0.0,
        "unrequested_denominator": 1,
        "asked_rate": 1.0,
        "on_demand_success_rate": 1.0,
        "arm": {"auto_artifacts": False, "asked_followup": True, "system_prompt_hash": "abc"},
        "counts": {"grounded": 13, "total": 14, "ungrounded": 1},
        "tokens": {"prompt": 30000, "completion": 5000},
        "failures": [{"task": "e3p-02", "tool": "source.fetch", "n": 1, "error": "403"}],
        "failed_tool_calls": 1,
        "tasks": [task],
    }
    payload.update(overrides)
    return payload


def test_report_shows_the_failing_tool() -> None:
    """端到端：渲染出来的 Markdown 里必须能读到失败的工具与错误。"""
    markdown = render({"generated_at": "20261005-234323", "sets": {"e3": e3_payload()}})
    assert "失败的工具调用" in markdown
    assert "source.fetch ×1" in markdown
    assert "e3p-02" in markdown
    assert "错误 / 失败工具" in markdown


def test_report_says_none_when_nothing_failed() -> None:
    payload = e3_payload(failures=[], failed_tool_calls=0)
    payload["tasks"][0]["failures"] = []
    markdown = render({"generated_at": "x", "sets": {"e3": payload}})
    assert "| 无 |" in markdown
